"""统一图片生成服务 — 尾帧 / 首帧 / 自定义 / CC 编辑共用一个底座。

共享：Vertex genai client（按 project/location 记忆化）、超时包装、
图像步（generate_content IMAGE + 空响应处理 + 写文件 + 裁剪）、观测。
模式函数在后续任务中从 face_calibration_client 迁移进来（尾帧生成已迁移为
generate_tail_frame，首帧生成已迁移为 generate_first_frame）。
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from google import genai
from google.genai import types

from app.agents.frame_porter import center_crop_to_aspect
from app.config import settings
from app import observability

logger = logging.getLogger(__name__)

_clients: dict[tuple[str, str], genai.Client] = {}

# Minimum characters the CoT end-pose description must have before we accept it.
_COT_MIN_LEN = 30

# Phrases that signal the CoT tried to shortcut with a "no change" answer.
_COT_CONSERVATIVE_MARKERS = (
    "same as starting",
    "same as the starting",
    "unchanged",
    "no movement",
    "no change",
    "identical to the starting",
    "no visible change",
)


def _is_cot_too_weak(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < _COT_MIN_LEN:
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in _COT_CONSERVATIVE_MARKERS)


def get_client(project: Optional[str] = None, location: Optional[str] = None) -> genai.Client:
    """Memoized Vertex client per (project, location). Never uses API keys."""
    key = (project or settings.tf_project, location or settings.tf_location)
    if key not in _clients:
        _clients[key] = genai.Client(vertexai=True, project=key[0], location=key[1])
    return _clients[key]


def _mime_for(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "image/png"


def _extract_text(response) -> str:
    out = ""
    for part in response.parts:
        if part.text:
            out += part.text
    return out


async def _call_with_timeout(
    coro_factory: Callable[[], Awaitable], *, label: str, timeout: int = 120
):
    """Await a model call with a hard timeout, turning a timeout into a clear error."""
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{label} timed out after {timeout}s (network issue)")


def parts_from_paths(paths: Optional[List[str]]) -> list:
    """Image Parts from file paths; silently skips missing files."""
    parts: list = []
    for p in paths or []:
        fp = Path(p)
        if fp.exists():
            parts.append(
                types.Part.from_bytes(data=fp.read_bytes(), mime_type=_mime_for(p))
            )
    return parts


async def run_image_step(
    *,
    image_parts: list,
    prompt: str,
    output_path: str,
    span_name: str,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    pin_aspect: bool = False,
    temperature: Optional[float] = None,
    client: Optional[genai.Client] = None,
) -> str:
    """One IMAGE generation call: parts+prompt → image file (+ optional center-crop).

    aspect_ratio=None 表示不裁剪（CC 编辑保持原图尺寸）。pin_aspect=True 时
    额外用 ImageConfig 钉住输出方向（首帧生成的既有行为）。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    client = client or get_client()
    model = model or settings.tf_model

    config_kwargs: dict = {"response_modalities": ["IMAGE"]}
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if pin_aspect and aspect_ratio:
        config_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)

    all_parts = image_parts + [types.Part(text=prompt)]
    model_params = dict(config_kwargs)
    model_params.pop("image_config", None)

    with observability.generation(
        name=span_name,
        model=model,
        input={"prompt": prompt[:500], "num_image_parts": len(image_parts)},
        model_parameters=model_params,
    ) as gen:
        response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=all_parts)],
                config=types.GenerateContentConfig(**config_kwargs),
            ),
            label=f"{span_name} API call",
        )

        saved = False
        parts = response.parts or []
        if not parts:
            block_reason = getattr(response, "prompt_feedback", None)
            candidates = getattr(response, "candidates", None)
            finish_reason = None
            if candidates:
                finish_reason = getattr(candidates[0], "finish_reason", None)
            logger.error(
                "%s returned no parts. block_reason=%s finish_reason=%s candidates=%s",
                span_name, block_reason, finish_reason, candidates,
            )
            raise RuntimeError(
                f"Gemini returned empty response (blocked or filtered). "
                f"block_reason={block_reason}, finish_reason={finish_reason}"
            )

        for part in parts:
            if part.inline_data is not None:
                Path(output_path).write_bytes(part.inline_data.data)
                saved = True
                logger.info("%s: saved %s", span_name, output_path)
                break
            if part.text is not None:
                logger.info("%s text response: %s", span_name, part.text[:200])

        if not saved:
            raise RuntimeError(
                "Gemini did not return an image. "
                f"Response parts: {[type(p).__name__ for p in parts]}"
            )

        observability.update_span(gen, output={"output_path": output_path})

    if aspect_ratio:
        center_crop_to_aspect(output_path, aspect_ratio)
    return output_path


async def generate_custom(
    prompt: str,
    output_path: str,
    character_ref_paths: Optional[List[str]] = None,
    object_ref_paths: Optional[List[str]] = None,
    context_frame_path: Optional[str] = None,
    aspect_ratio: str = "9:16",
) -> str:
    """自定义提示词单步直出：用户提示词即最终提示词，不走 CoT。

    图片顺序沿用两步链的约定：context → object → character（身份最后、最强条件）。
    """
    image_parts = parts_from_paths([context_frame_path] if context_frame_path else [])
    image_parts += parts_from_paths(object_ref_paths)
    image_parts += parts_from_paths(character_ref_paths)
    return await run_image_step(
        image_parts=image_parts,
        prompt=prompt,
        output_path=output_path,
        span_name="services-custom-image-generate",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,
        temperature=1.0,
    )


async def generate_tail_frame(
    character_ref_paths: List[str],
    first_frame_path: Optional[str],
    motion_prompt: str,
    output_path: str,
    object_ref_paths: Optional[List[str]] = None,
    aspect_ratio: str = "9:16",
    on_cot_complete: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """Generate a target tail frame based on the director's motion prompt.

    Two-step process using gemini-3.1-flash-image-preview:
      Step 1 (TEXT): Analyze the motion prompt to derive the final pose.
      Step 2 (IMAGE): Generate the tail frame image with the analyzed pose.

    Args:
        character_ref_paths: Paths to character reference images (identity).
        first_frame_path: Path to the shot's first frame (starting state).
        motion_prompt: Director-generated motion prompt describing the action.
        output_path: Path to write the generated tail frame image.
        object_ref_paths: Optional paths to object/prop reference images.

    Returns:
        The output_path.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    client = get_client()
    model = settings.tf_model

    # --- Collect image parts once, keyed by role ---
    char_parts = parts_from_paths(character_ref_paths)
    obj_parts = parts_from_paths(object_ref_paths)
    first_frame_parts = parts_from_paths([first_frame_path] if first_frame_path else [])

    logger.info(
        "TF: calling %s  char_refs=%d  obj_refs=%d  first_frame=%s",
        model,
        len(character_ref_paths),
        len(object_ref_paths) if object_ref_paths else 0,
        first_frame_path,
    )

    # --- Step 1: CoT analysis (TEXT only) — use text model to reason about end pose ---
    # CoT only sees text output; image order doesn't affect conditioning weight,
    # so keep the legacy order here.
    cot_image_parts = char_parts + obj_parts + first_frame_parts
    cot_prompt = settings.tf_cot_prompt.format(motion_prompt=motion_prompt)
    cot_parts = cot_image_parts + [types.Part(text=cot_prompt)]

    with observability.generation(
        name="services-tail-frame-cot-analysis",
        model=settings.tf_cot_model,
        input={"motion_prompt": motion_prompt},
        model_parameters={"temperature": 0.6},
    ) as cot_gen:
        cot_response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=settings.tf_cot_model,
                contents=[types.Content(role="user", parts=cot_parts)],
                config=types.GenerateContentConfig(temperature=0.6),
            ),
            label="TF CoT API call",
        )
        end_pose = _extract_text(cot_response)
        observability.update_span(cot_gen, output=end_pose)
    logger.info("TF CoT end pose: %s", end_pose[:500])

    # Retry once if the CoT produced empty/too-short/conservative output — those
    # correlate strongly with the image step outputting a near-copy of an input
    # image. Re-roll the SAME prompt at a higher temperature so the resample
    # actually diverges from the weak first answer.
    if _is_cot_too_weak(end_pose):
        logger.warning("TF CoT output too weak, re-rolling at higher temperature")
        retry_response = await client.aio.models.generate_content(
            model=settings.tf_cot_model,
            contents=[types.Content(role="user", parts=cot_parts)],
            config=types.GenerateContentConfig(temperature=0.9),
        )
        retry_text = _extract_text(retry_response)
        logger.info("TF CoT retry end pose: %s", retry_text[:500])
        if not _is_cot_too_weak(retry_text):
            end_pose = retry_text
        else:
            # Hard fallback: make sure the image step still sees a strong
            # "must differ" instruction even when CoT keeps failing.
            end_pose = (
                "The character MUST be in a pose visibly different from the "
                "starting frame (different head angle, hand position, or eye "
                "direction). Base the end pose on this action: "
                f"{motion_prompt}"
            )

    if on_cot_complete:
        await on_cot_complete(end_pose)

    # --- Step 2: Image generation (IMAGE only) with CoT result ---
    # Reordered: [first_frame] (context) → [object refs] → [character refs] (identity).
    # Putting first_frame last was over-conditioning the model into "edit-this-image"
    # behavior, causing it to copy the starting pose. Character ref is now last so
    # facial identity stays strong, paired with an explicit "only features, not pose"
    # instruction in tf_prompt.
    img_image_parts = first_frame_parts + obj_parts + char_parts
    img_prompt = settings.tf_prompt.format(motion_prompt=motion_prompt, end_pose=end_pose)
    await run_image_step(
        image_parts=img_image_parts,
        prompt=img_prompt,
        output_path=output_path,
        span_name="services-tail-frame-generate-image",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,        # 恢复原实现的方向钉：防横版返回导致 9:16 裁切成放大观感
        temperature=1.0,
    )
    return output_path


async def generate_first_frame(
    character_ref_paths: List[str],
    context_frame_path: Optional[str],
    visual_description: str,
    shot_type: str,
    output_path: str,
    motion_prompt: Optional[str] = None,
    object_ref_paths: Optional[List[str]] = None,
    aspect_ratio: str = "9:16",
) -> str:
    """Generate an opening first frame for a shot.

    Args:
        character_ref_paths: Paths to character reference images (identity).
        context_frame_path: Optional context frame — this may be either the
            CURRENT opening frame (forward continuity) or the shot's ENDING
            frame (backward continuity) — used only for background/lighting/
            wardrobe continuity, never for pose.
        visual_description: The shot's visual description (primary input).
        shot_type: Close-up / Medium Shot / Wide Shot (framing hint).
        output_path: Path to write the generated first frame image.
        motion_prompt: Optional motion prompt — the opening frame must be a
            natural starting point for it.
        object_ref_paths: Optional paths to object/prop reference images.

    Returns:
        The output_path.
    """
    client = get_client()
    model = settings.tf_model

    # --- Collect image parts once, keyed by role ---
    char_parts = parts_from_paths(character_ref_paths)
    obj_parts = parts_from_paths(object_ref_paths)
    context_parts = parts_from_paths([context_frame_path] if context_frame_path else [])

    logger.info(
        "FF: calling %s  char_refs=%d  obj_refs=%d  context_frame=%s",
        model,
        len(character_ref_paths),
        len(object_ref_paths) if object_ref_paths else 0,
        context_frame_path,
    )

    # --- Step 1: CoT analysis (TEXT only) — reason about the opening composition ---
    cot_image_parts = char_parts + obj_parts + context_parts
    cot_prompt = settings.ff_cot_prompt.format(
        shot_type=shot_type,
        visual_description=visual_description,
        motion_prompt=motion_prompt or "(not specified)",
    )
    cot_parts = cot_image_parts + [types.Part(text=cot_prompt)]

    with observability.generation(
        name="services-first-frame-cot-analysis",
        model=settings.tf_cot_model,
        input={"visual_description": visual_description, "shot_type": shot_type},
        model_parameters={"temperature": 0.6},
    ) as cot_gen:
        cot_response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=settings.tf_cot_model,
                contents=[types.Content(role="user", parts=cot_parts)],
                config=types.GenerateContentConfig(temperature=0.6),
            ),
            label="FF CoT API call",
        )
        opening_composition = _extract_text(cot_response)
        observability.update_span(cot_gen, output=opening_composition)
    logger.info("FF CoT opening composition: %s", opening_composition[:500])

    if not opening_composition.strip():
        # Hard fallback: keep the image step grounded in the shot description.
        opening_composition = (
            f"A {shot_type} opening frame matching: {visual_description}"
        )

    # --- Step 2: Image generation (IMAGE only) with CoT result ---
    # Same ordering rationale as tail frame: context first, character identity last.
    img_image_parts = context_parts + obj_parts + char_parts
    img_prompt = settings.ff_prompt.format(
        visual_description=visual_description,
        opening_composition=opening_composition,
    )
    await run_image_step(
        image_parts=img_image_parts,
        prompt=img_prompt,
        output_path=output_path,
        span_name="services-first-frame-generate-image",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,        # 保持旧行为：首帧钉 ImageConfig
        temperature=1.0,
    )
    return output_path


async def calibrate_face(
    reference_image_paths: List[str],
    source_frame_path: str,
    output_frame_path: str,
) -> str:
    """CC 人脸校准（edit 模式）：只换脸部身份，姿态/背景保持 BASE 帧。

    身份参考在前、BASE 帧最后 — 模型把 "edit-this-image" 锚定在最后一张图上，
    帧在最后其姿态才被保留（帧在前会导致复制参考图姿态）。不做裁剪。
    """
    image_parts = parts_from_paths(reference_image_paths)
    image_parts += parts_from_paths([source_frame_path])
    logger.info(
        "CC: calling %s refs=%d frame=%s",
        settings.cc_model, len(reference_image_paths), source_frame_path,
    )
    return await run_image_step(
        image_parts=image_parts,
        prompt=settings.cc_prompt,
        output_path=output_frame_path,
        span_name="services-face-calibration-generate-image",
        model=settings.cc_model,
        aspect_ratio=None,
        client=get_client(settings.cc_project, settings.cc_location),
    )

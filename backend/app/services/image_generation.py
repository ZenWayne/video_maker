"""统一图片生成服务 — 尾帧 / 首帧 / 自定义 / CC 编辑共用一个底座。

共享：Vertex genai client（按 project/location 记忆化）、超时包装、
图像步（generate_content IMAGE + 空响应处理 + 写文件 + 裁剪）、观测。
模式函数在后续任务中从 tail_frame_generator / first_frame_generator /
face_calibration_client 迁移进来。
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

"""Unit tests for tail frame generation and video generator last_frame support."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_generate_videos_config_last_frame_field():
    """GenerateVideosConfig accepts last_frame as a valid field."""
    from google.genai import types

    config = types.GenerateVideosConfig(
        aspect_ratio="16:9",
        duration_seconds=6,
        number_of_videos=1,
        last_frame=types.Image(image_bytes=b"\x89PNG"),
    )
    assert config.last_frame is not None


@pytest.mark.asyncio
async def test_last_frame_set_in_image_to_video_mode():
    """When last_frame_path is provided in image-to-video mode, config.last_frame is set."""
    from app.agents.video_generator import generate_video

    captured_config = None

    async def fake_generate_videos(model, prompt, image, config):
        nonlocal captured_config
        captured_config = config
        op = MagicMock()
        op.done = True
        op.error = None
        video = MagicMock()
        video.video_bytes = b"\x00" * 1024
        op.response.generated_videos = [MagicMock(video=video)]
        return op

    mock_client = MagicMock()
    mock_client.aio.models.generate_videos = fake_generate_videos

    with patch("app.agents.video_generator._get_veo_client", return_value=mock_client), \
         patch("google.genai.types.Image") as mock_image:
        mock_image.from_file.return_value = MagicMock()
        await generate_video(
            client=None,
            motion_prompt="Walk forward",
            first_frame_path="/fake/first.png",
            shot_duration=6,
            spoken_text="",
            last_frame_path="/fake/last.png",
        )

    assert captured_config is not None
    assert captured_config.last_frame is not None
    # Image.from_file called for both first and last frame
    assert mock_image.from_file.call_count == 2


@pytest.mark.asyncio
async def test_last_frame_not_set_in_asset_mode():
    """When reference_image_paths are provided (ASSET mode), last_frame is NOT set."""
    from app.agents.video_generator import generate_video

    captured_config = None

    async def fake_generate_videos(model, prompt, config):
        nonlocal captured_config
        captured_config = config
        op = MagicMock()
        op.done = True
        op.error = None
        video = MagicMock()
        video.video_bytes = b"\x00" * 1024
        op.response.generated_videos = [MagicMock(video=video)]
        return op

    mock_client = MagicMock()
    mock_client.aio.models.generate_videos = fake_generate_videos

    with patch("app.agents.video_generator._get_veo_client", return_value=mock_client), \
         patch("google.genai.types.Image") as mock_image, \
         patch("google.genai.types.VideoGenerationReferenceImage") as mock_ref_image, \
         patch("google.genai.types.VideoGenerationReferenceType") as mock_ref_type:
        mock_image.from_file.return_value = MagicMock()
        mock_ref_image.return_value = MagicMock()
        await generate_video(
            client=None,
            motion_prompt="Walk forward",
            first_frame_path=None,
            shot_duration=8,
            spoken_text="",
            reference_image_paths=["/fake/ref1.png", "/fake/ref2.png"],
            last_frame_path="/fake/last.png",
        )

    assert captured_config is not None
    assert captured_config.last_frame is None


@pytest.mark.asyncio
async def test_last_frame_not_set_when_none():
    """When last_frame_path is None, config.last_frame stays None."""
    from app.agents.video_generator import generate_video

    captured_config = None

    async def fake_generate_videos(model, prompt, image, config):
        nonlocal captured_config
        captured_config = config
        op = MagicMock()
        op.done = True
        op.error = None
        video = MagicMock()
        video.video_bytes = b"\x00" * 1024
        op.response.generated_videos = [MagicMock(video=video)]
        return op

    mock_client = MagicMock()
    mock_client.aio.models.generate_videos = fake_generate_videos

    with patch("app.agents.video_generator._get_veo_client", return_value=mock_client), \
         patch("google.genai.types.Image") as mock_image:
        mock_image.from_file.return_value = MagicMock()
        await generate_video(
            client=None,
            motion_prompt="Walk forward",
            first_frame_path="/fake/first.png",
            shot_duration=6,
            spoken_text="",
            last_frame_path=None,
        )

    assert captured_config is not None
    assert captured_config.last_frame is None


def _is_image_call(config) -> bool:
    """A request targets the image step iff its config opts into IMAGE modality."""
    modalities = getattr(config, "response_modalities", None) or []
    return "IMAGE" in modalities


def _make_fake_generate_content(captured, image_bytes: bytes, cot_text: str):
    """Build a fake that returns real text for CoT calls and an image for the image call."""

    async def fake(model, contents, config):
        if _is_image_call(config):
            captured["image_contents"] = contents
            part = MagicMock()
            part.inline_data = MagicMock()
            part.inline_data.data = image_bytes
            part.text = None
            resp = MagicMock()
            resp.parts = [part]
            return resp
        captured.setdefault("cot_calls", []).append(contents)
        text_part = MagicMock()
        text_part.inline_data = None
        text_part.text = cot_text
        resp = MagicMock()
        resp.parts = [text_part]
        return resp

    return fake


@pytest.mark.asyncio
async def test_tail_frame_generator_builds_correct_prompt(tmp_path):
    """generate_tail_frame passes first_frame, object refs, character refs, then prompt text."""
    from app.services.tail_frame_generator import generate_tail_frame

    char_ref = tmp_path / "char.png"
    char_ref.write_bytes(b"\x89PNG_char")
    obj_ref = tmp_path / "obj.png"
    obj_ref.write_bytes(b"\x89PNG_obj")
    first_frame = tmp_path / "first.png"
    first_frame.write_bytes(b"\x89PNG_first")
    output = tmp_path / "output.png"

    captured = {}
    cot_text = (
        "Head tilted 15 degrees right, right hand raised to chin level, "
        "torso leaning forward 10 degrees, eyes looking down-left."
    )
    fake = _make_fake_generate_content(captured, b"\x89PNG_generated", cot_text)

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake

    with patch("app.services.tail_frame_generator._get_client", return_value=mock_client), \
         patch("app.services.tail_frame_generator.center_crop_to_aspect"):
        result = await generate_tail_frame(
            character_ref_paths=[str(char_ref)],
            first_frame_path=str(first_frame),
            motion_prompt="角色向前走两步",
            output_path=str(output),
            object_ref_paths=[str(obj_ref)],
        )

    assert result == str(output)
    assert output.read_bytes() == b"\x89PNG_generated"

    # Strong CoT output → no retry → exactly one CoT call + one image call.
    assert len(captured["cot_calls"]) == 1

    # Image-step order: [first_frame, obj, char, text] — first_frame up front as
    # context, character ref last for identity conditioning.
    image_parts = captured["image_contents"][0].parts
    assert len(image_parts) == 4
    assert image_parts[0].inline_data.data == b"\x89PNG_first"
    assert image_parts[1].inline_data.data == b"\x89PNG_obj"
    assert image_parts[2].inline_data.data == b"\x89PNG_char"
    assert "角色向前走两步" in image_parts[-1].text
    # CoT result must be injected into the image prompt.
    assert "Head tilted 15 degrees right" in image_parts[-1].text


@pytest.mark.asyncio
async def test_tail_frame_generator_no_object_refs(tmp_path):
    """generate_tail_frame works without object reference images."""
    from app.services.tail_frame_generator import generate_tail_frame

    char_ref = tmp_path / "char.png"
    char_ref.write_bytes(b"\x89PNG_char")
    first_frame = tmp_path / "first.png"
    first_frame.write_bytes(b"\x89PNG_first")
    output = tmp_path / "output.png"

    captured = {}
    cot_text = (
        "Head turned 20 degrees left, right arm lowered, left hand relaxed, "
        "eyes focused straight ahead, lips slightly parted."
    )
    fake = _make_fake_generate_content(captured, b"\x89PNG_result", cot_text)

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake

    with patch("app.services.tail_frame_generator._get_client", return_value=mock_client), \
         patch("app.services.tail_frame_generator.center_crop_to_aspect"):
        await generate_tail_frame(
            character_ref_paths=[str(char_ref)],
            first_frame_path=str(first_frame),
            motion_prompt="Test prompt",
            output_path=str(output),
            object_ref_paths=None,
        )

    # 1 first_frame + 1 char_ref + 1 text = 3 parts
    image_parts = captured["image_contents"][0].parts
    assert len(image_parts) == 3


@pytest.mark.asyncio
async def test_tail_frame_cot_retries_on_empty(tmp_path):
    """When the first CoT returns empty text, generator retries with the stronger prompt."""
    from app.services.tail_frame_generator import generate_tail_frame

    char_ref = tmp_path / "char.png"
    char_ref.write_bytes(b"\x89PNG_char")
    first_frame = tmp_path / "first.png"
    first_frame.write_bytes(b"\x89PNG_first")
    output = tmp_path / "output.png"

    call_log: list[str] = []

    async def fake(model, contents, config):
        modalities = getattr(config, "response_modalities", None) or []
        if "IMAGE" in modalities:
            call_log.append("image")
            part = MagicMock()
            part.inline_data = MagicMock()
            part.inline_data.data = b"\x89PNG_generated"
            part.text = None
            resp = MagicMock()
            resp.parts = [part]
            return resp

        call_log.append("cot")
        text_part = MagicMock()
        text_part.inline_data = None
        if call_log.count("cot") == 1:
            text_part.text = ""  # first CoT empty → triggers retry
        else:
            text_part.text = (
                "Head turned 30 degrees right, right hand placed on desk, "
                "eyes closed, torso upright."
            )
        resp = MagicMock()
        resp.parts = [text_part]
        return resp

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake

    with patch("app.services.tail_frame_generator._get_client", return_value=mock_client), \
         patch("app.services.tail_frame_generator.center_crop_to_aspect"):
        await generate_tail_frame(
            character_ref_paths=[str(char_ref)],
            first_frame_path=str(first_frame),
            motion_prompt="Walk forward",
            output_path=str(output),
        )

    # Expect: CoT → retry CoT → image
    assert call_log == ["cot", "cot", "image"]


@pytest.mark.asyncio
async def test_tail_frame_generator_raises_on_no_image(tmp_path):
    """generate_tail_frame raises when Gemini returns no image."""
    from app.services.tail_frame_generator import generate_tail_frame

    char_ref = tmp_path / "char.png"
    char_ref.write_bytes(b"\x89PNG")
    output = tmp_path / "output.png"

    async def fake_generate_content(model, contents, config):
        part = MagicMock()
        part.inline_data = None
        part.text = "Sorry, I cannot generate that image."
        resp = MagicMock()
        resp.parts = [part]
        return resp

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake_generate_content

    with patch("app.services.tail_frame_generator._get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="did not return an image"):
            await generate_tail_frame(
                character_ref_paths=[str(char_ref)],
                first_frame_path=None,
                motion_prompt="Test",
                output_path=str(output),
            )

"""Unit tests for video_generator agent.

These tests verify that GenerateVideosConfig is constructed with only valid fields
(catching cases where the SDK API changes and removes a field we depend on),
and that the prompt is assembled correctly.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_generate_videos_config_valid_fields():
    """GenerateVideosConfig must not raise with the fields we actually pass."""
    from google.genai import types

    # This will raise pydantic.ValidationError if any field is invalid.
    config = types.GenerateVideosConfig(
        aspect_ratio="16:9",
        duration_seconds=6,
        number_of_videos=1,
    )
    assert config.number_of_videos == 1


def test_generate_videos_config_rejects_spoken_text():
    """spoken_text is NOT a valid field — construction must raise."""
    from google.genai import types
    import pydantic

    with pytest.raises((pydantic.ValidationError, TypeError)):
        types.GenerateVideosConfig(
            aspect_ratio="16:9",
            duration_seconds=6,
            generate_audio=True,
            spoken_text="some dialogue",  # invalid field
            number_of_videos=1,
        )


@pytest.mark.asyncio
async def test_spoken_text_appended_to_prompt():
    """When spoken_text is provided it is appended to the motion prompt."""
    from app.agents.video_generator import generate_video

    captured_prompt = None

    async def fake_generate_videos(model, prompt, image, config):
        nonlocal captured_prompt
        captured_prompt = prompt
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
            motion_prompt="A cat walks forward",
            first_frame_path="/fake/frame.png",
            shot_duration=6,
            spoken_text="Hello world",
        )

    assert captured_prompt is not None
    assert "A cat walks forward" in captured_prompt
    assert "Hello world" in captured_prompt


@pytest.mark.asyncio
async def test_empty_spoken_text_not_appended():
    """When spoken_text is blank, prompt is left unchanged."""
    from app.agents.video_generator import generate_video

    captured_prompt = None

    async def fake_generate_videos(model, prompt, image, config):
        nonlocal captured_prompt
        captured_prompt = prompt
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
            motion_prompt="A cat walks forward",
            first_frame_path="/fake/frame.png",
            shot_duration=6,
            spoken_text="   ",
        )

    assert captured_prompt == "A cat walks forward"

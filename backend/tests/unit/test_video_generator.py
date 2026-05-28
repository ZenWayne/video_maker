"""Unit tests for the video_generator agent.

Covers:
* GenerateVideosConfig field validation (catches SDK API drift).
* Prompt assembly (spoken dialogue appended to motion prompt).
* Provider selection via settings.video_provider.
* The kie.ai REST provider flow (upload -> generate -> poll -> download),
  with httpx fully mocked so no real API is hit.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.agents.video_generator as vg
from app.agents.video_generator import (
    KieVeoProvider,
    VertexVeoProvider,
    VideoGenerationError,
    _clamp_kie_duration,
    generate_video,
    get_video_provider,
)


# --------------------------------------------------------------------------- #
# GenerateVideosConfig validation
# --------------------------------------------------------------------------- #

def test_generate_videos_config_valid_fields():
    """GenerateVideosConfig must not raise with the fields we actually pass."""
    from google.genai import types

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


# --------------------------------------------------------------------------- #
# Vertex provider — prompt assembly
# --------------------------------------------------------------------------- #

def _vertex_op_with_bytes(captured: dict):
    async def fake_generate_videos(model, prompt, image, config):
        captured["prompt"] = prompt
        op = MagicMock()
        op.done = True
        op.error = None
        video = MagicMock()
        video.video_bytes = b"\x00" * 1024
        op.response.generated_videos = [MagicMock(video=video)]
        return op
    return fake_generate_videos


@pytest.mark.asyncio
async def test_spoken_text_appended_to_prompt():
    """When spoken_text is provided it is appended to the motion prompt."""
    captured: dict = {}
    mock_client = MagicMock()
    mock_client.aio.models.generate_videos = _vertex_op_with_bytes(captured)

    with patch.object(VertexVeoProvider, "_client", return_value=mock_client), \
         patch("app.agents.video_generator.center_crop_to_aspect", side_effect=lambda p, *a, **k: p), \
         patch("google.genai.types.Image") as mock_image:
        mock_image.from_file.return_value = MagicMock()
        await generate_video(
            client=None,
            motion_prompt="A cat walks forward",
            first_frame_path="/fake/frame.png",
            shot_duration=6,
            spoken_text="Hello world",
        )

    assert "A cat walks forward" in captured["prompt"]
    assert "Hello world" in captured["prompt"]


@pytest.mark.asyncio
async def test_empty_spoken_text_not_appended():
    """When spoken_text is blank, prompt is left unchanged."""
    captured: dict = {}
    mock_client = MagicMock()
    mock_client.aio.models.generate_videos = _vertex_op_with_bytes(captured)

    with patch.object(VertexVeoProvider, "_client", return_value=mock_client), \
         patch("app.agents.video_generator.center_crop_to_aspect", side_effect=lambda p, *a, **k: p), \
         patch("google.genai.types.Image") as mock_image:
        mock_image.from_file.return_value = MagicMock()
        await generate_video(
            client=None,
            motion_prompt="A cat walks forward",
            first_frame_path="/fake/frame.png",
            shot_duration=6,
            spoken_text="   ",
        )

    assert captured["prompt"] == "A cat walks forward"


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #

def test_default_provider_is_vertex(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "vertex")
    assert isinstance(get_video_provider(), VertexVeoProvider)


def test_kie_provider_selected(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "kie")
    assert isinstance(get_video_provider(), KieVeoProvider)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "bogus")
    with pytest.raises(VideoGenerationError):
        get_video_provider()


# --------------------------------------------------------------------------- #
# kie.ai duration clamping + mode mapping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("requested,expected", [
    (3, 4), (4, 4), (5, 4), (6, 6), (7, 6), (8, 8), (10, 8),
])
def test_clamp_kie_duration(requested, expected):
    assert _clamp_kie_duration(requested) == expected


def test_resolve_mode_text():
    mode, imgs, model = KieVeoProvider._resolve_mode(None, None, None)
    assert mode == "TEXT_2_VIDEO"
    assert imgs == []


def test_resolve_mode_first_frame_only():
    mode, imgs, _ = KieVeoProvider._resolve_mode("/a.png", None, None)
    assert mode == "FIRST_AND_LAST_FRAMES_2_VIDEO"
    assert imgs == ["/a.png"]


def test_resolve_mode_first_and_last():
    mode, imgs, _ = KieVeoProvider._resolve_mode("/a.png", "/b.png", None)
    assert mode == "FIRST_AND_LAST_FRAMES_2_VIDEO"
    assert imgs == ["/a.png", "/b.png"]


def test_resolve_mode_reference_forces_fast_and_caps_three():
    mode, imgs, model = KieVeoProvider._resolve_mode(
        "/a.png", "/b.png", ["/r1.png", "/r2.png", "/r3.png", "/r4.png"],
    )
    assert mode == "REFERENCE_2_VIDEO"
    assert imgs == ["/r1.png", "/r2.png", "/r3.png"]  # capped at 3
    assert model == "veo3_fast"


# --------------------------------------------------------------------------- #
# kie.ai full REST flow (httpx mocked)
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Routes kie.ai endpoints to canned responses; records the calls."""

    def __init__(self, record, success_flag=1, **kwargs):
        self.record = record
        self.success_flag = success_flag

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        if "file-base64-upload" in url:
            self.record["uploads"].append(json)
            n = len(self.record["uploads"])
            return _FakeResponse({"success": True, "data": {"downloadUrl": f"https://h/up{n}.png"}})
        if "/veo/generate" in url:
            self.record["generate"] = json
            return _FakeResponse({"code": 200, "msg": "success", "data": {"taskId": "task_1"}})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, params=None):
        if "/veo/record-info" in url:
            self.record["polled"] = params
            return _FakeResponse({
                "data": {
                    "successFlag": self.success_flag,
                    "errorCode": 500,
                    "errorMessage": "boom" if self.success_flag != 1 else "",
                    "response": {"resultUrls": ["https://h/result.mp4"]},
                }
            })
        if url == "https://h/result.mp4":
            return _FakeResponse(content=b"MP4DATA")
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def kie_env(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "kie")
    monkeypatch.setattr(vg.settings, "kie_api_key", "test-key")
    monkeypatch.setattr(vg.settings, "kie_poll_interval_seconds", 0)
    # crop is a no-op so we don't need real images on disk for the prompt path
    monkeypatch.setattr(vg, "center_crop_to_aspect", lambda p, *a, **k: p)


def _patch_httpx(record, success_flag=1):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(record, success_flag=success_flag, **kwargs)
    return patch("app.agents.video_generator.httpx.AsyncClient", side_effect=factory)


@pytest.mark.asyncio
async def test_kie_image_to_video_flow(kie_env, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG fake")
    record = {"uploads": []}

    with _patch_httpx(record):
        out = await generate_video(
            motion_prompt="A cat walks forward",
            first_frame_path=str(frame),
            shot_duration=6,
            spoken_text="Meow",
            aspect_ratio="16:9",
        )

    assert out == b"MP4DATA"
    assert len(record["uploads"]) == 1  # only the first frame uploaded
    gen = record["generate"]
    assert gen["generationType"] == "FIRST_AND_LAST_FRAMES_2_VIDEO"
    assert gen["imageUrls"] == ["https://h/up1.png"]
    assert gen["duration"] == 6
    assert gen["aspect_ratio"] == "16:9"
    assert "Meow" in gen["prompt"]
    assert record["polled"] == {"taskId": "task_1"}


@pytest.mark.asyncio
async def test_kie_reference_mode_uploads_and_caps(kie_env, tmp_path):
    refs = []
    for i in range(4):
        p = tmp_path / f"r{i}.png"
        p.write_bytes(b"\x89PNG")
        refs.append(str(p))
    record = {"uploads": []}

    with _patch_httpx(record):
        out = await generate_video(
            motion_prompt="reference shot",
            first_frame_path=None,
            shot_duration=4,
            spoken_text="",
            reference_image_paths=refs,
        )

    assert out == b"MP4DATA"
    gen = record["generate"]
    assert gen["generationType"] == "REFERENCE_2_VIDEO"
    assert gen["model"] == "veo3_fast"
    assert len(gen["imageUrls"]) == 3        # capped at 3
    assert len(record["uploads"]) == 3
    assert gen["duration"] == 8              # reference mode forces 8s


@pytest.mark.asyncio
async def test_kie_text_to_video_no_upload(kie_env):
    record = {"uploads": []}
    with _patch_httpx(record):
        out = await generate_video(
            motion_prompt="text only shot",
            first_frame_path=None,
            shot_duration=8,
            spoken_text="",
        )
    assert out == b"MP4DATA"
    assert record["uploads"] == []
    assert "imageUrls" not in record["generate"]
    assert record["generate"]["generationType"] == "TEXT_2_VIDEO"


@pytest.mark.asyncio
async def test_kie_failure_flag_raises(kie_env, tmp_path):
    record = {"uploads": []}
    with _patch_httpx(record, success_flag=2):
        with pytest.raises(VideoGenerationError, match="boom"):
            await generate_video(
                motion_prompt="doomed shot",
                first_frame_path=None,
                shot_duration=8,
                spoken_text="",
            )


@pytest.mark.asyncio
async def test_kie_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "kie")
    monkeypatch.setattr(vg.settings, "kie_api_key", "")
    with pytest.raises(VideoGenerationError, match="API key"):
        await generate_video(
            motion_prompt="x",
            first_frame_path=None,
            shot_duration=8,
            spoken_text="",
        )

"""统一图片服务底座：run_image_step / parts_from_paths / generate_custom（mock genai client）."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_client_returning(parts):
    resp = MagicMock()
    resp.parts = parts
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


def _image_part(data=b"\x89PNGfake"):
    part = MagicMock()
    part.inline_data.data = data
    part.text = None
    return part


def test_parts_from_paths_skips_missing(tmp_path):
    from app.services.image_generation import parts_from_paths
    ok = tmp_path / "a.png"; ok.write_bytes(b"\x89PNG")
    parts = parts_from_paths([str(ok), str(tmp_path / "missing.png")])
    assert len(parts) == 1


def test_parts_from_paths_none():
    from app.services.image_generation import parts_from_paths
    assert parts_from_paths(None) == []


@pytest.mark.asyncio
async def test_run_image_step_writes_file(tmp_path):
    from app.services.image_generation import run_image_step
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part(b"IMGDATA")])
    with patch("app.services.image_generation.center_crop_to_aspect") as crop:
        result = await run_image_step(
            image_parts=[], prompt="p", output_path=str(out),
            span_name="test-span", aspect_ratio="9:16", client=client,
        )
    assert result == str(out)
    assert out.read_bytes() == b"IMGDATA"
    crop.assert_called_once_with(str(out), "9:16")


@pytest.mark.asyncio
async def test_run_image_step_no_crop_when_no_aspect(tmp_path):
    from app.services.image_generation import run_image_step
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part()])
    with patch("app.services.image_generation.center_crop_to_aspect") as crop:
        await run_image_step(
            image_parts=[], prompt="p", output_path=str(out),
            span_name="test-span", client=client,
        )
    crop.assert_not_called()


@pytest.mark.asyncio
async def test_run_image_step_empty_parts_raises(tmp_path):
    from app.services.image_generation import run_image_step
    client = _mock_client_returning([])
    with pytest.raises(RuntimeError, match="blocked or filtered"):
        await run_image_step(
            image_parts=[], prompt="p", output_path=str(tmp_path / "x.png"),
            span_name="test-span", client=client,
        )


@pytest.mark.asyncio
async def test_generate_custom_part_order_and_prompt(tmp_path):
    """custom 模式：图片顺序 = context → object → character，提示词就是用户提示词。"""
    from app.services import image_generation as ig
    ctx = tmp_path / "ctx.png"; ctx.write_bytes(b"C")
    obj = tmp_path / "obj.png"; obj.write_bytes(b"O")
    char = tmp_path / "char.png"; char.write_bytes(b"H")
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part()])
    with patch.object(ig, "get_client", return_value=client), patch.object(ig, "center_crop_to_aspect"):
        await ig.generate_custom(
            prompt="my custom prompt",
            output_path=str(out),
            character_ref_paths=[str(char)],
            object_ref_paths=[str(obj)],
            context_frame_path=str(ctx),
            aspect_ratio="9:16",
        )
    call = client.aio.models.generate_content.await_args
    parts = call.kwargs["contents"][0].parts
    # 最后一个 part 是文本提示词，且原样使用用户输入
    assert parts[-1].text == "my custom prompt"
    # 图片顺序：context(C) → object(O) → character(H)
    datas = [p.inline_data.data for p in parts[:-1]]
    assert datas == [b"C", b"O", b"H"]

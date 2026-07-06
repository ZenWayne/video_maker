"""cc_edit 模式：part 顺序（refs 前、BASE 帧最后）+ 不裁剪 + cc 模型."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_calibrate_face_order_model_and_no_crop(tmp_path):
    from app.services import image_generation as ig
    from app.config import settings

    ref = tmp_path / "ref.png"; ref.write_bytes(b"R")
    src = tmp_path / "last_frame.png"; src.write_bytes(b"S")
    out = tmp_path / "cc_out.png"

    part = MagicMock(); part.inline_data.data = b"CAL"; part.text = None
    resp = MagicMock(); resp.parts = [part]
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)

    with patch.object(ig, "get_client", return_value=client) as gc, \
         patch.object(ig, "center_crop_to_aspect") as crop:
        await ig.calibrate_face([str(ref)], str(src), str(out))

    gc.assert_called_once_with(settings.cc_project, settings.cc_location)
    crop.assert_not_called()
    assert out.read_bytes() == b"CAL"

    call = client.aio.models.generate_content.await_args
    assert call.kwargs["model"] == settings.cc_model
    parts = call.kwargs["contents"][0].parts
    assert parts[-1].text == settings.cc_prompt
    assert [p.inline_data.data for p in parts[:-1]] == [b"R", b"S"]  # ref 前、BASE 最后

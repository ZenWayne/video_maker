import subprocess
import pytest
from sqlalchemy import select
from app.models.project import Project
from tests.integration.conftest import HEADERS


def _wav_bytes(tmp_path):
    p = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(p)],
        check=True, capture_output=True,
    )
    return p.read_bytes()


async def test_upload_sets_file_clears_shot(client, make_project, db_session_factory, tmp_path):
    proj = await make_project()
    pid = proj["id"]

    # Seed a non-null reference_voice_shot_id directly in the DB so the upload
    # actually has something to clear (otherwise the assert trivially passes on a
    # fresh project that already has null).
    async with db_session_factory() as s:
        proj_row = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        proj_row.reference_voice_shot_id = 1
        await s.commit()

    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    r = await client.post(f"/api/projects/{pid}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    # Upload response must clear the shot reference and set the file path.
    assert body["reference_voice_path"] is not None
    assert body["reference_voice_shot_id"] is None

    # Verify the clearing also persisted to the DB.
    got = (await client.get(f"/api/projects/{pid}", headers=HEADERS)).json()
    assert got["reference_voice_shot_id"] is None
    assert got["reference_voice_path"] is not None


async def test_upload_rejects_bad_extension(client, make_project):
    proj = await make_project()
    files = {"file": ("x.txt", b"hello", "text/plain")}
    r = await client.post(f"/api/projects/{proj['id']}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 400


async def test_auto_toggle_requires_base_voice(client, make_project):
    proj = await make_project()
    r = await client.post(f"/api/projects/{proj['id']}/auto-voice-calibrate",
                          json={"enabled": True}, headers=HEADERS)
    assert r.status_code == 409


async def test_auto_toggle_ok_after_upload(client, make_project, tmp_path):
    proj = await make_project()
    pid = proj["id"]
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    await client.post(f"/api/projects/{pid}/reference-voice/upload",
                      files=files, headers=HEADERS)
    r = await client.post(f"/api/projects/{pid}/auto-voice-calibrate",
                          json={"enabled": True}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["auto_voice_calibrate"] is True


async def test_clear_resets_everything(client, make_project, tmp_path):
    proj = await make_project()
    pid = proj["id"]
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    await client.post(f"/api/projects/{pid}/reference-voice/upload",
                      files=files, headers=HEADERS)
    await client.post(f"/api/projects/{pid}/auto-voice-calibrate",
                      json={"enabled": True}, headers=HEADERS)
    r = await client.delete(f"/api/projects/{pid}/reference-voice", headers=HEADERS)
    assert r.status_code == 200
    got = (await client.get(f"/api/projects/{pid}", headers=HEADERS)).json()
    assert got["reference_voice_path"] is None
    assert got["reference_voice_shot_id"] is None
    assert got["auto_voice_calibrate"] is False

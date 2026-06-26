import subprocess
import pytest
from sqlalchemy import select
from app.models.project import Project, Shot
from tests.integration.conftest import HEADERS


def _wav_bytes(tmp_path):
    p = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(p)],
        check=True, capture_output=True,
    )
    return p.read_bytes()


async def _seed_completed_shot(db_session_factory, project_id: str, shot_id: int = 1):
    """Insert a completed shot with a fake video_path into the DB."""
    async with db_session_factory() as s:
        shot = Shot(
            project_id=project_id,
            shot_id=shot_id,
            text=f"Shot {shot_id} dialogue",
            shot_type="Medium Shot",
            visual_description=f"Visual description {shot_id}",
            shot_duration=6,
            status="completed",
            video_path="/fake/output.mp4",
        )
        s.add(shot)
        await s.commit()


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


# ── File base voice: manual VC endpoints ───────────────────────────────────────


async def test_voice_convert_shot_with_file_source(client, make_project, db_session_factory, tmp_path):
    """voice-convert endpoint must succeed when base voice is a file (not a shot)."""
    proj = await make_project()
    pid = proj["id"]

    # Upload a file base voice (sets reference_voice_path, clears reference_voice_shot_id)
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    r = await client.post(f"/api/projects/{pid}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["reference_voice_shot_id"] is None

    # Seed a completed shot
    await _seed_completed_shot(db_session_factory, pid, shot_id=1)

    # Manual voice-convert must return 202, not 400
    r = await client.post(f"/api/projects/{pid}/shots/1/voice-convert", headers=HEADERS)
    assert r.status_code == 202, f"Expected 202, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["status"] == "queued"
    assert body["shot_id"] == 1

    # arq should have been called
    client.arq.enqueue_job.assert_called()


async def test_voice_convert_all_with_file_source(client, make_project, db_session_factory, tmp_path):
    """voice-convert-all endpoint must succeed when base voice is a file (not a shot)."""
    proj = await make_project()
    pid = proj["id"]

    # Upload a file base voice
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    r = await client.post(f"/api/projects/{pid}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 200

    # Seed two completed shots (no reference_voice_shot_id, so both should be eligible)
    await _seed_completed_shot(db_session_factory, pid, shot_id=1)
    await _seed_completed_shot(db_session_factory, pid, shot_id=2)

    # voice-convert-all must return 202
    r = await client.post(f"/api/projects/{pid}/voice-convert-all", headers=HEADERS)
    assert r.status_code == 202, f"Expected 202, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["status"] == "queued"
    # Both shots must be enqueued
    assert sorted(body["shot_ids"]) == [1, 2]

    # arq batch job should have been called
    client.arq.enqueue_job.assert_called()


async def test_voice_convert_shot_no_base_voice_still_400(client, make_project, db_session_factory):
    """voice-convert must still 400 when no base voice is set at all."""
    proj = await make_project()
    pid = proj["id"]
    await _seed_completed_shot(db_session_factory, pid, shot_id=1)

    r = await client.post(f"/api/projects/{pid}/shots/1/voice-convert", headers=HEADERS)
    assert r.status_code == 400


async def test_voice_convert_all_no_base_voice_still_400(client, make_project):
    """voice-convert-all must still 400 when no base voice is set at all."""
    proj = await make_project()
    r = await client.post(f"/api/projects/{proj['id']}/voice-convert-all", headers=HEADERS)
    assert r.status_code == 400

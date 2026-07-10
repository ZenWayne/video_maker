"""Regression: the continuity preview must stay in sync when one shot has been
voice-cloned.

The user-visible bug: shot 1 played, then the picture froze on its last frame
the moment shot 2 began.  Cause: shot 2's effective clip was baked at the VC
wav's 24 kHz mono, shot 1's at 48 kHz stereo, and the concat demuxer + -c copy
wrote a single audio decoder config for both -- so shot 2's audio decoded as
garbage and the <video> element's audio-driven clock stalled.

Drives the real /join-preview endpoint against the real DB and real ffmpeg.
"""
import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services.storage import join_preview_path, shot_dir
from tests.ffprobe_helpers import decode_errors, make_vc_wav, stream_duration
from tests.integration.conftest import (
    HEADERS, _make_project, _add_shot, seed_shot_with_source,
)


@pytest.mark.asyncio
async def test_join_preview_stays_in_sync_with_voice_cloned_shot(
    client, db_session_factory
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="completed")
    # 120 frames @ 30fps = 4.0s each
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)
    await seed_shot_with_source(db_session_factory, pid, 2, frames=120)

    # shot 1: trimmed to 60 frames (2.0s).  shot 2: voice-cloned, untrimmed.
    # The wav is 5.0s -- longer than the 4.0s video, so -shortest bounds it.
    vc_wav = shot_dir(pid, 2) / "audio_vc.wav"
    make_vc_wav(vc_wav, seconds=5.0)
    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.trim_frames = 60
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        shot2.vc_audio_path = str(vc_wav)
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    previews = sorted(join_preview_path(pid).parent.glob("join_preview*.mp4"))
    assert previews, "no join preview produced"
    out = str(previews[-1])

    # The preview must decode cleanly end to end...
    assert decode_errors(out) == 0, "preview has audio decode errors"

    # ...and its audio must span the whole 2.0s + 4.0s timeline, not stop at
    # the segment boundary.
    v_dur = stream_duration(out, "v")
    a_dur = stream_duration(out, "a")
    assert v_dur == pytest.approx(6.0, abs=0.2), f"video {v_dur}"
    assert a_dur == pytest.approx(v_dur, abs=0.2), (
        f"audio {a_dur} does not span the video {v_dur}"
    )

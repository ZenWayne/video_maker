"""Regression: the continuity preview must stay in sync when one shot has been
voice-cloned.

The user-visible bug: shot 1 played, then the picture froze on its last frame
the moment shot 2 began.  Cause: shot 2's effective clip was baked at the VC
wav's 24 kHz mono, shot 1's at 48 kHz stereo, and the concat demuxer + -c copy
wrote a single audio decoder config for both -- so shot 2's audio decoded as
garbage and the <video> element's audio-driven clock stalled.

Drives the real /join-preview endpoint against the real DB, real COS, and real
ffmpeg.

Two scenarios: both shots edited (reproduces the incident), and only shot 2
edited (isolates the merge-side concat-filter fix, since shot 1 then reaches
merge_shots as a raw 44.1 kHz mono passthrough).

Rewritten for Task 11 (join-preview moved from local-storage-root paths to
workspace()+COS; vc_audio_path is now a COS key, published for real instead of
written to a local shot_dir). Gated on requires_cos/cos_prefix.
"""
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services import object_store
from app.services.storage import join_preview_key, shot_audio_vc_key
from tests.ffprobe_helpers import decode_errors, make_vc_wav, stream_duration
from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import (
    HEADERS, _make_project, _add_shot, seed_shot_with_source,
)

pytestmark = requires_cos


async def _publish_vc_wav(tmp_path_factory, pid: str, shot_id: int, seconds: float) -> str:
    """Synthesize a CosyVoice-shaped wav and publish it to the shot's VC key."""
    local = tmp_path_factory / f"vc_{shot_id}.wav"
    make_vc_wav(local, seconds=seconds)
    key = shot_audio_vc_key(pid, shot_id)
    return await object_store.put(key, local)


async def _download_join_preview(pid: str, td: Path) -> str:
    key = join_preview_key(pid)
    out = td / "preview.mp4"
    await object_store.get(key, out)
    return str(out)


@pytest.mark.asyncio
async def test_join_preview_stays_in_sync_with_voice_cloned_shot(
    client, db_session_factory, cos_prefix, tmp_path
):
    """Reproduces the reported incident: shot 1 trimmed, shot 2 voice-cloned.

    Both shots carry an edit, so build_effective_clip bakes and normalizes both
    before the merge.  That means this test cannot tell which of the two fixes
    regressed — the concat demuxer stitches already-homogeneous inputs cleanly.
    test_join_preview_stays_in_sync_when_only_one_shot_is_baked isolates the
    merge-side fix.
    """
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="completed")
    # 120 frames @ 30fps = 4.0s each
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)
    await seed_shot_with_source(db_session_factory, pid, 2, frames=120)

    # shot 1: trimmed to 60 frames (2.0s).  shot 2: voice-cloned, untrimmed.
    # The wav is 5.0s -- longer than the 4.0s video, so -shortest bounds it.
    vc_key = await _publish_vc_wav(tmp_path, pid, 2, seconds=5.0)
    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.trim_frames = 60
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        shot2.vc_audio_path = vc_key
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    with tempfile.TemporaryDirectory() as td:
        out = await _download_join_preview(pid, Path(td))

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


@pytest.mark.asyncio
async def test_join_preview_stays_in_sync_when_only_one_shot_is_baked(
    client, db_session_factory, cos_prefix, tmp_path
):
    """Isolates the concat-filter fix from the audio-normalization fix.

    When both shots carry an edit, build_effective_clip re-encodes both and
    hands merge_shots two already-homogeneous clips — so even the old concat
    demuxer stitched them cleanly.  The demuxer's defect only surfaces when the
    merge inputs genuinely differ: here shot 1 passes through untouched (44.1 kHz
    mono, straight from the source) while shot 2 is baked to canonical 48 kHz
    stereo by its voice clone.  Under the demuxer the container carried a single
    decoder config for both segments and the audio ran long — 8.7s against an
    8.0s video — without emitting a single decode error.
    """
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="completed")
    # 120 frames @ 30fps = 4.0s each
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)
    await seed_shot_with_source(db_session_factory, pid, 2, frames=120)

    # shot 1: no edits at all -> passthrough, keeps the source's 44.1 kHz mono.
    # shot 2: voice-cloned -> baked to canonical 48 kHz stereo.
    vc_key = await _publish_vc_wav(tmp_path, pid, 2, seconds=5.0)
    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        shot2.vc_audio_path = vc_key
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    with tempfile.TemporaryDirectory() as td:
        out = await _download_join_preview(pid, Path(td))

        assert decode_errors(out) == 0, "preview has audio decode errors"

        # The demuxer regression shows up here, not in decode_errors: mismatched
        # inputs made the audio run ~0.7s past the video.
        v_dur = stream_duration(out, "v")
        a_dur = stream_duration(out, "a")
        assert v_dur == pytest.approx(8.0, abs=0.2), f"video {v_dur}"
        assert a_dur == pytest.approx(v_dur, abs=0.2), (
            f"audio {a_dur} does not span the video {v_dur}"
        )

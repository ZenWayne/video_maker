"""Bake a shot's effective clip from the immutable source + EDL metadata.

The single place ffmpeg applies trim / audio-substitution. Used by both the
merger (export) and the connectivity join-preview endpoint.

COS note: this module does no object-store I/O. `effective_clip_paths` only
ever sees *local* paths — callers (worker.tasks.merge_project_shots,
app.api.pipeline.join_preview) are responsible for fetching each shot's video
(and vc_audio, if set) out of COS into a workspace first, via
`workspace().fetch(key, name=...)` with an explicit per-shot name (fetching
several shots into one workspace needs distinct names — see Workspace.fetch's
docstring). This used to instead take raw `Shot` ORM objects and fall back to
scanning a local shot directory (`shot_source_path`) when `video_path` was
unset — a local-storage-root era concept that no longer exists once COS is the
only store, so that fallback is gone; `video_path` (a COS key) is always the
source of truth now.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ffmpeg import FFmpeg

from app.agents.merger import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE
from app.services.storage import ts_uuid_name

logger = logging.getLogger(__name__)


def build_effective_clip(
    source_path: str,
    *,
    trim_frames: int | None,
    vc_audio_path: str | None,
    out_path: str,
    audio_head_mute_frames: int | None = None,
    vcodec: str = "libx264",
    crf: int = 18,
    acodec: str = "aac",
) -> None:
    """Render <source> with trim + audio-substitution applied into out_path.

    - trim_frames: keep frames 0..trim_frames-1 (frame-precise via -vframes); the
      audio is cut to the SAME duration (trim_frames/fps) via an atrim filter —
      -shortest alone does NOT bound audio when -vframes caps the video, leaving
      a full-length (uncut) audio track.
    - vc_audio_path: replace the audio with this wav (bounded by -shortest when not
      trimming, or by atrim when trimming).
    - audio_head_mute_frames: mute the first N/fps seconds of audio (orthogonal to
      trim/vc — composes with either) via a volume filter; the timeline is
      untouched (A/V stays in sync).
    - Re-encoded output is always CANONICAL_SAMPLE_RATE / CANONICAL_CHANNELS audio.
    - No edits → straight copy of the source bytes.
    """
    if not trim_frames and not vc_audio_path and not audio_head_mute_frames:
        shutil.copy2(source_path, out_path)
        return

    ff = FFmpeg().option("y").input(source_path)
    audio_map = "0:a"
    if vc_audio_path:
        ff = ff.input(vc_audio_path)
        audio_map = "1:a"

    opts: dict = {
        "map": ["0:v", audio_map],
        "vcodec": vcodec,
        "acodec": acodec,
        # Without this a VC clip inherits the CosyVoice wav's 24 kHz mono layout.
        "ar": CANONICAL_SAMPLE_RATE,
        "ac": CANONICAL_CHANNELS,
    }
    if vcodec == "libx264":
        opts["preset"] = "fast"
        opts["crf"] = crf

    fps = None
    af_parts: list[str] = []
    if trim_frames:
        from app.agents.video_trimmer import get_video_info
        fps = get_video_info(source_path)["fps"]
        opts["vframes"] = trim_frames                       # exact video frame count
        # cut the audio to match the trimmed video duration (the actual fix)
        af_parts.append(f"atrim=end={trim_frames / fps:.6f}")
        af_parts.append("asetpts=PTS-STARTPTS")
    elif vc_audio_path:
        opts["shortest"] = None  # vc-only: bound the substituted audio to the video
    if audio_head_mute_frames:
        if fps is None:
            from app.agents.video_trimmer import get_video_info
            fps = get_video_info(source_path)["fps"]
        mute_sec = audio_head_mute_frames / fps
        # 只把 t < mute_sec 的音频压到 0，其余原样、时间轴不动 → A/V 不错位
        af_parts.append(f"volume=enable='lt(t\\,{mute_sec:.6f})':volume=0")
    if af_parts:
        opts["af"] = ",".join(af_parts)

    ff.output(out_path, **opts).execute()
    if not Path(out_path).exists():
        raise RuntimeError(f"build_effective_clip produced no output: {out_path}")
    logger.info(
        "Effective clip %s (trim=%s vc=%s headmute=%s)",
        out_path, trim_frames, bool(vc_audio_path), audio_head_mute_frames,
    )


@dataclass
class ClipSpec:
    """One shot's already-*local* inputs + EDL metadata, ready to bake.

    Both `local_video_path` and `local_vc_audio_path` (if set) must already
    exist on disk — this module performs no COS fetches; the caller fetches
    via `workspace().fetch(...)` before building specs.
    """
    local_video_path: str
    trim_frames: Optional[int]
    local_vc_audio_path: Optional[str]
    audio_head_mute_frames: Optional[int] = None


def effective_clip_paths(specs: list[ClipSpec], tmp_dir: str) -> list[str]:
    """Return one playable local path per spec: passthrough if unedited, else a
    freshly-baked clip under tmp_dir. Caller owns tmp_dir cleanup (a workspace's
    root works — it self-cleans on exit).
    """
    out: list[str] = []
    for i, spec in enumerate(specs):
        if not spec.trim_frames and not spec.local_vc_audio_path and not spec.audio_head_mute_frames:
            out.append(spec.local_video_path)
            continue
        clip = str(Path(tmp_dir) / f"eff_{i:04d}_{ts_uuid_name('.mp4')}")
        build_effective_clip(
            spec.local_video_path,
            trim_frames=spec.trim_frames,
            vc_audio_path=spec.local_vc_audio_path,
            out_path=clip,
            audio_head_mute_frames=spec.audio_head_mute_frames,
        )
        out.append(clip)
    return out

"""Bake a shot's effective clip from the immutable source + EDL metadata.

The single place ffmpeg applies trim / audio-substitution. Used by the merger
at export time; preview compositing is done independently on the frontend from
the same DB metadata (trim_frames, vc_audio_path).
"""

import logging
import shutil
from pathlib import Path

from ffmpeg import FFmpeg

from app.services.storage import shot_source_path, ts_uuid_name

logger = logging.getLogger(__name__)


def build_effective_clip(
    source_path: str,
    *,
    trim_frames: int | None,
    vc_audio_path: str | None,
    out_path: str,
    vcodec: str = "libx264",
    crf: int = 18,
    acodec: str = "aac",
) -> None:
    """Render <source> with trim + audio-substitution applied into out_path.

    - trim_frames: keep frames 0..trim_frames-1 (frame-precise via -vframes);
      -shortest bounds the audio stream to the trimmed video length.
    - vc_audio_path: replace the audio with this full-length wav (clamped by -shortest).
    - No edits → straight copy of the source bytes.
    """
    if not trim_frames and not vc_audio_path:
        shutil.copy2(source_path, out_path)
        return

    ff = FFmpeg().option("y").input(source_path)
    audio_map = "0:a"
    if vc_audio_path:
        ff = ff.input(vc_audio_path)
        audio_map = "1:a"

    opts: dict = {"map": ["0:v", audio_map], "vcodec": vcodec, "acodec": acodec,
                  "shortest": None}  # always bound audio to video duration
    if vcodec == "libx264":
        opts["preset"] = "fast"
        opts["crf"] = crf
    if trim_frames:
        opts["vframes"] = trim_frames

    ff.output(out_path, **opts).execute()
    if not Path(out_path).exists():
        raise RuntimeError(f"build_effective_clip produced no output: {out_path}")
    logger.info(
        "Effective clip %s (trim=%s vc=%s)", out_path, trim_frames, bool(vc_audio_path)
    )


def effective_clip_paths(shots: list, tmp_dir: str) -> list[str]:
    """Return one playable path per shot: source passthrough if unedited, else a
    freshly-baked temp clip under tmp_dir. Caller owns tmp_dir cleanup.

    Each shot must expose .project_id, .shot_id, .trim_frames, .vc_audio_path.
    If vc_audio_path is set but the file does not exist on disk, the shot is
    treated as no-vc (falls back to source audio) and a warning is logged.
    """
    out: list[str] = []
    for s in shots:
        # The DB field is the source of truth (the immutable output_*.mp4); fall
        # back to the prefix-glob only if it's unset.
        source_path = s.video_path if (s.video_path and Path(s.video_path).exists()) else None
        if source_path is None:
            sp = shot_source_path(s.project_id, s.shot_id)
            source_path = str(sp) if sp else None
        if source_path is None:
            raise FileNotFoundError(f"Shot {s.shot_id}: no source video")
        source = source_path

        vc_audio = s.vc_audio_path
        if vc_audio and not Path(vc_audio).exists():
            logger.warning(
                "Shot %s: vc_audio_path %r does not exist on disk — falling back to source audio",
                s.shot_id,
                vc_audio,
            )
            vc_audio = None

        if not s.trim_frames and not vc_audio:
            out.append(str(source))
            continue
        clip = str(Path(tmp_dir) / f"eff_{s.shot_id}_{ts_uuid_name('.mp4')}")
        build_effective_clip(
            str(source),
            trim_frames=s.trim_frames,
            vc_audio_path=vc_audio,
            out_path=clip,
        )
        out.append(clip)
    return out

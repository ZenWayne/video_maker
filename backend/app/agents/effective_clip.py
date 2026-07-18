"""Bake a shot's effective clip from the immutable source + EDL metadata.

The single place ffmpeg applies trim / audio-substitution. Used by the merger
at export time; preview compositing is done independently on the frontend from
the same DB metadata (trim_frames, vc_audio_path).
"""

import logging
import shutil
from pathlib import Path

from ffmpeg import FFmpeg

from app.agents.merger import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE
from app.services.storage import shot_source_path, ts_uuid_name

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

        head_mute = getattr(s, "audio_head_mute_frames", None)
        if not s.trim_frames and not vc_audio and not head_mute:
            out.append(str(source))
            continue
        clip = str(Path(tmp_dir) / f"eff_{s.shot_id}_{ts_uuid_name('.mp4')}")
        build_effective_clip(
            str(source),
            trim_frames=s.trim_frames,
            vc_audio_path=vc_audio,
            out_path=clip,
            audio_head_mute_frames=head_mute,
        )
        out.append(clip)
    return out

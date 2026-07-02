"""Unit tests for frame_porter and merger agents.

Fake test videos are generated using ffmpeg's lavfi (virtual input) source so no
real video files are required.  All tests are skipped when the ffmpeg binary is
not present in PATH (e.g. on developer machines without ffmpeg installed).
"""

import shutil
import subprocess
import pytest
from pathlib import Path

from ffmpeg import FFmpeg

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not found in PATH",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_video(path: Path, duration: int = 2) -> None:
    """Generate a tiny synthetic MP4 using ffmpeg lavfi color source."""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=10:duration={duration}", f="lavfi")
        .output(str(path), pix_fmt="yuv420p", vcodec="libx264", an=None)
    ).execute()


def _make_test_video_with_audio(path: Path, duration: int = 2) -> None:
    """Generate a tiny synthetic MP4 with a silent audio track."""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=10:duration={duration}", f="lavfi")
        .input(f"anullsrc=r=44100:cl=stereo", f="lavfi", t=str(duration))
        .output(str(path), pix_fmt="yuv420p", vcodec="libx264", acodec="aac", shortest=None)
    ).execute()


def _pix_fmt(path: Path) -> str:
    """Return the video stream's pixel format via ffprobe."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# frame_porter tests
# ---------------------------------------------------------------------------

class TestExtractLastFrame:
    def test_output_file_created(self, tmp_path):
        from app.agents.frame_porter import extract_last_frame

        video = tmp_path / "input.mp4"
        frame = tmp_path / "frame.png"
        _make_test_video(video)

        extract_last_frame(str(video), str(frame))

        assert frame.exists()
        assert frame.stat().st_size > 0

    def test_overwrites_existing_file(self, tmp_path):
        from app.agents.frame_porter import extract_last_frame

        video = tmp_path / "input.mp4"
        frame = tmp_path / "frame.png"
        _make_test_video(video)

        # Write a sentinel so we can confirm it was replaced
        frame.write_bytes(b"old-content")

        extract_last_frame(str(video), str(frame))

        assert frame.read_bytes() != b"old-content"

    def test_raises_on_missing_input(self, tmp_path):
        from app.agents.frame_porter import extract_last_frame

        with pytest.raises(Exception):
            extract_last_frame(str(tmp_path / "nonexistent.mp4"), str(tmp_path / "out.png"))


class TestExtractFrameAtTime:
    def test_output_file_created(self, tmp_path):
        from app.agents.frame_porter import extract_frame_at_time

        video = tmp_path / "input.mp4"
        frame = tmp_path / "frame.png"
        _make_test_video(video, duration=2)

        extract_frame_at_time(str(video), str(frame), time_seconds=0.5)

        assert frame.exists()
        assert frame.stat().st_size > 0

    def test_frame_at_zero(self, tmp_path):
        from app.agents.frame_porter import extract_frame_at_time

        video = tmp_path / "input.mp4"
        frame = tmp_path / "frame.png"
        _make_test_video(video)

        extract_frame_at_time(str(video), str(frame), time_seconds=0)

        assert frame.exists()

    def test_raises_on_missing_input(self, tmp_path):
        from app.agents.frame_porter import extract_frame_at_time

        with pytest.raises(Exception):
            extract_frame_at_time(
                str(tmp_path / "nonexistent.mp4"),
                str(tmp_path / "out.png"),
                time_seconds=0.5,
            )


# ---------------------------------------------------------------------------
# merger tests
# ---------------------------------------------------------------------------

class TestMergeShots:
    def test_single_shot_copied(self, tmp_path):
        from app.agents.merger import merge_shots

        video = tmp_path / "shot1.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(video)

        merge_shots([str(video)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_multiple_shots_merged(self, tmp_path):
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(v1)
        _make_test_video(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_output_larger_than_single_input(self, tmp_path):
        """Merged file should be at least as large as one input."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(v1)
        _make_test_video(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.stat().st_size >= v1.stat().st_size

    def test_filters_none_paths(self, tmp_path):
        from app.agents.merger import merge_shots

        video = tmp_path / "shot1.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(video)

        # None and empty string paths should be silently filtered
        merge_shots([None, str(video), ""], str(output))

        assert output.exists()

    def test_creates_parent_directory(self, tmp_path):
        from app.agents.merger import merge_shots

        video = tmp_path / "shot1.mp4"
        output = tmp_path / "subdir" / "deep" / "merged.mp4"
        _make_test_video(video)

        merge_shots([str(video)], str(output))

        assert output.exists()

    def test_raises_on_empty_list(self, tmp_path):
        from app.agents.merger import merge_shots

        with pytest.raises(ValueError, match="No shot paths provided"):
            merge_shots([], str(tmp_path / "out.mp4"))

    def test_raises_on_all_none_paths(self, tmp_path):
        from app.agents.merger import merge_shots

        with pytest.raises(ValueError, match="No valid shot paths provided"):
            merge_shots([None, ""], str(tmp_path / "out.mp4"))


class TestMergeShotsWithReencoding:
    def test_single_shot(self, tmp_path):
        from app.agents.merger import merge_shots_with_reencoding

        video = tmp_path / "shot1.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(video)

        merge_shots_with_reencoding([str(video)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_multiple_shots(self, tmp_path):
        from app.agents.merger import merge_shots_with_reencoding

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(v1)
        _make_test_video(v2)

        merge_shots_with_reencoding([str(v1), str(v2)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_custom_codec_options(self, tmp_path):
        from app.agents.merger import merge_shots_with_reencoding

        video = tmp_path / "shot1.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(video)

        merge_shots_with_reencoding([str(video)], str(output), codec="libx264", preset="fast", crf=28)

        assert output.exists()

    def test_raises_on_empty_list(self, tmp_path):
        from app.agents.merger import merge_shots_with_reencoding

        with pytest.raises(ValueError, match="No shot paths provided"):
            merge_shots_with_reencoding([], str(tmp_path / "out.mp4"))

    def test_reencoded_output_is_yuv420p(self, tmp_path):
        """Re-encoded concat must stay yuv420p (browser/hardware decodable)."""
        from app.agents.merger import merge_shots_with_reencoding

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1)
        _make_test_video_with_audio(v2)

        merge_shots_with_reencoding([str(v1), str(v2)], str(output))

        assert _pix_fmt(output) == "yuv420p"


class TestMergeShotsWithCrossfade:
    def test_crossfade_output_is_yuv420p(self, tmp_path):
        """xfade negotiates yuv444p internally; the encoded output must be
        forced back to yuv420p or browsers cannot decode the merged video."""
        from app.agents.merger import merge_shots_with_crossfade

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1, duration=2)
        _make_test_video_with_audio(v2, duration=2)

        merge_shots_with_crossfade([str(v1), str(v2)], str(output), crossfade_duration=0.3)

        assert output.exists()
        assert _pix_fmt(output) == "yuv420p"

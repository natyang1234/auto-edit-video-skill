"""A failed render must name its own cause, and a good file must read as good.

Two defects met in one Studio session: a healthy staged clip was declared
"not a video" because ffprobe grew a section, and the resulting toast quoted
libx264's encoding statistics instead of the failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import graphic_package  # noqa: E402
import tool_output  # noqa: E402


X264_STATISTICS = """
frame=   38 fps= 38 q=26.0 size=    1024KiB time=00:00:00.60 bitrate=13967.7kbits/s speed=0.593x
frame=  680 fps= 72 q=-1.0 Lsize=   19679KiB time=00:00:11.31 bitrate=14252.0kbits/s speed=1.19x
[mp4 @ 0x947027700] Starting second pass: moving the moov atom to the beginning of the file
[out#0/mp4 @ 0x946c409c0] video:19670KiB audio:0KiB muxing overhead: 0.045646%
[libx264 @ 0x946c3f480] frame I:23    Avg QP:18.77  size:150429
[libx264 @ 0x946c3f480] mb I  I16..4: 11.7% 73.1% 15.3%
[libx264 @ 0x946c3f480] ref B L1: 99.5%  0.5%
[libx264 @ 0x946c3f480] kb/s:14203.08
""".strip()


class StagedClipIsReadAsVideoTests(unittest.TestCase):
    """The probe verdict must survive ffprobe's own output growing."""

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_real_encoded_clip_counts_as_video(self) -> None:
        with tempfile.TemporaryDirectory(prefix="auto-edit-hasvideo-") as tmp:
            clip = Path(tmp) / "staged.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=0x336699:s=320x568:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-r", "30",
                    "-movflags", "+faststart",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            self.assertTrue(graphic_package._has_video(clip))

    def test_side_data_section_does_not_void_the_stream(self) -> None:
        # ffprobe 8 reports an empty SIDE_DATA section for the same stream,
        # so a one-field CSV row arrives as "video," instead of "video".
        completed = subprocess.CompletedProcess(["ffprobe"], 0, '{"streams":[{"codec_type":"video"}]}', "")
        original = graphic_package.subprocess.run
        graphic_package.subprocess.run = lambda *a, **k: completed  # type: ignore[assignment]
        try:
            probe = Path(__file__)  # any existing file; the probe result is stubbed
            self.assertTrue(graphic_package._has_video(probe))
        finally:
            graphic_package.subprocess.run = original  # type: ignore[assignment]

    def test_a_stream_less_file_is_still_rejected(self) -> None:
        completed = subprocess.CompletedProcess(["ffprobe"], 0, '{"streams":[]}', "")
        original = graphic_package.subprocess.run
        graphic_package.subprocess.run = lambda *a, **k: completed  # type: ignore[assignment]
        try:
            self.assertFalse(graphic_package._has_video(Path(__file__)))
        finally:
            graphic_package.subprocess.run = original  # type: ignore[assignment]


class FailureMessagesNameTheFailureTests(unittest.TestCase):
    """Encoder statistics are not a diagnosis."""

    def test_statistics_alone_do_not_masquerade_as_an_error(self) -> None:
        summary = tool_output.summarize_tool_failure(X264_STATISTICS)
        self.assertNotIn("[libx264 @", summary)
        self.assertNotIn("kb/s:", summary)
        self.assertNotIn("frame=", summary)
        self.assertTrue(summary.strip())

    def test_the_real_error_survives_the_statistics_that_follow_it(self) -> None:
        noisy = "\n".join(
            [
                "[libx264 @ 0x1] using SAR=1/1",
                "RuntimeError: graphic package storyboard has no visual layers",
                X264_STATISTICS,
            ]
        )
        summary = tool_output.summarize_tool_failure(noisy)
        self.assertIn("graphic package storyboard has no visual layers", summary)
        self.assertNotIn("kb/s:", summary)

    def test_a_traceback_is_kept_whole(self) -> None:
        noisy = "\n".join(
            [
                X264_STATISTICS,
                "Traceback (most recent call last):",
                '  File "render_editor_timeline.py", line 2716, in render_project',
                "    ensure_graphic_package(project_dir, state, manifest, clip)",
                "ValueError: graphic package clip duration must be positive",
            ]
        )
        summary = tool_output.summarize_tool_failure(noisy)
        self.assertIn("Traceback (most recent call last):", summary)
        self.assertIn("ValueError: graphic package clip duration must be positive", summary)
        self.assertNotIn("[libx264 @", summary)

    def test_a_lone_exit_line_is_reported_rather_than_dropped(self) -> None:
        summary = tool_output.summarize_tool_failure(
            X264_STATISTICS + "\nffmpeg exited with code 137"
        )
        self.assertIn("exited with code 137", summary)

    def test_the_stream_inventory_block_is_not_a_diagnosis(self) -> None:
        noisy = "\n".join(
            [
                "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/original.mov':",
                "  Metadata:",
                "    com.apple.quicktime.model: iPhone 14 Pro",
                "  Duration: 00:00:11.34, start: 0.000000, bitrate: 13704 kb/s",
                "  Stream #0:0(und): Video: hevc, yuv420p(tv), 1080x1920, 59.94 fps",
                "      Display Matrix: rotation of -90.00 degrees",
                "Output #0, mp4, to '/tmp/staged.mp4':",
                "    encoder         : Lavc62.28.101 libx264",
                "Error opening output files: Invalid argument",
            ]
        )
        summary = tool_output.summarize_tool_failure(noisy)
        self.assertEqual(summary, "Error opening output files: Invalid argument")

    def test_pure_noise_falls_back_to_a_readable_reason(self) -> None:
        summary = tool_output.summarize_tool_failure("", fallback="render failed")
        self.assertEqual(summary, "render failed")

    def test_non_ffmpeg_output_passes_through_untouched(self) -> None:
        summary = tool_output.summarize_tool_failure("chromium could not start: no display")
        self.assertEqual(summary, "chromium could not start: no display")

    def test_the_summary_is_bounded(self) -> None:
        summary = tool_output.summarize_tool_failure(
            "\n".join(f"Error: line {index} went wrong" for index in range(400)),
            limit=1200,
        )
        self.assertLessEqual(len(summary), 1200)
        self.assertIn("line 399 went wrong", summary)


class StagingFailureReportsItsCauseTests(unittest.TestCase):
    """The graphic package is where the masked message was minted."""

    def test_staging_error_is_denoised_before_it_leaves_the_package(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg"], 1, "", "Error opening input: No such file or directory\n" + X264_STATISTICS
        )
        original = graphic_package.subprocess.run
        graphic_package.subprocess.run = lambda *a, **k: completed  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory(prefix="auto-edit-staging-") as tmp:
                root = Path(tmp)
                with self.assertRaises(RuntimeError) as caught:
                    graphic_package._stage_input_video(
                        root=root,
                        source=root / "source.mov",
                        output=root / "input-video.mp4",
                        clip_start=0.0,
                        duration=1.0,
                        template_state=graphic_package._resolved_template_state(None),
                        ffmpeg="ffmpeg",
                    )
        finally:
            graphic_package.subprocess.run = original  # type: ignore[assignment]
        message = str(caught.exception)
        self.assertIn("No such file or directory", message)
        self.assertNotIn("[libx264 @", message)


if __name__ == "__main__":
    unittest.main()

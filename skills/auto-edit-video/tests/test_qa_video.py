"""Delivery QA gate regression: black, silent, and audio-less finals must fail closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import qa_video  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_video(
    output: Path,
    *,
    video_source: str,
    audio_source: str | None,
    duration: float = 2.0,
) -> None:
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    separator = ":" if "=" in video_source else "="
    command += [
        "-f",
        "lavfi",
        "-i",
        f"{video_source}{separator}size=320x240:rate=30:duration={duration}",
    ]
    if audio_source is not None:
        command += ["-f", "lavfi", "-i", f"{audio_source}:duration={duration}"]
    command += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio_source is not None:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(output)]
    subprocess.run(command, check=True, text=True, capture_output=True)


class QaVideoGateTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FFMPEG or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe are required for QA gate tests")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def inspect(self, video: Path, policy: "qa_video.QaPolicy | None" = None):
        report_path = self.dir / f"{video.stem}-report.json"
        contact_path = self.dir / f"{video.stem}-contact.png"
        if policy is None:
            return qa_video.inspect(video, report_path, contact_path)
        return qa_video.inspect(video, report_path, contact_path, policy=policy)

    def test_reference_video_with_audio_passes(self) -> None:
        video = self.dir / "reference.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        report, ok = self.inspect(video)
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["status"], "pass")
        self.assertIn("policy", report, "report must echo the enforced QA policy")

    def test_black_silent_video_fails_closed(self) -> None:
        video = self.dir / "black-silent.mp4"
        make_video(video, video_source="color=c=black", audio_source="anullsrc=r=48000:cl=stereo")
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a fully black, silent final must not pass QA")
        self.assertEqual(report["status"], "fail")
        joined = " ".join(report["failures"]).lower()
        self.assertIn("black", joined)
        self.assertTrue(
            "loudness" in joined or "silent" in joined,
            f"silence must be a failure, got: {report['failures']}",
        )

    def test_missing_audio_fails_by_default(self) -> None:
        video = self.dir / "no-audio.mp4"
        make_video(video, video_source="testsrc", audio_source=None)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "missing audio must fail closed by default")
        self.assertIn("audio stream is missing", report["failures"])

    def test_missing_audio_can_be_allowed_by_policy(self) -> None:
        video = self.dir / "no-audio-allowed.mp4"
        make_video(video, video_source="testsrc", audio_source=None)
        policy = qa_video.QaPolicy(allow_missing_audio=True)
        report, ok = self.inspect(video, policy=policy)
        self.assertTrue(ok, report["failures"])
        self.assertIn("audio stream is missing", report["warnings"])

    def test_relaxed_black_policy_is_configurable_but_silence_still_fails(self) -> None:
        video = self.dir / "black-with-tone.mp4"
        make_video(video, video_source="color=c=black", audio_source="sine=frequency=440")
        relaxed = qa_video.QaPolicy(max_black_segment_seconds=60.0, max_black_ratio=1.1)
        report, ok = self.inspect(video, policy=relaxed)
        self.assertTrue(ok, report["failures"])
        self.assertTrue(
            any("black" in item for item in report["warnings"]),
            "relaxed black policy must still surface a warning",
        )
        # Same video under the default policy must fail on black coverage.
        report, ok = self.inspect(video)
        self.assertFalse(ok, "default policy must fail a fully black final")

    def test_fragmented_black_frames_cannot_evade_the_ratio_gate(self) -> None:
        # 0.45s black pulses every 0.5s: ~90% black overall, yet every segment
        # stays under the 0.5s blackdetect floor the gate previously relied on.
        video = self.dir / "strobe-black.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=320x240:r=30:d=4,"
            "geq=lum='if(lt(mod(T,0.5),0.45),16,235)':cb=128:cr=128",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "fragmented black frames must not evade the coverage gate")
        self.assertTrue(
            any("black frames cover" in item for item in report["failures"]),
            report["failures"],
        )

    def test_single_frame_black_flicker_at_60fps_fails(self) -> None:
        # Alternating black/white single frames at 60fps: 50% black overall,
        # each black run lasting one frame (~16.7ms) — below any detection
        # floor that sits above the frame duration.
        video = self.dir / "flicker-60fps.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=320x240:r=60:d=4,"
            "geq=lum='if(mod(N,2),16,235)':cb=128:cr=128",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "50% single-frame black flicker must fail the coverage gate")
        self.assertTrue(
            any("black frames cover" in item for item in report["failures"]),
            report["failures"],
        )

    def test_black_frame_with_caption_box_still_counts_as_black(self) -> None:
        # A failed background render that still draws the caption box leaves a
        # frame that is ~94% black. Frame-level black detection must not need
        # a near-perfectly black frame to notice.
        video = self.dir / "black-with-caption-box.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x480:r=30:d=3,"
            "drawbox=x=20:y=380:w=300:h=60:color=white:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a black frame carrying a caption box must still fail")
        self.assertTrue(
            any("black" in item for item in report["failures"]), report["failures"]
        )

    def test_letterboxed_content_is_not_treated_as_black(self) -> None:
        # Pillarboxed delivery (portrait content on a landscape canvas) has
        # large black margins but real content; it must keep passing.
        video = self.dir / "pillarboxed.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=180x480:rate=30:duration=3,"
            "pad=width=640:height=480:x=230:y=0:color=black",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"pillarboxed content must not be flagged: {report['failures']}")

    def test_black_frame_with_side_bars_still_fails(self) -> None:
        # Decorative side bars cover only a small share of the frame but span
        # most of its height. Measuring only the area they bound would hide
        # the black between them.
        video = self.dir / "black-side-bars.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=540x960:r=30:d=3,"
            "drawbox=x=70:y=305:w=60:h=350:color=white:t=fill,"
            "drawbox=x=410:y=305:w=60:h=350:color=white:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a black frame framed by side bars must fail")
        self.assertTrue(
            any("black" in item for item in report["failures"]), report["failures"]
        )

    def test_audio_that_stops_partway_fails(self) -> None:
        # Total silent coverage alone lets this through: the audio dies 30%
        # in, leaving 70% silence, which is under the coverage threshold.
        video = self.dir / "audio-stops-partway.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='gte(t,1.8)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "audio that stops partway must fail")
        self.assertTrue(
            any("silent" in item for item in report["failures"]), report["failures"]
        )

    def make_audio_shaped_video(self, name: str, volume_expr: str, duration: float) -> Path:
        video = self.dir / name
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size=320x240:rate=30:duration={duration}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-af",
                f"volume='{volume_expr}':eval=frame",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        return video

    def test_long_dead_air_fails_even_when_proportionally_small(self) -> None:
        # 20s delivery losing audio at 13s: 35% of the timeline, under any
        # proportional limit, but seven unbroken seconds of dead air.
        video = self.make_audio_shaped_video(
            "long-dead-air.mp4", "if(lt(t,13),0.3,0)", 20
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "seven seconds of unbroken dead air must fail")
        self.assertTrue(
            any("unbroken" in item for item in report["failures"]), report["failures"]
        )

    def test_sparse_audio_fails_even_when_each_gap_is_short(self) -> None:
        # Sound for 1.5s out of every 8s: no single gap is large and total
        # coverage stays under the blanket limit, yet the delivery is silent
        # for four fifths of its length.
        video = self.make_audio_shaped_video(
            "sparse-audio.mp4", "if(lt(mod(t,8),1.5),0.3,0)", 24
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a mostly silent delivery must fail")
        self.assertTrue(
            any("carries sound" in item or "silent" in item for item in report["failures"]),
            report["failures"],
        )

    def test_short_clip_clipping_still_fails(self) -> None:
        video = self.dir / "short-clipping.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=0.3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=997:sample_rate=48000:duration=0.3,volume=10",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a clipping short clip must fail on peak level")
        self.assertTrue(
            any("clipping" in item for item in report["failures"]), report["failures"]
        )

    def test_narration_with_pauses_is_not_flagged(self) -> None:
        # Repeated short gaps are normal pacing, not a dropout.
        video = self.dir / "narration-pauses.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='between(mod(t,2),1,2)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"paced narration must not be flagged: {report['failures']}")

    def test_moderately_dark_contain_delivery_passes(self) -> None:
        # Contain fit pads to canvas in a near-black tone; a dark but real
        # picture inside those bars must still pass.
        video = self.dir / "contain-dark.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=3,"
            "eq=brightness=-0.32:contrast=0.6,"
            "scale=360:640:force_original_aspect_ratio=decrease,"
            "pad=360:640:(ow-iw)/2:(oh-ih)/2:color=0x171512",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(
            ok, f"dark content inside contain padding must pass: {report['failures']}"
        )

    def test_audio_that_stops_after_the_opening_fails(self) -> None:
        # Integrated loudness is gated and ignores silence, so a final whose
        # narration was truncated still measures a healthy level.
        video = self.dir / "audio-drops-out.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='gte(t,0.3)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "audio that stops after the opening must fail")
        self.assertTrue(
            any("silent for" in item for item in report["failures"]), report["failures"]
        )

    def test_very_short_audible_clip_is_not_failed_for_loudness(self) -> None:
        # EBU R128 integrates over 400ms and reports -70 LUFS below that.
        video = self.dir / "very-short.mp4"
        make_video(
            video,
            video_source="testsrc",
            audio_source="sine=frequency=440",
            duration=0.3,
        )
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"a 0.3s audible clip must not fail: {report['failures']}")

    def test_short_fully_black_video_fails(self) -> None:
        video = self.dir / "short-black.mp4"
        make_video(
            video,
            video_source="color=c=black",
            audio_source="sine=frequency=440",
            duration=0.4,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a fully black clip under 0.5s must still fail")

    def test_non_finite_policy_values_are_rejected(self) -> None:
        for field in (
            "max_black_segment_seconds",
            "max_black_ratio",
            "min_integrated_lufs",
            "max_true_peak_dbfs",
        ):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.assertRaises(ValueError, msg=f"{field}={bad}"):
                    qa_video.QaPolicy(**{field: bad})

        video = self.dir / "nan-flags.mp4"
        make_video(video, video_source="color=c=black", audio_source=None)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(video),
                "--report",
                str(self.dir / "nan-report.json"),
                "--contact",
                str(self.dir / "nan-contact.png"),
                "--max-black-segment-seconds",
                "nan",
                "--max-black-ratio",
                "nan",
                "--allow-missing-audio",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0, "NaN thresholds must not produce a pass")
        self.assertNotIn('"status": "pass"', result.stdout)

    def test_true_peak_clipping_and_unmeasured_peak_fail(self) -> None:
        video = self.dir / "peak-check.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        from unittest.mock import patch

        with patch.object(
            qa_video, "loudness", return_value={"integrated_lufs": -14.0, "true_peak_dbfs": 0.5}
        ):
            report, ok = self.inspect(video)
        self.assertFalse(ok)
        self.assertTrue(any("clipping" in item for item in report["failures"]), report["failures"])

        with patch.object(qa_video, "loudness", return_value={"integrated_lufs": -14.0}):
            report, ok = self.inspect(video)
        self.assertFalse(ok)
        self.assertTrue(
            any("true peak could not be measured" in item for item in report["failures"]),
            report["failures"],
        )

    def test_cli_exit_codes_and_flags(self) -> None:
        bad = self.dir / "cli-black-silent.mp4"
        make_video(bad, video_source="color=c=black", audio_source="anullsrc=r=48000:cl=stereo")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(bad),
                "--report",
                str(self.dir / "cli-report.json"),
                "--contact",
                str(self.dir / "cli-contact.png"),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")

        good = self.dir / "cli-black-tone.mp4"
        make_video(good, video_source="color=c=black", audio_source="sine=frequency=440")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(good),
                "--report",
                str(self.dir / "cli-relaxed-report.json"),
                "--contact",
                str(self.dir / "cli-relaxed-contact.png"),
                "--max-black-segment-seconds",
                "60",
                "--max-black-ratio",
                "1.1",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

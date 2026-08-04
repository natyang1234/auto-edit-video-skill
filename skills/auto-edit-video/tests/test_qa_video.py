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

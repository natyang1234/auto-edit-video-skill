from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
AUTO_EDIT = SKILL / "scripts/auto_edit.py"
RENDER_CUT = SKILL / "scripts/render_cut.py"
QA_VIDEO = SKILL / "scripts/qa_video.py"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PortableCoreIntegrationTests(unittest.TestCase):
    def make_source(self, output: Path) -> None:
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=4",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(output),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_standalone_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "AUTO_EDIT_SKILLS_ROOTS": ""}
            for name in (
                "VIDEO_AUTOPILOT_SKILL_DIR",
                "CUT_NARRATION_SKILL_DIR",
                "NARRATION_VIDEO_SKILL_DIR",
                "EMBEDDED_CAPTIONS_SKILL_DIR",
                "TALKING_HEAD_RECUT_SKILL_DIR",
                "HYPERFRAMES_MEDIA_SKILL_DIR",
                "RUMI_VOICE_SYSTEM",
            ):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, str(AUTO_EDIT), "preflight"],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["mode"], "standalone")
            self.assertEqual(payload["missing_required"], [])
            self.assertIn("local_transcription", payload["capabilities"])
            self.assertEqual(
                payload["capabilities"]["local_transcription"],
                bool(shutil.which("whisper") or shutil.which("whisper-cli")),
            )

    def test_approved_cut_and_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for name in ("source", "working", "renders", "qa"):
                (project / name).mkdir(parents=True, exist_ok=True)
            source = project / "source/original.mp4"
            self.make_source(source)
            manifest = {
                "schema_version": 1,
                "project_dir": str(project),
                "source": {
                    "original_path": str(source),
                    "staged_path": "source/original.mp4",
                },
                "approvals": {"destructive_edit": {"approved": True}},
                "stages": {},
                "artifacts": {},
            }
            (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
            candidates = {
                "schema_version": 1,
                "items": [
                    {"id": "edit-0001", "start": 1.0, "end": 2.0, "type": "silence"}
                ],
            }
            decisions = {
                "schema_version": 1,
                "approved_from": "test",
                "items": [
                    {
                        "candidate_id": "edit-0001",
                        "action": "delete",
                        "review_status": "approved",
                    }
                ],
            }
            (project / "working/edit_candidates.json").write_text(
                json.dumps(candidates), encoding="utf-8"
            )
            (project / "working/edit_decisions.json").write_text(
                json.dumps(decisions), encoding="utf-8"
            )

            cut = subprocess.run(
                [sys.executable, str(RENDER_CUT), "--manifest", str(project / "project.json")],
                text=True,
                capture_output=True,
            )
            self.assertEqual(cut.returncode, 0, cut.stderr)
            cut_payload = json.loads(cut.stdout)
            self.assertTrue(cut_payload["ok"])
            self.assertAlmostEqual(cut_payload["expected_duration_s"], 3.0, delta=0.05)
            final = project / "renders/source_cut.mp4"
            self.assertTrue(final.is_file())

            probe = subprocess.run(
                [
                    shutil.which("ffprobe") or "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    str(final),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertAlmostEqual(float(probe.stdout.strip()), 3.0, delta=0.15)

            qa = subprocess.run(
                [sys.executable, str(QA_VIDEO), "--video", str(final)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(qa.returncode, 0, qa.stderr)
            qa_payload = json.loads(qa.stdout)
            self.assertEqual(qa_payload["status"], "pass")
            self.assertTrue((project / "qa/qa-report.json").is_file())
            self.assertTrue((project / "qa/final-contact.png").is_file())


if __name__ == "__main__":
    unittest.main()

"""Phase 1a M1: folder ingest and narrative-plan application tests."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))


def folder_digest(folder: Path) -> str:
    payload = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            payload.append(
                (path.relative_to(folder).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            )
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class FolderIngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        cls._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-ingest-tests-")
        cls.base = Path(cls._tmp.name)
        cls.folder = cls.base / "素材夾"
        cls.folder.mkdir()
        for name, seconds in (("short.mp4", 0.3), ("main-talk.mp4", 0.9)):
            subprocess.run(
                [
                    cls.ffmpeg, "-y",
                    "-f", "lavfi", "-i", f"testsrc=size=192x336:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-shortest", "-pix_fmt", "yuv420p", str(cls.folder / name),
                ],
                check=True, capture_output=True,
            )
        (cls.folder / "封面.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
        (cls.folder / "字幕.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n哈囉\n", "utf-8")
        (cls.folder / "notes.txt").write_text("備忘", "utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_cli(self, *argv: str, expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "auto_edit.py"), *argv],
            capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, expected,
            f"argv={argv}\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        return result

    def test_ingest_folder_end_to_end(self) -> None:
        before = folder_digest(self.folder)
        project = self.base / "project-e2e"
        self.run_cli("ingest-folder", "--folder", str(self.folder), "--project-dir", str(project))
        self.assertEqual(folder_digest(self.folder), before, "source folder must stay untouched")

        inventory = json.loads((project / "working/folder_inventory.json").read_text("utf-8"))
        self.assertEqual(inventory["main_video_path"], "main-talk.mp4", "longest video wins")
        roles = {entry["path"]: entry["role"] for entry in inventory["files"]}
        self.assertEqual(roles["main-talk.mp4"], "main_video")
        self.assertEqual(roles["short.mp4"], "broll")
        self.assertEqual(roles["封面.png"], "asset")
        self.assertEqual(roles["字幕.srt"], "transcript")
        self.assertEqual(roles["notes.txt"], "ignored")

        import contract_registry

        self.assertEqual(contract_registry.validate_artifact("folder_inventory", inventory), [])

        manifest = json.loads((project / "project.json").read_text("utf-8"))
        self.assertEqual(manifest["source"]["ingest_method"], "folder_import")
        staged = project / manifest["source"]["staged_path"]
        self.assertTrue(staged.is_file())
        self.assertFalse(staged.stat().st_mode & 0o222, "owned copy must be immutable")
        imported = list((project / "assets/imported").glob("*"))
        self.assertTrue(any("short.mp4" in path.name for path in imported))
        self.assertTrue(any("封面.png" in path.name for path in imported))
        self.assertFalse(any("notes.txt" in path.name for path in imported))

    def test_apply_narrative_plan_reorder_requires_confirmation(self) -> None:
        project = self.base / "project-plan"
        self.run_cli("ingest-folder", "--folder", str(self.folder), "--project-dir", str(project))
        plan = {
            "segments": [
                {"id": "segment-aaaaaaaaaaaa", "source_start": 0.5, "source_end": 0.8, "purpose": "hook"},
                {"id": "segment-bbbbbbbbbbbb", "source_start": 0.0, "source_end": 0.3, "purpose": "context"},
            ]
        }
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(plan), "utf-8")
        result = self.run_cli(
            "apply-narrative-plan", "--project-dir", str(project),
            "--plan", str(plan_path), "--draft", expected=2,
        )
        self.assertIn("high-risk", result.stderr + result.stdout)

        self.run_cli(
            "apply-narrative-plan", "--project-dir", str(project),
            "--plan", str(plan_path), "--draft", "--confirm-high-risk",
        )
        state = json.loads((project / "working/editor_state.json").read_text("utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(
            [segment["source_start"] for segment in state["segments"]], [0.5, 0.0]
        )
        self.assertEqual(state["segments"][0]["origin"], "narrative")
        saved_plan = json.loads(
            (project / "working/narrative_edit_plan.json").read_text("utf-8")
        )
        self.assertTrue(saved_plan["reorder"])
        self.assertEqual(saved_plan["risk"], "high")

        import contract_registry

        self.assertEqual(
            contract_registry.validate_artifact("narrative_edit_plan", saved_plan), []
        )


if __name__ == "__main__":
    unittest.main()

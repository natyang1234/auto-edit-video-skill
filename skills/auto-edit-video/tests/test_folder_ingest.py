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
        # The named video is the short one on purpose: the name decides
        # which video is the talking, not which one runs longest.
        for name, seconds in (("main.mp4", 0.3), ("broll.mp4", 0.9)):
            subprocess.run(
                [
                    cls.ffmpeg, "-y",
                    "-f", "lavfi", "-i", f"testsrc=size=192x336:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-shortest", "-pix_fmt", "yuv420p", str(cls.folder / name),
                ],
                check=True, capture_output=True,
            )
        # A real picture, not a stub with a PNG header: ingest now checks
        # that a file named like an image decodes into one.
        subprocess.run(
            [
                cls.ffmpeg, "-y", "-f", "lavfi",
                "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1",
                str(cls.folder / "封面.png"),
            ],
            check=True, capture_output=True,
        )
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
        self.assertEqual(
            inventory["main_video_path"], "main.mp4",
            "the name decides, even though broll.mp4 runs three times longer",
        )
        roles = {entry["path"]: entry["role"] for entry in inventory["files"]}
        self.assertEqual(roles["main.mp4"], "main_video")
        self.assertEqual(roles["broll.mp4"], "broll")
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
        self.assertTrue(any("broll.mp4" in path.name for path in imported))
        self.assertTrue(any("封面.png" in path.name for path in imported))
        self.assertFalse(any("notes.txt" in path.name for path in imported))

    def video(self, folder: Path, name: str, seconds: float) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        subprocess.run(
            [
                self.ffmpeg, "-y",
                "-f", "lavfi", "-i", f"testsrc=size=192x336:rate=30:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-shortest", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True, capture_output=True,
        )
        return path

    def main_video_of(self, folder: Path, project: Path, *extra: str) -> str:
        self.run_cli(
            "ingest-folder", "--folder", str(folder),
            "--project-dir", str(project), *extra,
        )
        inventory = json.loads(
            (project / "working/folder_inventory.json").read_text("utf-8")
        )
        return inventory["main_video_path"]

    def test_a_folder_with_several_videos_and_no_main_stops(self) -> None:
        # Picking the longest used to stand in for asking. It reads as a rule
        # until the day the B-roll runs longer than the talking, and then the
        # wrong video is cut with nothing said about it.
        folder = self.base / "ambiguous"
        self.video(folder, "alpha.mp4", 0.3)
        self.video(folder, "beta.mp4", 0.9)
        result = self.run_cli(
            "ingest-folder", "--folder", str(folder),
            "--project-dir", str(self.base / "project-ambiguous"), expected=2,
        )
        message = result.stdout + result.stderr
        self.assertIn("none is named main", message)
        self.assertIn("alpha.mp4", message, "it says which videos it found")
        self.assertIn("beta.mp4", message)

    def test_refusing_leaves_no_half_built_project(self) -> None:
        folder = self.base / "ambiguous-2"
        self.video(folder, "alpha.mp4", 0.3)
        self.video(folder, "beta.mp4", 0.9)
        project = self.base / "project-refused"
        self.run_cli(
            "ingest-folder", "--folder", str(folder),
            "--project-dir", str(project), expected=2,
        )
        self.assertFalse(
            (project / "project.json").is_file(),
            "a folder it would not read must not leave a project behind",
        )

    def test_the_name_beats_the_length(self) -> None:
        folder = self.base / "named"
        self.video(folder, "main.mp4", 0.3)
        self.video(folder, "much-longer.mp4", 0.9)
        self.assertEqual(
            self.main_video_of(folder, self.base / "project-named"), "main.mp4"
        )

    def test_the_extension_does_not_matter(self) -> None:
        folder = self.base / "named-mov"
        self.video(folder, "MAIN.MOV", 0.3)
        self.video(folder, "other.mp4", 0.9)
        self.assertEqual(
            self.main_video_of(folder, self.base / "project-mov"), "MAIN.MOV"
        )

    def test_two_files_both_named_main_stop_rather_than_choose(self) -> None:
        folder = self.base / "two-mains"
        self.video(folder, "main.mp4", 0.3)
        self.video(folder, "main.mov", 0.9)
        result = self.run_cli(
            "ingest-folder", "--folder", str(folder),
            "--project-dir", str(self.base / "project-two-mains"), expected=2,
        )
        self.assertIn("more than one file is named main", result.stdout + result.stderr)

    def test_one_video_needs_no_name(self) -> None:
        # Nothing to disambiguate, so nothing to ask about.
        folder = self.base / "single"
        self.video(folder, "whatever-i-called-it.mp4", 0.3)
        self.assertEqual(
            self.main_video_of(folder, self.base / "project-single"),
            "whatever-i-called-it.mp4",
        )

    def test_naming_it_explicitly_still_wins(self) -> None:
        folder = self.base / "explicit"
        self.video(folder, "main.mp4", 0.3)
        chosen = self.video(folder, "the-one-i-want.mp4", 0.9)
        self.assertEqual(
            self.main_video_of(
                folder, self.base / "project-explicit", "--main", str(chosen)
            ),
            "the-one-i-want.mp4",
        )

    def test_a_file_lying_about_what_it_is_stays_out(self) -> None:
        # An extension is a claim about content, and the renderer believes
        # it: a text file called cover.png reaches the frame as a picture
        # that cannot be decoded, and the failure lands inside ffmpeg
        # minutes later instead of here, while the file is still in hand.
        folder = self.base / "liars"
        self.video(folder, "main.mp4", 0.3)
        (folder / "fake.mp4").write_text("this is not a video at all", "utf-8")
        (folder / "fake.png").write_text("nor is this a picture", "utf-8")
        project = self.base / "project-liars"
        result = self.run_cli(
            "ingest-folder", "--folder", str(folder), "--project-dir", str(project)
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["assets_copied"], 0)
        self.assertEqual(len(payload["warnings"]), 2, payload["warnings"])
        for name in ("fake.mp4", "fake.png"):
            self.assertTrue(
                any(name in note for note in payload["warnings"]),
                f"{name} left out without saying so",
            )
        imported = list((project / "assets/imported").glob("*"))
        self.assertEqual(imported, [], "nothing unreadable reached the project")

    def test_a_still_picture_is_not_mistaken_for_a_broken_one(self) -> None:
        # A still has no duration. Checking for one rejected every real
        # photograph the folder was carrying.
        folder = self.base / "stills"
        self.video(folder, "main.mp4", 0.3)
        subprocess.run(
            [
                self.ffmpeg, "-y", "-f", "lavfi",
                "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1",
                str(folder / "photo.png"),
            ],
            check=True, capture_output=True,
        )
        project = self.base / "project-stills"
        result = self.run_cli(
            "ingest-folder", "--folder", str(folder), "--project-dir", str(project)
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["warnings"], [])
        self.assertTrue(
            any("photo.png" in path.name
                for path in (project / "assets/imported").glob("*"))
        )

    def test_a_symlink_cannot_carry_anything_into_the_project(self) -> None:
        # Following one would copy whatever it points at — a key, a
        # document — into a project directory that gets shared as a package.
        outside = self.base / "outside-the-folder"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("SECRET-CANARY-VALUE", "utf-8")
        folder = self.base / "with-links"
        self.video(folder, "main.mp4", 0.3)
        (folder / "notes.txt").symlink_to(outside / "secret.txt")
        (folder / "cover.png").symlink_to(outside / "secret.txt")
        project = self.base / "project-links"
        self.run_cli(
            "ingest-folder", "--folder", str(folder), "--project-dir", str(project)
        )
        leaked = [
            path for path in project.rglob("*")
            if path.is_file() and b"SECRET-CANARY-VALUE" in path.read_bytes()
        ]
        self.assertEqual(leaked, [], "a symlink carried outside content in")

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

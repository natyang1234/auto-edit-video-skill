"""Generated images: ask once per beat, and be able to find it again."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import generated_images as gi  # noqa: E402


class FakeResult:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class GeneratedImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="generated-images-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.calls: list[list[str]] = []

    def runner(self, image_bytes: bytes = b"\x89PNG\r\n\x1a\nfake", fail: bool = False):
        def run(command: list[str]) -> FakeResult:
            self.calls.append(command)
            if fail:
                return FakeResult(1, "bridge exploded")
            out = Path(command[command.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"image-{len(self.calls)}.png").write_bytes(image_bytes)
            # The bridge drops a metadata sidecar beside the picture.
            (out / f"image-{len(self.calls)}.json").write_text("{}", encoding="utf-8")
            return FakeResult(0)

        return run

    def generate(self, beat_id: str, prompt: str, **kwargs):
        return gi.generate_image(
            self.project,
            beat_id,
            prompt,
            runner=kwargs.pop("runner", self.runner()),
            **kwargs,
        )

    def test_the_same_beat_is_not_generated_twice(self) -> None:
        with unittest.mock.patch.object(
            gi, "bridge_status", return_value={"ready": True, "cdp": "x"}
        ):
            first = self.generate("beat-1", "a cat on a roof")
            self.assertTrue(first["ok"])
            self.assertFalse(first["reused"])

            again = self.generate("beat-1", "a cat on a roof")
            self.assertTrue(again["reused"], "the beat already has its picture")
            self.assertEqual(again["path"], first["path"])
            self.assertEqual(len(self.calls), 1, "the bridge must not run twice")

            # A different beat is a different picture.
            other = self.generate("beat-2", "a cat on a roof")
            self.assertFalse(other["reused"])
            self.assertEqual(len(self.calls), 2)

    def test_the_metadata_sidecar_is_not_mistaken_for_the_picture(self) -> None:
        with unittest.mock.patch.object(
            gi, "bridge_status", return_value={"ready": True, "cdp": "x"}
        ):
            record = self.generate("beat-1", "a cat on a roof")
        self.assertTrue(record["path"].endswith(".png"), record["path"])

    def test_rewording_the_same_beat_regenerates(self) -> None:
        with unittest.mock.patch.object(
            gi, "bridge_status", return_value={"ready": True, "cdp": "x"}
        ):
            self.generate("beat-1", "a cat on a roof")
            changed = self.generate("beat-1", "a dog on a roof")
            self.assertFalse(changed["reused"])
            self.assertEqual(len(self.calls), 2)
            ledger = json.loads((self.project / gi.LEDGER_REL).read_text("utf-8"))
            self.assertEqual(
                len([item for item in ledger["items"] if item["beat_id"] == "beat-1"]),
                1,
                "a beat keeps one picture, not a pile of them",
            )

    def test_the_prompt_stays_in_the_project(self) -> None:
        with unittest.mock.patch.object(
            gi, "bridge_status", return_value={"ready": True, "cdp": "x"}
        ):
            record = self.generate("beat-1", "  a cat on a roof  ")
        stored = self.project / gi.PROMPTS_REL / f"{record['prompt_sha256']}.txt"
        self.assertEqual(stored.read_text("utf-8").strip(), "a cat on a roof")
        # The ledger carries the digest, not the words.
        ledger = json.loads((self.project / gi.LEDGER_REL).read_text("utf-8"))
        self.assertNotIn("cat", json.dumps(ledger))

    def test_a_failed_bridge_leaves_nothing_behind(self) -> None:
        with unittest.mock.patch.object(
            gi, "bridge_status", return_value={"ready": True, "cdp": "x"}
        ):
            result = self.generate("beat-1", "a cat", runner=self.runner(fail=True))
        self.assertFalse(result["ok"])
        self.assertIn("bridge exploded", result["reason"])
        self.assertFalse((self.project / gi.LEDGER_REL).exists())

    def test_an_unreachable_browser_is_reported_not_raised(self) -> None:
        result = gi.generate_image(
            self.project, "beat-1", "a cat", cdp="http://127.0.0.1:9", runner=self.runner()
        )
        self.assertFalse(result["ok"])
        self.assertIn("browser", result["reason"])
        self.assertEqual(self.calls, [], "nothing should run without a browser")

    def test_unknown_bridge_is_refused(self) -> None:
        self.assertFalse(gi.bridge_status("not-a-bridge")["ready"])


if __name__ == "__main__":
    unittest.main()

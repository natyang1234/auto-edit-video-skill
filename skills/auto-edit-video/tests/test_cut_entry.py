"""What `cut` accepts, and what it refuses."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import auto_edit  # noqa: E402


class ArgumentSourcingTests(unittest.TestCase):
    """Sub-command arguments come from that sub-command's own parser."""

    def test_defaults_are_filled_by_the_command_that_declares_them(self) -> None:
        # Hand-assembling a Namespace means restating every default the
        # parser already declares; the one missed surfaced as an
        # AttributeError deep inside init.
        args = auto_edit._args_for(
            "init", "--input", "a.mp4", "--project-dir", "p"
        )
        for name in ("voice_language", "subtitle_mode", "platform", "emphasis"):
            self.assertTrue(hasattr(args, name), name)

    def test_an_unknown_flag_is_rejected_rather_than_ignored(self) -> None:
        with self.assertRaises(SystemExit):
            auto_edit._args_for("init", "--input", "a.mp4", "--not-a-flag", "x")


class EntryPointTests(unittest.TestCase):
    def parser(self):
        return auto_edit.build_parser()

    def test_a_video_is_accepted(self) -> None:
        args = self.parser().parse_args(["cut", "--input", "a.mp4", "--out", "o"])
        self.assertEqual(args.input, "a.mp4")
        self.assertEqual(args.folder, "")

    def test_a_folder_is_accepted(self) -> None:
        # The PRD's premise is that a folder is enough; it also carries the
        # pictures and B-roll that a single file cannot.
        args = self.parser().parse_args(["cut", "--folder", "f", "--out", "o"])
        self.assertEqual(args.folder, "f")
        self.assertEqual(args.input, "")

    def test_neither_is_refused_with_a_usable_message(self) -> None:
        args = self.parser().parse_args(["cut", "--out", "o"])
        code = auto_edit.cmd_cut(args)
        self.assertNotEqual(code, 0)

    def test_a_folder_that_is_not_there_is_refused(self) -> None:
        args = self.parser().parse_args(
            ["cut", "--folder", "/no/such/folder", "--out", "/tmp/x"]
        )
        self.assertNotEqual(auto_edit.cmd_cut(args), 0)

    def test_a_video_that_is_not_there_is_refused(self) -> None:
        args = self.parser().parse_args(
            ["cut", "--input", "/no/such/video.mp4", "--out", "/tmp/x"]
        )
        self.assertNotEqual(auto_edit.cmd_cut(args), 0)


if __name__ == "__main__":
    unittest.main()

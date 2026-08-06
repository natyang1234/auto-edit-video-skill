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


class DeliveryGateCallTests(unittest.TestCase):
    """The gate call is checkable without spending a render on it.

    It was not, and a call built with the wrong helper signature reached a
    real run with the whole suite green: nothing in the tests got as far as
    the line, because getting there meant transcribing and rendering first.
    """

    def project(self, canvas: dict | None) -> Path:
        import json
        import tempfile

        tmp = tempfile.TemporaryDirectory(prefix="auto-edit-gate-call-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "working").mkdir(parents=True)
        if canvas is not None:
            (root / "working/editor_state.json").write_text(
                json.dumps({"schema_version": 1, "canvas": canvas}), "utf-8"
            )
        return root

    def test_the_call_is_built_at_all(self) -> None:
        command = auto_edit.clip_qa_command(
            self.project(None), {}, Path("/out/clip.mp4")
        )
        self.assertIn("--video", command)
        self.assertIn("/out/clip.mp4", command)

    def test_a_letterboxed_clip_tells_the_gate_where_the_picture_is(self) -> None:
        command = auto_edit.clip_qa_command(
            self.project({"width": 1080, "height": 1920, "fit": "contain"}),
            {"source": {"width": 1920, "height": 1080}},
            Path("/out/clip.mp4"),
        )
        self.assertIn("--content-rect", command)

    def test_a_cropped_clip_claims_no_geometry(self) -> None:
        command = auto_edit.clip_qa_command(
            self.project({"width": 1080, "height": 1920, "fit": "cover"}),
            {"source": {"width": 1920, "height": 1080}},
            Path("/out/clip.mp4"),
        )
        self.assertNotIn("--content-rect", command)

    def test_a_project_with_no_state_yet_still_produces_a_call(self) -> None:
        # Not a crash: the gate runs on whatever the render produced even if
        # the state was never written.
        command = auto_edit.clip_qa_command(
            self.project(None),
            {"source": {"width": 1920, "height": 1080}},
            Path("/out/clip.mp4"),
        )
        self.assertNotIn("--content-rect", command)

    def test_the_gate_accepts_everything_the_call_passes(self) -> None:
        # The two sides are separate programs; an argument added on one side
        # and unknown on the other fails only at run time.
        import qa_video

        parser = qa_video.build_parser()
        command = auto_edit.clip_qa_command(
            self.project({"width": 1080, "height": 1920, "fit": "contain"}),
            {"source": {"width": 1920, "height": 1080}},
            Path("/out/clip.mp4"),
        )
        parsed = parser.parse_args(command[2:])
        self.assertTrue(parsed.content_rect)
        self.assertEqual(
            qa_video.parse_content_rect(parsed.content_rect)[3], 0.316406
        )


class ClipLengthTargetTests(unittest.TestCase):
    """A length to aim for only means something on a source longer than it.

    Asked for on 2026-08-06: set the seconds for long sources; when the
    source is shorter, go straight on. Imposing a thirty-second window on a
    twenty-second video turns something complete into something cut short.
    """

    def test_a_long_source_gets_the_target(self) -> None:
        self.assertEqual(auto_edit.cut_target_seconds(30.0, 480.0), 30.0)

    def test_a_source_shorter_than_the_target_is_left_alone(self) -> None:
        self.assertIsNone(auto_edit.cut_target_seconds(30.0, 20.0))

    def test_a_source_exactly_the_target_is_left_alone(self) -> None:
        # There is nothing to choose between: the whole thing is the clip.
        self.assertIsNone(auto_edit.cut_target_seconds(30.0, 30.0))

    def test_asking_for_no_length_sets_none(self) -> None:
        for requested in (0, 0.0, -5.0, float("nan")):
            with self.subTest(requested):
                self.assertIsNone(auto_edit.cut_target_seconds(requested, 480.0))

    def test_an_unknown_source_length_does_not_cancel_the_target(self) -> None:
        # Not knowing how long the source is, is not evidence that it is
        # short; the target stands until something says otherwise.
        self.assertEqual(auto_edit.cut_target_seconds(30.0, 0.0), 30.0)

    def test_nonsense_lengths_are_refused_rather_than_guessed(self) -> None:
        self.assertIsNone(auto_edit.cut_target_seconds("half a minute", 480.0))


class OneRouteForLengthAndPlatformTests(unittest.TestCase):
    """A folder and a file must reach the same target.

    They did not. A folder went to ingest-folder, which takes neither
    --seconds nor --platform, so both did nothing on that route and said so
    only in a warning buried in the output: the same decision made in two
    places, with one of them not making it.
    """

    def test_the_target_command_accepts_what_cut_sends_it(self) -> None:
        # Two sub-commands, one parser each; an argument added on one side
        # and unknown on the other fails only when a person runs it.
        parsed = auto_edit.build_parser().parse_args([
            "set-target", "--manifest", "m.json",
            "--platform", "instagram-reels", "--target-duration", "30.0",
        ])
        self.assertEqual(parsed.target_duration, 30.0)
        self.assertEqual(parsed.platform, "instagram-reels")

    def test_every_platform_cut_offers_is_one_the_target_accepts(self) -> None:
        parser = auto_edit.build_parser()
        for platform in auto_edit.PLATFORMS:
            with self.subTest(platform):
                parsed = parser.parse_args(
                    ["set-target", "--manifest", "m.json", "--platform", platform]
                )
                self.assertEqual(parsed.platform, platform)

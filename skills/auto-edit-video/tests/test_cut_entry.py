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


class PlanningWindowsTests(unittest.TestCase):
    """The director decides per segment, so it must be given more than one.

    A clip handed over as a single segment gave it exactly one decision to
    make, and — since the first segment is the opening — that decision was
    always the opening title. Every other kind of card was unreachable in
    the one command this tool is normally driven by.
    """

    def windows(self, start: float, end: float) -> list[dict]:
        return auto_edit.planning_segments(
            {"id": "segment-1", "source_start": start, "source_end": end}
        )

    def test_a_long_clip_is_read_in_several_windows(self) -> None:
        self.assertGreater(len(self.windows(0.0, 30.0)), 1)

    def test_a_short_clip_stays_whole(self) -> None:
        # Nothing to divide: slicing eight seconds into halves would only
        # make each half too small to say anything about.
        self.assertEqual(len(self.windows(0.0, 8.0)), 1)

    def test_the_windows_cover_the_clip_without_gaps_or_overlap(self) -> None:
        windows = self.windows(10.0, 42.0)
        self.assertAlmostEqual(windows[0]["source_start"], 10.0, places=3)
        self.assertAlmostEqual(windows[-1]["source_end"], 42.0, places=3)
        for earlier, later in zip(windows, windows[1:]):
            self.assertAlmostEqual(
                earlier["source_end"], later["source_start"], places=3
            )

    def test_every_window_stays_inside_the_clip(self) -> None:
        # Plan items are mapped onto the timeline the clip actually renders;
        # a window reaching past it would name time that is not there.
        for window in self.windows(5.0, 37.0):
            self.assertGreaterEqual(window["source_start"], 5.0)
            self.assertLessEqual(window["source_end"], 37.0)

    def test_windows_are_distinguishable(self) -> None:
        ids = [window["id"] for window in self.windows(0.0, 40.0)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_more_than_one_kind_of_card_becomes_reachable(self) -> None:
        # The point of the split, stated as a behaviour: a clip whose later
        # half asks a question can now carry a question card, which a single
        # segment could never produce.
        import visual_director

        evidence = [
            {"id": "evidence-aaaa1111", "kind": "quote",
             "literal": "今天要講的是這個", "start": 1.0, "end": 3.0},
            {"id": "evidence-bbbb2222", "kind": "quote",
             "literal": "為什麼大家都做錯", "start": 25.0, "end": 27.0},
        ]
        segment = {"id": "segment-1", "source_start": 0.0, "source_end": 32.0}
        whole = visual_director.plan_visuals(
            [segment], evidence, editorial_title="做錯的原因"
        )
        split = visual_director.plan_visuals(
            auto_edit.planning_segments(segment), evidence,
            editorial_title="做錯的原因",
        )
        self.assertEqual(
            [item["beat"] for item in whole["visual_plan"]["items"]], ["title"]
        )
        self.assertIn(
            "question", [item["beat"] for item in split["visual_plan"]["items"]]
        )


class MaterialiseClipVisualDensityTests(unittest.TestCase):
    def test_director_density_reaches_visual_planner(self) -> None:
        from unittest.mock import patch

        import editor_server
        import video_analyzer
        import visual_director

        state = {
            "segments": [{"id": "base", "source_start": 0.0, "source_end": 8.0}],
            "canvas": {},
            "director_style": "high-energy",
        }
        evidence = {
            "items": [{
                "id": "evidence-aaaa1111",
                "kind": "number",
                "literal": "87%",
                "start": 1.0,
                "end": 1.5,
            }]
        }
        captured: dict[str, object] = {}

        def fake_read_json(path, fallback=None):
            if str(path).endswith("evidence_map.json"):
                return evidence
            return fallback

        def fake_plan(*args, **kwargs):
            captured.update(kwargs)
            return {"visual_plan": {"items": []}, "structured_layers": {"items": []}}

        with (
            patch.object(editor_server, "default_editor_state", return_value=state),
            patch.object(editor_server, "read_json", side_effect=fake_read_json),
            patch.object(editor_server, "active_editorial_title", return_value=""),
            patch.object(editor_server, "publish_layer_bundle"),
            patch.object(video_analyzer, "atomic_write_json"),
            patch.object(visual_director, "plan_visuals", side_effect=fake_plan),
            patch.object(visual_director, "validate", return_value=[]),
        ):
            auto_edit.materialise_clip(
                Path("/tmp/auto-edit-density-test"),
                {},
                {"id": "highlight-aaaa1111", "start": 0.0, "end": 8.0},
                fit="contain",
                cards=True,
                trim_pauses=False,
            )

        self.assertEqual(
            captured["visual_density"],
            editor_server.DIRECTOR_PRESETS["high-energy"]["visual_density"],
        )


class DirectorRegistryContractTests(unittest.TestCase):
    def test_motion_intensity_matches_each_director_mode(self) -> None:
        import json

        payload = json.loads(
            (SKILL_DIR / "contracts/instances/director_mode__registry.json")
            .read_text("utf-8")
        )
        by_id = {mode["id"]: mode for mode in payload["modes"]}
        expected = {
            "teacher-punch": "medium",
            "high-energy": "high",
            "documentary": "low",
            "minimal": "low",
            "editorial-clean": "low",
        }
        for mode_id, intensity in expected.items():
            with self.subTest(mode_id=mode_id):
                self.assertEqual(by_id[mode_id]["envelope"]["motion_intensity"], intensity)


class GlossaryReachesTheProjectTests(unittest.TestCase):
    """Spellings the recogniser gets wrong on its own.

    `init` has taken a glossary since the beginning; `cut` never offered one,
    so on the one command this tool is driven by there was no way to supply
    any. A mis-heard word now reaches a card, where it is much more visible
    than in a caption: a real cut put 「巨型學校」 on screen for 「句型」.
    """

    def test_cut_accepts_a_glossary(self) -> None:
        args = auto_edit.build_parser().parse_args(
            ["cut", "--input", "a.mp4", "--out", "o", "--glossary", "cigar,cigarette"]
        )
        self.assertEqual(args.glossary, ["cigar,cigarette"])

    def test_cut_accepts_corrections(self) -> None:
        args = auto_edit.build_parser().parse_args(
            ["cut", "--input", "a.mp4", "--out", "o", "--fix", "句型=巨型;菸=yan"]
        )
        self.assertEqual(
            auto_edit.normalize_transcription_calibrations(args.fix),
            [
                {"canonical": "句型", "aliases": ["巨型"]},
                {"canonical": "菸", "aliases": ["yan"]},
            ],
        )

    def test_the_two_do_different_jobs(self) -> None:
        # A glossary only takes terms with Latin letters in them, so it
        # cannot touch a Chinese mis-hearing — asking it to would look like
        # the fix was applied when nothing happened.
        self.assertEqual(auto_edit.normalize_transcription_glossary(["句型"]), ["句型"])
        data = {"segments": [{"words": [{"word": "巨型", "start": 0.0, "end": 1.0}]}]}
        self.assertEqual(auto_edit.apply_glossary_corrections(data, ["句型"]), [])
        self.assertEqual(data["segments"][0]["words"][0]["word"], "巨型")

    def test_a_correction_rewrites_it_and_keeps_the_timing(self) -> None:
        data = {"segments": [{"words": [
            {"word": "這個", "start": 0.0, "end": 0.4},
            {"word": "巨型", "start": 0.4, "end": 1.0},
        ]}]}
        rules = auto_edit.normalize_transcription_calibrations(["句型=巨型"])
        applied = auto_edit.apply_transcription_calibrations(data, rules)
        self.assertEqual(applied[0]["to"], "句型")
        words = data["segments"][0]["words"]
        self.assertEqual("".join(word["word"] for word in words), "這個句型")
        self.assertEqual(words[-1]["start"], 0.4)
        self.assertEqual(words[-1]["end"], 1.0)

    def test_a_correction_can_turn_latin_into_chinese(self) -> None:
        # 菸 arriving as "yan" is the shape a glossary can never repair,
        # because a glossary only ever produces Latin.
        data = {"segments": [{"words": [{"word": "yan", "start": 0.0, "end": 0.3}]}]}
        auto_edit.apply_transcription_calibrations(
            data, auto_edit.normalize_transcription_calibrations(["菸=yan"])
        )
        self.assertEqual(data["segments"][0]["words"][0]["word"], "菸")

    def test_terms_are_split_the_same_way_init_splits_them(self) -> None:
        # One normaliser, so a term accepted on one route is accepted on the
        # other and split identically.
        self.assertEqual(
            auto_edit.normalize_transcription_glossary(["句型,虛主詞", "be 動詞"]),
            ["句型", "虛主詞", "be 動詞"],
        )

    def test_no_glossary_is_not_an_empty_glossary(self) -> None:
        args = auto_edit.build_parser().parse_args(
            ["cut", "--input", "a.mp4", "--out", "o"]
        )
        self.assertEqual(args.glossary, [])

    def test_an_oversized_term_is_refused_rather_than_truncated(self) -> None:
        with self.assertRaises(ValueError):
            auto_edit.normalize_transcription_glossary(["x" * 81])


class SavedTermsTests(unittest.TestCase):
    """A term list is only useful if it is kept.

    The same brand names and the same mis-hearings come back on every
    project; retyping them on each run is the same as not having them.
    """

    def write(self, payload) -> Path:
        import json
        import tempfile

        tmp = tempfile.TemporaryDirectory(prefix="auto-edit-terms-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "terms.json"
        path.write_text(
            payload if isinstance(payload, str)
            else __import__("json").dumps(payload, ensure_ascii=False),
            "utf-8",
        )
        return path

    def test_a_kept_list_is_read(self) -> None:
        path = self.write({"glossary": ["D-Town"], "fix": ["句型=巨型"]})
        self.assertEqual(auto_edit.saved_terms(path), (["D-Town"], ["句型=巨型"]))

    def test_no_file_means_nothing_kept(self) -> None:
        self.assertEqual(auto_edit.saved_terms(Path("/no/such/terms.json")), ([], []))

    def test_a_file_that_cannot_be_read_stops_rather_than_being_skipped(self) -> None:
        # Silently ignoring a list someone wrote is how a mis-heard word
        # reaches a card anyway, with the run reporting success.
        for payload in ("{not json", {"glossary": "D-Town"}, {"fix": [1, 2]}, "[]"):
            with self.subTest(payload):
                with self.assertRaises(ValueError):
                    auto_edit.saved_terms(self.write(payload))

    def test_missing_keys_are_allowed(self) -> None:
        self.assertEqual(auto_edit.saved_terms(self.write({})), ([], []))

    def test_the_kept_rules_survive_normalising(self) -> None:
        # What is stored has to be what the two normalisers accept, or the
        # file is a list of terms that quietly never apply.
        path = self.write({
            "glossary": ["D-Town", "cigar"],
            "fix": ["句型=巨型", "be 動詞=b動詞|b 動詞"],
        })
        glossary, fix = auto_edit.saved_terms(path)
        self.assertEqual(
            auto_edit.normalize_transcription_glossary(glossary), ["D-Town", "cigar"]
        )
        rules = auto_edit.normalize_transcription_calibrations(fix)
        self.assertEqual(rules[0], {"canonical": "句型", "aliases": ["巨型"]})
        self.assertEqual(rules[1]["aliases"], ["b動詞", "b 動詞"])

    def test_a_correction_with_context_spares_the_same_word_elsewhere(self) -> None:
        # 為了 is heard as "weight", but "to lose weight" is correct. The
        # rule carries the words around it so only the wrong one is rewritten.
        data = {"segments": [{"words": [
            {"word": "它叫做", "start": 0.0, "end": 0.5},
            {"word": " weight", "start": 0.5, "end": 1.0},
            {"word": "為了要減肥to lose", "start": 1.0, "end": 2.0},
            {"word": " weight", "start": 2.0, "end": 2.5},
        ]}]}
        auto_edit.apply_transcription_calibrations(
            data, auto_edit.normalize_transcription_calibrations(["叫做 為了=叫做 weight"])
        )
        joined = "".join(word["word"] for word in data["segments"][0]["words"])
        self.assertIn("叫做 為了", joined)
        self.assertIn("lose weight", joined)


class PauseTrimmingTests(unittest.TestCase):
    """Dead air is cut out of the clip; words never are.

    The analyzer has always proposed these edits and the renderer has always
    accepted a timeline split around removed regions — captions and cards
    map their windows across the gaps. `cut` simply never connected them.
    """

    def silence(self, start, end, kind="silence", risk="low"):
        return {"type": kind, "risk": risk, "start": start, "end": end}

    def test_a_pause_is_cut_with_breathing_room(self) -> None:
        cuts = auto_edit.silence_deletions([self.silence(5.0, 6.2)], 0.0, 30.0)
        self.assertEqual(cuts, [(5.12, 6.08)])

    def test_a_short_pause_is_not_worth_a_jump_cut(self) -> None:
        # After the breathing room a 0.35s pause has nothing left to cut;
        # cutting it anyway reads as a stutter in the picture.
        self.assertEqual(
            auto_edit.silence_deletions([self.silence(9.0, 9.35)], 0.0, 30.0), []
        )

    def test_words_are_never_cut_automatically(self) -> None:
        # A filler or a stutter has a caption; cutting its audio while the
        # caption still shows it desynchronises the two. Those stay
        # proposals for a person.
        for kind in ("filler", "stutter"):
            with self.subTest(kind):
                self.assertEqual(
                    auto_edit.silence_deletions(
                        [self.silence(5.0, 6.5, kind=kind)], 0.0, 30.0
                    ),
                    [],
                )

    def test_a_pause_outside_the_clip_is_ignored(self) -> None:
        self.assertEqual(
            auto_edit.silence_deletions([self.silence(50.0, 52.0)], 0.0, 30.0), []
        )

    def test_the_surviving_pieces_cover_everything_but_the_cuts(self) -> None:
        pieces = auto_edit.window_minus_deletions(
            4.0, 30.0, [(10.0, 11.0), (20.0, 21.5)]
        )
        self.assertEqual(pieces, [(4.0, 10.0), (11.0, 20.0), (21.5, 30.0)])

    def test_a_flash_frame_scrap_is_dropped(self) -> None:
        # A 0.1s sliver between two cuts is a flash, not a segment.
        pieces = auto_edit.window_minus_deletions(
            0.0, 10.0, [(2.0, 4.0), (4.1, 6.0)]
        )
        self.assertEqual(pieces, [(0.0, 2.0), (6.0, 10.0)])

    def test_cutting_everything_keeps_the_original_window(self) -> None:
        # Rather than delivering nothing, an all-silence verdict is treated
        # as a wrong verdict.
        self.assertEqual(
            auto_edit.window_minus_deletions(0.0, 1.0, [(0.0, 1.0)]),
            [(0.0, 1.0)],
        )

    def test_overlapping_proposals_merge_into_one_cut(self) -> None:
        cuts = auto_edit.silence_deletions(
            [self.silence(5.0, 6.0), self.silence(5.8, 7.0)], 0.0, 30.0
        )
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0][1], 6.88, places=2)

    def test_cut_offers_the_switch_and_defaults_to_trimming(self) -> None:
        args = auto_edit.build_parser().parse_args(
            ["cut", "--input", "a.mp4", "--out", "o"]
        )
        self.assertFalse(args.keep_pauses)

    def test_cut_defaults_to_final_quality_but_honors_explicit_quality(self) -> None:
        parser = auto_edit.build_parser()
        self.assertEqual(
            parser.parse_args(["cut", "--input", "a.mp4", "--out", "o"]).quality,
            "final",
        )
        for quality in ("preview", "final"):
            with self.subTest(quality=quality):
                args = parser.parse_args(
                    ["cut", "--input", "a.mp4", "--out", "o", "--quality", quality]
                )
                self.assertEqual(args.quality, quality)


class RotatedSourceTests(unittest.TestCase):
    """A phone stores the sensor frame and a rotation to display it by.

    Reporting the sensor's 1920x1080 for a portrait phone clip made the
    framing rule read it as wide — and handed the QA gate a letterbox
    exclusion for bars that do not exist, so two thirds of a portrait
    delivery went unjudged. Found on the first real phone video this tool
    was given.
    """

    def probe(self, rotation) -> dict:
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("ffmpeg"):
            self.skipTest("needs ffmpeg")
        tmp = tempfile.TemporaryDirectory(prefix="auto-edit-rotate-")
        self.addCleanup(tmp.cleanup)
        flat = Path(tmp.name) / "flat.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=red:s=320x180:d=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-shortest", "-pix_fmt", "yuv420p", str(flat)],
            check=True, capture_output=True,
        )
        if not rotation:
            return auto_edit.probe_media(flat)
        # A display matrix, the way a phone writes one — a rotate metadata
        # tag is a different thing and probe rightly ignores it.
        path = Path(tmp.name) / "clip.mov"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-display_rotation", str(rotation), "-i", str(flat),
             "-c", "copy", str(path)],
            check=True, capture_output=True,
        )
        return auto_edit.probe_media(path)

    def test_a_sideways_rotation_swaps_the_reported_dimensions(self) -> None:
        media = self.probe(90)
        self.assertEqual((media["width"], media["height"]), (180, 320))

    def test_no_rotation_reports_the_frame_as_stored(self) -> None:
        media = self.probe(0)
        self.assertEqual((media["width"], media["height"]), (320, 180))

    def test_upside_down_does_not_swap(self) -> None:
        media = self.probe(180)
        self.assertEqual((media["width"], media["height"]), (320, 180))

    def test_the_framing_rule_sees_the_displayed_shape(self) -> None:
        # The consequence that matters: a portrait phone clip fills a
        # portrait canvas instead of being letterboxed as if it were wide.
        portrait = self.probe(90)
        self.assertEqual(auto_edit.framing_for("auto", {"source": portrait}), "cover")


class ShortSourceIsTheClipTests(unittest.TestCase):
    """A source no longer than the requested length IS the clip.

    The duration rule said so; selection did not hear it. On a 19-second
    birthday video one run delivered only the last 7.6 seconds, because the
    picker chose a span the way it would inside a ten-minute lesson. The
    picker still names the clip; it does not get to shorten it.
    """

    def test_the_whole_source_ships_whichever_span_was_picked(self) -> None:
        import inspect

        source = inspect.getsource(auto_edit.cmd_cut)
        # The collapse happens only when no target applies (short source),
        # keeps the best-scored item's naming, and spans the full duration.
        self.assertIn("if target is None:", source)
        self.assertIn("start=0.0, end=round(source_duration, 3)", source)

"""Phase 3 SPEC v1 sub-slice 2: explicit type tokens and line-height rules.

docs/SPEC-phase3-bilingual-typography-v1.md §1-2. Three things this locks
down: the primary caption never autofits below a 40px floor (it wraps
instead), the secondary (translation) size is primary*0.62 with its own
32px hard floor that wins on conflict and is flagged rather than silently
shrunk further, and neither tier is ever allowed a third line — that is a
fail-closed condition, not a smaller caption.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as cc  # noqa: E402
import contract_registry  # noqa: E402


def monospace(width_per_char: float = 1.0):
    return lambda text: len(text) * width_per_char


class TypeTokenConstantsTests(unittest.TestCase):
    """The numbers in the SPEC table exist as named code, not magic literals."""

    def test_primary_floor_is_40px(self) -> None:
        self.assertEqual(cc.CAPTION_PRIMARY_FLOOR, 40.0)

    def test_secondary_floor_is_32px(self) -> None:
        self.assertEqual(cc.CAPTION_SECONDARY_FLOOR, 32.0)

    def test_secondary_scale_is_the_existing_translation_scale(self) -> None:
        self.assertEqual(cc.TRANSLATION_SCALE, 0.62)

    def test_line_height_multiples_match_the_spec_table(self) -> None:
        self.assertEqual(cc.LINE_HEIGHT_PRIMARY_SCALE, 1.25)
        self.assertEqual(cc.LINE_HEIGHT_SECONDARY_SCALE, 1.20)
        self.assertEqual(cc.BLOCK_GAP_SCALE, 0.35)

    def test_max_caption_lines_is_two(self) -> None:
        self.assertEqual(cc.MAX_CAPTION_LINES, 2)


class LineHeightHelperTests(unittest.TestCase):
    def test_primary_line_height_is_1_25x_font_size(self) -> None:
        self.assertAlmostEqual(cc.line_height_px(52.0), 65.0)

    def test_secondary_line_height_is_1_20x_font_size(self) -> None:
        self.assertAlmostEqual(cc.line_height_px(32.0, secondary=True), 38.4)

    def test_block_gap_is_0_35x_the_secondary_size(self) -> None:
        self.assertAlmostEqual(cc.block_gap_px(32.0), 11.2)


class SecondaryFontSizeTests(unittest.TestCase):
    """secondary = primary * 0.62, floored at 32px; the floor wins."""

    def test_a_comfortable_primary_size_uses_the_plain_ratio(self) -> None:
        size, needs_shortening = cc.secondary_font_size(60.0)
        self.assertAlmostEqual(size, 37.2)
        self.assertFalse(needs_shortening)

    def test_the_default_52px_primary_still_clears_the_floor(self) -> None:
        size, needs_shortening = cc.secondary_font_size(52.0)
        self.assertAlmostEqual(size, 52.0 * 0.62)
        self.assertFalse(needs_shortening)

    def test_a_shrunk_primary_forces_the_32px_floor_and_flags_it(self) -> None:
        # 40 * 0.62 = 24.8, below the 32px hard floor: the floor wins and
        # the caller must be told, not silently handed a smaller number.
        size, needs_shortening = cc.secondary_font_size(40.0)
        self.assertEqual(size, 32.0)
        self.assertTrue(needs_shortening)

    def test_never_returns_below_the_secondary_floor(self) -> None:
        size, _ = cc.secondary_font_size(10.0)
        self.assertGreaterEqual(size, 32.0)

    def test_the_floor_is_in_the_same_units_as_the_size_it_floors(self) -> None:
        # A preview renders at half scale, so the primary is 26px and the
        # SPEC's 32px floor is 16px in those units. Held at an absolute 32
        # the translation came out bigger than the line it translates, and
        # every preview caption claimed it needed shortening.
        size, needs_shortening = cc.secondary_font_size(26.0, render_scale=0.5)
        self.assertAlmostEqual(size, 26.0 * 0.62)
        self.assertLess(size, 26.0)
        self.assertFalse(needs_shortening)

    def test_a_scaled_floor_still_wins_when_the_ratio_falls_under_it(self) -> None:
        # 20 * 0.62 = 12.4, under the half-scale 16px floor.
        size, needs_shortening = cc.secondary_font_size(20.0, render_scale=0.5)
        self.assertAlmostEqual(size, 16.0)
        self.assertTrue(needs_shortening)


class FitCaptionTextFloorTests(unittest.TestCase):
    """SPEC §3's order: wrap at the caption's own size, shrink only after.

    The absolute 40px floor is where a caption fails closed (§1), which
    makes it the bound on the line-budget autofit of §3 step 2 — not a
    licence to shrink further while avoiding a wrap. Given as that licence
    it took an ordinary caption 18% down to keep it on one line, and every
    such caption then pinned its translation to the 32px floor.
    """

    @staticmethod
    def measure_at(size: float):
        return lambda text: len(text) * size

    def test_a_caption_that_only_fits_by_shrinking_hard_wraps_instead(self) -> None:
        # 22 chars at 52 needs 1144px, and 902px is only enough at 41 — a
        # fifth of the caption's size away. §3 step 1 wraps it at its own
        # size instead, which is two comfortable lines rather than one small
        # one.
        text = "一" * 22
        lines, size = cc.fit_caption_text(
            text, self.measure_at, 52.0, 902.0,
            floor_px=40.0, max_lines=cc.MAX_CAPTION_LINES,
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(size, 52.0)

    def test_the_floor_bounds_the_autofit_that_the_line_budget_forces(self) -> None:
        # 50 chars in 1040px is three lines at 52 and two only below 41.6,
        # under the legacy 42.64 ratio floor: this is the shrink §3 step 2
        # asks for, and the absolute floor is what stops it.
        text = "一" * 50
        lines, size = cc.fit_caption_text(
            text, self.measure_at, 52.0, 1040.0,
            floor_px=40.0, max_lines=cc.MAX_CAPTION_LINES,
        )
        self.assertEqual(len(lines), 2)
        self.assertGreaterEqual(size, 40.0)
        self.assertLess(size, 52.0 * cc.MIN_AUTOFIT_SCALE)

    def test_without_a_floor_px_the_legacy_ratio_still_applies(self) -> None:
        text = "一" * 22
        lines, size = cc.fit_caption_text(text, self.measure_at, 52.0, 902.0)
        self.assertGreater(len(lines), 1)
        self.assertGreaterEqual(size, 52.0 * cc.MIN_AUTOFIT_SCALE)

    def test_it_never_shrinks_below_the_absolute_floor(self) -> None:
        # Two lines are out of reach at every size down to the floor, so it
        # gives up rather than going under: the caller fails closed on the
        # line budget instead of drawing a caption nobody can read.
        text = "一" * 24
        lines, size = cc.fit_caption_text(
            text, self.measure_at, 52.0, 260.0,
            floor_px=40.0, max_lines=cc.MAX_CAPTION_LINES,
        )
        self.assertGreaterEqual(size, 40.0)
        self.assertGreater(len(lines), cc.MAX_CAPTION_LINES)


class CaptionOverflowRenderTests(unittest.TestCase):
    """Real CoreText raster: floors and the two-line cap as actually drawn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-typography-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def state(self, text: str, translation: str | None = None, max_width: float = 84):
        overlay = {
            "id": "caption-0001", "type": "caption", "text": text,
            "start": 0.0, "end": 2.0, "visible": True,
            "style": {"font_size": 52, "max_width": max_width},
        }
        if translation:
            overlay["translation"] = translation
        return {"canvas": {"width": 1080, "height": 1920}, "overlays": [overlay]}

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_third_line_fails_closed_instead_of_rendering(self) -> None:
        # A very narrow frame forces this into far more than two lines at
        # any legal size; the caption must be refused, not shrunk further
        # or silently drawn overflowing.
        text = "這是一段完全沒有辦法只用兩行放進這個超級窄畫面的中文字幕內容示範"
        with self.assertRaises(cc.CaptionOverflowError):
            cc.build_render_plan(self.project, self.state(text, max_width=8))

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_that_forces_the_floor_is_flagged_needs_shortening(self) -> None:
        # A frame just narrow enough to shrink the primary autofit, but not
        # so narrow it needs a second line: 52*0.62=32.24 clears the 32px
        # floor, but the shrunk size does not, so the floor must win and
        # the item must say so.
        text = "我們今天要介紹全新規格"
        translation = "a short translation line"
        plan = cc.build_render_plan(
            self.project, self.state(text, translation=translation, max_width=50)
        )
        item = plan["items"][0]
        self.assertIn("typography", item)
        typography = item["typography"]
        self.assertLess(typography["primary_font_size"], 52.0)
        self.assertEqual(typography["secondary_font_size"], 32.0)
        self.assertTrue(typography["needs_shortening"])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_render_plan_stays_contract_valid_with_typography_metadata(self) -> None:
        plan = cc.build_render_plan(self.project, self.state("看到 想到 為什麼"))
        self.assertEqual(
            contract_registry.validate_artifact("caption_render_plan", plan), []
        )
        self.assertIn("typography", plan["items"][0])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_explicit_line_height_constant_actually_drives_the_raster(self) -> None:
        # Not just bookkeeping: doubling the primary line-height multiple
        # must make the raster of a two-line caption taller by roughly the
        # same amount, or the paragraph style above is not doing its job.
        text = "看到 想到 為什麼 這樣子 完全不對"
        baseline = cc.build_render_plan(
            self.project, self.state(text, max_width=40)
        )["items"][0]
        self.assertEqual(baseline["typography"]["primary_line_count"], 2)
        baseline_height = baseline["artifact"]["height"]

        second_tmp = tempfile.TemporaryDirectory(prefix="phase3-typography-tests-2-")
        second_project = Path(second_tmp.name)
        (second_project / "working").mkdir()
        self.addCleanup(second_tmp.cleanup)
        old_scale = cc.LINE_HEIGHT_PRIMARY_SCALE
        cc.LINE_HEIGHT_PRIMARY_SCALE = old_scale * 2.0
        try:
            doubled = cc.build_render_plan(
                second_project, self.state(text, max_width=40)
            )["items"][0]
        finally:
            cc.LINE_HEIGHT_PRIMARY_SCALE = old_scale
        self.assertGreater(doubled["artifact"]["height"], baseline_height + 30)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_never_renders_below_the_32px_floor(self) -> None:
        text = "我們今天要介紹全新規格"
        translation = "an extremely long translated sentence that keeps going and going"
        plan = cc.build_render_plan(
            self.project, self.state(text, translation=translation, max_width=60)
        )
        self.assertGreaterEqual(
            plan["items"][0]["typography"]["secondary_font_size"], 32.0
        )


class LaidOutLineCountTests(unittest.TestCase):
    """The two-line cap is about the frame, not about the wrapper's opinion.

    `_wrap_translation` hands back one "line" whenever it finds nothing to
    break on — but CoreText then breaks the same run wherever it likes and
    draws seven. Counting the wrapper's list is counting an intention; the
    lines the framesetter produced are the caption. So the cap is enforced,
    and reported, on what was laid out.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-laidout-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def state(self, text: str, translation: str | None = None, max_width: float = 84):
        overlay = {
            "id": "caption-0001", "type": "caption", "text": text,
            "start": 0.0, "end": 2.0, "visible": True,
            "style": {"font_size": 52, "max_width": max_width},
        }
        if translation:
            overlay["translation"] = translation
        return {"canvas": {"width": 1080, "height": 1920}, "overlays": [overlay]}

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_an_unbreakable_translation_fails_closed_instead_of_drawing_seven_lines(
        self,
    ) -> None:
        # 312 characters with nowhere to wrap: the wrapper returns it as a
        # single line, the framesetter draws it as seven or eight, and the
        # caption used to render exit 0 with secondary_line_count 1.
        with self.assertRaises(cc.CaptionOverflowError):
            cc.build_render_plan(
                self.project,
                self.state("我們今天要介紹全新規格", translation="a" * 312),
            )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_an_unbreakable_spoken_caption_fails_closed_too(self) -> None:
        # The same hole existed one tier up: an unbreakable primary run was
        # wrapped as one line, drawn on eleven, and reported as one.
        with self.assertRaises(cc.CaptionOverflowError):
            cc.build_render_plan(self.project, self.state("a" * 312))

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_drawn_on_two_lines_is_reported_as_two(self) -> None:
        # Same shape, short enough that the framesetter fits it inside the
        # two-line cap: legal, so it renders — and the count it renders at
        # is the count the plan has to carry.
        item = cc.build_render_plan(
            self.project,
            self.state("我們今天要介紹全新規格", translation="a" * 60),
        )["items"][0]
        typography = item["typography"]
        self.assertEqual(typography["secondary_line_count"], 2)
        self.assertIsNotNone(typography["measured"]["secondary_line_pitch"])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_drawn_on_one_line_still_reports_one(self) -> None:
        # The counterpart: a translation that really is one line must not be
        # inflated by the new measurement, and a single line has no pitch.
        item = cc.build_render_plan(
            self.project,
            self.state("我們今天要介紹全新規格", translation="a short line"),
        )["items"][0]
        typography = item["typography"]
        self.assertEqual(typography["secondary_line_count"], 1)
        self.assertIsNone(typography["measured"]["secondary_line_pitch"])


class MeasuredLayoutTests(unittest.TestCase):
    """The line height in the SPEC is the pitch in the raster, not a label.

    CoreText adds the font's own leading on top of a clamped line height, so
    a caption asked for 1.25x came out at 1.75x while every number the plan
    reported said 1.25x. These read the layout back out of the frame that was
    actually drawn.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-measured-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def project_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="phase3-measured-tests-")
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        (project / "working").mkdir()
        return project

    def state(self, text: str, translation: str | None = None, max_width: float = 84):
        overlay = {
            "id": "caption-0001", "type": "caption", "text": text,
            "start": 0.0, "end": 2.0, "visible": True,
            "style": {"font_size": 52, "max_width": max_width},
        }
        if translation:
            overlay["translation"] = translation
        return {"canvas": {"width": 1080, "height": 1920}, "overlays": [overlay]}

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_two_spoken_lines_sit_exactly_one_line_height_apart(self) -> None:
        item = cc.build_render_plan(
            self.project, self.state("看到 想到 為什麼 這樣子 完全不對", max_width=40)
        )["items"][0]
        typography = item["typography"]
        self.assertEqual(typography["primary_line_count"], 2)
        self.assertAlmostEqual(
            typography["measured"]["primary_line_pitch"],
            cc.LINE_HEIGHT_PRIMARY_SCALE * typography["primary_font_size"],
            delta=1.0,
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_raster_is_as_tall_as_the_line_heights_say_it_is(self) -> None:
        # Black-box: two lines of ink plus the padding on each side, nothing
        # else. The leftover leading showed up here as ~50px of empty raster.
        item = cc.build_render_plan(
            self.project, self.state("看到 想到 為什麼 這樣子 完全不對", max_width=40)
        )["items"][0]
        typography = item["typography"]
        expected = (
            2 * cc.LINE_HEIGHT_PRIMARY_SCALE * typography["primary_font_size"]
            + 2 * item["artifact"]["padding"]
        )
        self.assertAlmostEqual(item["artifact"]["height"], expected, delta=3.0)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_translation_block_sits_one_block_gap_below_the_spoken_one(self) -> None:
        item = cc.build_render_plan(
            self.project,
            self.state("看到 想到 為什麼", translation="what you see and think"),
        )["items"][0]
        typography = item["typography"]
        self.assertAlmostEqual(
            typography["measured"]["block_gap"],
            cc.BLOCK_GAP_SCALE * typography["secondary_font_size"],
            delta=1.0,
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_two_translation_lines_use_the_secondary_line_height(self) -> None:
        item = cc.build_render_plan(
            self.project,
            self.state(
                "看到 想到 為什麼",
                translation="what you see and what you then think",
                max_width=45,
            ),
        )["items"][0]
        typography = item["typography"]
        self.assertGreaterEqual(typography["secondary_line_count"], 2)
        self.assertAlmostEqual(
            typography["measured"]["secondary_line_pitch"],
            cc.LINE_HEIGHT_SECONDARY_SCALE * typography["secondary_font_size"],
            delta=1.0,
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_translation_height_is_the_height_the_translation_added(self) -> None:
        # spoken_height is the only number the renderer's y-shift reads
        # (render_editor_timeline.captionized_overlays). Computed from a
        # formula instead of the drawn frame it disagreed with the raster by
        # tens of pixels, and the spoken line drifted off its declared y.
        text = "看到 想到 為什麼"
        plain = cc.build_render_plan(self.project_dir(), self.state(text))["items"][0]
        translated = cc.build_render_plan(
            self.project_dir(), self.state(text, translation="what you see and think")
        )["items"][0]
        added = translated["artifact"]["height"] - plain["artifact"]["height"]
        reported = (
            translated["artifact"]["height"] - translated["artifact"]["spoken_height"]
        )
        self.assertGreater(added, 0)
        self.assertAlmostEqual(reported, added, delta=3.0)
        self.assertAlmostEqual(
            translated["typography"]["measured"]["translation_height"],
            added,
            delta=3.0,
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_preview_translation_stays_smaller_than_the_line_it_translates(self) -> None:
        # render_scale 0.5: an absolute 32px floor made the English larger
        # than the Chinese above it, and flagged a perfectly ordinary
        # caption as needing shortening.
        plan = cc.build_render_plan(
            self.project,
            self.state("看到 想到 為什麼", translation="what you see and think"),
            render_scale=0.5,
        )
        typography = plan["items"][0]["typography"]
        self.assertLess(
            typography["secondary_font_size"], typography["primary_font_size"]
        )
        self.assertFalse(typography["needs_shortening"])

    # --- emphasis and line height -------------------------------------
    # A clamped line height is a ceiling as well as a floor: pinned at
    # 1.25x the *base* size, an emphasised run whose ascent is taller than
    # that made CoreText compress the line box instead of opening it, and
    # two CJK lines ended up with their faces touching (1.18 drew a 61px
    # pitch where the tokens said 65; 1.8 drew 42.2px, less than the
    # caption's own 44.2px type size). Line height follows the tallest run
    # on the line, and never falls under 1.25x the base.

    # Long enough to break into two lines even at the narrower wrap width an
    # emphasised run reserves, short enough not to need a third.
    TWO_LINE_TEXT = "看到 想到 為什麼 這樣子 完全不對 真的很"

    def emphasised(
        self,
        text: str,
        font_scale: float,
        max_width: float = 84,
        start_char: int = 0,
        length: int = 2,
    ):
        state = self.state(text, max_width=max_width)
        state["overlays"][0]["effect_spans"] = [
            {
                "start_char": start_char,
                "end_char": start_char + length,
                "style": {"effect": "pop", "font_scale": font_scale},
            }
        ]
        return state

    def measured_pitch(self, state) -> tuple[float, float]:
        item = cc.build_render_plan(self.project_dir(), state)["items"][0]
        typography = item["typography"]
        self.assertEqual(typography["primary_line_count"], 2)
        return (
            typography["measured"]["primary_line_pitch"],
            typography["primary_font_size"],
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_modestly_emphasised_run_opens_the_line_rather_than_squashing_it(
        self,
    ) -> None:
        # The pipeline's own default emphasis (editor_server/graphic_package
        # both ship font_scale 1.18): it drew a 61.0px pitch where the tokens
        # said 65.0 — emphasis made the lines *tighter*.
        pitch, base = self.measured_pitch(self.emphasised(self.TWO_LINE_TEXT, 1.18))
        self.assertAlmostEqual(
            pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base * 1.18, delta=1.0
        )
        self.assertGreaterEqual(pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base - 1.0)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_an_extreme_emphasis_never_pulls_the_lines_under_their_own_size(
        self,
    ) -> None:
        pitch, base = self.measured_pitch(self.emphasised(self.TWO_LINE_TEXT, 1.8))
        self.assertAlmostEqual(
            pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base * 1.8, delta=1.0
        )
        # The failure this guards: pitch smaller than the type size, i.e.
        # the two rows of CJK faces overlapping.
        self.assertGreater(pitch, base)

    # The block's pitch is a property of the block (emphasis_line_scale
    # applies the tallest run's scale to the whole spoken block on purpose),
    # so it cannot depend on which line the emphasised run happens to fall
    # on. It did: the compensation that fixed the short pitch could only add
    # spacing, and a span on the second line makes the drawn pitch *longer*
    # than the pin (1.18 drew 80.7 against 76.7; 1.8 drew 132 against 117),
    # which nothing could pull back.

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_pitch_is_the_same_wherever_the_emphasis_falls(self) -> None:
        for font_scale in (1.18, 1.8):
            for start_char in range(0, len(self.TWO_LINE_TEXT) - 1):
                with self.subTest(font_scale=font_scale, start_char=start_char):
                    pitch, base = self.measured_pitch(
                        self.emphasised(
                            self.TWO_LINE_TEXT, font_scale, start_char=start_char
                        )
                    )
                    self.assertAlmostEqual(
                        pitch,
                        cc.LINE_HEIGHT_PRIMARY_SCALE * base * font_scale,
                        delta=1.0,
                    )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_emphasis_on_the_second_line_keeps_the_pinned_pitch(self) -> None:
        # The caption is exactly two lines (measured_pitch asserts it), so
        # its last two characters are on the second one by construction —
        # no assumption about where the break lands.
        for font_scale in (1.18, 1.8):
            with self.subTest(font_scale=font_scale):
                pitch, base = self.measured_pitch(
                    self.emphasised(
                        self.TWO_LINE_TEXT,
                        font_scale,
                        start_char=len(self.TWO_LINE_TEXT) - 2,
                    )
                )
                self.assertAlmostEqual(
                    pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base * font_scale, delta=1.0
                )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_emphasis_spanning_the_line_break_keeps_the_pinned_pitch(self) -> None:
        # A span over the whole caption necessarily crosses the break, so
        # both lines carry the taller run and neither is the odd one out.
        for font_scale in (1.18, 1.8):
            with self.subTest(font_scale=font_scale):
                pitch, base = self.measured_pitch(
                    self.emphasised(
                        self.TWO_LINE_TEXT,
                        font_scale,
                        start_char=0,
                        length=len(self.TWO_LINE_TEXT),
                    )
                )
                self.assertAlmostEqual(
                    pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base * font_scale, delta=1.0
                )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_an_unemphasised_caption_keeps_exactly_the_1_25_pitch(self) -> None:
        # The case that was already exact, pinned so making emphasis work
        # cannot loosen it: no spans, no scaling, 1.25x to the pixel.
        pitch, base = self.measured_pitch(
            self.state("看到 想到 為什麼 這樣子 完全不對", max_width=40)
        )
        self.assertAlmostEqual(pitch, cc.LINE_HEIGHT_PRIMARY_SCALE * base, delta=1.0)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_emphasis_does_not_move_the_translation_tier(self) -> None:
        # Emphasis lives on the spoken tier. Opening the spoken lines up for
        # a big run must not drag the translation's own pitch or the gap
        # between the blocks along with it: both stay on their own tokens.
        marked = self.emphasised(self.TWO_LINE_TEXT, 1.8)
        marked["overlays"][0]["translation"] = (
            "what you see and what you then think about it"
        )
        typography = cc.build_render_plan(self.project_dir(), marked)["items"][0][
            "typography"
        ]
        secondary = typography["secondary_font_size"]
        self.assertGreaterEqual(typography["secondary_line_count"], 2)
        self.assertAlmostEqual(
            typography["measured"]["secondary_line_pitch"],
            cc.LINE_HEIGHT_SECONDARY_SCALE * secondary,
            delta=1.0,
        )
        self.assertAlmostEqual(
            typography["measured"]["block_gap"],
            cc.BLOCK_GAP_SCALE * secondary,
            delta=1.0,
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_caption_without_a_translation_reports_no_secondary_tier(self) -> None:
        # It reported the primary's own size as the secondary size, which
        # reads as a translation drawn at full size that does not exist.
        item = cc.build_render_plan(self.project, self.state("看到 想到"))["items"][0]
        typography = item["typography"]
        self.assertIsNone(typography["secondary_font_size"])
        self.assertEqual(typography["secondary_line_count"], 0)
        self.assertEqual(typography["measured"]["translation_height"], 0)
        self.assertIsNone(typography["measured"]["block_gap"])


class PlanCacheKnowsAboutTypographyTests(unittest.TestCase):
    """A cached plan from a different set of type tokens is not this plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-cache-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def state(self):
        return {
            "canvas": {"width": 1080, "height": 1920},
            "overlays": [{
                "id": "caption-0001", "type": "caption", "text": "看到 想到",
                "start": 0.0, "end": 2.0, "visible": True,
                "style": {"font_size": 52},
            }],
        }

    def test_changing_the_typography_revision_changes_the_cache_key(self) -> None:
        canvas = {"width": 1080, "height": 1920}
        before = cc.caption_content_revision(self.state(), canvas, 1.0)
        original = cc.TYPOGRAPHY_REVISION
        cc.TYPOGRAPHY_REVISION = original + "-changed"
        try:
            after = cc.caption_content_revision(self.state(), canvas, 1.0)
        finally:
            cc.TYPOGRAPHY_REVISION = original
        self.assertNotEqual(before, after)

    def test_changing_a_type_token_changes_the_cache_key(self) -> None:
        canvas = {"width": 1080, "height": 1920}
        before = cc.caption_content_revision(self.state(), canvas, 1.0)
        original = cc.LINE_HEIGHT_PRIMARY_SCALE
        cc.LINE_HEIGHT_PRIMARY_SCALE = original * 2.0
        try:
            after = cc.caption_content_revision(self.state(), canvas, 1.0)
        finally:
            cc.LINE_HEIGHT_PRIMARY_SCALE = original
        self.assertNotEqual(before, after)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_cached_plan_that_no_longer_validates_is_rebuilt(self) -> None:
        # The revision only covered content, so a plan written before the
        # typography block existed was served back unchanged — a plan the
        # current contract rejects, handed out without ever being checked.
        import json

        plan_path = self.project / cc.RENDER_PLAN_REL
        cc.build_render_plan(self.project, self.state())
        stale = json.loads(plan_path.read_text("utf-8"))
        for item in stale["items"]:
            item.pop("typography", None)
        plan_path.write_text(json.dumps(stale, ensure_ascii=False), "utf-8")

        rebuilt = cc.build_render_plan(self.project, self.state())
        self.assertIn("typography", rebuilt["items"][0])
        self.assertEqual(
            contract_registry.validate_artifact("caption_render_plan", rebuilt), []
        )


class TranslationRangeUnitTests(unittest.TestCase):
    """Which lines belong to the translation, measured in one unit.

    The block split asks "is this line at or past where the translation
    starts", and until this class existed the two sides of that comparison
    were counted differently: the boundary came from Python string lengths
    (code points) while the line's own location comes from CoreText
    (UTF-16 code units). For text inside the BMP those are the same number,
    which is why it held for years; every emoji in a caption makes them
    drift apart by one, and the drift is one-directional — a *spoken* line
    is pushed past the boundary and counted as translation.

    Everything downstream of that split is then wrong at once: the two-line
    cap (SPEC Phase 3 v1 §4) is enforced per block, so a spoken line moved
    into the translation bucket both hides a spoken overflow and invents a
    translation overflow; the reported line counts describe a layout that
    was not drawn; and the measured pitch/gap are read off the wrong
    baselines.

    These tests pin the two ends. The control in each is the same caption
    with no emoji: emoji do not change how many lines a run needs, so any
    difference between the two is the unit bug and nothing else.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-utf16-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def state(self, text: str, translation: str | None = None, max_width: float = 84):
        overlay = {
            "id": "caption-0001", "type": "caption", "text": text,
            "start": 0.0, "end": 2.0, "visible": True,
            "style": {"font_size": 52, "max_width": max_width},
        }
        if translation:
            overlay["translation"] = translation
        return {"canvas": {"width": 1080, "height": 1920}, "overlays": [overlay]}

    def typography(self, text: str, translation: str | None = None) -> dict:
        return cc.build_render_plan(
            self.project, self.state(text, translation=translation)
        )["items"][0]["typography"]

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_one_line_translation_under_emoji_is_reported_as_one_line(self) -> None:
        # 36 emoji wrap onto two spoken lines — the same two the caption
        # renders with no translation at all, asserted first so the number
        # below is compared against something measured, not assumed. The
        # translation is a single short word, so the only honest answer is
        # 2 + 1; the code-point boundary sits mid-emoji-run and hands the
        # second spoken line to the translation, which reported 2 + 2.
        self.assertEqual(self.typography("\U0001F600" * 36)["primary_line_count"], 2)
        typography = self.typography("\U0001F600" * 36, "ok")
        self.assertEqual(typography["primary_line_count"], 2)
        self.assertEqual(typography["secondary_line_count"], 1)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_emoji_do_not_shrink_the_reported_spoken_block(self) -> None:
        # Further past the boundary the first spoken line goes too, and the
        # plan claims a one-line caption for a block drawn on two.
        text = "\U0001F600" * 38 + "短"
        self.assertEqual(self.typography(text)["primary_line_count"], 2)
        typography = self.typography(text, "ok")
        self.assertEqual(typography["primary_line_count"], 2)
        self.assertEqual(typography["secondary_line_count"], 1)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_legal_two_plus_two_caption_is_not_refused_for_containing_emoji(
        self,
    ) -> None:
        # The expensive half of the same bug: two spoken lines and a
        # two-line translation is exactly what §4 allows, and the control
        # renders it. Add emoji to the spoken text and spoken lines pile
        # into the translation bucket until it counts three, and a caption
        # that fits fails closed — an emoji in the transcript taking the
        # whole cut down.
        translation = "a" * 60
        control = self.typography("看到 想到 為什麼 這樣子 完全不對", translation)
        self.assertEqual(control["secondary_line_count"], 2)

        text = "\U0001F600" * 38 + "短"
        self.assertEqual(self.typography(text)["primary_line_count"], 2)
        typography = self.typography(text, translation)
        self.assertEqual(typography["primary_line_count"], 2)
        self.assertEqual(typography["secondary_line_count"], 2)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_measured_layout_is_read_off_the_right_baselines(self) -> None:
        # The counts are the visible symptom; the measurements come off the
        # same split. A two-line spoken block has a primary pitch, and a
        # one-line translation has no secondary pitch — with the buckets
        # crossed the caption reported the opposite of both.
        typography = self.typography("\U0001F600" * 36, "ok")
        measured = typography["measured"]
        self.assertIsNotNone(measured["primary_line_pitch"])
        self.assertIsNone(measured["secondary_line_pitch"])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_emoji_free_captions_are_unaffected(self) -> None:
        # The regression guard for the fix itself: for text with no
        # surrogate pairs the two units agree, so nothing about these
        # numbers may move.
        typography = self.typography("看到 想到 為什麼 這樣子 完全不對", "a" * 60)
        self.assertEqual(typography["primary_line_count"], 1)
        self.assertEqual(typography["secondary_line_count"], 2)


if __name__ == "__main__":
    unittest.main()

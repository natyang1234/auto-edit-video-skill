"""A second caption line, from request to pixels.

Three separate faults each made `--translate` look like it worked while
delivering nothing: the translator's failure was swallowed, the cache served
single-line rasters because the key ignored translations, and when the line
did render the taller block climbed up over the picture.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as compositor  # noqa: E402
import editorial_planner  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402


class CacheKnowsAboutTranslationsTests(unittest.TestCase):
    def state(self, translation=None):
        overlay = {
            "id": "caption-0001", "type": "caption", "visible": True,
            "text": "我們是無煙家庭", "style": {"font_size": 52},
        }
        if translation:
            overlay["translation"] = translation
        return {"overlays": [overlay]}

    def test_adding_a_translation_changes_the_revision(self) -> None:
        # It did not, so the cached single-line rasters were served as if
        # nothing had happened — a silent no for the whole feature.
        canvas = {"width": 1080, "height": 1920}
        plain = compositor.caption_content_revision(self.state(), canvas, 1.0)
        translated = compositor.caption_content_revision(
            self.state("We are a smoke-free family."), canvas, 1.0
        )
        self.assertNotEqual(plain, translated)


class SpokenLineStaysPutTests(unittest.TestCase):
    def overlay_for(self, artifact):
        plan = {"items": [{"caption_item_id": "caption-0001", "artifact": artifact}]}
        source = [{
            "id": "caption-0001", "type": "caption", "visible": True,
            "text": "hello", "start": 0.0, "end": 2.0,
            "style": {"x": 50, "y": 72},
        }]
        return renderer.captionized_overlays(source, plan, 1080, 1920)[0]

    def test_a_translated_caption_moves_down_by_half_the_added_height(self) -> None:
        # The declared y positions the spoken line. Centring the taller
        # block at the same y pushed the spoken line up over the picture.
        moved = self.overlay_for({
            "rgba_path": "working/captions/c.png", "width": 800,
            "height": 200, "padding": 8, "spoken_height": 100,
        })
        self.assertAlmostEqual(moved["style"]["y"], 72 + 50 / 1920 * 100, places=4)

    def test_an_untranslated_caption_does_not_move(self) -> None:
        still = self.overlay_for({
            "rgba_path": "working/captions/c.png", "width": 800,
            "height": 100, "padding": 8, "spoken_height": 100,
        })
        self.assertEqual(still["style"]["y"], 72)


REELS_SAFE = {"top": 8, "right": 8, "bottom": 18, "left": 8}


class CaptionWrapRespectsThePlatformTests(unittest.TestCase):
    """A caption wraps to the director's own max_width, chosen for how it
    looks and unaware of any platform. Reaching past the platform's own
    reserved column is not a style choice once this frame is going there."""

    def caption(self, max_width):
        return {
            "id": "caption-0001", "type": "caption", "visible": True,
            "text": "hello", "style": {"x": 50, "y": 72, "max_width": max_width},
        }

    def test_a_wider_wrap_than_the_platform_allows_is_narrowed(self) -> None:
        narrowed = renderer.constrain_caption_wrap_to_safe_area(
            [self.caption(86)], REELS_SAFE
        )
        self.assertEqual(narrowed[0]["style"]["max_width"], 84)

    def test_a_wrap_already_inside_the_platform_is_untouched(self) -> None:
        untouched = renderer.constrain_caption_wrap_to_safe_area(
            [self.caption(80)], REELS_SAFE
        )
        self.assertEqual(untouched[0]["style"]["max_width"], 80)

    def test_without_a_known_platform_nothing_is_narrowed(self) -> None:
        untouched = renderer.constrain_caption_wrap_to_safe_area(
            [self.caption(86)], None
        )
        self.assertEqual(untouched[0]["style"]["max_width"], 86)

    def test_a_non_caption_overlay_is_untouched(self) -> None:
        card = {"id": "visual-beat-1", "type": "image", "visible": True,
                "style": {"x": 50, "y": 42, "max_width": 90}}
        untouched = renderer.constrain_caption_wrap_to_safe_area([card], REELS_SAFE)
        self.assertEqual(untouched[0]["style"]["max_width"], 90)

    def test_an_asymmetric_platform_margin_also_recentres_the_wrap(self) -> None:
        # TikTok's real registry preset: left 8, right 14. Narrowing the
        # width to what both margins leave (78) is not enough on its own —
        # a block still centred at x=50 spans [11, 89] and still crosses
        # the tighter right margin at 86. The safe column's own centre,
        # not the frame's, is what a narrowed block must sit on.
        tiktok_safe = {"top": 8, "right": 14, "bottom": 20, "left": 8}
        narrowed = renderer.constrain_caption_wrap_to_safe_area(
            [self.caption(90)], tiktok_safe
        )
        style = narrowed[0]["style"]
        self.assertAlmostEqual(style["max_width"], 78.0, places=6)
        self.assertAlmostEqual(style["x"], 47.0, places=6)


class CaptionBlockClearsThePlatformMarginTests(unittest.TestCase):
    """A translation grows the block downward from the spoken line without
    knowing where the platform's own controls sit. Moving the block up is
    the only thing that keeps a normal caption clear of them."""

    def image_caption(self, y, caption_kind="caption", overlay_id="caption-instance-abc"):
        return {
            "id": overlay_id, "type": "image", "visible": True,
            "start": 0.0, "end": 1.0,
            "drawn": {"width": 500, "height": 500, "padding": 0},
            "style": {"width": 50.0, "x": 50, "y": y},
            "caption_kind": caption_kind,
        }

    def test_a_block_reaching_past_the_margin_is_pulled_up_to_meet_it(self) -> None:
        safe = {"top": 0, "right": 0, "bottom": 20, "left": 0}
        moved = renderer.clamp_captions_into_safe_area(
            [self.image_caption(90.0)], safe, 1000, 1000
        )
        self.assertAlmostEqual(moved[0]["style"]["y"], 55.0, places=6)

    def test_a_block_already_clear_of_the_margin_is_untouched(self) -> None:
        safe = {"top": 0, "right": 0, "bottom": 20, "left": 0}
        untouched = renderer.clamp_captions_into_safe_area(
            [self.image_caption(50.0)], safe, 1000, 1000
        )
        self.assertEqual(untouched[0]["style"]["y"], 50.0)

    def test_without_a_known_platform_nothing_is_moved(self) -> None:
        untouched = renderer.clamp_captions_into_safe_area(
            [self.image_caption(90.0)], None, 1000, 1000
        )
        self.assertEqual(untouched[0]["style"]["y"], 90.0)

    def test_a_non_caption_image_is_untouched(self) -> None:
        safe = {"top": 0, "right": 0, "bottom": 20, "left": 0}
        card = {
            "id": "visual-beat-1", "type": "image", "visible": True,
            "start": 0.0, "end": 1.0,
            "drawn": {"width": 500, "height": 500, "padding": 0},
            "style": {"width": 50.0, "x": 50, "y": 90.0},
        }
        untouched = renderer.clamp_captions_into_safe_area([card], safe, 1000, 1000)
        self.assertEqual(untouched[0]["style"]["y"], 90.0)

    def test_an_emphasis_block_reaching_past_the_margin_is_also_pulled_up(self) -> None:
        # Emphasis overlays are captioned by the same compositor and keep
        # their own "planned-emphasis-*"/"em-*" id through it — never the
        # "caption-instance-*" id bilingual translation assigns only to
        # type "caption". A prefix check on that one id shape left every
        # emphasis block free to sit under the platform's own controls.
        safe = {"top": 0, "right": 0, "bottom": 20, "left": 0}
        moved = renderer.clamp_captions_into_safe_area(
            [self.image_caption(
                90.0, caption_kind="emphasis", overlay_id="planned-emphasis-0004",
            )],
            safe, 1000, 1000,
        )
        self.assertAlmostEqual(moved[0]["style"]["y"], 55.0, places=6)

    def test_an_untranslated_caption_block_is_also_pulled_up(self) -> None:
        # Most captions are never translated and keep their plain
        # "caption-000N" id, which the same prefix check also missed.
        safe = {"top": 0, "right": 0, "bottom": 20, "left": 0}
        moved = renderer.clamp_captions_into_safe_area(
            [self.image_caption(
                90.0, caption_kind="caption", overlay_id="caption-0004",
            )],
            safe, 1000, 1000,
        )
        self.assertAlmostEqual(moved[0]["style"]["y"], 55.0, places=6)


class TranslationThatNeedsShorteningDoesNotPublishTests(unittest.TestCase):
    """Where SPEC Phase 3 v1 §3 runs out of steps, nothing is published.

    §3 step 1 passes a translation the 32px floor held above its 0.62 share
    as long as it still wraps inside two lines, so that on its own is
    reported, not refused — and refusing it would name a remedy that cannot
    work, since shortening the English does not change a ratio the spoken
    size set. A translation that needs a third line even at its floor is
    the case §3 cannot finish (its step 3, provider re-translation, is not
    built), and that one never reaches an output file.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-shortening-gate-")
        self.project = Path(self._tmp.name)
        (self.project / "source").mkdir()
        (self.project / "working").mkdir()
        (self.project / "source/source.mp4").write_bytes(b"source")
        self.addCleanup(self._tmp.cleanup)
        self.manifest = {
            "source": {
                "staged_path": "source/source.mp4",
                "duration_s": 2.0,
                "has_audio": True,
            }
        }

    def state(self, translation: str, max_width: float = 45) -> dict:
        return {
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "overlays": [{
                "id": "caption-0001", "type": "caption", "visible": True,
                "text": "我們今天要介紹全新規格與完整的操作流程說明",
                "translation": translation,
                "start": 0.0, "end": 2.0,
                # Narrow enough that the spoken line only fits two lines at
                # 44px, which pins the translation to its 32px floor.
                "style": {"font_size": 52, "max_width": max_width, "x": 50, "y": 50},
            }],
        }

    def build(self, quality: str, translation: str, max_width: float = 45) -> list[str]:
        return renderer.build_render_command(
            self.project,
            self.state(translation, max_width),
            self.manifest,
            self.project / "out.mp4",
            quality,
        )

    @unittest.skipUnless(
        compositor.compositor_available(), "needs macOS CoreText"
    )
    def test_a_final_cut_refuses_a_translation_that_cannot_be_laid_out(self) -> None:
        long_translation = (
            "today we are introducing the entirely new specification along "
            "with the complete operating procedure and its notes"
        )
        with self.assertRaisesRegex(ValueError, "shortened"):
            self.build("final", long_translation)
        self.assertFalse((self.project / "out.mp4").exists())

    @unittest.skipUnless(
        compositor.compositor_available(), "needs macOS CoreText"
    )
    def test_a_preview_refuses_it_too_rather_than_dropping_words(self) -> None:
        # There is no smaller size and no third line to put it on, so there
        # is nothing to preview either — the alternative is silently losing
        # the end of the sentence.
        long_translation = (
            "today we are introducing the entirely new specification along "
            "with the complete operating procedure and its notes"
        )
        with self.assertRaisesRegex(ValueError, "shortened"):
            self.build("preview", long_translation)

    @unittest.skipUnless(
        compositor.compositor_available(), "needs macOS CoreText"
    )
    def test_a_translation_held_up_by_its_floor_still_publishes(self) -> None:
        # 0.72 of the spoken size rather than 0.62, because the spoken line
        # shrank and the floor did not. It is reported, and it ships: no
        # amount of shortening would change that ratio.
        command = self.build("final", "a short translation line")
        self.assertEqual(command[-1], str(self.project / "out.mp4"))

    @unittest.skipUnless(
        compositor.compositor_available(), "needs macOS CoreText"
    )
    def test_the_floor_conflict_is_reported_rather_than_swallowed(self) -> None:
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.build("final", "a short translation line")
        reported = [
            json.loads(line)["caption_typography"]
            for line in stderr.getvalue().splitlines()
            if "caption_typography" in line
        ]
        self.assertEqual(
            reported[0]["floor_overrode_ratio"], ["caption-0001"], stderr.getvalue()
        )
        self.assertGreater(reported[0]["ratios"][0], compositor.TRANSLATION_SCALE)

    def test_the_cli_reports_a_refused_cut_as_exit_2(self) -> None:
        from unittest.mock import patch

        output = self.project / "gated.mp4"
        argv = [
            "render_editor_timeline.py", "--project-dir", str(self.project),
            "--output", str(output), "--quality", "final",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            renderer, "render_project",
            side_effect=ValueError("captions need shortening"),
        ):
            self.assertEqual(renderer.main(), 2)
        self.assertFalse(output.exists())


class ProviderFallbackTests(unittest.TestCase):
    """When the gateway's quota is spent, the subscription CLI answers."""

    def test_each_provider_gets_its_own_command_shape(self) -> None:
        # The claude CLI takes -p and no gateway flags; sending it
        # --session-id/--message fails at run time, on quota day.
        gateway, gateway_cwd = editorial_planner.provider_command(
            editorial_planner.DEFAULT_PROVIDER, "hi"
        )
        self.assertIn("--message", gateway)
        self.assertIsNone(gateway_cwd)
        claude, claude_cwd = editorial_planner.provider_command(
            editorial_planner.CLAUDE_PROVIDER, "hi"
        )
        self.assertEqual(claude[:3], ["claude", "-p", "hi"])
        self.assertIn("--setting-sources", claude)

    def test_claude_runs_from_an_empty_directory(self) -> None:
        # Started inside a repository it reads the repository as session
        # context, and a translation request comes back as a code review.
        _, cwd = editorial_planner.provider_command(
            editorial_planner.CLAUDE_PROVIDER, "hi"
        )
        self.assertTrue(cwd)
        self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_an_explicit_provider_is_used_alone(self) -> None:
        # A fallback behind someone's explicit choice is a different model
        # answering than the one they named.
        with self.assertRaises(editorial_planner.EditorialUnavailable) as caught:
            editorial_planner.call_provider(
                "hi", provider=("no-such-binary-xyz",), timeout_s=5
            )
        self.assertNotIn("claude", str(caught.exception))

    def test_the_default_chain_names_every_failure(self) -> None:
        real_which = editorial_planner.shutil.which
        editorial_planner.shutil.which = lambda name: None
        try:
            with self.assertRaises(editorial_planner.EditorialUnavailable) as caught:
                editorial_planner.call_provider("hi", timeout_s=5)
        finally:
            editorial_planner.shutil.which = real_which
        message = str(caught.exception)
        self.assertIn("openclaw", message)
        self.assertIn("claude", message)


if __name__ == "__main__":
    unittest.main()

"""A second caption line, from request to pixels.

Three separate faults each made `--translate` look like it worked while
delivering nothing: the translator's failure was swallowed, the cache served
single-line rasters because the key ignored translations, and when the line
did render the taller block climbed up over the picture.
"""
from __future__ import annotations

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

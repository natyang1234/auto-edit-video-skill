"""The real content animations, and the fallback that keeps deliveries safe.

Four pack presets animate a card's contents; a finished PNG cannot, so they
shipped as entrance approximations flagged unfaithful. The animator rebuilds
the card as a GSAP composition and renders a transparent clip. When it cannot
— no CLI, a browser failure, a preset with no animation — the static card
ships with its approximation, exactly as before.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import card_animator  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402
import structured_card_compositor as compositor  # noqa: E402


class WhatAnimatesTests(unittest.TestCase):
    def test_the_content_presets_are_the_unfaithful_ones(self) -> None:
        # These are exactly the presets the entrance table flags as not
        # what was asked for — minus flip, which needs a second page these
        # payloads do not carry.
        unfaithful = {
            name for name, (_mapped, faithful) in renderer.MOTION_PRESETS.items()
            if not faithful
        }
        self.assertEqual(card_animator.CONTENT_PRESETS | {"flip"}, unfaithful)

    def test_cjk_arrives_by_character_and_latin_by_word(self) -> None:
        self.assertEqual(
            card_animator._chunks("賺更多 money"), ["賺", "更", "多", "money"]
        )

    def test_every_content_preset_builds_a_page(self) -> None:
        layers = {
            "word-cascade": {"type": "title", "payload": {"title": "三個生日願望"}},
            "staggered-reveal": {"type": "dynamic_list",
                                 "payload": {"items": [{"text": "第一項"}]}},
            "count-up": {"type": "stat",
                         "payload": {"value": "87%", "label": "留存"}},
            "fill": {"type": "stat",
                     "payload": {"value": "60%", "ratio": 0.6, "label": "進度"}},
        }
        for preset, layer in layers.items():
            with self.subTest(preset):
                page = card_animator.build_card_html(
                    layer, {}, preset, 800, 200, 5.5, 30
                )
                self.assertIn('data-composition-id="card-animation"', page)
                self.assertIn("window.__timelines", page)

    def test_a_preset_with_no_animation_is_refused_not_guessed(self) -> None:
        with self.assertRaises(ValueError):
            card_animator.build_card_html(
                {"type": "title", "payload": {}}, {}, "flip", 800, 200, 5.5, 30
            )

    def test_the_page_holds_the_static_cards_geometry(self) -> None:
        # Same width and height as the measured static card, so placement,
        # collision checks and safe-area maths do not know the difference.
        page = card_animator.build_card_html(
            {"type": "title", "payload": {"title": "T"}}, {},
            "word-cascade", 907, 200, 5.5, 30,
        )
        self.assertIn('data-width="907"', page)
        self.assertIn('data-height="200"', page)

    def test_disabled_by_environment_means_unavailable(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"AUTO_EDIT_DISABLE_CARD_ANIMATION": "1"}):
            self.assertFalse(card_animator.available())


class FallbackTests(unittest.TestCase):
    def test_an_unknown_layer_falls_back_to_the_static_card(self) -> None:
        self.assertIsNone(
            renderer.animated_card_overlay_source(
                Path("/tmp"), {}, {"items": []}, "structured-layer-none",
                {"width": 800, "height": 200}, 5.5, 30,
            )
        )

    def test_an_entrance_preset_never_reaches_the_animator(self) -> None:
        # prompt_card asks for slide-in, which the overlay pipeline already
        # does faithfully; rebuilding it in a browser would buy nothing.
        pack = {"components": [
            {"id": "prompt-card", "kind": "prompt_card", "layout": "left-panel",
             "motion": {"preset": "slide-in"}},
        ]}
        layers = {"items": [{"id": "structured-layer-aaaa1111", "type": "title",
                             "payload": {"title": "T"}}]}
        self.assertIsNone(
            renderer.animated_card_overlay_source(
                Path("/tmp"), pack, layers, "structured-layer-aaaa1111",
                {"width": 800, "height": 200}, 5.5, 30,
            )
        )


class CardCacheTests(unittest.TestCase):
    def test_the_drawing_code_is_part_of_the_cache_check(self) -> None:
        # The type-size bump shipped while every existing card sailed on
        # through the artifact cache: layer, pack and mode were checked, the
        # code that draws them was not. Same family as the caption
        # translation and burned-in caches.
        import inspect

        source = inspect.getsource(compositor.build_structured_artifacts)
        self.assertIn('cached.get("compiler_version") == COMPILER_VERSION', source)


if __name__ == "__main__":
    unittest.main()

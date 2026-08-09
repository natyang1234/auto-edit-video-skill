from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import asset_registry  # noqa: E402
from render_editor_timeline import font_path, project_font_binding  # noqa: E402


class ProjectFontResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="renderer-font-")
        self.project = Path(self.temp.name)
        (self.project / "assets/fonts").mkdir(parents=True)
        for suffix, payload in (("a", b"font-a"), ("b", b"font-b")):
            (self.project / f"assets/fonts/{suffix}.ttf").write_bytes(payload)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_asset_id_selects_exact_same_family_file(self) -> None:
        ids = {
            "font-google-fonts-0123456789abcdef-0123456789abcdef": "a",
            "font-google-fonts-fedcba9876543210-fedcba9876543210": "b",
        }

        def resolve(_project: Path, asset_id: str, required_text: str = "") -> dict:
            suffix = ids[asset_id]
            payload = (self.project / f"assets/fonts/{suffix}.ttf").read_bytes()
            return {
                "asset_id": asset_id,
                "path": f"assets/fonts/{suffix}.ttf",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "family": "Same Family",
            }

        with patch.object(asset_registry, "resolve_project_font", side_effect=resolve):
            first = project_font_binding(self.project, font_asset_id=next(iter(ids)), required_text="甲")
            second = project_font_binding(self.project, font_asset_id=list(ids)[1], required_text="乙")
        self.assertEqual(first["path"].name, "a.ttf")
        self.assertEqual(second["path"].name, "b.ttf")
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_selected_missing_font_never_uses_legacy_fallback(self) -> None:
        asset_id = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        with patch.object(
            asset_registry,
            "resolve_project_font",
            side_effect=asset_registry.AssetRegistryError("receipt missing"),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                font_path(self.project, {"caption_defaults": {"font_asset_id": asset_id}})


class CardMotionTests(unittest.TestCase):
    """Cards arrive the way their component says they should."""

    def setUp(self) -> None:
        import structured_card_compositor

        self.pack = structured_card_compositor.load_default_pack()
        self.layers = {
            "items": [
                {"id": "L-progress", "type": "stat", "component_id": "progress"},
                {"id": "L-carousel", "type": "dynamic_list", "component_id": "carousel-grid"},
                {"id": "L-lockup", "type": "title", "component_id": "title-lockup"},
            ]
        }

    def test_each_component_gets_the_motion_its_pack_declares(self) -> None:
        from render_editor_timeline import motion_for_layer

        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-progress"), "slide-in")
        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-carousel"), "pan")
        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-lockup"), "slide-up")

    def test_an_unknown_layer_still_renders(self) -> None:
        from render_editor_timeline import motion_for_layer

        self.assertEqual(motion_for_layer(self.pack, self.layers, "nope"), "fade")

    def test_content_animations_are_approximated_and_marked_as_such(self) -> None:
        # Digits counting up or a page turning cannot be done by moving a
        # finished image; those take the nearest entrance and are not claimed
        # to be faithful.
        from render_editor_timeline import resolve_motion

        for preset in ("count-up", "word-cascade", "staggered-reveal", "flip", "fill"):
            with self.subTest(preset):
                animation, faithful = resolve_motion(preset)
                self.assertIn(animation, {"fade", "pop", "slide-in", "slide-up", "pan"})
                self.assertFalse(faithful)
        for preset in ("slide-in", "slide-up", "pan", "check-pop"):
            with self.subTest(preset):
                self.assertTrue(resolve_motion(preset)[1])

    def test_horizontal_motion_reaches_the_filter(self) -> None:
        from render_editor_timeline import image_filter

        overlay = {
            "id": "o1",
            "type": "image",
            "source": "card.png",
            "start": 1.0,
            "end": 4.0,
            "style": {"width": 84.0, "x": 50, "y": 46, "animation": "slide-in"},
        }
        built = image_filter("in", "out", "asset", overlay, 1080, 1920)
        self.assertIn("overlay=x=", built)
        self.assertIn("if(lt(t,", built.split("overlay=x=")[1][:80])

    def test_real_content_animation_is_recorded_as_faithful(self) -> None:
        import structured_card_compositor
        from render_editor_timeline import card_visual_evidence

        pack = structured_card_compositor.load_style_pack("kinetic-social")
        layers = {
            "items": [{
                "id": "L-list",
                "type": "dynamic_list",
                "component_id": "dynamic-list",
            }]
        }
        evidence = card_visual_evidence(
            pack,
            layers,
            "L-list",
            1.0,
            "working/structured_cards/anim-L-list.mov",
        )
        self.assertEqual(evidence["minimum_primary_font_px"], 36.0)
        self.assertEqual(evidence["motion"]["requested"], "staggered-reveal")
        self.assertEqual(evidence["motion"]["delivered"], "staggered-reveal")
        self.assertTrue(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "rendered")

    def test_static_content_fallback_is_never_claimed_faithful(self) -> None:
        import structured_card_compositor
        from render_editor_timeline import card_visual_evidence

        pack = structured_card_compositor.load_style_pack("kinetic-social")
        layers = {
            "items": [{
                "id": "L-list",
                "type": "dynamic_list",
                "component_id": "dynamic-list",
            }]
        }
        evidence = card_visual_evidence(pack, layers, "L-list", 1.0, None)
        self.assertEqual(evidence["motion"]["delivered"], "fade")
        self.assertFalse(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "fallback")

    def test_native_entrance_is_faithful_without_browser_animation(self) -> None:
        from render_editor_timeline import card_visual_evidence

        evidence = card_visual_evidence(
            self.pack, self.layers, "L-carousel", 1.0, None
        )
        self.assertEqual(evidence["motion"]["requested"], "pan")
        self.assertEqual(evidence["motion"]["delivered"], "pan")
        self.assertTrue(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "native")

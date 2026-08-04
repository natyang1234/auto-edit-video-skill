"""Phase 1b N1: CoreText caption compositor."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as cc  # noqa: E402
import contract_registry  # noqa: E402


def make_state(text: str = "看到 It 想到👍to V", span_color: str = "#FF5533") -> dict:
    return {
        "canvas": {"width": 1080, "height": 1920},
        "overlays": [
            {
                "id": "caption-1",
                "type": "caption",
                "text": text,
                "start": 0.0,
                "end": 2.0,
                "visible": True,
                "style": {"font_size": 52, "color": "#F7F2E8", "stroke_width": 3, "max_width": 84},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
                "effect_spans": [
                    {
                        "id": "fx1",
                        "text": "It",
                        "start_char": 3,
                        "end_char": 5,
                        "style": {"effect": "pop", "color": span_color, "font_scale": 1.3},
                    }
                ],
            }
        ],
    }


@unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
class CompositorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="caption-compositor-tests-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_plan_is_contract_valid_deterministic_and_cached(self) -> None:
        state = make_state()
        plan = cc.build_render_plan(self.project, state)
        self.assertEqual(
            contract_registry.validate_artifact("caption_render_plan", plan), []
        )
        png = self.project / plan["items"][0]["artifact"]["rgba_path"]
        self.assertTrue(png.is_file())
        self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        first_mtime = png.stat().st_mtime_ns

        again = cc.build_render_plan(self.project, state)
        self.assertEqual(again["revision"], plan["revision"])
        self.assertEqual(png.stat().st_mtime_ns, first_mtime, "cache hit must not re-render")

    def test_emoji_runs_use_sanctioned_fallback(self) -> None:
        plan = cc.build_render_plan(self.project, make_state())
        fonts = {run["font_asset_id"] for run in plan["items"][0]["glyph_runs"]}
        self.assertIn(cc.PROJECT_FONT_ASSET_ID, fonts)
        self.assertIn(cc.EMOJI_FONT_ASSET_ID, fonts)
        self.assertEqual(plan["receipt"]["disallowed_fallbacks"], [])
        self.assertTrue(plan["receipt"]["fonts"][cc.PROJECT_FONT_ASSET_ID]["sha256"])

    def test_span_style_change_changes_artifact(self) -> None:
        baseline = cc.build_render_plan(self.project, make_state(span_color="#FF5533"))
        changed = cc.build_render_plan(self.project, make_state(span_color="#00CC88"))
        self.assertNotEqual(baseline["caption_revision"], changed["caption_revision"])
        self.assertNotEqual(
            baseline["items"][0]["artifact"]["artifact_hash"],
            changed["items"][0]["artifact"]["artifact_hash"],
            "span colour must change rendered pixels",
        )

    def test_clusters_match_boundary_authority(self) -> None:
        plan = cc.build_render_plan(self.project, make_state())
        import caption_engine

        self.assertEqual(
            plan["items"][0]["clusters"],
            caption_engine.boundary_map(make_state()["overlays"][0]["text"]),
        )

    def test_disabled_env_reports_not_configured(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"AUTO_EDIT_DISABLE_CORETEXT": "1"}):
            cc._CORETEXT = None
            try:
                self.assertFalse(cc.compositor_available())
                self.assertEqual(cc.engine_descriptor()["status"], "not_configured")
                with self.assertRaises(RuntimeError):
                    cc.build_render_plan(self.project, make_state())
            finally:
                cc._CORETEXT = None


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
class CompositorRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="caption-compositor-reg-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def render_ppm(self, plan: dict) -> tuple[int, int, bytes]:
        import subprocess

        artifact = plan["items"][0]["artifact"]
        png = self.project / artifact["rgba_path"]
        ppm = self.project / "check.ppm"
        subprocess.run(
            [
                "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-y",
                "-f", "lavfi", "-i",
                f"color=c=0x202020:s={artifact['width']}x{artifact['height']}",
                "-i", str(png),
                "-filter_complex", "overlay=0:0",
                "-frames:v", "1", "-f", "image2", "-c:v", "ppm", str(ppm),
            ],
            check=True, capture_output=True,
        )
        data = ppm.read_bytes()
        parts = data.split(b"\n", 3)
        width, height = map(int, parts[1].split())
        return width, height, parts[3]

    def test_vertical_orientation_is_not_flipped(self) -> None:
        state = {
            "canvas": {"width": 1080, "height": 1920},
            "overlays": [
                {
                    "id": "cap-flip",
                    "type": "caption",
                    "text": "上上上上\n下下下下",
                    "start": 0, "end": 1, "visible": True,
                    "style": {"font_size": 60, "color": "#FFFFFF", "stroke_width": 0},
                    "effect_spans": [
                        {
                            "id": "fx-top", "text": "上上上上",
                            "start_char": 0, "end_char": 4,
                            "style": {"effect": "pop", "color": "#FF0000", "font_scale": 1.0},
                        }
                    ],
                }
            ],
        }
        plan = cc.build_render_plan(self.project, state)
        width, height, pixels = self.render_ppm(plan)
        red_rows = [
            y for y in range(height)
            if any(
                pixels[(y * width + x) * 3] > 150
                and pixels[(y * width + x) * 3 + 1] < 90
                and pixels[(y * width + x) * 3 + 2] < 90
                for x in range(width)
            )
        ]
        self.assertTrue(red_rows, "red top line must be visible")
        self.assertLess(
            max(red_rows), height / 2,
            "the FIRST text line must appear in the TOP half — orientation flipped?",
        )

    def test_highlight_effect_paints_a_backdrop(self) -> None:
        state = {
            "canvas": {"width": 1080, "height": 1920},
            "overlays": [
                {
                    "id": "cap-hl",
                    "type": "caption",
                    "text": "重點標記測試",
                    "start": 0, "end": 1, "visible": True,
                    "style": {"font_size": 60, "color": "#FFFFFF", "stroke_width": 0},
                    "effect_spans": [
                        {
                            "id": "fx-hl", "text": "標記",
                            "start_char": 2, "end_char": 4,
                            "style": {"effect": "highlight", "color": "#F5A623", "font_scale": 1.0},
                        }
                    ],
                }
            ],
        }
        plan = cc.build_render_plan(self.project, state)
        width, height, pixels = self.render_ppm(plan)
        amber = sum(
            1
            for i in range(0, len(pixels), 3)
            if pixels[i] > pixels[i + 1] + 25 and pixels[i + 1] > pixels[i + 2] + 15
        )
        self.assertGreater(amber, 100, "highlight span must paint a visible backdrop")

    def test_tampered_artifact_is_rerendered_not_reused(self) -> None:
        state = make_state()
        plan = cc.build_render_plan(self.project, state)
        png = self.project / plan["items"][0]["artifact"]["rgba_path"]
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"tampered")
        again = cc.build_render_plan(self.project, state)
        fresh = self.project / again["items"][0]["artifact"]["rgba_path"]
        self.assertEqual(
            again["items"][0]["artifact"]["artifact_hash"],
            plan["items"][0]["artifact"]["artifact_hash"],
            "deterministic re-render must restore the same artifact",
        )
        import hashlib

        self.assertEqual(
            hashlib.sha256(fresh.read_bytes()).hexdigest(),
            again["items"][0]["artifact"]["artifact_hash"],
            "tampered bytes must be replaced by an honest re-render",
        )


@unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
class PackPrecedenceTests(unittest.TestCase):
    """Plan v2 P2: manual > pack default > system, with receipt sources."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-precedence-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def state_with(self, manual_color: str | None, pack: bool) -> dict:
        style = {"font_size": 40, "stroke_width": 0}
        if manual_color:
            style["color"] = manual_color
        state = {
            "canvas": {"width": 1080, "height": 1920},
            "overlays": [
                {
                    "id": "caption-p2", "type": "caption", "text": "風格包測試",
                    "start": 0, "end": 1, "visible": True,
                    "style": style, "effect_spans": [],
                }
            ],
        }
        if pack:
            state["style_pack"] = {"project_default": "dark-data-presenter"}
        return state

    def test_manual_key_survives_pack_switch(self) -> None:
        manual = cc.build_render_plan(self.project, self.state_with("#123456", pack=False))
        with_pack = cc.build_render_plan(self.project, self.state_with("#123456", pack=True))
        self.assertEqual(
            manual["items"][0]["style_sources"]["color"], "manual"
        )
        self.assertEqual(
            with_pack["items"][0]["style_sources"]["color"], "manual",
            "a manually set key must never be overridden by the pack",
        )
        self.assertEqual(
            manual["items"][0]["artifact"]["artifact_hash"],
            with_pack["items"][0]["artifact"]["artifact_hash"],
            "manual colour pixels must be byte-stable across pack switch",
        )

    def test_unset_key_takes_pack_default_and_goes_stale(self) -> None:
        without = cc.build_render_plan(self.project, self.state_with(None, pack=False))
        self.assertEqual(without["items"][0]["style_sources"]["color"], "system")
        with_pack = cc.build_render_plan(self.project, self.state_with(None, pack=True))
        self.assertEqual(
            with_pack["items"][0]["style_sources"]["color"],
            "pack-project:dark-data-presenter",
        )
        self.assertNotEqual(
            without["caption_revision"], with_pack["caption_revision"],
            "pack switch must invalidate the caption plan",
        )
        self.assertNotEqual(
            without["items"][0]["artifact"]["artifact_hash"],
            with_pack["items"][0]["artifact"]["artifact_hash"],
            "unset colour must actually change pixels",
        )

    def test_unknown_pack_fails_closed(self) -> None:
        state = self.state_with(None, pack=True)
        state["style_pack"]["project_default"] = "vaporwave-9000"
        with self.assertRaises(ValueError):
            cc.build_render_plan(self.project, state)

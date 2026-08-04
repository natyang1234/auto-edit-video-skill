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

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fx_overlay  # noqa: E402
import fx_preview_contract  # noqa: E402
import fx_preview_render  # noqa: E402
import render_editor_timeline  # noqa: E402
import editor_server  # noqa: E402


class FxOpacityContractTests(unittest.TestCase):
    def test_boundaries_and_invalid_values(self):
        for raw, expected in ((0, 0.0), (-0.0, 0.0), (0.5, 0.5), (1, 1.0)):
            normalized = fx_preview_contract.normalize_fx_opacity(raw)
            self.assertEqual(normalized, expected)
            if raw == 0:
                self.assertEqual(math.copysign(1, normalized), 1)
        for raw in (None, True, False, "0.5", -0.001, 1.001, math.nan,
                    math.inf, -math.inf, 10**400):
            with self.subTest(raw=raw):
                with self.assertRaises(fx_preview_contract.FxPreviewContractError) as got:
                    fx_preview_contract.normalize_fx_opacity(raw)
                self.assertEqual(got.exception.code, "opacity_invalid")

    def test_zero_survives_fx_to_video_conversion(self):
        video = fx_overlay.as_video_overlay(
            {"id": "fx-1", "type": "fx", "style": {"opacity": 0}},
            {"source": "working/fx/T3.mov", "code": "T3", "block": "typography",
             "theme": "kwons", "main": "paper", "scheme": "paper", "person": None},
        )
        self.assertEqual(video["style"]["opacity"], 0.0)

    def test_one_preserves_old_graph_and_only_fx_gets_alpha(self):
        self.assertEqual(
            fx_preview_render.single_preview_composite_filter(1),
            "[base][1:v]overlay=0:0:shortest=1[v]",
        )
        common = {"type": "video", "start": 0, "end": 1,
                  "style": {"width": 100, "x": 50, "y": 50,
                            "animation": "none", "opacity": 0.5}}
        ordinary = render_editor_timeline.image_filter("base", "out", "asset", common, 64, 64)
        fx = render_editor_timeline.image_filter(
            "base", "out", "asset", {**common, "fx": {"code": "T3"}}, 64, 64)
        self.assertNotIn("colorchannelmixer", ordinary)
        self.assertIn("colorchannelmixer=aa=0.500000", fx)
        full = render_editor_timeline.image_filter(
            "base", "out", "asset",
            {**common, "fx": {"code": "T3"}, "style": {**common["style"], "opacity": 1}},
            64, 64)
        self.assertNotIn("colorchannelmixer", full)

    def test_direct_editor_state_huge_fx_opacity_is_a_validation_error(self):
        state = {
            "schema_version": 2,
            "project_id": "opacity-validation",
            "overlays": [{
                "id": "fx-huge",
                "type": "fx",
                "code": "T3",
                "start": 0,
                "end": 1,
                "values": {},
                "placement": "",
                "scheme": "",
                "font_ids": {},
                "style": {"opacity": 10**400},
            }],
        }
        errors = editor_server.validate_editor_state(state, 2.0)
        self.assertIn("overlay fx-huge style opacity is invalid", errors)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class FxOpacityCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fx-opacity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe = shutil.which("ffprobe") or "ffprobe"
        self.base, self.fx = self.root / "base.mp4", self.root / "fx.mov"
        self.fx_half = self.root / "fx-half.mov"
        self.run_cmd([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                      "color=c=red:s=64x64:r=30:d=1", "-f", "lavfi", "-i",
                      "sine=frequency=1000:sample_rate=48000:duration=1",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-shortest", str(self.base)])
        self.run_cmd([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                      "color=c=blue@1.0:s=64x64:r=30:d=1,format=rgba",
                      "-c:v", "qtrle", str(self.fx)])
        self.run_cmd([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                      "color=c=blue@0.5:s=64x64:r=30:d=1,format=rgba",
                      "-c:v", "qtrle", str(self.fx_half)])

    def run_cmd(self, command):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def render(self, route, opacity, fx_path=None, suffix=""):
        out = self.root / f"{route}-{opacity}{suffix}.mp4"
        if route == "single":
            graph = "[0:v]format=yuv420p[base];" + fx_preview_render.single_preview_composite_filter(opacity)
            label = "[v]"
        else:
            overlay = {"type": "video", "fx": {"code": "T3"}, "start": 0, "end": 1,
                       "style": {"width": 100, "x": 50, "y": 50,
                                 "animation": "none", "opacity": opacity}}
            graph = render_editor_timeline.image_filter("0:v", "out", "1:v", overlay, 64, 64)
            label = "[out]"
        self.run_cmd([self.ffmpeg, "-v", "error", "-y", "-i", str(self.base),
                      "-i", str(fx_path or self.fx), "-filter_complex", graph, "-map", label,
                      "-map", "0:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-t", "1", "-shortest", str(out)])
        if os.environ.get("P2_OPACITY_ARTIFACT_DIR"):
            target = Path(os.environ["P2_OPACITY_ARTIFACT_DIR"]); target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, target / out.name)
        return out

    def mean_rgb(self, path):
        result = subprocess.run(
            [self.ffmpeg, "-v", "error", "-i", str(path), "-vf",
             "select=eq(n\\,0),format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"],
            capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(len(result.stdout), 64 * 64 * 3)
        return tuple(sum(result.stdout[c::3]) / (64 * 64) for c in range(3))

    def probe(self, path):
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,start_time,duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_single_and_timeline_final_routes_apply_true_alpha(self):
        for route in ("single", "timeline-final"):
            outputs = {value: self.render(route, value) for value in (0.0, 0.5, 1.0)}
            colors = {value: self.mean_rgb(path) for value, path in outputs.items()}
            self.assertGreater(colors[0.0][0], 220); self.assertLess(colors[0.0][2], 35)
            self.assertLess(colors[1.0][0], 35); self.assertGreater(colors[1.0][2], 220)
            self.assertTrue(90 < colors[0.5][0] < colors[0.0][0])
            self.assertTrue(90 < colors[0.5][2] < colors[1.0][2])
            for output in outputs.values():
                probe = self.probe(output)
                self.assertEqual({s["codec_type"] for s in probe["streams"]}, {"video", "audio"})
                self.assertAlmostEqual(float(probe["format"]["duration"]), 1.0, delta=0.08)
                for stream in probe["streams"]:
                    self.assertAlmostEqual(float(stream.get("start_time", 0)), 0.0, delta=0.05)

    def test_existing_half_alpha_is_multiplied_not_replaced(self):
        output = self.render("single", 0.5, self.fx_half, "-existing-half-alpha")
        red, green, blue = self.mean_rgb(output)
        self.assertTrue(165 < red < 220, (red, green, blue))
        self.assertLess(green, 35)
        self.assertTrue(40 < blue < 95, (red, green, blue))


if __name__ == "__main__":
    unittest.main()

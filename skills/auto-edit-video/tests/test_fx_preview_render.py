#!/usr/bin/env python3
"""Unit contracts for the isolated Studio FX preview worker."""
import json
import shutil
import sys
import tempfile
import unittest
import re
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fx_preview_render as worker  # noqa: E402


class FxPreviewRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / "project"; self.project.mkdir()
        source_dir = self.project / "source"; source_dir.mkdir()
        self.source = source_dir / "source.mp4"; self.source.write_bytes(b"fixture")
        self.base = {
            "project_dir": str(self.project), "source": str(self.source),
            "cache_dir": str(self.project / "working" / "fx-preview" / "renderer-cache"),
            "output": str(self.project / "working" / "fx-preview" / "runtime" / "out.mp4"),
            "overlay": {"id": "fx-preview", "type": "fx", "code": "T3", "start": 1, "end": 3,
                        "values": {}, "placement": "", "scheme": "", "font_ids": {}, "style": {}},
            "pack_id": "", "canvas": {"width": 1080, "height": 1920},
        }

    def test_broll_is_rejected_before_source_probe(self):
        request = json.loads(json.dumps(self.base)); request["overlay"]["code"] = "B1"
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "broll_requires_asset")

    def test_non_portrait_canvas_is_rejected(self):
        request = json.loads(json.dumps(self.base)); request["canvas"] = {"width": 1920, "height": 1080}
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "invalid_canvas")

    def test_unknown_canvas_fit_is_rejected(self):
        request = json.loads(json.dumps(self.base)); request["canvas"]["fit"] = "stretch"
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "invalid_canvas")

    def test_more_than_four_seconds_is_rejected(self):
        request = json.loads(json.dumps(self.base)); request["overlay"]["end"] = 5.01
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "invalid_window")

    def test_person_render_failure_is_fail_closed(self):
        request = json.loads(json.dumps(self.base)); request["overlay"]["code"] = "P5"
        fake_compose = mock.Mock(); fake_compose.probe.return_value = {"duration": 10}
        catalog = {"effects": [{"code": "P5", "needs_person": False}]}
        with mock.patch.object(worker.fx_overlay, "_compose", return_value=fake_compose), \
             mock.patch.object(worker.fx_overlay, "catalog", return_value=catalog), \
             mock.patch.object(worker.fx_overlay, "codes", return_value={"P5"}), \
             mock.patch.object(worker.fx_overlay, "render_for_timeline", return_value=None):
            with self.assertRaises(worker.PreviewError) as got:
                worker.render(request)
        self.assertEqual(got.exception.code, "person_unavailable")

    def test_result_json_is_atomic_shape_on_safe_error(self):
        request = self.tmp / "request.json"; result = self.tmp / "result.json"
        request.write_text(json.dumps({**self.base, "overlay": {**self.base["overlay"], "code": "B1"}}), encoding="utf-8")
        with mock.patch.object(sys, "argv", ["fx_preview_render.py", "--request", str(request), "--result", str(result)]):
            self.assertEqual(worker.main(), 1)
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"ok": False, "error": "B1 需要指定補畫面素材，不能以原片假裝補畫面", "code": "broll_requires_asset"})


class StrictSourceCatalogTests(unittest.TestCase):
    def test_all_167_non_broll_blocks_have_only_seeded_literal_media(self):
        """A new literal demo filename cannot silently leak into strict preview."""
        compose = worker.fx_overlay._compose()
        effects, _ = compose.load_effects()
        self.assertEqual(len(effects), 168)
        seeded = {"person.webm", "screen.jpg", "shot1.jpg", "shot2.jpg", "shot3.jpg", "shot4.jpg",
                  "shot5.jpg", "shot6.jpg", "talk.mp4", "talk2.mp4"}
        covered = []
        for code, effect in effects.items():
            if code == "B1":
                continue
            schema = compose.block_schema(effect["block"])
            self.assertIsNotNone(schema, code)
            html = Path(compose.BLOCKS, effect["block"] + ".html").read_text(encoding="utf-8")
            names = set(re.findall(r"assets/([A-Za-z0-9_.-]+)", html))
            self.assertTrue(names <= seeded, f"{code}: {names - seeded}")
            # All declared dynamic slots are filled from source by strict_source.
            self.assertIsInstance(schema["images"], list, code)
            self.assertIsInstance(schema["clips"], list, code)
            covered.append(code)
        self.assertEqual(len(covered), 167)


if __name__ == "__main__":
    unittest.main()

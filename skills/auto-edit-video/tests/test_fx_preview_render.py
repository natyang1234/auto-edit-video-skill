#!/usr/bin/env python3
"""Unit contracts for the isolated Studio FX preview worker."""
import json
import hashlib
import os
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
import render_editor_timeline as timeline  # noqa: E402


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
            "sample_window": {"start_s": 1, "duration_s": 2},
            "state_snapshot": {}, "manifest_snapshot": {"source": {"duration_s": 10}},
            "dependency_snapshot": {"source": {"relative": "source/source.mp4", "sha256": hashlib.sha256(b"fixture").hexdigest()}},
        }

    def test_broll_is_rejected_before_source_probe(self):
        request = json.loads(json.dumps(self.base)); request["overlay"]["code"] = "B1"
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "material_invalid")

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

    def test_sample_more_than_four_seconds_is_rejected_while_apply_can_be_long(self):
        request = json.loads(json.dumps(self.base)); request["overlay"]["end"] = 8
        request["sample_window"] = {"start_s": 1, "duration_s": 4.01}
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(request)
        self.assertEqual(got.exception.code, "window_out_of_range")

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
        self.assertEqual(payload, {"ok": False, "error": "可信素材綁定格式無效", "code": "material_invalid"})

    def test_cli_inherited_fds_pin_request_and_result_directory(self):
        request_dir = self.tmp / "request-dir"
        result_dir = self.tmp / "result-dir"
        external = self.tmp / "external"
        request_dir.mkdir(); result_dir.mkdir(); external.mkdir()
        request = request_dir / "request.json"
        result = result_dir / "result.json"
        original = json.loads(json.dumps(self.base))
        original["canvas"] = {"width": 1920, "height": 1080}
        request.write_text(json.dumps(original), encoding="utf-8")
        request_fd = os.open(request, os.O_RDONLY)
        result_dir_fd = os.open(result_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        pinned_result_dir = self.tmp / "result-dir-pinned"
        try:
            request.rename(request_dir / "request-pinned.json")
            malicious = {**self.base, "overlay": {**self.base["overlay"], "code": "B1"}}
            request.write_text(json.dumps(malicious), encoding="utf-8")
            result_dir.rename(pinned_result_dir)
            result_dir.symlink_to(external, target_is_directory=True)
            with mock.patch.dict(
                os.environ,
                {
                    "FX_PREVIEW_REQUEST_FD": str(request_fd),
                    "FX_PREVIEW_RESULT_DIR_FD": str(result_dir_fd),
                },
            ), mock.patch.object(
                sys,
                "argv",
                ["fx_preview_render.py", "--request", str(request), "--result", str(result)],
            ):
                self.assertEqual(worker.main(), 1)
            payload = json.loads((pinned_result_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["code"], "invalid_canvas")
            self.assertFalse((external / "result.json").exists())
        finally:
            os.close(request_fd)
            os.close(result_dir_fd)

    def test_source_digest_change_is_rejected_before_probe(self):
        self.source.write_bytes(b"changed")
        with self.assertRaises(worker.PreviewError) as got:
            worker.render(self.base)
        self.assertEqual(got.exception.code, "dependency_changed")

    def test_verified_copy_is_independent_and_detects_copy_race(self):
        dst = self.project / "copy.mp4"
        worker.verified_copy(self.source, dst)
        self.assertNotEqual(self.source.stat().st_ino, dst.stat().st_ino)
        original = worker.shutil.copy2
        def racing(src, target):
            original(src, target)
            src.write_bytes(b"race")
        with mock.patch.object(worker.shutil, "copy2", side_effect=racing):
            with self.assertRaises(worker.PreviewError) as got:
                worker.verified_copy(self.source, dst)
        self.assertEqual(got.exception.code, "dependency_changed")

    def test_timeline_shadow_copies_only_frozen_dependencies(self):
        cache = self.project / "working/fx-preview/renderer-cache"
        output = self.project / "working/fx-preview/runtime/out.mp4"
        cache.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        referenced = self.project / "assets/referenced.png"
        referenced.parent.mkdir()
        referenced.write_bytes(b"\x89PNG\r\n\x1a\nreferenced")
        referenced_digest = hashlib.sha256(referenced.read_bytes()).hexdigest()
        registry = self.project / "assets/provenance.json"
        registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "asset-upload-timeline",
                            "path": "assets/referenced.png",
                            "sha256": referenced_digest,
                            "origin": "user-upload",
                            "provider_id": None,
                            "source_url": None,
                            "license": {
                                "spdx": "UNKNOWN",
                                "attribution_required": False,
                                "attribution_text": "",
                                "verified_at": "2026-11-07T00:00:00Z",
                            },
                            "review_status": "pending",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        structured = self.project / "working/structured_layers.json"
        structured.parent.mkdir(exist_ok=True)
        structured.write_text("{}", encoding="utf-8")
        unrelated = self.project / "assets/unreferenced-large.bin"
        unrelated.write_bytes(b"x" * (2 * 1024 * 1024))
        outside = self.tmp / "outside"
        outside.write_text("sentinel", encoding="utf-8")
        (self.project / "renders").mkdir()
        (self.project / "renders/unrelated-link").symlink_to(outside)
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.source, referenced, structured, unrelated, outside)
        }
        dependencies = {
            "source": {
                "relative": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "asset_digests": {"assets/referenced.png": referenced_digest},
            "provenance_digests": {
                "assets/provenance.json": hashlib.sha256(registry.read_bytes()).hexdigest(),
            },
            "timeline_project_inputs": {
                "working/structured_layers.json": hashlib.sha256(structured.read_bytes()).hexdigest(),
            },
            "material": {
                "asset_id": "asset-upload-timeline",
                "path": "assets/referenced.png",
                "sha256": referenced_digest,
                "kind": "image",
            },
        }
        snapshot = {
            "manifest": {"source": {"duration_s": 10, "path": "source/source.mp4"}},
            "state": {
                "segments": [{"source_start": 0, "source_end": 10}],
                "overlays": [
                    {
                        "id": "fx-preview-candidate",
                        "type": "fx",
                        "code": "B1",
                        "start": 1.0,
                        "end": 3.0,
                        "material": {
                            "asset_id": "asset-upload-timeline",
                            "path": "assets/referenced.png",
                            "sha256": referenced_digest,
                            "kind": "image",
                        },
                    }
                ],
            },
        }
        observed = {}

        def render_shadow(shadow, full, *_args, **_kwargs):
            shadow = Path(shadow)
            observed["files"] = {
                path.relative_to(shadow).as_posix()
                for path in shadow.rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                json.loads((shadow / "project.json").read_text(encoding="utf-8")),
                snapshot["manifest"],
            )
            binding = snapshot["state"]["overlays"][0]["material"]
            self.assertEqual(
                worker.fx_preview_contract.verify_material_binding(
                    shadow, binding, decode_probe=lambda _path: True
                ),
                binding,
            )
            worker.fx_overlay.render_for_timeline(
                shadow,
                snapshot["state"]["overlays"][0],
                shadow / "source/source.mp4",
                "",
            )
            (shadow / "working/generated-only-in-shadow.txt").write_text(
                "generated", encoding="utf-8"
            )
            Path(full).write_bytes(b"full")

        def fake_fx_render(project, overlay, *_args, **_kwargs):
            return {
                "source": "working/fx/B1-test/B1-test.mov",
                "material": {
                    **overlay["material"],
                    "path": str(Path(project) / overlay["material"]["path"]),
                },
            }

        def fake_ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"preview")
            return __import__("subprocess").CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(timeline, "render_project", side_effect=render_shadow),
            mock.patch.object(worker.fx_overlay, "render_for_timeline", side_effect=fake_fx_render),
            mock.patch.object(worker, "_broll_workspace_media", return_value=referenced),
            mock.patch.object(worker.subprocess, "run", side_effect=fake_ffmpeg),
            mock.patch.object(worker, "media_ok", return_value=True),
        ):
            report = worker.timeline_preview(
                self.project, cache, output, snapshot, 1.0, 2.0, dependencies
            )
        self.assertEqual(report["render_duration_s"], 2.0)
        self.assertIn("source/source.mp4", observed["files"])
        self.assertIn("assets/referenced.png", observed["files"])
        self.assertIn("assets/provenance.json", observed["files"])
        self.assertIn("working/structured_layers.json", observed["files"])
        self.assertNotIn("assets/unreferenced-large.bin", observed["files"])
        self.assertFalse(any("unrelated-link" in path for path in observed["files"]))
        self.assertFalse((self.project / "working/generated-only-in-shadow.txt").exists())
        self.assertFalse(any(cache.glob("timeline-project-*")))
        for path, identity in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), identity)

    def test_timeline_shadow_rejects_missing_or_changed_referenced_input(self):
        cache = self.project / "working/fx-preview/renderer-cache"
        output = self.project / "working/fx-preview/runtime/out.mp4"
        cache.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        asset = self.project / "assets/referenced.png"
        asset.parent.mkdir()
        asset.write_bytes(b"original")
        dependencies = {
            "source": {
                "relative": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "asset_digests": {
                "assets/referenced.png": hashlib.sha256(asset.read_bytes()).hexdigest(),
            },
            "timeline_project_inputs": {},
        }
        snapshot = {
            "manifest": {"source": {"duration_s": 10}},
            "state": {"segments": [{"source_start": 0, "source_end": 10}]},
        }
        for mutation in ("changed", "missing"):
            with self.subTest(mutation=mutation):
                asset.write_bytes(b"changed")
                if mutation == "missing":
                    asset.unlink()
                with self.assertRaises(worker.PreviewError) as got:
                    worker.timeline_preview(
                        self.project, cache, output, snapshot, 1.0, 2.0, dependencies
                    )
                self.assertEqual(got.exception.code, "dependency_changed")
                self.assertFalse(any(cache.glob("timeline-project-*")))


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

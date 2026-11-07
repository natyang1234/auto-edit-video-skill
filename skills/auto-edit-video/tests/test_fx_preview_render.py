#!/usr/bin/env python3
"""Unit contracts for the isolated Studio FX preview worker."""
import json
import hashlib
import fcntl
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
import fx_preview_jobs as jobs  # noqa: E402
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

    def _simple_timeline_inputs(self):
        cache = self.project / "working/fx-preview/renderer-cache"
        output = self.project / "working/fx-preview/runtime/out.mp4"
        cache.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        dependencies = {
            "source": {
                "relative": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "timeline_project_inputs": {},
        }
        snapshot = {
            "manifest": {"source": {"duration_s": 10, "path": "source/source.mp4"}},
            "state": {"segments": [{"source_start": 0, "source_end": 10}], "overlays": []},
        }
        return cache, output, dependencies, snapshot

    @staticmethod
    def _write_simple_timeline_output(
        _shadow,
        full,
        _quality,
        **_kwargs,
    ):
        Path(full).write_bytes(b"full")

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

        def render_shadow(
            shadow,
            full,
            quality,
            *,
            snapshot_path,
            preview_fx_quality,
            preview_output_window,
            preview_progress,
        ):
            shadow = Path(shadow)
            full = Path(full)
            self.assertEqual(quality, "preview")
            self.assertEqual(snapshot_path.parent.parent.parent, shadow)
            self.assertTrue(snapshot_path.name.startswith("fx-preview-"))
            self.assertEqual(snapshot_path.suffix, ".json")
            self.assertEqual(preview_fx_quality, "standard")
            self.assertEqual(preview_output_window, (1.0, 2.0))
            self.assertEqual(full.relative_to(shadow), Path("working/fx-preview-output.mp4"))
            preview_progress("encoding")
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
            full.write_bytes(b"full")

        def fake_fx_render(project, overlay, *_args, **_kwargs):
            return {
                "source": "working/fx/B1-test/B1-test.mov",
                "material": {
                    **overlay["material"],
                    "path": str(Path(project) / overlay["material"]["path"]),
                },
            }

        with (
            mock.patch.object(timeline, "render_project", side_effect=render_shadow),
            mock.patch.object(worker.fx_overlay, "render_for_timeline", side_effect=fake_fx_render),
            mock.patch.object(worker, "_broll_workspace_media", return_value=referenced),
            mock.patch.object(worker, "media_ok", return_value=True),
            mock.patch.object(worker, "progress") as progress_mock,
        ):
            report = worker.timeline_preview(
                self.project, cache, output, snapshot, 1.0, 2.0, dependencies
            )
        self.assertEqual(report["render_duration_s"], 2.0)
        self.assertEqual(output.read_bytes(), b"full")
        progress_mock.assert_any_call("encoding", "正在處理時間軸預覽")
        progress_mock.assert_any_call("validating", "正在驗證時間軸預覽")
        self.assertIn("source/source.mp4", observed["files"])
        self.assertIn("assets/referenced.png", observed["files"])
        self.assertIn("assets/provenance.json", observed["files"])
        self.assertIn("working/structured_layers.json", observed["files"])
        self.assertNotIn("assets/unreferenced-large.bin", observed["files"])
        self.assertFalse(any("unrelated-link" in path for path in observed["files"]))
        self.assertFalse((self.project / "working/generated-only-in-shadow.txt").exists())
        self.assertFalse(any(cache.glob("timeline-project-*")))
        self.assertFalse(any(cache.glob(".timeline-owner-*.lock")))
        for path, identity in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), identity)

    def test_timeline_failure_paths_remove_owned_shadow_and_unpublished_output(self):
        cache = self.project / "working/fx-preview/renderer-cache"
        output = self.project / "working/fx-preview/runtime/out.mp4"
        cache.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        dependencies = {
            "source": {
                "relative": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "timeline_project_inputs": {},
        }
        snapshot = {
            "manifest": {"source": {"duration_s": 10, "path": "source/source.mp4"}},
            "state": {"segments": [{"source_start": 0, "source_end": 10}], "overlays": []},
        }

        cases = (
            ("media_invalid", None, False, "output_invalid"),
            ("preview_error", worker.PreviewError("fixture_preview", "fixture"), True, "fixture_preview"),
            ("ordinary_error", RuntimeError("fixture"), True, "timeline_render_failed"),
        )
        for label, raised, media_valid, expected_code in cases:
            with self.subTest(case=label):
                captured = {}

                def render_shadow(
                    shadow,
                    full,
                    quality,
                    *,
                    snapshot_path,
                    preview_fx_quality,
                    preview_output_window,
                    preview_progress,
                ):
                    captured["shadow"] = Path(shadow)
                    captured["full"] = Path(full)
                    self.assertEqual(quality, "preview")
                    self.assertEqual(preview_fx_quality, "standard")
                    self.assertEqual(preview_output_window, (1.0, 2.0))
                    self.assertTrue(callable(preview_progress))
                    self.assertEqual(snapshot_path.parent.parent.parent, Path(shadow))
                    Path(full).write_bytes(b"unpublished")
                    if raised is not None:
                        raise raised

                with (
                    mock.patch.object(timeline, "render_project", side_effect=render_shadow),
                    mock.patch.object(worker, "media_ok", return_value=media_valid),
                ):
                    with self.assertRaises(worker.PreviewError) as got:
                        worker.timeline_preview(
                            self.project,
                            cache,
                            output,
                            snapshot,
                            1.0,
                            2.0,
                            dependencies,
                        )
                self.assertEqual(got.exception.code, expected_code)
                self.assertFalse(output.exists())
                self.assertFalse(captured["full"].exists())
                self.assertFalse(captured["shadow"].exists())
                self.assertFalse(any(cache.glob("timeline-project-*")))
                self.assertFalse(any(cache.glob(".timeline-owner-*.lock")))

    def test_direct_timeline_cleanup_removes_shadow_then_owned_lease(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        owner = "a" * 32
        with (
            mock.patch.dict(os.environ, {"FX_PREVIEW_SCRATCH_LEASE_FD": ""}),
            mock.patch.object(
                timeline,
                "render_project",
                side_effect=self._write_simple_timeline_output,
            ),
            mock.patch.object(worker, "media_ok", return_value=True),
        ):
            worker.timeline_preview(
                self.project,
                cache,
                output,
                snapshot,
                1.0,
                2.0,
                dependencies,
                scratch_owner=owner,
            )

        self.assertEqual(output.read_bytes(), b"full")
        self.assertFalse(any(cache.glob(f"timeline-project-{owner}-*")))
        self.assertFalse((cache / f".timeline-owner-{owner}.lock").exists())

    def test_malformed_matching_broll_window_closes_fds_and_cleans_owned_scratch(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        material = {
            "asset_id": "asset-fixture",
            "path": "assets/fixture.png",
            "sha256": "0" * 64,
            "kind": "image",
        }
        dependencies["material"] = material
        foreign = cache / "foreign.keep"
        foreign.write_text("keep", encoding="utf-8")
        original_open = os.open

        cases = (
            ("start_none", None, 3.0, TypeError, "1" * 32),
            ("end_string", 1.0, "not-a-number", ValueError, "2" * 32),
        )
        for label, candidate_start, candidate_end, cause_type, owner in cases:
            with self.subTest(case=label):
                current = json.loads(json.dumps(snapshot))
                current["state"]["overlays"] = [{
                    "id": "malformed-b1",
                    "type": "fx",
                    "code": "B1",
                    "start": candidate_start,
                    "end": candidate_end,
                    "material": material,
                }]
                lease_name = f".timeline-owner-{owner}.lock"
                opened: dict[str, int] = {}
                cache_identity = (cache.stat().st_dev, cache.stat().st_ino)

                def observed_open(path, flags, mode=0o777, *, dir_fd=None):
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == cache_identity:
                        opened["cache"] = descriptor
                    elif path == lease_name and dir_fd is not None:
                        opened["lease"] = descriptor
                    return descriptor

                with (
                    mock.patch.dict(os.environ, {"FX_PREVIEW_SCRATCH_LEASE_FD": ""}),
                    mock.patch.object(worker.os, "open", side_effect=observed_open),
                    mock.patch.object(timeline, "render_project") as render_mock,
                ):
                    with self.assertRaises(worker.PreviewError) as got:
                        worker.timeline_preview(
                            self.project,
                            cache,
                            output,
                            current,
                            1.0,
                            2.0,
                            dependencies,
                            scratch_owner=owner,
                        )

                self.assertEqual(got.exception.code, "timeline_render_failed")
                self.assertIsInstance(got.exception.__cause__, cause_type)
                self.assertEqual(set(opened), {"cache", "lease"})
                for descriptor in opened.values():
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                render_mock.assert_not_called()
                self.assertFalse(any(cache.glob(f"timeline-project-{owner}-*")))
                self.assertFalse((cache / lease_name).exists())
                self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

    def test_direct_timeline_rmtree_permission_error_keeps_lease_for_manager_retry(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        owner = "b" * 32
        lease = cache / f".timeline-owner-{owner}.lock"
        original_unlink = os.unlink
        injected = False

        def fail_shadow_project_json(path, *args, **kwargs):
            nonlocal injected
            if not injected and path == "project.json" and kwargs.get("dir_fd") is not None:
                injected = True
                raise PermissionError("injected shadow cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with (
            mock.patch.dict(os.environ, {"FX_PREVIEW_SCRATCH_LEASE_FD": ""}),
            mock.patch.object(
                timeline,
                "render_project",
                side_effect=self._write_simple_timeline_output,
            ),
            mock.patch.object(worker, "media_ok", return_value=True),
            mock.patch.object(worker.os, "unlink", side_effect=fail_shadow_project_json),
        ):
            worker.timeline_preview(
                self.project,
                cache,
                output,
                snapshot,
                1.0,
                2.0,
                dependencies,
                scratch_owner=owner,
            )

        shadows = list(cache.glob(f"timeline-project-{owner}-*"))
        self.assertTrue(injected)
        self.assertEqual(len(shadows), 1)
        self.assertTrue(lease.is_file())
        manager = jobs.FxPreviewJobManager(self.project, context_provider=lambda: {})
        try:
            self.assertFalse(shadows[0].exists())
            self.assertFalse(lease.exists())
        finally:
            manager.close()

    def test_inherited_parent_lease_is_never_unlinked_by_direct_timeline(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        owner = "c" * 32
        lease = cache / f".timeline-owner-{owner}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lease_fd = os.open(lease, flags, 0o600)
        self.addCleanup(os.close, lease_fd)
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lease_identity = (os.fstat(lease_fd).st_dev, os.fstat(lease_fd).st_ino)

        with (
            mock.patch.dict(
                os.environ,
                {"FX_PREVIEW_SCRATCH_LEASE_FD": str(lease_fd)},
            ),
            mock.patch.object(
                timeline,
                "render_project",
                side_effect=self._write_simple_timeline_output,
            ),
            mock.patch.object(worker, "media_ok", return_value=True),
        ):
            worker.timeline_preview(
                self.project,
                cache,
                output,
                snapshot,
                1.0,
                2.0,
                dependencies,
                scratch_owner=owner,
            )

        self.assertTrue(lease.is_file())
        self.assertEqual((lease.stat().st_dev, lease.stat().st_ino), lease_identity)
        self.assertFalse(any(cache.glob(f"timeline-project-{owner}-*")))

    def test_direct_timeline_replaced_shadow_keeps_lease_without_following_symlink(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        owner = "d" * 32
        lease = cache / f".timeline-owner-{owner}.lock"
        external = self.tmp / "external-shadow-target"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        foreign = cache / "foreign.keep"
        foreign.write_text("keep", encoding="utf-8")
        original_rmtree = shutil.rmtree

        def replace_shadow_with_symlink(path, ignore_errors=False):
            original_rmtree(path, ignore_errors=ignore_errors)
            Path(path).symlink_to(external, target_is_directory=True)

        with (
            mock.patch.dict(os.environ, {"FX_PREVIEW_SCRATCH_LEASE_FD": ""}),
            mock.patch.object(
                timeline,
                "render_project",
                side_effect=self._write_simple_timeline_output,
            ),
            mock.patch.object(worker, "media_ok", return_value=True),
            mock.patch.object(
                worker.shutil,
                "rmtree",
                side_effect=replace_shadow_with_symlink,
            ),
        ):
            worker.timeline_preview(
                self.project,
                cache,
                output,
                snapshot,
                1.0,
                2.0,
                dependencies,
                scratch_owner=owner,
            )

        shadows = list(cache.glob(f"timeline-project-{owner}-*"))
        self.assertEqual(len(shadows), 1)
        self.assertTrue(shadows[0].is_symlink())
        self.assertTrue(lease.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

        manager = jobs.FxPreviewJobManager(self.project, context_provider=lambda: {})
        try:
            self.assertFalse(shadows[0].exists())
            self.assertFalse(lease.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")
        finally:
            manager.close()

    def test_direct_timeline_keeps_lease_for_older_same_owner_shadow(self):
        cache, output, dependencies, snapshot = self._simple_timeline_inputs()
        owner = "e" * 32
        lease = cache / f".timeline-owner-{owner}.lock"
        old_shadow = (
            cache
            / f"timeline-project-{owner}-ffffffffffffffff-older-crash"
        )
        old_shadow.mkdir()
        (old_shadow / "partial.mp4").write_bytes(b"old")

        with (
            mock.patch.dict(os.environ, {"FX_PREVIEW_SCRATCH_LEASE_FD": ""}),
            mock.patch.object(
                timeline,
                "render_project",
                side_effect=self._write_simple_timeline_output,
            ),
            mock.patch.object(worker, "media_ok", return_value=True),
        ):
            worker.timeline_preview(
                self.project,
                cache,
                output,
                snapshot,
                1.0,
                2.0,
                dependencies,
                scratch_owner=owner,
            )

        self.assertTrue(old_shadow.is_dir())
        self.assertTrue(lease.is_file())
        manager = jobs.FxPreviewJobManager(self.project, context_provider=lambda: {})
        try:
            self.assertFalse(old_shadow.exists())
            self.assertFalse(lease.exists())
        finally:
            manager.close()

    def test_timeline_setup_failure_removes_only_its_locked_direct_lease(self):
        cache = self.project / "working/fx-preview/renderer-cache"
        output = self.project / "working/fx-preview/runtime/out.mp4"
        cache.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        dependencies = {
            "source": {
                "relative": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "timeline_project_inputs": {},
        }
        snapshot = {
            "manifest": {"source": {"duration_s": 10}},
            "state": {"segments": [{"source_start": 0, "source_end": 10}]},
        }
        with mock.patch.object(worker.tempfile, "mkdtemp", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                worker.timeline_preview(
                    self.project,
                    cache,
                    output,
                    snapshot,
                    1.0,
                    2.0,
                    dependencies,
                )
        self.assertFalse(any(cache.glob(".timeline-owner-*.lock")))
        self.assertFalse(any(cache.glob("timeline-project-*")))

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

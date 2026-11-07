from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import subprocess
import time
import hashlib
from unittest.mock import patch
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import style_presets  # noqa: E402
import editor_server  # noqa: E402
import asset_registry  # noqa: E402
from font_security import validate_font_bytes  # noqa: E402
from tests.font_test_fixture import build_ttf  # noqa: E402
from tests import test_fx_preview_api as fx_api_test  # noqa: E402


OFL_LICENSE = (Path(__file__).resolve().parents[1] / "contracts/licenses/OFL-1.1.txt").read_bytes()


def publish_verified_font(project: Path, candidate_name: str) -> tuple[dict, dict]:
    """Publish the same physical font under a project-local provider identity."""
    raw = build_ttf()
    result = validate_font_bytes(
        raw, "A", license_spdx="OFL-1.1", declared_mime="font/ttf"
    )
    blob_sha = hashlib.sha1(
        f"blob {len(raw)}\0".encode("ascii") + raw,
        usedforsecurity=False,
    ).hexdigest()
    candidate_id = f"{blob_sha}:{candidate_name}.ttf"
    candidate_hash = hashlib.sha256(candidate_id.encode()).hexdigest()[:16]
    source_url = (
        "https://github.com/google/fonts/blob/"
        f"2796410152d4f9524b68ed46e69c1b60f8e0f7c3/ofl/phasetwotest/{candidate_name}.ttf"
    )
    license_url = (
        "https://raw.githubusercontent.com/google/fonts/"
        "2796410152d4f9524b68ed46e69c1b60f8e0f7c3/ofl/phasetwotest/OFL.txt"
    )
    item = {
        "asset_id": f"font-google-fonts-{candidate_hash}-{result.sha256[:16]}",
        "path": f"assets/fonts/{result.sha256}.ttf",
        "sha256": result.sha256,
        "origin": "provider",
        "provider_id": "google-fonts",
        "source_url": source_url,
        "license": {
            "spdx": "OFL-1.1",
            "evidence_url": license_url,
            "attribution_required": True,
            "attribution_text": "Phase Two Test — OFL-1.1",
            "verified_at": "2026-08-04T03:00:00Z",
        },
        "review_status": "approved",
    }
    normalized, normalized_sha = asset_registry.validate_font_license_text(
        OFL_LICENSE, "OFL-1.1"
    )
    assert normalized
    license_sha = hashlib.sha256(OFL_LICENSE).hexdigest()
    for relative, payload in (
        (item["path"], raw),
        (f"licenses/{license_sha}.txt", OFL_LICENSE),
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    receipt = asset_registry.save_font_provider_receipt(
        project,
        item,
        candidate_id=candidate_id,
        query="ofl/phasetwotest",
        download_url=source_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/"),
        license_download_url=license_url,
        candidate_metadata={
            "family": "phasetwotest",
            "style": "",
            "weight": None,
            "subset": "",
            "unicode_range": "",
            "version": "2796410152d4f9524b68ed46e69c1b60f8e0f7c3",
            "source_url": source_url,
        },
        font_result=result,
        license_path=f"licenses/{license_sha}.txt",
        license_sha256=license_sha,
        license_normalized_sha256=normalized_sha,
        license_size=len(OFL_LICENSE),
        capability_identity=asset_registry._font_capability_identity(),
    )
    asset_registry.upsert_item(project, item)
    return item, receipt


def caption_settings(*, color: str = "#112233") -> dict:
    return {
        "style": {
            "font_size": 48,
            "color": color,
            "emphasis_color": "#D9482B",
            "stroke_color": "#000000",
            "stroke_width": 2,
            "x": 50,
            "y": 82,
            "max_width": 84,
            "animation": "fade",
        },
        "font": None,
    }


def fx_settings(*, opacity: float = 0.5) -> dict:
    return {
        "code": "T3",
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "opacity": opacity,
        "text_fit": True,
    }


class StylePresetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="style-presets-")
        self.addCleanup(self.temp.cleanup)
        # macOS exposes /var as a compatibility symlink. Resolve only the
        # test-owned temporary root so the store can keep rejecting every
        # symlink in the configured path itself.
        self.temp_root = Path(self.temp.name).resolve()
        self.directory = self.temp_root / "isolated-store"
        self.store = style_presets.StylePresetStore(self.directory)

    def test_missing_read_is_empty_and_does_not_create_directory(self) -> None:
        first = self.store.collection()
        self.assertEqual(first["presets"], [])
        self.assertFalse(self.directory.exists())
        self.assertEqual(first, style_presets.StylePresetStore(self.directory).collection())

    def test_crud_persists_and_uses_revision_cas(self) -> None:
        empty = self.store.collection()
        created = self.store.create(
            empty["revision"], "  品牌字幕  ", "caption", caption_settings()
        )
        self.assertEqual(created["preset"]["name"], "品牌字幕")
        self.assertEqual(len(created["presets"]), 1)
        self.assertEqual(
            style_presets.StylePresetStore(self.directory).collection()["presets"],
            created["presets"],
        )
        with self.assertRaises(style_presets.StylePresetError) as stale:
            self.store.create(empty["revision"], "另一筆", "caption", caption_settings())
        self.assertEqual(stale.exception.code, "library_revision_conflict")

        updated = self.store.update(
            created["preset"]["id"],
            created["revision"],
            "品牌字幕新版",
            "caption",
            caption_settings(color="#445566"),
        )
        self.assertEqual(updated["preset"]["settings"]["style"]["color"], "#445566")
        deleted = self.store.delete(updated["preset"]["id"], updated["revision"])
        self.assertEqual(deleted["deleted_id"], updated["preset"]["id"])
        self.assertEqual(deleted["presets"], [])

    def test_normalized_casefold_duplicate_and_kind_change_are_rejected(self) -> None:
        empty = self.store.collection()
        first = self.store.create(empty["revision"], "Élan", "caption", caption_settings())
        with self.assertRaises(style_presets.StylePresetError) as duplicate:
            self.store.create(first["revision"], " e\u0301LAN ", "caption", caption_settings())
        self.assertEqual(duplicate.exception.code, "name_conflict")
        with self.assertRaises(style_presets.StylePresetError) as changed:
            self.store.update(
                first["preset"]["id"], first["revision"], "Élan", "fx", fx_settings()
            )
        self.assertEqual(changed.exception.code, "kind_immutable")

    def test_schema_rejects_unknown_path_asset_and_nonfinite_values(self) -> None:
        invalid_caption = caption_settings()
        invalid_caption["style"]["font_asset_id"] = "font-secret"
        cases = [
            ("caption", invalid_caption),
            ("caption", {**caption_settings(), "path": "/tmp/font.ttf"}),
            ("caption", {"style": {"font_size": float("nan")}, "font": None}),
            ("fx", {**fx_settings(), "material": {"path": "/tmp/a.mp4"}}),
            ("fx", {**fx_settings(), "opacity": float("inf")}),
            ("fx", {**fx_settings(), "source": "project.json"}),
        ]
        for kind, settings in cases:
            with self.subTest(kind=kind, settings=settings):
                with self.assertRaises(style_presets.StylePresetError):
                    style_presets.normalize_settings(kind, settings)

    def test_omitted_caption_font_canonicalizes_to_preserve(self) -> None:
        normalized = style_presets.normalize_caption_settings(
            {"style": {"color": "#123456"}}
        )
        self.assertEqual(normalized, {"style": {"color": "#123456"}, "font": None})

    def test_fx_zero_and_half_are_canonical_and_text_is_not_path_interpreted(self) -> None:
        for opacity in (0, 0.5, 1):
            normalized = style_presets.normalize_fx_settings(
                {**fx_settings(opacity=opacity), "values": {"line1": "https://example/a/b"}}
            )
            self.assertEqual(normalized["opacity"], float(opacity))
            self.assertEqual(normalized["values"]["line1"], "https://example/a/b")

    def test_corrupt_and_unsafe_store_are_preserved(self) -> None:
        self.directory.mkdir()
        path = self.directory / "presets.json"
        path.write_bytes(b"{not-json")
        before = path.read_bytes()
        with self.assertRaises(style_presets.StylePresetError) as corrupt:
            self.store.create("0" * 64, "name", "caption", caption_settings())
        self.assertEqual(corrupt.exception.code, "store_corrupt")
        self.assertEqual(path.read_bytes(), before)

        path.unlink()
        outside = self.temp_root / "outside.json"
        outside.write_text(json.dumps(style_presets.empty_document()), encoding="utf-8")
        path.symlink_to(outside)
        with self.assertRaises(style_presets.StylePresetError) as unsafe:
            self.store.collection()
        self.assertEqual(unsafe.exception.code, "store_unsafe")
        self.assertEqual(json.loads(outside.read_text()), style_presets.empty_document())

    def test_ancestor_symlink_is_rejected_without_touching_foreign_directory(self) -> None:
        outside = self.temp_root / "foreign"
        outside.mkdir()
        link = self.temp_root / "linked-parent"
        link.symlink_to(outside, target_is_directory=True)
        unsafe_store = style_presets.StylePresetStore(link / "presets")

        with self.assertRaises(style_presets.StylePresetError) as read_error:
            unsafe_store.collection()
        self.assertEqual(read_error.exception.code, "store_unsafe")
        with self.assertRaises(style_presets.StylePresetError) as write_error:
            unsafe_store.create(
                style_presets.revision_for_document(style_presets.empty_document()),
                "不得寫入",
                "caption",
                caption_settings(),
            )
        self.assertEqual(write_error.exception.code, "store_unsafe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_same_revision_has_one_winner_without_lost_update(self) -> None:
        revision = self.store.collection()["revision"]
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def create(name: str) -> None:
            local = style_presets.StylePresetStore(self.directory)
            barrier.wait()
            try:
                local.create(revision, name, "caption", caption_settings())
                outcomes.append("ok")
            except style_presets.StylePresetError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=create, args=(name,)) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["library_revision_conflict", "ok"])
        self.assertEqual(len(self.store.collection()["presets"]), 1)

    def test_apply_reader_recreates_missing_lock_and_blocks_cross_process_writer(self) -> None:
        first = self.store.create(
            self.store.collection()["revision"], "A", "caption", caption_settings()
        )
        self.store.lock_path.unlink()
        command = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); import style_presets; "
            "from tests.test_style_presets import caption_settings; "
            "s=style_presets.StylePresetStore(Path(sys.argv[2])); "
            "s.create(sys.argv[3], 'B', 'caption', caption_settings())"
        )
        process = None
        with self.store.locked_collection() as locked:
            self.assertEqual(locked["revision"], first["revision"])
            self.assertTrue(self.store.lock_path.is_file())
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    command,
                    str(SCRIPTS),
                    str(self.directory),
                    first["revision"],
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
            )
            time.sleep(0.15)
            self.assertIsNone(process.poll(), "writer bypassed the apply reader lock")
        assert process is not None
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(len(self.store.collection()["presets"]), 2)

    def test_environment_override_is_explicit_and_default_is_not_touched(self) -> None:
        override = self.temp_root / "override"
        old = os.environ.get("AUTO_EDIT_PRESETS_DIR")
        os.environ["AUTO_EDIT_PRESETS_DIR"] = str(override)
        self.addCleanup(
            lambda: os.environ.__setitem__("AUTO_EDIT_PRESETS_DIR", old)
            if old is not None
            else os.environ.pop("AUTO_EDIT_PRESETS_DIR", None)
        )
        store = style_presets.StylePresetStore()
        self.assertEqual(store.directory, override)
        self.assertFalse((Path.home() / "AutoEditStudio" / "presets").exists())


class StylePresetApiTests(unittest.TestCase):
    """Exercise the real HTTP/CSRF/persistence path without inheriting FX tests."""

    def setUp(self) -> None:
        self.old_store = os.environ.get("AUTO_EDIT_PRESETS_DIR")
        self.store_temp = tempfile.TemporaryDirectory(prefix="style-presets-api-store-")
        self.store_dir = Path(self.store_temp.name).resolve() / "store"
        os.environ["AUTO_EDIT_PRESETS_DIR"] = str(self.store_dir)
        self.harness = fx_api_test.FxPreviewApiTests(methodName="runTest")
        self.harness.setUp()

    def tearDown(self) -> None:
        self.harness.doCleanups()
        if self.old_store is None:
            os.environ.pop("AUTO_EDIT_PRESETS_DIR", None)
        else:
            os.environ["AUTO_EDIT_PRESETS_DIR"] = self.old_store
        self.store_temp.cleanup()

    def request(self, method: str, path: str, payload: object | None = None):
        return self.harness._json_request(method, path, payload)

    def install_caption(
        self, harness: fx_api_test.FxPreviewApiTests | None = None
    ) -> tuple[dict, str]:
        harness = harness or self.harness
        state_path = harness.project / "working/editor_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["overlays"].append({
            "id": "caption-preset-target",
            "type": "caption",
            "start": 1.0,
            "end": 2.0,
            "text": "A",
            "visible": True,
            "locked": False,
            "z_index": 20,
            "style": {"color": "#FFFFFF", "font_size": 32.0},
        })
        state["revision"] = editor_server.editor_state_revision(state)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return state, state["revision"]

    def create_caption_preset(self, revision: str, *, color: str = "#123456") -> dict:
        status, payload = self.request("POST", "/api/style-presets", {
            "expected_library_revision": revision,
            "name": "跨專案字幕",
            "kind": "caption",
            "settings": {"style": {"color": color}},
        })
        self.assertEqual(status, 201, payload)
        return payload

    def test_crud_contract_csrf_and_library_cas(self) -> None:
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        self.assertEqual(set(empty), {"ok", "revision", "presets"})
        self.assertFalse(self.store_dir.exists())

        status, _headers, raw = self.harness._request(
            "POST",
            "/api/style-presets",
            json.dumps({}).encode(),
            {"Content-Type": "application/json", "X-Auto-Edit-CSRF": "wrong"},
        )
        self.assertEqual(status, 403, raw)
        self.assertFalse(self.store_dir.exists())

        created = self.create_caption_preset(empty["revision"])
        self.assertEqual(set(created), {"ok", "revision", "presets", "preset"})
        self.assertEqual(created["preset"]["settings"]["font"], None)
        self.assertNotIn("font_asset_id", json.dumps(created))

        status, stale = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "stale",
            "kind": "caption",
            "settings": {"style": {"color": "#654321"}},
        })
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["reason"], "library_revision_conflict")

        preset = created["preset"]
        status, updated = self.request("PUT", f"/api/style-presets/{preset['id']}", {
            "expected_library_revision": created["revision"],
            "name": "跨專案字幕新版",
            "kind": "caption",
            "settings": {"style": {"color": "#ABCDEF"}},
        })
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["preset"]["settings"]["font"], None)
        status, deleted = self.request(
            "DELETE",
            f"/api/style-presets/{preset['id']}",
            {"expected_library_revision": updated["revision"]},
        )
        self.assertEqual(status, 200, deleted)
        self.assertEqual(set(deleted), {"ok", "revision", "presets", "deleted_id"})

    def test_caption_plan_apply_digest_and_no_change_are_transactional(self) -> None:
        _state, project_revision = self.install_caption()
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        created = self.create_caption_preset(empty["revision"])
        base = {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": project_revision,
            "overlay_id": "caption-preset-target",
            "scope": "single",
        }
        state_path = self.harness.project / "working/editor_state.json"
        before_plan = state_path.read_bytes()
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        self.assertEqual(state_path.read_bytes(), before_plan)
        self.assertEqual(plan["changes"], [{
            "overlay_id": "caption-preset-target",
            "field": "style.color",
            "before": "#FFFFFF",
            "after": "#123456",
        }])
        self.assertEqual(plan["applied_to"], ["caption-preset-target"])

        status, changed = self.request("POST", "/api/style-presets/caption-apply", {
            **base,
            "plan_digest": "0" * 64,
        })
        self.assertEqual(status, 409, changed)
        self.assertEqual(changed["reason"], "plan_changed")
        self.assertEqual(state_path.read_bytes(), before_plan)

        status, applied = self.request("POST", "/api/style-presets/caption-apply", {
            **base,
            "plan_digest": plan["plan_digest"],
        })
        self.assertEqual(status, 200, applied)
        self.assertFalse(applied["no_change"])
        target = next(item for item in applied["state"]["overlays"] if item["id"] == "caption-preset-target")
        self.assertEqual(target["style"]["color"], "#123456")
        self.assertNotIn("font_asset_id", target["style"])
        self.assertEqual(target["text"], "A")
        self.assertEqual((target["start"], target["end"]), (1.0, 2.0))

        no_change_base = {**base, "expected_revision": applied["revision"]}
        status, no_change_plan = self.request(
            "POST", "/api/style-presets/caption-plan", no_change_base
        )
        self.assertEqual(status, 200, no_change_plan)
        self.assertTrue(no_change_plan["no_change"])
        before_no_change = state_path.read_bytes()
        status, no_change = self.request("POST", "/api/style-presets/caption-apply", {
            **no_change_base,
            "plan_digest": no_change_plan["plan_digest"],
        })
        self.assertEqual(status, 200, no_change)
        self.assertTrue(no_change["no_change"])
        self.assertEqual(no_change["invalidated_gates"], [])
        self.assertEqual(state_path.read_bytes(), before_no_change)

    def test_semantically_equal_caption_style_is_no_change_and_preserves_bytes(self) -> None:
        _state, project_revision = self.install_caption()
        state_path = self.harness.project / "working/editor_state.json"
        manifest_path = self.harness.project / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        target = next(item for item in state["overlays"] if item["id"] == "caption-preset-target")
        target["style"] = {
            "font_size": 58,
            "stroke_width": 5,
            "x": 50,
            "max_width": 86,
            "color": "#abcdef",
            "stroke_color": "#0a0b0c",
        }
        state["revision"] = editor_server.editor_state_revision(state)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        project_revision = state["revision"]
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        status, created = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "語意相同字幕",
            "kind": "caption",
            "settings": {
                "style": {
                    "font_size": 58.0,
                    "stroke_width": 5.0,
                    "x": 50.0,
                    "max_width": 86.0,
                    "color": "#ABCDEF",
                    "stroke_color": "#0A0B0C",
                }
            },
        })
        self.assertEqual(status, 201, created)
        base = {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": project_revision,
            "overlay_id": "caption-preset-target",
            "scope": "single",
        }
        state_before = state_path.read_bytes()
        manifest_before = manifest_path.read_bytes()
        state_mtime = state_path.stat().st_mtime_ns
        manifest_mtime = manifest_path.stat().st_mtime_ns
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        self.assertTrue(plan["no_change"])
        self.assertEqual(plan["changes"], [])
        status, applied = self.request("POST", "/api/style-presets/caption-apply", {
            **base,
            "plan_digest": plan["plan_digest"],
        })
        self.assertEqual(status, 200, applied)
        self.assertTrue(applied["no_change"])
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(state_path.stat().st_mtime_ns, state_mtime)
        self.assertEqual(manifest_path.stat().st_mtime_ns, manifest_mtime)
        returned = next(item for item in applied["state"]["overlays"] if item["id"] == "caption-preset-target")
        self.assertIs(type(returned["style"]["font_size"]), int)
        self.assertEqual(returned["style"]["color"], "#abcdef")

    def test_caption_apply_restores_both_files_when_manifest_write_fails(self) -> None:
        _state, project_revision = self.install_caption()
        manifest_path = self.harness.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": "stale",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        created = self.create_caption_preset(empty["revision"])
        base = {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": project_revision,
            "overlay_id": "caption-preset-target",
            "scope": "single",
        }
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        state_path = self.harness.project / "working/editor_state.json"
        state_before = state_path.read_bytes()
        manifest_before = manifest_path.read_bytes()
        real_atomic = editor_server.atomic_write_json

        def fail_manifest(path: Path, payload: object) -> None:
            if Path(path).resolve() == manifest_path.resolve():
                raise OSError("injected project manifest write failure")
            real_atomic(Path(path), payload)

        with patch.object(editor_server, "atomic_write_json", side_effect=fail_manifest):
            status, failed = self.request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            })
        self.assertEqual(status, 500, failed)
        self.assertEqual(failed["reason"], "transaction_failed")
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertFalse(
            (self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL).exists()
        )

    def test_prepared_caption_apply_journal_recovers_exact_before_bytes(self) -> None:
        state_path = self.harness.project / "working/editor_state.json"
        manifest_path = self.harness.project / "project.json"
        state_before = state_path.read_bytes()
        manifest_before = manifest_path.read_bytes()
        editor_server.begin_caption_preset_transaction(self.harness.project)
        state_path.write_bytes(b'{"partial":"state"}\n')
        manifest_path.write_bytes(b'{"partial":"manifest"}\n')

        editor_server.recover_caption_preset_transaction(self.harness.project)

        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertFalse(
            (self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL).exists()
        )

    def test_caption_apply_retains_journal_when_rollback_itself_fails(self) -> None:
        _state, project_revision = self.install_caption()
        manifest_path = self.harness.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": "stale",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        created = self.create_caption_preset(empty["revision"])
        base = {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": project_revision,
            "overlay_id": "caption-preset-target",
            "scope": "single",
        }
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        real_atomic = editor_server.atomic_write_json

        def fail_manifest(path: Path, payload: object) -> None:
            if Path(path).resolve() == manifest_path.resolve():
                raise OSError("injected project manifest write failure")
            real_atomic(Path(path), payload)

        with (
            patch.object(editor_server, "atomic_write_json", side_effect=fail_manifest),
            patch.object(
                editor_server,
                "_atomic_write_exact",
                side_effect=OSError("injected rollback failure"),
            ),
        ):
            status, failed = self.request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            })
        self.assertEqual(status, 500, failed)
        self.assertEqual(failed["reason"], "transaction_recovery_required")
        journal = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        self.assertTrue(journal.is_file())
        self.assertEqual(json.loads(journal.read_text())["phase"], "prepared")
        # Once the injected storage fault is gone, the retained journal is
        # sufficient to finish the exact rollback.
        editor_server.recover_caption_preset_transaction(self.harness.project)
        self.assertFalse(journal.exists())

    def test_portable_font_resolves_same_bytes_to_current_project_asset(self) -> None:
        item1, _receipt1 = publish_verified_font(self.harness.project, "ProjectOne")
        fonts1 = asset_registry.list_project_fonts(self.harness.project)
        self.assertEqual(len(fonts1), 1)
        descriptor = {
            key: fonts1[0][key] for key in ("sha256", "family", "style", "weight")
        }
        self.assertIsInstance(descriptor["style"], str)
        self.assertIs(type(descriptor["weight"]), int)

        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        status, created = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "實體字型字幕",
            "kind": "caption",
            "settings": {"style": {"font_size": 44}, "font": descriptor},
        })
        self.assertEqual(status, 201, created)
        self.assertNotIn("asset_id", json.dumps(created["preset"], ensure_ascii=False))

        second = fx_api_test.FxPreviewApiTests(methodName="runTest")
        second.setUp()
        try:
            _state2, revision2 = self.install_caption(second)
            status, shared = second._json_request("GET", "/api/style-presets")
            self.assertEqual(status, 200, shared)
            base = {
                "preset_id": created["preset"]["id"],
                "expected_library_revision": created["revision"],
                "expected_revision": revision2,
                "overlay_id": "caption-preset-target",
                "scope": "single",
            }
            status, unavailable = second._json_request(
                "POST", "/api/style-presets/caption-plan", base
            )
            self.assertEqual(status, 409, unavailable)
            self.assertEqual(unavailable["reason"], "font_unavailable")

            item2, _receipt2 = publish_verified_font(second.project, "ProjectTwo")
            self.assertNotEqual(item1["asset_id"], item2["asset_id"])
            status, font_public = second._json_request("GET", "/api/fonts")
            self.assertEqual(status, 200, font_public)
            public = next(item for item in font_public["fonts"] if item["asset_id"] == item2["asset_id"])
            self.assertEqual(
                {key: public[key] for key in ("sha256", "family", "style", "weight")},
                descriptor,
            )

            status, plan = second._json_request(
                "POST", "/api/style-presets/caption-plan", base
            )
            self.assertEqual(status, 200, plan)
            font_change = next(change for change in plan["changes"] if change["field"] == "font")
            self.assertEqual(font_change["after"], descriptor)
            self.assertNotIn("asset_id", font_change)
            status, applied = second._json_request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            })
            self.assertEqual(status, 200, applied)
            target = next(
                item for item in applied["state"]["overlays"]
                if item["id"] == "caption-preset-target"
            )
            self.assertEqual(target["style"]["font_asset_id"], item2["asset_id"])
            self.assertNotEqual(target["style"]["font_asset_id"], item1["asset_id"])
        finally:
            second.doCleanups()


if __name__ == "__main__":
    unittest.main()

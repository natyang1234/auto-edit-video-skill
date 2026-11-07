from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
import subprocess
import time
import hashlib
import copy
import errno
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import style_presets  # noqa: E402
import editor_server  # noqa: E402
import studio_server  # noqa: E402
import asset_registry  # noqa: E402
from font_security import validate_font_bytes  # noqa: E402
from tests.font_test_fixture import build_ttf  # noqa: E402
from tests import test_fx_preview_api as fx_api_test  # noqa: E402


OFL_LICENSE = (Path(__file__).resolve().parents[1] / "contracts/licenses/OFL-1.1.txt").read_bytes()


class StylePresetOSProxy:
    """Track only style_presets' successful open/close pairs without patching global os."""

    def __init__(self) -> None:
        self._delegate = os
        self._lock = threading.Lock()
        self._live: dict[int, object] = {}

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def open(self, *args, **kwargs) -> int:
        with self._lock:
            descriptor = self._delegate.open(*args, **kwargs)
            self._live[descriptor] = args[0] if args else kwargs.get("path")
        return descriptor

    def close(self, descriptor: int) -> None:
        with self._lock:
            self._delegate.close(descriptor)
            self._live.pop(descriptor, None)

    @property
    def live_fds(self) -> dict[int, object]:
        with self._lock:
            return dict(self._live)


@contextmanager
def short_socket_temporary_directory(prefix: str):
    """Create a task-owned AF_UNIX fixture path without platform-specific hardcoding."""
    if not hasattr(socket, "AF_UNIX"):
        raise unittest.SkipTest("AF_UNIX is unavailable on this platform")
    bases: list[Path | None] = []
    for candidate in (Path("/tmp"), Path(tempfile.gettempdir())):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in bases:
            bases.append(resolved)
    bases.append(None)
    attempts: list[str] = []
    temporary = None
    root = None
    for base in bases:
        try:
            candidate_temp = tempfile.TemporaryDirectory(
                prefix=prefix,
                dir=None if base is None else base,
            )
        except OSError as exc:
            attempts.append(f"{base or 'default'}: {type(exc).__name__}")
            continue
        candidate_root = Path(candidate_temp.name)
        longest = max(
            len(os.fsencode(candidate_root / "presets.json")),
            len(os.fsencode(candidate_root / ".presets.lock")),
        )
        if longest <= 103:
            temporary = candidate_temp
            root = candidate_root
            break
        attempts.append(f"{base or 'default'}: path-bytes={longest}")
        candidate_temp.cleanup()
    if temporary is None or root is None:
        raise unittest.SkipTest(
            "no task-owned AF_UNIX fixture path fits the 103-byte portable limit; "
            + "; ".join(attempts)
        )
    try:
        yield root
    finally:
        temporary.cleanup()


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

    def test_fd_tracker_serializes_close_with_reused_descriptor_open(self) -> None:
        class PausedCloseDelegate:
            def __init__(self) -> None:
                self.real_close_done = threading.Event()
                self.allow_close_return = threading.Event()

            def __getattr__(self, name: str):
                return getattr(os, name)

            def close(self, descriptor: int) -> None:
                os.close(descriptor)
                self.real_close_done.set()
                if not self.allow_close_return.wait(timeout=5):
                    raise TimeoutError("fd tracker close barrier timed out")

        self.directory.mkdir()
        tracker = StylePresetOSProxy()
        delegate = PausedCloseDelegate()
        tracker._delegate = delegate
        first_path = self.directory / "first.tmp"
        second_path = self.directory / "second.tmp"
        first_fd = tracker.open(
            first_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        close_errors: list[BaseException] = []
        open_errors: list[BaseException] = []
        second: list[int] = []
        opened = threading.Event()

        def close_first() -> None:
            try:
                tracker.close(first_fd)
            except BaseException as exc:
                close_errors.append(exc)

        def open_second() -> None:
            try:
                second.append(tracker.open(
                    second_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                ))
            except BaseException as exc:
                open_errors.append(exc)
            finally:
                opened.set()

        closer = threading.Thread(target=close_first)
        closer.start()
        self.assertTrue(delegate.real_close_done.wait(timeout=5))
        opener = threading.Thread(target=open_second)
        opener.start()
        self.assertFalse(opened.wait(timeout=0.1), "open bypassed the tracker close lock")
        delegate.allow_close_return.set()
        closer.join(timeout=5)
        opener.join(timeout=5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(opener.is_alive())
        self.assertEqual(close_errors, [])
        self.assertEqual(open_errors, [])
        self.assertEqual(len(second), 1)
        second_fd = second[0]
        self.assertEqual(first_fd, second_fd)
        self.assertIn(second_fd, tracker.live_fds)
        self.assertTrue(stat.S_ISREG(os.fstat(second_fd).st_mode))
        tracker.close(second_fd)
        self.assertEqual(tracker.live_fds, {})

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

    def test_fx_catalog_drift_is_per_record_and_does_not_change_revision(self) -> None:
        created = self.store.create(
            self.store.collection()["revision"], "FX drift", "fx", fx_settings()
        )
        persisted_before = self.store.path.read_bytes()
        revision_before = created["revision"]
        drift = copy.deepcopy(style_presets.fx_overlay.catalog(refresh=True))
        for effect in drift.get("effects", []):
            if effect.get("code") == "T3":
                effect["available"] = False

        with patch.object(style_presets.fx_overlay, "catalog", return_value=drift) as catalog_mock:
            collection = self.store.collection()
            self.assertEqual(catalog_mock.call_count, 1)
            unavailable = next(
                item for item in collection["presets"] if item["id"] == created["preset"]["id"]
            )
            self.assertEqual(unavailable["availability"]["status"], "unavailable")
            self.assertEqual(unavailable["availability"]["reason"], "effect_unavailable")
            self.assertEqual(collection["revision"], revision_before)
            self.assertEqual(self.store.path.read_bytes(), persisted_before)
            with self.assertRaises(style_presets.StylePresetError) as unavailable_write:
                self.store.update(
                    unavailable["id"],
                    collection["revision"],
                    unavailable["name"],
                    "fx",
                    unavailable["settings"],
                )
            self.assertEqual(unavailable_write.exception.code, "effect_unavailable")

            caption = self.store.create(
                collection["revision"], "Caption survives drift", "caption", caption_settings()
            )
            deleted = self.store.delete(unavailable["id"], caption["revision"])
            self.assertEqual(deleted["deleted_id"], unavailable["id"])
            self.assertEqual([item["kind"] for item in deleted["presets"]], ["caption"])

        with patch.object(
            style_presets.fx_overlay,
            "catalog",
            return_value={"effects": None, "schemes": {}, "fonts": []},
        ):
            # The remaining library contains only a caption, so runtime FX
            # catalog corruption is not consulted and cannot block caption CRUD.
            caption_only = self.store.collection()
            self.assertEqual([item["kind"] for item in caption_only["presets"]], ["caption"])

        persisted = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertTrue(all("availability" not in record for record in persisted["presets"]))

    def test_fx_missing_global_font_is_unavailable_but_schema_corruption_still_fails(self) -> None:
        verified = next(
            (
                item for item in style_presets.fx_overlay.fonts_catalog().get("fonts", [])
                if item.get("verified") is True
            ),
            None,
        )
        if verified is None:
            self.skipTest("no verified global FX font fixture")
        settings = fx_settings()
        settings["font_ids"] = {"display": verified["id"]}
        created = self.store.create(
            self.store.collection()["revision"], "FX font", "fx", settings
        )
        missing_fonts_catalog = copy.deepcopy(style_presets.fx_overlay.catalog(refresh=True))
        missing_fonts_catalog["fonts"] = []
        with patch.object(
            style_presets.fx_overlay, "catalog", return_value=missing_fonts_catalog
        ) as catalog_mock:
            collection = self.store.collection()
            self.assertEqual(catalog_mock.call_count, 1)
            record = next(item for item in collection["presets"] if item["id"] == created["preset"]["id"])
            self.assertEqual(record["availability"]["reason"], "font_unavailable")

        with patch.object(
            style_presets.fx_overlay,
            "catalog",
            return_value={"effects": None, "schemes": {}, "fonts": []},
        ):
            malformed_runtime = self.store.collection()
            self.assertEqual(
                malformed_runtime["presets"][0]["availability"]["reason"],
                "catalog_changed",
            )

        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        raw["presets"][0]["settings"]["path"] = "/tmp/unsafe"
        self.store.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(style_presets.StylePresetError) as corrupt:
            self.store.collection()
        self.assertEqual(corrupt.exception.code, "store_corrupt")

    def test_fx_catalog_import_failure_is_recoverable_but_new_fx_write_fails_closed(self) -> None:
        created = self.store.create(
            self.store.collection()["revision"], "FX import drift", "fx", fx_settings()
        )
        revision_before = created["revision"]
        bytes_before = self.store.path.read_bytes()
        with patch.object(
            style_presets.fx_overlay,
            "catalog",
            side_effect=ImportError("injected missing compose dependency"),
        ):
            collection = self.store.collection()
            self.assertEqual(collection["revision"], revision_before)
            self.assertEqual(self.store.path.read_bytes(), bytes_before)
            self.assertEqual(
                collection["presets"][0]["availability"]["reason"],
                "catalog_changed",
            )
            with self.assertRaises(style_presets.StylePresetError) as rejected_write:
                self.store.create(
                    collection["revision"], "new FX", "fx", fx_settings()
                )
            self.assertEqual(rejected_write.exception.code, "catalog_changed")

            caption = self.store.create(
                collection["revision"], "Caption remains usable", "caption", caption_settings()
            )
            deleted = self.store.delete(created["preset"]["id"], caption["revision"])
            self.assertEqual(deleted["deleted_id"], created["preset"]["id"])
            self.assertEqual([item["kind"] for item in deleted["presets"]], ["caption"])

    def test_fx_catalog_general_exceptions_are_typed_without_swallowing_base_exceptions(self) -> None:
        class CatalogProbeError(Exception):
            pass

        for index, failure in enumerate(
            (
                KeyError("effects"),
                AttributeError("catalog root has no get"),
                CatalogProbeError("unexpected runtime catalog failure"),
            )
        ):
            with self.subTest(failure=type(failure).__name__):
                store = style_presets.StylePresetStore(
                    self.temp_root / f"catalog-exception-{index}"
                )
                created = store.create(
                    store.collection()["revision"], "FX", "fx", fx_settings()
                )
                revision_before = created["revision"]
                bytes_before = store.path.read_bytes()
                with patch.object(
                    style_presets.fx_overlay, "catalog", side_effect=failure
                ):
                    collection = store.collection()
                    self.assertEqual(collection["revision"], revision_before)
                    self.assertEqual(store.path.read_bytes(), bytes_before)
                    self.assertEqual(
                        collection["presets"][0]["availability"]["reason"],
                        "catalog_changed",
                    )
                    with self.assertRaises(style_presets.StylePresetError) as rejected:
                        store.create(
                            collection["revision"], "new FX", "fx", fx_settings()
                        )
                    self.assertEqual(rejected.exception.code, "catalog_changed")
                    caption = store.create(
                        collection["revision"],
                        "Caption remains usable",
                        "caption",
                        caption_settings(),
                    )
                    deleted = store.delete(created["preset"]["id"], caption["revision"])
                    self.assertEqual(deleted["deleted_id"], created["preset"]["id"])
                    self.assertEqual(
                        [item["kind"] for item in deleted["presets"]], ["caption"]
                    )

        for base_failure in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(base_failure=type(base_failure).__name__):
                store = style_presets.StylePresetStore(
                    self.temp_root / f"base-exception-{type(base_failure).__name__}"
                )
                store.create(store.collection()["revision"], "FX", "fx", fx_settings())
                with patch.object(
                    style_presets.fx_overlay, "catalog", side_effect=base_failure
                ):
                    with self.assertRaises(type(base_failure)):
                        store.collection()
                    with self.assertRaises(type(base_failure)):
                        store.create(
                            style_presets.revision_for_document(
                                json.loads(store.path.read_text(encoding="utf-8"))
                            ),
                            "new FX",
                            "fx",
                            fx_settings(),
                        )

    def test_environment_override_is_explicit_and_default_is_not_touched(self) -> None:
        override = self.temp_root / "override"
        fake_home = self.temp_root / "fake-home"
        fake_default = fake_home / "AutoEditStudio" / "presets"
        fake_default.mkdir(parents=True)
        operator_bytes = (
            json.dumps(
                style_presets.empty_document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        (fake_default / "presets.json").write_bytes(operator_bytes)
        old = os.environ.get("AUTO_EDIT_PRESETS_DIR")
        os.environ["AUTO_EDIT_PRESETS_DIR"] = str(override)
        self.addCleanup(
            lambda: os.environ.__setitem__("AUTO_EDIT_PRESETS_DIR", old)
            if old is not None
            else os.environ.pop("AUTO_EDIT_PRESETS_DIR", None)
        )
        with patch.object(Path, "home", return_value=fake_home):
            store = style_presets.StylePresetStore()
            self.assertEqual(store.directory, override)
            self.assertEqual(store.collection()["presets"], [])
            os.environ.pop("AUTO_EDIT_PRESETS_DIR", None)
            default_store = style_presets.StylePresetStore()
            self.assertEqual(default_store.directory, fake_default)
            self.assertEqual(default_store.collection()["presets"], [])
        self.assertEqual((fake_default / "presets.json").read_bytes(), operator_bytes)
        self.assertFalse(override.exists())


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

    def test_http_fx_drift_metadata_allows_delete_without_revision_change(self) -> None:
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        status, created = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "HTTP FX drift",
            "kind": "fx",
            "settings": fx_settings(),
        })
        self.assertEqual(status, 201, created)
        persisted_before = self.harness.server.style_preset_store.path.read_bytes()
        drift = copy.deepcopy(style_presets.fx_overlay.catalog(refresh=True))
        for effect in drift.get("effects", []):
            if effect.get("code") == "T3":
                effect["available"] = False
        with patch.object(style_presets.fx_overlay, "catalog", return_value=drift):
            status, collection = self.request("GET", "/api/style-presets")
            self.assertEqual(status, 200, collection)
            self.assertEqual(collection["revision"], created["revision"])
            self.assertEqual(collection["presets"][0]["availability"]["reason"], "effect_unavailable")
            self.assertEqual(
                self.harness.server.style_preset_store.path.read_bytes(), persisted_before
            )
            status, deleted = self.request(
                "DELETE",
                f"/api/style-presets/{created['preset']['id']}",
                {"expected_library_revision": collection["revision"]},
            )
            self.assertEqual(status, 200, deleted)
            self.assertEqual(deleted["presets"], [])

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

    def test_corrupt_recovery_journal_keeps_editor_offline_with_actionable_cli_error(self) -> None:
        project = self.harness.project.resolve()
        journal = project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        journal.write_bytes(b"{corrupt-journal")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "editor_server.py"),
                "--project-dir",
                str(project),
                "--port",
                "0",
            ],
            text=True,
            capture_output=True,
            timeout=10,
            env=dict(os.environ),
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(str(journal), result.stderr)
        self.assertIn("Do not delete or edit this journal", result.stderr)
        self.assertIn("disk space or file permissions", result.stderr)
        self.assertIn("embedded before-backup", result.stderr)
        self.assertTrue(journal.is_file())
        self.assertEqual(journal.read_bytes(), b"{corrupt-journal")

    def test_unhashable_phase_and_bool_schema_journals_fail_closed_without_disconnect(self) -> None:
        project = self.harness.project.resolve()
        journal = project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        base = {
            "schema_version": 1,
            "phase": "prepared",
            "before": {
                name: editor_server._caption_preset_snapshot(path)
                for name, path in editor_server._caption_preset_target_paths(project).items()
            },
            "after_sha256": None,
        }
        cases = (
            {**base, "phase": []},
            {**base, "phase": {}},
            {**base, "schema_version": True},
            {
                **base,
                "phase": "committed",
                "after_sha256": {"state": "0" * 64, "manifest": False},
            },
            {
                **base,
                "phase": "committed",
                "before": {
                    **base["before"],
                    "state": {
                        "exists": False,
                        "sha256": "0" * 64,
                        "data_base64": None,
                    },
                },
                "after_sha256": {"state": "0" * 64, "manifest": "1" * 64},
            },
        )
        for payload in cases:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            journal.write_bytes(raw)
            with self.subTest(payload=payload):
                status, blocked = self.request("PUT", "/api/editor-state", {})
                self.assertEqual(status, 503, blocked)
                self.assertEqual(blocked["reason"], "transaction_recovery_required")
                self.assertTrue(blocked["restart_required"])
                self.assertEqual(journal.read_bytes(), raw)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "editor_server.py"),
                        "--project-dir",
                        str(project),
                        "--port",
                        "0",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    env=dict(os.environ),
                )
                self.assertEqual(result.returncode, 3, result)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn(str(journal), result.stderr)
                self.assertEqual(journal.read_bytes(), raw)
        journal.unlink()

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

    def test_prepared_journal_blocks_queued_and_later_project_mutations(self) -> None:
        state, project_revision = self.install_caption()
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

        rollback_started = threading.Event()
        release_rollback = threading.Event()
        put_started = threading.Event()
        apply_result: list[tuple[int, dict]] = []
        put_result: list[tuple[int, dict]] = []
        real_atomic = editor_server.atomic_write_json
        failed_manifest = False

        def fail_manifest_once(path: Path, payload: object) -> None:
            nonlocal failed_manifest
            if Path(path).resolve() == manifest_path.resolve() and not failed_manifest:
                failed_manifest = True
                raise OSError("injected project manifest write failure")
            real_atomic(Path(path), payload)

        def fail_rollback(_path: Path, _raw: bytes) -> None:
            rollback_started.set()
            self.assertTrue(release_rollback.wait(timeout=3))
            raise OSError("injected rollback failure")

        put_state = copy.deepcopy(state)
        target = next(
            item for item in put_state["overlays"]
            if item["id"] == "caption-preset-target"
        )
        target["style"]["color"] = "#FEDCBA"
        put_state["x_expected_revision"] = project_revision

        def apply_request() -> None:
            apply_result.append(self.request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            }))

        def put_request() -> None:
            put_started.set()
            put_result.append(self.request("PUT", "/api/editor-state", put_state))

        with (
            patch.object(editor_server, "atomic_write_json", side_effect=fail_manifest_once),
            patch.object(editor_server, "_atomic_write_exact", side_effect=fail_rollback),
        ):
            apply_thread = threading.Thread(target=apply_request)
            apply_thread.start()
            self.assertTrue(rollback_started.wait(timeout=3))
            put_thread = threading.Thread(target=put_request)
            put_thread.start()
            self.assertTrue(put_started.wait(timeout=1))
            time.sleep(0.05)
            self.assertTrue(put_thread.is_alive())
            release_rollback.set()
            apply_thread.join(timeout=3)
            put_thread.join(timeout=3)
        self.assertFalse(apply_thread.is_alive())
        self.assertFalse(put_thread.is_alive())
        self.assertEqual(apply_result[0][0], 500, apply_result)
        self.assertEqual(apply_result[0][1]["reason"], "transaction_recovery_required")
        self.assertEqual(put_result[0][0], 503, put_result)
        self.assertEqual(put_result[0][1]["reason"], "transaction_recovery_required")
        self.assertTrue(put_result[0][1]["restart_required"])
        self.assertEqual(
            put_result[0][1]["journal"],
            str(editor_server.CAPTION_PRESET_TXN_JOURNAL_REL),
        )
        journal = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        self.assertEqual(json.loads(journal.read_text())["phase"], "prepared")

        for method, path in (
            ("PUT", "/api/edit-decisions"),
            ("PUT", "/api/voice-selection"),
            ("POST", "/api/fx/apply"),
            ("POST", "/api/director"),
            ("POST", "/api/fx-previews/" + "0" * 32 + "/confirm"),
            ("POST", "/api/approve"),
        ):
            with self.subTest(method=method, path=path):
                blocked_status, blocked = self.request(method, path, {})
                self.assertEqual(blocked_status, 503, blocked)
                self.assertEqual(blocked["reason"], "transaction_recovery_required")

        blocked_status, blocked = self.request("GET", "/api/project")
        self.assertEqual(blocked_status, 503, blocked)
        self.assertEqual(blocked["reason"], "transaction_recovery_required")
        blocked_status, blocked = self.request(
            "POST", "/api/fx-previews", self.harness._candidate()
        )
        self.assertEqual(blocked_status, 503, blocked)
        self.assertEqual(blocked["reason"], "transaction_recovery_required")

        # The cross-project preset library is not project authority and remains
        # repairable while this project waits for restart recovery.
        status, library = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, library)
        status, caption = self.request("POST", "/api/style-presets", {
            "expected_library_revision": library["revision"],
            "name": "Recovery-independent caption",
            "kind": "caption",
            "settings": {"style": {"color": "#445566"}},
        })
        self.assertEqual(status, 201, caption)

        editor_server.recover_caption_preset_transaction(self.harness.project)
        self.assertFalse(journal.exists())

    def test_committed_journal_cleanup_never_rolls_back_a_later_http_edit(self) -> None:
        state, project_revision = self.install_caption()
        journal_path = (
            self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        )
        journal = editor_server.begin_caption_preset_transaction(self.harness.project)
        real_unlink = Path.unlink
        failed_cleanup = False

        def fail_cleanup_once(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal failed_cleanup
            if Path(path) == journal_path and not failed_cleanup:
                failed_cleanup = True
                raise OSError("injected committed journal unlink failure")
            real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_cleanup_once):
            editor_server.commit_caption_preset_transaction(
                self.harness.project, journal
            )
        self.assertEqual(json.loads(journal_path.read_text())["phase"], "committed")

        later = copy.deepcopy(state)
        target = next(
            item for item in later["overlays"]
            if item["id"] == "caption-preset-target"
        )
        target["style"]["color"] = "#A1B2C3"
        later["x_expected_revision"] = project_revision
        state_path = self.harness.project / "working/editor_state.json"
        before_blocked_cleanup = state_path.read_bytes()

        def keep_failing_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            if Path(path).resolve() == journal_path.resolve():
                raise OSError("injected persistent committed journal unlink failure")
            real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=keep_failing_cleanup):
            blocked_status, blocked = self.request(
                "PUT", "/api/editor-state", copy.deepcopy(later)
            )
        self.assertEqual(blocked_status, 503, blocked)
        self.assertEqual(blocked["reason"], "transaction_recovery_required")
        self.assertEqual(state_path.read_bytes(), before_blocked_cleanup)
        self.assertEqual(json.loads(journal_path.read_text())["phase"], "committed")

        status, saved = self.request("PUT", "/api/editor-state", later)
        self.assertEqual(status, 200, saved)
        later_bytes = state_path.read_bytes()

        restarted = editor_server.EditorServer(("127.0.0.1", 0), self.harness.project)
        restarted.server_close()
        self.assertFalse(journal_path.exists())
        self.assertEqual(
            (self.harness.project / "working/editor_state.json").read_bytes(),
            later_bytes,
        )

    def test_non_string_animation_and_placement_are_typed_and_corrupt_store_is_preserved(self) -> None:
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        invalid_requests = (
            ("caption", {**caption_settings(), "style": {**caption_settings()["style"], "animation": []}}),
            ("fx", {**fx_settings(), "placement": {}}),
        )
        for kind, settings in invalid_requests:
            with self.subTest(kind=kind):
                status, rejected = self.request("POST", "/api/style-presets", {
                    "expected_library_revision": empty["revision"],
                    "name": f"invalid-{kind}",
                    "kind": kind,
                    "settings": settings,
                })
                self.assertEqual(status, 422, rejected)
                self.assertEqual(rejected["reason"], "settings_invalid")

        self.store_dir.mkdir(parents=True, exist_ok=True)
        for kind, settings in invalid_requests:
            document = {
                "schema_version": 1,
                "presets": [{
                    "id": "preset_" + ("1" if kind == "caption" else "2") * 32,
                    "name": f"corrupt-{kind}",
                    "kind": kind,
                    "settings": settings,
                    "created_at": "2026-11-08T00:00:00+00:00",
                    "updated_at": "2026-11-08T00:00:00+00:00",
                }],
            }
            raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
            (self.store_dir / "presets.json").write_bytes(raw)
            with self.subTest(corrupt_kind=kind):
                status, corrupt = self.request("GET", "/api/style-presets")
                self.assertEqual(status, 409, corrupt)
                self.assertEqual(corrupt["reason"], "store_corrupt")
                self.assertEqual((self.store_dir / "presets.json").read_bytes(), raw)

    def test_studio_pipeline_reservation_serializes_before_caption_apply(self) -> None:
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
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)

        studio = studio_server.StudioServer(
            ("127.0.0.1", 0), self.harness.project.parent, auto_process=True
        )
        studio.editor_servers.append((self.harness.server, self.harness.thread))
        reservation_written = threading.Event()
        release_reservation = threading.Event()
        worker_started = threading.Event()
        release_worker = threading.Event()
        apply_started = threading.Event()
        apply_result: list[tuple[int, dict]] = []
        start_errors: list[BaseException] = []
        real_atomic = studio_server.atomic_write_json

        def blocking_atomic(path: Path, payload: object) -> None:
            real_atomic(path, payload)
            if (
                isinstance(payload, dict)
                and payload.get("state") == "running"
                and payload.get("phase") == "reserved"
            ):
                reservation_written.set()
                self.assertTrue(release_reservation.wait(timeout=3))

        def parked_worker(*_args: object, **_kwargs: object) -> None:
            worker_started.set()
            release_worker.wait(timeout=3)
            with studio.pipeline_lock:
                studio.active_pipeline_project = None

        def start_pipeline() -> None:
            try:
                studio.start_local_pipeline(self.harness.project, "teacher-punch", "")
            except BaseException as exc:
                start_errors.append(exc)

        def apply_preset() -> None:
            apply_started.set()
            apply_result.append(self.request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            }))

        try:
            with (
                patch.object(studio_server, "atomic_write_json", side_effect=blocking_atomic),
                patch.object(studio, "pipeline_worker", side_effect=parked_worker),
            ):
                start_thread = threading.Thread(target=start_pipeline)
                start_thread.start()
                self.assertTrue(reservation_written.wait(timeout=3))
                apply_thread = threading.Thread(target=apply_preset)
                apply_thread.start()
                self.assertTrue(apply_started.wait(timeout=1))
                time.sleep(0.05)
                self.assertTrue(apply_thread.is_alive())
                release_reservation.set()
                start_thread.join(timeout=3)
                self.assertTrue(worker_started.wait(timeout=3))
                status, pipeline = self.request("GET", "/api/pipeline-status")
                self.assertEqual(status, 200, pipeline)
                self.assertEqual(pipeline["state"], "running")
                apply_thread.join(timeout=3)
            self.assertEqual(start_errors, [])
            self.assertEqual(apply_result[0][0], 409, apply_result)
            self.assertEqual(apply_result[0][1]["reason"], "project_busy")
            status_path = self.harness.project / "working/pipeline_status.json"
            reservation_bytes = status_path.read_bytes()
            studio.start_local_pipeline(self.harness.project, "teacher-punch", "")
            self.assertEqual(status_path.read_bytes(), reservation_bytes)
            self.assertEqual(len(studio.pipeline_threads), 1)
            self.assertFalse(
                (self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL).exists()
            )
        finally:
            release_reservation.set()
            release_worker.set()
            for thread in list(studio.pipeline_threads):
                thread.join(timeout=3)
            studio.pipeline_threads.clear()
            studio.editor_servers.clear()
            studio.server_close()

    def test_cover_is_blocked_before_subprocess_when_recovery_journal_is_pending(self) -> None:
        cover = self.harness.project / "renders/cover.png"
        cover.write_bytes(b"existing-cover")
        journal = editor_server.begin_caption_preset_transaction(self.harness.project)
        self.assertEqual(journal["phase"], "prepared")

        def would_replace_cover(command: list[str], **_kwargs: object):
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"replacement-cover")
            return subprocess.CompletedProcess(command, 0, "", "")

        try:
            with patch.object(
                editor_server.subprocess, "run", side_effect=would_replace_cover
            ) as render:
                status, rejected = self.request("POST", "/api/cover", {
                    "platform_id": "instagram-reels",
                    "time": 0,
                    "text": "must not render",
                })
            self.assertEqual(status, 503, rejected)
            self.assertEqual(rejected["reason"], "transaction_recovery_required")
            self.assertEqual(cover.read_bytes(), b"existing-cover")
            render.assert_not_called()
        finally:
            editor_server.recover_caption_preset_transaction(self.harness.project)

    def test_stale_pipeline_status_does_not_block_caption_apply_without_live_writer(self) -> None:
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
        status_path = self.harness.project / "working/pipeline_status.json"
        status_path.write_text(
            json.dumps({
                "schema_version": 1,
                "state": "running",
                "phase": "transcribe",
                "message": "stale after crashed Studio process",
            }),
            encoding="utf-8",
        )

        status, project = self.request("GET", "/api/project")
        self.assertEqual(status, 200, project)
        self.assertEqual(project["pipeline_status"]["state"], "needs_attention")
        self.assertEqual(
            project["pipeline_status"]["error_code"], "pipeline_interrupted"
        )
        status, pipeline = self.request("GET", "/api/pipeline-status")
        self.assertEqual(status, 200, pipeline)
        self.assertEqual(pipeline["state"], "needs_attention")
        self.assertEqual(pipeline["error_code"], "pipeline_interrupted")
        base["expected_revision"] = project["state"]["revision"]
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)

        status, applied = self.request("POST", "/api/style-presets/caption-apply", {
            **base,
            "plan_digest": plan["plan_digest"],
        })
        self.assertEqual(status, 200, applied)
        self.assertEqual(applied["applied_to"], ["caption-preset-target"])
        self.assertFalse(
            (self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL).exists()
        )

    def test_single_render_journal_gate_precedes_all_publication(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            previous = {
                "schema_version": 1,
                "status": "pass",
                "sentinel": "single-publication-must-survive",
            }
            case.write_json("working/latest_final_qa.json", previous)
            state, clip_ids = case.approve_batch_prerequisites()
            manifest_path = case.project / "project.json"
            manifest_before = manifest_path.read_bytes()
            real_gate = editor_server.ensure_caption_preset_mutation_ready
            calls = 0

            def fail_publication_gate(project: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise editor_server.CaptionPresetRecoveryError(
                        "injected pending recovery at publication"
                    )
                real_gate(project)

            fake_run = case.fake_batch_subprocess()
            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server,
                    "ensure_caption_preset_mutation_ready",
                    side_effect=fail_publication_gate,
                ),
            ):
                status, accepted = case.json_request("POST", "/api/render", {
                    "quality": "final",
                    "clip_id": clip_ids[0],
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed", failed)
            self.assertTrue(failed["previous_publication_preserved"])
            case.assert_single_route_published_nothing(previous)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(
                sorted((case.project / "working/render_receipts").glob("*.json")),
                [],
            )
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_second_member_gate_rolls_back_first_member_auxiliary_records(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            previous = {
                "schema_version": 1,
                "status": "pass",
                "sentinel": "batch-publication-must-survive",
            }
            case.write_json("working/latest_final_qa.json", previous)
            state, _clip_ids = case.approve_batch_prerequisites()
            manifest_path = case.project / "project.json"
            manifest_before = manifest_path.read_bytes()
            real_gate = editor_server.ensure_caption_preset_mutation_ready
            calls = 0

            def fail_member_gate(project: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    editor_server.begin_caption_preset_transaction(project)
                real_gate(project)

            fake_run = case.fake_batch_subprocess()
            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server,
                    "ensure_caption_preset_mutation_ready",
                    side_effect=fail_member_gate,
                ),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed", failed)
            case.assert_batch_published_nothing(previous)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(
                sorted((case.project / "working/render_receipts").glob("*.json")),
                [],
            )
            self.assertEqual(
                sorted((case.project / "working/delivery_qa").glob("render_*.json")),
                [],
            )
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_auxiliary_rollback_restores_preexisting_exact_bytes(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            previous = {"schema_version": 1, "status": "pass", "sentinel": "prior"}
            case.write_json("working/latest_final_qa.json", previous)
            state, _clip_ids = case.approve_batch_prerequisites()
            real_gate = editor_server.ensure_caption_preset_mutation_ready
            real_publish = editor_server.delivery_envelope.publish_direct_delivery
            calls = 0
            prior: dict[Path, bytes] = {}

            def fail_second_member(project: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    editor_server.begin_caption_preset_transaction(project)
                real_gate(project)

            def seed_prior_records(*args: object, **kwargs: object):
                publication = real_publish(*args, **kwargs)
                if not prior and isinstance(
                    publication, editor_server.delivery_envelope.DeferredPublication
                ):
                    for directory, payload in (
                        ("working/render_receipts", b"prior-receipt-bytes"),
                        ("working/delivery_qa", b"prior-pass-qa-bytes"),
                    ):
                        path = case.project / directory / f"{publication.render_id}.json"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(payload)
                        prior[path] = payload
                return publication

            fake_run = case.fake_batch_subprocess()
            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server,
                    "ensure_caption_preset_mutation_ready",
                    side_effect=fail_second_member,
                ),
                patch.object(
                    editor_server.delivery_envelope,
                    "publish_direct_delivery",
                    side_effect=seed_prior_records,
                ),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed", failed)
            self.assertTrue(failed["previous_latest_preserved"], failed)
            self.assertEqual({path: path.read_bytes() for path in prior}, prior)
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_auxiliary_cas_mismatch_is_reported_and_not_deleted(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            state, _clip_ids = case.approve_batch_prerequisites()
            real_gate = editor_server.ensure_caption_preset_mutation_ready
            calls = 0
            tampered = b"changed-after-member-publication"

            def tamper_then_fail_second_member(project: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    receipts = sorted(
                        (case.project / "working/render_receipts").glob("render_*.json")
                    )
                    self.assertEqual(len(receipts), 1)
                    receipts[0].write_bytes(tampered)
                    editor_server.begin_caption_preset_transaction(project)
                real_gate(project)

            fake_run = case.fake_batch_subprocess()
            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server,
                    "ensure_caption_preset_mutation_ready",
                    side_effect=tamper_then_fail_second_member,
                ),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed_unclean", failed)
            self.assertFalse(failed["previous_latest_preserved"])
            self.assertTrue(failed["rollback_errors"])
            self.assertTrue(failed["leaked_paths"])
            leaked_receipt = Path(failed["leaked_paths"][0])
            self.assertEqual(leaked_receipt.read_bytes(), tampered)
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_member_auxiliary_write_failure_rolls_back_both_records(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            previous = {"schema_version": 1, "status": "pass", "sentinel": "prior"}
            case.write_json("working/latest_final_qa.json", previous)
            state, _clip_ids = case.approve_batch_prerequisites()
            real_atomic = editor_server.atomic_write_json
            failed_once = False

            def fail_after_member_qa_write(path: Path, payload: object) -> None:
                nonlocal failed_once
                real_atomic(path, payload)
                if (
                    not failed_once
                    and path.parent.name == "delivery_qa"
                    and path.name.startswith("render_")
                ):
                    failed_once = True
                    raise OSError("injected failure after member QA replacement")

            fake_run = case.fake_batch_subprocess()
            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server,
                    "atomic_write_json",
                    side_effect=fail_after_member_qa_write,
                ),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertTrue(failed_once)
            self.assertEqual(failed["state"], "failed", failed)
            case.assert_batch_published_nothing(previous)
            self.assertEqual(
                sorted((case.project / "working/render_receipts").glob("render_*.json")),
                [],
            )
            self.assertEqual(
                sorted((case.project / "working/delivery_qa").glob("render_*.json")),
                [],
            )
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_auxiliary_restore_and_unlink_io_failures_are_unclean(self) -> None:
        from tests.test_editor_server import EditorServerTests

        for mode in ("restore", "unlink"):
            with self.subTest(mode=mode):
                case = EditorServerTests(methodName="runTest")
                case.setUp()
                try:
                    state, _clip_ids = case.approve_batch_prerequisites()
                    real_gate = editor_server.ensure_caption_preset_mutation_ready
                    real_publish = editor_server.delivery_envelope.publish_direct_delivery
                    real_exact = editor_server._atomic_write_exact
                    real_unlink = Path.unlink
                    calls = 0
                    prior_path: Path | None = None

                    def fail_second_member(project: Path) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == 3:
                            editor_server.begin_caption_preset_transaction(project)
                        real_gate(project)

                    def seed_prior(*args: object, **kwargs: object):
                        nonlocal prior_path
                        publication = real_publish(*args, **kwargs)
                        if (
                            mode == "restore"
                            and prior_path is None
                            and isinstance(
                                publication,
                                editor_server.delivery_envelope.DeferredPublication,
                            )
                        ):
                            prior_path = (
                                case.project
                                / "working/render_receipts"
                                / f"{publication.render_id}.json"
                            )
                            prior_path.parent.mkdir(parents=True, exist_ok=True)
                            prior_path.write_bytes(b"prior-receipt")
                        return publication

                    def fail_exact(path: Path, raw: bytes) -> None:
                        if mode == "restore":
                            raise OSError("injected auxiliary restore failure")
                        real_exact(path, raw)

                    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
                        if (
                            mode == "unlink"
                            and path.parent.name == "render_receipts"
                            and path.name.startswith("render_")
                        ):
                            raise OSError("injected auxiliary unlink failure")
                        real_unlink(path, *args, **kwargs)

                    with (
                        patch.object(
                            editor_server.subprocess,
                            "run",
                            side_effect=case.fake_batch_subprocess(),
                        ),
                        patch.object(
                            editor_server, "ffprobe_has_visual_stream", return_value=True
                        ),
                        patch.object(
                            editor_server,
                            "ensure_caption_preset_mutation_ready",
                            side_effect=fail_second_member,
                        ),
                        patch.object(
                            editor_server.delivery_envelope,
                            "publish_direct_delivery",
                            side_effect=seed_prior,
                        ),
                        patch.object(editor_server, "_atomic_write_exact", side_effect=fail_exact),
                        patch.object(Path, "unlink", new=fail_unlink),
                    ):
                        status, accepted = case.json_request("POST", "/api/render-batch", {
                            "quality": "final",
                            "expected_revision": state["revision"],
                        })
                        self.assertEqual(status, 202, accepted)
                        failed = case.wait_for_render_terminal()
                    self.assertEqual(failed["state"], "failed_unclean", failed)
                    self.assertFalse(failed["previous_latest_preserved"])
                    self.assertTrue(failed["rollback_errors"])
                    self.assertTrue(failed["leaked_paths"])
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_batch_post_commit_failure_keeps_member_auxiliary_records(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            state, _clip_ids = case.approve_batch_prerequisites()
            real_atomic = editor_server.atomic_write_json

            def fail_after_latest(path: Path, payload: object) -> None:
                real_atomic(path, payload)
                if path.name == "latest_final_qa.json":
                    raise OSError("injected post-commit pointer failure")

            with (
                patch.object(
                    editor_server.subprocess,
                    "run",
                    side_effect=case.fake_batch_subprocess(),
                ),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(editor_server, "atomic_write_json", side_effect=fail_after_latest),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed", failed)
            self.assertNotIn("leaked_paths", failed)
            member_receipts = sorted(
                (case.project / "working/render_receipts").glob("render_*.json")
            )
            member_qa = sorted(
                (case.project / "working/delivery_qa").glob("render_*.json")
            )
            self.assertEqual((len(member_receipts), len(member_qa)), (2, 2))
            for path in (*member_receipts, *member_qa):
                self.assertTrue(path.read_bytes())
        finally:
            case.tearDown()
            case.doCleanups()

    def test_batch_cleanup_failure_keeps_original_root_cause_and_classification(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            state, _clip_ids = case.approve_batch_prerequisites()
            real_atomic = editor_server.atomic_write_json
            real_unlink = Path.unlink
            failed_write = False

            def fail_with_qa_root_cause(path: Path, payload: object) -> None:
                nonlocal failed_write
                real_atomic(path, payload)
                if (
                    not failed_write
                    and path.parent.name == "delivery_qa"
                    and path.name.startswith("render_")
                ):
                    failed_write = True
                    raise RuntimeError("delivery QA failed: injected root cause")

            def fail_receipt_cleanup(path: Path, *args: object, **kwargs: object) -> None:
                if (
                    failed_write
                    and path.parent.name == "render_receipts"
                    and path.name.startswith("render_")
                ):
                    raise OSError("injected cleanup failure")
                real_unlink(path, *args, **kwargs)

            with (
                patch.object(
                    editor_server.subprocess,
                    "run",
                    side_effect=case.fake_batch_subprocess(),
                ),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(
                    editor_server, "atomic_write_json", side_effect=fail_with_qa_root_cause
                ),
                patch.object(Path, "unlink", new=fail_receipt_cleanup),
            ):
                status, accepted = case.json_request("POST", "/api/render-batch", {
                    "quality": "final",
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertTrue(failed_write)
            self.assertEqual(failed["state"], "failed_unclean", failed)
            self.assertIn("delivery QA failed: injected root cause", failed["message"])
            self.assertTrue(failed["rollback_errors"])
        finally:
            case.tearDown()
            case.doCleanups()

    def test_preset_store_io_errors_are_typed_and_preserve_document(self) -> None:
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        created = self.create_caption_preset(empty["revision"])
        preset = created["preset"]
        store_path = self.harness.server.style_preset_store.path

        cases = (
            ("GET", "/api/style-presets", None, style_presets.os, "read", OSError("read EIO")),
            (
                "POST",
                "/api/style-presets",
                {
                    "expected_library_revision": created["revision"],
                    "name": "disk-full-create",
                    "kind": "caption",
                    "settings": {"style": {"color": "#123456"}},
                },
                style_presets.os,
                "write",
                OSError("write ENOSPC"),
            ),
            (
                "PUT",
                f"/api/style-presets/{preset['id']}",
                {
                    "expected_library_revision": created["revision"],
                    "name": "fsync-update",
                    "kind": "caption",
                    "settings": {"style": {"color": "#654321"}},
                },
                style_presets.os,
                "fsync",
                OSError("fsync EIO"),
            ),
            (
                "DELETE",
                f"/api/style-presets/{preset['id']}",
                {"expected_library_revision": created["revision"]},
                style_presets.os,
                "replace",
                PermissionError("replace EACCES"),
            ),
        )
        for method, path, payload, owner, attribute, failure in cases:
            with self.subTest(method=method, fault=attribute):
                before = store_path.read_bytes()
                with patch.object(owner, attribute, side_effect=failure):
                    status, rejected = self.request(method, path, payload)
                self.assertEqual(status, 503, rejected)
                self.assertEqual(rejected["reason"], "store_io_error")
                self.assertEqual(store_path.read_bytes(), before)
                status, healthy = self.request("GET", "/api/style-presets")
                self.assertEqual(status, 200, healthy)
                self.assertEqual(healthy["revision"], created["revision"])

    def test_preset_store_postreplace_fsync_error_requires_reload(self) -> None:
        status, initial = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, initial)
        real_fsync = style_presets.os.fsync
        injected = False

        def fail_directory_fsync(descriptor: int) -> None:
            nonlocal injected
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                injected = True
                raise OSError("injected post-replace directory fsync failure")
            real_fsync(descriptor)

        with patch.object(style_presets.os, "fsync", side_effect=fail_directory_fsync):
            status, uncertain = self.request("POST", "/api/style-presets", {
                "expected_library_revision": initial["revision"],
                "name": "post-replace-visible",
                "kind": "caption",
                "settings": {"style": {"color": "#123456"}},
            })
        self.assertTrue(injected)
        self.assertEqual(status, 503, uncertain)
        self.assertEqual(uncertain["reason"], "store_io_error")
        self.assertIn("重新載入清單確認", uncertain["error"])
        self.assertNotIn("原檔未變更", uncertain["error"])

        status, current = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, current)
        self.assertNotEqual(current["revision"], initial["revision"])
        self.assertEqual(
            [preset["name"] for preset in current["presets"]],
            ["post-replace-visible"],
        )

        stale_payload = {
            "expected_library_revision": initial["revision"],
            "name": "stale-cas",
            "kind": "caption",
            "settings": {"style": {"color": "#654321"}},
        }
        status, stale = self.request("POST", "/api/style-presets", stale_payload)
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["reason"], "library_revision_conflict")
        stale_payload["expected_library_revision"] = current["revision"]
        stale_payload["name"] = "current-cas"
        status, created = self.request("POST", "/api/style-presets", stale_payload)
        self.assertEqual(status, 201, created)
        self.assertNotEqual(created["revision"], current["revision"])

    def test_caption_apply_store_read_error_is_typed_and_writes_nothing(self) -> None:
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
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        paths = (
            self.harness.server.style_preset_store.path,
            self.harness.project / "working/editor_state.json",
            self.harness.project / "project.json",
        )
        before = {path: path.read_bytes() for path in paths}
        journal = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        self.assertFalse(journal.exists())

        with patch.object(
            style_presets.os,
            "read",
            side_effect=OSError("injected locked collection read EIO"),
        ):
            status, rejected = self.request("POST", "/api/style-presets/caption-apply", {
                **base,
                "plan_digest": plan["plan_digest"],
            })
        self.assertEqual(status, 503, rejected)
        self.assertEqual(rejected["reason"], "store_io_error")
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertFalse(journal.exists())
        status, current = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, current)
        self.assertEqual(current["revision"], created["revision"])

    def test_store_operational_errno_is_503_across_http_boundaries(self) -> None:
        tracker = StylePresetOSProxy()
        os_proxy = patch.object(style_presets, "os", tracker)
        os_proxy.start()
        self.addCleanup(os_proxy.stop)
        state_path = self.harness.project / "working/editor_state.json"
        manifest_path = self.harness.project / "project.json"
        journal_path = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        store_path = self.harness.server.style_preset_store.path

        def authority_snapshot() -> dict[Path, bytes | None]:
            return {
                path: path.read_bytes() if path.exists() else None
                for path in (store_path, state_path, manifest_path, journal_path)
            }

        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        real_mkdir = style_presets.os.mkdir
        for failure_errno in (errno.EACCES, errno.ENOSPC, errno.EIO, errno.EROFS):
            with self.subTest(boundary="mkdir", errno=failure_errno):
                before = authority_snapshot()

                def fail_store_mkdir(path, *args, **kwargs):
                    if path == self.store_dir.name:
                        raise OSError(failure_errno, "injected store mkdir failure")
                    return real_mkdir(path, *args, **kwargs)

                with patch.object(style_presets.os, "mkdir", side_effect=fail_store_mkdir):
                    status, rejected = self.request("POST", "/api/style-presets", {
                        "expected_library_revision": empty["revision"],
                        "name": f"mkdir-{failure_errno}",
                        "kind": "caption",
                        "settings": {"style": {"color": "#123456"}},
                    })
                self.assertEqual(status, 503, rejected)
                self.assertEqual(rejected["reason"], "store_io_error")
                self.assertEqual(authority_snapshot(), before)
                self.assertEqual(tracker.live_fds, {})

        _state, project_revision = self.install_caption()
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
        held_fd = style_presets.os.open(
            self.store_dir,
            style_presets.os.O_RDONLY | getattr(style_presets.os, "O_DIRECTORY", 0),
        )
        self.assertIn(held_fd, tracker.live_fds)
        style_presets.os.close(held_fd)
        self.assertEqual(tracker.live_fds, {})
        real_open = style_presets.os.open

        for endpoint, request, component_errno in (
            ("collection", ("GET", "/api/style-presets", None), errno.EACCES),
            ("caption-plan", ("POST", "/api/style-presets/caption-plan", base), errno.EIO),
        ):
            with self.subTest(boundary=endpoint, errno=component_errno):
                before = authority_snapshot()

                def fail_store_component(path, flags, *args, **kwargs):
                    if path == self.store_dir.name:
                        raise OSError(component_errno, "injected store component failure")
                    return real_open(path, flags, *args, **kwargs)

                with patch.object(style_presets.os, "open", side_effect=fail_store_component):
                    status, rejected = self.request(*request)
                self.assertEqual(status, 503, rejected)
                self.assertEqual(rejected["reason"], "store_io_error")
                self.assertEqual(authority_snapshot(), before)
                self.assertEqual(tracker.live_fds, {})

        for boundary, errnos in (
            ("caption-apply-lock", (errno.EACCES, errno.ENOSPC, errno.EIO, errno.EROFS)),
            ("mutation-lock", (errno.EACCES, errno.ENOSPC, errno.EIO, errno.EROFS)),
        ):
            for failure_errno in errnos:
                with self.subTest(boundary=boundary, errno=failure_errno):
                    before = authority_snapshot()

                    def fail_lock_open(path, flags, *args, **kwargs):
                        if path == ".presets.lock":
                            raise OSError(failure_errno, "injected preset lock failure")
                        return real_open(path, flags, *args, **kwargs)

                    with patch.object(style_presets.os, "open", side_effect=fail_lock_open):
                        if boundary == "caption-apply-lock":
                            status, rejected = self.request(
                                "POST",
                                "/api/style-presets/caption-apply",
                                {**base, "plan_digest": plan["plan_digest"]},
                            )
                        else:
                            status, rejected = self.request("POST", "/api/style-presets", {
                                "expected_library_revision": created["revision"],
                                "name": f"lock-{failure_errno}",
                                "kind": "caption",
                                "settings": {"style": {"color": "#654321"}},
                            })
                    self.assertEqual(status, 503, rejected)
                    self.assertEqual(rejected["reason"], "store_io_error")
                    self.assertEqual(authority_snapshot(), before)
                    self.assertEqual(tracker.live_fds, {})

        lock_path = self.harness.server.style_preset_store.lock_path
        lock_path.unlink()
        lock_path.mkdir()
        try:
            for boundary in ("caption-apply-special-lock", "mutation-special-lock"):
                with self.subTest(boundary=boundary):
                    before = authority_snapshot()
                    if boundary == "caption-apply-special-lock":
                        status, rejected = self.request(
                            "POST",
                            "/api/style-presets/caption-apply",
                            {**base, "plan_digest": plan["plan_digest"]},
                        )
                    else:
                        status, rejected = self.request("POST", "/api/style-presets", {
                            "expected_library_revision": created["revision"],
                            "name": "special-lock",
                            "kind": "caption",
                            "settings": {"style": {"color": "#654321"}},
                        })
                    self.assertEqual(status, 409, rejected)
                    self.assertEqual(rejected["reason"], "store_unsafe")
                    self.assertEqual(authority_snapshot(), before)
                    self.assertEqual(tracker.live_fds, {})
        finally:
            lock_path.rmdir()

    def test_store_fifo_is_rejected_without_waiting_for_writer(self) -> None:
        tracker = StylePresetOSProxy()
        os_proxy = patch.object(style_presets, "os", tracker)
        os_proxy.start()
        self.addCleanup(os_proxy.stop)
        self.store_dir.mkdir(parents=True)
        store_path = self.harness.server.style_preset_store.path
        os.mkfifo(store_path, mode=0o600)
        started = time.monotonic()
        status, rejected = self.request("GET", "/api/style-presets")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["reason"], "store_unsafe")
        self.assertTrue(store_path.exists())
        self.assertTrue(stat.S_ISFIFO(store_path.stat().st_mode))
        self.assertEqual(tracker.live_fds, {})

        store_path.unlink()
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        created = self.create_caption_preset(empty["revision"])
        status, current = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, current)
        self.assertEqual(current["revision"], created["revision"])
        self.assertEqual(len(current["presets"]), 1)
        self.assertEqual(tracker.live_fds, {})

    def test_store_socket_is_unsafe_and_fd_tracker_has_positive_control(self) -> None:
        original_store = self.harness.server.style_preset_store
        with short_socket_temporary_directory("p3-store-socket-") as short_store:
            self.harness.server.style_preset_store = style_presets.StylePresetStore(short_store)
            tracker = StylePresetOSProxy()
            bound = None
            try:
                with patch.object(style_presets, "os", tracker):
                    held_fd = style_presets.os.open(
                        short_store,
                        style_presets.os.O_RDONLY
                        | getattr(style_presets.os, "O_DIRECTORY", 0),
                    )
                    self.assertIn(held_fd, tracker.live_fds)
                    style_presets.os.close(held_fd)
                    self.assertEqual(tracker.live_fds, {})

                    store_path = self.harness.server.style_preset_store.path
                    state_path = self.harness.project / "working/editor_state.json"
                    manifest_path = self.harness.project / "project.json"
                    before = (state_path.read_bytes(), manifest_path.read_bytes())
                    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    bound.bind(str(store_path))
                    status, rejected = self.request("GET", "/api/style-presets")
                    self.assertEqual(status, 409, rejected)
                    self.assertEqual(rejected["reason"], "store_unsafe")
                    self.assertTrue(stat.S_ISSOCK(store_path.lstat().st_mode))
                    self.assertEqual(
                        (state_path.read_bytes(), manifest_path.read_bytes()),
                        before,
                    )
                    self.assertEqual(tracker.live_fds, {})

                    bound.close()
                    bound = None
                    store_path.unlink()
                    status, empty = self.request("GET", "/api/style-presets")
                    self.assertEqual(status, 200, empty)
                    created = self.create_caption_preset(empty["revision"])
                    status, current = self.request("GET", "/api/style-presets")
                    self.assertEqual(status, 200, current)
                    self.assertEqual(current["revision"], created["revision"])
                    self.assertEqual(tracker.live_fds, {})
            finally:
                if bound is not None:
                    bound.close()
                socket_path = self.harness.server.style_preset_store.path
                if socket_path.exists() or socket_path.is_socket():
                    socket_path.unlink()
                self.harness.server.style_preset_store = original_store

    def test_lock_socket_is_unsafe_for_caption_apply_and_mutation(self) -> None:
        original_store = self.harness.server.style_preset_store
        with short_socket_temporary_directory("p3-lock-socket-") as short_store:
            self.harness.server.style_preset_store = style_presets.StylePresetStore(short_store)
            tracker = StylePresetOSProxy()
            bound = None
            try:
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
                status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
                self.assertEqual(status, 200, plan)
                lock_path = self.harness.server.style_preset_store.lock_path
                lock_path.unlink()
                bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                bound.bind(str(lock_path))
                store_path = self.harness.server.style_preset_store.path
                state_path = self.harness.project / "working/editor_state.json"
                manifest_path = self.harness.project / "project.json"
                journal_path = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
                before = {
                    "store": store_path.read_bytes(),
                    "state": state_path.read_bytes(),
                    "manifest": manifest_path.read_bytes(),
                    "journal": journal_path.read_bytes() if journal_path.exists() else None,
                }
                with patch.object(style_presets, "os", tracker):
                    status, apply_rejected = self.request(
                        "POST",
                        "/api/style-presets/caption-apply",
                        {**base, "plan_digest": plan["plan_digest"]},
                    )
                    self.assertEqual(status, 409, apply_rejected)
                    self.assertEqual(apply_rejected["reason"], "store_unsafe")
                    self.assertEqual(tracker.live_fds, {})
                    status, mutation_rejected = self.request("POST", "/api/style-presets", {
                        "expected_library_revision": created["revision"],
                        "name": "lock-socket-mutation",
                        "kind": "caption",
                        "settings": {"style": {"color": "#123456"}},
                    })
                    self.assertEqual(status, 409, mutation_rejected)
                    self.assertEqual(mutation_rejected["reason"], "store_unsafe")
                    self.assertEqual(tracker.live_fds, {})
                self.assertTrue(stat.S_ISSOCK(lock_path.lstat().st_mode))
                self.assertEqual(store_path.read_bytes(), before["store"])
                self.assertEqual(state_path.read_bytes(), before["state"])
                self.assertEqual(manifest_path.read_bytes(), before["manifest"])
                self.assertEqual(
                    journal_path.read_bytes() if journal_path.exists() else None,
                    before["journal"],
                )
            finally:
                if bound is not None:
                    bound.close()
                lock_path = self.harness.server.style_preset_store.lock_path
                if lock_path.exists() or lock_path.is_socket():
                    lock_path.unlink()
                self.harness.server.style_preset_store = original_store

    def test_extreme_corrupt_json_is_409_and_preserved_across_http_boundaries(self) -> None:
        tracker = StylePresetOSProxy()
        os_proxy = patch.object(style_presets, "os", tracker)
        os_proxy.start()
        self.addCleanup(os_proxy.stop)
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
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        store_path = self.harness.server.style_preset_store.path
        valid_raw = store_path.read_bytes()
        state_path = self.harness.project / "working/editor_state.json"
        manifest_path = self.harness.project / "project.json"
        journal_path = self.harness.project / editor_server.CAPTION_PRESET_TXN_JOURNAL_REL
        timestamp = "2026-11-08T00:00:00Z"
        surrogate_document = {
            "schema_version": 1,
            "presets": [{
                "id": "preset_" + "1" * 32,
                "name": "\ud800",
                "kind": "caption",
                "settings": {"style": {"color": "#123456"}, "font": None},
                "created_at": timestamp,
                "updated_at": timestamp,
            }],
        }
        deep_record_document = {
            "schema_version": 1,
            "presets": [{
                "id": "preset_" + "2" * 32,
                "name": "deep-record",
                "kind": "caption",
                "settings": {
                    "style": {"color": [[[[[[[[[["bad"]]]]]]]]]]},
                    "font": None,
                },
                "created_at": timestamp,
                "updated_at": timestamp,
            }],
        }
        corrupt_cases = {
            "big-integer": b'{"schema_version":1,"presets":[' + b"9" * 4301 + b"]}",
            "deep-json": ("[" * 200000 + "0" + "]" * 200000).encode("ascii"),
            "lone-surrogate": json.dumps(
                surrogate_document, ensure_ascii=True
            ).encode("ascii"),
            "deep-record": json.dumps(deep_record_document).encode("utf-8"),
            "bool-schema": b'{"schema_version":true,"presets":[]}',
        }

        requests = (
            ("GET", "/api/style-presets", None),
            ("POST", "/api/style-presets/caption-plan", base),
            (
                "POST",
                "/api/style-presets/caption-apply",
                {**base, "plan_digest": plan["plan_digest"]},
            ),
            (
                "POST",
                "/api/style-presets",
                {
                    "expected_library_revision": created["revision"],
                    "name": "corrupt-mutation",
                    "kind": "caption",
                    "settings": {"style": {"color": "#123456"}},
                },
            ),
        )
        for label, raw in corrupt_cases.items():
            store_path.write_bytes(raw)
            before = {
                "store": store_path.read_bytes(),
                "state": state_path.read_bytes(),
                "manifest": manifest_path.read_bytes(),
                "journal": journal_path.read_bytes() if journal_path.exists() else None,
            }
            for method, path, body in requests:
                with self.subTest(case=label, endpoint=path):
                    status, rejected = self.request(method, path, body)
                    self.assertEqual(status, 409, rejected)
                    self.assertEqual(rejected["reason"], "store_corrupt")
                    self.assertEqual(store_path.read_bytes(), before["store"])
                    self.assertEqual(state_path.read_bytes(), before["state"])
                    self.assertEqual(manifest_path.read_bytes(), before["manifest"])
                    self.assertEqual(
                        journal_path.read_bytes() if journal_path.exists() else None,
                        before["journal"],
                    )
                    self.assertEqual(tracker.live_fds, {})

        oversized = b"x" * (style_presets.MAX_STORE_BYTES + 1)
        store_path.write_bytes(oversized)
        status, rejected = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["reason"], "store_corrupt")
        self.assertEqual(store_path.read_bytes(), oversized)
        self.assertEqual(tracker.live_fds, {})

        store_path.write_bytes(valid_raw)
        status, current = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, current)
        self.assertEqual(current["revision"], created["revision"])
        self.assertEqual(tracker.live_fds, {})

    def test_preview_failure_after_replace_reports_publication_truthfully(self) -> None:
        from tests.test_editor_server import EditorServerTests

        case = EditorServerTests(methodName="runTest")
        case.setUp()
        try:
            state, clip_ids = case.approve_batch_prerequisites()
            fake_run = case.fake_batch_subprocess()
            real_atomic = editor_server.atomic_write_json

            def fail_receipt(path: Path, payload: object) -> None:
                if "working/render_receipts" in str(path):
                    raise OSError("injected preview receipt failure")
                real_atomic(path, payload)

            with (
                patch.object(editor_server.subprocess, "run", side_effect=fake_run),
                patch.object(editor_server, "ffprobe_has_visual_stream", return_value=True),
                patch.object(editor_server, "atomic_write_json", side_effect=fail_receipt),
            ):
                status, accepted = case.json_request("POST", "/api/render", {
                    "quality": "preview",
                    "clip_id": clip_ids[0],
                    "expected_revision": state["revision"],
                })
                self.assertEqual(status, 202, accepted)
                failed = case.wait_for_render_terminal()
            self.assertEqual(failed["state"], "failed", failed)
            self.assertFalse(failed["previous_publication_preserved"])
            self.assertEqual(
                len(list((case.project / "renders").glob("*-preview.mp4"))), 1
            )
        finally:
            case.tearDown()
            case.doCleanups()

    def test_studio_pipeline_start_failure_clears_reservation(self) -> None:
        studio = studio_server.StudioServer(
            ("127.0.0.1", 0), self.harness.project.parent, auto_process=True
        )
        studio.editor_servers.append((self.harness.server, self.harness.thread))
        try:
            with patch.object(
                studio_server.threading.Thread,
                "start",
                side_effect=RuntimeError("injected thread start failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected thread start failure"):
                    studio.start_local_pipeline(
                        self.harness.project, "teacher-punch", ""
                    )
            self.assertIsNone(studio.active_pipeline_project)
            self.assertIsNone(self.harness.server.pipeline_active_owner)
            self.assertEqual(studio.pipeline_threads, [])
            status = json.loads(
                (self.harness.project / "working/pipeline_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_code"], "pipeline_start_failed")

            journal = editor_server.begin_caption_preset_transaction(
                self.harness.project
            )
            self.assertEqual(journal["phase"], "prepared")
            status_before = (
                self.harness.project / "working/pipeline_status.json"
            ).read_bytes()
            with self.assertRaises(editor_server.CaptionPresetRecoveryError):
                studio.start_local_pipeline(
                    self.harness.project, "teacher-punch", ""
                )
            self.assertIsNone(studio.active_pipeline_project)
            self.assertEqual(
                (self.harness.project / "working/pipeline_status.json").read_bytes(),
                status_before,
            )
            editor_server.recover_caption_preset_transaction(self.harness.project)
        finally:
            studio.editor_servers.clear()
            studio.server_close()

    def test_studio_pipeline_completion_clears_live_project_owner(self) -> None:
        studio = studio_server.StudioServer(
            ("127.0.0.1", 0), self.harness.project.parent, auto_process=True
        )
        studio.editor_servers.append((self.harness.server, self.harness.thread))
        completed = subprocess.CompletedProcess([], 0, "", "")
        try:
            with (
                patch.object(studio, "run_pipeline_command", return_value=completed),
                patch.object(studio, "transcript_has_speech", return_value=True),
                patch.object(studio, "highlight_review_message", return_value="done"),
            ):
                studio.start_local_pipeline(self.harness.project, "teacher-punch", "")
                self.assertIsNotNone(self.harness.server.pipeline_active_owner)
                for thread in list(studio.pipeline_threads):
                    thread.join(timeout=3)
            self.assertIsNone(studio.active_pipeline_project)
            self.assertIsNone(self.harness.server.pipeline_active_owner)
        finally:
            studio.pipeline_threads.clear()
            studio.editor_servers.clear()
            studio.server_close()

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

    def test_verified_font_can_replace_invalid_old_font_without_using_fallback(self) -> None:
        valid_item, _receipt = publish_verified_font(self.harness.project, "Replacement")
        fonts = asset_registry.list_project_fonts(self.harness.project)
        descriptor = {
            key: fonts[0][key] for key in ("sha256", "family", "style", "weight")
        }
        state, _revision = self.install_caption()
        state_path = self.harness.project / "working/editor_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        target = next(
            item for item in state["overlays"] if item["id"] == "caption-preset-target"
        )
        invalid_old_id = "font-google-fonts-" + "0" * 16 + "-" + "1" * 16
        target["style"]["font_asset_id"] = invalid_old_id
        target["style"]["font_family"] = "Unverified fallback must not survive"
        state["revision"] = editor_server.editor_state_revision(state)
        state_path.write_text(json.dumps(state), encoding="utf-8")

        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        status, created = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "修復失效字型",
            "kind": "caption",
            "settings": {"style": {"font_size": 44}, "font": descriptor},
        })
        self.assertEqual(status, 201, created)
        base = {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": state["revision"],
            "overlay_id": "caption-preset-target",
            "scope": "single",
        }
        status, plan = self.request("POST", "/api/style-presets/caption-plan", base)
        self.assertEqual(status, 200, plan)
        font_change = next(change for change in plan["changes"] if change["field"] == "font")
        self.assertEqual(font_change["before"], None)
        self.assertIs(font_change["before_unavailable"], True)
        self.assertEqual(font_change["after"], descriptor)
        self.assertNotIn(invalid_old_id, json.dumps(plan))

        status, applied = self.request("POST", "/api/style-presets/caption-apply", {
            **base,
            "plan_digest": plan["plan_digest"],
        })
        self.assertEqual(status, 200, applied)
        applied_target = next(
            item for item in applied["state"]["overlays"]
            if item["id"] == "caption-preset-target"
        )
        self.assertEqual(applied_target["style"]["font_asset_id"], valid_item["asset_id"])
        self.assertEqual(applied_target["style"]["font_family"], descriptor["family"])
        self.assertNotEqual(applied_target["style"]["font_asset_id"], invalid_old_id)

    def test_replacement_font_still_blocks_when_new_font_lacks_text_coverage(self) -> None:
        publish_verified_font(self.harness.project, "CoverageBound")
        fonts = asset_registry.list_project_fonts(self.harness.project)
        descriptor = {
            key: fonts[0][key] for key in ("sha256", "family", "style", "weight")
        }
        _state, _revision = self.install_caption()
        status, empty = self.request("GET", "/api/style-presets")
        self.assertEqual(status, 200, empty)
        status, created = self.request("POST", "/api/style-presets", {
            "expected_library_revision": empty["revision"],
            "name": "Coverage strict",
            "kind": "caption",
            "settings": {"style": {"font_size": 44}, "font": descriptor},
        })
        self.assertEqual(status, 201, created)

        state_path = self.harness.project / "working/editor_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        target = next(
            item for item in state["overlays"] if item["id"] == "caption-preset-target"
        )
        target["text"] = "龍"
        target["style"]["font_asset_id"] = "font-google-fonts-" + "0" * 16 + "-" + "1" * 16
        state["revision"] = editor_server.editor_state_revision(state)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        status, blocked = self.request("POST", "/api/style-presets/caption-plan", {
            "preset_id": created["preset"]["id"],
            "expected_library_revision": created["revision"],
            "expected_revision": state["revision"],
            "overlay_id": "caption-preset-target",
            "scope": "single",
        })
        self.assertEqual(status, 409, blocked)
        self.assertEqual(blocked["reason"], "font_coverage_failed")


if __name__ == "__main__":
    unittest.main()

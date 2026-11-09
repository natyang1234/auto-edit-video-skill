from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import project_versions  # noqa: E402


class ProjectVersionUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="project-version-unit-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        (self.project / "working").mkdir(parents=True)
        (self.project / "project.json").write_text("{}\n", encoding="utf-8")
        (self.project / "working/editor_state.json").write_text("{}\n", encoding="utf-8")

    def state(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "project_id": "fixture-project",
            "source_sha256": "a" * 64,
            "segments": [{"id": "segment-1", "source_start": 0.0, "source_end": 2.0, "origin": "manual"}],
            "variants": [{"variant_id": "vertical", "preset_id": "instagram-reels", "overrides": []}],
            "rights": {"asserted": True, "assertion_revision": "b" * 64},
            "highlight_plan_revision": "c" * 64,
            "active_highlight_id": "highlight-1",
            "highlights": [
                {"id": "highlight-1", "review_status": "approved"},
                {"id": "highlight-2", "review_status": "rejected"},
            ],
            "overlays": [
                {
                    "id": "caption-1",
                    "semantic_review": {
                        "status": "checked",
                        "unit_id": "unit-1",
                        "candidates": [],
                        "reviewer": "person",
                        "checked_at": "yesterday",
                    },
                }
            ],
            "publishing": {
                "platform_id": "instagram-reels",
                "draft_status": "ready",
                "title": "Title",
                "body": "Body",
                "hashtags": ["tag"],
                "cover": {"time": 1.0, "text": "Cover", "output": "/renders/cover.png"},
            },
            "review": {"selected_overlay_id": "caption-1", "warnings_acknowledged": ["warning"]},
            "asset_digests": {},
            "updated_at": "now",
            "revision": "d" * 64,
        }

    def create_record(self, store: project_versions.ProjectVersionStore) -> dict[str, object]:
        state = self.state()
        snapshot = project_versions.sanitize_state(state)
        return store.create(
            name="Version A",
            kind="named",
            state=state,
            captured_render_revision="1" * 64,
            snapshot_state=snapshot,
            snapshot_render_revision="2" * 64,
            source_binding={"path": "source/original.mov", "sha256": "a" * 64},
            asset_bindings={},
            dependency_bindings={},
        )

    def test_sanitize_is_explicit_and_never_revives_positive_authority(self) -> None:
        snapshot = project_versions.sanitize_state(self.state())
        self.assertNotIn("updated_at", snapshot)
        self.assertNotIn("revision", snapshot)
        self.assertNotIn("asset_digests", snapshot)
        self.assertEqual(snapshot["highlights"][0]["review_status"], "pending")
        self.assertEqual(snapshot["highlights"][1]["review_status"], "rejected")
        semantic = snapshot["overlays"][0]["semantic_review"]
        self.assertEqual(semantic["status"], "pending")
        self.assertNotIn("reviewer", semantic)
        self.assertNotIn("checked_at", semantic)
        self.assertEqual(snapshot["review"]["warnings_acknowledged"], [])
        self.assertEqual(snapshot["publishing"]["draft_status"], "local_draft_requires_review")
        self.assertIsNone(snapshot["publishing"]["cover"]["output"])
        self.assertEqual(snapshot["rights"], {"asserted": False, "assertion_revision": None})

    def test_sanitize_manifest_reports_only_actually_approved_variant_maps(self) -> None:
        timestamp = "2026-11-09T00:00:00Z"
        manifest = {
            "approvals": {
                "highlight_selection": {"approved": True},
                "timeline": {"approved": False},
                "timeline_by_variant": {
                    "vertical": {"approved": True, "state_revision": "a" * 64},
                },
                "final_by_variant": {
                    "vertical": {"approved": False, "state_revision": "b" * 64},
                    "square": {"approved": True, "state_revision": "c" * 64},
                },
                "preview_by_variant": {
                    "vertical": {"approved": False, "at": timestamp},
                },
                "empty_by_variant": {},
                "metadata_by_variant": {
                    "schema_version": 1,
                    "note": "not a variant approval slot",
                    "future": {"approved": "true"},
                },
            },
            "stages": {"render": "complete", "qa": "complete"},
        }

        sanitized, invalidated = project_versions.sanitize_manifest(
            manifest,
            ("highlight_selection", "timeline", "final"),
            timestamp=timestamp,
        )

        self.assertEqual(
            invalidated,
            ["final_by_variant", "highlight_selection", "timeline_by_variant"],
        )
        self.assertEqual(
            set(sanitized["approvals"]),
            {"highlight_selection", "timeline", "final"},
        )
        self.assertTrue(all(not entry["approved"] for entry in sanitized["approvals"].values()))
        self.assertEqual(sanitized["stages"]["render"], "pending")
        self.assertEqual(sanitized["stages"]["qa"], "pending")

    def test_unknown_state_field_is_rejected_instead_of_silently_dropped(self) -> None:
        state = self.state()
        state["future_authority"] = True
        with self.assertRaises(project_versions.ProjectVersionError) as caught:
            project_versions.sanitize_state(state)
        self.assertEqual(caught.exception.code, "unsupported_snapshot")
        self.assertEqual(caught.exception.details["unsupported_fields"], ["future_authority"])

    def test_name_rejects_unicode_controls_and_line_separators(self) -> None:
        for value in (
            "A\u0085B",
            "A\u009fB",
            "A\u2028B",
            "A\u2029B",
            "A\u2028",
            "\u2029A",
        ):
            with self.subTest(value=value.encode("unicode_escape").decode("ascii")):
                with self.assertRaises(project_versions.ProjectVersionError) as caught:
                    project_versions.validate_name(value)
                self.assertEqual(caught.exception.status, 422)
                self.assertEqual(caught.exception.code, "validation_error")

        self.assertEqual(project_versions.validate_name("版本 A — 安全"), "版本 A — 安全")

    def test_record_is_immutable_integrity_checked_and_corruption_is_not_hidden(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        record = self.create_record(store)
        self.assertEqual(store.load(record["version_id"])["record_digest"], record["record_digest"])
        path = store.records_dir / f"{record['version_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["name"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(project_versions.ProjectVersionError) as caught:
            store.list_records()
        self.assertEqual(caught.exception.code, "version_recovery_required")

    def test_partial_write_and_file_fsync_faults_never_publish_a_final_record(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        real_write = os.write

        writes = 0

        def partial_then_fail(descriptor: int, data: bytes | memoryview) -> int:
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(descriptor, bytes(data[:17]))
            raise OSError("synthetic partial write")

        with patch.object(project_versions.os, "write", side_effect=partial_then_fail):
            with self.assertRaises(OSError):
                self.create_record(store)
        self.assertEqual(store.list_records(), [])
        self.assertEqual(list(store.records_dir.iterdir()), [])

        with patch.object(project_versions.os, "fsync", side_effect=OSError("synthetic fsync")):
            with self.assertRaises(OSError):
                self.create_record(store)
        self.assertEqual(store.list_records(), [])
        self.assertEqual(list(store.records_dir.iterdir()), [])

    def test_new_owned_files_are_0600_under_restrictive_umask(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        store.records_dir.mkdir(parents=True, mode=0o700)
        store.records_dir.chmod(0o700)
        journal = self.project / project_versions.JOURNAL_REL
        restore_target = self.project / "working/restrictive-target.json"

        previous_umask = os.umask(0o277)
        try:
            project_versions._atomic_replace_exact(journal, b'{"phase":"synthetic"}\n')
            project_versions._atomic_replace_exact(
                restore_target, b'{"target":"synthetic"}\n'
            )
            record = self.create_record(store)
        finally:
            os.umask(previous_umask)

        record_path = store.records_dir / f"{record['version_id']}.json"
        modes = {
            "record": record_path.stat().st_mode & 0o777,
            "journal": journal.stat().st_mode & 0o777,
            "restore_target": restore_target.stat().st_mode & 0o777,
        }
        print(
            "P7_UMASK existing="
            + json.dumps({key: f"{value:04o}" for key, value in modes.items()}, sort_keys=True),
            flush=True,
        )
        self.assertEqual(modes, {"record": 0o600, "journal": 0o600, "restore_target": 0o600})
        self.assertEqual(store.load(record["version_id"]), record)

    def test_first_create_makes_only_new_owned_directories_exact_0700(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        version_root = self.project / "working/project_versions"
        self.assertFalse(version_root.exists())

        previous_umask = os.umask(0o277)
        try:
            record = self.create_record(store)
        finally:
            os.umask(previous_umask)

        record_path = store.records_dir / f"{record['version_id']}.json"
        modes = {
            "version_root": version_root.stat().st_mode & 0o777,
            "records": store.records_dir.stat().st_mode & 0o777,
            "record": record_path.stat().st_mode & 0o777,
        }
        print(
            "P7_UMASK new_store="
            + json.dumps({key: f"{value:04o}" for key, value in modes.items()}, sort_keys=True),
            flush=True,
        )
        self.assertEqual(modes, {"version_root": 0o700, "records": 0o700, "record": 0o600})
        self.assertEqual(store.load(record["version_id"]), record)

    def test_standard_owner_only_umask_keeps_exact_modes(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        previous_umask = os.umask(0o077)
        try:
            record = self.create_record(store)
        finally:
            os.umask(previous_umask)
        record_path = store.records_dir / f"{record['version_id']}.json"
        self.assertEqual((self.project / "working/project_versions").stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.records_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)

    def test_owner_bits_cleared_files_are_repaired_but_new_directory_fails_closed(self) -> None:
        existing_store = project_versions.ProjectVersionStore(self.project)
        existing_store.records_dir.mkdir(parents=True, mode=0o700)
        existing_store.records_dir.chmod(0o700)
        target = self.project / "working/owner-bits-cleared.json"
        previous_umask = os.umask(0o777)
        try:
            project_versions._atomic_replace_exact(target, b"synthetic\n")
            record = self.create_record(existing_store)
        finally:
            os.umask(previous_umask)
        record_path = existing_store.records_dir / f"{record['version_id']}.json"
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)

        blocked_project = Path(self.temp.name) / "owner-bits-cleared-new-store"
        (blocked_project / "working").mkdir(parents=True)
        blocked_store = project_versions.ProjectVersionStore(blocked_project)
        previous_umask = os.umask(0o777)
        try:
            with self.assertRaises(OSError):
                self.create_record(blocked_store)
        finally:
            os.umask(previous_umask)
        self.assertFalse((blocked_project / "working/project_versions").exists())

    def test_fchmod_failures_never_publish_or_replace(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        store.records_dir.mkdir(parents=True, mode=0o700)
        store.records_dir.chmod(0o700)
        with patch.object(
            project_versions.os,
            "fchmod",
            side_effect=OSError("synthetic file fchmod failure"),
        ):
            with self.assertRaises(OSError):
                self.create_record(store)
        self.assertEqual(list(store.records_dir.iterdir()), [])

        target = self.project / "working/existing-target.json"
        original = b'{"original":true}\n'
        target.write_bytes(original)
        target.chmod(0o600)
        with patch.object(
            project_versions.os,
            "fchmod",
            side_effect=OSError("synthetic atomic fchmod failure"),
        ):
            with self.assertRaises(OSError):
                project_versions._atomic_replace_exact(target, b'{"replacement":true}\n')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            [path for path in target.parent.iterdir() if path.name.startswith(f".{target.name}.")],
            [],
        )

    def test_new_directory_fchmod_failure_cleans_only_created_identity(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        resolved_project = self.project.resolve()
        version_root = resolved_project / "working/project_versions"
        with patch.object(
            project_versions.os,
            "fchmod",
            side_effect=OSError("synthetic directory fchmod failure"),
        ):
            with self.assertRaises(OSError):
                self.create_record(store)
        self.assertFalse(version_root.exists())

        version_root.mkdir(mode=0o750)
        records = version_root / "records"
        records.mkdir(mode=0o750)
        before = {
            version_root: version_root.stat().st_mode & 0o777,
            records: records.stat().st_mode & 0o777,
        }
        project_versions._assert_owned_parents(
            resolved_project,
            records / "placeholder.json",
            create=True,
        )
        self.assertEqual(
            {path: path.stat().st_mode & 0o777 for path in before},
            before,
        )

        records.rmdir()
        version_root.rmdir()
        external = Path(self.temp.name) / "external-records"
        external.mkdir()
        version_root.symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            project_versions._assert_owned_parents(
                resolved_project,
                version_root / "records/placeholder.json",
                create=True,
            )
        self.assertTrue(version_root.is_symlink())
        self.assertEqual(list(external.iterdir()), [])

    def test_leftover_temp_is_cleaned_but_corrupt_final_record_is_not_hidden(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        store.records_dir.mkdir(parents=True)
        leftover = store.records_dir / f".pv-record-{'a' * 32}.tmp"
        leftover.write_bytes(b"incomplete pre-publication bytes")
        leftover.chmod(0o600)
        self.assertEqual(store.list_records(), [])
        self.assertFalse(leftover.exists())

        corrupt = store.records_dir / f"pv_{'b' * 32}.json"
        corrupt.write_bytes(b"truncated formal record")
        corrupt.chmod(0o600)
        with self.assertRaises(project_versions.ProjectVersionError) as caught:
            store.list_records()
        self.assertEqual(caught.exception.code, "version_recovery_required")

    def test_only_regular_ds_store_metadata_is_ignored(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        store.records_dir.mkdir(parents=True)
        metadata = store.records_dir / ".DS_Store"
        metadata_bytes = b"synthetic Finder metadata"
        metadata.write_bytes(metadata_bytes)
        metadata.chmod(0o644)
        self.assertEqual(store.list_records(), [])
        self.assertEqual(store.capacity()["used"], 0)
        self.assertEqual(metadata.read_bytes(), metadata_bytes)

        metadata.unlink()
        unknown = store.records_dir / ".unknown-metadata"
        unknown.write_bytes(b"unknown")
        with self.assertRaises(project_versions.ProjectVersionError) as unknown_error:
            store.list_records()
        self.assertEqual(unknown_error.exception.code, "version_recovery_required")

        unknown.unlink()
        corrupt = store.records_dir / f"pv_{'b' * 32}.json"
        corrupt.write_bytes(b"truncated formal record")
        corrupt.chmod(0o600)
        with self.assertRaises(project_versions.ProjectVersionError) as corrupt_error:
            store.list_records()
        self.assertEqual(corrupt_error.exception.code, "version_recovery_required")

        corrupt.unlink()
        target = self.project / "working/finder-metadata-target"
        target.write_bytes(metadata_bytes)
        metadata.symlink_to(target)
        with self.assertRaises(project_versions.ProjectVersionError) as symlink_error:
            store.list_records()
        self.assertEqual(symlink_error.exception.code, "version_recovery_required")

        metadata.unlink()
        metadata.mkdir()
        with self.assertRaises(project_versions.ProjectVersionError) as directory_error:
            store.list_records()
        self.assertEqual(directory_error.exception.code, "version_recovery_required")

    def test_hard_link_publish_is_required_and_supported_locally(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        real_link = os.link
        published_inodes: list[tuple[int, int]] = []

        def observed_link(source: Path, target: Path, **kwargs: object) -> None:
            real_link(source, target, **kwargs)
            published_inodes.append((source.stat().st_ino, target.stat().st_ino))

        with patch.object(project_versions.os, "link", side_effect=observed_link):
            record = self.create_record(store)
        self.assertEqual(len(published_inodes), 1)
        self.assertEqual(published_inodes[0][0], published_inodes[0][1])
        self.assertEqual(store.load(record["version_id"]), record)

        unsupported_store = project_versions.ProjectVersionStore(
            Path(self.temp.name) / "unsupported-link"
        )
        (unsupported_store.project_dir / "working").mkdir(parents=True)
        with patch.object(
            project_versions.os,
            "link",
            side_effect=OSError(errno.ENOTSUP, "synthetic unsupported link"),
        ):
            with self.assertRaises(project_versions.ProjectVersionError) as unsupported:
                self.create_record(unsupported_store)
        self.assertEqual(unsupported.exception.status, 503)
        self.assertEqual(unsupported.exception.code, "version_storage_unsupported")
        self.assertEqual(unsupported.exception.details, {})
        self.assertEqual(unsupported_store.list_records(), [])
        self.assertEqual(list(unsupported_store.records_dir.iterdir()), [])

    def test_duplicate_generated_id_never_overwrites_existing_record(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        first = self.create_record(store)
        first_path = store.records_dir / f"{first['version_id']}.json"
        first_bytes = first_path.read_bytes()
        duplicate_hex = str(first["version_id"])[3:]
        new_hex = "c" * 32
        generated = [
            uuid.UUID(hex=duplicate_hex),
            uuid.UUID(hex="d" * 32),
            uuid.UUID(hex=new_hex),
            uuid.UUID(hex="e" * 32),
        ]
        with patch.object(project_versions.uuid, "uuid4", side_effect=generated):
            second = self.create_record(store)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(second["version_id"], f"pv_{new_hex}")
        self.assertEqual(len(store.list_records()), 2)

    def test_directory_fsync_failure_after_publish_preserves_complete_record(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        with patch.object(
            project_versions,
            "_fsync_directory",
            side_effect=OSError("synthetic directory fsync failure"),
        ):
            with self.assertRaises(OSError):
                self.create_record(store)
        records = store.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Version A")
        self.assertFalse(
            any(project_versions.RECORD_TEMP_RE.fullmatch(path.name) for path in store.records_dir.iterdir())
        )

    def test_capacity_reserves_one_auto_backup_and_never_overwrites(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        with patch.object(store, "list_records", return_value=[{}] * 99):
            with self.assertRaises(project_versions.ProjectVersionError) as named:
                store.create(
                    name="named",
                    kind="named",
                    state=self.state(),
                    captured_render_revision="1" * 64,
                    snapshot_state=project_versions.sanitize_state(self.state()),
                    snapshot_render_revision="2" * 64,
                    source_binding={},
                    asset_bindings={},
                    dependency_bindings={},
                )
            self.assertEqual(named.exception.code, "version_capacity_reserved")
        with patch.object(store, "list_records", return_value=[{}] * 100):
            with self.assertRaises(project_versions.ProjectVersionError) as auto:
                store.create(
                    name="auto",
                    kind="auto_before_restore",
                    state=self.state(),
                    captured_render_revision="1" * 64,
                    snapshot_state=project_versions.sanitize_state(self.state()),
                    snapshot_render_revision="2" * 64,
                    source_binding={},
                    asset_bindings={},
                    dependency_bindings={},
                )
            self.assertEqual(auto.exception.code, "version_capacity_exhausted")

    def test_restore_rolls_forward_after_each_durable_write_interruption(self) -> None:
        original = project_versions._atomic_replace_exact
        for fail_call in range(1, 6):
            with self.subTest(fail_call=fail_call):
                project = Path(self.temp.name) / f"journal-{fail_call}"
                (project / "working").mkdir(parents=True)
                (project / "project.json").write_text("{}\n", encoding="utf-8")
                (project / "working/editor_state.json").write_text("{}\n", encoding="utf-8")
                (project / "working/rights_assertion.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                store = project_versions.ProjectVersionStore(project)
                backup = self.create_record(store)
                targets = {
                    "project.json": project_versions.json_bytes({"restored": "manifest"}),
                    "working/rights_assertion.json": project_versions.json_bytes(
                        project_versions.empty_rights_assertion()
                    ),
                    "working/editor_state.json": project_versions.json_bytes(
                        {"restored": "state"}
                    ),
                }
                calls = 0

                def fail_after_selected_write(path: Path, raw: bytes) -> None:
                    nonlocal calls
                    calls += 1
                    original(path, raw)
                    if calls == fail_call:
                        raise OSError("synthetic interruption")

                with patch.object(
                    project_versions,
                    "_atomic_replace_exact",
                    side_effect=fail_after_selected_write,
                ):
                    with self.assertRaises(project_versions.ProjectVersionRecoveryError):
                        project_versions.execute_restore(project, backup, targets)
                self.assertTrue((project / project_versions.JOURNAL_REL).exists())
                project_versions.recover_restore_transaction(project)
                self.assertEqual(
                    json.loads((project / "project.json").read_text()),
                    {"restored": "manifest"},
                )
                self.assertEqual(
                    json.loads((project / "working/editor_state.json").read_text()),
                    {"restored": "state"},
                )
                self.assertFalse((project / project_versions.JOURNAL_REL).exists())

    def test_post_commit_cleanup_fsync_failure_rebuilds_committed_marker(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        backup = self.create_record(store)
        targets = {
            "project.json": project_versions.json_bytes({"restored": "manifest"}),
            "working/rights_assertion.json": project_versions.json_bytes(
                project_versions.empty_rights_assertion()
            ),
            "working/editor_state.json": project_versions.json_bytes(
                {"restored": "state"}
            ),
        }
        real_fsync_directory = project_versions._fsync_directory
        calls = 0

        def fail_once_after_committed_unlink(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 6:
                self.assertFalse(
                    (self.project / project_versions.JOURNAL_REL).exists()
                )
                raise OSError("synthetic committed cleanup fsync failure")
            real_fsync_directory(path)

        with patch.object(
            project_versions,
            "_fsync_directory",
            side_effect=fail_once_after_committed_unlink,
        ):
            with self.assertRaises(project_versions.ProjectVersionRecoveryError):
                project_versions.execute_restore(self.project, backup, targets)

        journal_path = self.project / project_versions.JOURNAL_REL
        self.assertTrue(journal_path.exists())
        journal, _decoded = project_versions._read_journal(self.project)
        self.assertEqual(journal["phase"], "committed")
        for relative, expected in targets.items():
            self.assertEqual((self.project / relative).read_bytes(), expected)
        with self.assertRaises(project_versions.ProjectVersionRecoveryError):
            project_versions.ensure_restore_ready(self.project)

        project_versions.recover_restore_transaction(self.project)
        self.assertFalse(journal_path.exists())
        project_versions.ensure_restore_ready(self.project)

    def test_diff_is_current_to_saved_bounded_and_does_not_hide_resets(self) -> None:
        diff = project_versions.diff_values(
            {"state": {"rights": {"asserted": True}, "title": "B"}},
            {"state": {"rights": {"asserted": False}, "title": "A"}},
        )
        by_path = {item["path"]: item for item in diff["changes"]}
        self.assertEqual(by_path["/state/rights/asserted"]["current_preview"], "true")
        self.assertEqual(by_path["/state/rights/asserted"]["saved_preview"], "false")

    def test_symlink_and_oversize_records_fail_closed(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        record = self.create_record(store)
        original = store.records_dir / f"{record['version_id']}.json"
        link = store.records_dir / f"pv_{'f' * 32}.json"
        link.symlink_to(original.name)
        with self.assertRaises(project_versions.ProjectVersionError) as symlinked:
            store.list_records()
        self.assertEqual(symlinked.exception.code, "version_recovery_required")
        link.unlink()
        oversized = store.records_dir / f"pv_{'e' * 32}.json"
        with oversized.open("wb") as handle:
            handle.truncate(project_versions.MAX_RECORD_BYTES + 1)
        with self.assertRaises(project_versions.ProjectVersionError) as too_large:
            store.list_records()
        self.assertEqual(too_large.exception.code, "version_recovery_required")

    def test_broken_store_symlink_and_permissive_record_fail_closed(self) -> None:
        broken_project = Path(self.temp.name) / "broken-store"
        (broken_project / "working/project_versions").mkdir(parents=True)
        records = broken_project / project_versions.RECORDS_REL
        records.symlink_to("missing-records")
        with self.assertRaises(project_versions.ProjectVersionError) as broken:
            project_versions.ProjectVersionStore(broken_project).list_records()
        self.assertEqual(broken.exception.code, "version_recovery_required")

        store = project_versions.ProjectVersionStore(self.project)
        record = self.create_record(store)
        path = store.records_dir / f"{record['version_id']}.json"
        original = path.read_bytes()
        for mode in (0o644, 0o200):
            with self.subTest(mode=f"{mode:04o}"):
                path.chmod(mode)
                with self.assertRaises(project_versions.ProjectVersionError) as permissive:
                    store.load(record["version_id"])
                self.assertEqual(permissive.exception.status, 503)
                self.assertEqual(
                    permissive.exception.code, "version_permissions_repair_required"
                )
                self.assertEqual(
                    permissive.exception.details,
                    {
                        "record_id": record["version_id"],
                        "expected_mode": "0600",
                        "current_mode": f"{mode:04o}",
                        "relative_path": (
                            f"working/project_versions/records/{record['version_id']}.json"
                        ),
                        "repair_action": "restrict_to_owner",
                    },
                )

                # Product code never repairs permissions. A fixture-only manual
                # repair proves recovery without changing the record bytes.
                path.chmod(0o600)
                self.assertEqual(store.load(record["version_id"]), record)
                self.assertEqual(path.read_bytes(), original)

        with patch.object(project_versions.os, "open", side_effect=PermissionError()):
            with self.assertRaises(project_versions.ProjectVersionError) as unreadable_0600:
                store.load(record["version_id"])
        self.assertEqual(unreadable_0600.exception.code, "version_recovery_required")

    def test_oversize_journal_is_rejected_before_prepared_write(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        backup = self.create_record(store)
        targets = {
            "project.json": project_versions.json_bytes({"restored": "manifest"}),
            "working/rights_assertion.json": project_versions.json_bytes(
                project_versions.empty_rights_assertion()
            ),
            "working/editor_state.json": project_versions.json_bytes({"restored": "state"}),
        }
        with patch.object(project_versions, "MAX_JOURNAL_BYTES", 128):
            with self.assertRaises(ValueError):
                project_versions.execute_restore(self.project, backup, targets)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())

    def test_recovery_refuses_missing_automatic_backup_and_retains_journal(self) -> None:
        store = project_versions.ProjectVersionStore(self.project)
        backup = self.create_record(store)
        targets = {
            "project.json": project_versions.json_bytes({"restored": "manifest"}),
            "working/rights_assertion.json": project_versions.json_bytes(project_versions.empty_rights_assertion()),
            "working/editor_state.json": project_versions.json_bytes({"restored": "state"}),
        }
        journal = project_versions._journal_payload(backup, targets)
        project_versions._atomic_replace_exact(
            self.project / project_versions.JOURNAL_REL,
            project_versions.json_bytes(journal),
        )
        (store.records_dir / f"{backup['version_id']}.json").unlink()
        before_manifest = (self.project / "project.json").read_bytes()
        with self.assertRaises(project_versions.ProjectVersionRecoveryError):
            project_versions.recover_restore_transaction(self.project)
        self.assertEqual((self.project / "project.json").read_bytes(), before_manifest)
        self.assertTrue((self.project / project_versions.JOURNAL_REL).exists())


if __name__ == "__main__":
    unittest.main()

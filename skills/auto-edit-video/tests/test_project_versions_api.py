from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import editor_server  # noqa: E402
import project_versions  # noqa: E402
from editor_server import EditorServer  # noqa: E402


class ProjectVersionsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="project-version-api-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        for relative in ("source", "working", "assets", "renders", "qa"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        source = self.project / "source/original.mov"
        source.write_bytes(b"bounded-fake-video-source")
        self.source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        self.plan_revision = "a" * 64
        self.plan = {
            "schema_version": 1,
            "plan_revision": self.plan_revision,
            "items": [
                {
                    "id": "highlight-1",
                    "start": 0.0,
                    "end": 1.5,
                    "title": "Fixture highlight",
                    "review_status": "approved",
                }
            ],
        }
        self.write_json("working/highlight_plan.json", self.plan)
        self.state = self.make_state()
        self.manifest = self.make_manifest()
        self.write_json("working/editor_state.json", self.state)
        self.write_json("project.json", self.manifest)
        self.write_json(
            "working/rights_assertion.json",
            {
                "schema_version": 1,
                "items": [
                    {
                        "asserted": True,
                        "asset_sha256": "f" * 64,
                        "asserted_by": "fixture-person",
                    }
                ],
                "revision": "e" * 64,
            },
        )
        self.start_server()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def start_server(self) -> None:
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def restart_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.start_server()

    def write_json(self, relative: str, value: object) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def make_manifest(self) -> dict[str, object]:
        approved = {
            "approved": True,
            "confirmed_by": "fixture-person",
            "at": "2026-11-09T00:00:00+00:00",
            "state_revision": "b" * 64,
        }
        return {
            "schema_version": 1,
            "project_id": "fixture-project",
            "source": {
                "staged_path": "source/original.mov",
                "sha256": self.source_sha,
                "duration_s": 2.0,
            },
            "approvals": {
                "destructive_edit": dict(approved),
                "highlight_selection": dict(approved),
                "timeline": dict(approved),
                "final": dict(approved),
                "timeline_by_variant": {"vertical": dict(approved)},
                "final_by_variant": {"vertical": dict(approved)},
            },
            "stages": {
                "edit_review": "complete",
                "highlight_plan": "complete",
                "timeline_review": "complete",
                "render": "complete",
                "qa": "complete",
            },
            "updated_at": "2026-11-09T00:00:00+00:00",
        }

    def make_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "schema_version": 2,
            "project_id": "fixture-project",
            "source_sha256": self.source_sha,
            "caption_generation": {"enabled": True, "reason": "fixture"},
            "segments": [
                {"id": "segment-1", "source_start": 0.0, "source_end": 2.0, "origin": "manual"}
            ],
            "variants": [
                {"variant_id": "vertical", "preset_id": "instagram-reels", "overrides": []}
            ],
            "rights": {"asserted": True, "assertion_revision": "c" * 64},
            "highlight_plan_revision": self.plan_revision,
            "active_highlight_id": "highlight-1",
            "highlights": [
                {
                    "id": "highlight-1",
                    "plan_item_id": "highlight-1",
                    "start": 0.0,
                    "end": 1.5,
                    "title": "Fixture highlight",
                    "review_status": "approved",
                }
            ],
            "asset_digests": {},
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "fit": "contain",
                "show_safe_zones": True,
            },
            "director_style": "teacher-punch",
            "visual_quality_mode": "designed",
            "graphic_package_style": "craft-stack",
            "editing_brief": "Version A",
            "caption_defaults": {},
            "overlays": [
                {
                    "id": "caption-1",
                    "type": "caption",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Fixture caption",
                    "visible": True,
                    "locked": False,
                    "z_index": 20,
                    "style": {},
                    "effect_spans": [],
                    "semantic_review": {
                        "status": "checked",
                        "unit_id": "unit-1",
                        "candidates": [],
                        "reviewer": "fixture-person",
                        "checked_at": "2026-11-09T00:00:00+00:00",
                    },
                }
            ],
            "publishing": {
                "platform_id": "instagram-reels",
                "draft_status": "ready",
                "title": "Title A",
                "body": "Body A",
                "hashtags": ["fixture"],
                "cover": {"time": 1.0, "text": "Cover A", "output": "/renders/cover.png"},
            },
            "review": {"selected_overlay_id": "caption-1", "warnings_acknowledged": ["fixture"]},
            "updated_at": "2026-11-09T00:00:00+00:00",
        }
        state["revision"] = editor_server.editor_state_revision(state)
        return state

    def request(self, method: str, path: str, body: object | None = None) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers: dict[str, str] = {}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-Auto-Edit-CSRF"] = self.server.csrf_token
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def create_version(self, name: str = "Version A") -> dict[str, object]:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        status, saved = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": name,
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
            },
        )
        self.assertEqual(status, 201, saved)
        return saved

    def wait_for_caption_quiescence(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.server.caption_job_lock:
                if self.server.caption_job.get("state") != "rendering":
                    return
            time.sleep(0.02)
        self.fail(f"caption worker did not quiesce: {self.server.caption_job}")

    def put_state(self, state: dict[str, object]) -> dict[str, object]:
        payload = json.loads(json.dumps(state))
        payload["x_expected_revision"] = state["revision"]
        status, saved = self.request("PUT", "/api/editor-state", payload)
        self.assertEqual(status, 200, saved)
        self.wait_for_caption_quiescence()
        return json.loads((self.project / "working/editor_state.json").read_text())

    def exercise_stock_state_shape(
        self,
        mutate: Callable[[dict[str, object]], None],
        assert_preserved: Callable[[dict[str, object]], None],
    ) -> None:
        target = json.loads((self.project / "working/editor_state.json").read_text())
        mutate(target)
        target["editing_brief"] = "stock producer target"
        target = self.put_state(target)
        saved = self.create_version("stock producer target")

        current = json.loads((self.project / "working/editor_state.json").read_text())
        current["editing_brief"] = "stock producer current before restore"
        current = self.put_state(current)

        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        status, restored = self.request(
            "POST",
            f"/api/project-versions/{saved['version']['version_id']}/restore",
            {
                "expected_state_digest": listing["current"]["state_digest"],
                "expected_render_revision": listing["current"]["render_revision"],
                "expected_manifest_digest": listing["current"]["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 200, restored)
        restored_target = json.loads((self.project / "working/editor_state.json").read_text())
        self.assertEqual(restored_target["editing_brief"], "stock producer target")
        assert_preserved(restored_target)
        manifest = json.loads((self.project / "project.json").read_text())
        rights = json.loads((self.project / "working/rights_assertion.json").read_text())
        self.assertNotIn("timeline_by_variant", manifest["approvals"])
        self.assertNotIn("final_by_variant", manifest["approvals"])
        self.assertTrue(all(not item["approved"] for item in manifest["approvals"].values()))
        self.assertEqual(rights["items"], [])

        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        automatic = next(item for item in listing["versions"] if item["kind"] == "auto_before_restore")
        status, restored_backup = self.request(
            "POST",
            f"/api/project-versions/{automatic['version_id']}/restore",
            {
                "expected_state_digest": listing["current"]["state_digest"],
                "expected_render_revision": listing["current"]["render_revision"],
                "expected_manifest_digest": listing["current"]["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 200, restored_backup)
        restored_current = json.loads((self.project / "working/editor_state.json").read_text())
        self.assertEqual(restored_current["editing_brief"], "stock producer current before restore")
        assert_preserved(restored_current)

    def test_copy_draft_generator_round_trips_named_and_automatic_versions(self) -> None:
        def mutate(state: dict[str, object]) -> None:
            publishing = state["publishing"]
            publishing.update(
                editor_server.copy_draft(
                    "instagram-reels", "這是一段用來產生本機發布文案草稿的逐字稿"
                )
            )
            publishing["platform_id"] = "instagram-reels"

        def assert_preserved(state: dict[str, object]) -> None:
            publishing = state["publishing"]
            self.assertEqual(publishing["generator"], "deterministic-transcript-draft-v1")
            self.assertEqual(publishing["draft_status"], "local_draft_requires_review")
            self.assertIsNone(publishing["cover"]["output"])

        self.exercise_stock_state_shape(mutate, assert_preserved)

    def test_pending_resolved_at_null_round_trips_named_and_automatic_versions(self) -> None:
        def mutate(state: dict[str, object]) -> None:
            state["overlays"][0]["semantic_review"] = {
                "status": "pending",
                "unit_id": "unit-1",
                "candidates": [{"source": "錯字", "replacement": "正字"}],
                "resolved_at": None,
            }

        def assert_preserved(state: dict[str, object]) -> None:
            semantic = state["overlays"][0]["semantic_review"]
            self.assertEqual(semantic["status"], "pending")
            self.assertEqual(semantic["candidates"], [{"source": "錯字", "replacement": "正字"}])
            self.assertIsNone(semantic["resolved_at"])

        self.exercise_stock_state_shape(mutate, assert_preserved)

    def test_resolved_manual_round_trips_named_and_automatic_versions(self) -> None:
        def mutate(state: dict[str, object]) -> None:
            state["overlays"][0]["semantic_review"] = {
                "status": "resolved_manual",
                "unit_id": "unit-1",
                "candidates": [],
                "resolved_at": "2026-11-09T01:24:03.795Z",
            }

        def assert_preserved(state: dict[str, object]) -> None:
            semantic = state["overlays"][0]["semantic_review"]
            self.assertEqual(semantic["status"], "resolved_manual")
            self.assertEqual(semantic["candidates"], [])
            self.assertEqual(semantic["resolved_at"], "2026-11-09T01:24:03.795Z")

        self.exercise_stock_state_shape(mutate, assert_preserved)

    def restore_with_post_commit_cleanup_failure(
        self, *, fail_marker_rebuild: bool
    ) -> tuple[int, dict[str, object], dict[str, str], int]:
        saved = self.create_version("post-commit cleanup target")
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        journal_path = self.server.project_dir / project_versions.JOURNAL_REL
        real_replace = project_versions._atomic_replace_exact
        real_fsync_directory = project_versions._fsync_directory
        journal_writes = 0
        cleanup_failed = False
        committed_target_hashes: dict[str, str] = {}

        def observe_journal_writes(path: Path, raw: bytes) -> None:
            nonlocal journal_writes, committed_target_hashes
            if path == journal_path:
                journal_writes += 1
                payload = json.loads(raw.decode("utf-8"))
                if payload.get("phase") == "committed":
                    committed_target_hashes = {
                        relative: item["sha256"]
                        for relative, item in payload["targets"].items()
                    }
                if fail_marker_rebuild and journal_writes == 3:
                    raise OSError("synthetic committed marker rebuild failure")
            real_replace(path, raw)

        def fail_cleanup_fsync_once(path: Path) -> None:
            nonlocal cleanup_failed
            if (
                journal_writes >= 2
                and not os.path.lexists(journal_path)
                and not cleanup_failed
            ):
                cleanup_failed = True
                raise OSError("synthetic final journal cleanup fsync failure")
            real_fsync_directory(path)

        with (
            patch.object(
                project_versions,
                "_atomic_replace_exact",
                side_effect=observe_journal_writes,
            ),
            patch.object(
                project_versions,
                "_fsync_directory",
                side_effect=fail_cleanup_fsync_once,
            ),
        ):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertTrue(cleanup_failed)
        self.assertEqual(set(committed_target_hashes), set(project_versions.TARGET_ORDER))
        return status, result, committed_target_hashes, journal_writes

    def test_save_diff_restore_backup_and_restart_persistence(self) -> None:
        saved = self.create_version()
        version_id = saved["version"]["version_id"]

        changed = json.loads((self.project / "working/editor_state.json").read_text())
        changed["editing_brief"] = "Version B"
        changed["publishing"]["title"] = "Title B"
        changed["updated_at"] = "2026-11-09T00:01:00+00:00"
        changed["revision"] = editor_server.editor_state_revision(changed)
        self.write_json("working/editor_state.json", changed)

        status, detail = self.request("GET", f"/api/project-versions/{version_id}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["diff"]["direction"], {"from": "current", "to": "saved"})
        paths = {item["path"] for item in detail["diff"]["changes"]}
        self.assertIn("/state/highlights/0/review_status", paths)
        self.assertIn("/state/publishing/cover/output", paths)
        self.assertIn("/rights_assertion/items/0", paths)
        self.assertTrue(detail["diff"]["rights_assertion_reset"])

        current = detail["current"]
        status, restored = self.request(
            "POST",
            f"/api/project-versions/{version_id}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 200, restored)
        self.assertTrue(restored["reload_required"])
        self.assertEqual(restored["automatic_backup"]["kind"], "auto_before_restore")

        state = json.loads((self.project / "working/editor_state.json").read_text())
        manifest = json.loads((self.project / "project.json").read_text())
        rights = json.loads((self.project / "working/rights_assertion.json").read_text())
        self.assertEqual(state["editing_brief"], "Version A")
        self.assertEqual(state["highlights"][0]["review_status"], "pending")
        self.assertEqual(state["overlays"][0]["semantic_review"]["status"], "pending")
        self.assertIsNone(state["publishing"]["cover"]["output"])
        self.assertEqual(state["rights"], {"asserted": False, "assertion_revision": None})
        self.assertEqual(rights["items"], [])
        self.assertNotIn("timeline_by_variant", manifest["approvals"])
        self.assertNotIn("final_by_variant", manifest["approvals"])
        self.assertTrue(all(not item["approved"] for item in manifest["approvals"].values()))

        self.restart_server()
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        self.assertEqual(len(listing["versions"]), 2)

    def test_restore_preserves_segments_captions_and_manual_spotlight_path(self) -> None:
        original = json.loads((self.project / "working/editor_state.json").read_text())
        spotlight_path = {
            "version": 1,
            "interpolation": "smoothstep",
            "keyframes": [
                {"source_at": 0.25, "x": 0.2, "y": 0.3},
                {"source_at": 1.25, "x": 0.8, "y": 0.45},
            ],
        }
        original["segments"] = [
            {
                "id": "segment-spotlight-a",
                "source_start": 0.0,
                "source_end": 1.75,
                "origin": "manual",
            }
        ]
        original["overlays"][0]["text"] = "Saved caption A"
        original["overlays"].append(
            {
                "id": "fx-manual-spotlight-a",
                "type": "fx",
                "code": "P5",
                "start": 0.25,
                "end": 1.25,
                "values": {},
                "placement": "",
                "scheme": "",
                "font_ids": {},
                "style": {"opacity": 0.75},
                "spotlight_path": spotlight_path,
                "visible": True,
                "locked": False,
                "z_index": 30,
            }
        )
        original["updated_at"] = "2026-11-09T00:02:00+00:00"
        original["revision"] = editor_server.editor_state_revision(original)
        self.write_json("working/editor_state.json", original)

        saved = self.create_version("P6 manual spotlight")

        changed = json.loads((self.project / "working/editor_state.json").read_text())
        changed["segments"] = [
            {
                "id": "segment-spotlight-b",
                "source_start": 0.25,
                "source_end": 2.0,
                "origin": "manual",
            }
        ]
        changed["overlays"][0]["text"] = "Changed caption B"
        changed["overlays"][1]["spotlight_path"] = {
            "version": 1,
            "interpolation": "smoothstep",
            "keyframes": [
                {"source_at": 0.25, "x": 0.4, "y": 0.4},
                {"source_at": 1.25, "x": 0.6, "y": 0.6},
            ],
        }
        changed["updated_at"] = "2026-11-09T00:03:00+00:00"
        changed["revision"] = editor_server.editor_state_revision(changed)
        self.write_json("working/editor_state.json", changed)

        status, detail = self.request(
            "GET", f"/api/project-versions/{saved['version']['version_id']}"
        )
        self.assertEqual(status, 200, detail)
        current = detail["current"]
        status, restored = self.request(
            "POST",
            f"/api/project-versions/{saved['version']['version_id']}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 200, restored)

        roundtrip = json.loads((self.project / "working/editor_state.json").read_text())
        self.assertEqual(roundtrip["segments"], original["segments"])
        self.assertEqual(roundtrip["overlays"][0]["text"], "Saved caption A")
        restored_fx = next(
            item for item in roundtrip["overlays"] if item["id"] == "fx-manual-spotlight-a"
        )
        self.assertEqual(restored_fx["spotlight_path"], spotlight_path)

    def test_full_state_cas_rejects_publishing_change_with_same_render_revision(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200)
        old = listing["current"]
        changed = json.loads((self.project / "working/editor_state.json").read_text())
        changed["publishing"]["title"] = "non-render authority changed"
        self.assertEqual(editor_server.editor_state_revision(changed), old["render_revision"])
        self.write_json("working/editor_state.json", changed)
        status, result = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": "stale",
                "expected_state_digest": old["state_digest"],
                "expected_render_revision": old["render_revision"],
                "expected_manifest_digest": old["manifest_digest"],
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "revision_conflict")
        self.assertNotEqual(result["current"]["state_digest"], old["state_digest"])

    def test_full_state_cas_rejects_rights_change_with_same_render_revision(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200)
        old = listing["current"]
        changed = json.loads((self.project / "working/editor_state.json").read_text())
        changed["rights"] = {"asserted": False, "assertion_revision": None}
        self.assertEqual(editor_server.editor_state_revision(changed), old["render_revision"])
        self.write_json("working/editor_state.json", changed)
        status, result = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": "stale rights",
                "expected_state_digest": old["state_digest"],
                "expected_render_revision": old["render_revision"],
                "expected_manifest_digest": old["manifest_digest"],
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "revision_conflict")
        self.assertNotEqual(result["current"]["state_digest"], old["state_digest"])

    def test_manifest_authority_change_conflicts_even_when_state_is_unchanged(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200)
        old = listing["current"]
        manifest = json.loads((self.project / "project.json").read_text())
        manifest["approvals"]["timeline"]["note"] = "authority changed"
        self.write_json("project.json", manifest)
        status, result = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": "stale manifest",
                "expected_state_digest": old["state_digest"],
                "expected_render_revision": old["render_revision"],
                "expected_manifest_digest": old["manifest_digest"],
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "revision_conflict")
        self.assertNotEqual(result["current"]["manifest_digest"], old["manifest_digest"])

    def test_source_byte_drift_rejects_restore_before_backup_or_journal(self) -> None:
        saved = self.create_version()
        version_id = saved["version"]["version_id"]
        (self.project / "source/original.mov").write_bytes(b"changed-source-bytes")
        status, detail = self.request("GET", f"/api/project-versions/{version_id}")
        self.assertEqual(status, 200, detail)
        current = detail["current"]
        status, result = self.request(
            "POST",
            f"/api/project-versions/{version_id}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "source_changed")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())

    def test_referenced_asset_byte_drift_rejects_restore_before_backup(self) -> None:
        asset = self.project / "assets/card.png"
        asset.write_bytes(b"fake-png-v1")
        state = json.loads((self.project / "working/editor_state.json").read_text())
        state["overlays"].append(
            {
                "id": "image-1",
                "type": "image",
                "start": 0.0,
                "end": 1.0,
                "source": "assets/card.png",
                "visible": True,
                "z_index": 5,
                "style": {},
                "effect_spans": [],
            }
        )
        state["asset_digests"] = editor_server.referenced_asset_digests(self.project, state)
        state["revision"] = editor_server.editor_state_revision(state)
        self.write_json("working/editor_state.json", state)
        saved = self.create_version("asset version")
        asset.write_bytes(b"fake-png-v2")
        status, listing = self.request("GET", "/api/project-versions")
        current = listing["current"]
        status, result = self.request(
            "POST",
            f"/api/project-versions/{saved['version']['version_id']}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "asset_changed")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)

    def test_missing_record_asset_mapping_is_rejected_before_backup(self) -> None:
        asset = self.project / "assets/card.png"
        asset.write_bytes(b"fake-png-v1")
        state = json.loads((self.project / "working/editor_state.json").read_text())
        state["overlays"].append(
            {
                "id": "image-1",
                "type": "image",
                "start": 0.0,
                "end": 1.0,
                "source": "assets/card.png",
                "visible": True,
                "z_index": 5,
                "style": {},
                "effect_spans": [],
            }
        )
        state["asset_digests"] = editor_server.referenced_asset_digests(self.project, state)
        state["revision"] = editor_server.editor_state_revision(state)
        self.write_json("working/editor_state.json", state)
        saved = self.create_version("asset mapping target")
        store = project_versions.ProjectVersionStore(self.project)
        path = store.records_dir / f"{saved['version']['version_id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["asset_bindings"] = {}
        material = {key: value for key, value in record.items() if key != "record_digest"}
        record["record_digest"] = project_versions.canonical_digest(material)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        status, result = self.request(
            "POST",
            f"/api/project-versions/{saved['version']['version_id']}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "asset_changed")
        self.assertEqual(len(store.list_records()), 1)

    def test_dependency_byte_change_with_same_plan_revision_is_rejected(self) -> None:
        saved = self.create_version("dependency version")
        plan = json.loads((self.project / "working/highlight_plan.json").read_text())
        plan["non_authority_note"] = "changed bytes, same declared revision"
        self.write_json("working/highlight_plan.json", plan)
        status, detail = self.request(
            "GET", f"/api/project-versions/{saved['version']['version_id']}"
        )
        self.assertEqual(status, 200, detail)
        current = detail["current"]
        status, result = self.request(
            "POST",
            f"/api/project-versions/{saved['version']['version_id']}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "dependency_changed")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)

    def test_caption_writer_busy_fails_without_deadlocking_or_writing(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        current = listing["current"]
        with self.server.caption_job_lock:
            self.server.caption_job["state"] = "rendering"
        status, result = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": "busy",
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "project_busy")
        self.assertIn("caption", result["busy_reasons"])
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])

    def test_pipeline_render_and_fx_busy_states_fail_without_writing(self) -> None:
        cases = ("pipeline", "render", "fx_preview")
        for reason in cases:
            with self.subTest(reason=reason):
                status, listing = self.request("GET", "/api/project-versions")
                self.assertEqual(status, 200, listing)
                current = listing["current"]
                self.server.pipeline_active_owner = object() if reason == "pipeline" else None
                self.server.render_status["state"] = "running" if reason == "render" else "idle"
                fx_active = reason == "fx_preview"
                with patch.object(
                    self.server.fx_preview_jobs,
                    "has_active_job",
                    return_value=fx_active,
                ):
                    status, result = self.request(
                        "POST",
                        "/api/project-versions",
                        {
                            "name": f"busy {reason}",
                            "expected_state_digest": current["state_digest"],
                            "expected_render_revision": current["render_revision"],
                            "expected_manifest_digest": current["manifest_digest"],
                        },
                    )
                self.server.pipeline_active_owner = None
                self.server.render_status["state"] = "idle"
                self.assertEqual(status, 409, result)
                self.assertEqual(result["reason"], "project_busy")
                self.assertIn(reason, result["busy_reasons"])
                self.assertEqual(
                    project_versions.ProjectVersionStore(self.project).list_records(), []
                )

    def test_automatic_backup_failure_stops_restore_before_authority_write(self) -> None:
        saved = self.create_version("backup failure target")
        before_state = (self.project / "working/editor_state.json").read_bytes()
        before_manifest = (self.project / "project.json").read_bytes()
        before_rights = (self.project / "working/rights_assertion.json").read_bytes()
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        original_create = project_versions.ProjectVersionStore.create

        def fail_auto_backup(store, *args, **kwargs):
            if kwargs.get("kind") == "auto_before_restore":
                raise OSError("synthetic backup failure")
            return original_create(store, *args, **kwargs)

        with patch.object(
            project_versions.ProjectVersionStore,
            "create",
            new=fail_auto_backup,
        ):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_storage_unavailable")
        self.assertEqual((self.project / "working/editor_state.json").read_bytes(), before_state)
        self.assertEqual((self.project / "project.json").read_bytes(), before_manifest)
        self.assertEqual(
            (self.project / "working/rights_assertion.json").read_bytes(), before_rights
        )
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)

    def test_partial_backup_write_failure_leaves_store_healthy_and_authority_unchanged(self) -> None:
        saved = self.create_version("partial backup target")
        before = {
            relative: (self.project / relative).read_bytes()
            for relative in (
                "project.json",
                "working/rights_assertion.json",
                "working/editor_state.json",
            )
        }
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        real_write = os.write
        writes = 0

        def partial_then_fail(descriptor: int, data: bytes | memoryview) -> int:
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(descriptor, bytes(data[:17]))
            raise OSError("synthetic partial backup write")

        with patch.object(project_versions.os, "write", side_effect=partial_then_fail):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_storage_unavailable")
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        for relative, expected in before.items():
            self.assertEqual((self.project / relative).read_bytes(), expected, relative)
        records = project_versions.ProjectVersionStore(self.project).list_records()
        self.assertEqual([item["version_id"] for item in records], [saved["version"]["version_id"]])
        self.assertFalse(
            any(
                project_versions.RECORD_TEMP_RE.fullmatch(path.name)
                for path in (self.project / project_versions.RECORDS_REL).iterdir()
            )
        )

    def test_post_backup_source_drift_keeps_backup_without_writing_authority(self) -> None:
        saved = self.create_version("post-backup drift target")
        before = {
            relative: (self.project / relative).read_bytes()
            for relative in (
                "project.json",
                "working/rights_assertion.json",
                "working/editor_state.json",
            )
        }
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        original_capture = editor_server.EditorHandler._capture_project_version

        def capture_then_drift(handler, store, **kwargs):
            record = original_capture(handler, store, **kwargs)
            if kwargs.get("kind") == "auto_before_restore":
                (self.project / "source/original.mov").write_bytes(b"post-backup drift")
            return record

        with patch.object(
            editor_server.EditorHandler,
            "_capture_project_version",
            new=capture_then_drift,
        ):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "source_changed")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 2)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        for relative, expected in before.items():
            self.assertEqual((self.project / relative).read_bytes(), expected, relative)

    def test_source_full_hash_count_is_zero_for_detail_and_two_phase_for_restore(self) -> None:
        saved = self.create_version("source hash count")
        version_id = saved["version"]["version_id"]
        source_path = (self.project / "source/original.mov").resolve()
        real_hash = editor_server.file_sha256

        detail_paths: list[Path] = []

        def detail_hash(path: Path) -> str:
            detail_paths.append(path.resolve())
            return real_hash(path)

        with patch.object(editor_server, "file_sha256", side_effect=detail_hash):
            status, detail = self.request("GET", f"/api/project-versions/{version_id}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail_paths.count(source_path), 0)

        restore_paths: list[Path] = []

        def restore_hash(path: Path) -> str:
            restore_paths.append(path.resolve())
            return real_hash(path)

        current = detail["current"]
        with patch.object(editor_server, "file_sha256", side_effect=restore_hash):
            status, restored = self.request(
                "POST",
                f"/api/project-versions/{version_id}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 200, restored)
        self.assertEqual(restore_paths.count(source_path), 2)

    def test_capacity_exhaustion_refuses_restore_before_any_authority_write(self) -> None:
        saved = self.create_version("capacity target")
        version_id = saved["version"]["version_id"]
        before_state = (self.project / "working/editor_state.json").read_bytes()
        before_manifest = (self.project / "project.json").read_bytes()
        status, listing = self.request("GET", "/api/project-versions")
        current = listing["current"]
        exhausted = {
            "used": 100,
            "max": 100,
            "named_save_available": False,
            "restore_backup_available": False,
        }
        with patch.object(project_versions.ProjectVersionStore, "capacity", return_value=exhausted):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{version_id}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 507, result)
        self.assertEqual(result["reason"], "version_capacity_exhausted")
        self.assertEqual((self.project / "working/editor_state.json").read_bytes(), before_state)
        self.assertEqual((self.project / "project.json").read_bytes(), before_manifest)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())

    def test_record_asset_mapping_extra_entry_is_rejected_before_backup(self) -> None:
        saved = self.create_version("mapping target")
        version_id = saved["version"]["version_id"]
        store = project_versions.ProjectVersionStore(self.project)
        path = store.records_dir / f"{version_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["asset_bindings"]["assets/not-referenced.png"] = "f" * 64
        material = {key: value for key, value in record.items() if key != "record_digest"}
        record["record_digest"] = project_versions.canonical_digest(material)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        status, listing = self.request("GET", "/api/project-versions")
        current = listing["current"]
        status, result = self.request(
            "POST",
            f"/api/project-versions/{version_id}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "asset_changed")
        self.assertEqual(len(store.list_records()), 1)

    def test_detail_is_diff_only_while_restore_maps_dependency_and_io_errors(self) -> None:
        saved = self.create_version("diff versus eligibility")
        version_path = f"/api/project-versions/{saved['version']['version_id']}"
        restore_path = f"{version_path}/restore"
        authority_before = {
            relative: (self.project / relative).read_bytes()
            for relative in (
                "project.json",
                "working/rights_assertion.json",
                "working/editor_state.json",
            )
        }

        manifest = json.loads((self.project / "project.json").read_text())
        manifest["source"] = []
        self.write_json("project.json", manifest)
        status, result = self.request("GET", version_path)
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "unsupported_snapshot")

        self.write_json("project.json", self.manifest)
        self.write_json("working/highlight_plan.json", ["not", "a", "mapping"])
        status, detail = self.request("GET", version_path)
        self.assertEqual(status, 200, detail)
        current = detail["current"]
        status, result = self.request(
            "POST",
            restore_path,
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "dependency_changed")

        self.write_json("working/highlight_plan.json", self.plan)
        plan_path = self.project / "working/highlight_plan.json"
        path_type = type(plan_path)
        real_read_text = path_type.read_text

        def fail_plan_read(path: Path, *args, **kwargs):
            if path.resolve() == plan_path.resolve():
                raise OSError("synthetic plan read failure")
            return real_read_text(path, *args, **kwargs)

        with patch.object(path_type, "read_text", new=fail_plan_read):
            status, detail = self.request("GET", version_path)
            self.assertEqual(status, 200, detail)
            current = detail["current"]
            status, result = self.request(
                "POST",
                restore_path,
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_storage_unavailable")

        with patch.object(
            editor_server,
            "file_sha256",
            side_effect=OSError("synthetic source read failure"),
        ):
            status, detail = self.request("GET", version_path)
            self.assertEqual(status, 200, detail)
            current = detail["current"]
            status, result = self.request(
                "POST",
                restore_path,
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_storage_unavailable")
        self.assertNotIn("synthetic", json.dumps(result))
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        for relative, expected in authority_before.items():
            self.assertEqual((self.project / relative).read_bytes(), expected, relative)

    def test_create_maps_malformed_source_plan_and_hash_io_without_writing(self) -> None:
        def create_body(name: str) -> dict[str, object]:
            status, listing = self.request("GET", "/api/project-versions")
            self.assertEqual(status, 200, listing)
            current = listing["current"]
            return {
                "name": name,
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
            }

        manifest = json.loads((self.project / "project.json").read_text())
        manifest["source"] = []
        self.write_json("project.json", manifest)
        status, result = self.request(
            "POST", "/api/project-versions", create_body("bad source")
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "source_changed")

        self.write_json("project.json", self.manifest)
        self.write_json("working/highlight_plan.json", ["not", "a", "mapping"])
        status, result = self.request(
            "POST", "/api/project-versions", create_body("bad plan")
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "dependency_changed")

        self.write_json("working/highlight_plan.json", self.plan)
        body = create_body("hash io")
        with patch.object(
            editor_server,
            "file_sha256",
            side_effect=OSError("synthetic source read failure"),
        ):
            status, result = self.request("POST", "/api/project-versions", body)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_storage_unavailable")
        self.assertNotIn("synthetic", json.dumps(result))
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())

    def test_detail_rejects_malformed_authority_manifest_and_corrupt_record_as_json(self) -> None:
        saved = self.create_version("closed detail inputs")
        detail_path = f"/api/project-versions/{saved['version']['version_id']}"
        manifest = json.loads((self.project / "project.json").read_text())
        manifest["approvals"] = []
        self.write_json("project.json", manifest)
        status, result = self.request("GET", detail_path)
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "unsupported_snapshot")

        self.write_json("project.json", self.manifest)
        record_path = (
            self.project
            / project_versions.RECORDS_REL
            / f"{saved['version']['version_id']}.json"
        )
        record_path.write_bytes(b"{truncated")
        status, result = self.request("GET", detail_path)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_recovery_required")

    def test_detail_exposes_untruncated_rights_reset_semantics(self) -> None:
        saved = self.create_version("rights reset semantics")
        detail_path = f"/api/project-versions/{saved['version']['version_id']}"
        rights = {
            "schema_version": 1,
            "items": [
                {
                    "asserted": True,
                    "asset_sha256": "f" * 64,
                    "asserted_by": "fixture-person",
                    "metadata": "x" * 300,
                }
            ],
            "revision": "e" * 64,
        }
        self.write_json("working/rights_assertion.json", rights)

        status, detail = self.request("GET", detail_path)
        self.assertEqual(status, 200, detail)
        removed = next(
            item
            for item in detail["diff"]["changes"]
            if item["path"] == "/rights_assertion/items/0"
        )
        self.assertTrue(removed["current_preview"].endswith("…"))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(removed["current_preview"])
        self.assertTrue(detail["diff"]["rights_assertion_reset"])

        state = json.loads((self.project / "working/editor_state.json").read_text())
        state["rights"] = {"asserted": False, "assertion_revision": None}
        self.write_json("working/editor_state.json", state)
        rights["items"][0]["asserted"] = False
        rights["revision"] = "d" * 64
        self.write_json("working/rights_assertion.json", rights)
        status, detail = self.request("GET", detail_path)
        self.assertEqual(status, 200, detail)
        self.assertFalse(detail["diff"]["rights_assertion_reset"])

    def test_request_local_inventory_scans_once_and_never_hides_corruption(self) -> None:
        saved = self.create_version("request inventory")
        real_list_records = project_versions.ProjectVersionStore.list_records
        scans: list[str] = []

        def tracked_list(store: project_versions.ProjectVersionStore) -> list[dict[str, object]]:
            scans.append(str(store.project_dir))
            return real_list_records(store)

        with patch.object(
            project_versions.ProjectVersionStore,
            "list_records",
            autospec=True,
            side_effect=tracked_list,
        ):
            status, first = self.request("GET", "/api/project-versions")
            self.assertEqual(status, 200, first)
            self.assertEqual(len(scans), 1)
            status, second = self.request("GET", "/api/project-versions")
            self.assertEqual(status, 200, second)
            self.assertEqual(len(scans), 2)

            current = second["current"]
            status, restored = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
            self.assertEqual(status, 200, restored)
            self.assertEqual(len(scans), 3)

        record_path = (
            self.project
            / project_versions.RECORDS_REL
            / f"{saved['version']['version_id']}.json"
        )
        record_path.write_bytes(b"{truncated")
        status, result = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_recovery_required")

    def test_unsafe_record_permissions_have_explicit_manual_repair_path(self) -> None:
        saved = self.create_version("permission repair")
        version_id = saved["version"]["version_id"]
        record_path = (
            self.project / project_versions.RECORDS_REL / f"{version_id}.json"
        )
        original = record_path.read_bytes()
        for mode in (0o644, 0o200):
            with self.subTest(mode=f"{mode:04o}"):
                record_path.chmod(mode)
                status, result = self.request("GET", "/api/project-versions")
                self.assertEqual(status, 503, result)
                self.assertEqual(
                    result["reason"], "version_permissions_repair_required"
                )
                self.assertEqual(result["record_id"], version_id)
                self.assertEqual(result["expected_mode"], "0600")
                self.assertEqual(result["current_mode"], f"{mode:04o}")
                self.assertEqual(
                    result["relative_path"],
                    f"working/project_versions/records/{version_id}.json",
                )
                self.assertEqual(result["repair_action"], "restrict_to_owner")
                self.assertNotIn(
                    str(self.project), json.dumps(result, ensure_ascii=False)
                )

                record_path.chmod(0o600)
                status, listing = self.request("GET", "/api/project-versions")
                self.assertEqual(status, 200, listing)
                self.assertEqual(len(listing["versions"]), 1)
                self.assertEqual(record_path.read_bytes(), original)

    def test_restore_requires_exact_fields_and_literal_true_confirmation(self) -> None:
        saved = self.create_version("restore validation")
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        valid = {
            "expected_state_digest": current["state_digest"],
            "expected_render_revision": current["render_revision"],
            "expected_manifest_digest": current["manifest_digest"],
            "confirm_restore": True,
        }
        cases = {
            "false": {**valid, "confirm_restore": False},
            "missing": {
                key: value for key, value in valid.items() if key != "confirm_restore"
            },
            "extra": {**valid, "unexpected": "field"},
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                status, result = self.request(
                    "POST",
                    f"/api/project-versions/{saved['version']['version_id']}/restore",
                    body,
                )
                self.assertEqual(status, 422, result)
                self.assertEqual(result["reason"], "validation_error")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())

    def test_create_rejects_unicode_controls_and_line_separators(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        capture_calls = 0
        source_hash_calls = 0
        source_path = (self.project / "source/original.mov").resolve()
        real_capture = editor_server.EditorHandler._capture_project_version
        real_hash = editor_server.file_sha256

        def counted_capture(handler, store, **kwargs):
            nonlocal capture_calls
            capture_calls += 1
            return real_capture(handler, store, **kwargs)

        def counted_hash(path: Path) -> str:
            nonlocal source_hash_calls
            if path.resolve() == source_path:
                source_hash_calls += 1
            return real_hash(path)

        with patch.object(
            editor_server.EditorHandler,
            "_capture_project_version",
            new=counted_capture,
        ), patch.object(editor_server, "file_sha256", side_effect=counted_hash):
            for name in (
                "A\u0085B",
                "A\u009fB",
                "A\u2028B",
                "A\u2029B",
                "A\u2028",
                "\u2029A",
            ):
                with self.subTest(name=name.encode("unicode_escape").decode("ascii")):
                    status, result = self.request(
                        "POST",
                        "/api/project-versions",
                        {
                            "name": name,
                            "expected_state_digest": current["state_digest"],
                            "expected_render_revision": current["render_revision"],
                            "expected_manifest_digest": current["manifest_digest"],
                        },
                    )
                    self.assertEqual(status, 422, result)
                    self.assertEqual(result["reason"], "validation_error")
        self.assertEqual(capture_calls, 0)
        self.assertEqual(source_hash_calls, 0)
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])

    def test_named_capacity_is_rejected_before_capture_or_source_hash(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        capture_calls = 0
        source_hash_calls = 0
        source_path = (self.project / "source/original.mov").resolve()
        real_capture = editor_server.EditorHandler._capture_project_version
        real_hash = editor_server.file_sha256

        def full_inventory(store):
            return project_versions.ProjectVersionInventory(
                store._inventory_key, [{}] * 99
            )

        def counted_capture(handler, store, **kwargs):
            nonlocal capture_calls
            capture_calls += 1
            return real_capture(handler, store, **kwargs)

        def counted_hash(path: Path) -> str:
            nonlocal source_hash_calls
            if path.resolve() == source_path:
                source_hash_calls += 1
            return real_hash(path)

        with patch.object(
            project_versions.ProjectVersionStore,
            "inventory",
            autospec=True,
            side_effect=full_inventory,
        ), patch.object(
            editor_server.EditorHandler,
            "_capture_project_version",
            new=counted_capture,
        ), patch.object(editor_server, "file_sha256", side_effect=counted_hash):
            status, result = self.request(
                "POST",
                "/api/project-versions",
                {
                    "name": "capacity reserved",
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                },
            )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["reason"], "version_capacity_reserved")
        self.assertEqual(capture_calls, 0)
        self.assertEqual(source_hash_calls, 0)
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])

    def test_unsupported_hard_link_is_specific_and_writes_no_authority(self) -> None:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        body = {
            "name": "unsupported hard link",
            "expected_state_digest": current["state_digest"],
            "expected_render_revision": current["render_revision"],
            "expected_manifest_digest": current["manifest_digest"],
        }
        authority_before = {
            relative: (self.project / relative).read_bytes()
            for relative in (
                "project.json",
                "working/rights_assertion.json",
                "working/editor_state.json",
            )
        }
        with patch.object(
            project_versions.os,
            "link",
            side_effect=OSError(errno.ENOTSUP, "synthetic unsupported link"),
        ) as unsupported_link:
            status, result = self.request("POST", "/api/project-versions", body)
        self.assertEqual(status, 503, result)
        self.assertEqual(unsupported_link.call_count, 1)
        self.assertEqual(result["reason"], "version_storage_unsupported")
        self.assertNotIn(str(self.project), json.dumps(result, ensure_ascii=False))
        self.assertEqual(set(result), {"ok", "reason", "error"})
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])
        self.assertEqual(
            authority_before,
            {
                relative: (self.project / relative).read_bytes()
                for relative in authority_before
            },
        )

        with patch.object(
            project_versions.os,
            "link",
            side_effect=OSError(errno.EIO, "synthetic generic I/O"),
        ) as failed_link:
            status, result = self.request("POST", "/api/project-versions", body)
        self.assertEqual(status, 503, result)
        self.assertEqual(failed_link.call_count, 1)
        self.assertEqual(result["reason"], "version_storage_unavailable")
        self.assertEqual(project_versions.ProjectVersionStore(self.project).list_records(), [])

    def test_mid_restore_http_failure_blocks_writes_then_restart_rolls_forward(self) -> None:
        saved = self.create_version("journal HTTP recovery")
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        original_replace = project_versions._atomic_replace_exact
        writes = 0

        def fail_after_manifest(path: Path, raw: bytes) -> None:
            nonlocal writes
            writes += 1
            original_replace(path, raw)
            if writes == 2:
                raise OSError("synthetic interruption after manifest")

        with patch.object(
            project_versions,
            "_atomic_replace_exact",
            side_effect=fail_after_manifest,
        ):
            status, result = self.request(
                "POST",
                f"/api/project-versions/{saved['version']['version_id']}/restore",
                {
                    "expected_state_digest": current["state_digest"],
                    "expected_render_revision": current["render_revision"],
                    "expected_manifest_digest": current["manifest_digest"],
                    "confirm_restore": True,
                },
            )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_recovery_required")
        self.assertTrue((self.project / project_versions.JOURNAL_REL).exists())

        status, blocked_get = self.request("GET", "/api/project")
        self.assertEqual(status, 503, blocked_get)
        self.assertEqual(blocked_get["reason"], "version_recovery_required")
        status, blocked_write = self.request(
            "POST",
            "/api/project-versions",
            {
                "name": "must remain blocked",
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
            },
        )
        self.assertEqual(status, 503, blocked_write)
        self.assertEqual(blocked_write["reason"], "version_recovery_required")
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 2)

        self.restart_server()
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        state = json.loads((self.project / "working/editor_state.json").read_text())
        rights = json.loads((self.project / "working/rights_assertion.json").read_text())
        manifest = json.loads((self.project / "project.json").read_text())
        self.assertEqual(state["rights"], {"asserted": False, "assertion_revision": None})
        self.assertEqual(rights, project_versions.empty_rights_assertion())
        self.assertTrue(all(not item["approved"] for item in manifest["approvals"].values()))

    def test_post_commit_cleanup_failure_rebuilds_marker_and_restart_cleans_it(self) -> None:
        status, result, target_hashes, journal_writes = (
            self.restore_with_post_commit_cleanup_failure(fail_marker_rebuild=False)
        )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_recovery_required")
        self.assertEqual(journal_writes, 3)
        journal_path = self.server.project_dir / project_versions.JOURNAL_REL
        self.assertTrue(journal_path.exists())
        journal, _targets = project_versions._read_journal(self.server.project_dir)
        self.assertEqual(journal["phase"], "committed")
        self.assertTrue(self.server.project_version_recovery_latched)
        for relative, expected in target_hashes.items():
            self.assertEqual(
                hashlib.sha256((self.server.project_dir / relative).read_bytes()).hexdigest(),
                expected,
                relative,
            )

        status, blocked_get = self.request("GET", "/api/project")
        self.assertEqual(status, 503, blocked_get)
        self.assertEqual(blocked_get["reason"], "version_recovery_required")
        status, blocked_put = self.request(
            "PUT", "/api/editor-state", {"schema_version": 2}
        )
        self.assertEqual(status, 503, blocked_put)
        self.assertEqual(blocked_put["reason"], "version_recovery_required")
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200, health)

        self.restart_server()
        self.assertFalse(journal_path.exists())
        self.assertFalse(self.server.project_version_recovery_latched)
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)

    def test_post_commit_marker_rebuild_failure_uses_process_latch(self) -> None:
        status, result, target_hashes, journal_writes = (
            self.restore_with_post_commit_cleanup_failure(fail_marker_rebuild=True)
        )
        self.assertEqual(status, 503, result)
        self.assertEqual(result["reason"], "version_recovery_required")
        self.assertEqual(journal_writes, 3)
        journal_path = self.server.project_dir / project_versions.JOURNAL_REL
        self.assertFalse(journal_path.exists())
        self.assertTrue(self.server.project_version_recovery_latched)
        for relative, expected in target_hashes.items():
            self.assertEqual(
                hashlib.sha256((self.server.project_dir / relative).read_bytes()).hexdigest(),
                expected,
                relative,
            )

        status, blocked_get = self.request("GET", "/api/project")
        self.assertEqual(status, 503, blocked_get)
        self.assertEqual(blocked_get["reason"], "version_recovery_required")
        status, blocked_put = self.request(
            "PUT", "/api/editor-state", {"schema_version": 2}
        )
        self.assertEqual(status, 503, blocked_put)
        self.assertEqual(blocked_put["reason"], "version_recovery_required")
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200, health)

    def test_server_startup_refuses_corrupt_or_unrecoverable_restore_journal(self) -> None:
        saved = self.create_version("startup refusal backup")
        source_store = project_versions.ProjectVersionStore(self.project)
        source_record = source_store.load(saved["version"]["version_id"])
        targets = {
            relative: (self.project / relative).read_bytes()
            for relative in project_versions.TARGET_ORDER
        }

        corrupt_project = Path(self.temp.name) / "corrupt-startup-project"
        shutil.copytree(self.project, corrupt_project)
        project_versions._atomic_replace_exact(
            corrupt_project / project_versions.JOURNAL_REL,
            b"{not valid json\n",
        )
        with self.assertRaises(project_versions.ProjectVersionRecoveryError):
            EditorServer(("127.0.0.1", 0), corrupt_project)
        self.assertTrue((corrupt_project / project_versions.JOURNAL_REL).exists())

        missing_backup_project = Path(self.temp.name) / "missing-backup-startup-project"
        shutil.copytree(self.project, missing_backup_project)
        journal = project_versions._journal_payload(source_record, targets)
        project_versions._atomic_replace_exact(
            missing_backup_project / project_versions.JOURNAL_REL,
            project_versions.json_bytes(journal),
        )
        (
            missing_backup_project
            / project_versions.RECORDS_REL
            / f"{saved['version']['version_id']}.json"
        ).unlink()
        with self.assertRaises(project_versions.ProjectVersionRecoveryError):
            EditorServer(("127.0.0.1", 0), missing_backup_project)
        self.assertTrue((missing_backup_project / project_versions.JOURNAL_REL).exists())

    def test_invalid_opaque_id_returns_json_not_found(self) -> None:
        status, payload = self.request("GET", "/api/project-versions/not-an-id")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["reason"], "version_not_found")


if __name__ == "__main__":
    unittest.main()

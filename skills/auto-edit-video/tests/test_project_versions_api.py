from __future__ import annotations

import copy
import errno
import hashlib
import http.client
import io
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

    def restore_version(self, version_id: str) -> tuple[int, dict[str, object]]:
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200, listing)
        current = listing["current"]
        return self.request(
            "POST",
            f"/api/project-versions/{version_id}/restore",
            {
                "expected_state_digest": current["state_digest"],
                "expected_render_revision": current["render_revision"],
                "expected_manifest_digest": current["manifest_digest"],
                "confirm_restore": True,
            },
        )

    def mixed_audio_state(self) -> tuple[dict[str, object], dict[str, object], str]:
        state = json.loads((self.project / "working/editor_state.json").read_text())
        state["highlights"] = [
            {
                "id": "highlight-1",
                "plan_item_id": "highlight-1",
                "start": 0.0,
                "end": 0.8,
                "title": "Approved source highlight",
                "review_status": "approved",
            },
            {
                "id": "highlight-2",
                "plan_item_id": "highlight-2",
                "start": 0.9,
                "end": 1.4,
                "title": "Second approved source highlight",
                "review_status": "approved",
            },
            {
                "id": "highlight-3",
                "plan_item_id": "highlight-3",
                "start": 1.5,
                "end": 1.9,
                "title": "Already pending highlight",
                "review_status": "pending",
            },
        ]
        state["active_highlight_id"] = "highlight-1"
        self.plan["items"] = [
            {
                "id": item["plan_item_id"],
                "start": item["start"],
                "end": item["end"],
                "title": item["title"],
                "review_status": item["review_status"],
            }
            for item in state["highlights"]
        ]
        self.write_json("working/highlight_plan.json", self.plan)
        editor_server.upgrade_video_template_state(state)
        plan = json.loads(
            (SKILL_DIR / "contracts/fixtures/audio_event_plan/valid_v2.json").read_text(
                encoding="utf-8"
            )
        )
        source_revision = editor_server.editor_base_state_revision(state)
        plan["timeline_revision"] = source_revision
        plan_hash = editor_server.contract_registry.canonical_hash(plan)
        source_event = plan["events"][0]
        edits = {
            "schema_version": 1,
            "source_render_id": "version-audio-source",
            "source_plan_sha256": plan_hash,
            "source_timeline_revision": source_revision,
            "events": [
                {
                    "id": source_event["id"],
                    "source_event_sha256": editor_server.contract_registry.canonical_hash(
                        source_event
                    ),
                    "event_start_sample": source_event["event_start_sample"],
                    "gain_db": -18,
                }
            ],
        }
        state["audio_event_edits"] = edits
        state["revision"] = editor_server.editor_state_revision(state)
        return state, plan, plan_hash

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

    def test_main_reports_version_restore_journal_without_binding(self) -> None:
        saved = self.create_version("CLI recovery fixture")
        source_store = project_versions.ProjectVersionStore(self.project)
        source_record = source_store.load(saved["version"]["version_id"])
        cli_project = Path(self.temp.name) / "cli-recovery-project"
        shutil.copytree(self.project, cli_project)
        cli_store = project_versions.ProjectVersionStore(cli_project)
        automatic = cli_store.create(
            name="automatic recovery fixture",
            kind="auto_before_restore",
            state=source_record["snapshot_state"],
            captured_render_revision=source_record["captured_render_revision"],
            snapshot_state=source_record["snapshot_state"],
            snapshot_render_revision=source_record["snapshot_render_revision"],
            source_binding=source_record["source_binding"],
            asset_bindings=source_record["asset_bindings"],
            dependency_bindings=source_record["dependency_bindings"],
        )
        targets = {
            relative: (cli_project / relative).read_bytes()
            for relative in project_versions.TARGET_ORDER
        }
        journal = project_versions._journal_payload(automatic, targets)
        first_target = project_versions.TARGET_ORDER[0]
        journal["targets"][first_target]["data_base64"] = "AA=="
        journal_path = cli_project / project_versions.JOURNAL_REL
        project_versions._atomic_replace_exact(
            journal_path, project_versions.json_bytes(journal)
        )
        backup_path = (
            cli_project
            / project_versions.RECORDS_REL
            / f"{automatic['version_id']}.json"
        )
        journal_before = journal_path.read_bytes()
        backup_before = backup_path.read_bytes()
        stderr = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "editor_server.py",
                    "--project-dir",
                    str(cli_project),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
            ),
            patch.object(sys, "stderr", stderr),
            patch.object(
                editor_server.ThreadingHTTPServer,
                "__init__",
                side_effect=AssertionError("listener was bound"),
            ) as bind_listener,
        ):
            result = editor_server.main()

        diagnostic = stderr.getvalue()
        self.assertEqual(result, 3)
        bind_listener.assert_not_called()
        self.assertIn(".project-version-restore-journal.json", diagnostic)
        self.assertIn("preserve it if present", diagnostic)
        self.assertIn("complete automatic backup record", diagnostic)
        self.assertIn("Do not delete or modify", diagnostic)
        self.assertNotIn("journal retained", diagnostic.lower())
        self.assertNotIn("Traceback", diagnostic)
        self.assertNotIn(str(cli_project), diagnostic)
        self.assertNotIn(automatic["version_id"], diagnostic)
        self.assertEqual(journal_path.read_bytes(), journal_before)
        self.assertEqual(backup_path.read_bytes(), backup_before)

    def test_main_reports_marker_rebuild_loss_without_claiming_journal_retained(self) -> None:
        stderr = io.StringIO()
        recovery_error = project_versions.ProjectVersionRecoveryError(
            "committed restore journal cleanup failed and marker rebuild failed"
        )

        with (
            patch.object(
                sys,
                "argv",
                [
                    "editor_server.py",
                    "--project-dir",
                    str(self.project),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
            ),
            patch.object(sys, "stderr", stderr),
            patch.object(
                project_versions,
                "recover_restore_transaction",
                side_effect=recovery_error,
            ),
            patch.object(
                editor_server.ThreadingHTTPServer,
                "__init__",
                side_effect=AssertionError("listener was bound"),
            ) as bind_listener,
        ):
            result = editor_server.main()

        diagnostic = stderr.getvalue()
        self.assertEqual(result, 3)
        bind_listener.assert_not_called()
        self.assertIn("preserve it if present", diagnostic)
        self.assertIn("complete automatic backup record", diagnostic)
        self.assertNotIn("journal retained", diagnostic.lower())
        self.assertNotIn("Traceback", diagnostic)
        self.assertNotIn(str(self.project), diagnostic)

    def test_audio_versions_rebind_only_the_unique_sanitized_approval_subset(self) -> None:
        live, plan, plan_hash = self.mixed_audio_state()
        edits = copy.deepcopy(live["audio_event_edits"])
        self.write_json("working/editor_state.json", live)

        with patch.object(
            editor_server,
            "_finalized_audio_plan",
            return_value=(plan, plan_hash),
        ):
            saved = self.create_version("audio approval downgrade")
            version_id = saved["version"]["version_id"]

            current = json.loads(
                (self.project / "working/editor_state.json").read_text(encoding="utf-8")
            )
            current["editing_brief"] = "current audio state before restore"
            current["revision"] = editor_server.editor_state_revision(current)
            self.write_json("working/editor_state.json", current)

            status, restored = self.restore_version(version_id)
            self.assertEqual(status, 200, restored)
            self.assertEqual(restored["automatic_backup"]["kind"], "auto_before_restore")
            restored_state = json.loads(
                (self.project / "working/editor_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["review_status"] for item in restored_state["highlights"]],
                ["pending", "pending", "pending"],
            )
            self.assertEqual(restored_state["audio_event_edits"], edits)

            status, project = self.request("GET", "/api/project")
            self.assertEqual(status, 200, project)
            self.assertTrue(project["audio_event_timeline"]["editable"], project)
            self.assertEqual(project["audio_event_timeline"]["events"][0]["gain_db"], -18)

            status, rejected = self.request(
                "POST",
                "/api/render",
                {
                    "quality": "final",
                    "expected_revision": project["state"]["revision"],
                },
            )
            self.assertEqual(status, 409, rejected)

            correct = copy.deepcopy(restored_state)
            correct["highlights"][0]["review_status"] = "approved"
            correct["highlights"][1]["review_status"] = "approved"
            render_id, resolved, resolved_hash = editor_server.resolve_audio_event_source(
                self.project, correct
            )
            self.assertEqual((render_id, resolved_hash), ("version-audio-source", plan_hash))
            self.assertEqual(resolved, plan)
            self.assertEqual(
                editor_server.resolve_studio_audio_plan(plan, edits)["studio_edits"], edits
            )

            partial = copy.deepcopy(restored_state)
            partial["highlights"][0]["review_status"] = "approved"
            editor_server.resolve_audio_event_source(self.project, partial)
            partial = self.put_state(partial)
            status, destructive = self.request(
                "POST",
                "/api/approve",
                {
                    "gate": "destructive_edit",
                    "expected_revision": editor_server.gate_revision(
                        self.project, "destructive_edit", partial
                    ),
                },
            )
            self.assertEqual(status, 200, destructive)
            status, highlight_approval = self.request(
                "POST",
                "/api/approve",
                {
                    "gate": "highlight_selection",
                    "expected_revision": editor_server.gate_revision(
                        self.project, "highlight_selection", partial
                    ),
                },
            )
            self.assertEqual(status, 200, highlight_approval)
            jobs_before = copy.deepcopy(self.server.fx_preview_jobs._jobs)
            render_before = copy.deepcopy(self.server.render_status)
            status, timeline_blocked = self.request(
                "POST",
                "/api/approve",
                {
                    "gate": "timeline",
                    "expected_revision": editor_server.gate_revision(
                        self.project, "timeline", partial
                    ),
                },
            )
            self.assertEqual(status, 409, timeline_blocked)
            self.assertIn("Second approved source highlight", timeline_blocked["error"])
            self.assertIn("highlight-2", timeline_blocked["error"])
            self.assertNotIn("Already pending highlight", timeline_blocked["error"])
            self.assertNotIn("highlight-3", timeline_blocked["error"])
            self.assertEqual(self.server.fx_preview_jobs._jobs, jobs_before)
            self.assertEqual(self.server.render_status, render_before)

            status, render_blocked = self.request(
                "POST",
                "/api/render",
                {
                    "quality": "final",
                    "expected_revision": partial["revision"],
                },
            )
            self.assertEqual(status, 409, render_blocked)
            self.assertIn("Second approved source highlight", render_blocked["error"])
            self.assertNotIn("highlight-3", render_blocked["error"])
            self.assertEqual(self.server.fx_preview_jobs._jobs, jobs_before)
            self.assertEqual(self.server.render_status, render_before)

            manifest = json.loads(
                (self.project / "project.json").read_text(encoding="utf-8")
            )
            manifest["approvals"]["timeline"] = {
                "approved": True,
                "state_revision": editor_server.gate_revision(
                    self.project, "timeline", partial
                ),
            }
            with patch.object(editor_server, "delivery_qa_errors", return_value=[]):
                self.assertEqual(
                    editor_server.approval_prerequisite_errors(
                        self.project, manifest, partial, "final"
                    )[0],
                    timeline_blocked["error"],
                )
            partial_plan = copy.deepcopy(plan)
            partial_plan["timeline_revision"] = editor_server.editor_base_state_revision(
                partial
            )
            with self.assertRaisesRegex(
                editor_server.AudioEventEditError,
                "source_timeline_revision.*does not match plan timeline",
            ):
                editor_server.resolve_studio_audio_plan(partial_plan, edits)

            complete = copy.deepcopy(partial)
            complete["highlights"][1]["review_status"] = "approved"
            complete = self.put_state(complete)
            status, highlight_approval = self.request(
                "POST",
                "/api/approve",
                {
                    "gate": "highlight_selection",
                    "expected_revision": editor_server.gate_revision(
                        self.project, "highlight_selection", complete
                    ),
                },
            )
            self.assertEqual(status, 200, highlight_approval)
            status, timeline_approval = self.request(
                "POST",
                "/api/approve",
                {
                    "gate": "timeline",
                    "expected_revision": editor_server.gate_revision(
                        self.project, "timeline", complete
                    ),
                },
            )
            self.assertEqual(status, 200, timeline_approval)
            self.assertEqual(
                editor_server.resolve_studio_audio_plan(plan, edits)["studio_edits"], edits
            )

            wrong = copy.deepcopy(restored_state)
            wrong["highlights"][2]["review_status"] = "approved"
            with self.assertRaisesRegex(
                editor_server.AudioEventEditError, "stale for current visual state"
            ):
                editor_server.resolve_audio_event_source(self.project, wrong)

            for label, mutate in (
                ("title", lambda state: state["highlights"][0].update(title="drift")),
                ("timing", lambda state: state["highlights"][0].update(end=0.7)),
            ):
                stale = copy.deepcopy(restored_state)
                mutate(stale)
                with self.subTest(label=label), self.assertRaisesRegex(
                    editor_server.AudioEventEditError, "stale for current visual state"
                ):
                    editor_server.resolve_audio_event_source(self.project, stale)
                readiness = editor_server.audio_event_producer_readiness_errors(
                    self.project, stale
                )
                self.assertEqual(len(readiness), 1)
                self.assertIn("不一致", readiness[0])

            changed_hash = copy.deepcopy(restored_state)
            changed_hash["audio_event_edits"]["source_plan_sha256"] = "f" * 64
            with self.assertRaisesRegex(editor_server.AudioEventEditError, "plan hash changed"):
                editor_server.resolve_audio_event_source(self.project, changed_hash)
            changed_hash_readiness = editor_server.audio_event_producer_readiness_errors(
                self.project, changed_hash
            )
            self.assertEqual(len(changed_hash_readiness), 1)
            self.assertIn("不一致", changed_hash_readiness[0])

            changed_timeline = copy.deepcopy(plan)
            changed_timeline["timeline_revision"] = "e" * 64
            with patch.object(
                editor_server,
                "_finalized_audio_plan",
                return_value=(changed_timeline, plan_hash),
            ), self.assertRaisesRegex(
                editor_server.AudioEventEditError, "timeline binding changed"
            ):
                editor_server.resolve_audio_event_source(self.project, restored_state)

            malformed = copy.deepcopy(restored_state)
            malformed["highlights"] = "not-a-list"
            with self.assertRaisesRegex(editor_server.AudioEventEditError, "highlights"):
                editor_server.resolve_audio_event_source(self.project, malformed)

            too_many = copy.deepcopy(restored_state)
            too_many["highlights"] = [
                {
                    "id": f"highlight-{index}",
                    "start": 0.0,
                    "end": 0.1,
                    "title": str(index),
                    "review_status": "pending",
                }
                for index in range(11)
            ]
            with self.assertRaisesRegex(editor_server.AudioEventEditError, "10"):
                editor_server.resolve_audio_event_source(self.project, too_many)

    def test_project_font_dependencies_are_stable_but_bytes_and_receipts_are_bound(self) -> None:
        font_a = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        font_b = "font-fontsource-1123456789abcdef-1123456789abcdef"
        font_c = "font-github-fonts-2123456789abcdef-2123456789abcdef"
        bindings: dict[str, dict[str, object]] = {}
        dynamic_nonce = "initial"

        def make_binding(asset_id: str, marker: str) -> dict[str, object]:
            return {
                "asset_id": asset_id,
                "path": f"assets/providers/fonts/{asset_id}.ttf",
                "sha256": hashlib.sha256(f"font-bytes-{marker}".encode()).hexdigest(),
                "provider_id": "google-fonts",
                "license_spdx": "OFL-1.1",
                "receipt": {"schema_version": 3, "identity": f"receipt-{marker}"},
                "validation_receipt": {"required_coverage_sha256": dynamic_nonce},
                "coverage": {"required_coverage_sha256": dynamic_nonce},
            }

        bindings.update(
            {
                font_a: make_binding(font_a, "a"),
                font_b: make_binding(font_b, "b"),
                font_c: make_binding(font_c, "c"),
            }
        )

        def resolve_font(_project: Path, asset_id: str, required_text: str = ""):
            del required_text
            if asset_id not in bindings:
                raise editor_server.asset_registry.AssetRegistryError("font missing")
            value = copy.deepcopy(bindings[asset_id])
            value["validation_receipt"]["required_coverage_sha256"] = dynamic_nonce
            value["coverage"]["required_coverage_sha256"] = dynamic_nonce
            return value

        target = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        target["caption_defaults"]["font_asset_id"] = font_a
        target["overlays"][0]["style"]["font_asset_id"] = font_b
        target["revision"] = editor_server.editor_state_revision(target)
        self.write_json("working/editor_state.json", target)

        with patch.object(
            editor_server.asset_registry,
            "resolve_project_font",
            side_effect=resolve_font,
        ):
            saved = self.create_version("font dependency target")
            version_id = saved["version"]["version_id"]
            record = project_versions.ProjectVersionStore(self.project).load(version_id)
            self.assertEqual(
                {
                    key
                    for key in record["dependency_bindings"]
                    if key.startswith("project-font:")
                },
                {f"project-font:{font_a}", f"project-font:{font_b}"},
            )

            current = copy.deepcopy(target)
            current["editing_brief"] = "current uses a distinct overlay font"
            current["overlays"][0]["text"] = "Normal caption text change"
            current["overlays"][0]["style"]["font_asset_id"] = font_c
            current["revision"] = editor_server.editor_state_revision(current)
            self.write_json("working/editor_state.json", current)

            original_b = copy.deepcopy(bindings[font_b])
            del bindings[font_b]
            status, missing = self.restore_version(version_id)
            self.assertEqual((status, missing["reason"]), (409, "asset_changed"))

            bindings[font_b] = copy.deepcopy(original_b)
            bindings[font_b]["sha256"] = "f" * 64
            status, changed_bytes = self.restore_version(version_id)
            self.assertEqual((status, changed_bytes["reason"]), (409, "dependency_changed"))

            bindings[font_b] = copy.deepcopy(original_b)
            bindings[font_b]["receipt"]["identity"] = "receipt-drift"
            status, changed_receipt = self.restore_version(version_id)
            self.assertEqual((status, changed_receipt["reason"]), (409, "dependency_changed"))

            bindings[font_b] = copy.deepcopy(original_b)
            dynamic_nonce = "text-dependent-validation-change"
            status, restored = self.restore_version(version_id)
            self.assertEqual(status, 200, restored)
            restored_state = json.loads(
                (self.project / "working/editor_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                restored_state["overlays"][0]["style"]["font_asset_id"], font_b
            )
            automatic_id = restored["automatic_backup"]["version_id"]
            automatic = project_versions.ProjectVersionStore(self.project).load(automatic_id)
            self.assertIn(f"project-font:{font_c}", automatic["dependency_bindings"])
            self.assertNotIn(f"project-font:{font_b}", automatic["dependency_bindings"])

    def test_fx_preview_is_blocked_by_version_journal_and_process_latch(self) -> None:
        manager = self.server.fx_preview_jobs

        def storage_snapshot() -> dict[str, object]:
            return {
                "jobs": copy.deepcopy(manager._jobs),
                "active": manager._active_job_id,
                "cache": sorted(
                    path.relative_to(manager.root).as_posix()
                    for path in manager.root.rglob("*")
                    if path.is_file()
                ),
            }

        before = storage_snapshot()
        journal = self.project / project_versions.JOURNAL_REL
        journal.write_text("{}\n", encoding="utf-8")
        body = {
            "schema_version": 1,
            "expected_revision": self.state["revision"],
            "candidate": {
                "code": "T3",
                "window": {"start_s": 0.0, "duration_s": 0.5},
                "values": {},
                "placement": "",
                "scheme": "",
                "font_ids": {},
            },
        }
        status, blocked = self.request("POST", "/api/fx-previews", body)
        self.assertEqual((status, blocked["reason"]), (503, "version_recovery_required"))
        self.assertTrue(self.server.project_version_recovery_latched)
        self.assertEqual(storage_snapshot(), before)

        journal.unlink()
        status, latched = self.request("POST", "/api/fx-previews", body)
        self.assertEqual((status, latched["reason"]), (503, "version_recovery_required"))
        self.assertEqual(storage_snapshot(), before)

        self.server.project_version_recovery_latched = False
        healthy = self.server.fx_preview_context()
        self.assertEqual(healthy["state_revision"], self.state["revision"])
        with patch.object(
            editor_server,
            "ensure_caption_preset_mutation_ready",
            side_effect=editor_server.CaptionPresetRecoveryError("caption journal"),
        ), self.assertRaisesRegex(editor_server.FxPreviewJobError, "字幕預設交易") as raised:
            self.server.fx_preview_context()
        self.assertEqual(raised.exception.code, "transaction_recovery_required")

    def test_invalid_opaque_id_returns_json_not_found(self) -> None:
        status, payload = self.request("GET", "/api/project-versions/not-an-id")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["reason"], "version_not_found")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
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
        status, listing = self.request("GET", "/api/project-versions")
        self.assertEqual(status, 200)
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
        self.assertEqual(result["reason"], "version_recovery_required")
        self.assertEqual((self.project / "working/editor_state.json").read_bytes(), before_state)
        self.assertEqual((self.project / "project.json").read_bytes(), before_manifest)
        self.assertEqual(
            (self.project / "working/rights_assertion.json").read_bytes(), before_rights
        )
        self.assertFalse((self.project / project_versions.JOURNAL_REL).exists())
        self.assertEqual(len(project_versions.ProjectVersionStore(self.project).list_records()), 1)

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

    def test_invalid_opaque_id_returns_json_not_found(self) -> None:
        status, payload = self.request("GET", "/api/project-versions/not-an-id")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["reason"], "version_not_found")


if __name__ == "__main__":
    unittest.main()

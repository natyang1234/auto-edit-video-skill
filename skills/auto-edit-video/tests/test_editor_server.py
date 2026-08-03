from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import editor_server  # noqa: E402
from editor_server import (  # noqa: E402
    EditorServer,
    caption_effect_spans,
    editor_state_revision,
    extract_effect_keywords,
    gate_revision,
    migrate_editor_state_v1_to_v2,
    render_download_errors,
    validate_editor_state,
)
from render_editor_timeline import build_render_command, text_filter  # noqa: E402


class CaptionEffectModelTests(unittest.TestCase):
    def test_semantic_review_metadata_does_not_change_render_revision(self) -> None:
        state = {
            "schema_version": 1,
            "project_id": "revision-test",
            "overlays": [
                {
                    "id": "caption-1",
                    "type": "caption",
                    "text": "字幕",
                    "start": 0.0,
                    "end": 1.0,
                }
            ],
        }
        original = editor_state_revision(state)
        state["overlays"][0]["semantic_review"] = {
            "status": "pending",
            "candidates": [{"source": "字暮", "replacement": "字幕"}],
        }

        self.assertEqual(editor_state_revision(state), original)

    def test_highlight_copy_proposes_bounded_editable_caption_keywords(self) -> None:
        keywords = extract_effect_keywords(
            [
                "It 作虛主詞：看到 It，想到 to V",
                "真正主詞＝後面的不定詞片語",
            ]
        )
        spans = caption_effect_spans(
            {"items": []},
            "是什麼意思？同學看到 It，想到 to V，你就這樣記吧。",
            10.0,
            16.0,
            "#ffb000",
            keywords,
        )
        self.assertEqual([item["text"] for item in spans], ["It", "to V"])
        self.assertEqual({item["style"]["effect"] for item in spans}, {"pop", "highlight"})
        self.assertLessEqual(len(spans), 2)

    def test_keyword_matching_keeps_natural_chinese_de_in_caption_range(self) -> None:
        spans = caption_effect_spans(
            {"items": []},
            "真正的主詞，也就是最重要的意思。",
            20.0,
            24.0,
            "#ffb000",
            ["真正主詞"],
        )
        self.assertEqual(spans[0]["text"], "真正的主詞")
        self.assertEqual("真正的主詞，也就是最重要的意思。"[spans[0]["start_char"] : spans[0]["end_char"]], "真正的主詞")


class EditorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {"RUMI_VOICE_SYSTEM": str(RUMI_FIXTURE)},
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-editor-tests-")
        self.project = Path(self._tmp.name) / "project"
        for name in ("source", "working", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        (self.project / "source/source.mp4").write_bytes(b"0123456789abcdef")
        self.write_json(
            "project.json",
            {
                "schema_version": 1,
                "project_id": "editor-test",
                "source": {"staged_path": "source/source.mp4", "duration_s": 2.0},
                "approvals": {
                    "destructive_edit": {"approved": False},
                    "timeline": {"approved": False},
                    "final": {"approved": False},
                },
            },
        )
        self.write_json(
            "working/transcript_words.json",
            {
                "text": "雪茄與香菸的差別",
                "segments": [
                    {"id": "segment-0001", "start": 0.1, "end": 1.4, "text": "雪茄與香菸的差別"}
                ],
            },
        )
        self.write_json(
            "working/transcript_calibration.json",
            {
                "status": "applied_needs_review",
                "rule_count": 2,
                "correction_count": 3,
                "human_review_required": True,
            },
        )
        self.write_json(
            "working/transcript_review.json",
            {
                "status": "needs_review",
                "risk_status": "review_required",
                "mechanical_issue_count": 0,
                "semantic_calibration": {
                    "status": "applied_needs_review",
                    "correction_count": 3,
                },
            },
        )
        self.write_json(
            "working/transcript_semantic_review.json",
            {
                "status": "complete_needs_review",
                "coverage_status": "complete",
                "reviewed_unit_count": 1,
                "total_unit_count": 1,
                "accepted_count": 1,
                "pending_count": 0,
                "applied_correction_count": 1,
                "human_review_required": True,
            },
        )
        self.write_json(
            "working/edit_candidates.json",
            {
                "items": [
                    {
                        "id": "edit-0001",
                        "type": "silence",
                        "start": 1.4,
                        "end": 1.7,
                        "risk": "low",
                    }
                ]
            },
        )
        self.write_json("working/edit_decisions.json", {"items": []})
        self.write_json(
            "working/emphasis_plan.json",
            {
                "items": [
                    {
                        "id": "em-0001",
                        "start": 0.4,
                        "end": 0.8,
                        "text": "差別",
                        "review_status": "pending",
                    }
                ]
            },
        )
        self.write_json(
            "working/visual_plan.json",
            {
                "items": [
                    {
                        "id": "visual-0001",
                        "start": 0.1,
                        "end": 1.0,
                        "type": "title_card",
                        "text": "雪茄與香菸",
                        "review_status": "pending",
                    }
                ]
            },
        )
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def test_hidden_stage_placeholder_has_explicit_css_contract(self) -> None:
        css = (SKILL_DIR / "editor/styles.css").read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r"\.stage-empty\[hidden\]\s*\{[^}]*display:\s*none",
            "the loaded-video placeholder must not override the hidden attribute",
        )

    def test_hybrid_editor_workflow_controls_are_present(self) -> None:
        html = (SKILL_DIR / "editor/index.html").read_text(encoding="utf-8")
        css = (SKILL_DIR / "editor/styles.css").read_text(encoding="utf-8")
        script = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")
        for element_id in (
            "source-file-name",
            "transcript-calibration-status",
            "highlight-list",
            "highlight-editor",
            "approve-highlights",
            "editing-brief",
            "director-grid",
            "template-grid",
            "template-frame-x",
            "template-subject-scale",
            "template-background-input",
            "replan-highlights",
            "layer-list",
            "timeline-tracks",
            "render-button",
            "render-batch-final",
            "batch-render-progress",
            "batch-delivery-qa",
            "download-batch-archive",
            "download-output",
            "approve-final",
            "delivery-qa-status",
            "qa-contact-link",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("Hybrid editorial workstation", css)
        self.assertIn('const DIRECTOR_ORDER = ["teacher-punch", "high-energy"', script)
        self.assertIn("function selectVideoTemplate(templateId)", script)
        self.assertIn('name: "影片", kind: "source"', script)
        self.assertIn('name: "字幕", types: ["caption"]', script)

    def test_inline_effect_and_layout_adjustment_controls_are_present(self) -> None:
        html = (SKILL_DIR / "editor/index.html").read_text(encoding="utf-8")
        script = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")
        for element_id in (
            "effect-editor",
            "effect-style",
            "effect-color",
            "effect-scale",
            "add-effect-span",
            "effect-span-list",
            "overlay-max-width",
            "card-height",
            "layout-warning",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("effect_spans", script)
        self.assertIn("enableOverlayDrag", script)
        self.assertIn("renderLayoutWarning", script)
        self.assertIn("renderTranscriptStatus", script)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def write_json(self, relative: str, payload: object) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        request_headers = dict(headers or {})
        if method.upper() not in {"GET", "HEAD"} and "X-Auto-Edit-CSRF" not in request_headers:
            request_headers["X-Auto-Edit-CSRF"] = self.server.csrf_token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = response.headers
        connection.close()
        return status, response_headers, payload

    def json_request(
        self,
        method: str,
        path: str,
        payload: object,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request_headers = {"Content-Type": "application/json"}
        if method.upper() not in {"GET", "HEAD"}:
            request_headers["X-Auto-Edit-CSRF"] = self.server.csrf_token
        request_headers.update(headers or {})
        status, _response_headers, body = self.request(
            method,
            path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            request_headers,
        )
        return status, json.loads(body.decode("utf-8"))

    def write_approved_highlight_plan(self) -> list[str]:
        clip_ids = ["highlight-plan-first", "highlight-plan-second"]
        self.write_json(
            "working/highlight_plan.json",
            {
                "schema_version": 1,
                "plan_revision": "c" * 64,
                "items": [
                    {
                        "id": clip_ids[0],
                        "start": 0.1,
                        "end": 0.9,
                        "title": "第一段精華",
                        "review_status": "approved",
                        "score": 0.9,
                    },
                    {
                        "id": "highlight-plan-pending",
                        "start": 0.3,
                        "end": 0.7,
                        "title": "尚待確認",
                        "review_status": "pending",
                        "score": 0.85,
                    },
                    {
                        "id": clip_ids[1],
                        "start": 0.9,
                        "end": 1.7,
                        "title": "第二段精華",
                        "review_status": "approved",
                        "score": 0.8,
                    },
                    {
                        "id": "highlight-plan-rejected",
                        "start": 1.1,
                        "end": 1.5,
                        "title": "已排除",
                        "review_status": "rejected",
                        "score": 0.7,
                    },
                ],
            },
        )
        (self.project / "working/editor_state.json").unlink(missing_ok=True)
        return clip_ids

    def approve_batch_prerequisites(self) -> tuple[dict[str, object], list[str]]:
        clip_ids = self.write_approved_highlight_plan()
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [{"candidate_id": "edit-0001", "action": "keep"}],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, destructive = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
            },
        )
        self.assertEqual(status, 200, destructive)
        status, highlights = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "highlight_selection",
                "expected_revision": destructive["approval_revisions"]["highlight_selection"],
            },
        )
        self.assertEqual(status, 200, highlights)
        status, timeline = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "timeline",
                "expected_revision": highlights["approval_revisions"]["timeline"],
            },
        )
        self.assertEqual(status, 200, timeline)
        return state, clip_ids

    def wait_for_render_terminal(self, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = dict(self.server.render_status)
            if status.get("state") != "running":
                return status
            time.sleep(0.01)
        self.fail(f"render did not finish: {self.server.render_status}")

    def fake_batch_subprocess(
        self,
        *,
        fail_render_number: int | None = None,
        fail_qa_number: int | None = None,
        after_render: object | None = None,
    ):
        render_count = 0
        qa_count = 0

        def fake_run(command, **_kwargs):
            nonlocal render_count, qa_count
            script_name = Path(str(command[1])).name
            if script_name == "render_editor_timeline.py":
                render_count += 1
                if fail_render_number == render_count:
                    return subprocess.CompletedProcess(command, 1, "", "synthetic render failure")
                output = Path(command[command.index("--output") + 1])
                snapshot = Path(command[command.index("--snapshot") + 1])
                clip_id = json.loads(snapshot.read_text(encoding="utf-8"))["clip"]["id"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(f"fake-mp4:{clip_id}".encode("utf-8"))
                if callable(after_render):
                    after_render(render_count)
                return subprocess.CompletedProcess(command, 0, "", "")
            if script_name == "qa_video.py":
                qa_count += 1
                report = Path(command[command.index("--report") + 1])
                contact = Path(command[command.index("--contact") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                qa_failed = fail_qa_number == qa_count
                report.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "fail" if qa_failed else "pass",
                            "warnings": [],
                            "failures": ["synthetic QA failure"] if qa_failed else [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                contact.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-contact")
                return subprocess.CompletedProcess(
                    command,
                    2 if qa_failed else 0,
                    "",
                    "synthetic QA failure" if qa_failed else "",
                )
            raise AssertionError(f"unexpected subprocess: {command}")

        return fake_run

    def test_project_bootstrap_exposes_presets_and_caption_layer(self) -> None:
        status, headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(len(payload["platform_presets"]), 6)
        self.assertEqual(len(payload["director_presets"]), 5)
        self.assertEqual(len(payload["video_templates"]), 8)
        self.assertIn("cutout", payload["template_capabilities"])
        self.assertEqual(payload["state"]["video_template"]["id"], "dynamic-craft")
        self.assertTrue(
            any(voice["voice_id"] == "rumi" for voice in payload["voice_catalog"]["voices"])
        )
        self.assertEqual(payload["state"]["overlays"][0]["type"], "caption")
        caption = payload["state"]["overlays"][0]
        self.assertEqual(caption["effect_spans"][0]["text"], "差別")
        self.assertEqual(caption["effect_spans"][0]["style"]["effect"], "pop")
        self.assertEqual(payload["state"]["editing_brief"], "")
        self.assertIn("highlight_plan", payload)
        self.assertEqual(payload["transcript_calibration"]["correction_count"], 3)
        self.assertEqual(payload["transcript_review"]["risk_status"], "review_required")
        self.assertEqual(
            payload["transcript_semantic_review"]["coverage_status"],
            "complete",
        )
        self.assertEqual(payload["pipeline_status"]["state"], "not_started")
        self.assertEqual(
            {preset["label"] for preset in payload["director_presets"].values()},
            {"專業教學", "爆款短影音", "八卦時事", "POV 藏鏡人", "編輯精簡"},
        )
        self.assertEqual(
            {item["type"] for item in payload["state"]["overlays"]},
            {"caption", "emphasis", "title"},
        )
        self.assertTrue((self.project / "working/editor_state.json").is_file())

    def test_pipeline_status_exposes_semantic_caption_progress(self) -> None:
        self.write_json(
            "working/pipeline_status.json",
            {
                "state": "running",
                "phase": "semantic_calibration",
                "message": "正在校準字幕…",
            },
        )
        self.write_json(
            "working/transcript_semantic_review.json",
            {
                "status": "running",
                "reviewed_unit_count": 42,
                "total_unit_count": 116,
                "candidate_count": 7,
                "model_error_count": 0,
            },
        )

        status, _headers, body = self.request("GET", "/api/pipeline-status")
        payload = json.loads(body.decode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["semantic_progress"]["reviewed_unit_count"], 42)
        self.assertIn("42/116", payload["message"])

    def test_state_validation_accepts_valid_effect_spans_and_rejects_stale_offsets(self) -> None:
        state = {
            "schema_version": 2,
            "segments": [
                {
                    "id": "segment-abcdef012345",
                    "source_start": 0.0,
                    "source_end": 2.0,
                    "origin": "default_full_source",
                }
            ],
            "variants": [],
            "rights": {"asserted": False, "assertion_revision": None},
            "director_style": "teacher-punch",
            "visual_quality_mode": "basic",
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "fit": "contain",
            },
            "overlays": [
                {
                    "id": "caption-1",
                    "type": "caption",
                    "start": 0.1,
                    "end": 1.0,
                    "text": "看到 It",
                    "layout": {"x": 50, "y": 50, "width": 80, "height": 20},
                    "effect_spans": [
                        {
                            "id": "fx-it",
                            "text": "It",
                            "start_char": 3,
                            "end_char": 5,
                            "style": {"effect": "pop", "color": "#ffb000", "font_scale": 1.2},
                        }
                    ],
                }
            ],
        }
        self.assertEqual(validate_editor_state(state, 2.0), [])
        state["overlays"][0]["effect_spans"][0]["start_char"] = 2
        errors = validate_editor_state(state, 2.0)
        self.assertTrue(any("effect span text does not match" in error for error in errors), errors)

    def test_rumi_voice_selection_is_saved_without_synthesis(self) -> None:
        status, payload = self.json_request(
            "PUT",
            "/api/voice-selection",
            {
                "enabled": True,
                "language": "zh-TW",
                "gender": "female",
                "provider": "rumi",
                "voice_id": "rumi",
                "mode": "replace",
                "speed": 1.0,
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["generated"])
        self.assertEqual(payload["voiceover"]["engine"], "rumi-voice-system")
        self.assertEqual(payload["voiceover"]["voice_id"], "rumi")
        self.assertTrue(payload["voiceover"]["cloud_consent_required"])
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["voiceover"]["selection_status"], "resolved_not_generated")

        status, payload = self.json_request(
            "PUT",
            "/api/voice-selection",
            {
                "enabled": True,
                "language": "zh-TW",
                "gender": "female",
                "provider": "rumi",
                "voice_id": "not-allowed",
                "mode": "replace",
                "speed": 1.0,
            },
        )
        self.assertEqual(status, 422)
        self.assertIn("allowed shared catalog", str(payload["error"]))

    def test_editor_can_replan_highlights_with_selected_director_and_brief(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        bootstrap = json.loads(body.decode("utf-8"))
        status, planned = self.json_request(
            "POST",
            "/api/plan-highlights",
            {
                "director": "high-energy",
                "brief": "保留最明確的差別",
                "count": 3,
                "expected_revision": bootstrap["state"]["revision"],
            },
        )
        self.assertEqual(status, 200, planned)
        self.assertEqual(
            planned["highlight_plan"]["configuration"]["director_profile"],
            "high-energy",
        )
        self.assertEqual(
            planned["highlight_plan"]["configuration"]["editing_brief"],
            "保留最明確的差別",
        )
        self.assertEqual(planned["state"]["director_style"], "high-energy")
        self.assertLessEqual(len(planned["state"]["highlights"]), 3)

    def test_media_range_request(self) -> None:
        status, headers, body = self.request(
            "GET",
            "/media/source",
            headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 2-5/16")
        self.assertEqual(body, b"2345")

    def test_scoped_routes_reject_traversal(self) -> None:
        status, _headers, _body = self.request("GET", "/assets/../project.json")
        self.assertEqual(status, 403)
        status, _headers, _body = self.request(
            "GET", "/renders/%2e%2e/working/editor_state.json"
        )
        self.assertEqual(status, 403)

    def test_source_symlink_outside_project_is_rejected(self) -> None:
        outside = Path(self._tmp.name) / "outside.mp4"
        outside.write_bytes(b"private-outside-project")
        source = self.project / "source/source.mp4"
        source.unlink()
        source.symlink_to(outside)
        status, _headers, _body = self.request("GET", "/media/source")
        self.assertEqual(status, 403)

    def test_cross_origin_write_and_rebinding_host_are_rejected(self) -> None:
        status, payload = self.json_request(
            "POST",
            "/api/copy-draft",
            {"platform_id": "instagram-reels"},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", str(payload["error"]))

        status, _headers, _body = self.request(
            "GET",
            "/api/health",
            headers={"Host": f"evil.example:{self.port}"},
        )
        self.assertEqual(status, 403)

    def test_state_save_asset_scope_upload_and_final_gate(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["overlays"][0]["style"]["font_size"] = 72
        status, payload = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, payload)

        invalid_state = json.loads(json.dumps(state))
        invalid_state["overlays"].append(
            {
                "id": "image-leak",
                "type": "image",
                "start": 0.2,
                "end": 1.0,
                "source": "working/editor_state.json",
                "visible": True,
                "style": {"x": 50, "y": 50, "width": 30},
            }
        )
        status, payload = self.json_request("PUT", "/api/editor-state", invalid_state)
        self.assertEqual(status, 422)
        self.assertIn("under assets", " ".join(payload["errors"]))

        upload_body = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        upload_path = "/api/assets?filename=" + urllib.parse.quote("icon.png")
        status, _headers, body = self.request(
            "POST",
            upload_path,
            upload_body,
            {"Content-Type": "image/png"},
        )
        self.assertEqual(status, 200)
        upload = json.loads(body.decode("utf-8"))
        self.assertTrue((self.project / upload["source"]).is_file())
        provenance = json.loads(
            (self.project / "assets/provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["items"][0]["source"], "user-uploaded-through-local-editor")
        expected_digest = hashlib.sha256(upload_body).hexdigest()
        self.assertEqual(upload["sha256"], expected_digest)
        self.assertEqual(provenance["items"][0]["sha256"], expected_digest)

        status, _headers, body = self.request("GET", "/api/project")
        state_with_asset = json.loads(body.decode("utf-8"))["state"]
        state_with_asset["overlays"].append(
            {
                "id": "image-owned",
                "type": "image",
                "start": 0.2,
                "end": 1.0,
                "source": upload["source"],
                "visible": True,
                "z_index": 30,
                "style": {"x": 50, "y": 50, "width": 30},
            }
        )
        state_with_asset["video_template"] = {
            "id": "cutout-image",
            "frame": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
            "subject": {"x": 50, "y": 54, "scale": 1.0, "feather": 2, "mask_stride": 3},
            "background": {"color": "#17251d", "source": upload["source"], "fit": "cover", "blur": 0},
        }
        status, saved = self.json_request("PUT", "/api/editor-state", state_with_asset)
        self.assertEqual(status, 200, saved)
        persisted = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["asset_digests"][upload["source"]], expected_digest)

        original_revision = persisted["revision"]
        persisted["video_template"]["subject"]["x"] = 61
        self.assertNotEqual(editor_state_revision(persisted), original_revision)

        status, payload = self.json_request(
            "POST",
            "/api/render",
            {"quality": "final", "expected_revision": persisted["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertIn("approved", str(payload["error"]))

    def test_asset_upload_rejects_mime_mismatch_and_fake_video(self) -> None:
        path = "/api/assets?filename=" + urllib.parse.quote("payload.mp4")
        status, _headers, _body = self.request(
            "POST",
            path,
            b"not-a-video",
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        status, _headers, _body = self.request(
            "POST",
            path,
            b"not-a-video",
            {"Content-Type": "video/mp4"},
        )
        self.assertEqual(status, 415)
        self.assertFalse(any(item.name.startswith("payload-") for item in (self.project / "assets").iterdir()))

    def test_cutout_asset_template_fails_closed_before_render(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["video_template"] = {
            "id": "cutout-image",
            "frame": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
            "subject": {"x": 50, "y": 54, "scale": 1.0, "feather": 2, "mask_stride": 3},
            "background": {"color": "#17251d", "source": None, "fit": "cover", "blur": 0},
        }
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)
        status, rejected = self.json_request(
            "POST",
            "/api/render",
            {"quality": "preview", "expected_revision": saved["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertIn("image background asset", rejected["error"])

    def test_timeline_approval_is_bound_to_render_state_revision(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        bootstrap = json.loads(body.decode("utf-8"))
        state = bootstrap["state"]

        status, missing_cas = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "timeline", "confirmed_by": "unit-test"},
        )
        self.assertEqual(status, 409)
        self.assertIn("expected_revision", str(missing_cas["error"]))

        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [
                    {
                        "candidate_id": "edit-0001",
                        "action": "keep",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, destructive = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
                "confirmed_by": "unit-test",
            },
        )
        self.assertEqual(status, 200, destructive)
        status, approval = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "timeline",
                "expected_revision": destructive["approval_revisions"]["timeline"],
                "confirmed_by": "unit-test",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(approval["approval"]["state_revision"])

        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200)
        self.assertEqual(saved["invalidated_gates"], [])

        state["overlays"][0]["text"] = "已修改的字幕"
        state["overlays"][0]["effect_spans"] = []
        state["overlays"][0]["emphasis"] = []
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200)
        self.assertIn("timeline", saved["invalidated_gates"])
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["approvals"]["timeline"]["approved"])

    def test_final_render_fails_closed_when_reviewed_deletes_are_unapplied(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [{"candidate_id": "edit-0001", "action": "delete"}],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, approved = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
            },
        )
        self.assertEqual(status, 200, approved)
        status, blocked = self.json_request(
            "POST",
            "/api/render",
            {
                "quality": "final",
                "expected_revision": state["revision"],
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("not applied", str(blocked["error"]))

    def test_highlight_approval_requires_current_plan_review_and_cas(self) -> None:
        plan_revision = "c" * 64
        highlight_id = "highlight-abcdef123456"
        self.write_json(
            "working/highlight_plan.json",
            {
                "schema_version": 1,
                "plan_revision": plan_revision,
                "items": [
                    {
                        "id": highlight_id,
                        "start": 0.1,
                        "end": 1.2,
                        "title": "雪茄與香菸的差別",
                        "review_status": "pending",
                        "score": 0.9,
                    }
                ],
            },
        )
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["highlights"][0]["review_status"] = "approved"
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)

        status, blocked = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "highlight_selection",
                "expected_revision": saved["approval_revisions"]["highlight_selection"],
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("destructive_edit", str(blocked["error"]))

        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [{"candidate_id": "edit-0001", "action": "keep"}],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, destructive = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
            },
        )
        self.assertEqual(status, 200, destructive)

        status, stale = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "highlight_selection", "expected_revision": "0" * 64},
        )
        self.assertEqual(status, 409)
        self.assertIn("stale", str(stale["error"]))

        status, approved = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "highlight_selection",
                "expected_revision": destructive["approval_revisions"]["highlight_selection"],
            },
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["approval"]["plan_revision"], plan_revision)

        state["highlights"][0]["end"] = 1.1
        status, changed = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, changed)
        self.assertIn("highlight_selection", changed["invalidated_gates"])

    def test_final_approval_requires_current_untampered_delivery_qa(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [{"candidate_id": "edit-0001", "action": "keep"}],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, destructive = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
            },
        )
        self.assertEqual(status, 200, destructive)
        status, timeline = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "timeline",
                "expected_revision": destructive["approval_revisions"]["timeline"],
            },
        )
        self.assertEqual(status, 200, timeline)
        status, blocked = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "final",
                "expected_revision": timeline["approval_revisions"]["final"],
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("delivery QA", str(blocked["error"]))

        render_id = "render_delivery_test"
        state_revision = editor_state_revision(state)
        output = self.project / "renders/final-test.mp4"
        report = self.project / "qa/final-test-report.json"
        contact = self.project / "qa/final-test-contact.png"
        render_receipt = self.project / "working/render_receipts/render_delivery_test.json"
        output.write_bytes(b"verified-final-output")
        self.write_json("qa/final-test-report.json", {"schema_version": 1, "status": "pass"})
        contact.write_bytes(b"\x89PNG\r\n\x1a\nverified-contact")
        self.write_json(
            "working/render_receipts/render_delivery_test.json",
            {
                "schema_version": 1,
                "render_id": render_id,
                "quality": "final",
                "state_revision": state_revision,
                "output": "renders/final-test.mp4",
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
        )
        delivery = {
            "schema_version": 1,
            "render_id": render_id,
            "quality": "final",
            "state_revision": state_revision,
            "status": "pass",
            "output": "renders/final-test.mp4",
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "report": "qa/final-test-report.json",
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "contact_sheet": "qa/final-test-contact.png",
            "contact_sheet_sha256": hashlib.sha256(contact.read_bytes()).hexdigest(),
            "render_receipt": "working/render_receipts/render_delivery_test.json",
            "render_receipt_sha256": hashlib.sha256(render_receipt.read_bytes()).hexdigest(),
        }
        self.write_json("working/latest_final_qa.json", delivery)

        final_revision = gate_revision(self.project, "final", state)
        self.assertNotEqual(final_revision, state_revision)
        status, approved = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "final",
                "expected_revision": final_revision,
                "confirmed_by": "unit-test",
            },
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["approval"]["state_revision"], final_revision)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["render"], "complete")
        self.assertEqual(manifest["stages"]["qa"], "complete")

        output.write_bytes(b"tampered-after-qa")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "final",
                "expected_revision": final_revision,
                "confirmed_by": "unit-test",
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("changed after verification", str(rejected["error"]))
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        bootstrap = json.loads(body.decode("utf-8"))
        self.assertFalse(bootstrap["approval_current"]["final"])

    def test_batch_render_requires_cas_and_current_human_gates(self) -> None:
        self.write_approved_highlight_plan()
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]

        status, missing_cas = self.json_request(
            "POST",
            "/api/render-batch",
            {"quality": "final"},
        )
        self.assertEqual(status, 409)
        self.assertIn("expected_revision", str(missing_cas["error"]))

        status, blocked = self.json_request(
            "POST",
            "/api/render-batch",
            {"quality": "final", "expected_revision": state["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertIn("destructive_edit", str(blocked["error"]))
        self.assertEqual(self.server.render_status["state"], "idle")

    def test_batch_render_checks_visual_contract_for_every_approved_clip(self) -> None:
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["source"]["duration_s"] = 40.0
        self.write_json("project.json", manifest)
        clip_ids = ["highlight-visual-first", "highlight-visual-second"]
        self.write_json(
            "working/highlight_plan.json",
            {
                "schema_version": 1,
                "plan_revision": "d" * 64,
                "items": [
                    {
                        "id": clip_ids[0],
                        "start": 0.1,
                        "end": 16.1,
                        "title": "第一段有完整視覺",
                        "review_status": "approved",
                    },
                    {
                        "id": clip_ids[1],
                        "start": 16.2,
                        "end": 32.2,
                        "title": "第二段缺少視覺",
                        "review_status": "approved",
                    },
                ],
            },
        )
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["overlays"] = [
            item
            for item in state["overlays"]
            if item.get("highlight_id") != clip_ids[1]
        ]
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)
        status, decisions = self.json_request(
            "PUT",
            "/api/edit-decisions",
            {
                "approved": True,
                "items": [{"candidate_id": "edit-0001", "action": "keep"}],
            },
        )
        self.assertEqual(status, 200, decisions)
        status, destructive = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "destructive_edit",
                "expected_revision": decisions["approval_revision"],
            },
        )
        self.assertEqual(status, 200, destructive)
        status, highlights = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "highlight_selection",
                "expected_revision": destructive["approval_revisions"]["highlight_selection"],
            },
        )
        self.assertEqual(status, 200, highlights)
        status, timeline = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "timeline",
                "expected_revision": highlights["approval_revisions"]["timeline"],
            },
        )
        self.assertEqual(status, 200, timeline)

        status, blocked = self.json_request(
            "POST",
            "/api/render-batch",
            {"quality": "final", "expected_revision": saved["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertIn(clip_ids[1], str(blocked["error"]))
        self.assertIn("five", str(blocked["error"]))

    def test_batch_render_aggregates_every_clip_and_tampering_fails_closed(self) -> None:
        state, clip_ids = self.approve_batch_prerequisites()
        fake_run = self.fake_batch_subprocess()
        with (
            patch("editor_server.subprocess.run", side_effect=fake_run),
            patch("editor_server.ffprobe_has_visual_stream", return_value=True),
        ):
            status, accepted = self.json_request(
                "POST",
                "/api/render-batch",
                {"quality": "final", "expected_revision": state["revision"]},
            )
            self.assertEqual(status, 202, accepted)
            self.assertEqual(accepted["status"]["clip_ids"], clip_ids)
            completed = self.wait_for_render_terminal()

        self.assertEqual(completed["state"], "complete", completed)
        self.assertEqual(completed["completed_clips"], 2)
        delivery = json.loads(
            (self.project / "working/latest_final_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(delivery["schema_version"], 2)
        self.assertEqual(delivery["kind"], "batch")
        self.assertEqual(delivery["delivery_kind"], "batch")
        self.assertEqual(delivery["clip_ids"], clip_ids)
        self.assertEqual(delivery["item_count"], 2)
        self.assertEqual([item["clip_id"] for item in delivery["items"]], clip_ids)
        self.assertEqual(len(delivery["items"]), 2)
        for item in delivery["items"]:
            self.assertTrue((self.project / item["output"]).is_file())
            self.assertTrue((self.project / item["report"]).is_file())
            self.assertTrue((self.project / item["contact_sheet"]).is_file())
            self.assertTrue((self.project / item["render_receipt"]).is_file())
        archive = self.project / delivery["archive"]
        self.assertTrue(archive.is_file())
        self.assertEqual(delivery["archive_download_name"], archive.name)
        with zipfile.ZipFile(archive, "r") as bundle:
            self.assertEqual(
                bundle.namelist(),
                [item["archive_name"] for item in delivery["items"]],
            )

        final_revision = gate_revision(self.project, "final", state)
        status, approved = self.json_request(
            "POST",
            "/api/approve",
            {
                "gate": "final",
                "expected_revision": final_revision,
                "confirmed_by": "unit-test",
            },
        )
        self.assertEqual(status, 200, approved)

        second_output = self.project / delivery["items"][1]["output"]
        original_output = second_output.read_bytes()
        second_output.write_bytes(b"tampered-batch-output")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": final_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("changed after verification", str(rejected["error"]))
        second_output.write_bytes(original_output)

        second_contact = self.project / delivery["items"][1]["contact_sheet"]
        original_contact = second_contact.read_bytes()
        second_contact.write_bytes(original_contact + b"tampered")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": final_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("contact_sheet changed after verification", str(rejected["error"]))
        second_contact.write_bytes(original_contact)

        second_report = self.project / delivery["items"][1]["report"]
        original_report = second_report.read_bytes()
        second_report.write_bytes(original_report + b"tampered")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": final_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("report changed after verification", str(rejected["error"]))
        second_report.write_bytes(original_report)

        second_receipt = self.project / delivery["items"][1]["render_receipt"]
        original_receipt = second_receipt.read_bytes()
        second_receipt.write_bytes(original_receipt + b"tampered")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": final_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("render_receipt changed after verification", str(rejected["error"]))
        second_receipt.write_bytes(original_receipt)

        original_archive = archive.read_bytes()
        archive.write_bytes(original_archive + b"tampered")
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": final_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("archive changed after verification", str(rejected["error"]))
        archive.write_bytes(original_archive)

        delivery["clip_ids"] = list(reversed(delivery["clip_ids"]))
        self.write_json("working/latest_final_qa.json", delivery)
        tampered_revision = gate_revision(self.project, "final", state)
        status, rejected = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "final", "expected_revision": tampered_revision},
        )
        self.assertEqual(status, 409)
        self.assertIn("clip set", str(rejected["error"]))

    def test_failed_batch_preserves_previous_latest_delivery(self) -> None:
        state, _clip_ids = self.approve_batch_prerequisites()
        previous = {
            "schema_version": 1,
            "status": "pass",
            "sentinel": "previous-latest-must-survive",
        }
        self.write_json("working/latest_final_qa.json", previous)
        fake_run = self.fake_batch_subprocess(fail_render_number=2)
        with (
            patch("editor_server.subprocess.run", side_effect=fake_run),
            patch("editor_server.ffprobe_has_visual_stream", return_value=True),
        ):
            status, accepted = self.json_request(
                "POST",
                "/api/render-batch",
                {"quality": "final", "expected_revision": state["revision"]},
            )
            self.assertEqual(status, 202, accepted)
            failed = self.wait_for_render_terminal()

        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["completed_clips"], 1)
        self.assertTrue(failed["previous_latest_preserved"])
        persisted = json.loads(
            (self.project / "working/latest_final_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted, previous)

    def test_qa_failed_batch_reports_attempted_items_and_preserves_previous_latest(self) -> None:
        state, clip_ids = self.approve_batch_prerequisites()
        previous = {
            "schema_version": 1,
            "status": "pass",
            "sentinel": "previous-latest-must-survive-qa-failure",
        }
        self.write_json("working/latest_final_qa.json", previous)
        fake_run = self.fake_batch_subprocess(fail_qa_number=2)
        with (
            patch("editor_server.subprocess.run", side_effect=fake_run),
            patch("editor_server.ffprobe_has_visual_stream", return_value=True),
        ):
            status, accepted = self.json_request(
                "POST",
                "/api/render-batch",
                {"quality": "final", "expected_revision": state["revision"]},
            )
            self.assertEqual(status, 202, accepted)
            failed = self.wait_for_render_terminal()

        self.assertEqual(failed["state"], "qa_failed", failed)
        self.assertEqual(failed["completed_clips"], 1)
        self.assertEqual(failed["failed_clip_id"], clip_ids[1])
        self.assertEqual(
            [item["status"] for item in failed["qa"]["items"]],
            ["pass", "fail"],
        )
        self.assertEqual(failed["qa"]["item_count"], 2)
        persisted = json.loads(
            (self.project / "working/latest_final_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted, previous)

    def test_batch_authorization_change_during_render_preserves_previous_latest(self) -> None:
        state, _clip_ids = self.approve_batch_prerequisites()
        previous = {
            "schema_version": 1,
            "status": "pass",
            "sentinel": "previous-latest-must-survive-gate-change",
        }
        self.write_json("working/latest_final_qa.json", previous)

        def change_edit_decision(render_number: int) -> None:
            if render_number != 1:
                return
            decisions = json.loads(
                (self.project / "working/edit_decisions.json").read_text(encoding="utf-8")
            )
            decisions["items"][0]["action"] = "delete"
            self.write_json("working/edit_decisions.json", decisions)

        fake_run = self.fake_batch_subprocess(after_render=change_edit_decision)
        with (
            patch("editor_server.subprocess.run", side_effect=fake_run),
            patch("editor_server.ffprobe_has_visual_stream", return_value=True),
        ):
            status, accepted = self.json_request(
                "POST",
                "/api/render-batch",
                {"quality": "final", "expected_revision": state["revision"]},
            )
            self.assertEqual(status, 202, accepted)
            failed = self.wait_for_render_terminal()

        self.assertEqual(failed["state"], "failed", failed)
        self.assertIn("destructive_edit approval changed", failed["message"])
        self.assertTrue(failed["previous_latest_preserved"])
        persisted = json.loads(
            (self.project / "working/latest_final_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted, previous)


    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _phase0_mutation_routes() -> list[tuple[str, str]]:
        return [
            ("PUT", "/api/editor-state"),
            ("PUT", "/api/edit-decisions"),
            ("PUT", "/api/voice-selection"),
            ("POST", "/api/assets?filename=a.png"),
            ("POST", "/api/copy-draft"),
            ("POST", "/api/plan-highlights"),
            ("POST", "/api/approve"),
            ("POST", "/api/render"),
            ("POST", "/api/render-batch"),
            ("POST", "/api/cover"),
        ]

    def test_every_mutation_route_rejects_missing_or_wrong_csrf(self) -> None:
        for method, path in self._phase0_mutation_routes():
            for token in ("", "wrong-token"):
                status, _headers, body = self.request(
                    method,
                    path,
                    b"{}",
                    {"Content-Type": "application/json", "X-Auto-Edit-CSRF": token},
                )
                self.assertEqual(
                    status, 403, f"{method} {path} with token {token!r} returned {status}"
                )
                self.assertIn("CSRF", body.decode("utf-8"))

    def test_project_get_exposes_csrf_token(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["csrf_token"], self.server.csrf_token)

    def test_v1_state_migrates_on_project_get_and_voids_every_gate(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        for key in ("segments", "variants", "rights", "migrated_from"):
            state.pop(key, None)
        state["schema_version"] = 1
        self.write_json("working/editor_state.json", state)
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest["approvals"] = {
            gate: {"approved": True, "state_revision": "0" * 64}
            for gate in ("destructive_edit", "highlight_selection", "timeline", "final")
        }
        self.write_json("project.json", manifest)

        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        migrated = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["segments"][0]["origin"], "default_full_source")
        self.assertAlmostEqual(migrated["segments"][0]["source_end"], 2.0)
        self.assertEqual(migrated["variants"], [])
        self.assertFalse(migrated["rights"]["asserted"])
        self.assertEqual(migrated["migrated_from"]["schema_version"], 1)
        manifest_after = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        for gate in ("destructive_edit", "highlight_selection", "timeline", "final"):
            approval = manifest_after["approvals"][gate]
            self.assertFalse(
                approval["approved"], f"gate {gate} must not survive migration"
            )
            self.assertIn("migration", str(approval.get("note", "")))

    def test_put_of_v1_state_is_rejected(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        state["schema_version"] = 1
        status, payload = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 422)
        self.assertTrue(
            any("migrated" in str(error) for error in payload.get("errors", [])),
            payload,
        )

    def test_render_download_gate_blocks_unapproved_and_unknown_finals(self) -> None:
        (self.project / "renders/preview-1.mp4").write_bytes(b"preview-bytes")
        (self.project / "renders/final-1.mp4").write_bytes(b"final-bytes")
        (self.project / "renders/mystery.mp4").write_bytes(b"mystery-bytes")
        (self.project / "renders/cover.png").write_bytes(b"cover-bytes")
        self.write_json(
            "working/render_receipts/render-prev.json",
            {
                "schema_version": 1,
                "render_id": "render-prev",
                "quality": "preview",
                "output": "renders/preview-1.mp4",
            },
        )
        self.write_json(
            "working/render_receipts/render-fin.json",
            {
                "schema_version": 1,
                "render_id": "render-fin",
                "quality": "final",
                "output": "renders/final-1.mp4",
            },
        )
        status, _headers, _body = self.request("GET", "/renders/preview-1.mp4")
        self.assertEqual(status, 200)
        status, _headers, _body = self.request("GET", "/renders/cover.png")
        self.assertEqual(status, 200)
        status, _headers, body = self.request("GET", "/renders/final-1.mp4")
        self.assertEqual(status, 403)
        self.assertIn("final", body.decode("utf-8"))
        status, _headers, _body = self.request("GET", "/renders/mystery.mp4")
        self.assertEqual(status, 403)

    def _install_synthetic_final_delivery(self) -> dict[str, object]:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state_revision = editor_state_revision(state)
        output_bytes = b"approved-final-bytes"
        (self.project / "renders/final-ok.mp4").write_bytes(output_bytes)
        report_payload = {"status": "pass", "failures": [], "warnings": []}
        report_text = json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n"
        (self.project / "qa/final-ok.json").write_text(report_text, encoding="utf-8")
        contact_bytes = b"contact-sheet-bytes"
        (self.project / "qa/final-ok-contact.png").write_bytes(contact_bytes)
        render_receipt = {
            "schema_version": 1,
            "render_id": "render-ok",
            "quality": "final",
            "clip_id": "",
            "state_revision": state_revision,
            "output": "renders/final-ok.mp4",
            "output_sha256": self._sha256_bytes(output_bytes),
        }
        self.write_json("working/render_receipts/render-ok.json", render_receipt)
        receipt_bytes = (
            self.project / "working/render_receipts/render-ok.json"
        ).read_bytes()
        delivery = {
            "schema_version": 1,
            "render_id": "render-ok",
            "quality": "final",
            "clip_id": "",
            "state_revision": state_revision,
            "status": "pass",
            "output": "renders/final-ok.mp4",
            "output_sha256": self._sha256_bytes(output_bytes),
            "report": "qa/final-ok.json",
            "report_sha256": self._sha256_bytes(report_text.encode("utf-8")),
            "contact_sheet": "qa/final-ok-contact.png",
            "contact_sheet_sha256": self._sha256_bytes(contact_bytes),
            "render_receipt": "working/render_receipts/render-ok.json",
            "render_receipt_sha256": self._sha256_bytes(receipt_bytes),
            "warnings": [],
            "failures": [],
            "visual_quality": None,
            "human_review_required": True,
        }
        self.write_json("working/latest_final_qa.json", delivery)
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest.setdefault("approvals", {})["final"] = {
            "approved": True,
            "state_revision": gate_revision(self.project, "final"),
        }
        self.write_json("project.json", manifest)
        return state

    def test_current_final_approval_unlocks_receipt_bound_download_only(self) -> None:
        (self.project / "qa/stale-old.json").write_text(
            json.dumps({"status": "pass"}), encoding="utf-8"
        )
        status, _headers, _body = self.request("GET", "/qa/stale-old.json")
        self.assertEqual(status, 200, "QA evidence must stay readable before approval")

        state = self._install_synthetic_final_delivery()
        status, _headers, _body = self.request("GET", "/renders/final-ok.mp4")
        self.assertEqual(status, 200, "current approved final must be downloadable")
        status, _headers, _body = self.request("GET", "/qa/final-ok.json")
        self.assertEqual(status, 200)
        status, _headers, _body = self.request("GET", "/qa/stale-old.json")
        self.assertEqual(
            status, 403, "stale QA evidence must be blocked once final is approved"
        )

        tampered = json.loads(json.dumps(state))
        tampered.setdefault("caption_defaults", {})["font_size"] = 55
        status, _payload = self.json_request("PUT", "/api/editor-state", tampered)
        self.assertEqual(status, 200)
        status, _headers, _body = self.request("GET", "/renders/final-ok.mp4")
        self.assertEqual(
            status, 403, "state change must re-lock the final download gate"
        )

    def test_tampered_final_output_is_blocked_even_with_approval(self) -> None:
        self._install_synthetic_final_delivery()
        status, _headers, _body = self.request("GET", "/renders/final-ok.mp4")
        self.assertEqual(status, 200)
        (self.project / "renders/final-ok.mp4").write_bytes(b"tampered-bytes")
        status, _headers, _body = self.request("GET", "/renders/final-ok.mp4")
        self.assertEqual(status, 403, "hash mismatch must block the download")

    def test_final_receipt_wins_over_preview_receipt_for_same_path(self) -> None:
        (self.project / "renders/collide.mp4").write_bytes(b"collide-bytes")
        self.write_json(
            "working/render_receipts/render-a.json",
            {
                "schema_version": 1,
                "render_id": "render-a",
                "quality": "preview",
                "output": "renders/collide.mp4",
            },
        )
        self.write_json(
            "working/render_receipts/render-b.json",
            {
                "schema_version": 1,
                "render_id": "render-b",
                "quality": "final",
                "output": "renders/collide.mp4",
            },
        )
        status, _headers, _body = self.request("GET", "/renders/collide.mp4")
        self.assertEqual(
            status, 403, "a path with any final receipt must be gated as final"
        )

    def test_batch_archive_requires_final_approval(self) -> None:
        self.write_json(
            "working/latest_final_qa.json",
            {
                "schema_version": 2,
                "status": "pass",
                "archive": "renders/batch.zip",
                "archive_sha256": "b" * 64,
                "items": [],
            },
        )
        errors = render_download_errors(self.project, "renders/batch.zip")
        self.assertTrue(errors)
        self.assertIn("final", errors[0])

    def test_migration_state_write_failure_leaves_recoverable_v1(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        for key in ("segments", "variants", "rights", "migrated_from"):
            state.pop(key, None)
        state["schema_version"] = 1
        self.write_json("working/editor_state.json", state)
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest["approvals"] = {
            gate: {"approved": True, "state_revision": "0" * 64}
            for gate in ("destructive_edit", "highlight_selection", "timeline", "final")
        }
        self.write_json("project.json", manifest)

        real_write = editor_server.atomic_write_json

        def explode_on_state(path, payload):
            if path.name == "editor_state.json":
                raise OSError("disk full (simulated)")
            real_write(path, payload)

        with patch.object(editor_server, "atomic_write_json", explode_on_state):
            with self.assertRaises(OSError):
                migrate_editor_state_v1_to_v2(
                    self.project,
                    json.loads(
                        (self.project / "project.json").read_text(encoding="utf-8")
                    ),
                    json.loads(
                        (self.project / "working/editor_state.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                )
        on_disk_state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            on_disk_state["schema_version"], 1, "failed migration must keep v1 on disk"
        )
        manifest_after = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        for gate in ("destructive_edit", "highlight_selection", "timeline", "final"):
            self.assertFalse(
                manifest_after["approvals"][gate]["approved"],
                "approvals must be voided before the state write can fail",
            )
        status, _headers, _body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        recovered = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            recovered["schema_version"], 2, "next load must complete the migration"
        )

    def test_v1_state_fails_closed_outside_the_editor_page(self) -> None:
        with self.assertRaises(ValueError):
            gate_revision(self.project, "timeline", {"schema_version": 1})
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        state["schema_version"] = 1
        for key in ("segments", "variants", "rights", "migrated_from"):
            state.pop(key, None)
        self.write_json("working/editor_state.json", state)
        status, _headers, body = self.request("GET", "/api/approval-revisions")
        self.assertEqual(status, 200)
        revisions = json.loads(body.decode("utf-8"))["revisions"]
        self.assertEqual(revisions["timeline"], "unmigrated-editor-state-v1")



class EditorRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.ffprobe = shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("ffmpeg/ffprobe are unavailable")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-render-tests-")
        self.project = Path(self._tmp.name) / "project"
        for name in ("source", "working", "assets", "renders"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        source = self.project / "source/source.mp4"
        result = subprocess.run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x3b332d:s=360x640:d=0.45",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        self.write_json(
            "project.json",
            {
                "project_id": "render-test",
                "source": {"staged_path": "source/source.mp4", "duration_s": 0.45},
                "approvals": {"timeline": {"approved": False}},
            },
        )
        self.write_json(
            "working/editor_state.json",
            {
                "schema_version": 2,
                "segments": [
                    {
                        "id": "segment-abcdef012345",
                        "source_start": 0.0,
                        "source_end": 0.45,
                        "origin": "default_full_source",
                    }
                ],
                "variants": [],
                "rights": {"asserted": False, "assertion_revision": None},
                "canvas": {
                    "platform_id": "instagram-reels",
                    "width": 360,
                    "height": 640,
                    "fps": 30,
                    "fit": "cover",
                },
                "caption_defaults": {
                    "font_size": 42,
                    "color": "#f7f2e8",
                    "stroke_color": "#17130f",
                    "stroke_width": 3,
                },
                "overlays": [
                    {
                        "id": "caption-1",
                        "type": "caption",
                        "start": 0.02,
                        "end": 0.40,
                        "text": "核心重點",
                        "visible": True,
                        "z_index": 20,
                        "style": {
                            "font_size": 42,
                            "color": "#f7f2e8",
                            "stroke_color": "#17130f",
                            "stroke_width": 3,
                            "x": 50,
                            "y": 78,
                            "max_width": 84,
                            "animation": "fade",
                        },
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_json(self, relative: str, payload: object) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_renderer(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "render_editor_timeline.py"),
                "--project-dir",
                str(self.project),
                *args,
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def test_preview_cover_and_final_approval_gate(self) -> None:
        preview = self.project / "renders/preview.mp4"
        self.run_renderer("--quality", "preview", "--output", str(preview))
        self.assertTrue(preview.is_file())

        final = self.project / "renders/final.mp4"
        self.run_renderer("--quality", "final", "--output", str(final), expected=2)
        self.assertFalse(final.is_file())
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": editor_state_revision(state),
        }
        self.write_json("project.json", manifest)
        self.run_renderer("--quality", "final", "--output", str(final))
        self.assertTrue(final.is_file())
        dimensions = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(final),
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(dimensions, "360x640")

        cover = self.project / "renders/cover.png"
        self.run_renderer(
            "--cover",
            "--platform",
            "instagram-reels",
            "--cover-time",
            "0.1",
            "--cover-text",
            "測試封面",
            "--output",
            str(cover),
        )
        self.assertTrue(cover.is_file())

    def test_snapshot_render_trims_to_selected_highlight_and_preserves_last_good_output(self) -> None:
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        highlight = {
            "id": "highlight-abcdef123456",
            "plan_item_id": "highlight-abcdef123456",
            "start": 0.10,
            "end": 0.32,
            "title": "核心重點",
            "review_status": "approved",
            "score": 0.9,
            "source": "working/highlight_plan.json",
        }
        state.update(
            {
                "source_sha256": None,
                "highlight_plan_revision": "c" * 64,
                "active_highlight_id": highlight["id"],
                "highlights": [highlight],
                "asset_digests": {},
            }
        )
        revision = editor_state_revision(state)
        snapshot = self.project / "working/render_snapshots/render_test.json"
        self.write_json(
            "working/render_snapshots/render_test.json",
            {
                "schema_version": 1,
                "render_id": "render_test",
                "quality": "preview",
                "project_id": "render-test",
                "state_revision": revision,
                "approval_revisions": {},
                "authorization": {},
                "clip": {
                    key: highlight[key]
                    for key in (
                        "id",
                        "plan_item_id",
                        "start",
                        "end",
                        "title",
                        "review_status",
                    )
                },
                "manifest": manifest,
                "state": state,
            },
        )
        output = self.project / "renders/highlight-preview.mp4"
        self.run_renderer(
            "--quality",
            "preview",
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
        )
        rendered_duration = float(
            subprocess.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        self.assertGreater(rendered_duration, 0.15)
        self.assertLess(rendered_duration, 0.36)
        last_good_hash = hashlib.sha256(output.read_bytes()).hexdigest()

        broken = json.loads(snapshot.read_text(encoding="utf-8"))
        broken["state_revision"] = "0" * 64
        self.write_json("working/render_snapshots/render_test.json", broken)
        self.run_renderer(
            "--quality",
            "preview",
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
            expected=2,
        )
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), last_good_hash)

    def test_same_asset_can_be_used_on_multiple_timeline_layers(self) -> None:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        (self.project / "assets/pixel.png").write_bytes(pixel)
        state_path = self.project / "working/editor_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for index, x_position in enumerate((25, 75), start=1):
            state["overlays"].append(
                {
                    "id": f"image-{index}",
                    "type": "image",
                    "start": 0.03,
                    "end": 0.40,
                    "source": "assets/pixel.png",
                    "visible": True,
                    "z_index": 30 + index,
                    "style": {
                        "width": 12,
                        "x": x_position,
                        "y": 40,
                        "animation": "fade",
                    },
                }
            )
        self.write_json("working/editor_state.json", state)
        output = self.project / "renders/repeated-asset.mp4"
        self.run_renderer("--quality", "preview", "--output", str(output))
        self.assertTrue(output.is_file())

    def test_text_animation_is_encoded_in_ffmpeg_filter(self) -> None:
        overlay = {
            "type": "caption",
            "start": 1.0,
            "end": 2.0,
            "style": {
                "font_size": 48,
                "color": "#ffffff",
                "stroke_color": "#000000",
                "stroke_width": 3,
                "x": 50,
                "y": 75,
                "animation": "fade",
            },
        }
        filters = text_filter(
            "v0",
            "v1",
            overlay,
            1080,
            1920,
            1.0,
            Path("/tmp/test-font.ttf"),
            Path("/tmp/test-caption.txt"),
        )
        self.assertIn("alpha=", filters)

    def test_renderer_normalizes_audio_for_social_delivery(self) -> None:
        source = self.project / "source/source.mp4"
        result = subprocess.run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x3b332d:s=360x640:d=0.45",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=0.45",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        command = build_render_command(
            self.project,
            state,
            manifest,
            self.project / "renders/loudness-preview.mp4",
            "preview",
        )
        self.assertIn("-af", command)
        self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", command)

    def test_renderer_skips_loudnorm_for_silent_audio(self) -> None:
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        command = build_render_command(
            self.project,
            state,
            manifest,
            self.project / "renders/silent-preview.mp4",
            "preview",
        )
        self.assertNotIn("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", command)

    def test_designed_package_is_the_visual_base_while_source_audio_is_preserved(self) -> None:
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        visual_source = self.project / "working/graphic_packages/test/output.mp4"
        visual_source.parent.mkdir(parents=True, exist_ok=True)
        visual_source.write_bytes(b"designed-visual")
        clip = {"id": "highlight-rich", "start": 0.0, "end": 0.4}
        command = build_render_command(
            self.project,
            state,
            manifest,
            self.project / "renders/designed-preview.mp4",
            "preview",
            clip,
            visual_source,
        )
        inputs = [command[index + 1] for index, value in enumerate(command) if value == "-i"]
        self.assertEqual(
            inputs[:2],
            [str(self.project / "source/source.mp4"), str(visual_source.resolve())],
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertTrue(filters.startswith("[1:v]scale="), filters)
        self.assertNotIn("drawtext=", filters, "designed package must already contain captions")
        audio_map = command[command.index("-map", command.index("-map") + 1) + 1]
        self.assertEqual(audio_map, "0:a?")

    def test_designed_package_outside_project_graphics_root_is_rejected(self) -> None:
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        external = Path(self._tmp.name) / "external-visual.mp4"
        external.write_bytes(b"outside")
        with self.assertRaisesRegex(ValueError, "working/graphic_packages"):
            build_render_command(
                self.project,
                state,
                manifest,
                self.project / "renders/designed-preview.mp4",
                "preview",
                {"id": "highlight-rich", "start": 0.0, "end": 0.4},
                external,
            )

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import http.client
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import editor_server  # noqa: E402
import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402
import render_editor_timeline  # noqa: E402
import qa_video  # noqa: E402
import sfx_delivery  # noqa: E402
from editor_server import (  # noqa: E402
    EditorServer,
    caption_effect_spans,
    editor_state_revision,
    extract_effect_keywords,
    MAX_CAPTION_EMPHASIS_SPANS,
    gate_revision,
    migrate_editor_state_v1_to_v2,
    render_download_errors,
    validate_editor_state,
)

# Mirrors what the policy-enforcing qa_video writes; delivery validation
# rejects QA reports that lack this block (pre-policy reports).
SYNTHETIC_QA_POLICY = dataclasses.asdict(qa_video.QaPolicy())
from render_editor_timeline import (  # noqa: E402
    build_render_command,
    direct_final_render_id,
    image_filter,
    text_filter,
)


class FakeAssetProviderService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.search_error = None

    def status(self) -> dict[str, object]:
        self.calls.append(("status",))
        return {
            "providers": [
                {
                    "id": "openverse",
                    "label": "Openverse",
                    "kind": "image",
                    "consent_required": True,
                    "cost_class": "free",
                    "network_disclosure": "會送出短搜尋詞。",
                    "consented": False,
                    "consented_at": None,
                    "confirmed_by": None,
                }
            ]
        }

    def set_consent(self, provider_id: str, consented: bool, confirmed_by: str) -> dict[str, object]:
        self.calls.append(("consent", provider_id, consented, confirmed_by))
        return {
            "provider_id": provider_id,
            "kind": "image",
            "consented": consented,
            "consented_at": "2026-08-04T00:00:00Z",
            "confirmed_by": confirmed_by,
        }

    def search(self, provider_id: str, query: str, page: int) -> dict[str, object]:
        self.calls.append(("search", provider_id, query, page))
        if self.search_error is not None:
            raise self.search_error
        return {
            "provider_id": provider_id,
            "page": page,
            "items": [{"candidate_id": "candidate", "import_token": "opaque-token"}],
        }

    def import_candidate(self, token: str, visual_validator) -> dict[str, object]:
        self.calls.append(("import", token, callable(visual_validator)))
        return {
            "source": "assets/providers/openverse/candidate.jpg",
            "url": "/assets/providers/openverse/candidate.jpg",
            "idempotent": False,
        }


class CaptionEffectModelTests(unittest.TestCase):
    def test_audio_event_edits_are_revision_bound_and_strictly_validated(self) -> None:
        state = {
            "schema_version": 2,
            "project_id": "revision-test",
            "audio_event_edits": {
                "schema_version": 1,
                "source_render_id": "direct-final-0123456789abcdef",
                "source_plan_sha256": "a" * 64,
                "source_timeline_revision": "b" * 64,
                "events": [
                    {
                        "id": "sfx-event-0001",
                        "source_event_sha256": "c" * 64,
                        "event_start_sample": 1200,
                        "gain_db": -18,
                    }
                ],
            },
            "overlays": [],
        }
        without_edits = {key: value for key, value in state.items() if key != "audio_event_edits"}
        self.assertNotEqual(editor_state_revision(state), editor_state_revision(without_edits))

        malformed = json.loads(json.dumps(state))
        malformed["audio_event_edits"]["events"][0]["event_start_sample"] = True
        self.assertTrue(
            any("audio_event_edits" in error for error in validate_editor_state(malformed, 1.0))
        )

    def test_font_asset_id_requires_a_safe_string_and_changes_revision(self) -> None:
        state = {
            "schema_version": 2,
            "caption_defaults": {"font_asset_id": "font-google-fonts-0123456789abcdef-0123456789abcdef"},
            "overlays": [{"id": "caption-1", "type": "caption", "style": {}}],
        }
        before = editor_state_revision(state)
        state["caption_defaults"]["font_asset_id"] = "font-google-fonts-fedcba9876543210-fedcba9876543210"
        self.assertNotEqual(before, editor_state_revision(state))

        invalid_default = {**state, "caption_defaults": {"font_asset_id": True}}
        self.assertIn("caption_defaults font_asset_id is invalid", validate_editor_state(invalid_default, 1.0))
        invalid_overlay = {
            **state,
            "overlays": [{"id": "caption-1", "type": "caption", "style": {"font_asset_id": 7}}],
        }
        self.assertIn(
            "overlay caption-1 style font_asset_id is invalid",
            validate_editor_state(invalid_overlay, 1.0),
        )

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
        # Every keyword has to read as emphasised. Alternating the effect by
        # reading order meant the second term became a backdrop marker that
        # keeps the base text colour, so a line with two keywords showed one.
        self.assertEqual({item["style"]["effect"] for item in spans}, {"pop"})
        self.assertLessEqual(len(spans), MAX_CAPTION_EMPHASIS_SPANS)

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


class StructuredCardMotionFilterTests(unittest.TestCase):
    """Finished card images must still move horizontally during their hold."""

    def test_slide_in_and_pan_change_image_x_over_time(self) -> None:
        for animation, expected in (
            ("slide-in", ("if(lt(t,", "-t)/")),
            ("pan", ("min(1,max(0,(t-", "*min(1,max(0,")),
        ):
            with self.subTest(animation=animation):
                overlay = {
                    "id": "structured-card-image",
                    "type": "image",
                    "source": "working/structured_cards/card.png",
                    "start": 1.0,
                    "end": 3.0,
                    "style": {
                        "width": 60.0,
                        "x": 50,
                        "y": 42,
                        "animation": animation,
                    },
                }
                rendered = image_filter("base", "out", "asset", overlay, 960, 540)
                x_expression = rendered.split("overlay=x='", 1)[1].split("':y=", 1)[0]
                self.assertTrue(
                    all(fragment in x_expression for fragment in expected),
                    x_expression,
                )


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

    def test_editor_blocks_unavailable_director_before_replan_mutation(self) -> None:
        script = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")
        self.assertIn('"kinetic-explainer"', script)
        self.assertIn("missing_capabilities", script)
        self.assertIn("option.disabled", script)
        self.assertIn('button.setAttribute("aria-disabled"', script)
        self.assertIn("if (!isDirectorAvailable(preset))", script)
        self.assertIn("if (!isDirectorAvailable(currentPreset))", script)
        unavailable_guard = script.index("if (!isDirectorAvailable(currentPreset))")
        save_call = script.index("await saveState(false)", unavailable_guard)
        post_call = script.index('request("/api/plan-highlights"', save_call)
        self.assertLess(unavailable_guard, save_call)
        self.assertLess(save_call, post_call)

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

    def test_style_pack_picker_is_populated_from_the_project_registry(self) -> None:
        html = (SKILL_DIR / "editor/index.html").read_text(encoding="utf-8")
        script = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")
        picker = html.split('id="style-pack-select"', 1)[1].split("</select>", 1)[0]
        self.assertNotIn("dark-data-presenter", picker)
        self.assertIn("projectPayload.style_packs", script)
        self.assertIn('elements["style-pack-select"].replaceChildren()', script)

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

    def publish_editable_audio_source(
        self, state: dict[str, object], render_id: str = "studio-audio-source"
    ) -> dict[str, object]:
        output = self.project / f"renders/{render_id}.mp4"
        stage = delivery_envelope.begin_staging(
            self.project, render_id, expected_output=output
        )
        evidence = {
            "schema_version": 1,
            "duration_s": 6.0,
            "items": [
                {
                    "id": "title-1",
                    "start": "0.20",
                    "end": "0.50",
                    "kind": "title",
                    "component_id": "title-lockup",
                    "motion": {
                        "requested": "slide-up",
                        "delivered": "slide-up",
                        "faithful": True,
                        "status": "native",
                    },
                },
                {
                    "id": "row-1",
                    "start": "0.70",
                    "end": "1.10",
                    "kind": "dynamic_list",
                    "component_id": "dynamic-list",
                    "motion": {
                        "requested": "staggered-reveal",
                        "delivered": "staggered-reveal",
                        "faithful": True,
                        "status": "rendered",
                    },
                },
            ],
        }
        timeline_revision = editor_server.editor_base_state_revision(state)
        cut_hash = sfx_delivery.effective_cut_map_sha256(self.project, state)
        sfx_delivery.stage_multi_event_delivery(
            Path(stage), evidence, timeline_revision, cut_hash
        )
        staged = Path(stage)
        (staged / "candidate.mp4").write_bytes(b"synthetic-final")
        (staged / "qa_report.json").write_text('{"status":"pass"}\n', encoding="utf-8")
        (staged / "contact_sheet.png").write_bytes(b"synthetic-contact")
        (staged / "visual_evidence.json").write_text(
            json.dumps(evidence) + "\n", encoding="utf-8"
        )
        sources = {
            "output": staged / "candidate.mp4",
            "qa_report": staged / "qa_report.json",
            "contact_sheet": staged / "contact_sheet.png",
            "visual_evidence": staged / "visual_evidence.json",
            "motion_evidence": staged / "visual_evidence.json",
            "audio_event_plan": staged / "audio_event_plan.json",
            "audio_catalog": staged / "audio_catalog.json",
            "sfx_stem": staged / "sfx_stem.wav",
        }
        prepared = delivery_envelope.build_prepared_envelope(
            self.project,
            render_id,
            output,
            state,
            sources,
            renderer_script=Path(render_editor_timeline.__file__).resolve(),
            ffmpeg_executable=Path(render_editor_timeline.ffmpeg_path()).resolve(),
        )
        delivery_envelope.write_prepared_envelope(stage, prepared)
        delivery_envelope.publish_direct_delivery(
            self.project, stage, staged_sources=sources, expected_output=output
        )
        return json.loads(
            (self.project / f"working/audio_event_plans/{render_id}.json").read_text(
                encoding="utf-8"
            )
        )

    def test_audio_event_edits_put_get_exact_round_trip_from_finalized_source(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body)["state"]
        source_plan = self.publish_editable_audio_source(state)

        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        project = json.loads(body)
        timeline = project["audio_event_timeline"]
        self.assertTrue(timeline["editable"], timeline)
        source_event = source_plan["events"][0]
        edits = {
            "schema_version": 1,
            "source_render_id": "studio-audio-source",
            "source_plan_sha256": timeline["source_plan_sha256"],
            "source_timeline_revision": timeline["source_timeline_revision"],
            "events": [
                {
                    "id": source_event["id"],
                    "source_event_sha256": contract_registry.canonical_hash(source_event),
                    "event_start_sample": source_event["event_start_sample"],
                    "gain_db": -18,
                }
            ],
        }
        state = project["state"]
        state["audio_event_edits"] = edits
        state["x_expected_revision"] = state["revision"]
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        for gate in ("timeline", "final"):
            manifest.setdefault("approvals", {})[gate] = {
                "approved": True,
                "state_revision": "0" * 64,
            }
        self.write_json("project.json", manifest)

        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)
        self.assertEqual(set(saved["invalidated_gates"]), {"timeline", "final"})
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        persisted = json.loads(body)
        self.assertEqual(persisted["state"]["audio_event_edits"], edits)
        self.assertEqual(persisted["audio_event_timeline"]["events"][0]["gain_db"], -18)
        self.assertEqual(
            persisted["audio_event_timeline"]["studio_edits_sha256"],
            contract_registry.canonical_hash(edits),
        )

    def test_audio_event_edit_boundaries_and_stale_source_fail_closed(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body)["state"]
        source = self.publish_editable_audio_source(state)
        event = source["events"][0]
        base = {
            "schema_version": 1,
            "source_render_id": "studio-audio-source",
            "source_plan_sha256": "a" * 64,
            "source_timeline_revision": source["timeline_revision"],
            "events": [
                {
                    "id": event["id"],
                    "source_event_sha256": contract_registry.canonical_hash(event),
                    "event_start_sample": event["event_start_sample"],
                    "gain_db": -24,
                }
            ],
        }
        for gain in (-24, -6):
            candidate = json.loads(json.dumps(base))
            candidate["events"][0]["gain_db"] = gain
            resolved = editor_server.resolve_studio_audio_plan(source, candidate)
            self.assertEqual(resolved["events"][0]["gain_db"], gain)

        malformed_cases = []
        for bad_value in (True, 1.5):
            candidate = json.loads(json.dumps(base))
            candidate["events"][0]["event_start_sample"] = bad_value
            malformed_cases.append(candidate)
        for bad_gain in (True, float("nan"), -24.01, -5.99):
            candidate = json.loads(json.dumps(base))
            candidate["events"][0]["gain_db"] = bad_gain
            malformed_cases.append(candidate)
        duplicate = json.loads(json.dumps(base))
        duplicate["events"].append(json.loads(json.dumps(duplicate["events"][0])))
        malformed_cases.append(duplicate)
        for candidate in malformed_cases:
            with self.subTest(candidate=str(candidate)), self.assertRaises(
                editor_server.AudioEventEditError
            ):
                editor_server.resolve_studio_audio_plan(source, candidate)

        for label, mutate, pattern in (
            (
                "event_hash",
                lambda item: item["events"][0].update(source_event_sha256="f" * 64),
                "hash is stale",
            ),
            (
                "no_change",
                lambda item: item["events"][0].update(gain_db=-12),
                "has no change",
            ),
            (
                "bounds",
                lambda item: item["events"][0].update(
                    event_start_sample=source["sfx_stem_sample_count"]
                ),
                "outside sfx stem bounds",
            ),
            (
                "alignment",
                lambda item: item["events"][0].update(
                    event_start_sample=event["event_start_sample"] + 3841
                ),
                "3840-sample trigger tolerance",
            ),
        ):
            candidate = json.loads(json.dumps(base))
            mutate(candidate)
            with self.subTest(label=label), self.assertRaisesRegex(
                editor_server.AudioEventEditError, pattern
            ):
                editor_server.resolve_studio_audio_plan(source, candidate)

        status, _headers, body = self.request("GET", "/api/project")
        project = json.loads(body)
        stale = project["state"]
        stale["audio_event_edits"] = {
            **base,
            "source_render_id": "source-swapped-without-envelope",
            "source_plan_sha256": project["audio_event_timeline"]["source_plan_sha256"],
        }
        stale["x_expected_revision"] = stale["revision"]
        status, rejected = self.json_request("PUT", "/api/editor-state", stale)
        self.assertEqual(status, 422, rejected)
        self.assertIn("audio_event_edits", str(rejected))

        plan_path = self.project / "working/audio_event_plans/studio-audio-source.json"
        backup = self.project / "working/audio_event_plans/source-backup.json"
        backup.write_bytes(plan_path.read_bytes())
        plan_path.write_bytes(plan_path.read_bytes() + b" ")
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        mismatch = json.loads(body)["audio_event_timeline"]
        self.assertFalse(mismatch["editable"])
        self.assertIn("invalid", mismatch["reason"])
        plan_path.write_bytes(backup.read_bytes())
        plan_path.unlink()
        plan_path.symlink_to(backup)
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        unavailable = json.loads(body)["audio_event_timeline"]
        self.assertFalse(unavailable["editable"])
        self.assertIn("invalid", unavailable["reason"])

    def test_fonts_endpoint_projects_only_safe_metadata(self) -> None:
        asset_id = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        resolved = {
            "asset_id": asset_id,
            "family": "Example Sans",
            "style": "Regular",
            "weight": 400,
            "coverage": {"unicode_coverage_count": 42},
            "scripts": ["Latin"],
            "license_spdx": "OFL-1.1",
            "provider_id": "google-fonts",
            "sha256": "a" * 64,
            "path": "assets/fonts/private.ttf",
            "receipt": {"download_url": "https://private.invalid/secret"},
            "validation": {"required_text": "secret"},
        }
        with patch.object(editor_server.asset_registry, "list_project_fonts", return_value=[resolved]):
            status, _headers, body = self.request("GET", "/api/fonts")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["fonts"][0]["asset_id"], asset_id)
        self.assertEqual(payload["selected"], None)
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("private.ttf", "private.invalid", "download_url", "required_text", "receipt", "validation"):
            self.assertNotIn(forbidden, serialized)

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
                snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
                clip_id = snapshot_payload["clip"]["id"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(f"fake-mp4:{clip_id}".encode("utf-8"))
                visual_path = editor_server.rendered_visual_evidence_path(
                    self.project, snapshot_payload["render_id"]
                )
                visual_path.parent.mkdir(parents=True, exist_ok=True)
                visual_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "source": "renderer_evidence",
                            "status": "pass",
                            "duration_s": 10.0,
                            "minimum_primary_font_px": 48.0,
                            "expected_visual_beat_count": 1,
                            "visual_beat_count": 1,
                            "component_ids": ["synthetic"],
                            "component_count": 1,
                            "skin_ids": ["synthetic"],
                            "skin_count": 1,
                            "longest_no_change_gap_s": 5.0,
                            "motion_requested_count": 1,
                            "motion_faithful_count": 1,
                            "motion_fallback_count": 0,
                            "motion_faithful_ratio": 1.0,
                            "failures": [],
                            "warnings": [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if callable(after_render):
                    after_render(render_count)
                return subprocess.CompletedProcess(command, 0, "", "")
            if script_name == "qa_video.py":
                qa_count += 1
                report = Path(command[command.index("--report") + 1])
                contact = Path(command[command.index("--contact") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                qa_failed = fail_qa_number == qa_count
                visual_delivery = json.loads(
                    Path(command[command.index("--visual-evidence") + 1]).read_text(
                        encoding="utf-8"
                    )
                )
                report.write_text(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "status": "fail" if qa_failed else "pass",
                            "profile": "strict",
                            "policy": SYNTHETIC_QA_POLICY,
                            "visual_delivery": visual_delivery,
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
        self.assertEqual(len(payload["director_presets"]), 6)
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
            {"專業教學", "爆款短影音", "八卦時事", "POV 藏鏡人", "編輯精簡", "動畫解說"},
        )
        metadata_keys = (
            "profile_id",
            "registry_schema_version",
            "registry_entry_version",
            "experience",
            "required_capabilities",
            "rules",
            "resolved_hash",
        )
        for preset in payload["director_presets"].values():
            for key in metadata_keys:
                self.assertIn(key, preset)
        kinetic = payload["director_presets"]["kinetic-explainer"]
        self.assertTrue(kinetic["available"])
        self.assertEqual(kinetic["missing_capabilities"], [])
        resolved = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "scripts/auto_edit.py"),
                "resolve-director",
                "--director",
                "kinetic-explainer",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(kinetic["resolved_hash"], json.loads(resolved.stdout)["resolved_hash"])
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
        self.assertEqual(
            planned["state"]["style_pack"]["project_default"], "kinetic-social"
        )
        self.assertLessEqual(len(planned["state"]["highlights"]), 3)

    def test_kinetic_highlight_planning_persists_director_state_and_plan(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        bootstrap = json.loads(body.decode("utf-8"))
        plan_path = self.project / "working/highlight_plan.json"
        status, payload = self.json_request(
            "POST",
            "/api/plan-highlights",
            {
                "director": "kinetic-explainer",
                "brief": "動畫解說",
                "count": 3,
                "expected_revision": bootstrap["state"]["revision"],
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            payload["highlight_plan"]["configuration"]["director_profile"],
            "kinetic-explainer",
        )
        self.assertEqual(payload["state"]["director_style"], "kinetic-explainer")
        self.assertEqual(
            payload["state"]["style_pack"]["project_default"], "kinetic-social"
        )
        self.assertNotEqual(payload["state"]["revision"], bootstrap["state"]["revision"])
        self.assertTrue(plan_path.is_file())
        stored_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            stored_plan["configuration"]["director_profile"], "kinetic-explainer"
        )

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

    def test_assets_route_rejects_svg_xml_variants_but_serves_png(self) -> None:
        blocked = {
            "unsafe.svg": b"<svg>must not be served</svg>",
            "unsafe.SVGZ": b"compressed SVG must not be served",
            "unsafe.XML": b"<?xml version='1.0'?>",
        }
        for name, payload in blocked.items():
            (self.project / "assets" / name).write_bytes(payload)
            encoded_name = urllib.parse.quote(name, safe="")
            status, _headers, body = self.request("GET", f"/assets/{encoded_name}")
            self.assertEqual(status, 403, name)
            self.assertNotIn(payload, body, name)

        png = self.project / "assets" / "safe.PNG"
        png.write_bytes(b"PNG bytes are still served")
        status, _headers, body = self.request("GET", "/assets/safe.%50%4E%47")
        self.assertEqual(status, 200)
        self.assertEqual(body, png.read_bytes())

    def test_generic_assets_route_never_serves_project_font_bytes(self) -> None:
        hostile = b"NOT-A-FONT hostile bytes probe"
        fonts = self.project / "assets/fonts"
        fonts.mkdir()
        (fonts / "unregistered.ttf").write_bytes(hostile)
        (fonts / "nested").mkdir()
        (fonts / "nested/preview.png").write_bytes(hostile)
        (self.project / "assets/font-alias").symlink_to(fonts, target_is_directory=True)
        imported = self.project / "assets/imported"
        imported.mkdir()
        (imported / "renamed.OTF").write_bytes(hostile)

        blocked = (
            "/assets/fonts/unregistered.ttf",
            "/assets/FONTS/unregistered.ttf",
            "/assets/%66onts/unregistered.ttf",
            "/assets/fonts%2Funregistered.ttf",
            "/assets/fonts/unregistered.%74%74%66",
            "/assets/fonts/nested/preview.png",
            "/assets/./fonts/nested/preview.png",
            "/assets/x/../fonts/nested/preview.png",
            "/assets/%2e/fonts/nested/preview.png",
            "/assets/x/%2e%2e/fonts/nested/preview.png",
            "/assets//fonts/nested/preview.png",
            "/assets/fonts//nested/preview.png",
            "/assets/fonts%2F%2Fnested/preview.png",
            "/assets/%/fonts/nested/preview.png",
            "/assets/%FF/fonts/nested/preview.png",
            "/assets/font-alias/nested/preview.png",
            "/assets/imported/renamed.OTF",
            "/assets/imported/renamed.%4f%54%46",
        )
        for route in blocked:
            with self.subTest(route=route):
                status, _headers, body = self.request("GET", route)
                self.assertEqual(status, 403)
                self.assertNotIn(hostile, body)

        status, _headers, body = self.request("GET", "/api/fonts/fake/bytes")
        self.assertEqual(status, 404)
        self.assertNotIn(hostile, body)

    def test_project_font_bytes_route_is_fixed_mime_nosniff_and_no_range(self) -> None:
        asset_id = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        font = self.project / "assets/fonts/verified.ttf"
        font.parent.mkdir()
        payload = b"verified receipt-bound font bytes"
        font.write_bytes(payload)
        binding = {
            "asset_id": asset_id,
            "path": "assets/fonts/verified.ttf",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

        with patch.object(
            editor_server.asset_registry,
            "resolve_project_font",
            return_value=binding,
        ) as resolve:
            status, headers, body = self.request(
                "GET",
                f"/api/fonts/{asset_id}/bytes",
                headers={"Range": "bytes=2-5"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers["Content-Type"], "font/ttf")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNone(headers.get("Accept-Ranges"))
        self.assertIsNone(headers.get("Content-Range"))
        resolve.assert_called_once_with(self.project.resolve(), asset_id)

        with patch.object(
            editor_server.asset_registry,
            "resolve_project_font",
            side_effect=editor_server.asset_registry.AssetRegistryError("tampered"),
        ):
            status, _headers, body = self.request(
                "GET", f"/api/fonts/{asset_id}/bytes"
            )
        self.assertEqual(status, 404)
        self.assertNotIn(payload, body)

        outside = Path(self._tmp.name) / "outside.ttf"
        outside.write_bytes(payload)
        symlink = self.project / "assets/fonts/symlink.ttf"
        symlink.symlink_to(outside)
        with patch.object(
            editor_server.asset_registry,
            "resolve_project_font",
            return_value={
                "asset_id": asset_id,
                "path": "assets/fonts/symlink.ttf",
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        ):
            status, _headers, body = self.request(
                "GET", f"/api/fonts/{asset_id}/bytes"
            )
        self.assertEqual(status, 404)
        self.assertNotIn(payload, body)

        with patch.object(
            editor_server.asset_registry,
            "resolve_project_font",
            return_value={
                "asset_id": asset_id,
                "path": "assets/fonts/missing.ttf",
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        ):
            status, _headers, body = self.request(
                "GET", f"/api/fonts/{asset_id}/bytes"
            )
        self.assertEqual(status, 404)
        self.assertNotIn(payload, body)

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

    def test_provider_routes_share_csrf_validation_and_safe_error_mapping(self) -> None:
        fake = FakeAssetProviderService()
        self.server.asset_provider_service = fake

        status, _headers, body = self.request("GET", "/api/providers/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["providers"][0]["id"], "openverse")

        status, payload = self.json_request(
            "POST",
            "/api/providers/consent",
            {"provider_id": "openverse", "consented": True, "confirmed_by": "nat"},
            headers={"X-Auto-Edit-CSRF": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertIn("CSRF", str(payload["error"]))

        status, payload = self.json_request("POST", "/api/providers/consent", [])
        self.assertEqual(status, 400)
        status, payload = self.json_request(
            "POST",
            "/api/providers/consent",
            {"provider_id": "openverse", "consented": True, "confirmed_by": "nat"},
        )
        self.assertEqual(status, 200, payload)

        status, payload = self.json_request(
            "POST",
            "/api/assets/search",
            {"provider_id": "openverse", "query": "cat", "page": 2},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["items"][0]["import_token"], "opaque-token")

        status, payload = self.json_request(
            "POST", "/api/assets/import-provider", {"import_token": "opaque-token"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["source"], "assets/providers/openverse/candidate.jpg")

        fake.search_error = editor_server.AssetProviderError(
            "provider request failed", status_code=502, code="provider_failure"
        )
        status, payload = self.json_request(
            "POST",
            "/api/assets/search",
            {"provider_id": "openverse", "query": "secret query"},
        )
        self.assertEqual(status, 502)
        self.assertNotIn("secret query", str(payload))

    def test_asset_library_exposes_current_license_and_fails_closed_on_bad_registry(self) -> None:
        path = self.project / "assets/providers/openverse/candidate.jpg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"registered-provider-image")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        editor_server.asset_registry.upsert_item(
            self.project,
            {
                "asset_id": "provider-openverse-candidate",
                "path": "assets/providers/openverse/candidate.jpg",
                "sha256": digest,
                "origin": "provider",
                "provider_id": "openverse",
                "source_url": "https://example.org/candidate",
                "license": {
                    "spdx": "CC-BY-4.0",
                    "evidence_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution_required": True,
                    "attribution_text": "Jane Example",
                    "verified_at": "2026-08-04T00:00:00Z",
                },
                "review_status": "approved",
            },
        )

        status, _headers, body = self.request("GET", "/api/assets/library")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        item = next(asset for asset in payload["assets"] if asset["path"].endswith("candidate.jpg"))
        self.assertEqual(item["provider_id"], "openverse")
        self.assertEqual(item["license_spdx"], "CC-BY-4.0")
        self.assertEqual(item["review_status"], "approved")
        self.assertTrue(item["attribution_required"])
        self.assertIsNone(payload["registry_error"])

        (self.project / "assets/provenance.json").write_text(
            '{"items": []}', encoding="utf-8"
        )
        status, _headers, body = self.request("GET", "/api/assets/library")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        item = next(asset for asset in payload["assets"] if asset["path"].endswith("candidate.jpg"))
        self.assertTrue(payload["registry_error"])
        self.assertIsNone(item["provider_id"])
        self.assertNotEqual(item["review_status"], "approved")

    def test_cross_origin_gets_cannot_trigger_legacy_registry_migration(self) -> None:
        asset = self.project / "assets/legacy.png"
        asset.write_bytes(b"legacy-upload")
        registry = self.project / editor_server.asset_registry.PROVENANCE_REL
        legacy = json.dumps(
            {
                "items": [
                    {
                        "file": "assets/legacy.png",
                        "original_name": "legacy.png",
                        "source": "user-uploaded-through-local-editor",
                        "bytes": len(asset.read_bytes()),
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "uploaded_at": "2026-08-04T03:00:00+00:00",
                    }
                ]
            }
        ).encode("utf-8")
        registry.write_bytes(legacy)
        mtime_before = registry.stat().st_mtime_ns

        for endpoint in ("/api/assets/library", "/api/rights"):
            status, _headers, _body = self.request(
                "GET", endpoint, headers={"Origin": "https://attacker.invalid"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(registry.read_bytes(), legacy)
            self.assertEqual(registry.stat().st_mtime_ns, mtime_before)
            self.assertFalse(
                (self.project / editor_server.asset_registry.ATTRIBUTION_REL).exists()
            )

    def test_server_initialization_migrates_legacy_before_get_requests(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

        asset = self.project / "assets/legacy-init.png"
        asset.write_bytes(b"legacy-init-upload")
        registry = self.project / editor_server.asset_registry.PROVENANCE_REL
        registry.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "file": "assets/legacy-init.png",
                            "original_name": "legacy-init.png",
                            "source": "user-uploaded-through-local-editor",
                            "bytes": len(asset.read_bytes()),
                            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                            "uploaded_at": "2026-08-04T03:00:00+00:00",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.server = EditorServer(("127.0.0.1", 0), self.project)
        migrated_before_get = registry.read_bytes()
        mtime_before_get = registry.stat().st_mtime_ns
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

        migrated = json.loads(migrated_before_get)
        self.assertEqual(migrated["schema_version"], 1)
        self.assertEqual(migrated["items"][0]["origin"], "user-upload")
        for endpoint in ("/api/assets/library", "/api/rights"):
            status, _headers, _body = self.request("GET", endpoint)
            self.assertEqual(status, 200)
            self.assertEqual(registry.read_bytes(), migrated_before_get)
            self.assertEqual(registry.stat().st_mtime_ns, mtime_before_get)

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
        self.assertEqual(provenance["schema_version"], 1)
        self.assertEqual(provenance["items"][0]["origin"], "user-upload")
        self.assertEqual(provenance["items"][0]["path"], upload["source"])
        self.assertIsNone(provenance["items"][0]["provider_id"])
        self.assertIsNone(provenance["items"][0]["source_url"])
        self.assertEqual(provenance["items"][0]["license"]["spdx"], "UNKNOWN")
        self.assertFalse(provenance["items"][0]["license"]["attribution_required"])
        self.assertEqual(provenance["items"][0]["review_status"], "pending")
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

    def test_manual_upload_rolls_back_asset_and_registry_on_attribution_failure(self) -> None:
        upload_body = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        real_write = editor_server.asset_registry._atomic_write_bytes
        failed = False

        def fail_attribution_once(path: Path, payload: bytes, label: str) -> None:
            nonlocal failed
            if label == "ATTRIBUTION.md" and not failed:
                failed = True
                raise editor_server.asset_registry.AssetRegistryError("simulated failure")
            real_write(path, payload, label)

        with patch.object(
            editor_server.asset_registry,
            "_atomic_write_bytes",
            fail_attribution_once,
        ):
            status, _headers, _body = self.request(
                "POST",
                "/api/assets?filename=rollback.png",
                upload_body,
                {"Content-Type": "image/png"},
            )

        self.assertEqual(status, 409)
        self.assertEqual(list((self.project / "assets").glob("rollback-*.png")), [])
        self.assertFalse((self.project / "assets/provenance.json").exists())
        self.assertFalse((self.project / "ATTRIBUTION.md").exists())

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
        # This test targets delivery-QA tampering; drop effect spans so the
        # Phase 1a effect-span final gate (tested separately) stays out of the way.
        for overlay in state.get("overlays", []):
            overlay.pop("effect_spans", None)
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
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
        self.write_json(
            "qa/final-test-report.json",
            {"schema_version": 1, "status": "pass", "policy": SYNTHETIC_QA_POLICY},
        )
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

    def test_project_get_exposes_all_style_packs_and_director_motion(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(
            {pack["id"] for pack in payload["style_packs"]},
            {"dark-data-presenter", "kinetic-social", "editorial-paper"},
        )
        self.assertEqual(
            payload["director_presets"]["high-energy"]["motion_intensity"],
            "high",
        )

    def test_new_default_state_selects_the_default_style_pack(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        self.assertEqual(
            state["style_pack"],
            {"project_default": "dark-data-presenter", "per_highlight": {}},
        )
        self.assertEqual(validate_editor_state(state, 2.0), [])

    def test_director_selects_a_matching_default_style_pack(self) -> None:
        for director, expected in (
            ("high-energy", "kinetic-social"),
            ("teacher-punch", "dark-data-presenter"),
            ("editorial-clean", "editorial-paper"),
        ):
            with self.subTest(director=director):
                self.write_json(
                    "working/highlight_plan.json",
                    {"configuration": {"director_profile": director}, "items": []},
                )
                manifest = json.loads(
                    (self.project / "project.json").read_text(encoding="utf-8")
                )
                state = editor_server.default_editor_state(self.project, manifest)
                self.assertEqual(state["style_pack"]["project_default"], expected)

    def test_auto_visuals_passes_the_directors_density_to_the_planner(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["director_style"] = "high-energy"
        self.write_json("working/editor_state.json", state)
        self.write_json(
            "working/evidence_map.json",
            {
                "schema_version": 1,
                "source_sha256": "a" * 64,
                "transcript_revision": "b" * 64,
                "revision": "c" * 64,
                "items": [{
                    "id": "evidence-aaaa1111", "kind": "quote",
                    "literal": "這是一段一般敘述", "start": 0.1, "end": 0.8,
                    "confidence": 0.99, "review_status": "approved",
                }],
            },
        )
        original = editor_server.visual_director.plan_visuals
        observed: dict[str, object] = {}

        def spy(*args, **kwargs):
            observed.update(kwargs)
            return original(*args, **kwargs)

        with patch.object(editor_server.visual_director, "plan_visuals", side_effect=spy):
            status, _headers, body = self.request("POST", "/api/auto-visuals")
            payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(observed.get("visual_density"), "dense")

    def test_output_variant_receipt_records_per_highlight_style_pack(self) -> None:
        state = {
            "schema_version": 2,
            "director_style": "high-energy",
            "highlights": [
                {"id": "highlight-aaaa1111"},
                {"id": "highlight-bbbb2222"},
            ],
            "style_pack": {
                "project_default": "kinetic-social",
                "per_highlight": {"highlight-bbbb2222": "editorial-paper"},
            },
        }
        editor_server.write_output_variant_set(self.project, state, {}, [])
        receipt = json.loads(
            (self.project / "working/output_variant_set.json").read_text("utf-8")
        )
        by_highlight = {
            item["highlight_id"]: item["style_pack"]
            for item in receipt["highlight_modes"]
        }
        self.assertEqual(
            by_highlight["highlight-aaaa1111"],
            {"id": "kinetic-social", "selection": "project-default"},
        )
        self.assertEqual(
            by_highlight["highlight-bbbb2222"],
            {"id": "editorial-paper", "selection": "user"},
        )

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

    def _install_synthetic_final_delivery(
        self, report_payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state_revision = editor_state_revision(state)
        output_bytes = b"approved-final-bytes"
        (self.project / "renders/final-ok.mp4").write_bytes(output_bytes)
        if report_payload is None:
            report_payload = {
                "status": "pass",
                "policy": SYNTHETIC_QA_POLICY,
                "failures": [],
                "warnings": [],
            }
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

    def test_pre_policy_qa_report_blocks_final_download(self) -> None:
        # A hash-consistent delivery whose QA report predates the enforced
        # QaPolicy (no "policy" block) must not unlock the final download.
        state = self._install_synthetic_final_delivery(
            report_payload={"status": "pass", "failures": [], "warnings": []}
        )
        errors = editor_server.delivery_qa_errors(self.project, state)
        self.assertTrue(
            any("predates the enforced QA policy" in item for item in errors), errors
        )
        status, _headers, _body = self.request("GET", "/renders/final-ok.mp4")
        self.assertEqual(status, 403)

    def test_schema3_delivery_requires_matching_passing_visual_evidence(self) -> None:
        visual_delivery = {
            "schema_version": 1,
            "source": "renderer_evidence",
            "status": "pass",
            "duration_s": 2.0,
            "minimum_primary_font_px": 48.0,
            "expected_visual_beat_count": 1,
            "visual_beat_count": 1,
            "component_ids": ["title"],
            "component_count": 1,
            "skin_ids": ["dark-data-presenter"],
            "skin_count": 1,
            "longest_no_change_gap_s": 0.0,
            "motion_requested_count": 1,
            "motion_faithful_count": 1,
            "motion_fallback_count": 0,
            "motion_faithful_ratio": 1.0,
            "failures": [],
            "warnings": [],
        }
        state = self._install_synthetic_final_delivery(
            report_payload={
                "schema_version": 3,
                "status": "pass",
                "profile": "strict",
                "policy": SYNTHETIC_QA_POLICY,
                "visual_delivery": visual_delivery,
                "failures": [],
                "warnings": [],
            }
        )
        errors = editor_server.delivery_qa_errors(self.project, state)
        self.assertTrue(any("visual delivery evidence" in item for item in errors), errors)

        receipt_path = self.project / "working/latest_final_qa.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["visual_delivery"] = visual_delivery
        self.write_json("working/latest_final_qa.json", receipt)
        errors = editor_server.delivery_qa_errors(self.project, state)
        self.assertFalse(any("visual delivery evidence" in item for item in errors), errors)

    def test_schema3_variant_requires_matching_passing_visual_evidence(self) -> None:
        visual_delivery = {
            "schema_version": 1,
            "source": "renderer_evidence",
            "status": "pass",
            "duration_s": 2.0,
            "minimum_primary_font_px": 48.0,
            "expected_visual_beat_count": 1,
            "visual_beat_count": 1,
            "component_ids": ["title"],
            "component_count": 1,
            "skin_ids": ["dark-data-presenter"],
            "skin_count": 1,
            "longest_no_change_gap_s": 0.0,
            "motion_requested_count": 1,
            "motion_faithful_count": 1,
            "motion_fallback_count": 0,
            "motion_faithful_ratio": 1.0,
            "failures": [],
            "warnings": [],
        }
        report_payload = {
            "schema_version": 3,
            "status": "pass",
            "profile": "strict",
            "policy": SYNTHETIC_QA_POLICY,
            "visual_delivery": visual_delivery,
            "failures": [],
            "warnings": [],
        }
        report_text = json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n"
        report_path = self.project / "qa/variant-x.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
        receipt = {
            "report": "qa/variant-x.json",
            "report_sha256": self._sha256_bytes(report_text.encode("utf-8")),
        }

        errors = editor_server._variant_report_errors(
            self.project, receipt, "variant-x"
        )
        self.assertTrue(any("visual delivery evidence" in item for item in errors), errors)

        receipt["visual_delivery"] = visual_delivery
        errors = editor_server._variant_report_errors(
            self.project, receipt, "variant-x"
        )
        self.assertFalse(any("visual delivery evidence" in item for item in errors), errors)

    def test_pre_policy_variant_qa_report_blocks_variant_download(self) -> None:
        # The variant download slot must re-verify the QA report: a
        # hash-consistent variant delivery whose report lacks the enforced
        # policy block (pre-policy pipeline output) stays locked.
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        state["variants"] = [
            {"variant_id": "variant-x", "preset_id": "youtube-landscape", "overrides": []}
        ]
        self.write_json("working/editor_state.json", state)
        output_bytes = b"variant-final-bytes"
        (self.project / "renders/variant-x-final.mp4").write_bytes(output_bytes)
        report_payload = {"status": "pass", "failures": [], "warnings": []}
        report_text = json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n"
        (self.project / "qa").mkdir(parents=True, exist_ok=True)
        (self.project / "qa/variant-x.json").write_text(report_text, encoding="utf-8")
        self.write_json(
            "working/render_receipts/render-variant-x.json",
            {
                "schema_version": 1,
                "render_id": "render-variant-x",
                "quality": "final",
                "output": "renders/variant-x-final.mp4",
            },
        )
        self.write_json(
            "working/delivery_qa/variant-x.json",
            {
                "schema_version": 1,
                "variant_id": "variant-x",
                "status": "pass",
                "output": "renders/variant-x-final.mp4",
                "output_sha256": self._sha256_bytes(output_bytes),
                "report": "qa/variant-x.json",
                "report_sha256": self._sha256_bytes(report_text.encode("utf-8")),
            },
        )
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revision = editor_server.variant_gate_revision(
            self.project, "final", state, "variant-x"
        )
        manifest.setdefault("approvals", {})["final_by_variant"] = {
            "variant-x": {"approved": True, "state_revision": revision}
        }
        self.write_json("project.json", manifest)
        errors = editor_server.render_download_errors(
            self.project, "renders/variant-x-final.mp4"
        )
        self.assertTrue(
            any("predates the enforced QA policy" in item for item in errors), errors
        )
        status, _headers, _body = self.request("GET", "/renders/variant-x-final.mp4")
        self.assertEqual(status, 403)

        # Intermediate-directory symlink escape: a receipt whose report path
        # tunnels through a project-internal symlink to an outside file must
        # stay rejected even with a matching sha256.
        outside = Path(self._tmp.name) / "outside-qa"
        outside.mkdir(parents=True, exist_ok=True)
        outside_report = outside / "outside-report.json"
        outside_text = json.dumps(
            {"status": "pass", "policy": SYNTHETIC_QA_POLICY, "failures": [], "warnings": []}
        )
        outside_report.write_text(outside_text, encoding="utf-8")
        (self.project / "qalink").symlink_to(outside, target_is_directory=True)
        receipt = json.loads(
            (self.project / "working/delivery_qa/variant-x.json").read_text(encoding="utf-8")
        )
        receipt["report"] = "qalink/outside-report.json"
        receipt["report_sha256"] = self._sha256_bytes(outside_text.encode("utf-8"))
        self.write_json("working/delivery_qa/variant-x.json", receipt)
        errors = editor_server._variant_report_errors(self.project, receipt, "variant-x")
        self.assertTrue(
            any("missing or does not match" in item for item in errors), errors
        )

        # A symlinked report inside qa/ must be rejected too, matching the
        # single and batch paths.
        inside_target = self.project / "qa/other-passing.json"
        inside_text = json.dumps(
            {"status": "pass", "policy": SYNTHETIC_QA_POLICY, "failures": [], "warnings": []}
        )
        inside_target.write_text(inside_text, encoding="utf-8")
        linked = self.project / "qa/variant-linked.json"
        linked.symlink_to(inside_target)
        receipt["report"] = "qa/variant-linked.json"
        receipt["report_sha256"] = self._sha256_bytes(inside_text.encode("utf-8"))
        self.write_json("working/delivery_qa/variant-x.json", receipt)
        errors = editor_server._variant_report_errors(self.project, receipt, "variant-x")
        self.assertTrue(
            any("missing or does not match" in item for item in errors), errors
        )

    def test_qa_policy_declaration_is_closed_and_binds_approvals(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        before = editor_server.editor_state_revision(state)

        # Closed schema: only a known profile with a stated intent.
        for bad in (
            {"profile": "anything_goes", "intent": "x"},
            {"profile": "silent_delivery"},
            {"profile": "silent_delivery", "intent": "  "},
            {"profile": "strict", "extra": 1},
            "silent_delivery",
        ):
            with self.subTest(repr(bad)):
                self.assertTrue(editor_server.qa_policy_errors(bad), f"{bad!r} must be rejected")
        self.assertEqual(editor_server.qa_policy_errors(None), [])
        self.assertEqual(
            editor_server.qa_policy_errors({"profile": "silent_delivery", "intent": "b-roll"}),
            [],
        )

        # Declaring a different kind of delivery re-opens every approval.
        state["qa_policy"] = {"profile": "silent_delivery", "intent": "b-roll"}
        self.assertNotEqual(editor_server.editor_state_revision(state), before)
        self.assertEqual(
            editor_server.qa_policy_args(state),
            ["--qa-profile", "silent_delivery", "--qa-intent", "b-roll"],
        )
        self.assertEqual(editor_server.qa_policy_args({"qa_policy": {"profile": "strict"}}), [])

    def test_report_profile_must_match_what_the_project_authorizes(self) -> None:
        strict_state: dict[str, object] = {}
        relaxed_state = {"qa_policy": {"profile": "silent_delivery", "intent": "b-roll"}}
        import dataclasses as _dc
        import qa_video as _qa

        def report_for(profile: str, intent: str = "b-roll") -> dict[str, object]:
            policy = _dc.asdict(_qa.QaPolicy.for_profile(profile, intent))
            return {"schema_version": 2, "profile": profile, "policy": policy}

        relaxed_report = report_for("silent_delivery")
        strict_report = report_for("strict", "")
        legacy_report = {"schema_version": 1, "policy": {"allow_missing_audio": False}}
        forged_legacy = {
            "schema_version": 1,
            "policy": {"allow_missing_audio": True, "allow_silent_delivery": True},
        }
        mislabelled = report_for("strict", "")
        mislabelled["profile"] = "silent_delivery"

        # A relaxed report cannot be presented to a project that never
        # authorized relaxing anything.
        self.assertTrue(
            editor_server.qa_profile_binding_errors(relaxed_report, strict_state, "delivery")
        )
        # Nor can a report that does not say what it ran under.
        self.assertTrue(
            editor_server.qa_profile_binding_errors(
                {"schema_version": 2, "policy": {}}, strict_state, "delivery"
            )
        )
        self.assertEqual(
            editor_server.qa_profile_binding_errors(strict_report, strict_state, "delivery"), []
        )
        self.assertEqual(
            editor_server.qa_profile_binding_errors(relaxed_report, relaxed_state, "delivery"), []
        )
        # Reports predating profiles could only have run strict, so they stay
        # valid for a strict project and are rejected for a relaxed one.
        self.assertEqual(
            editor_server.qa_profile_binding_errors(legacy_report, strict_state, "delivery"), []
        )
        self.assertTrue(
            editor_server.qa_profile_binding_errors(legacy_report, relaxed_state, "delivery")
        )
        # A report claiming to predate profiles cannot carry a relaxation.
        self.assertTrue(
            editor_server.qa_profile_binding_errors(forged_legacy, strict_state, "delivery")
        )
        # Nor can the label disagree with the thresholds actually applied.
        self.assertTrue(
            editor_server.qa_profile_binding_errors(mislabelled, strict_state, "delivery")
        )
        # A v2 report whose thresholds were loosened after the fact is rejected
        # even though it names the authorized profile.
        tampered = report_for("strict", "")
        tampered["policy"]["min_audible_ratio"] = 0.0
        self.assertTrue(
            editor_server.qa_profile_binding_errors(tampered, strict_state, "delivery")
        )

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

    def test_effect_span_final_gate_follows_route_table(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("viral_structure_plan", payload)
        self.assertIn("narrative_plan", payload)
        has_spans = any(
            overlay.get("effect_spans") for overlay in state.get("overlays", [])
        )
        self.assertTrue(has_spans, "default state should carry effect spans")

        import caption_compositor

        if caption_compositor.compositor_available():
            # Route table: compositor renders spans on every route — no gate.
            self.assertEqual(editor_server.effect_span_final_errors(state, None), [])
        with unittest.mock.patch.object(
            caption_compositor, "compositor_available", lambda: False
        ):
            errors = editor_server.effect_span_final_errors(state, None)
            self.assertTrue(errors and "compositor" in errors[0])
            stripped = json.loads(json.dumps(state))
            for overlay in stripped.get("overlays", []):
                overlay.pop("effect_spans", None)
            self.assertEqual(editor_server.effect_span_final_errors(stripped, None), [])

    def test_platform_presets_come_from_validated_registry(self) -> None:
        registry = json.loads(
            (SKILL_DIR / "contracts/instances/platform_preset__registry.json").read_text(
                encoding="utf-8"
            )
        )
        registry_ids = {preset["id"] for preset in registry["presets"]}
        self.assertEqual(set(editor_server.PLATFORM_PRESETS), registry_ids)
        for preset in registry["presets"]:
            runtime = editor_server.PLATFORM_PRESETS[preset["id"]]
            for key in ("label", "width", "height", "aspect", "fps", "safe",
                        "cover_width", "cover_height", "review_due_at"):
                self.assertEqual(runtime[key], preset[key], f"{preset['id']}.{key}")

    def test_director_registry_matches_code_presets(self) -> None:
        registry = json.loads(
            (SKILL_DIR / "contracts/instances/director_mode__registry.json").read_text(
                encoding="utf-8"
            )
        )
        by_id = {mode["id"]: mode for mode in registry["modes"]}
        self.assertEqual(set(by_id), set(editor_server.DIRECTOR_PRESETS))
        for mode_id, preset in editor_server.DIRECTOR_PRESETS.items():
            mode = by_id[mode_id]
            for key, value in mode["constraints"].items():
                self.assertEqual(preset[key], value, f"{mode_id}.{key}")
            for key in ("cut_density", "motion_intensity"):
                self.assertEqual(preset[key], mode["envelope"][key], f"{mode_id}.{key}")

    def test_editor_ui_renders_formula_panel_and_stale_marker(self) -> None:
        app_js = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")
        self.assertIn("renderFormulaPanel(projectPayload)", app_js)
        self.assertIn("evidence re-anchor", app_js)
        self.assertIn("規格待重核", app_js)

    def test_caption_snap_endpoint_and_boundary_validation(self) -> None:
        payload = {"text": "我愛👩‍👩‍👧‍👦你", "start_char": 3, "end_char": 5}
        status, snapped = self.json_request("POST", "/api/captions/snap", payload)
        self.assertEqual(status, 200, snapped)
        self.assertFalse(snapped["removed"])
        self.assertEqual((snapped["start_char"], snapped["end_char"]), (2, 13))
        self.assertEqual(snapped["text"], "👩‍👩‍👧‍👦")

        status, _headers, body = self.request("GET", "/api/project")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("caption_engine", payload)
        state = payload["state"]
        state["overlays"] = [
            {
                "id": "caption-emoji",
                "type": "caption",
                "text": "我愛👩‍👩‍👧‍👦你",
                "start": 0.0,
                "end": 1.0,
                "visible": True,
                "style": {},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
                "effect_spans": [
                    {
                        "id": "fx-bad",
                        "text": "愛👩",
                        "start_char": 1,
                        "end_char": 4,
                        "style": {"effect": "pop", "color": "#FF5533", "font_scale": 1.2},
                    }
                ],
            }
        ]
        status, rejected = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 422, rejected)
        self.assertTrue(
            any("grapheme" in str(e) or "surrogate" in str(e) for e in rejected["errors"]),
            rejected,
        )

        state["overlays"][0]["effect_spans"] = [
            {
                "id": "fx-good",
                "text": "👩‍👩‍👧‍👦",
                "start_char": 2,
                "end_char": 13,
                "style": {"effect": "pop", "color": "#FF5533", "font_scale": 1.2},
            }
        ]
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)

    def test_legacy_span_migration_snaps_or_removes(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        state["overlays"] = [
            {
                "id": "caption-legacy",
                "type": "caption",
                "text": "去🇹🇼旅行",
                "start": 0.0,
                "end": 1.0,
                "visible": True,
                "style": {},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
                "effect_spans": [
                    {
                        "id": "fx-mid-flag",
                        "text": "🇹",
                        "start_char": 1,
                        "end_char": 3,
                        "style": {"effect": "highlight", "color": "#F5A623", "font_scale": 1.1},
                    }
                ],
            }
        ]
        # Write directly to disk (legacy state that predates boundary rules).
        self.write_json("working/editor_state.json", state)
        status, _headers, body = self.request("GET", "/api/project")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        warnings = payload["caption_span_migration"]
        self.assertTrue(warnings, "migration must report span adjustments")
        migrated = payload["state"]["overlays"][0]["effect_spans"]
        self.assertEqual(len(migrated), 1)
        self.assertEqual(
            (migrated[0]["start_char"], migrated[0]["end_char"]), (1, 5),
            "span must snap outward to the full flag cluster",
        )
        self.assertEqual(migrated[0]["text"], "🇹🇼")

    def test_caption_status_immutable_png_and_apply_style(self) -> None:
        import caption_compositor

        if not caption_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        captions = [
            o for o in state.get("overlays", [])
            if o.get("type") in {"caption", "emphasis"} and not o.get("design_role")
        ]
        self.assertTrue(captions, "default state should include captions")
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)

        ready_payload = None
        for _ in range(60):
            status, _headers, body = self.request("GET", "/api/captions/status")
            self.assertEqual(status, 200)
            payload = json.loads(body.decode("utf-8"))
            if payload.get("ready"):
                ready_payload = payload
                break
            time.sleep(0.25)
        self.assertIsNotNone(ready_payload, "caption render job never became ready")
        self.assertTrue(ready_payload["items"])
        item = ready_payload["items"][0]

        status, headers, png = self.request("GET", item["url"])
        self.assertEqual(status, 200)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("immutable", headers.get("Cache-Control", ""))

        bogus = item["url"].replace(item["artifact_hash"], "0" * 64)
        status, _headers, _body = self.request("GET", bogus)
        self.assertEqual(status, 404, "wrong artifact hash must not serve a file")

        target = captions[0]["id"]
        status, applied = self.json_request(
            "POST",
            "/api/captions/apply-style",
            {"scope": "track", "overlay_id": target, "style": {"color": "#00CC88"}},
        )
        self.assertEqual(status, 200, applied)
        self.assertGreaterEqual(len(applied["applied_to"]), len(captions))
        persisted = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        for overlay in persisted["overlays"]:
            if overlay.get("type") in {"caption", "emphasis"} and not overlay.get("design_role"):
                self.assertEqual(overlay["style"].get("color"), "#00CC88")

        status, rejected = self.json_request(
            "POST",
            "/api/captions/apply-style",
            {"scope": "single", "overlay_id": target, "style": {"start": 99}},
        )
        self.assertEqual(status, 422, rejected)

    def test_put_cas_rejects_stale_revision(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        stale = json.loads(json.dumps(state))
        stale["x_expected_revision"] = "0" * 64
        status, payload = self.json_request("PUT", "/api/editor-state", stale)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("error_code"), "revision_conflict")

    def test_tampered_caption_png_is_not_served(self) -> None:
        import caption_compositor

        if not caption_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        status, _saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200)
        for _ in range(60):
            status, _headers, body = self.request("GET", "/api/captions/status")
            payload = json.loads(body.decode("utf-8"))
            if payload.get("ready"):
                break
            time.sleep(0.25)
        self.assertTrue(payload.get("ready"))
        item = payload["items"][0]
        plan = json.loads(
            (self.project / "working/caption_render_plan.json").read_text("utf-8")
        )
        png = self.project / plan["items"][0]["artifact"]["rgba_path"]
        png.write_bytes(b"\x89PNG\r\n\x1a\ntampered")
        status, _headers, _body = self.request("GET", item["url"])
        self.assertEqual(status, 404, "tampered artifact bytes must never be served")

    def test_structured_layer_crud_transaction_and_gate_staleness(self) -> None:
        import structured_card_compositor

        if not structured_card_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        before_timeline = gate_revision(self.project, "timeline", state)

        status, created = self.json_request(
            "POST",
            "/api/structured-layers",
            {
                "action": "upsert",
                "layer": {
                    "type": "stat",
                    "payload": {
                        "value": "87%",
                        "label": "留存率",
                        "evidence_id": "evidence-abcdef01",
                        "source_literal": "留存是87%",
                    },
                },
                "timing": {"start": 0.1, "end": 0.4},
            },
        )
        self.assertEqual(status, 200, created)
        self.assertEqual(created["capability"]["status"], "static_fallback")
        layer_id = created["layers"]["items"][0]["id"]
        plan_item = created["visual_plan"]["items"][0]
        self.assertEqual(plan_item["structured_layer_id"], layer_id)
        self.assertEqual(plan_item["start"], 0.1)
        self.assertNotIn("start", created["layers"]["items"][0], "timing SSOT: envelope has no timing")
        artifact = created["artifacts"]["items"][0]
        self.assertTrue((self.project / artifact["artifact_id"]).is_file())

        after_timeline = gate_revision(self.project, "timeline", state)
        self.assertNotEqual(
            before_timeline, after_timeline,
            "layer edits must invalidate the timeline gate revision",
        )

        # factual layer without evidence must be rejected by the bundle gate
        status, rejected = self.json_request(
            "POST",
            "/api/structured-layers",
            {
                "action": "upsert",
                "layer": {"type": "stat", "payload": {"value": "1", "label": "x"}},
                "timing": {"start": 0.1, "end": 0.2},
            },
        )
        self.assertEqual(status, 422, rejected)

        # renderer picks the card PNG as an overlay
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        renderer_evidence = {}
        command = build_render_command(
            self.project, state, manifest,
            self.project / "renders/layer-route.mp4", "preview",
            visual_evidence=renderer_evidence,
        )
        self.assertIn("working/structured_cards/", " ".join(command))
        self.assertEqual(renderer_evidence["source"], "renderer_evidence_raw")
        self.assertGreaterEqual(renderer_evidence["visual_beat_count"], 1)
        stat_evidence = next(
            item for item in renderer_evidence["items"] if item["kind"] == "stat"
        )
        self.assertIn("motion", stat_evidence)

        # delete removes both sides transactionally
        status, deleted = self.json_request(
            "POST", "/api/structured-layers", {"action": "delete", "id": layer_id}
        )
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["layers"]["items"], [])
        self.assertEqual(deleted["visual_plan"]["items"], [])

    def test_layer_transaction_journal_rolls_forward(self) -> None:
        layers = {"schema_version": 1, "items": []}
        plan = {
            "schema_version": 1,
            "highlight_plan_revision": "0" * 64,
            "items": [],
            "revision": "1" * 64,
        }
        editor_server.atomic_write_json(
            self.project / editor_server.LAYER_TXN_JOURNAL_REL,
            {
                "generation": "t",
                "files": {
                    editor_server.LAYERS_REL.as_posix(): layers,
                    editor_server.VISUAL_PLAN_REL.as_posix(): plan,
                },
            },
        )
        # simulate the crash: journal exists, target files half-written
        (self.project / editor_server.LAYERS_REL).unlink(missing_ok=True)
        loaded_layers, loaded_plan = editor_server.load_layer_bundle(self.project)
        self.assertEqual(loaded_layers, layers)
        self.assertEqual(loaded_plan["revision"], "1" * 64)
        self.assertFalse(
            (self.project / editor_server.LAYER_TXN_JOURNAL_REL).is_file(),
            "journal must clear after roll-forward",
        )

    def test_rights_gate_closure_assert_and_hash_invalidation(self) -> None:
        # a referenced project asset appears in the closure and blocks final
        (self.project / "assets/imported").mkdir(parents=True, exist_ok=True)
        asset = self.project / "assets/imported/broll.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"broll-bytes")
        status, _headers, body = self.request("GET", "/api/project")
        state = json.loads(body.decode("utf-8"))["state"]
        state["overlays"] = [
            {
                "id": "broll-1",
                "type": "image",
                "source": "assets/imported/broll.png",
                "start": 0.1,
                "end": 0.5,
                "visible": True,
                "style": {"width": 40, "x": 50, "y": 50},
                "layout": {"x": 10, "y": 10, "width": 40, "height": 40},
            }
        ]
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)

        status, _headers, body = self.request("GET", "/api/rights")
        payload = json.loads(body.decode("utf-8"))
        target = next(i for i in payload["inputs"] if i["path"].endswith("broll.png"))
        self.assertTrue(target["requires_assertion"])
        self.assertFalse(target["asserted"])

        state = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(errors and "rights assertion" in errors[0])

        status, asserted = self.json_request(
            "POST",
            "/api/rights/assert",
            {"asset_path": "assets/imported/broll.png", "basis": "own_work"},
        )
        self.assertEqual(status, 200, asserted)
        self.assertEqual(editor_server.rights_gate_errors(self.project, state), [])

        # licensed without proof must be rejected by the contract
        status, rejected = self.json_request(
            "POST",
            "/api/rights/assert",
            {"asset_path": "assets/imported/broll.png", "basis": "licensed"},
        )
        self.assertEqual(status, 422, rejected)

        # changing the file bytes voids the assertion (sha binding)
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"different-bytes")
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(errors, "hash change must invalidate the assertion")

    def test_current_provider_license_bypasses_assertion_but_tamper_blocks_final(self) -> None:
        asset = self.project / "assets/providers/openverse/licensed.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG\r\n\x1a\nlicensed")
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        editor_server.asset_registry.upsert_item(
            self.project,
            {
                "asset_id": "provider-openverse-licensed",
                "path": "assets/providers/openverse/licensed.png",
                "sha256": digest,
                "origin": "provider",
                "provider_id": "openverse",
                "source_url": "https://example.org/licensed",
                "license": {
                    "spdx": "CC-BY-4.0",
                    "evidence_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution_required": True,
                    "attribution_text": "Jane Example",
                    "verified_at": "2026-08-04T00:00:00Z",
                },
                "review_status": "approved",
            },
        )
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body)["state"]
        state["overlays"] = [
            {
                "id": "provider-image",
                "type": "image",
                "source": "assets/providers/openverse/licensed.png",
                "start": 0.1,
                "end": 0.5,
                "visible": True,
                "style": {"width": 40, "x": 50, "y": 50},
                "layout": {"x": 10, "y": 10, "width": 40, "height": 40},
            }
        ]

        self.assertTrue(
            editor_server.rights_gate_errors(self.project, state),
            "registry metadata alone must not auto-approve a provider asset",
        )
        registered = editor_server.asset_registry.load_registry(self.project)["items"][0]
        editor_server.asset_registry.save_provider_receipt(
            self.project,
            registered,
            candidate_id="licensed",
            download_url="https://api.openverse.org/v1/images/licensed/thumb/",
        )

        referenced = editor_server.referenced_render_inputs(self.project, state)
        provider_input = next(item for item in referenced if item["path"].endswith("licensed.png"))
        self.assertFalse(provider_input["requires_assertion"])
        self.assertEqual(provider_input["license_status"], "provider-approved")
        self.assertEqual(editor_server.rights_gate_errors(self.project, state), [])

        (self.project / "ATTRIBUTION.md").write_text("tampered\n", encoding="utf-8")
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(any("ATTRIBUTION" in error for error in errors))

        editor_server.asset_registry.refresh_attribution(self.project)
        asset.write_bytes(b"\x89PNG\r\n\x1a\ntampered")
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(any("provenance" in error for error in errors))

    def test_generated_svg_png_requires_provider_receipt_even_with_manual_assertion(self) -> None:
        def png_chunk(kind: bytes, payload: bytes) -> bytes:
            checksum = binascii.crc32(kind)
            checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

        png_bytes = b"\x89PNG\r\n\x1a\n" + png_chunk(
            b"IHDR", struct.pack(">IIBBBBB", 24, 24, 8, 6, 0, 0, 0)
        ) + png_chunk(
            b"IDAT", zlib.compress(b"".join(b"\0" + b"\x33" * (24 * 4) for _ in range(24)))
        ) + png_chunk(b"IEND", b"")
        png_digest = hashlib.sha256(png_bytes).hexdigest()
        png = self.project / f"assets/generated/svg/{png_digest}.png"
        png.parent.mkdir(parents=True)
        png.write_bytes(png_bytes)
        png_relative = png.relative_to(self.project).as_posix()
        state = {
            "overlays": [
                {
                    "id": "generated-svg",
                    "type": "image",
                    "source": png_relative,
                }
            ]
        }

        # A manual rights assertion must not turn an unregistered generated
        # SVG derivative into a final-eligible input.
        status, payload = self.json_request(
            "POST",
            "/api/rights/assert",
            {"asset_path": png_relative, "basis": "own_work"},
        )
        self.assertEqual(status, 200, payload)
        inputs = editor_server.referenced_render_inputs(self.project, state)
        self.assertEqual(inputs[0]["license_status"], "provider-provenance-missing")
        self.assertTrue(inputs[0]["requires_assertion"])
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(any("provider provenance" in error for error in errors), errors)
        self.assertFalse(any("rights assertion" in error for error in errors), errors)

        digest = hashlib.sha256(png.read_bytes()).hexdigest()
        item = {
            "asset_id": f"provider-heroicons-arrow-right-{digest[:16]}",
            "path": png_relative,
            "sha256": digest,
            "origin": "provider",
            "provider_id": "heroicons",
            "source_url": "https://github.com/tailwindlabs/heroicons/blob/0435d4ca364a608cc75e2f8683d374e55abbae26/optimized/24/outline/arrow-right.svg",
            "license": {
                "spdx": "MIT",
                "evidence_url": "https://github.com/tailwindlabs/heroicons/blob/0435d4ca364a608cc75e2f8683d374e55abbae26/LICENSE",
                "attribution_required": True,
                "attribution_text": "Tailwind Labs",
                "verified_at": "2026-08-04T00:00:00Z",
            },
            "review_status": "approved",
        }
        editor_server.asset_registry.upsert_item(self.project, item)
        errors = editor_server.rights_gate_errors(self.project, state)
        self.assertTrue(any("provider provenance" in error for error in errors), errors)

        raw_bytes = b"<svg/>"
        sanitized_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        sanitized_digest = hashlib.sha256(sanitized_bytes).hexdigest()
        raw = self.project / f"working/source_artifacts/svg/{raw_digest}.svg.untrusted"
        sanitized = self.project / f"working/sanitized_svg/{sanitized_digest}.svg"
        raw.parent.mkdir(parents=True, exist_ok=True)
        sanitized.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(raw_bytes)
        sanitized.write_bytes(sanitized_bytes)
        from svg_security import LIMITS_SHA256, POLICY_VERSION, SANITIZER_VERSION

        query_hash = hashlib.sha256(b"arrow-right").hexdigest()
        cache_key = editor_server.asset_registry.contract_registry.canonical_hash(
            {
                "raw_sha256": raw_digest,
                "policy_version": POLICY_VERSION,
                "sanitizer_version": SANITIZER_VERSION,
                "limits_sha256": LIMITS_SHA256,
            }
        )
        editor_server.asset_registry.save_svg_provider_receipt(
            self.project,
            item,
            candidate_id="arrow-right",
            query_hash=query_hash,
            download_url="https://raw.githubusercontent.com/tailwindlabs/heroicons/0435d4ca364a608cc75e2f8683d374e55abbae26/optimized/24/outline/arrow-right.svg",
            raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            raw_path=raw.relative_to(self.project).as_posix(),
            raw_size=raw.stat().st_size,
            sanitized_sha256=sanitized_digest,
            sanitized_path=sanitized.relative_to(self.project).as_posix(),
            sanitized_size=sanitized.stat().st_size,
            png_size=png.stat().st_size,
            png_width=24,
            png_height=24,
            sanitizer_identity={
                "policy_version": POLICY_VERSION,
                "sanitizer_version": SANITIZER_VERSION,
                "limits_sha256": LIMITS_SHA256,
                "sanitize_cache_key_sha256": cache_key,
            },
            rasterizer_identity={
                "version": "resvg-test-1",
                "executable_sha256": "c" * 64,
                "sandbox_executable_sha256": "d" * 64,
                "sandbox_profile_sha256": "e" * 64,
            },
        )
        self.assertEqual(
            editor_server.referenced_render_inputs(self.project, state)[0]["license_status"],
            "provider-approved",
        )
        self.assertEqual(editor_server.rights_gate_errors(self.project, state), [])


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
        self.rebuild_source(audio_source="sine=frequency=440:sample_rate=48000")
        self._write_render_test_manifest()

    def rebuild_source(self, audio_source: str) -> None:
        """(Re)encode the shared 0.45s source clip with the given lavfi audio."""
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
                audio_source,
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

    def _write_render_test_manifest(self) -> None:
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
            "state_revision": gate_revision(self.project, "timeline", state),
        }
        self.write_json("project.json", manifest)
        self.run_renderer("--quality", "final", "--output", str(final))
        self.assertTrue(final.is_file())
        import render_editor_timeline

        direct_render_id = render_editor_timeline.direct_final_render_id(state, final)
        evidence_path = editor_server.rendered_visual_evidence_path(
            self.project, direct_render_id
        )
        self.assertTrue(evidence_path.is_file())
        direct_report = json.loads(
            (self.project / f"qa/{direct_render_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(direct_report["schema_version"], 3)
        self.assertEqual(direct_report["video"], str(final.resolve()))
        self.assertEqual(direct_report["visual_delivery"]["source"], "renderer_evidence")
        self.assertEqual(direct_report["visual_delivery"]["status"], "pass")
        envelope_path = (
            self.project
            / "working/delivery_envelopes"
            / f"{direct_render_id}.json"
        )
        self.assertTrue(envelope_path.is_file())
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["route"], "direct")
        self.assertEqual(envelope["state"], "finalized")
        self.assertEqual(envelope["quality"], "final")
        self.assertEqual(envelope["render_id"], direct_render_id)
        self.assertEqual(envelope["profile"]["id"], "teacher-punch")
        self.assertRegex(envelope["profile"]["resolved_profile_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            envelope["timeline"]["editor_state_revision"],
            editor_server.editor_state_revision(state),
        )
        self.assertIsNone(envelope["timeline"]["cut_map_sha256"])
        self.assertEqual(
            envelope["artifacts"]["output"]["sha256"],
            editor_server.file_sha256(final),
        )
        self.assertEqual(
            envelope["artifacts"]["qa_report"]["sha256"],
            editor_server.file_sha256(self.project / f"qa/{direct_render_id}.json"),
        )
        self.assertEqual(
            envelope["artifacts"]["contact_sheet"]["sha256"],
            editor_server.file_sha256(
                self.project / f"qa/{direct_render_id}-contact.png"
            ),
        )
        visual_path = editor_server.rendered_visual_evidence_path(
            self.project, direct_render_id
        )
        self.assertEqual(
            envelope["artifacts"]["visual_evidence"]["sha256"],
            editor_server.file_sha256(visual_path),
        )
        self.assertEqual(
            envelope["artifacts"]["motion_evidence"],
            envelope["artifacts"]["visual_evidence"],
        )
        for optional_artifact in (
            "caption_v2",
            "audio_event_plan",
            "audio_catalog",
            "sfx_stem",
        ):
            self.assertIsNone(envelope["artifacts"][optional_artifact])
        self.assertEqual(
            envelope["renderer_identity"]["name"], "render_editor_timeline"
        )
        self.assertRegex(
            envelope["renderer_identity"]["script_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            envelope["renderer_identity"]["ffmpeg_executable_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(envelope["prepared_envelope_hash"], r"^[0-9a-f]{64}$")
        staging_root = self.project / "working/delivery_envelopes/.staging"
        stage_residue = (
            [entry for entry in staging_root.iterdir() if entry.name != ".locks"]
            if staging_root.is_dir()
            else []
        )
        self.assertEqual(stage_residue, [])
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

    def test_direct_final_visual_failure_is_not_published(self) -> None:
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"].append(
            {
                "id": "tiny-card",
                "type": "card",
                "text": "這張字卡太小",
                "start": 0.05,
                "end": 0.35,
                "visible": True,
                "style": {"font_size": 31},
                "layout": {},
            }
        )
        self.write_json("working/editor_state.json", state)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": gate_revision(self.project, "timeline", state),
        }
        self.write_json("project.json", manifest)
        final = self.project / "renders/tiny-direct-final.mp4"

        result = self.run_renderer(
            "--quality", "final", "--output", str(final), expected=2
        )

        self.assertFalse(final.exists())
        self.assertIn("QA failed", result.stderr + result.stdout)

    def test_required_caption_missing_fails_before_direct_staging_and_preserves_output(self) -> None:
        transcript_source = {
            "schema_version": 1,
            "revision": "",
            "source_media_sha256": "a" * 64,
            "audio_stream_index": 0,
            "decoded_pcm": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_format": "s16le",
                "sha256": "b" * 64,
            },
            "engine": "openai-whisper",
            "engine_version": "1.0",
            "model": "base",
            "language": "zh",
            "decoding_params": {},
            "source_generation": 0,
            "raw_words": [
                {
                    "source_word_index": 0,
                    "start_us": 20_000,
                    "end_us": 400_000,
                    "text": "核心重點",
                    "speaker": None,
                }
            ],
        }
        material = dict(transcript_source)
        material.pop("revision")
        transcript_source["revision"] = contract_registry.canonical_hash(material)
        source_path = (
            self.project
            / f"working/transcript_sources/{transcript_source['revision']}.json"
        )
        caption_delivery._atomic_write(source_path, transcript_source)
        caption_delivery._atomic_write(
            self.project / "working/transcript_source_current.json",
            {
                "schema_version": 1,
                "revision": transcript_source["revision"],
                "path": f"working/transcript_sources/{transcript_source['revision']}.json",
                "artifact_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            },
        )
        self.write_json(
            "working/transcript_words.json",
            {
                "words": [
                    {"id": "word-00001", "text": "核心重點", "start": 0.02, "end": 0.40}
                ],
                "caption_segments": [
                    {
                        "id": "caption-segment-0001",
                        "text": "核心重點",
                        "start": 0.02,
                        "end": 0.40,
                        "word_ids": ["word-00001"],
                    }
                ],
            },
        )
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"][0]["source"] = "working/transcript_words.json"
        self.write_json("working/editor_state.json", state)
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest["subtitles"] = {
            "glossary": [],
            "contextual_semantic_calibration": {"model": "qwen2.5:7b"},
        }
        self.write_json("project.json", manifest)

        def fake_model(prompt: str, _stage: str, **_kwargs):
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return {
                "items": [
                    {
                        "caption_instance_id": requested[0]["caption_instance_id"],
                        "translated_text": "Core point",
                    }
                ]
            }

        caption_delivery.create_delivery(
            self.project,
            "en",
            required=True,
            model_call=fake_model,
        )
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": gate_revision(self.project, "timeline", state),
        }
        self.write_json("project.json", manifest)
        successful = self.project / "renders/caption-bound-success.mp4"
        self.run_renderer("--quality", "final", "--output", str(successful))
        success_render_id = direct_final_render_id(state, successful)
        success_envelope = json.loads(
            (
                self.project
                / f"working/delivery_envelopes/{success_render_id}.json"
            ).read_text(encoding="utf-8")
        )
        canonical_caption = self.project / caption_delivery.CAPTION_REL
        self.assertEqual(
            success_envelope["artifacts"]["caption_v2"]["path"],
            caption_delivery.CAPTION_REL.as_posix(),
        )
        self.assertEqual(
            success_envelope["artifacts"]["caption_v2"]["sha256"],
            hashlib.sha256(canonical_caption.read_bytes()).hexdigest(),
        )
        caption_artifact = json.loads(canonical_caption.read_text(encoding="utf-8"))
        render_plan = json.loads(
            (self.project / "working/caption_render_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["caption_item_id"] for item in render_plan["items"]],
            [item["caption_instance_id"] for item in caption_artifact["items"]],
        )

        race_output = self.project / "renders/caption-copy-race.mp4"
        race_output.write_bytes(b"prior-copy-race-output")
        finalized_before_race = set(
            (self.project / "working/delivery_envelopes").glob("*.json")
        )
        canonical_before_race = canonical_caption.read_bytes()
        real_copyfile = render_editor_timeline.shutil.copyfile

        def mutate_caption_during_copy(source, destination, *args, **kwargs):
            if Path(source).resolve() == canonical_caption.resolve():
                tampered = json.loads(canonical_caption.read_text(encoding="utf-8"))
                tampered["items"][0]["translated_text"] = "copy-race-tamper"
                caption_delivery._atomic_write(canonical_caption, tampered)
            return real_copyfile(source, destination, *args, **kwargs)

        with patch.object(
            render_editor_timeline.shutil,
            "copyfile",
            side_effect=mutate_caption_during_copy,
        ):
            with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
                render_editor_timeline.render_project(
                    self.project.resolve(),
                    race_output.resolve(),
                    "final",
                )
        self.assertEqual(caught.exception.code, "caption_binding_missing")
        self.assertEqual(race_output.read_bytes(), b"prior-copy-race-output")
        self.assertEqual(
            set((self.project / "working/delivery_envelopes").glob("*.json")),
            finalized_before_race,
        )
        caption_delivery._atomic_write(
            canonical_caption,
            json.loads(canonical_before_race.decode("utf-8")),
        )

        state["overlays"] = []
        self.write_json("working/editor_state.json", state)
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": gate_revision(self.project, "timeline", state),
        }
        self.write_json("project.json", manifest)
        final = self.project / "renders/caption-required.mp4"
        final.write_bytes(b"last-good-output")
        envelopes = self.project / "working/delivery_envelopes"
        finalized_before = set(envelopes.glob("*.json")) if envelopes.is_dir() else set()

        result = self.run_renderer(
            "--quality", "final", "--output", str(final), expected=2
        )

        self.assertIn("caption_binding_missing", result.stderr + result.stdout)
        self.assertEqual(final.read_bytes(), b"last-good-output")
        finalized_after = set(envelopes.glob("*.json")) if envelopes.is_dir() else set()
        self.assertEqual(finalized_after, finalized_before)
        staging = self.project / "working/delivery_envelopes/.staging"
        residue = (
            [entry for entry in staging.iterdir() if entry.name != ".locks"]
            if staging.is_dir()
            else []
        )
        self.assertEqual(residue, [])

    def test_direct_final_external_output_is_bound_by_its_envelope(self) -> None:
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["approvals"]["timeline"] = {
            "approved": True,
            "state_revision": gate_revision(self.project, "timeline", state),
        }
        self.write_json("project.json", manifest)
        final = Path(self._tmp.name) / "external-delivery/final.mp4"

        self.run_renderer("--quality", "final", "--output", str(final))

        render_id = direct_final_render_id(state, final)
        envelope = json.loads(
            (
                self.project
                / "working/delivery_envelopes"
                / f"{render_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(envelope["artifacts"]["output"]["path"], str(final.resolve()))
        self.assertEqual(
            envelope["artifacts"]["output"]["sha256"],
            editor_server.file_sha256(final),
        )
        self.assertEqual(
            json.loads(
                (self.project / f"qa/{render_id}.json").read_text(encoding="utf-8")
            )["video"],
            str(final.resolve()),
        )

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
        self.rebuild_source(audio_source="anullsrc=r=48000:cl=stereo")
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

    def test_multi_segment_reorder_concat_and_post_cut_captions(self) -> None:
        self.rebuild_source(audio_source="anullsrc=r=48000:cl=stereo")
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["segments"] = [
            {
                "id": "segment-aaaaaaaaaaaa",
                "source_start": 0.30,
                "source_end": 0.45,
                "origin": "narrative",
            },
            {
                "id": "segment-bbbbbbbbbbbb",
                "source_start": 0.00,
                "source_end": 0.15,
                "origin": "narrative",
            },
        ]
        state["overlays"] = [
            {
                "id": "caption-1",
                "type": "caption",
                "text": "哈囉",
                "start": 0.02,
                "end": 0.12,
                "visible": True,
                "style": {},
                "layout": {},
            }
        ]
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        command = build_render_command(
            self.project,
            state,
            manifest,
            self.project / "renders/multi.mp4",
            "preview",
        )
        joined = " ".join(command)
        self.assertIn("concat=n=2:v=1", joined)
        self.assertIn("concat=n=2:v=0:a=1", joined)
        self.assertNotIn("-ss", command)
        self.assertNotIn("-t", command)
        self.assertIn("[aout]", joined)
        self.assertIn("anull", joined, "silent source must skip loudnorm in the graph")
        # caption source 0.02–0.12 lives in the SECOND post-cut segment
        # (offset 0.15) → post-cut window 0.170–0.270
        self.assertIn("between(t,0.170,0.270)", joined)

        self.write_json("working/editor_state.json", state)
        output = self.project / "renders/multi.mp4"
        self.run_renderer("--quality", "preview", "--output", str(output))
        duration = float(
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
        self.assertAlmostEqual(
            duration, 0.30, delta=1.0 / 30.0 + 0.005,
            msg="post-cut duration must equal the segment sum within one frame",
        )

    def test_overlay_crossing_removed_region_splits_into_two_windows(self) -> None:
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["segments"] = [
            {
                "id": "segment-aaaaaaaaaaaa",
                "source_start": 0.00,
                "source_end": 0.10,
                "origin": "narrative",
            },
            {
                "id": "segment-bbbbbbbbbbbb",
                "source_start": 0.30,
                "source_end": 0.40,
                "origin": "narrative",
            },
        ]
        state["overlays"] = [
            {
                "id": "caption-1",
                "type": "caption",
                "text": "跨切點",
                "start": 0.05,
                "end": 0.35,
                "visible": True,
                "style": {},
                "layout": {},
            }
        ]
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        command = build_render_command(
            self.project,
            state,
            manifest,
            self.project / "renders/split.mp4",
            "preview",
        )
        joined = " ".join(command)
        self.assertIn("between(t,0.050,0.100)", joined)
        self.assertIn("between(t,0.100,0.150)", joined)

    def test_caption_route_uses_compositor_pngs(self) -> None:
        import caption_compositor

        if not caption_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"] = [
            {
                "id": "caption-route",
                "type": "caption",
                "text": "看到 It 想到 to V",
                "start": 0.05,
                "end": 0.40,
                "visible": True,
                "style": {"font_size": 40, "color": "#F7F2E8", "x": 50, "y": 78},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
                "effect_spans": [
                    {
                        "id": "fx1",
                        "text": "It",
                        "start_char": 3,
                        "end_char": 5,
                        "style": {"effect": "pop", "color": "#FF5533", "font_scale": 1.3},
                    }
                ],
            },
            {
                "id": "title-1",
                "type": "title",
                "text": "標題保持 drawtext",
                "start": 0.0,
                "end": 0.3,
                "visible": True,
                "style": {"font_size": 44},
                "layout": {"x": 10, "y": 10, "width": 80, "height": 20},
            },
        ]
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        command = build_render_command(
            self.project, state, manifest,
            self.project / "renders/caption-route.mp4", "preview",
        )
        joined = " ".join(command)
        self.assertIn("working/captions/caption-route-", joined,
                      "caption must ride its compositor PNG")
        self.assertEqual(
            joined.count("drawtext"), 1,
            "only the title overlay may use drawtext on the compositor route",
        )
        plan = json.loads(
            (self.project / "working/caption_render_plan.json").read_text("utf-8")
        )
        self.assertEqual(plan["items"][0]["caption_item_id"], "caption-route")

        self.write_json("working/editor_state.json", state)
        output = self.project / "renders/caption-route.mp4"
        self.run_renderer("--quality", "preview", "--output", str(output))
        self.assertTrue(output.is_file())

    def test_unsanctioned_font_fallback_blocks_final(self) -> None:
        import caption_compositor

        if not caption_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"] = [
            {
                "id": "caption-emoji",
                "type": "caption",
                "text": "測試👍字幕",
                "start": 0.05,
                "end": 0.40,
                "visible": True,
                "style": {"font_size": 40},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
            }
        ]
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        with unittest.mock.patch.object(
            caption_compositor, "SANCTIONED_FALLBACK_PS_NAMES", set()
        ):
            with self.assertRaises(ValueError) as ctx:
                build_render_command(
                    self.project, state, manifest,
                    self.project / "renders/blocked.mp4", "final",
                )
        self.assertIn("unsanctioned", str(ctx.exception))

    def test_strip_caption_overlays_keeps_design_cards(self) -> None:
        from render_editor_timeline import strip_caption_overlays

        state = {
            "overlays": [
                {"id": "c1", "type": "caption", "text": "字幕"},
                {"id": "d1", "type": "card", "design_role": "hook", "text": "卡"},
                {"id": "e1", "type": "emphasis", "text": "強調"},
            ]
        }
        stripped = strip_caption_overlays(state)
        self.assertEqual([o["id"] for o in stripped["overlays"]], ["d1"])

    def test_designed_route_still_overlays_compositor_captions(self) -> None:
        import caption_compositor

        if not caption_compositor.compositor_available():
            self.skipTest("needs macOS CoreText")
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"] = [
            {
                "id": "caption-designed",
                "type": "caption",
                "text": "designed 也要有字幕",
                "start": 0.05,
                "end": 0.40,
                "visible": True,
                "style": {"font_size": 40, "animation": "pop"},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
            }
        ]
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        graphics_dir = self.project / "working/graphic_packages"
        graphics_dir.mkdir(parents=True, exist_ok=True)
        fake_package = graphics_dir / "package.mp4"
        fake_package.write_bytes(b"fake")
        command = build_render_command(
            self.project, state, manifest,
            self.project / "renders/designed-caption.mp4", "preview",
            None, fake_package,
        )
        joined = " ".join(command)
        self.assertIn(
            "working/captions/caption-designed-", joined,
            "designed route must still overlay the compositor caption PNG",
        )
        self.assertIn("scale=eval=frame", joined, "pop must carry a timing scale")

    def test_variant_baseline_two_orientations_and_per_variant_gates(self) -> None:
        state = json.loads(
            (self.project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        state["overlays"] = [
            {
                "id": "caption-var",
                "type": "caption",
                "text": "橫直式測試字幕",
                "start": 0.05,
                "end": 0.40,
                "visible": True,
                "style": {"font_size": 40},
                "layout": {"x": 10, "y": 70, "width": 80, "height": 20},
            }
        ]
        state["variants"] = [
            {"variant_id": "portrait-main", "preset_id": "instagram-reels", "overrides": []},
            {
                "variant_id": "landscape-yt",
                "preset_id": "youtube-landscape",
                "overrides": [
                    {"path": "caption.y", "kind": "caption_layout", "value": 84}
                ],
            },
        ]
        self.write_json("working/editor_state.json", state)

        # preview both orientations through the CLI
        # preview clamps the long side to 960: portrait 1080x1920→540x960,
        # landscape 1920x1080→960x540
        for variant_id, expected_width in (("portrait-main", 540), ("landscape-yt", 960)):
            output = self.project / f"renders/{variant_id}.mp4"
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPTS_DIR / "render_editor_timeline.py"),
                    "--project-dir", str(self.project),
                    "--output", str(output),
                    "--quality", "preview",
                    "--variant", variant_id,
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = subprocess.run(
                [
                    shutil.which("ffprobe") or "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width",
                    "-of", "csv=p=0", str(output),
                ],
                capture_output=True, text=True, check=True,
            )
            width = int(probe.stdout.strip())
            self.assertEqual(
                width, expected_width, f"{variant_id}: unexpected width {width}"
            )

        # final without approval fails per-variant
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "render_editor_timeline.py"),
                "--project-dir", str(self.project),
                "--output", str(self.project / "renders/landscape-final.mp4"),
                "--quality", "final",
                "--variant", "landscape-yt",
            ],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)

        # approve landscape timeline via the variant gate and render final
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        revision = editor_server.variant_gate_revision(
            self.project, "timeline", state, "landscape-yt"
        )
        manifest.setdefault("approvals", {})["timeline_by_variant"] = {
            "landscape-yt": {"approved": True, "state_revision": revision}
        }
        self.write_json("project.json", manifest)
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "render_editor_timeline.py"),
                "--project-dir", str(self.project),
                "--output", str(self.project / "renders/landscape-final.mp4"),
                "--quality", "final",
                "--variant", "landscape-yt",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.project / "working/delivery_qa/landscape-yt.json").read_text("utf-8")
        )
        self.assertEqual(receipt["variant_id"], "landscape-yt")
        self.assertEqual(receipt["output"], "renders/landscape-final.mp4")
        report = json.loads(
            (self.project / "qa/variant-landscape-yt.json").read_text("utf-8")
        )
        self.assertEqual(report["visual_delivery"]["source"], "renderer_evidence")
        self.assertEqual(report["visual_delivery"]["status"], "pass")
        self.assertEqual(receipt["visual_delivery"], report["visual_delivery"])
        self.assertTrue(
            (self.project / "working/render_visual_evidence/variant-landscape-yt.json").is_file()
        )

        # per-variant receipts must not clobber each other
        self.assertFalse(
            (self.project / "working/delivery_qa/portrait-main.json").exists()
        )

        # full lifecycle: final approve AFTER render (receipt-bound), then
        # the download gate opens for exactly this variant
        state_on_disk = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        final_revision = editor_server.variant_gate_revision(
            self.project, "final", state_on_disk, "landscape-yt"
        )
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        manifest["approvals"]["final_by_variant"] = {
            "landscape-yt": {"approved": True, "state_revision": final_revision}
        }
        self.write_json("project.json", manifest)
        self.assertEqual(
            editor_server.render_download_errors(
                self.project, "renders/landscape-final.mp4"
            ),
            [],
            "approved variant final must be downloadable",
        )
        # the OTHER (unapproved) variant path stays gated
        errors = editor_server.render_download_errors(
            self.project, "renders/portrait-main.mp4"
        )
        # portrait preview is a preview receipt-less CLI render output — not
        # covered by any receipt → fail closed
        self.assertTrue(errors)

        # probe height too: landscape final must actually be 1920x1080
        probe = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                str(self.project / "renders/landscape-final.mp4"),
            ],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.stdout.strip(), "1920x1080")

        # master edit → variant approval stale
        state["overlays"][0]["style"]["font_size"] = 46
        self.write_json("working/editor_state.json", state)
        fresh = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        self.assertFalse(
            editor_server.variant_approval_is_current(
                self.project, manifest, "timeline", fresh, "landscape-yt"
            ),
            "editing the master must invalidate variant approvals",
        )
        self.assertTrue(
            editor_server.render_download_errors(
                self.project, "renders/landscape-final.mp4"
            ),
            "master edit must re-lock the variant download",
        )

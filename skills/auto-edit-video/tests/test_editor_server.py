from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from editor_server import EditorServer, editor_state_revision  # noqa: E402
from render_editor_timeline import text_filter  # noqa: E402


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
            "highlight-list",
            "editing-brief",
            "director-grid",
            "layer-list",
            "timeline-tracks",
            "render-button",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("Hybrid editorial workstation", css)
        self.assertIn('const DIRECTOR_ORDER = ["teacher-punch", "high-energy"', script)
        self.assertIn('name: "影片", kind: "source"', script)
        self.assertIn('name: "字幕", types: ["caption"]', script)

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
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
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
        request_headers.update(headers or {})
        status, _response_headers, body = self.request(
            method,
            path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            request_headers,
        )
        return status, json.loads(body.decode("utf-8"))

    def test_project_bootstrap_exposes_presets_and_caption_layer(self) -> None:
        status, headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(len(payload["platform_presets"]), 6)
        self.assertEqual(len(payload["director_presets"]), 5)
        self.assertTrue(
            any(voice["voice_id"] == "rumi" for voice in payload["voice_catalog"]["voices"])
        )
        self.assertEqual(payload["state"]["overlays"][0]["type"], "caption")
        self.assertEqual(payload["state"]["editing_brief"], "")
        self.assertEqual(
            {preset["label"] for preset in payload["director_presets"].values()},
            {"專業教學", "爆款短影音", "八卦時事", "POV 藏鏡人", "編輯精簡"},
        )
        self.assertEqual(
            {item["type"] for item in payload["state"]["overlays"]},
            {"caption", "emphasis", "title"},
        )
        self.assertTrue((self.project / "working/editor_state.json").is_file())

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

        upload_body = b"\x89PNG\r\n\x1a\nlocal-test"
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

        status, payload = self.json_request("POST", "/api/render", {"quality": "final"})
        self.assertEqual(status, 409)
        self.assertIn("timeline revision", str(payload["error"]))

    def test_timeline_approval_is_bound_to_render_state_revision(self) -> None:
        status, _headers, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        status, approval = self.json_request(
            "POST",
            "/api/approve",
            {"gate": "timeline", "confirmed_by": "unit-test"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(approval["approval"]["state_revision"])

        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200)
        self.assertEqual(saved["invalidated_gates"], [])

        state["overlays"][0]["text"] = "已修改的字幕"
        status, saved = self.json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200)
        self.assertIn("timeline", saved["invalidated_gates"])
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["approvals"]["timeline"]["approved"])


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
                "schema_version": 1,
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

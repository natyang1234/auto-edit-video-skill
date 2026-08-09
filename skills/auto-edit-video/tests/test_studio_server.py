from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from auto_edit import validate_manifest  # noqa: E402
import studio_server  # noqa: E402
from studio_server import StudioServer  # noqa: E402


class StudioHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("ffmpeg is unavailable")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-studio-tests-")
        self.root = Path(self._tmp.name)
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir()
        self.source = self.root / "Crystal source.mp4"
        result = subprocess.run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=navy:s=320x240:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                str(self.source),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        self.source_bytes = self.source.read_bytes()
        self.source_hash = hashlib.sha256(self.source_bytes).hexdigest()
        self.source_stat = self.source.stat()
        self.server = StudioServer(
            ("127.0.0.1", 0),
            self.projects_root,
            max_import_bytes=2 * 1024 * 1024,
            max_duration_s=60,
            max_source_pixels=1920 * 1080,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        status, payload = self.json_response("GET", "/api/studio")
        self.assertEqual(status, 200, payload)
        self.csrf_token = str(payload["csrf_token"])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = response.headers
        connection.close()
        return status, response_headers, payload

    def json_response(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        status, _response_headers, raw = self.request(method, path, body, request_headers)
        return status, json.loads(raw.decode("utf-8"))

    def create_import(self, *, name: str, size: int) -> dict[str, object]:
        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {
                "project_name": "Crystal 精華",
                "file": {
                    "name": name,
                    "size_bytes": size,
                    "last_modified_ms": int(self.source_stat.st_mtime * 1000),
                    "type": "video/mp4",
                },
                "settings": {
                    "source_language": "zh-TW",
                    "subtitle_mode": "source",
                    "platform": "youtube-shorts",
                    "duration_profile": "short",
                    "edit_preset": "balanced",
                    "director_profile": "documentary",
                    "editing_brief": "保留有證據的轉折，不要補寫內容。",
                },
            },
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 201, payload)
        return payload["import"]

    def upload(self, import_payload: dict[str, object], body: bytes, mime: str = "video/mp4"):
        return self.request(
            "PUT",
            str(import_payload["upload_url"]),
            body,
            {
                "Content-Type": mime,
                "X-Auto-Edit-CSRF": self.csrf_token,
            },
        )


class StudioServerTests(StudioHarness):
    def test_studio_bootstrap_exposes_registry_director_presets(self) -> None:
        status, payload = self.json_response("GET", "/api/studio")
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["director_presets"]), 6)
        kinetic = payload["director_presets"]["kinetic-explainer"]
        self.assertEqual(kinetic["profile_id"], "kinetic-explainer")
        self.assertRegex(kinetic["resolved_hash"], r"^[0-9a-f]{64}$")
        self.assertFalse(kinetic["available"])
        self.assertEqual(
            kinetic["missing_capabilities"], sorted(kinetic["missing_capabilities"])
        )

    def test_import_director_options_are_bootstrapped_from_registry(self) -> None:
        html = (SKILL_DIR / "editor/import.html").read_text(encoding="utf-8")
        script = (SKILL_DIR / "editor/import.js").read_text(encoding="utf-8")
        self.assertNotIn('<option value="teacher-punch">', html)
        self.assertIn("function populateDirectorOptions", script)
        self.assertIn("payload.director_presets", script)
        self.assertIn("尚未就緒", script)
        self.assertIn("option.disabled", script)

    def test_omitted_director_keeps_legacy_default_selection_reason(self) -> None:
        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {
                "project_name": "Default",
                "file": {
                    "name": self.source.name,
                    "size_bytes": len(self.source_bytes),
                    "type": "video/mp4",
                },
                "settings": {},
            },
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 201, payload)
        stored = self.server.imports[str(payload["import"]["id"])]
        request = stored["settings"]["director_selection_request"]
        self.assertEqual(stored["settings"]["director_profile"], "teacher-punch")
        self.assertEqual(request["selection_reason"], "default_unchanged")
        self.assertEqual(
            request["resolved_profile_hash"],
            stored["settings"]["resolved_director_profile"]["resolved_hash"],
        )

    def test_kinetic_import_fails_capability_preflight_before_session_mutation(self) -> None:
        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {
                "project_name": "Kinetic",
                "file": {
                    "name": self.source.name,
                    "size_bytes": len(self.source_bytes),
                    "type": "video/mp4",
                },
                "settings": {"director_profile": "kinetic-explainer"},
            },
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error_code"], "capability_missing")
        self.assertEqual(
            payload["missing_capabilities"], sorted(payload["missing_capabilities"])
        )
        self.assertEqual(self.server.imports, {})
        self.assertIsNone(self.server.active_upload_id)
        self.assertEqual(list(self.projects_root.iterdir()), [])

    def test_import_creates_owned_project_and_launches_scoped_editor(self) -> None:
        html_status, _headers, html = self.request("GET", "/")
        self.assertEqual(html_status, 200)
        self.assertIn("導入影片", html.decode("utf-8"))
        self.assertIn('id="import-director"', html.decode("utf-8"))
        self.assertIn('id="import-brief"', html.decode("utf-8"))
        self.assertIn('id="import-contextual-semantic"', html.decode("utf-8"))

        import_payload = self.create_import(
            name=self.source.name,
            size=len(self.source_bytes),
        )
        status, _headers, body = self.upload(import_payload, self.source_bytes)
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 201, response)
        self.assertTrue(response["editor_url"].startswith("http://127.0.0.1:"))
        self.assertEqual(response["import"]["selection_reason"], "explicit_profile")
        self.assertRegex(response["import"]["resolved_profile_hash"], r"^[0-9a-f]{64}$")

        project_id = response["project"]["id"]
        self.assertRegex(project_id, r"^[a-z0-9][a-z0-9-]{8,80}$")
        project = self.projects_root / project_id
        staged = project / "source/original.mp4"
        self.assertTrue(staged.is_file())
        self.assertFalse(staged.is_symlink())
        self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), self.source_hash)

        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(manifest["source"]["original_path"])
        self.assertEqual(manifest["source"]["original_name"], self.source.name)
        self.assertEqual(manifest["source"]["ingest_method"], "browser_upload")
        self.assertTrue(manifest["source"]["owned_copy"])
        self.assertEqual(manifest["source"]["sha256"], self.source_hash)
        self.assertEqual(manifest["stages"]["ingest"], "complete")
        self.assertEqual(manifest["stages"]["transcribe"], "pending")
        self.assertTrue(
            manifest["subtitles"]["contextual_semantic_calibration"]["enabled"]
        )
        self.assertTrue(all(not item["approved"] for item in manifest["approvals"].values()))
        resolved_profile = json.loads(
            (project / "working/resolved_director_profile.json").read_text(
                encoding="utf-8"
            )
        )
        selection_request = json.loads(
            (project / "working/director_selection_request.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(resolved_profile["profile_id"], "documentary")
        self.assertEqual(selection_request["profile_id"], "documentary")
        self.assertEqual(selection_request["selection_reason"], "explicit_profile")
        self.assertEqual(
            response["import"]["resolved_profile_hash"], resolved_profile["resolved_hash"]
        )
        self.assertEqual(
            selection_request["resolved_profile_hash"], resolved_profile["resolved_hash"]
        )
        stored_session = self.server.imports[str(import_payload["id"])]
        self.assertEqual(stored_session["settings"]["director_profile"], "documentary")
        self.assertEqual(
            stored_session["settings"]["resolved_director_profile"]["resolved_hash"],
            resolved_profile["resolved_hash"],
        )
        self.assertEqual(
            stored_session["settings"]["director_selection_request"]["resolved_profile_hash"],
            resolved_profile["resolved_hash"],
        )
        self.assertEqual(
            stored_session["settings"]["editing_brief"],
            "保留有證據的轉折，不要補寫內容。",
        )
        errors, _warnings = validate_manifest(manifest, manifest_path)
        self.assertEqual(errors, [])

        parsed = urllib.parse.urlsplit(response["editor_url"])
        editor = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        editor.request("GET", "/api/project")
        editor_response = editor.getresponse()
        editor_payload = json.loads(editor_response.read().decode("utf-8"))
        editor.close()
        self.assertEqual(editor_response.status, 200)
        self.assertEqual(editor_payload["manifest"]["project_id"], project_id)

        after = self.source.stat()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(after.st_size, self.source_stat.st_size)
        self.assertEqual(after.st_mtime_ns, self.source_stat.st_mtime_ns)

        second_status, _headers, second_body = self.upload(import_payload, self.source_bytes)
        self.assertEqual(second_status, 409, second_body)

    def test_import_accepts_mixed_zh_en_with_transcription_glossary(self) -> None:
        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {
                "project_name": "Crystal 中英教學",
                "file": {
                    "name": self.source.name,
                    "size_bytes": len(self.source_bytes),
                    "last_modified_ms": int(self.source_stat.st_mtime * 1000),
                    "type": "video/mp4",
                },
                "settings": {
                    "source_language": "zh-en",
                    "transcription_glossary": "It; to V; cigar; cigarette",
                    "transcription_calibration": "複數=富數;雪茄=學家|雪家;cigar=ciger",
                    "subtitle_mode": "source",
                    "platform": "youtube-shorts",
                    "duration_profile": "short",
                    "edit_preset": "balanced",
                    "director_profile": "teacher-punch",
                    "editing_brief": "保留英文術語原文。",
                },
            },
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 201, payload)
        import_payload = payload["import"]
        upload_status, _headers, body = self.upload(import_payload, self.source_bytes)
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(upload_status, 201, response)

        project = self.projects_root / response["project"]["id"]
        manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["subtitles"]["source_language"], "zh-en")
        self.assertEqual(
            manifest["subtitles"]["glossary"],
            ["It", "to V", "cigar", "cigarette"],
        )
        self.assertEqual(
            manifest["subtitles"]["calibrations"],
            [
                {"canonical": "複數", "aliases": ["富數"]},
                {"canonical": "雪茄", "aliases": ["學家", "雪家"]},
                {"canonical": "cigar", "aliases": ["ciger"]},
            ],
        )

    def test_import_rejects_bad_metadata_cross_site_and_oversize(self) -> None:
        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {"file": {"name": "../escape.mp4", "size_bytes": 12, "type": "video/mp4"}},
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 422, payload)

        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {"file": {"name": "notes.txt", "size_bytes": 12, "type": "text/plain"}},
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 415, payload)

        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {"file": {"name": "huge.mp4", "size_bytes": 2 * 1024 * 1024 + 1, "type": "video/mp4"}},
            {"X-Auto-Edit-CSRF": self.csrf_token},
        )
        self.assertEqual(status, 413, payload)

        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {"file": {"name": "clip.mp4", "size_bytes": 12, "type": "video/mp4"}},
            {
                "X-Auto-Edit-CSRF": self.csrf_token,
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(status, 403, payload)

        status, payload = self.json_response(
            "POST",
            "/api/imports",
            {"file": {"name": "clip.mp4", "size_bytes": 12, "type": "video/mp4"}},
        )
        self.assertEqual(status, 403, payload)

    def test_import_rejects_mime_mismatch_and_fake_video_atomically(self) -> None:
        import_payload = self.create_import(name="fake.mp4", size=12)
        status, _headers, _body = self.upload(import_payload, b"not-a-video!", "text/plain")
        self.assertEqual(status, 415)

        import_payload = self.create_import(name="fake2.mp4", size=12)
        status, _headers, body = self.upload(import_payload, b"not-a-video!", "video/mp4")
        self.assertEqual(status, 415, body)

        final_projects = [
            item for item in self.projects_root.iterdir() if not item.name.startswith(".")
        ]
        self.assertEqual(final_projects, [])
        self.assertFalse(any(self.projects_root.glob(".creating-*")))

    def test_local_pipeline_runs_all_draft_steps_without_approval(self) -> None:
        project = self.projects_root / "pipeline-project"
        (project / "working").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(_project: Path, arguments: list[str], *, timeout: int):
            self.assertGreater(timeout, 0)
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "{}", "")

        self.server.auto_process = True
        with patch.object(self.server, "run_pipeline_command", side_effect=fake_run):
            self.server.start_local_pipeline(project, "high-energy", "只保留明確結論")
            self.server.pipeline_threads[-1].join(timeout=3)

        self.assertEqual(
            [arguments[0] for arguments in calls],
            ["transcribe-local", "analyze-edits", "plan-overlays", "plan-highlights"],
        )
        self.assertIn("high-energy", calls[-1])
        self.assertIn("只保留明確結論", calls[-1])
        status = json.loads(
            (project / "working/pipeline_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["state"], "needs_review")
        self.assertEqual(status["phase"], "human_review")
        self.assertIsNone(self.server.active_pipeline_project)

    def test_local_pipeline_runs_contextual_semantic_pass_before_planning(self) -> None:
        project = self.projects_root / "semantic-pipeline-project"
        (project / "working").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(_project: Path, arguments: list[str], *, timeout: int):
            self.assertGreater(timeout, 0)
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "{}", "")

        self.server.auto_process = True
        with patch.object(self.server, "run_pipeline_command", side_effect=fake_run):
            self.server.start_local_pipeline(
                project,
                "teacher-punch",
                "逐句校準",
                True,
                "qwen2.5:7b",
            )
            self.server.pipeline_threads[-1].join(timeout=3)

        self.assertEqual(
            [arguments[0] for arguments in calls],
            [
                "transcribe-local",
                "semantic-calibrate",
                "analyze-edits",
                "plan-overlays",
                "plan-highlights",
            ],
        )
        self.assertIn("qwen2.5:7b", calls[1])

    def test_partial_contextual_coverage_stops_downstream_planning(self) -> None:
        project = self.projects_root / "partial-semantic-pipeline"
        (project / "working").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(_project: Path, arguments: list[str], *, timeout: int):
            self.assertGreater(timeout, 0)
            calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments,
                3 if arguments[0] == "semantic-calibrate" else 0,
                '{"coverage_status":"partial"}',
                "",
            )

        self.server.auto_process = True
        with patch.object(self.server, "run_pipeline_command", side_effect=fake_run):
            self.server.start_local_pipeline(
                project,
                "teacher-punch",
                "逐句校準",
                True,
                "qwen2.5:7b",
            )
            self.server.pipeline_threads[-1].join(timeout=3)

        self.assertEqual(
            [arguments[0] for arguments in calls],
            ["transcribe-local", "semantic-calibrate"],
        )
        status = json.loads(
            (project / "working/pipeline_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["state"], "needs_attention")
        self.assertEqual(status["error_code"], "semantic_calibration_partial")


if __name__ == "__main__":
    unittest.main()


class FolderImportTests(StudioHarness):
    """Phase 1a M4b: folder-first import sessions."""

    def folder_headers(self) -> dict[str, str]:
        return {"X-Auto-Edit-CSRF": self.csrf_token}

    def create_folder_session(self, files, **extra):
        payload = {
            "root_display_name": "素材夾",
            "project_name": "folder-e2e",
            "settings": {"source_language": "auto"},
            "files": files,
        }
        payload.update(extra)
        return self.json_response(
            "POST", "/api/folder-imports", payload, self.folder_headers()
        )

    def test_folder_session_rejects_traversal_and_undeclared_files(self) -> None:
        status, payload = self.create_folder_session(
            [{"path": "../evil.mp4", "size_bytes": 10}]
        )
        self.assertEqual(status, 422, payload)

        status, payload = self.create_folder_session(
            [{"path": "main.mp4", "size_bytes": len(self.source_bytes)}]
        )
        self.assertEqual(status, 201, payload)
        session = payload["session"]
        status, _headers, raw = self.request(
            "PUT",
            session["upload_url_template"] + "sneaky.bin",
            b"x" * 10,
            {"Content-Type": "application/octet-stream", **self.folder_headers()},
        )
        self.assertEqual(status, 422, raw)

    def test_folder_import_end_to_end(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        files = [
            {"path": "main.mp4", "size_bytes": len(self.source_bytes)},
            {"path": "assets/封面.png", "size_bytes": len(png)},
        ]
        status, payload = self.create_folder_session(files)
        self.assertEqual(status, 201, payload)
        session = payload["session"]
        for path, body in (("main.mp4", self.source_bytes), ("assets/封面.png", png)):
            status, _headers, raw = self.request(
                "PUT",
                session["upload_url_template"] + urllib.parse.quote(path),
                body,
                {"Content-Type": "application/octet-stream", **self.folder_headers()},
            )
            self.assertEqual(status, 200, raw)
        # duplicate upload must be refused
        status, _headers, raw = self.request(
            "PUT",
            session["upload_url_template"] + "main.mp4",
            self.source_bytes,
            {"Content-Type": "application/octet-stream", **self.folder_headers()},
        )
        self.assertEqual(status, 409, raw)

        self.server.start_local_pipeline = lambda *args, **kwargs: None
        status, finalized = self.json_response(
            "POST", str(session["finalize_url"]), None, self.folder_headers()
        )
        self.assertEqual(status, 201, finalized)
        project_dir = self.projects_root / finalized["project"]["id"]
        self.assertTrue(project_dir.is_dir())
        inventory = json.loads(
            (project_dir / "working/folder_inventory.json").read_text("utf-8")
        )
        self.assertEqual(inventory["main_video_path"], "main.mp4")
        session_artifact = json.loads(
            (project_dir / "working/folder_import_session.json").read_text("utf-8")
        )
        self.assertEqual(session_artifact["state"], "completed")
        self.assertTrue(session_artifact["csrf_bound"])
        self.assertFalse(
            (self.projects_root / f".folder-creating-{session['id']}").exists(),
            "staging directory must be removed after finalize",
        )
        self.assertIn("editor_url", finalized)

    def test_folder_session_requires_a_video(self) -> None:
        status, payload = self.create_folder_session(
            [{"path": "notes.txt", "size_bytes": 5}]
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload.get("error_code"), "no_video")

    def test_finalize_failure_releases_import_lock(self) -> None:
        files = [{"path": "main.mp4", "size_bytes": len(self.source_bytes)}]
        status, payload = self.create_folder_session(files)
        self.assertEqual(status, 201, payload)
        session = payload["session"]
        status, _headers, raw = self.request(
            "PUT",
            session["upload_url_template"] + "main.mp4",
            self.source_bytes,
            {"Content-Type": "application/octet-stream", **self.folder_headers()},
        )
        self.assertEqual(status, 200, raw)

        real_run = studio_server.subprocess.run

        def failing_run(command, *args, **kwargs):
            if any("ingest-folder" in str(part) for part in command):
                return subprocess.CompletedProcess(command, 1, "", "boom")
            return real_run(command, *args, **kwargs)

        with unittest.mock.patch.object(studio_server.subprocess, "run", failing_run):
            status, payload = self.json_response(
                "POST", str(session["finalize_url"]), None, self.folder_headers()
            )
        self.assertEqual(status, 500, payload)
        self.assertFalse(
            (self.projects_root / f".folder-creating-{session['id']}").exists(),
            "failed finalize must clean its staging dir",
        )
        # The lock must be free: a fresh session is accepted immediately.
        status, payload = self.create_folder_session(files)
        self.assertEqual(status, 201, payload)

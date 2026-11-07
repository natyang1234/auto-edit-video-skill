from __future__ import annotations

import hashlib
import http.client
import json
import os
import base64
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import editor_server  # noqa: E402
import fx_overlay  # noqa: E402
import fx_preview_contract  # noqa: E402
import fx_preview_jobs  # noqa: E402
from editor_server import EditorServer  # noqa: E402


FAST_RENDERER = r'''#!/usr/bin/env python3
import argparse, hashlib, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--request"); p.add_argument("--result"); a=p.parse_args()
request_fd=os.environ.get("FX_PREVIEW_REQUEST_FD")
if request_fd:
    fd=int(request_fd); os.lseek(fd,0,os.SEEK_SET); raw=b""
    while True:
        chunk=os.read(fd,65536)
        if not chunk: break
        raw += chunk
else:
    raw=Path(a.request).read_bytes()
req=json.loads(raw)
print(json.dumps({"phase":"rendering","message":"ignored"}), file=__import__('sys').stderr, flush=True)
out=Path(req["output"]); out.write_bytes(b"synthetic-mp4-" + req["overlay"]["code"].encode())
sample=req["sample_window"]
duration=sample["duration_s"]
keys=("project_dir","source","cache_dir","output","overlay","sample_window","preview_options","state_snapshot","manifest_snapshot","timeline_snapshot","dependency_snapshot","pack_id","canvas")
render_raw=json.dumps({key:req.get(key) for key in keys},ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
payload=json.dumps({
    "ok":True,
    "out":str(out),
    "request_sha256":hashlib.sha256(raw).hexdigest(),
    "render_request_sha256":hashlib.sha256(render_raw).hexdigest(),
    "render_duration_s":duration,
    "time_map":[{
        "source_start_s":sample["start_s"],
        "source_end_s":sample["start_s"]+duration,
        "output_start_s":0.0,
        "output_end_s":duration,
    }],
    "text_layout":{
        "version":2,
        "measurement":"composition-dom",
        "status":"pass",
        "analysis_scope":"single-fx-and-source-frame",
        "face_measurement":"unavailable",
        "samples":[{"at":0.0,"boxes":[{"id":"title","text":"must-not-leak","x":1,"y":2,"width":3,"height":4,"visible":True,"fitted":True}]}],
        "boxes":[],"findings":[],"subject_samples":[],"detail":"private-detail"
    },
})
result_dir_fd=os.environ.get("FX_PREVIEW_RESULT_DIR_FD")
if result_dir_fd:
    dfd=int(result_dir_fd); name=Path(a.result).name; tmp=name+".tmp"
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=dfd)
    with os.fdopen(fd,"w") as stream: stream.write(payload)
    os.replace(tmp,name,src_dir_fd=dfd,dst_dir_fd=dfd)
else:
    tmp=Path(a.result + ".tmp"); tmp.write_text(payload); os.replace(tmp,a.result)
'''

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

SLOW_RENDERER = r'''#!/usr/bin/env python3
import argparse, hashlib, json, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--request"); p.add_argument("--result"); a=p.parse_args()
request_fd=os.environ.get("FX_PREVIEW_REQUEST_FD")
if request_fd:
    fd=int(request_fd); os.lseek(fd,0,os.SEEK_SET); raw=b""
    while True:
        chunk=os.read(fd,65536)
        if not chunk: break
        raw += chunk
else:
    raw=Path(a.request).read_bytes()
req=json.loads(raw)
print(json.dumps({"phase":"rendering"}), file=__import__('sys').stderr, flush=True)
time.sleep(0.5)
out=Path(req["output"]); out.write_bytes(b"late-mp4")
keys=("project_dir","source","cache_dir","output","overlay","sample_window","preview_options","state_snapshot","manifest_snapshot","timeline_snapshot","dependency_snapshot","pack_id","canvas")
render_raw=json.dumps({key:req.get(key) for key in keys},ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
payload=json.dumps({"ok":True,"out":str(out),"request_sha256":hashlib.sha256(raw).hexdigest(),"render_request_sha256":hashlib.sha256(render_raw).hexdigest()})
result_dir_fd=os.environ.get("FX_PREVIEW_RESULT_DIR_FD")
if result_dir_fd:
    dfd=int(result_dir_fd); name=Path(a.result).name; tmp=name+".tmp"
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=dfd)
    with os.fdopen(fd,"w") as stream: stream.write(payload)
    os.replace(tmp,name,src_dir_fd=dfd,dst_dir_fd=dfd)
else:
    tmp=Path(a.result + ".tmp"); tmp.write_text(payload); os.replace(tmp,a.result)
'''

GRANDCHILD_RENDERER = r'''#!/usr/bin/env python3
import argparse, json, subprocess, sys, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--request"); p.add_argument("--result"); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
child=subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
Path(req["project_dir"], "working", "grandchild.pid").write_text(str(child.pid))
time.sleep(60)
'''


class CoordinatedRenderLock:
    """Force the historical project/render AB-BA interleaving deterministically."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self._calls_guard = threading.Lock()
        self._calls = 0
        self.first_attempt = threading.Event()
        self.second_acquired = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._calls_guard:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_attempt.set()
            # Old ordering lets preview acquire render_lock as call two while
            # full render owns project_lock.  New ordering blocks preview on
            # project_lock, so the first caller proceeds after this short gate.
            self.second_acquired.wait(timeout=0.4)
        if timeout == -1:
            acquired = self._inner.acquire(blocking)
        else:
            acquired = self._inner.acquire(blocking, timeout)
        if call == 2 and acquired:
            self.second_acquired.set()
        return acquired

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "CoordinatedRenderLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def break_historical_deadlock(self) -> None:
        if self._inner.locked():
            self._inner.release()


class FxPreviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fx-preview-api-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        for name in ("source", "working", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        self.source = self.project / "source/source.mp4"
        self.source.write_bytes(b"fixture-source-video")
        self._write_json(
            "project.json",
            {
                "schema_version": 1,
                "project_id": "fx-preview-test",
                "source": {
                    "staged_path": "source/source.mp4",
                    "duration_s": 8.0,
                    "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                },
                "platform": "instagram-reels",
                "approvals": {
                    "destructive_edit": {"approved": False},
                    "highlight_selection": {"approved": False},
                    "timeline": {"approved": False},
                    "final": {"approved": False},
                },
            },
        )
        self.fast_renderer = root / "fake-renderer.py"
        self.fast_renderer.write_text(FAST_RENDERER, encoding="utf-8")
        self.slow_renderer = root / "slow-renderer.py"
        self.slow_renderer.write_text(SLOW_RENDERER, encoding="utf-8")
        self.grandchild_renderer = root / "grandchild-renderer.py"
        self.grandchild_renderer.write_text(GRANDCHILD_RENDERER, encoding="utf-8")
        voice_patch = patch.object(editor_server, "load_voice_catalog", return_value={"voices": []})
        voice_patch.start()
        self.addCleanup(voice_patch.stop)
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.server.fx_preview_jobs.renderer_script = self.fast_renderer.resolve()
        self.server.fx_preview_jobs.artifact_validator = (
            lambda _path, duration: {"duration_s": duration}
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.host, self.port = self.server.server_address
        status, payload = self._json_request("GET", "/api/project")
        self.assertEqual(status, 200, payload)
        self.revision = payload["state"]["revision"]
        self.csrf = payload["csrf_token"]

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_json(self, relative: str, payload: object) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        request_headers = dict(headers or {})
        if method not in {"GET", "HEAD"} and "X-Auto-Edit-CSRF" not in request_headers:
            request_headers["X-Auto-Edit-CSRF"] = self.csrf
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.headers), payload
        connection.close()
        return result

    def _json_request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        body = None if payload is None and method == "GET" else json.dumps(payload if payload is not None else {}).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        status, _response_headers, raw = self._request(method, path, body, request_headers)
        return status, json.loads(raw or b"{}")

    def _candidate(self, code: str = "T3") -> dict:
        return {
            "schema_version": 1,
            "expected_revision": self.revision,
            "candidate": {
                "code": code,
                "window": {"start_s": 1.0, "duration_s": 2.0},
                "values": {},
                "placement": "",
                "scheme": "",
                "font_ids": {},
            },
        }

    def _upload_asset(self, filename: str, data: bytes = PNG_1X1) -> dict:
        status, _headers, raw = self._request(
            "POST",
            f"/api/assets?filename={filename}",
            data,
            {
                "Content-Type": "image/png",
                "X-Auto-Edit-CSRF": self.csrf,
            },
        )
        payload = json.loads(raw or b"{}")
        self.assertEqual(status, 200, payload)
        return payload

    def _wait_terminal(self, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload = self._json_request("GET", f"/api/fx-previews/{job_id}")
            self.assertEqual(status, 200, payload)
            if payload["job"]["state"] in {"ready", "failed", "cancelled"}:
                return payload
            time.sleep(0.05)
        self.fail("FX preview job did not reach a terminal state")

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def _kill_pid_if_alive(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_preview_is_non_mutating_then_confirm_uses_cas(self) -> None:
        state_path = self.project / "working/editor_state.json"
        manifest_path = self.project / "project.json"
        state_before = state_path.read_bytes()
        manifest_before = manifest_path.read_bytes()

        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        terminal = self._wait_terminal(created["job"]["id"])
        self.assertEqual(terminal["job"]["state"], "ready", terminal)
        self.assertEqual(terminal["job"]["text_layout"]["version"], 2)
        self.assertNotIn("text", terminal["job"]["text_layout"]["samples"][0]["boxes"][0])
        self.assertNotIn("detail", terminal["job"]["text_layout"])
        with self.server.fx_preview_jobs._lock:
            frozen_canvas = self.server.fx_preview_jobs._jobs[created["job"]["id"]]["dependencies"]["canvas"]
        self.assertEqual(frozen_canvas["fit"], "contain")
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

        media_url = terminal["job"]["media_url"]
        status, headers, body = self._request("GET", media_url, headers={"Range": "bytes=0-3"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"synt")
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertNotEqual(confirmed["revision"], self.revision)
        self.assertEqual(confirmed["state"]["overlays"][-1]["code"], "T3")
        self.assertEqual(confirmed["state"]["overlays"][-1]["id"], confirmed["overlay_id"])

        status, stale = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["reason"], "preview_stale")

        # Revision-only timeline changes do not alter rendered pixels.  The
        # artifact may be reused, while confirmation remains bound to the new
        # revision through the frozen dependency snapshot above.
        self.revision = confirmed["revision"]
        status, cached = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 200, cached)
        self.assertTrue(cached["job"]["cache_hit"])
        self.assertEqual(cached["job"]["text_layout"], terminal["job"]["text_layout"])

    def test_b1_unknown_fields_and_nan_fail_closed(self) -> None:
        status, rejected = self._json_request("POST", "/api/fx-previews", self._candidate("B1"))
        self.assertEqual(status, 422, rejected)
        self.assertEqual(rejected["reason"], "material_required")

        unknown = self._candidate()
        unknown["candidate"]["source"] = "/tmp/private.mp4"
        status, rejected = self._json_request("POST", "/api/fx-previews", unknown)
        self.assertEqual(status, 400, rejected)
        self.assertEqual(rejected["reason"], "malformed_request")

        nan_payload = json.dumps(self._candidate()).replace('"start_s": 1.0', '"start_s": NaN').encode()
        status, _headers, raw = self._request(
            "POST",
            "/api/fx-previews",
            nan_payload,
            {"Content-Type": "application/json", "X-Auto-Edit-CSRF": self.csrf},
        )
        self.assertEqual(status, 422, raw)
        self.assertEqual(json.loads(raw)["reason"], "window_invalid")

    def test_short_sample_confirms_the_full_apply_window(self) -> None:
        request = self._candidate()
        request["candidate"]["window"] = {"start_s": 2.0, "duration_s": 2.0}
        request["candidate"]["apply_window"] = {"start_s": 0.0, "duration_s": 8.0}
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)
        self.assertEqual(ready["job"]["render_duration_s"], 2.0)
        with self.server.fx_preview_jobs._lock:
            job = self.server.fx_preview_jobs._jobs[created["job"]["id"]]
            self.assertEqual(job["sample_window"], {"start_s": 2.0, "duration_s": 2.0})
            self.assertEqual((job["overlay"]["start"], job["overlay"]["end"]), (0.0, 8.0))
            self.assertEqual(
                (job["state_snapshot"]["overlays"][-1]["start"], job["state_snapshot"]["overlays"][-1]["end"]),
                (0.0, 8.0),
            )
        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(
            (confirmed["state"]["overlays"][-1]["start"], confirmed["state"]["overlays"][-1]["end"]),
            (0.0, 8.0),
        )

    def test_same_visual_add_then_replace_hits_single_preview_cache(self) -> None:
        candidate = self._candidate()
        status, created = self._json_request("POST", "/api/fx-previews", candidate)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{ready['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        self.revision = confirmed["revision"]
        replacement = self._candidate()
        replacement["replace_overlay_id"] = confirmed["overlay_id"]
        status, cached = self._json_request("POST", "/api/fx-previews", replacement)
        self.assertEqual(status, 200, cached)
        self.assertTrue(cached["job"]["cache_hit"], cached)

    def test_preview_options_and_subject_change_the_cache_identity(self) -> None:
        baseline = self._candidate()
        status, first = self._json_request("POST", "/api/fx-previews", baseline)
        self.assertEqual(status, 202, first)
        self.assertEqual(self._wait_terminal(first["job"]["id"])["job"]["state"], "ready")

        timeline = self._candidate()
        timeline["preview_options"] = {"mode": "timeline", "quality": "standard"}
        status, second = self._json_request("POST", "/api/fx-previews", timeline)
        self.assertEqual(status, 202, second)
        self.assertFalse(second["job"]["cache_hit"])
        self.assertEqual(second["job"]["preview_options"], timeline["preview_options"])
        self.assertEqual(self._wait_terminal(second["job"]["id"])["job"]["state"], "ready")
        with self.server.fx_preview_jobs._lock:
            dependencies = self.server.fx_preview_jobs._jobs[second["job"]["id"]]["dependencies"]
        self.assertIn("fx_preview_contract", dependencies["dependencies"])
        self.assertIn("text_fit", dependencies["dependencies"])
        self.assertIn("spotlight_track", dependencies["dependencies"])
        self.assertEqual(
            dependencies["dependencies"]["yunet_model"],
            "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
        )
        self.assertIn("render_editor_timeline.py", dependencies["timeline_python_dependencies"])
        self.assertIn("T3", dependencies["timeline_fx_blocks"])

        draft = self._candidate()
        draft["preview_options"] = {"mode": "single", "quality": "draft"}
        status, third = self._json_request("POST", "/api/fx-previews", draft)
        self.assertEqual(status, 202, third)
        self.assertFalse(third["job"]["cache_hit"])
        self.assertEqual(self._wait_terminal(third["job"]["id"])["job"]["state"], "ready")

        subject_a = self._candidate("P5")
        subject_a["candidate"]["subject"] = {"x": 0.25, "y": 0.5}
        status, fourth = self._json_request("POST", "/api/fx-previews", subject_a)
        self.assertEqual(status, 202, fourth)
        self.assertEqual(self._wait_terminal(fourth["job"]["id"])["job"]["state"], "ready")
        subject_b = self._candidate("P5")
        subject_b["candidate"]["subject"] = {"x": 0.75, "y": 0.5}
        status, fifth = self._json_request("POST", "/api/fx-previews", subject_b)
        self.assertEqual(status, 202, fifth)
        self.assertFalse(fifth["job"]["cache_hit"])
        self.assertEqual(self._wait_terminal(fifth["job"]["id"])["job"]["state"], "ready")

    def test_b1_uses_only_opaque_owned_asset_binding_and_rights_gate(self) -> None:
        uploaded = self._upload_asset("material.png")
        status, library = self._json_request("GET", "/api/assets/library")
        self.assertEqual(status, 200, library)
        material = next(item for item in library["assets"] if item["sha256"] == uploaded["sha256"])
        self.assertEqual(material["asset_id"], uploaded["asset_id"])

        request = self._candidate("B1")
        request["candidate"]["material"] = {
            "asset_id": material["asset_id"],
            "sha256": material["sha256"],
        }
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)

        second_uploaded = self._upload_asset("other-material.png")
        second_request = self._candidate("B1")
        second_request["candidate"]["material"] = {
            "asset_id": second_uploaded["asset_id"],
            "sha256": second_uploaded["sha256"],
        }
        status, second = self._json_request("POST", "/api/fx-previews", second_request)
        self.assertEqual(status, 202, second)
        self.assertFalse(second["job"]["cache_hit"])
        self.assertEqual(self._wait_terminal(second["job"]["id"])["job"]["state"], "ready")

        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        binding = confirmed["state"]["overlays"][-1]["material"]
        self.assertEqual(set(binding), {"asset_id", "path", "sha256", "kind"})
        self.assertEqual(binding["asset_id"], material["asset_id"])
        self.assertEqual(
            confirmed["state"]["asset_digests"][binding["path"]], binding["sha256"]
        )
        rights_errors = editor_server.rights_gate_errors(self.project, confirmed["state"])
        self.assertTrue(any(binding["path"] in error for error in rights_errors), rights_errors)

        tampered = json.loads(json.dumps(confirmed["state"]))
        tampered["overlays"][-1]["material"]["path"] = "assets/client-smuggled.png"
        status, rejected = self._json_request("PUT", "/api/editor-state", tampered)
        self.assertEqual(status, 422, rejected)
        self.assertTrue(
            any("素材" in error or "material" in error for error in rejected.get("errors", [])),
            rejected,
        )

    def test_timeline_b1_freezes_candidate_asset_and_registry_authority(self) -> None:
        uploaded = self._upload_asset("timeline-material.png")
        request = self._candidate("B1")
        request["candidate"]["material"] = {
            "asset_id": uploaded["asset_id"],
            "sha256": uploaded["sha256"],
        }
        request["preview_options"] = {"mode": "timeline", "quality": "standard"}
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)
        with self.server.fx_preview_jobs._lock:
            job = self.server.fx_preview_jobs._jobs[created["job"]["id"]]
            dependencies = job["dependencies"]
            frozen_state = job["timeline_snapshot"]["state"]
        relative = uploaded["source"]
        self.assertEqual(frozen_state["asset_digests"][relative], uploaded["sha256"])
        self.assertEqual(dependencies["asset_digests"][relative], uploaded["sha256"])
        self.assertIn("assets/provenance.json", dependencies["provenance_digests"])

    def test_timeline_b1_across_cut_preview_confirms_and_persists(self) -> None:
        status, current = self._json_request("GET", "/api/project")
        self.assertEqual(status, 200, current)
        state = current["state"]
        state["segments"] = [
            {
                "id": "segment-before-cut",
                "source_start": 0.0,
                "source_end": 5.0,
                "origin": "manual",
            },
            {
                "id": "segment-after-cut",
                "source_start": 6.0,
                "source_end": 8.0,
                "origin": "manual",
            },
        ]
        state["x_expected_revision"] = self.revision
        status, saved = self._json_request("PUT", "/api/editor-state", state)
        self.assertEqual(status, 200, saved)
        self.revision = saved["revision"]

        uploaded = self._upload_asset("timeline-confirm.png")
        request = self._candidate("B1")
        request["candidate"]["window"] = {"start_s": 3.0, "duration_s": 4.0}
        request["candidate"]["material"] = {
            "asset_id": uploaded["asset_id"],
            "sha256": uploaded["sha256"],
        }
        request["preview_options"] = {"mode": "timeline", "quality": "standard"}
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)

        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        persisted = confirmed["state"]["overlays"][-1]
        self.assertEqual((persisted["start"], persisted["end"]), (3.0, 7.0))
        self.assertEqual(persisted["material"]["asset_id"], uploaded["asset_id"])
        self.assertEqual(
            confirmed["state"]["asset_digests"][uploaded["source"]],
            uploaded["sha256"],
        )
        self.assertEqual(
            [(segment["source_start"], segment["source_end"]) for segment in confirmed["state"]["segments"]],
            [(0.0, 5.0), (6.0, 8.0)],
        )

    def test_timeline_t3_preview_confirms_and_persists(self) -> None:
        request = self._candidate("T3")
        request["preview_options"] = {"mode": "timeline", "quality": "draft"}
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)
        status, confirmed = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["state"]["overlays"][-1]["code"], "T3")

    def test_timeline_b1_confirm_rejects_asset_registry_and_code_drift(self) -> None:
        uploaded = self._upload_asset("timeline-drift.png")
        request = self._candidate("B1")
        request["candidate"]["material"] = {
            "asset_id": uploaded["asset_id"],
            "sha256": uploaded["sha256"],
        }
        request["preview_options"] = {"mode": "timeline", "quality": "standard"}

        def ready_job() -> str:
            status, created = self._json_request("POST", "/api/fx-previews", request)
            self.assertIn(status, {200, 202}, created)
            ready = self._wait_terminal(created["job"]["id"])
            self.assertEqual(ready["job"]["state"], "ready", ready)
            return created["job"]["id"]

        def rejected_confirm(job_id: str) -> dict:
            status, rejected = self._json_request(
                "POST",
                f"/api/fx-previews/{job_id}/confirm",
                {"expected_revision": self.revision},
            )
            self.assertEqual(status, 409, rejected)
            return rejected

        asset = self.project / uploaded["source"]
        asset_before = asset.read_bytes()
        job_id = ready_job()
        asset.write_bytes(b"changed-after-timeline-preview")
        try:
            rejected = rejected_confirm(job_id)
            self.assertIn(
                rejected["reason"],
                {"asset_changed", "material_changed", "material_unavailable"},
            )
        finally:
            asset.write_bytes(asset_before)

        registry = self.project / "assets/provenance.json"
        registry_before = registry.read_bytes()
        job_id = ready_job()
        registry.write_bytes(registry_before + b"\n")
        try:
            self.assertEqual(rejected_confirm(job_id)["reason"], "preview_stale")
        finally:
            registry.write_bytes(registry_before)

        renderer_before = self.fast_renderer.read_bytes()
        job_id = ready_job()
        self.fast_renderer.write_bytes(renderer_before + b"\n# code drift\n")
        try:
            self.assertEqual(rejected_confirm(job_id)["reason"], "preview_stale")
        finally:
            self.fast_renderer.write_bytes(renderer_before)

    def test_b1_rejects_client_paths_and_asset_changes_before_confirm(self) -> None:
        uploaded = self._upload_asset("material.png")
        malicious = self._candidate("B1")
        malicious["candidate"]["material"] = {
            "asset_id": uploaded["asset_id"],
            "sha256": uploaded["sha256"],
            "path": "/tmp/private.png",
        }
        status, rejected = self._json_request("POST", "/api/fx-previews", malicious)
        self.assertEqual(status, 422, rejected)
        self.assertEqual(rejected["reason"], "material_invalid")

        request = self._candidate("B1")
        request["candidate"]["material"] = {
            "asset_id": uploaded["asset_id"],
            "sha256": uploaded["sha256"],
        }
        status, created = self._json_request("POST", "/api/fx-previews", request)
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready)
        (self.project / uploaded["source"]).write_bytes(b"changed-after-preview")
        status, rejected = self._json_request(
            "POST",
            f"/api/fx-previews/{created['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 409, rejected)
        self.assertIn(
            rejected["reason"],
            {"material_changed", "material_unavailable", "material_invalid"},
        )

    def test_asset_library_omits_symlink_ancestors_svg_fonts_and_undecodable(self) -> None:
        uploaded = self._upload_asset("safe.png")
        outside = self.project.parent / "outside-assets"
        outside.mkdir()
        (outside / "stolen.png").write_bytes(PNG_1X1)
        (self.project / "assets/linked").symlink_to(outside, target_is_directory=True)
        (self.project / "assets/icon.svg").write_text("<svg/>", encoding="utf-8")
        (self.project / "assets/font.ttf").write_bytes(b"not-a-font")
        (self.project / "assets/broken.png").write_bytes(b"not-a-png")
        status, library = self._json_request("GET", "/api/assets/library")
        self.assertEqual(status, 200, library)
        paths = {item["path"] for item in library["assets"]}
        self.assertIn(uploaded["source"], paths)
        self.assertNotIn("assets/linked/stolen.png", paths)
        self.assertNotIn("assets/icon.svg", paths)
        self.assertNotIn("assets/font.ttf", paths)
        self.assertNotIn("assets/broken.png", paths)

    def test_extreme_integer_window_fields_return_422_without_disconnect(self) -> None:
        for field in ("start_s", "duration_s"):
            with self.subTest(field=field):
                payload = self._candidate()
                payload["candidate"]["window"][field] = 10**1000
                status, rejected = self._json_request("POST", "/api/fx-previews", payload)
                self.assertEqual(status, 422, rejected)
                self.assertEqual(rejected["reason"], "window_invalid")

    def test_timeline_time_map_allows_deleted_source_gap(self) -> None:
        normalized = fx_preview_jobs.FxPreviewJobManager._normalized_time_map(
            [
                {
                    "source_start_s": 3.0,
                    "source_end_s": 5.0,
                    "output_start_s": 0.0,
                    "output_end_s": 2.0,
                },
                {
                    "source_start_s": 6.0,
                    "source_end_s": 7.0,
                    "output_start_s": 2.0,
                    "output_end_s": 3.0,
                },
            ],
            {"start_s": 3.0, "duration_s": 4.0},
            3.0,
            mode="timeline",
        )
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[-1]["output_end_s"], 3.0)

    def test_text_layout_metadata_is_bounded_and_projected(self) -> None:
        box = {
            "id": "x" * 300,
            "text": "secret" * 1000,
            "x": 1,
            "y": 2,
            "width": 3,
            "height": 4,
            "fontFamily": "font" * 100,
            "visible": True,
            "fitted": False,
            "unknown": {"deep": "object"},
        }
        raw = {
            "version": 2,
            "measurement": "composition-dom",
            "status": "warn",
            "face_measurement": "source-opencv-yunet",
            "analysis_scope": "individual-fx-layout",
            "samples": [{"at": index, "boxes": [box] * 100} for index in range(20)],
            "boxes": [box] * 400,
            "findings": [
                {"kind": "face_overlap", "id": "title", "at": 1, "private": "drop"}
            ] * 200,
            "subject_samples": [
                {"at": 0, "source_at": 1, "boxes": [{"x": 1, "y": 2, "width": 3, "height": 4}] * 40}
            ] * 20,
            "detail": "drop",
            "layers": {
                f"layer-{index}": {
                    "version": 2,
                    "measurement": "composition-dom",
                    "status": "pass",
                    "samples": [],
                    "boxes": [],
                    "findings": [],
                }
                for index in range(100)
            },
        }
        clean = fx_preview_jobs._sanitize_text_layout(raw)
        self.assertEqual(len(clean["samples"]), 8)
        self.assertLessEqual(sum(len(sample["boxes"]) for sample in clean["samples"]), 512)
        self.assertLessEqual(len(clean["findings"]), 128)
        self.assertLessEqual(len(clean["layers"]), 64)
        self.assertNotIn("detail", clean)
        self.assertNotIn("text", clean["samples"][0]["boxes"][0])
        self.assertEqual(clean["findings"][0]["kind"], "face_overlap")
        self.assertEqual(clean["analysis_scope"], "individual-fx-layout")

    def test_text_layout_unknown_finding_cannot_remain_pass(self) -> None:
        clean = fx_preview_jobs._sanitize_text_layout(
            {
                "version": 2,
                "measurement": "composition-dom",
                "status": "pass",
                "findings": [{"kind": "future_warning_kind", "private": "drop"}],
            }
        )
        self.assertEqual(clean["status"], "warn")
        self.assertEqual(clean["findings"], [{"kind": "unrecognized_warning"}])

    def test_text_layout_truncated_or_nested_findings_cannot_remain_pass(self) -> None:
        saturated = [{"kind": "safe_area"} for _ in range(128)]
        saturated.append({"kind": "future_warning_kind", "private": "drop"})
        clean = fx_preview_jobs._sanitize_text_layout(
            {
                "version": 2,
                "measurement": "composition-dom",
                "status": "pass",
                "findings": saturated,
                "layers": {
                    "candidate": {
                        "version": 2,
                        "measurement": "composition-dom",
                        "status": "pass",
                        "findings": saturated,
                    }
                },
            }
        )
        self.assertEqual(len(clean["findings"]), 128)
        self.assertEqual(clean["status"], "warn")
        self.assertEqual(clean["layers"]["candidate"]["status"], "warn")

        layer_overflow = fx_preview_jobs._sanitize_text_layout(
            {
                "version": 2,
                "measurement": "composition-dom",
                "status": "pass",
                "findings": [],
                "layers": {
                    f"layer-{index}": {
                        "version": 2,
                        "measurement": "composition-dom",
                        "status": "pass",
                        "findings": [],
                    }
                    for index in range(65)
                },
            }
        )
        self.assertEqual(len(layer_overflow["layers"]), 64)
        self.assertEqual(layer_overflow["status"], "warn")

    def test_unregistered_material_is_visible_but_not_b1_selectable(self) -> None:
        loose = self.project / "assets/loose.png"
        loose.write_bytes(PNG_1X1)
        assets = fx_preview_contract.list_material_assets(
            self.project, decode_probe=lambda _path: True
        )
        item = next(asset for asset in assets if asset["path"] == "assets/loose.png")
        self.assertIsNone(item["asset_id"])
        self.assertIsNone(item["registry_item"])
        with self.assertRaises(fx_preview_contract.FxPreviewContractError) as raised:
            fx_preview_contract.resolve_material_binding(
                self.project,
                {
                    "asset_id": "asset-local-"
                    + hashlib.sha256(b"assets/loose.png").hexdigest(),
                    "sha256": item["sha256"],
                },
                decode_probe=lambda _path: True,
            )
        self.assertEqual(raised.exception.code, "material_unavailable")

    def test_directory_identity_swap_fails_closed_without_touching_external_sentinel(self) -> None:
        manager = self.server.fx_preview_jobs
        external = self.project.parent / "external-sentinel"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (external / "timeline-project-trap").mkdir()
        original = manager.renderer_cache_dir
        backup = original.with_name("renderer-cache-pinned")
        original.rename(backup)
        original.symlink_to(external, target_is_directory=True)
        try:
            with self.assertRaises(fx_preview_jobs.FxPreviewJobError):
                manager._cleanup_renderer_scratch()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue((external / "timeline-project-trap").is_dir())
        finally:
            original.unlink()
            backup.rename(original)

    def test_each_preview_storage_directory_swap_fails_closed(self) -> None:
        manager = self.server.fx_preview_jobs
        external = self.project.parent / "external-storage-trap"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")

        cases = (
            ("root", manager.root, manager.prune),
            ("cache", manager.cache_dir, manager.prune),
            ("runtime", manager.runtime_dir, manager._cleanup_orphan_partials),
        )
        for label, original, operation in cases:
            with self.subTest(directory=label):
                backup = original.with_name(f"{original.name}-pinned-{label}")
                original.rename(backup)
                original.symlink_to(external, target_is_directory=True)
                try:
                    with self.assertRaises(fx_preview_jobs.FxPreviewJobError):
                        operation()
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
                finally:
                    original.unlink()
                    backup.rename(original)

    def test_timeline_dependency_rejects_replaced_working_ancestor(self) -> None:
        manager = self.server.fx_preview_jobs
        working = self.project / "working"
        backup = self.project / "working-pinned"
        external = self.project.parent / "outside-working"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        working.rename(backup)
        working.symlink_to(external, target_is_directory=True)
        try:
            with self.assertRaises(fx_preview_jobs.FxPreviewJobError):
                manager._timeline_project_input_digests()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        finally:
            working.unlink()
            backup.rename(working)

    def test_tool_identity_hash_catches_same_size_same_mtime_rewrite(self) -> None:
        tool = self.project.parent / "fake-tool"
        tool.write_bytes(b"first-version")
        stamp = tool.stat()
        with patch.object(fx_preview_jobs.shutil, "which", return_value=str(tool)):
            first = fx_preview_jobs.FxPreviewJobManager._tool_identities()
            tool.write_bytes(b"other-version")
            os.utime(tool, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            second = fx_preview_jobs.FxPreviewJobManager._tool_identities()
        self.assertNotEqual(first["ffmpeg"]["sha256"], second["ffmpeg"]["sha256"])

    def test_font_digest_rehashes_same_size_same_mtime_rewrite(self) -> None:
        font = self.project.parent / "fixture-font.ttf"
        font.write_bytes(b"font-version-a")
        stamp = font.stat()
        first = fx_overlay._file_digest(font)
        font.write_bytes(b"font-version-b")
        os.utime(font, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        second = fx_overlay._file_digest(font)
        self.assertNotEqual(first, second)

    def test_owned_asset_hardlink_is_rejected(self) -> None:
        uploaded = self._upload_asset("hardlink.png")
        asset = self.project / uploaded["source"]
        os.link(asset, self.project.parent / "second-link.png")
        with self.assertRaises(fx_preview_contract.FxPreviewContractError) as raised:
            fx_preview_contract.resolve_material_binding(
                self.project,
                {"asset_id": uploaded["asset_id"], "sha256": uploaded["sha256"]},
            )
        self.assertEqual(raised.exception.code, "material_unavailable")

    def test_cancel_permission_race_is_retryable_and_never_claims_cancelled(self) -> None:
        manager = self.server.fx_preview_jobs
        job_id = "a" * 32
        fake_process = object()
        with manager._lock:
            manager._jobs[job_id] = {
                "id": job_id,
                "state": "running",
                "phase": "rendering",
                "message": "running",
                "cache_hit": False,
                "preview_options": {"mode": "single", "quality": "standard"},
                "process": fake_process,
                "cancel_requested": False,
                "updated_monotonic": time.time(),
                "created_monotonic": time.time(),
                "timing_started": time.monotonic() - 5,
                "timing_phase_started": time.monotonic() - 3,
                "phase_timings_s": {},
            }
            manager._active_job_id = job_id
        with patch.object(manager, "_terminate_process", return_value=False):
            first = manager.cancel(job_id)
        self.assertEqual(first["state"], "running")
        self.assertEqual(first["error"]["code"], "termination_unverified")
        self.assertGreaterEqual(first["phase_timings_s"].get("rendering", 0), 2.9)
        with patch.object(manager, "_terminate_process", return_value=True):
            second = manager.cancel(job_id)
        self.assertEqual(second["state"], "cancelled")

    def test_http_timing_lifecycle_and_sample_limits(self) -> None:
        manager = self.server.fx_preview_jobs
        delayed = self.fast_renderer.parent / "delayed-timing.py"
        marker = 'out=Path(req["output"]);'
        self.assertIn(marker, FAST_RENDERER)
        delayed.write_text(FAST_RENDERER.replace(marker, 'import time; time.sleep(1.0)\n' + marker), encoding="utf-8")
        manager.renderer_script = delayed.resolve()

        def body(start: float) -> dict:
            candidate = self._candidate()
            candidate["candidate"]["window"]["start_s"] = start
            return candidate

        terminal = None
        for i in range(3):
            status, created = self._json_request("POST", "/api/fx-previews", body(1.0 + i * 0.1))
            self.assertEqual(status, 202, created)
            self.assertIsNone(created["job"]["eta_remaining_s"])
            terminal = self._wait_terminal(created["job"]["id"])["job"]
            self.assertEqual(terminal["state"], "ready")
            self.assertEqual(terminal["eta_sample_count"], i + 1)
            self.assertIn("rendering", terminal["phase_timings_s"])
            self.assertIn("validating", terminal["phase_timings_s"])
        assert terminal is not None
        time.sleep(0.03)
        _, repeated = self._json_request("GET", terminal["status_url"])
        self.assertEqual(repeated["job"]["elapsed_s"], terminal["elapsed_s"])

        _, created = self._json_request("POST", "/api/fx-previews", body(1.4))
        _, active = self._json_request("GET", created["job"]["status_url"])
        self.assertEqual(active["job"]["eta_sample_count"], 3)
        self.assertIsInstance(active["job"]["eta_remaining_s"], (int, float))
        done = self._wait_terminal(created["job"]["id"])["job"]
        self.assertEqual(done["eta_sample_count"], 4)
        _, cached = self._json_request("POST", "/api/fx-previews", body(1.0))
        self.assertTrue(cached["job"]["cache_hit"])
        self.assertEqual(cached["job"]["eta_sample_count"], 4)

        failing = self.fast_renderer.parent / "fail-timing.py"
        failing.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        manager.renderer_script = failing.resolve()
        _, created = self._json_request("POST", "/api/fx-previews", body(1.5))
        failed = self._wait_terminal(created["job"]["id"])["job"]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["eta_sample_count"], 4)
        time.sleep(0.03)
        _, repeated = self._json_request("GET", failed["status_url"])
        self.assertEqual(repeated["job"]["elapsed_s"], failed["elapsed_s"])

        manager.renderer_script = self.slow_renderer.resolve()
        _, created = self._json_request("POST", "/api/fx-previews", body(1.6))
        _, response = self._json_request("POST", created["job"]["status_url"] + "/cancel", {})
        cancelled = response["job"]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["eta_sample_count"], 4)
        time.sleep(0.03)
        _, repeated = self._json_request("GET", cancelled["status_url"])
        self.assertEqual(repeated["job"]["elapsed_s"], cancelled["elapsed_s"])

        # Exercise retention through the same manager's recording method without
        # launching hundreds of renderer processes.
        with manager._lock:
            for i in range(101):
                manager._record_timing_sample(("single", "draft", f"test-{i}", 2.0, 2.0), 1.0)
            self.assertEqual(len(manager._timing_samples), 100)
            key = ("single", "draft", "last", 2.0, 2.0)
            for i in range(25):
                manager._record_timing_sample(key, float(i))
            self.assertEqual(manager._timing_samples[key], list(map(float, range(5, 25))))

    def test_sigkill_permission_error_is_caught_and_reported_unverified(self) -> None:
        class FakeProcess:
            pid = 424242

            @staticmethod
            def wait(timeout: float) -> None:
                raise __import__("subprocess").TimeoutExpired("fake", timeout)

            @staticmethod
            def poll() -> None:
                return None

        def signal_group(_pgid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise PermissionError("race")

        with (
            patch.object(fx_preview_jobs.os, "killpg", side_effect=signal_group),
            patch.object(fx_preview_jobs.FxPreviewJobManager, "_process_group_exists", return_value=True),
            patch.object(fx_preview_jobs.time, "monotonic", side_effect=[0.0, 4.0, 4.0, 7.0]),
            patch.object(fx_preview_jobs.time, "sleep", return_value=None),
        ):
            self.assertFalse(
                fx_preview_jobs.FxPreviewJobManager._terminate_process(FakeProcess())
            )

    def test_full_render_and_preview_reservation_cannot_abba_deadlock(self) -> None:
        coordinated = CoordinatedRenderLock()
        self.server.render_lock = coordinated
        results: dict[str, tuple[int, dict]] = {}

        def request_render() -> None:
            results["render"] = self._json_request(
                "POST",
                "/api/render",
                {"quality": "preview", "expected_revision": self.revision},
            )

        def request_preview() -> None:
            results["preview"] = self._json_request(
                "POST", "/api/fx-previews", self._candidate()
            )

        with patch.object(editor_server.EditorHandler, "render_worker", lambda *_args: None):
            render_thread = threading.Thread(target=request_render, daemon=True)
            render_thread.start()
            self.assertTrue(
                coordinated.first_attempt.wait(timeout=2),
                "full render never reached its reservation point",
            )
            preview_thread = threading.Thread(target=request_preview, daemon=True)
            preview_thread.start()
            render_thread.join(timeout=2)
            preview_thread.join(timeout=2)
            deadlocked = render_thread.is_alive() or preview_thread.is_alive()
            if deadlocked:
                # Keep a failing regression from leaking handler threads into
                # the rest of the suite when run against the old ordering.
                coordinated.break_historical_deadlock()
                render_thread.join(timeout=2)
                preview_thread.join(timeout=2)

        self.assertFalse(deadlocked, "full render and FX preview entered an AB-BA deadlock")
        self.assertEqual(results["render"][0], 202, results)
        self.assertEqual(results["preview"][0], 409, results)
        self.assertEqual(results["preview"][1]["reason"], "render_busy")

    def test_confirm_can_replace_only_the_frozen_existing_fx_layer(self) -> None:
        status, first = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, first)
        first_ready = self._wait_terminal(first["job"]["id"])
        status, first_confirm = self._json_request(
            "POST",
            f"/api/fx-previews/{first_ready['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, first_confirm)
        existing_id = first_confirm["overlay_id"]
        existing_state = first_confirm["state"]
        existing = next(item for item in existing_state["overlays"] if item.get("id") == existing_id)
        existing.update(
            {
                "z_index": 77,
                "visible": False,
                "locked": True,
                "highlight_id": "highlight-preserved",
                "style": {
                    "x": 12,
                    "y": 34,
                    "width": 56,
                    "animation": "none",
                    "opacity": 0.4,
                },
                "metadata": {"editor_note": "keep-me"},
            }
        )
        existing_state["highlights"] = [
            {
                "id": "highlight-preserved",
                "start": 0.5,
                "end": 4.0,
                "title": "preserved scope",
                "review_status": "approved",
            }
        ]
        self._write_json("working/editor_state.json", existing_state)
        status, refreshed = self._json_request("GET", "/api/project")
        self.assertEqual(status, 200, refreshed)
        self.revision = refreshed["state"]["revision"]

        replacement = self._candidate()
        replacement["replace_overlay_id"] = existing_id
        replacement["candidate"]["window"] = {"start_s": 2.0, "duration_s": 1.0}
        status, second = self._json_request("POST", "/api/fx-previews", replacement)
        self.assertEqual(status, 202, second)
        second_ready = self._wait_terminal(second["job"]["id"])
        status, replaced = self._json_request(
            "POST",
            f"/api/fx-previews/{second_ready['job']['id']}/confirm",
            {"expected_revision": self.revision},
        )
        self.assertEqual(status, 200, replaced)
        fx_layers = [item for item in replaced["state"]["overlays"] if item.get("type") == "fx"]
        self.assertEqual(len(fx_layers), 1)
        self.assertEqual(fx_layers[0]["id"], existing_id)
        self.assertEqual((fx_layers[0]["start"], fx_layers[0]["end"]), (2.0, 3.0))
        self.assertEqual(fx_layers[0]["z_index"], 77)
        self.assertFalse(fx_layers[0]["visible"])
        self.assertTrue(fx_layers[0]["locked"])
        self.assertEqual(fx_layers[0]["highlight_id"], "highlight-preserved")
        self.assertEqual(fx_layers[0]["style"]["opacity"], 0.4)
        self.assertEqual(fx_layers[0]["metadata"], {"editor_note": "keep-me"})
        self.assertTrue(replaced["replaced"])

    def test_source_change_during_job_prevents_publication(self) -> None:
        self.server.fx_preview_jobs.renderer_script = self.slow_renderer.resolve()
        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        self.source.write_bytes(b"changed-source")
        terminal = self._wait_terminal(created["job"]["id"], timeout=7)
        self.assertEqual(terminal["job"]["state"], "failed", terminal)
        self.assertEqual(terminal["job"]["error"]["code"], "preview_stale")
        self.assertNotIn("media_url", terminal["job"])

    def test_cancel_terminates_worker_and_never_publishes_media(self) -> None:
        self.server.fx_preview_jobs.renderer_script = self.slow_renderer.resolve()
        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        job_id = created["job"]["id"]
        status, cancelled = self._json_request("POST", f"/api/fx-previews/{job_id}/cancel", {})
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["job"]["state"], "cancelled")
        self.assertNotIn("media_url", cancelled["job"])

    def test_worker_timeout_fails_without_publishing_media(self) -> None:
        self.server.fx_preview_jobs.renderer_script = self.slow_renderer.resolve()
        self.server.fx_preview_jobs.render_timeout_s = 0.1
        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        terminal = self._wait_terminal(created["job"]["id"])
        self.assertEqual(terminal["job"]["state"], "failed")
        self.assertEqual(terminal["job"]["error"]["code"], "render_timeout")
        self.assertNotIn("media_url", terminal["job"])

    def test_cancel_kills_term_ignoring_worker_grandchild(self) -> None:
        self.server.fx_preview_jobs.renderer_script = self.grandchild_renderer.resolve()
        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        pid_path = self.project / "working/grandchild.pid"
        deadline = time.monotonic() + 3
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_path.is_file(), "fake renderer did not start its grandchild")
        child_pid = int(pid_path.read_text())
        self.addCleanup(self._kill_pid_if_alive, child_pid)

        status, cancelled = self._json_request(
            "POST", f"/api/fx-previews/{created['job']['id']}/cancel", {}
        )
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["job"]["state"], "cancelled")
        deadline = time.monotonic() + 3
        while self._pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._pid_exists(child_pid), "worker grandchild survived cancellation")

    def test_idle_cache_ttl_removes_only_preview_owned_artifacts(self) -> None:
        status, created = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, created)
        ready = self._wait_terminal(created["job"]["id"])
        self.assertEqual(ready["job"]["state"], "ready")
        with self.server.fx_preview_jobs._lock:
            job = self.server.fx_preview_jobs._jobs[created["job"]["id"]]
            media = job["media"]["path"]
            receipt = self.server.fx_preview_jobs.cache_dir / f"{job['cache_key']}.json"
        renderer_cache = self.server.fx_preview_jobs.renderer_cache_dir / "old-mask.bin"
        renderer_cache.write_bytes(b"preview-owned-cache")
        old = time.time() - fx_preview_jobs.CACHE_IDLE_TTL_S - 1
        os.utime(media, (old, old))
        os.utime(receipt, (old, old))
        os.utime(renderer_cache, (old, old))

        self.server.fx_preview_jobs.prune()
        self.assertFalse(media.exists())
        self.assertFalse(receipt.exists())
        self.assertFalse(renderer_cache.exists())
        terminal = self.server.fx_preview_jobs.response(created["job"]["id"])
        self.assertEqual(terminal["job"]["state"], "failed")
        self.assertEqual(terminal["job"]["error"]["code"], "cache_evicted")

    def test_cache_byte_cap_evicts_oldest_ready_artifact(self) -> None:
        status, first = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, first)
        first_ready = self._wait_terminal(first["job"]["id"])
        self.assertEqual(first_ready["job"]["state"], "ready")

        second_candidate = self._candidate()
        second_candidate["candidate"]["window"] = {"start_s": 2.0, "duration_s": 1.0}
        with patch.object(fx_preview_jobs, "CACHE_MAX_BYTES", 20):
            status, second = self._json_request("POST", "/api/fx-previews", second_candidate)
            self.assertEqual(status, 202, second)
            second_ready = self._wait_terminal(second["job"]["id"])
        self.assertEqual(second_ready["job"]["state"], "ready")
        evicted = self.server.fx_preview_jobs.response(first["job"]["id"])
        self.assertEqual(evicted["job"]["state"], "failed")
        self.assertEqual(evicted["job"]["error"]["code"], "cache_evicted")
        self.assertEqual(len(list(self.server.fx_preview_jobs.cache_dir.glob("*.mp4"))), 1)

    def test_csrf_busy_and_cache_hit_contracts(self) -> None:
        body = json.dumps(self._candidate()).encode()
        status, _headers, raw = self._request(
            "POST", "/api/fx-previews", body, {"Content-Type": "application/json", "X-Auto-Edit-CSRF": "wrong"}
        )
        self.assertEqual(status, 403, raw)

        self.server.render_status = {"state": "running"}
        status, busy = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 409, busy)
        self.assertEqual(busy["reason"], "render_busy")
        self.server.render_status = {"state": "idle"}

        status, first = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 202, first)
        first_ready = self._wait_terminal(first["job"]["id"])
        self.assertEqual(first_ready["job"]["state"], "ready")
        status, second = self._json_request("POST", "/api/fx-previews", self._candidate())
        self.assertEqual(status, 200, second)
        self.assertTrue(second["job"]["cache_hit"])
        self.assertEqual(second["job"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import http.client
import json
import os
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
import fx_preview_jobs  # noqa: E402
from editor_server import EditorServer  # noqa: E402


FAST_RENDERER = r'''#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--request"); p.add_argument("--result"); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
print(json.dumps({"phase":"rendering","message":"ignored"}), file=__import__('sys').stderr, flush=True)
out=Path(req["output"]); out.write_bytes(b"synthetic-mp4-" + req["overlay"]["code"].encode())
tmp=Path(a.result + ".tmp")
tmp.write_text(json.dumps({"ok":True,"out":str(out),"duration_s":req["overlay"]["end"]-req["overlay"]["start"]}))
os.replace(tmp, a.result)
'''

SLOW_RENDERER = r'''#!/usr/bin/env python3
import argparse, json, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--request"); p.add_argument("--result"); a=p.parse_args()
req=json.loads(Path(a.request).read_text())
print(json.dumps({"phase":"rendering"}), file=__import__('sys').stderr, flush=True)
time.sleep(0.5)
out=Path(req["output"]); out.write_bytes(b"late-mp4")
tmp=Path(a.result + ".tmp"); tmp.write_text(json.dumps({"ok":True,"out":str(out)})); os.replace(tmp, a.result)
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

    def test_extreme_integer_window_fields_return_422_without_disconnect(self) -> None:
        for field in ("start_s", "duration_s"):
            with self.subTest(field=field):
                payload = self._candidate()
                payload["candidate"]["window"][field] = 10**1000
                status, rejected = self._json_request("POST", "/api/fx-previews", payload)
                self.assertEqual(status, 422, rejected)
                self.assertEqual(rejected["reason"], "window_invalid")

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

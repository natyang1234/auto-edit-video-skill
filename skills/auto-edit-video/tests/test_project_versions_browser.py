from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_editor_browser as base
CHROME = base.CHROME
sync_playwright = base.sync_playwright


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome unavailable")
class ProjectVersionsBrowserTests(unittest.TestCase):
    """Chrome contract test; routes are an explicit UI stub, not backend E2E."""
    write_json = base.EditorBrowserSmokeTests.write_json
    stop_server = base.EditorBrowserSmokeTests.stop_server

    def setUp(self):
        base.EditorBrowserSmokeTests.setUp(self)
        evidence_root = Path(os.environ.get("P7_UI_STUB_EVIDENCE_DIR", self.project / "qa/p7-ui-stub"))
        evidence_root.mkdir(parents=True, exist_ok=True)
        self._stub_evidence_dir = evidence_root
        # The fixture's source binding is a real SHA of the generated video,
        # not the old fake-source shortcut used by some browser tests.
        source = self.project / "source/source.mp4"
        rendered = subprocess.run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "smptebars=s=360x640:d=2", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], text=True, capture_output=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["sha256"] = hashlib.sha256((self.project / "source/source.mp4").read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_version_dialog_stub_contract_and_responsive(self):
        def stage(name):
            print(f"P7_STAGE {name}", flush=True)

        host, port = self.server.server_address
        posts = []
        held_details = {}
        held_restore = []
        restore_attempts = []
        b_detail_requests = [0]
        capacity = {"used": 2, "max": 100, "named_save_available": True, "restore_backup_available": True}
        versions = [
            {"version_id": "a/b", "name": "A very long version name " * 8, "kind": "named", "created_at": "2026-11-09T01:02:03Z", "state_digest": "state-a", "render_revision": "render-a"},
            {"version_id": "b", "name": "Version B", "kind": "auto_before_restore", "created_at": "2026-11-09T01:03:03Z", "state_digest": "state-b", "render_revision": "render-b"},
        ]
        current = {"state_digest": "state-now", "render_revision": "render-now", "manifest_digest": "manifest-now"}
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.set_default_timeout(8000)
            page.on("console", lambda message: print(f"P7_CONSOLE {message.type}: {message.text}", flush=True))
            page.on("pageerror", lambda error: print(f"P7_PAGEERROR {error}", flush=True))

            def route(route):
                request = route.request; path = urlparse(request.url).path
                if not path.startswith("/api/project-versions"):
                    route.fallback(); return
                if request.method == "GET" and path == "/api/project-versions":
                    route.fulfill(json={"ok": True, "versions": versions, "current": current, "capacity": capacity}); return
                if request.method == "GET":
                    version = versions[0] if path.endswith("a%2Fb") else versions[1]
                    content_changes = [
                        {"path": "/state/overlays/0/text", "kind": "changed", "saved_preview": f"{version['version_id']}-saved", "current_preview": "current"},
                        {"path": "/state/overlays/1/text", "kind": "changed", "saved_preview": '"true"', "current_preview": '"caption B"'},
                        {"path": "/state/custom/setting", "kind": "changed", "saved_preview": "null", "current_preview": "true"},
                        {"path": "/state/segments/1/id", "kind": "changed", "saved_preview": '"segment-0000a002"', "current_preview": '"segment-0000b002"'},
                        {"path": "/state/segments/1/source_start", "kind": "changed", "saved_preview": "1.05", "current_preview": "1.15"},
                        {"path": "/state/segments/1/source_end", "kind": "changed", "saved_preview": "1.9", "current_preview": "1.95"},
                        {"path": "/state/overlays/2/text", "kind": "changed", "saved_preview": '"segment-0000a002"', "current_preview": '"user caption"'},
                        {"path": "/state/highlights/0/review_status", "kind": "changed", "saved_preview": '"pending"', "current_preview": '"approved"'},
                    ] + [
                        {"path": f"/state/overlays/{index + 2}/text", "kind": "changed", "saved_preview": f'"saved {index}"', "current_preview": f'"current {index}"'}
                        for index in range(78)
                    ]
                    reset_changes = [
                        {"path": "/manifest/approvals/timeline/note", "kind": "changed", "saved_preview": '"Invalidated note"', "current_preview": "null"},
                        {"path": "/rights_assertion/revision", "kind": "changed", "saved_preview": '"' + "a" * 64 + '"', "current_preview": '"' + "b" * 64 + '"'},
                    ]
                    if version["version_id"] == "b":
                        b_detail_requests[0] += 1
                    changes = content_changes + reset_changes
                    truncated = False
                    if version["version_id"] == "b" and b_detail_requests[0] > 1:
                        changes = reset_changes[:1]
                        truncated = True
                    payload = {"ok": True, "version": version, "current": current, "diff": {"changed": True, "total_changes": len(changes), "truncated": truncated, "approval_resets": ["timeline"], "changes": changes}}
                    if version["version_id"] == "a/b":
                        held_details.setdefault("a", (route, payload)); return
                    route.fulfill(json=payload); return
                body = json.loads(request.post_data or "{}")
                posts.append((path, body))
                if path.endswith("/restore"):
                    restore_attempts.append(body)
                    held_restore.append(route)
                    return
                if body.get("name") == "錯誤版本":
                    route.fulfill(status=422, json={"ok": False, "error": "validation_error", "error_code": "validation_error"})
                    return
                else:
                    route.fulfill(status=201, json={"ok": True, "version": versions[0], "current": current})
            page.route("**/api/project-versions**", route)
            stage("goto")
            page.goto(f"http://{host}:{port}/", wait_until="domcontentloaded")
            page.screenshot(path=str(self._stub_evidence_dir / "before-open.png"), full_page=True)
            stage("open")
            page.locator("#project-versions-open").click()
            page.locator("#project-versions-dialog[open]").wait_for()
            stage("list-open")
            for width in (1440, 1100, 390):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), width)
                page.screenshot(path=str(self._stub_evidence_dir / f"viewport-{width}.png"), full_page=True)
            page.set_viewport_size({"width": 1100, "height": 900})
            page.locator(".project-version-row").first.click()
            page.locator(".project-version-row").nth(1).click()
            page.locator("#project-version-detail").get_by_text("b-saved").wait_for()
            held_details["a"][0].fulfill(json=held_details["a"][1])
            page.wait_for_timeout(150)
            self.assertIn("圖層文字", page.locator("#project-version-detail").inner_text())
            self.assertIn("b-saved", page.locator("#project-version-detail").inner_text())
            self.assertIn("目前：current → 還原為：b-saved", page.locator("#project-version-detail").inner_text())
            detail_text = page.locator("#project-version-detail").inner_text()
            self.assertIn("還原時會重新驗證來源影片與素材；若已變更，將停止還原。", detail_text)
            self.assertIn("還原為：true", detail_text)
            self.assertIn("目前：啟用 → 還原為：未設定", detail_text)
            self.assertIn("目前：已核可 → 還原為：待確認", detail_text)
            self.assertNotIn("目前：approved → 還原為：pending", detail_text)
            self.assertIn("第2段開始時間", detail_text)
            self.assertIn("第2段結束時間", detail_text)
            self.assertIn("還原為：segment-0000a002", detail_text)
            self.assertNotIn("目前：segment-0000b002", detail_text)
            self.assertIn("時間軸需重新確認", detail_text)
            self.assertNotIn("素材權利需重新確認", detail_text)
            self.assertNotIn("Invalidated note", detail_text)
            self.assertNotIn("a" * 64, detail_text)
            self.assertLess(detail_text.index("剪輯內容"), detail_text.index("重新確認"))
            self.assertIn("目前顯示 80 項剪輯內容", detail_text)
            self.assertTrue(page.locator(".project-version-row").nth(1).get_attribute("aria-pressed") == "true")
            stage("detail")
            page.locator("#project-version-name").focus()
            page.keyboard.type("版本 A")
            self.assertEqual(page.locator("#project-version-name").input_value(), "版本 A")
            page.locator("#project-version-name").fill("錯誤版本")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('版本名稱規則')")
            self.assertNotIn("validation_error", page.locator("#project-versions-status").inner_text())
            page.locator("#project-version-name").fill("版本 A")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('已儲存')")
            self.assertFalse(page.locator("#project-version-save").is_disabled())
            save = next(body for path, body in posts if path == "/api/project-versions")
            self.assertEqual(save["expected_manifest_digest"], "manifest-now")
            page.locator("#project-version-name").fill("版本 B")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('已儲存')")
            self.assertEqual(sum(path == "/api/project-versions" for path, _body in posts), 3)
            page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
            page.locator("#project-version-restore-dialog[open]").wait_for()
            self.assertIn("還原時會重新驗證來源影片與素材；若已變更，將停止還原。", page.locator("#project-version-restore-status").inner_text())
            page.locator("#project-version-restore-confirm").click()
            self.assertTrue(page.locator("#project-version-restore-cancel").is_disabled())
            self.assertTrue(page.locator("#project-version-restore-dialog[open]").is_visible())
            held_restore[0].fulfill(status=409, json={"ok": False, "error": "source_changed", "error_code": "source_changed"})
            page.wait_for_function("() => document.querySelector('#project-version-restore-status').textContent.includes('來源影片已變更')")
            self.assertEqual(len(restore_attempts), 1)
            self.assertEqual(restore_attempts[0]["expected_state_digest"], "state-now")
            self.assertNotIn("source_changed", page.locator("#project-version-restore-status").inner_text())
            self.assertTrue(page.locator("#project-version-restore-dialog[open]").is_visible())
            page.locator("#project-version-restore-cancel").click()
            self.assertTrue(page.locator("#project-versions-dialog[open]").is_visible())
            page.locator("#project-versions-close").click()
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-versions-open")
            capacity.update({"used": 100, "named_save_available": False, "restore_backup_available": False})
            page.locator("#project-versions-open").click()
            page.locator(".project-version-row").nth(1).click()
            page.locator("#project-version-detail").get_by_text("差異尚未完整顯示").wait_for()
            metadata_only_text = page.locator("#project-version-detail").inner_text()
            self.assertIn("目前顯示 0 項剪輯內容", metadata_only_text)
            self.assertNotIn("沒有需要直接還原的剪輯內容", metadata_only_text)
            page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
            self.assertFalse(page.locator("#project-version-restore-dialog").evaluate("el => el.open"))
            self.assertIn("不會自動清理", page.locator("#project-versions-capacity").inner_text())
            stage("done")
            browser.close()

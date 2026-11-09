from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse

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
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is unavailable")
        evidence_root = Path(os.environ.get("P7_UI_STUB_EVIDENCE_DIR", self.project / "qa/p7-ui-stub"))
        evidence_root.mkdir(parents=True, exist_ok=True)
        self._stub_evidence_dir = evidence_root
        # The fixture's source binding is a real SHA of the generated video,
        # not the old fake-source shortcut used by some browser tests.
        source = self.project / "source/source.mp4"
        rendered = subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "smptebars=s=360x640:d=2", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], text=True, capture_output=True)
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
        held_lists = []
        held_saved_refresh = []
        saved_refresh = {"hold": False}
        list_modes = ["hold", "normal"]
        detail_mode = {"b": "full"}
        variant_reset_mode = {"b": "none"}
        detail_requests = []
        list_requests = []
        list_faults = []
        list_current_overrides = []
        restore_modes = []
        recovery_boot_hold = {"enabled": False}
        held_recovery_boot = []
        capacity = {"used": 2, "max": 100, "named_save_available": True, "restore_backup_available": True}
        versions = [
            {"version_id": "a/b", "name": "A very long version name " * 8, "kind": "named", "created_at": "2026-11-09T01:02:03Z", "state_digest": "state-a", "render_revision": "render-a"},
            {"version_id": "b", "name": "Version B", "kind": "auto_before_restore", "created_at": "2026-11-09T01:03:03Z", "state_digest": "state-b", "render_revision": "render-b"},
        ]
        current = {
            "state_digest": "1" * 64,
            "render_revision": "2" * 64,
            "manifest_digest": "3" * 64,
        }
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.set_default_timeout(8000)
            page.on("console", lambda message: print(f"P7_CONSOLE {message.type}: {message.text}", flush=True))
            page.on("pageerror", lambda error: print(f"P7_PAGEERROR {error}", flush=True))

            def assert_stable_project_versions_focus(label):
                focus = page.evaluate("""() => {
                  const active = document.activeElement;
                  return {
                    id: active?.id || "",
                    connected: Boolean(active?.isConnected),
                    disabled: Boolean(active?.disabled),
                    mainOpen: Boolean(document.querySelector('#project-versions-dialog[open]')),
                    restoreOpen: Boolean(document.querySelector('#project-version-restore-dialog[open]')),
                  };
                }""")
                print(f"P7_FOCUS {label}={focus}", flush=True)
                self.assertTrue(focus["mainOpen"], label)
                self.assertFalse(focus["restoreOpen"], label)
                self.assertTrue(focus["connected"], label)
                self.assertFalse(focus["disabled"], label)
                self.assertIn(focus["id"], {"project-version-name", "project-versions-close"}, label)

            def route(route):
                request = route.request; path = urlparse(request.url).path
                if not path.startswith("/api/project-versions"):
                    route.fallback(); return
                if request.method == "GET" and path == "/api/project-versions":
                    list_requests.append(path)
                    if list_modes:
                        mode = list_modes.pop(0)
                        if mode == "hold":
                            held_lists.append(route); return
                    if saved_refresh["hold"]:
                        saved_refresh["hold"] = False
                        held_saved_refresh.append(route); return
                    if recovery_boot_hold["enabled"]:
                        recovery_boot_hold["enabled"] = False
                        held_recovery_boot.append(route); return
                    if list_faults:
                        route.fulfill(status=list_faults.pop(0), json={"ok": False, "error": "list_unavailable", "error_code": "list_unavailable"}); return
                    listed_current = list_current_overrides.pop(0) if list_current_overrides else current
                    route.fulfill(json={"ok": True, "versions": versions, "current": listed_current, "capacity": capacity}); return
                if request.method == "GET":
                    requested_version_id = unquote(path.rsplit("/", 1)[-1])
                    version = next(item for item in versions if item["version_id"] == requested_version_id)
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
                        {"path": "/rights_assertion/items/0", "kind": "removed", "saved_preview": "null", "current_preview": '{"asserted":true,"asset":"' + "x" * 180 + '"}'},
                        {"path": "/state/rights/asserted", "kind": "changed", "saved_preview": "false", "current_preview": "true"},
                    ]
                    detail_requests.append(version["version_id"])
                    changes = content_changes + reset_changes
                    truncated = False
                    if detail_mode.get(version["version_id"]) == "metadata_only":
                        changes = reset_changes[:1]
                        truncated = True
                    approval_resets = ["timeline"]
                    if variant_reset_mode.get(version["version_id"]) == "present":
                        approval_resets.extend(["timeline_by_variant", "final_by_variant", "unknown_variant_a", "unknown_variant_b"])
                        changes = reset_changes
                        truncated = False
                    payload = {"ok": True, "version": version, "current": current, "diff": {"changed": True, "total_changes": len(changes), "truncated": truncated, "approval_resets": approval_resets, "rights_assertion_reset": detail_mode.get(version["version_id"]) != "metadata_only", "changes": changes}}
                    if version["version_id"] == "a/b":
                        held_details.setdefault("a", (route, payload)); return
                    route.fulfill(json=payload); return
                body = json.loads(request.post_data or "{}")
                posts.append((path, body))
                if path.endswith("/restore"):
                    restore_attempts.append(body)
                    if restore_modes:
                        mode = restore_modes.pop(0)
                        if mode == "refresh_fail":
                            list_faults.append(503)
                        elif mode == "refresh_and_boot_fail":
                            list_faults.extend((503, 503))
                        elif mode == "invalid_current":
                            list_current_overrides.append({"state_digest": "invalid", "manifest_digest": "", "render_revision": "2" * 64})
                        elif mode == "changed_current":
                            list_current_overrides.append({"state_digest": "4" * 64, "manifest_digest": "5" * 64, "render_revision": "2" * 64})
                        route.fulfill(status=409, json={"ok": False, "error": "source_changed", "error_code": "source_changed"})
                        return
                    held_restore.append(route)
                    return
                if body.get("name") == "錯誤版本":
                    route.fulfill(status=422, json={"ok": False, "error": "validation_error", "error_code": "validation_error"})
                    return
                if body.get("name") == "權限版本":
                    route.fulfill(status=503, json={"ok": False, "error": "version_permissions_repair_required", "error_code": "version_permissions_repair_required", "details": {"record_id": "pv_0123456789abcdef0123456789abcdef", "expected_mode": "0600", "current_mode": "0644", "relative_path": "working/project_versions/records/pv_0123456789abcdef0123456789abcdef.json", "repair_action": "restrict_to_owner"}})
                    return
                if body.get("name") == "儲存後清單失敗":
                    saved_after_list_failure = {"version_id": "saved-after-list-failure", "name": "儲存後清單失敗", "kind": "named", "created_at": "2026-11-09T01:05:03Z", "state_digest": "state-saved-after-list-failure", "render_revision": "render-saved-after-list-failure"}
                    versions.insert(0, saved_after_list_failure)
                    capacity.update({"used": 4})
                    saved_refresh["hold"] = True
                    route.fulfill(status=201, json={"ok": True, "version": saved_after_list_failure, "current": current})
                    return
                permanent_errors = {
                    "不支援快照": (409, "unsupported_snapshot"),
                    "遺失版本": (404, "version_not_found"),
                    "不支援存檔": (503, "version_storage_unsupported"),
                    "清單失敗版本": (409, "source_changed"),
                }
                if body.get("name") in permanent_errors:
                    status, code = permanent_errors[body["name"]]
                    if body["name"] == "清單失敗版本":
                        list_faults.append(503)
                    route.fulfill(status=status, json={"ok": False, "error": code, "error_code": code})
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
            page.locator("#project-versions-status").get_by_text("正在讀取版本…").wait_for()
            page.locator("#project-versions-close").click()
            page.locator("#project-versions-open").click()
            page.locator(".project-version-row").first.wait_for()
            page.locator("#project-versions-close").focus()
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-versions-close")
            held_lists[0].fulfill(json={"ok": True, "versions": [{"version_id": "stale", "name": "過期清單", "kind": "named", "created_at": "2026-11-09T00:00:00Z"}], "current": current, "capacity": capacity})
            page.wait_for_timeout(100)
            self.assertNotIn("過期清單", page.locator("#project-versions-list").inner_text())
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-versions-close")
            self.assertEqual(len(list_requests), 2)
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
            self.assertIn("時間軸／素材權利需重新確認", detail_text)
            self.assertNotIn("Invalidated note", detail_text)
            self.assertNotIn("a" * 64, detail_text)
            self.assertLess(detail_text.index("剪輯內容"), detail_text.index("重新確認"))
            self.assertIn("目前顯示 80 項剪輯內容", detail_text)
            self.assertTrue(page.locator(".project-version-row").nth(1).get_attribute("aria-pressed") == "true")
            variant_reset_mode["b"] = "present"
            page.locator(".project-version-row").nth(1).click()
            page.wait_for_function("() => document.querySelector('#project-version-detail').textContent.includes('重新確認提醒')")
            variant_detail_text = page.locator("#project-version-detail").inner_text()
            self.assertIn("另有 5 項重新確認提醒", variant_detail_text)
            self.assertIn("各版型時間軸", variant_detail_text)
            self.assertIn("各版型輸出", variant_detail_text)
            self.assertEqual(variant_detail_text.count("各版型時間軸"), 1)
            self.assertEqual(variant_detail_text.count("各版型輸出"), 1)
            self.assertEqual(variant_detail_text.count("其他核可"), 1)
            print(f"P7_VARIANTS present={variant_detail_text!r}", flush=True)
            variant_reset_mode["b"] = "none"
            page.locator(".project-version-row").nth(1).click()
            page.wait_for_function("() => document.querySelector('#project-version-detail').textContent.includes('素材權利需重新確認')")
            no_variant_detail_text = page.locator("#project-version-detail").inner_text()
            self.assertNotIn("各版型時間軸", no_variant_detail_text)
            self.assertNotIn("各版型輸出", no_variant_detail_text)
            self.assertIn("時間軸／素材權利需重新確認", no_variant_detail_text)
            no_variant_has_labels = any(label in no_variant_detail_text for label in ("各版型時間軸", "各版型輸出"))
            no_variant_keeps_timeline_rights = "時間軸／素材權利需重新確認" in no_variant_detail_text
            print(f"P7_VARIANTS none={{'has_variant_labels': {no_variant_has_labels}, 'keeps_timeline_rights': {no_variant_keeps_timeline_rights}}}", flush=True)
            stage("detail")
            page.locator("#project-version-name").focus()
            page.keyboard.type("版本 A")
            self.assertEqual(page.locator("#project-version-name").input_value(), "版本 A")
            self.assertTrue(page.evaluate("""() => {
              const event = new KeyboardEvent('keydown', {key: 's', ctrlKey: true, bubbles: true, cancelable: true});
              document.querySelector('#project-version-name').dispatchEvent(event);
              return event.defaultPrevented;
            }"""))
            self.assertFalse(page.evaluate("""() => {
              const event = new KeyboardEvent('keydown', {key: 'z', ctrlKey: true, bubbles: true, cancelable: true});
              document.querySelector('#project-version-name').dispatchEvent(event);
              return event.defaultPrevented;
            }"""))
            whitespace_posts = len(posts)
            page.locator("#project-version-name").fill("   ")
            page.locator("#project-version-save").click()
            page.wait_for_timeout(100)
            whitespace_status = page.locator("#project-versions-status")
            self.assertIn("請輸入版本名稱", whitespace_status.inner_text())
            self.assertTrue(whitespace_status.evaluate("el => el.classList.contains('is-error')"))
            self.assertEqual(len(posts), whitespace_posts)
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-version-name")
            print(f"P7_EMPTY_NAME status={whitespace_status.inner_text()!r} posts={len(posts)} focus={page.evaluate('() => document.activeElement.id')!r}", flush=True)
            page.locator("#project-version-name").fill("錯誤版本")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('版本名稱規則')")
            self.assertNotIn("validation_error", page.locator("#project-versions-status").inner_text())
            page.locator("#project-version-name").fill("權限版本")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('分享權限已變更')")
            permission_text = page.locator("#project-versions-status").inner_text()
            self.assertIn("working/project_versions/records", permission_text)
            self.assertIn("請勿刪除版本", permission_text)
            self.assertNotIn("/Users/", permission_text)
            for name, expected in (
                ("不支援快照", "無法安全還原"),
                ("遺失版本", "找不到這個版本"),
                ("不支援存檔", "不支援必要的安全版本存檔"),
            ):
                before_posts = len(posts)
                before_lists = len(list_requests)
                page.locator("#project-version-name").fill(name)
                page.locator("#project-version-save").click()
                page.wait_for_function(f"() => document.querySelector('#project-versions-status').textContent.includes('{expected}')")
                message = page.locator("#project-versions-status").inner_text()
                self.assertIn(expected, message)
                self.assertNotIn("稍後", message)
                self.assertEqual(len(posts), before_posts + 1)
                self.assertEqual(len(list_requests), before_lists + 1)
                self.assertFalse(page.locator("#project-version-save").is_disabled())
                assert_stable_project_versions_focus(f"failed_save_{name}")
            posts_before_named_saves = len(posts)
            page.locator("#project-version-name").fill("版本 A")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('已儲存')")
            self.assertFalse(page.locator("#project-version-save").is_disabled())
            assert_stable_project_versions_focus("success_save")
            save = next(body for path, body in posts if path == "/api/project-versions")
            self.assertEqual(save["expected_manifest_digest"], current["manifest_digest"])
            page.locator("#project-version-name").fill("版本 B")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('已儲存')")
            self.assertEqual(len(posts), posts_before_named_saves + 2)
            page.locator(".project-version-row").nth(1).click()
            page.locator("#project-version-detail").get_by_text("b-saved").wait_for()
            page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
            page.locator("#project-version-restore-dialog[open]").wait_for()
            self.assertIn("還原時會重新驗證來源影片與素材；若已變更，將停止還原。", page.locator("#project-version-restore-status").inner_text())
            page.locator("#project-version-restore-confirm").click()
            self.assertTrue(page.locator("#project-version-restore-cancel").is_disabled())
            self.assertTrue(page.locator("#project-version-restore-dialog[open]").is_visible())
            held_restore[0].fulfill(status=409, json={"ok": False, "error": "source_changed", "error_code": "source_changed"})
            versions.append({"version_id": "backup-b", "name": "還原前自動備份", "kind": "auto_before_restore", "created_at": "2026-11-09T01:04:03Z"})
            capacity.update({"used": 3})
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('來源影片已變更')")
            self.assertEqual(len(restore_attempts), 1)
            self.assertEqual(restore_attempts[0]["expected_state_digest"], current["state_digest"])
            self.assertFalse(page.locator("#project-version-restore-dialog").evaluate("el => el.open"))
            self.assertTrue(page.locator("#project-versions-dialog[open]").is_visible())
            self.assertIn("還原前自動備份", page.locator("#project-versions-list").inner_text())
            self.assertIn("已使用 3/100", page.locator("#project-versions-capacity").inner_text())
            self.assertTrue(page.locator("#project-version-detail").is_hidden())
            assert_stable_project_versions_focus("failed_restore")
            self.assertEqual(len(restore_attempts), 1)
            page.locator(".project-version-row").nth(1).click()
            detail_mode["b"] = "metadata_only"
            page.locator("#project-version-detail").get_by_text("差異尚未完整顯示").wait_for()
            page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
            self.assertFalse(page.locator("#project-version-restore-status").evaluate("el => el.classList.contains('is-error')"))
            page.locator("#project-version-restore-cancel").click()
            page.locator("#project-versions-close").click()
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-versions-open")
            list_faults.append(503)
            page.locator("#project-versions-open").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('無法讀取版本')")
            self.assertEqual(page.locator("#project-versions-list .project-version-row").count(), 0)
            self.assertTrue(page.locator("#project-version-save").is_disabled())
            self.assertEqual(page.locator("#project-versions-capacity").inner_text(), "")
            self.assertTrue(page.locator("#project-version-detail").is_hidden())
            self.assertEqual(page.locator("#project-version-detail").get_by_role("button", name="還原此版本").count(), 0)
            page.locator("#project-versions-close").click()
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "project-versions-open")
            capacity.update({"used": 100, "named_save_available": False, "restore_backup_available": False})
            page.locator("#project-versions-open").click()
            page.locator(".project-version-row").nth(1).click()
            page.locator("#project-version-detail").get_by_text("差異尚未完整顯示").wait_for()
            metadata_only_text = page.locator("#project-version-detail").inner_text()
            self.assertIn("目前顯示 0 項剪輯內容", metadata_only_text)
            self.assertNotIn("沒有需要直接還原的剪輯內容", metadata_only_text)
            self.assertNotIn("素材權利需重新確認", metadata_only_text)
            page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
            self.assertFalse(page.locator("#project-version-restore-dialog").evaluate("el => el.open"))
            self.assertIn("不會自動清理", page.locator("#project-versions-capacity").inner_text())
            capacity.update({"used": 3, "named_save_available": True, "restore_backup_available": True})
            page.locator("#project-versions-close").click()
            page.locator("#project-versions-open").click()
            page.locator(".project-version-row").first.wait_for()
            page.locator(".project-version-row").nth(1).click()
            page.locator("#project-version-detail").get_by_text("差異尚未完整顯示").wait_for()
            before_posts = len(posts)
            before_lists = len(list_requests)
            page.locator("#project-version-name").fill("儲存後清單失敗")
            page.locator("#project-version-save").click()
            page.wait_for_timeout(100)
            self.assertEqual(len(held_saved_refresh), 1)
            pending_refresh_state = page.evaluate("""() => ({
              rows: document.querySelectorAll('#project-versions-list .project-version-row').length,
              capacity: document.querySelector('#project-versions-capacity').textContent,
              detailHidden: document.querySelector('#project-version-detail').hidden,
              restoreActions: document.querySelectorAll('#project-version-detail button').length,
              saveDisabled: document.querySelector('#project-version-save').disabled,
            })""")
            print(f"P7_PENDING saved_then_list_refresh={pending_refresh_state}", flush=True)
            self.assertEqual(pending_refresh_state["rows"], 0)
            self.assertEqual(pending_refresh_state["capacity"], "")
            self.assertTrue(pending_refresh_state["detailHidden"])
            self.assertEqual(pending_refresh_state["restoreActions"], 0)
            self.assertTrue(pending_refresh_state["saveDisabled"])
            held_saved_refresh[0].fulfill(status=503, json={"ok": False, "error": "list_unavailable", "error_code": "list_unavailable"})
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('版本已儲存')")
            status_after_saved_list_failure = page.locator("#project-versions-status").inner_text()
            self.assertIn("版本已儲存，但無法更新版本清單", status_after_saved_list_failure)
            self.assertTrue(page.locator("#project-versions-status").evaluate("el => el.classList.contains('is-error')"))
            self.assertEqual(len(posts), before_posts + 1)
            self.assertEqual(len(list_requests), before_lists + 1)
            self.assertNotIn("儲存後清單失敗", page.locator("#project-versions-list").inner_text())
            self.assertTrue(page.locator("#project-version-save").is_disabled())
            self.assertTrue(page.locator("#project-version-detail").is_hidden())
            self.assertEqual(page.locator("#project-version-detail").get_by_role("button", name="還原此版本").count(), 0)
            assert_stable_project_versions_focus("saved_then_list_failure")
            page.locator("#project-versions-close").click()
            page.locator("#project-versions-open").click()
            page.get_by_role("button", name="儲存後清單失敗").wait_for()
            self.assertIn("儲存後清單失敗", page.locator("#project-versions-list").inner_text())

            before_posts = len(posts)
            before_lists = len(list_requests)
            page.locator("#project-version-name").fill("清單失敗版本")
            page.locator("#project-version-save").click()
            page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('無法更新版本清單')")
            self.assertEqual(len(posts), before_posts + 1)
            self.assertEqual(len(list_requests), before_lists + 1)
            self.assertTrue(page.locator("#project-version-save").is_disabled())
            self.assertEqual(page.locator("#project-versions-list .project-version-row").count(), 0)
            self.assertTrue(page.locator("#project-version-detail").is_hidden())
            self.assertEqual(page.locator("#project-version-detail").get_by_role("button", name="還原此版本").count(), 0)
            assert_stable_project_versions_focus("mutation_list_failure")

            def prepare_uncertain_restore():
                if page.locator("#project-versions-dialog").evaluate("el => el.open"):
                    page.locator("#project-versions-close").click()
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("() => document.querySelector('#project-name')?.textContent !== '讀取專案中'")
                page.locator("#project-versions-open").click()
                version_b = page.locator(".project-version-row").filter(has_text="Version B").first
                version_b.wait_for()
                version_b.click()
                page.locator("#project-version-detail").get_by_role("button", name="還原此版本").wait_for()
                page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
                page.locator("#project-version-restore-dialog[open]").wait_for()

            def assert_uncertain_restore_reloads(mode, *, storage_available=True, boot_list_failure=False):
                prepare_uncertain_restore()
                if not storage_available:
                    page.evaluate("""() => {
                      const originalSetItem = Storage.prototype.setItem;
                      Storage.prototype.setItem = function (key, value) {
                        Storage.prototype.setItem = originalSetItem;
                        throw new DOMException('synthetic storage unavailable', 'QuotaExceededError');
                      };
                    }""")
                before_restore_posts = len(restore_attempts)
                restore_modes.append(mode)
                with page.expect_navigation(wait_until="domcontentloaded"):
                    page.locator("#project-version-restore-confirm").click()
                page.wait_for_function("() => document.querySelector('#project-name')?.textContent !== '讀取專案中'")
                self.assertEqual(len(restore_attempts), before_restore_posts + 1)
                if storage_available:
                    page.locator("#project-versions-dialog[open]").wait_for()
                    page.wait_for_function("() => document.querySelector('#project-versions-status').textContent.includes('還原回應未完整收到，已重新載入目前專案，請確認內容後再繼續。')")
                    self.assertIn(
                        "還原回應未完整收到，已重新載入目前專案，請確認內容後再繼續。",
                        page.locator("#project-versions-status").inner_text(),
                    )
                    if boot_list_failure:
                        self.assertIn(
                            "版本清單目前無法讀取，請稍後關閉後重新開啟。",
                            page.locator("#project-versions-status").inner_text(),
                        )
                    page.locator("#project-versions-close").click()
                else:
                    self.assertFalse(page.locator("#project-versions-dialog").evaluate("el => el.open"))

            assert_uncertain_restore_reloads("refresh_fail")
            # The message is one-shot: an ordinary reload must not reopen it.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelector('#project-name')?.textContent !== '讀取專案中'")
            self.assertFalse(page.locator("#project-versions-dialog").evaluate("el => el.open"))
            assert_uncertain_restore_reloads("invalid_current")
            assert_uncertain_restore_reloads("refresh_and_boot_fail", boot_list_failure=True)
            assert_uncertain_restore_reloads("changed_current", storage_available=False)

            # Reproduce a late recovery-list response crossing a close/reopen
            # boundary. It must not overwrite the newer dialog's status.
            page.evaluate("""projectId => {
              const key = `auto-edit:project-version-restore-recovery:v1:${encodeURIComponent(location.pathname)}:${encodeURIComponent(projectId)}`;
              sessionStorage.setItem(key, JSON.stringify({expires_at: Date.now() + 120000}));
            }""", "browser-smoke")
            recovery_boot_hold["enabled"] = True
            page.reload(wait_until="domcontentloaded")
            page.locator("#project-versions-dialog[open]").wait_for()
            self.assertEqual(len(held_recovery_boot), 1)
            page.locator("#project-versions-close").click()
            page.locator("#project-versions-open").click()
            page.locator(".project-version-row").first.wait_for()
            self.assertEqual(page.locator("#project-versions-status").inner_text(), "")
            held_recovery_boot[0].fulfill(
                status=503,
                json={"ok": False, "error": "late_recovery_list_failure", "error_code": "late_recovery_list_failure"},
            )
            page.wait_for_timeout(100)
            self.assertNotIn(
                "還原回應未完整收到",
                page.locator("#project-versions-status").inner_text(),
            )
            stage("done")
            browser.close()

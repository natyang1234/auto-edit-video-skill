from __future__ import annotations

import copy
import json
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse


SKILL_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_TESTS))

import test_editor_browser as _editor_browser  # noqa: E402

CHROME = _editor_browser.CHROME
sync_playwright = _editor_browser.sync_playwright


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class FxPreviewBrowserTests(unittest.TestCase):
    """Browser contract for the local-draft FX preview dialog."""

    # Reuse the existing smoke harness' synthetic media/temp project without
    # inheriting its other test methods.
    setUp = _editor_browser.EditorBrowserSmokeTests.setUp
    write_json = _editor_browser.EditorBrowserSmokeTests.write_json
    stop_server = _editor_browser.EditorBrowserSmokeTests.stop_server

    def open_preview(self, page):
        page.locator('[data-add-type="fx"]').click()
        page.locator("#fx-preview-dialog[open]").wait_for()

    def mock_preview_api(self, page, delay_first_post: bool = False):
        project = page.evaluate(
            """async () => {
                const response = await fetch('/api/project');
                return await response.json();
            }"""
        )
        server_state = copy.deepcopy(project["state"])
        jobs: dict[str, dict] = {}
        posted: list[dict] = []
        cancelled: list[str] = []
        force_failed = [False]
        counter = 0

        def route_handler(route):
            nonlocal counter, server_state
            request = route.request
            path = urlparse(request.url).path
            if not path.startswith("/api/fx-previews"):
                route.fallback()
                return
            if path == "/api/fx-previews" and request.method == "POST":
                body = json.loads(request.post_data or "{}")
                counter += 1
                job_id = f"browser-fx-{counter}"
                job = {
                    "id": job_id,
                    "state": "failed" if force_failed[0] else "ready",
                    "phase": "rendering",
                    "message": "測試預覽失敗" if force_failed[0] else "測試影片預覽就緒",
                    "status_url": f"/api/fx-previews/{job_id}",
                    "cache_hit": False,
                }
                if force_failed[0]:
                    job["error"] = {"code": "test_failed", "message": "測試失敗"}
                else:
                    job["media_url"] = "/media/source"
                jobs[job_id] = job
                posted.append(body)
                if delay_first_post and counter == 1:
                    time.sleep(0.7)
                route.fulfill(json={"ok": True, "job": job, "state_revision": body["expected_revision"]})
                return
            parts = [part for part in path.split("/") if part]
            job_id = parts[2] if len(parts) > 2 else ""
            if request.method == "GET" and job_id in jobs:
                route.fulfill(json={"ok": True, "job": jobs[job_id], "state_revision": server_state.get("revision")})
                return
            if request.method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                job = jobs.get(job_id, {"id": job_id})
                job["state"] = "cancelled"
                cancelled.append(job_id)
                route.fulfill(json={"ok": True, "job": job, "state_revision": server_state.get("revision")})
                return
            if request.method == "POST" and len(parts) == 4 and parts[3] == "confirm":
                candidate = posted[-1]["candidate"]
                replace_id = posted[-1].get("replace_overlay_id")
                overlay_id = replace_id or f"fx-browser-{counter}"
                overlay = {
                    "id": overlay_id,
                    "type": "fx",
                    "start": candidate["window"]["start_s"],
                    "end": candidate["window"]["start_s"] + candidate["window"]["duration_s"],
                    "text": "",
                    "emphasis": [],
                    "visible": True,
                    "locked": False,
                    "z_index": 25,
                    "style": {"x": 50, "y": 50, "width": 100, "animation": "none", "opacity": 1},
                    "code": candidate["code"],
                    "values": candidate.get("values", {}),
                    "placement": candidate.get("placement", ""),
                    "scheme": candidate.get("scheme", ""),
                    "font_ids": candidate.get("font_ids", {}),
                    "provenance": "browser preview test",
                    "highlight_id": None,
                }
                server_state["overlays"] = [
                    item for item in server_state.get("overlays", []) if item.get("id") != overlay_id
                ] + [overlay]
                server_state["revision"] = "d" * 64
                route.fulfill(json={
                    "ok": True,
                    "state": server_state,
                    "revision": server_state["revision"],
                    "overlay_id": overlay_id,
                })
                return
            route.fallback()

        page.route("**/api/fx-previews**", route_handler)
        return posted, cancelled, force_failed

    def test_timing_status_text(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page()
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            running = page.evaluate("() => fxPreviewStatusForJob({state:'running', phase:'encoding', elapsed_s:7, eta_remaining_s:null})")
            estimated = page.evaluate("() => fxPreviewStatusForJob({state:'running', phase:'encoding', elapsed_s:8, eta_remaining_s:12})")
            ready = page.evaluate("() => fxPreviewStatusForJob({state:'ready', phase:'ready', elapsed_s:20, eta_remaining_s:null})")
            queued = page.evaluate("() => fxPreviewStatusForJob({state:'running', phase:'queued'})")
            cancelling = page.evaluate("() => fxPreviewStatusForJob({state:'running', phase:'cancelling'})")
            self.assertIn("排隊中", queued)
            self.assertIn("取消中", cancelling)
            self.assertIn("已用 7 秒", running)
            self.assertIn("預估時間計算中", running)
            self.assertIn("預估還需 12 秒", estimated)
            self.assertIn("依近期同設定估算", estimated)
            self.assertIn("已用 20 秒", ready)
            self.assertNotIn("計算中", ready)
            page.evaluate("""() => { elements['fx-preview-dialog'].showModal();
                fxPreviewGeneration = 123; fxPreviewDraftSignature = 'test'; fxPreviewUpgradeStarted = true; }""")
            for job, expected in [
                ({"state": "running", "phase": "encoding", "elapsed_s": 20, "eta_remaining_s": None,
                  "eta_sample_count": 3, "preview_options": {"quality": "standard"}}, "已超過近期預估，仍在處理"),
                ({"state": "running", "phase": "encoding", "elapsed_s": 7, "eta_remaining_s": None,
                  "preview_options": {"quality": "standard"}}, "預估時間計算中"),
                ({"state": "ready", "phase": "ready", "elapsed_s": 20, "media_url": "/media/source",
                  "preview_options": {"quality": "draft"}}, "快速版耗時 20 秒"),
                ({"state": "failed", "phase": "failed", "elapsed_s": 20,
                  "preview_options": {"quality": "standard"}}, "清晰版耗時 20 秒"),
                ({"state": "failed", "phase": "failed", "preview_options": {"quality": "standard"}}, "清晰版預覽失敗"),
            ]:
                page.evaluate('(job) => fxPreviewApplyJob(job, 123, "test")', job)
                self.assertIn(expected, page.locator("#fx-preview-status").inner_text())
            browser.close()

    def test_catalog_blocked_b1_preview_lifecycle_and_confirm(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.wait_for_timeout(300)
            posted, _cancelled, force_failed = self.mock_preview_api(page)

            trigger = page.locator('[data-add-type="fx"]')
            trigger.click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            self.assertEqual(page.locator("#fx-preview-grid .fx-preview-card").count(), 168)
            self.assertIn("卡片為風格示意", page.locator(".fx-preview-catalog-note").inner_text())
            preview_count_before_b1 = len(posted)
            page.locator("#fx-preview-search").fill("B1")
            self.assertEqual(page.locator("#fx-preview-grid .fx-preview-card").count(), 1)
            page.locator("#fx-preview-grid .fx-preview-card").click()
            self.assertIn("B1 需要B-roll素材，請先至素材區處理", page.locator("#fx-preview-status").inner_text())
            self.assertEqual(len(posted), preview_count_before_b1)
            page.keyboard.press("Escape")
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            self.assertTrue(trigger.evaluate("el => el === document.activeElement"))

            self.open_preview(page)
            page.wait_for_timeout(500)
            self.assertGreaterEqual(len(posted), 1)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            self.assertFalse(page.locator("#fx-preview-retry").is_visible())
            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")

            force_failed[0] = True
            self.open_preview(page)
            page.locator("#fx-preview-status").filter(has_text="預覽失敗").wait_for()
            self.assertTrue(page.locator("#fx-preview-retry").is_visible())
            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")

            force_failed[0] = False
            self.open_preview(page)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            page.locator("#fx-preview-confirm").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            self.assertIsNone(posted[-1].get("replace_overlay_id"))
            self.assertGreater(page.locator(".layer-row").count(), 0)
            browser.close()

    def test_candidate_change_invalidates_confirm_and_responsive_modal_has_no_overflow(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.wait_for_timeout(300)
            self.mock_preview_api(page)
            self.open_preview(page)
            bounds = page.locator("#fx-preview-dialog").bounding_box()
            confirm_bounds = page.locator("#fx-preview-confirm").bounding_box()
            self.assertIsNotNone(bounds)
            self.assertIsNotNone(confirm_bounds)
            self.assertLessEqual(confirm_bounds["y"] + confirm_bounds["height"], bounds["y"] + bounds["height"] + 1)
            page.screenshot(path="/Volumes/ORICO/Projects/codex-studio-fx-preview-20261107/fx-preview-1440.png", full_page=True)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            text_input = page.locator("#fx-preview-fields input[type='text']").first
            if text_input.count():
                text_input.fill("新的本片文字")
                self.assertTrue(page.locator("#fx-preview-confirm").is_disabled())
                page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            for width in (1440, 1100, 390):
                page.set_viewport_size({"width": width, "height": 1100})
                self.open_preview(page)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                page.locator("#fx-preview-cancel").click()
                page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            browser.close()

    def test_stale_post_response_is_cancelled_after_draft_changes(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.wait_for_timeout(300)
            posted, cancelled, _force_failed = self.mock_preview_api(page, delay_first_post=True)
            self.open_preview(page)
            page.evaluate(
                """setTimeout(() => {
                    const input = document.querySelector('#fx-preview-fields input[type="text"]');
                    if (input) {
                        input.value = 'stale draft';
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                }, 600)"""
            )
            page.wait_for_timeout(1400)
            self.assertGreaterEqual(len(posted), 1)
            self.assertGreaterEqual(len(cancelled), 1)
            self.assertGreaterEqual(len(posted), 2)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            self.assertIn("/media/source", page.locator("#fx-preview-video").get_attribute("src") or "")
            browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

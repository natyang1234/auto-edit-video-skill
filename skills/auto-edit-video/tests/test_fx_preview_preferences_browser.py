from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse


SKILL_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_TESTS))

import test_editor_browser as _editor_browser  # noqa: E402
from test_fx_preview_browser import FxPreviewBrowserTests  # noqa: E402


CHROME = _editor_browser.CHROME
sync_playwright = _editor_browser.sync_playwright


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class FxPreviewPreferencesBrowserTests(unittest.TestCase):
    """Exercise persisted FX preferences and source/effect time mapping in Chrome."""

    setUp = _editor_browser.EditorBrowserSmokeTests.setUp
    write_json = _editor_browser.EditorBrowserSmokeTests.write_json
    stop_server = _editor_browser.EditorBrowserSmokeTests.stop_server

    def open_preview(self, page):
        page.locator('[data-add-type="fx"]').click()
        page.locator("#fx-preview-dialog[open]").wait_for()

    def mock_upgrade_preview_api(self, page, layout_mode="warning"):
        """Return a draft map first, then control the standard-upgrade state."""
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
        upgrade_state = ["pending"]
        layout_state = [layout_mode]
        counter = 0
        draft_map = [
            {"output_start_s": 0.0, "output_end_s": 1.0, "source_start_s": 0.0, "source_end_s": 1.0},
            {"output_start_s": 1.0, "output_end_s": 2.0, "source_start_s": 3.0, "source_end_s": 4.0},
        ]
        standard_map = [
            {"output_start_s": 0.0, "output_end_s": 1.0, "source_start_s": 10.0, "source_end_s": 11.0},
            {"output_start_s": 1.0, "output_end_s": 1.75, "source_start_s": 13.0, "source_end_s": 13.75},
        ]
        warning_layout = {
            "version": 2,
            "measurement": "composition-dom",
            "status": "warn",
            "analysis_scope": "single",
            "face_measurement": "source-yunet",
            "samples": [{"time": 0.25, "boxes": [{"id": "text-1", "x": 0.5, "y": 0.5}]}],
            "findings": (
                [{"kind": "safe_area"}] * 3
                + [{"kind": "text_overflow"}] * 3
                + [{"kind": "subject_overlap"}] * 3
                + [{"kind": "face_overlap"}] * 2
                + [{"kind": "text_too_small"}] * 2
            ),
        }
        pass_layout = {
            "version": 2,
            "measurement": "composition-dom",
            "status": "pass",
            "analysis_scope": "full",
            "face_measurement": "source-yunet",
            "samples": [{"time": 0.25, "boxes": []}],
            "findings": [],
        }

        def layout_for(quality):
            if layout_state[0] == "missing":
                return None
            return pass_layout if quality == "standard" else warning_layout

        def route_handler(route):
            nonlocal counter
            request = route.request
            path = urlparse(request.url).path
            if not path.startswith("/api/fx-previews"):
                route.fallback()
                return
            if path == "/api/fx-previews" and request.method == "POST":
                body = json.loads(request.post_data or "{}")
                counter += 1
                job_id = f"browser-fx-upgrade-{counter}"
                quality = body.get("preview_options", {}).get("quality", "draft")
                state_name = "ready" if quality == "draft" else upgrade_state[0]
                job = {
                    "id": job_id,
                    "state": state_name,
                    "phase": "rendering",
                    "message": "測試預覽失敗" if state_name == "failed" else "測試影片預覽就緒",
                    "status_url": f"/api/fx-previews/{job_id}",
                    "cache_hit": False,
                    "preview_options": dict(body.get("preview_options") or {}),
                }
                if state_name == "failed":
                    job["error"] = {"code": "test_failed", "message": "測試失敗"}
                elif state_name == "ready":
                    job.update({
                        "media_url": "/media/source?fx-preview=standard" if quality == "standard" else "/media/source?fx-preview=draft",
                        "render_duration_s": 1.75 if quality == "standard" else 2.0,
                        "time_map": standard_map if quality == "standard" else draft_map,
                    })
                    text_layout = layout_for(quality)
                    if text_layout is not None:
                        job["text_layout"] = copy.deepcopy(text_layout)
                jobs[job_id] = job
                posted.append(body)
                route.fulfill(json={"ok": True, "job": job, "state_revision": body["expected_revision"]})
                return
            parts = [part for part in path.split("/") if part]
            job_id = parts[2] if len(parts) > 2 else ""
            if request.method == "GET" and job_id in jobs:
                job = jobs[job_id]
                if job["preview_options"].get("quality") == "standard" and job["state"] == "pending" and upgrade_state[0] == "failed":
                    job["state"] = "failed"
                    job["message"] = "測試預覽失敗"
                    job["error"] = {"code": "test_failed", "message": "測試失敗"}
                route.fulfill(json={"ok": True, "job": job, "state_revision": server_state.get("revision")})
                return
            if request.method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                job = jobs.get(job_id, {"id": job_id})
                job["state"] = "cancelled"
                cancelled.append(job_id)
                route.fulfill(json={"ok": True, "job": job, "state_revision": server_state.get("revision")})
                return
            route.fallback()

        page.route("**/api/fx-previews**", route_handler)
        return posted, cancelled, upgrade_state, layout_state

    def test_favorites_draft_restore_and_real_media_time_mapping(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            FxPreviewBrowserTests.mock_preview_api(self, page)

            self.open_preview(page)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            first_code = page.locator("#fx-preview-grid .fx-preview-card").first.get_attribute("data-fx-code")
            self.assertTrue(first_code)
            page.locator("#fx-preview-grid .fx-preview-favorite").first.click()
            favorite_box = page.locator("#fx-preview-grid .fx-preview-favorite").first.bounding_box()
            self.assertIsNotNone(favorite_box)
            self.assertGreaterEqual(favorite_box["width"], 44)
            self.assertGreaterEqual(favorite_box["height"], 44)
            page.get_by_role("tab", name="我的收藏").click()
            self.assertEqual(page.locator("#fx-preview-grid .fx-preview-card").count(), 1)
            self.assertEqual(
                page.locator("#fx-preview-grid .fx-preview-card").first.get_attribute("data-fx-code"), first_code
            )

            page.get_by_role("tab", name="全部").click()
            page.locator(".fx-preview-window > summary").click()
            source_input = page.locator("#fx-preview-source")
            source_input.fill("0.5")
            source_input.dispatch_event("input")
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            page.wait_for_function("() => document.querySelector('#fx-preview-source-video').readyState >= 1")
            page.evaluate(
                """() => {
                    const effect = document.querySelector('#fx-preview-video');
                    effect.currentTime = 0.25;
                    effect.dispatchEvent(new Event('seeking'));
                }"""
            )
            page.wait_for_function(
                "() => Math.abs(document.querySelector('#fx-preview-source-video').currentTime - 0.75) < 0.12"
            )
            # This is the real fixture MP4 served by the editor, not merely a DOM assertion.
            self.assertGreater(page.locator("#fx-preview-source-video").evaluate("video => video.duration"), 1.5)

            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            self.open_preview(page)
            self.assertIn("已還原未套用候選草稿", page.locator("#fx-preview-draft-copy").inner_text())
            self.assertEqual(page.locator("#fx-preview-source").input_value(), "0.5")
            draft_action = page.locator("#fx-preview-draft-notice .button:not([hidden])").first
            draft_action_box = draft_action.bounding_box()
            self.assertIsNotNone(draft_action_box)
            self.assertGreaterEqual(draft_action_box["width"], 44)
            self.assertGreaterEqual(draft_action_box["height"], 44)

            # A stored draft from another revision must be explicit, never silently restored.
            page.locator("#fx-preview-cancel").click()
            page.evaluate(
                """() => {
                    const key = 'auto-edit:fx-preview-drafts:v1';
                    const drafts = JSON.parse(localStorage.getItem(key));
                    const draft = Object.values(drafts)[0];
                    draft.revision = 'older-revision';
                    localStorage.setItem(key, JSON.stringify(drafts));
                }"""
            )
            page.reload(wait_until="networkidle")
            self.open_preview(page)
            self.assertIn("不會自動還原或套用", page.locator("#fx-preview-draft-copy").inner_text())
            self.assertTrue(page.locator("#fx-preview-restore-draft").is_visible())
            browser.close()

    def test_favorite_keyboard_does_not_select_parent_card(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            FxPreviewBrowserTests.mock_preview_api(self, page)
            self.open_preview(page)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            selected_before = page.evaluate("() => fxPreviewDraft.code")
            second_card = page.locator("#fx-preview-grid .fx-preview-card").nth(1)
            second_code = second_card.get_attribute("data-fx-code")
            self.assertNotEqual(selected_before, second_code)

            favorite = page.locator(f"[data-fx-code='{second_code}'] .fx-preview-favorite")
            for key, expected_favorite in (("Space", True), ("Enter", False)):
                favorite.focus()
                page.keyboard.press(key)
                page.wait_for_timeout(100)
                selected_code = page.evaluate("() => fxPreviewDraft.code")
                preferences = page.evaluate(
                    """() => JSON.parse(localStorage.getItem('auto-edit:fx-preview-preferences:v1') || '{}')"""
                )
                self.assertEqual(selected_code, selected_before)
                self.assertEqual(second_code in (preferences.get("favorites") or []), expected_favorite)
            browser.close()

    def test_standard_upgrade_keeps_visible_draft_map_until_ready(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, cancelled, upgrade_state, layout_state = self.mock_upgrade_preview_api(page)

            def clear_draft_and_close() -> None:
                page.locator("#fx-preview-cancel").click()
                page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
                page.evaluate("() => localStorage.clear()")

            # Pending standard upgrade: the ready draft remains the source of
            # truth for the media that is still displayed.
            self.open_preview(page)
            page.locator("#fx-preview-cancel-upgrade").wait_for(state="visible")
            self.assertAlmostEqual(page.evaluate("() => fxPreviewSourceTimeForEffectTime(1.5)"), 3.5)
            self.assertEqual(page.evaluate("() => fxPreviewVisibleMeta.render_duration_s"), 2)
            self.assertTrue(page.locator("#fx-preview-layout-check").is_visible())
            self.assertIn("有 4 項提醒", page.locator("#fx-preview-layout-check-summary").inner_text())
            detail_toggle = page.locator("#fx-preview-layout-check-toggle")
            detail_box = detail_toggle.bounding_box()
            self.assertIsNotNone(detail_box)
            self.assertGreaterEqual(detail_box["width"], 44)
            self.assertGreaterEqual(detail_box["height"], 44)
            detail_toggle.click()
            detail_items = page.locator("#fx-preview-layout-check-list li")
            self.assertLessEqual(detail_items.count(), 10)
            detail_text = detail_items.all_inner_texts()
            self.assertEqual(len(detail_text), len(set(detail_text)))
            self.assertIn("文字接近安全區邊緣", detail_text)
            self.assertIn("文字超出可用範圍", detail_text)
            self.assertNotIn("safe_area", " ".join(detail_text))
            self.assertEqual(posted[-1]["preview_options"]["quality"], "standard")
            clear_draft_and_close()

            # A later standard failure must not discard the draft time map.
            upgrade_state[0] = "pending"
            self.open_preview(page)
            page.locator("#fx-preview-cancel-upgrade").wait_for(state="visible")
            upgrade_state[0] = "failed"
            page.locator("#fx-preview-status").filter(has_text="清晰版預覽失敗").wait_for()
            self.assertAlmostEqual(page.evaluate("() => fxPreviewSourceTimeForEffectTime(1.5)"), 3.5)
            self.assertEqual(page.evaluate("() => fxPreviewVisibleMeta.render_duration_s"), 2)
            clear_draft_and_close()

            # Explicit cancellation also leaves the draft metadata attached to
            # the media element instead of falling back to the current job.
            upgrade_state[0] = "pending"
            self.open_preview(page)
            page.locator("#fx-preview-cancel-upgrade").wait_for(state="visible")
            page.locator("#fx-preview-cancel-upgrade").click()
            page.locator("#fx-preview-status").filter(has_text="已取消清晰版").wait_for()
            self.assertAlmostEqual(page.evaluate("() => fxPreviewSourceTimeForEffectTime(1.5)"), 3.5)
            self.assertEqual(page.evaluate("() => fxPreviewVisibleMeta.render_duration_s"), 2)
            self.assertTrue(cancelled)
            clear_draft_and_close()

            # Only a ready standard job can replace both the media and its map.
            upgrade_state[0] = "ready"
            self.open_preview(page)
            page.locator("#fx-preview-confirm:not([disabled])").wait_for()
            self.assertAlmostEqual(page.evaluate("() => fxPreviewSourceTimeForEffectTime(1.5)"), 13.5)
            self.assertEqual(page.evaluate("() => fxPreviewVisibleMeta.render_duration_s"), 1.75)
            self.assertIn("畫面檢查：通過", page.locator("#fx-preview-layout-check-summary").inner_text())
            self.assertFalse(page.locator("#fx-preview-layout-check-toggle").is_visible())
            self.assertIn("fx-preview=standard", page.locator("#fx-preview-video").get_attribute("src") or "")
            clear_draft_and_close()

            # A missing report is not turned into a fabricated pass state.
            layout_state[0] = "missing"
            upgrade_state[0] = "pending"
            self.open_preview(page)
            page.locator("#fx-preview-cancel-upgrade").wait_for(state="visible")
            self.assertFalse(page.locator("#fx-preview-layout-check").is_visible())
            self.assertNotIn("通過", page.locator("#fx-preview-status").inner_text())
            browser.close()

    def test_request_contract_keeps_eight_second_apply_range_and_b1_asset_opaque(self) -> None:
        """The browser must send separate sample/apply ranges and never a material path."""
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _failed = FxPreviewBrowserTests.mock_preview_api(self, page)
            page.route("**/api/assets/library", lambda route: route.fulfill(json={"assets": [{"asset_id": "asset-test", "sha256": "a" * 64, "path": "assets/test.png", "kind": "image"}]}))
            self.open_preview(page)
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            # The fixture itself is short, but this intercepted request verifies the
            # browser contract independently of renderer availability.
            page.evaluate(
                """() => {
                  const source = document.querySelector('#fx-preview-apply-source');
                  const duration = document.querySelector('#fx-preview-apply-duration');
                  source.value = '0'; source.dispatchEvent(new Event('input', {bubbles:true}));
                  duration.value = '1.8'; duration.dispatchEvent(new Event('input', {bubbles:true}));
                }"""
            )
            page.wait_for_function("() => window.__dummy || !document.querySelector('#fx-preview-confirm').disabled")
            self.assertTrue(posted)
            body = posted[-1]
            self.assertIn("apply_window", body["candidate"])
            self.assertIn("preview_options", body)
            self.assertEqual(body["preview_options"]["mode"], "single")
            self.assertEqual(body["preview_options"]["quality"], "standard")
            self.assertGreaterEqual(body["candidate"]["apply_window"]["duration_s"], body["candidate"]["window"]["duration_s"])
            page.locator("#fx-preview-search").fill("B1")
            page.locator("#fx-preview-grid .fx-preview-card").click()
            page.locator("#fx-preview-fields select").first.wait_for()
            page.locator("#fx-preview-fields select").first.select_option("asset-test")
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            b1_body = posted[-1]
            self.assertEqual(b1_body["candidate"]["material"], {"asset_id": "asset-test", "sha256": "a" * 64})
            self.assertNotIn("path", b1_body["candidate"]["material"])
            browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

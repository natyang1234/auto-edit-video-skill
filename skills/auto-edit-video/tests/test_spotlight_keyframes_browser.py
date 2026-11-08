from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import test_fx_preview_browser as _fx_preview_browser

CHROME = _fx_preview_browser.CHROME
sync_playwright = _fx_preview_browser.sync_playwright


ART = Path(os.environ.get(
    "P6_UI_EVIDENCE_DIR",
    "/Volumes/ORICO/Projects/codex-studio-round2-20261107/evidence/P6-ui-r1",
))


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class SpotlightKeyframesBrowserTests(unittest.TestCase):
    """Focused P6 UI contract checks against the existing browser fixture."""

    setUp = _fx_preview_browser.FxPreviewBrowserTests.setUp
    write_json = _fx_preview_browser.FxPreviewBrowserTests.write_json
    stop_server = _fx_preview_browser.FxPreviewBrowserTests.stop_server
    open_preview = _fx_preview_browser.FxPreviewBrowserTests.open_preview
    mock_preview_api = _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api

    def _open_p5(self, page):
        self.open_preview(page)
        if not page.locator("#fx-preview-search").is_visible():
            page.locator("#fx-preview-catalog-disclosure summary").click()
        page.locator("#fx-preview-search").fill("P5")
        page.locator("#fx-preview-grid .fx-preview-card").first.click()
        page.wait_for_function("() => fxPreviewDraft?.code === 'P5'")
        page.locator("#spotlight-keyframes-panel").wait_for()

    def _wait_for_post(self, posted, predicate, timeout=5.0):
        deadline = __import__("time").monotonic() + timeout
        while __import__("time").monotonic() < deadline:
            if any(predicate(item) for item in posted):
                return
            __import__("time").sleep(0.05)
        self.fail(f"no matching preview request: {json.dumps(posted[-3:], ensure_ascii=False)}")

    def test_manual_path_create_edit_delete_reconcile_and_auto_replacement(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _failed = self.mock_preview_api(page)
            self._open_p5(page)

            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => fxPreviewDraft.spotlight_mode === 'manual'")
            self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 1100)
            self.assertEqual(page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(), 2)

            # Bounds are source-time bounds.  They do not silently retime the
            # existing points; explicit reconciliation is required.
            page.locator("#fx-preview-window-title").click()
            page.locator("#fx-preview-apply-source").fill("0.400")
            page.locator("#fx-preview-apply-source").dispatch_event("input")
            page.locator("#fx-preview-apply-duration").fill("1.000")
            page.locator("#fx-preview-apply-duration").dispatch_event("input")
            self.assertTrue(any(token in page.locator("#spotlight-validation").inner_text() for token in ("端點", "套用範圍")))
            self.assertTrue(page.locator("#fx-preview-confirm").is_disabled())
            page.locator("#spotlight-reconcile-start").click()
            page.locator("#spotlight-reconcile-end").click()
            page.wait_for_function("() => fxPreviewSpotlightPathForDraft(fxPreviewDraft, {allowIncomplete:false}).ok")
            ART.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ART / "p6-ui-1100-manual.png"), full_page=True)

            # Numeric editing works without pointer coordinates.
            page.locator("#spotlight-point-x").fill("0.650000")
            page.locator("#spotlight-point-y").fill("0.350000")
            page.locator("#spotlight-update-point").click()
            page.locator("#spotlight-point-time").fill("0.800")
            page.locator("#spotlight-point-x").fill("0.700000")
            page.locator("#spotlight-point-y").fill("0.300000")
            page.locator("#spotlight-add-point").click()
            self.assertEqual(page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(), 3)
            # Keep a bounded viewport artifact with a selected point and a
            # non-zero path, in addition to the existing full-page capture.
            page.locator("#spotlight-source-stage").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-1100-path-viewport.png"))
            page.locator("#spotlight-point-x").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-1100-panel-viewport.png"))
            page.locator("#spotlight-delete-point").click()
            self.assertEqual(page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(), 2)

            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            self._wait_for_post(posted, lambda body: body.get("candidate", {}).get("code") == "P5"
                                and "spotlight_path" in body.get("candidate", {}))
            manual_body = next(
                body for body in reversed(posted)
                if body.get("candidate", {}).get("code") == "P5"
                and "spotlight_path" in body.get("candidate", {})
            )
            path = manual_body["candidate"]["spotlight_path"]
            self.assertEqual(path["version"], 1)
            self.assertEqual(path["interpolation"], "smoothstep")
            self.assertEqual(len(path["keyframes"]), 2)
            self.assertEqual(path["keyframes"][0]["source_at"], 0.4)
            self.assertEqual(path["keyframes"][-1]["source_at"], 1.4)
            self.assertNotIn("subject", manual_body["candidate"])

            # Explicitly switching back to automatic removes inherited path.
            page.locator("#spotlight-mode-automatic").check()
            page.wait_for_function("() => fxPreviewDraft.spotlight_mode === 'automatic'")
            page.wait_for_timeout(800)
            self._wait_for_post(posted, lambda body: body.get("candidate", {}).get("code") == "P5"
                                and "spotlight_path" not in body.get("candidate", {}))
            self.assertNotIn("spotlight_path", page.evaluate("() => fxPreviewDraft"))
            page.keyboard.press("Escape")
            browser.close()

    def test_source_click_uses_content_rect_and_manual_hold_blocks_sync_callback(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => { const v = document.querySelector('#fx-preview-source-video'); return v && v.readyState >= 1 && v.getBoundingClientRect().width > 0; }")
            self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 1440)
            ART.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ART / "p6-ui-1440-manual.png"), full_page=True)
            page.evaluate("() => { const v = document.querySelector('#fx-preview-source-video'); v.currentTime = 0.7; v.dispatchEvent(new Event('timeupdate')); }")
            before = page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count()
            page.locator("#spotlight-source-click-target").click(position={"x": 116, "y": 150})
            self.assertEqual(page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(), before + 1)
            page.locator("#spotlight-source-stage").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-1440-path-viewport.png"))
            box = page.locator("#fx-preview-source-video").bounding_box()
            self.assertIsNotNone(box)
            # A click in the native control strip must not add a point.
            page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] - 4)
            self.assertEqual(page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(), before + 1)
            page.locator("#spotlight-point-x").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-1440-panel-viewport.png"))
            state = page.evaluate("""() => ({
                held: fxPreviewManualSourceHold,
                paused: document.querySelector('#fx-preview-source-video').paused,
                source: document.querySelector('#fx-preview-source-video').currentTime,
            })""")
            self.assertTrue(state["held"])
            self.assertTrue(state["paused"])
            page.evaluate("() => { const v = document.querySelector('#fx-preview-video'); v.currentTime = 0.1; v.dispatchEvent(new Event('timeupdate')); }")
            self.assertAlmostEqual(
                page.locator("#fx-preview-source-video").evaluate("el => el.currentTime"),
                state["source"],
                delta=0.08,
            )
            page.keyboard.press("Escape")
            browser.close()

    def test_path_validator_canonicalizes_and_rejects_bounds_and_unknown_fields(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            result = page.evaluate("""() => {
                const base = {
                    version: 1, interpolation: 'smoothstep', keyframes: [
                        {source_at: -0.0004, x: -0, y: 0.3200004},
                        {source_at: 2.0004, x: 0.6500004, y: 0.35},
                    ],
                };
                return {
                    canonical: fxPreviewValidateSpotlightPath(base, 0, 2),
                    duplicate: fxPreviewValidateSpotlightPath({ ...base, keyframes: [base.keyframes[0], {...base.keyframes[1], source_at: 0.0004}] }, 0, 2),
                    unknown: fxPreviewValidateSpotlightPath({...base, extra: true}, 0, 2),
                    huge: fxPreviewValidateSpotlightPath({...base, keyframes: [{...base.keyframes[0], source_at: 1e30}, base.keyframes[1]]}, 0, 2),
                    incomplete: fxPreviewValidateSpotlightPath({version: 1, interpolation: 'smoothstep', keyframes: []}, 0, 2, {allowIncomplete:true}),
                    nonP5Draft: fxPreviewBuildDraft(fxEffectByCode('H1'), {spotlight_mode: 'manual', spotlight_path: base}, null, 0),
                };
            }""")
            self.assertTrue(result["canonical"]["ok"])
            self.assertEqual(result["canonical"]["value"]["keyframes"][0], {"source_at": 0, "x": 0, "y": 0.32})
            self.assertFalse(result["duplicate"]["ok"])
            self.assertFalse(result["unknown"]["ok"])
            self.assertFalse(result["huge"]["ok"])
            self.assertTrue(result["incomplete"]["incomplete"])
            self.assertNotIn("spotlight_path", result["nonP5Draft"])
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 390)
            ART.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ART / "p6-ui-390-manual.png"), full_page=True)
            page.screenshot(path=str(ART / "p6-ui-390-validator.png"))
            page.wait_for_function("() => { const v = document.querySelector('#fx-preview-source-video'); return v && v.readyState >= 1 && v.getBoundingClientRect().width > 0; }")
            page.evaluate("() => { const v = document.querySelector('#fx-preview-source-video'); v.currentTime = 0.7; v.dispatchEvent(new Event('timeupdate')); }")
            target_box = page.locator("#spotlight-source-click-target").bounding_box()
            self.assertIsNotNone(target_box)
            page.locator("#spotlight-source-click-target").click(
                position={"x": target_box["width"] * 0.5, "y": target_box["height"] * 0.5}
            )
            page.locator("#spotlight-source-stage").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-390-path-viewport.png"))
            page.locator("#spotlight-point-x").scroll_into_view_if_needed()
            page.screenshot(path=str(ART / "p6-ui-390-panel-viewport.png"))
            browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

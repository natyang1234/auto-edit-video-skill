from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_fx_preview_browser as _fx_preview_browser

CHROME = _fx_preview_browser.CHROME
sync_playwright = _fx_preview_browser.sync_playwright


_EVIDENCE_DIR = os.environ.get("P6_UI_EVIDENCE_DIR")
_DEFAULT_EVIDENCE_TMP = None if _EVIDENCE_DIR else tempfile.TemporaryDirectory(prefix="p6-ui-evidence-")
ART = Path(_EVIDENCE_DIR) if _EVIDENCE_DIR else Path(_DEFAULT_EVIDENCE_TMP.name)


class SpotlightEvidencePathTests(unittest.TestCase):
    def test_evidence_path_defaults_to_temp_and_honors_explicit_directory(self):
        probe = (
            "import test_spotlight_keyframes_browser as m; "
            "print(m.ART); print(m._DEFAULT_EVIDENCE_TMP is not None)"
        )
        clean_env = dict(os.environ)
        clean_env.pop("P6_UI_EVIDENCE_DIR", None)
        default = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(Path(__file__).parent),
            env=clean_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        default_lines = default.stdout.strip().splitlines()
        self.assertEqual(default_lines[-1], "True")
        self.assertTrue(Path(default_lines[-2]).is_relative_to(Path(tempfile.gettempdir())))

        explicit_dir = str(Path(tempfile.gettempdir()) / "p6-ui-explicit-evidence-check")
        explicit_env = dict(clean_env, P6_UI_EVIDENCE_DIR=explicit_dir)
        explicit = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(Path(__file__).parent),
            env=explicit_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        explicit_lines = explicit.stdout.strip().splitlines()
        self.assertEqual(explicit_lines[-2], explicit_dir)
        self.assertEqual(explicit_lines[-1], "False")


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class SpotlightKeyframesBrowserTests(unittest.TestCase):
    """Focused P6 UI contract checks against the existing browser fixture."""

    setUp = _fx_preview_browser.FxPreviewBrowserTests.setUp
    write_json = _fx_preview_browser.FxPreviewBrowserTests.write_json
    stop_server = _fx_preview_browser.FxPreviewBrowserTests.stop_server
    open_preview = _fx_preview_browser.FxPreviewBrowserTests.open_preview
    mock_preview_api = _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api

    @classmethod
    def tearDownClass(cls):
        if _DEFAULT_EVIDENCE_TMP is not None:
            _DEFAULT_EVIDENCE_TMP.cleanup()
        super().tearDownClass()

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
            automatic_body = next(
                body for body in reversed(posted)
                if body.get("candidate", {}).get("code") == "P5"
                and "spotlight_path" not in body.get("candidate", {})
            )
            self.assertNotIn("spotlight_path", page.evaluate("() => fxPreviewDraft"))
            self.assertNotIn("subject", automatic_body["candidate"])
            page.keyboard.press("Escape")
            browser.close()

    def test_keyframe_row_keyboard_edit_keeps_focus_until_change(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            row = page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").first
            time_input = row.locator("input").nth(0)
            time_input.focus()
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.type("1.234")
            self.assertEqual(
                page.evaluate("() => document.activeElement?.getAttribute('aria-label')"),
                time_input.get_attribute("aria-label"),
            )
            self.assertEqual(time_input.input_value(), "1.234")
            page.keyboard.press("Tab")
            page.wait_for_timeout(50)
            self.assertEqual(
                page.evaluate("() => document.activeElement?.getAttribute('aria-label')"),
                row.locator("input").nth(1).get_attribute("aria-label"),
            )
            browser.close()

    def test_source_hit_target_is_mouse_only_and_keyboard_controls_edit_path(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function(
                "() => document.querySelector('#spotlight-source-click-target')?.getBoundingClientRect().width > 0"
            )

            target = page.locator("#spotlight-source-click-target")
            self.assertEqual(target.evaluate("el => el.tagName"), "DIV")
            self.assertIsNone(target.get_attribute("tabindex"))
            self.assertIsNone(target.get_attribute("role"))
            self.assertIsNone(target.get_attribute("aria-label"))
            self.assertEqual(target.get_attribute("aria-hidden"), "true")

            # The source hit target remains a pointer-only affordance.  Walk
            # the real tab sequence from the preceding source preview control;
            # keyboard focus must never land on the target.
            page.locator("#fx-preview-resync").focus()
            tabbed_ids = []
            for _ in range(10):
                page.keyboard.press("Tab")
                tabbed_ids.append(page.evaluate("() => document.activeElement?.id"))
            self.assertNotIn("spotlight-source-click-target", tabbed_ids)

            # Check Chromium's accessibility tree as well as DOM attributes:
            # aria-hidden must remove the pointer-only target from the
            # advertised keyboard/action tree.
            cdp = page.context.new_cdp_session(page)
            document = cdp.send("DOM.getDocument", {"depth": -1})
            node_id = cdp.send(
                "DOM.querySelector",
                {
                    "nodeId": document["root"]["nodeId"],
                    "selector": "#spotlight-source-click-target",
                },
            )["nodeId"]
            ax_nodes = cdp.send(
                "Accessibility.getPartialAXTree",
                {"nodeId": node_id, "fetchRelatives": False},
            )["nodes"]
            self.assertTrue(ax_nodes, "source hit target missing from AX inspection")
            self.assertTrue(ax_nodes[0]["ignored"], ax_nodes[0])

            # Numeric controls and action buttons are the keyboard route for
            # editing.  Use actual keyboard text entry plus Enter activation
            # to update, add, and delete a point.
            row = page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").first
            inputs = row.locator("input")
            for field, value in zip((inputs.nth(0), inputs.nth(1), inputs.nth(2)), ("0.000", "0.250000", "0.350000")):
                field.focus()
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.type(value)
            page.locator("#spotlight-update-point").focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => fxPreviewDraft.spotlight_path.keyframes[0].x === 0.25"
            )
            self.assertEqual(page.evaluate("() => fxPreviewDraft.spotlight_path.keyframes[0].y"), 0.35)

            for selector, value in (
                ("#spotlight-point-time", "1.000"),
                ("#spotlight-point-x", "0.750000"),
                ("#spotlight-point-y", "0.250000"),
            ):
                field = page.locator(selector)
                field.focus()
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.type(value)
            page.locator("#spotlight-add-point").focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => document.querySelectorAll('#spotlight-keyframe-list .spotlight-keyframe-row').length === 3"
            )
            page.locator("#spotlight-delete-point").focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => document.querySelectorAll('#spotlight-keyframe-list .spotlight-keyframe-row').length === 2"
            )

            # Pointer activation still adds a point from the source content
            # rectangle after the keyboard-only edit path.
            page.evaluate(
                "() => { const video = document.querySelector('#fx-preview-source-video'); video.currentTime = 0.7; video.dispatchEvent(new Event('timeupdate')); }"
            )
            before = page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count()
            box = target.bounding_box()
            self.assertIsNotNone(box)
            target.click(position={"x": box["width"] * 0.25, "y": box["height"] * 0.4})
            self.assertEqual(
                page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(),
                before + 1,
            )
            self.assertNotEqual(page.evaluate("() => document.activeElement?.id"), "spotlight-source-click-target")
            page.keyboard.press("Enter")
            page.keyboard.press("Space")
            self.assertEqual(
                page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").count(),
                before + 1,
            )
            self.assertTrue(
                page.evaluate(
                    "() => fxPreviewDraft.spotlight_path.keyframes.some(point => Math.abs(point.source_at - 0.7) < 0.001)"
                )
            )
            browser.close()

    def test_manual_edge_contracts_for_empty_cap_marker_and_python_rounding(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            rounding = page.evaluate("""() => [
                fxPreviewCanonicalNumber(0.0625, 3),
                fxPreviewCanonicalNumber(2.0005, 3),
                fxPreviewCanonicalBounds(1.0004, 2.0004).end,
                fxPreviewCanonicalNumber(0.1234565, 6),
                fxPreviewCanonicalNumber(0.1234555, 6),
            ]""")
            expected_rounding = [
                round(0.0625, 3),
                round(2.0005, 3),
                round(round(1.0004, 3) + round(2.0004, 3), 3),
                round(0.1234565, 6),
                round(0.1234555, 6),
            ]
            self.assertEqual(rounding, expected_rounding)

            # Empty add fields use explicit defaults, while an empty update is
            # invalid and leaves the selected path point unchanged.
            page.locator("#spotlight-point-time").fill("")
            page.locator("#spotlight-point-x").fill("")
            page.locator("#spotlight-point-y").fill("")
            page.evaluate("() => { const v = document.querySelector('#fx-preview-source-video'); v.currentTime = 0.7; v.dispatchEvent(new Event('timeupdate')); }")
            page.locator("#spotlight-add-point").click()
            added = page.evaluate("() => fxPreviewDraft.spotlight_path.keyframes.find(p => p.source_at === 0.7)")
            self.assertEqual(added, {"source_at": 0.7, "x": 0.5, "y": 0.5})
            before = page.evaluate("() => structuredClone(fxPreviewDraft.spotlight_path.keyframes[fxPreviewSpotlightSelectedIndex])")
            page.locator("#spotlight-point-x").fill("")
            page.locator("#spotlight-update-point").click()
            self.assertEqual(page.evaluate("() => fxPreviewDraft.spotlight_path.keyframes[fxPreviewSpotlightSelectedIndex]"), before)
            self.assertIn("有限數字", page.locator("#spotlight-validation").inner_text())

            cap = page.evaluate("""() => {
                const path = fxPreviewDraft.spotlight_path.keyframes;
                const start = fxPreviewSpotlightBounds().start;
                const end = fxPreviewSpotlightBounds().end;
                path.splice(0, path.length, ...Array.from({length: 64}, (_, i) => ({source_at: i / 50, x: .2, y: .3})));
                const replacement = fxPreviewSpotlightAddPoint(path[10].source_at, .8, .7);
                const afterReplacement = path.length;
                const rejected = fxPreviewSpotlightAddPoint(end + .001, .6, .6);
                return {replacement, afterReplacement, rejected, finalCount: path.length, point: path[10]};
            }""")
            self.assertEqual(cap, {"replacement": True, "afterReplacement": 64, "rejected": False, "finalCount": 64, "point": {"source_at": cap["point"]["source_at"], "x": 0.8, "y": 0.7}})

            marker_sizes = []
            for width, height in ((390, 900), (1100, 900), (1440, 900), (1440, 390)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(50)
                marker_sizes.append(page.evaluate("""() => {
                    const marker = document.querySelector('#spotlight-path-markers ellipse');
                    const box = marker.getBoundingClientRect();
                    return {tag: marker.tagName, width: box.width, height: box.height};
                }"""))
            self.assertTrue(all(item["tag"] == "ellipse" for item in marker_sizes))
            self.assertTrue(all(abs(item["width"] - 12) < 0.2 and abs(item["height"] - 12) < 0.2 for item in marker_sizes), marker_sizes)
            browser.close()

    def test_row_empty_is_incomplete_and_native_bad_input_is_not_an_add_default(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            row_time = page.locator("#spotlight-keyframe-list .spotlight-keyframe-row").first.locator("input").nth(0)
            row_time.focus()
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            page.keyboard.press("Tab")
            page.wait_for_function("() => !Number.isFinite(fxPreviewDraft.spotlight_path.keyframes[0].source_at)")
            self.assertTrue(page.locator("#fx-preview-confirm").is_disabled())
            self.assertIn("關鍵點", page.locator("#spotlight-validation").inner_text())
            self.assertEqual(page.evaluate("() => document.activeElement?.getAttribute('aria-label')"), row_time.locator("xpath=following-sibling::input[1]").get_attribute("aria-label"))
            row_time.focus()
            page.keyboard.type("0")
            page.keyboard.press("Tab")
            page.wait_for_function("() => fxPreviewDraft.spotlight_path.keyframes[0].source_at === 0")

            for selector in ("#spotlight-point-time", "#spotlight-point-x", "#spotlight-point-y"):
                page.locator(selector).fill("0.5")
            bad = page.locator("#spotlight-point-time")
            bad.focus()
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.type("-")
            self.assertEqual(bad.evaluate("el => ({value: el.value, badInput: el.validity.badInput})"), {"value": "", "badInput": True})
            before = page.evaluate("() => structuredClone(fxPreviewDraft.spotlight_path.keyframes)")
            page.locator("#spotlight-add-point").click()
            self.assertEqual(page.evaluate("() => fxPreviewDraft.spotlight_path.keyframes"), before)
            self.assertIn("有限數字", page.locator("#spotlight-validation").inner_text())
            browser.close()

    def test_manual_hold_resets_when_selecting_another_effect(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => document.querySelector('#spotlight-source-click-target')?.getBoundingClientRect().width > 0")
            token_before = page.evaluate("() => fxPreviewManualInteractionToken")
            page.locator("#spotlight-source-click-target").click(position={"x": 90, "y": 120})
            page.wait_for_function("() => fxPreviewManualSourceHold")
            page.locator("#fx-preview-search").fill("H1")
            page.locator("#fx-preview-grid .fx-preview-card").first.click()
            page.wait_for_function("() => fxPreviewDraft?.code === 'H1'")
            page.wait_for_function("() => !fxPreviewManualSourceHold && fxPreviewManualInteractionToken > 0")
            self.assertGreater(page.evaluate("() => fxPreviewManualInteractionToken"), token_before)
            self.assertFalse(page.evaluate("() => fxPreviewManualSourceHold"))
            browser.close()

    def test_manual_path_rebuilds_after_switching_through_non_p5_effect(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => fxPreviewDraft?.spotlight_path?.keyframes?.length === 2")
            page.locator("#fx-preview-search").fill("T3")
            page.locator("#fx-preview-grid .fx-preview-card").first.click()
            page.wait_for_function("() => fxPreviewDraft?.code === 'T3'")
            page.locator("#fx-preview-search").fill("P5")
            page.locator("#fx-preview-grid .fx-preview-card").first.click()
            page.wait_for_function("() => fxPreviewDraft?.code === 'P5'")
            result = page.evaluate("""() => {
                const bounds = fxPreviewSpotlightBounds();
                const path = fxPreviewDraft.spotlight_path;
                return {mode: fxPreviewDraft.spotlight_mode, path, bounds};
            }""")
            self.assertEqual(result["mode"], "manual")
            self.assertIsNotNone(result["path"])
            self.assertEqual(len(result["path"]["keyframes"]), 2)
            self.assertEqual(result["path"]["keyframes"][0]["source_at"], result["bounds"]["start"])
            self.assertEqual(result["path"]["keyframes"][-1]["source_at"], result["bounds"]["end"])
            browser.close()

    def test_explicit_automatic_survives_non_p5_effect_without_path(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.locator("#spotlight-mode-automatic").check()
            page.wait_for_function("() => fxPreviewDraft?.spotlight_mode === 'automatic' && !fxPreviewDraft.spotlight_path")
            page.locator("#fx-preview-search").fill("T3")
            page.locator("#fx-preview-grid .fx-preview-card").first.click()
            page.wait_for_function("() => fxPreviewDraft?.code === 'T3'")
            page.locator("#fx-preview-search").fill("P5")
            page.locator("#fx-preview-grid .fx-preview-card").first.click()
            page.wait_for_function("() => fxPreviewDraft?.code === 'P5'")
            draft = page.evaluate("() => ({mode: fxPreviewDraft.spotlight_mode, hasPath: Object.prototype.hasOwnProperty.call(fxPreviewDraft, 'spotlight_path')})")
            self.assertEqual(draft, {"mode": "automatic", "hasPath": False})
            browser.close()

    def test_manual_hold_notice_release_resumes_sync_without_autoplay(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => document.querySelector('#spotlight-source-click-target')?.getBoundingClientRect().width > 0")
            page.locator("#spotlight-source-click-target").click(position={"x": 90, "y": 120})
            page.wait_for_function("() => fxPreviewManualSourceHold")
            notice = page.locator("#spotlight-manual-hold-notice")
            self.assertTrue(notice.is_visible())
            self.assertIn("定位中已暫停同步。恢復同步後即可播放試看。", notice.inner_text())
            release = page.locator("#spotlight-manual-hold-release")
            self.assertEqual(release.inner_text(), "恢復同步")
            self.assertEqual(release.evaluate("el => getComputedStyle(el).minHeight"), "44px")
            ART.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ART / "p6-ui-r3-1100-manual-hold.png"), full_page=True)

            # Seeking and a native row edit keep the visible hold affordance.
            page.locator("#spotlight-seek-end").click()
            self.assertTrue(notice.is_visible())
            page.locator("#spotlight-keyframe-list input").first.fill(
                page.locator("#spotlight-keyframe-list input").first.input_value()
            )
            self.assertTrue(notice.is_visible())
            token_before = page.evaluate("() => fxPreviewManualInteractionToken")
            confirm_before = page.locator("#fx-preview-confirm").is_disabled()
            page.locator("#spotlight-manual-hold-release").click()
            self.assertFalse(page.evaluate("() => fxPreviewManualSourceHold"))
            self.assertGreater(page.evaluate("() => fxPreviewManualInteractionToken"), token_before)
            self.assertFalse(notice.is_visible())
            self.assertEqual(page.locator("#fx-preview-confirm").is_disabled(), confirm_before)
            self.assertTrue(page.locator("#fx-preview-source-video").evaluate("el => el.paused"))
            self.assertTrue(page.locator("#fx-preview-video").evaluate("el => el.paused"))

            # After release, both native play handlers take the normal sync
            # branch; they no longer pause the media because of manual hold.
            calls = page.evaluate("""() => {
                const source = elements['fx-preview-source-video'];
                const effect = elements['fx-preview-video'];
                const events = [];
                source.play = () => { events.push('source.play'); return Promise.resolve(); };
                effect.play = () => { events.push('effect.play'); return Promise.resolve(); };
                source.pause = () => events.push('source.pause');
                effect.pause = () => events.push('effect.pause');
                source.dispatchEvent(new Event('play'));
                effect.dispatchEvent(new Event('play'));
                return {hold: fxPreviewManualSourceHold, events};
            }""")
            self.assertFalse(calls["hold"])
            self.assertIn("source.play", calls["events"])
            self.assertIn("effect.play", calls["events"])
            self.assertNotIn("source.pause", calls["events"])
            self.assertNotIn("effect.pause", calls["events"])
            browser.close()

    def test_saved_manual_inspector_timing_is_locked_and_routes_to_preview(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            page.evaluate("""() => {
                const overlay = {
                    id: 'fx-manual-inspector', type: 'fx', start: 0.2, end: 1.2,
                    text: '聚光燈', emphasis: [], visible: true, locked: false,
                    z_index: 25, style: {x: 50, y: 50, width: 100, animation: 'none'},
                    code: 'P5', values: {}, placement: '', scheme: '', font_ids: {},
                    spotlight_path: {version: 1, interpolation: 'smoothstep', keyframes: [
                        {source_at: 0.2, x: 0.5, y: 0.5},
                        {source_at: 1.2, x: 0.5, y: 0.5},
                    ]}, provenance: 'browser test', highlight_id: null,
                };
                state.overlays.push(overlay);
                state.review = state.review || {};
                selectedOverlayId = overlay.id;
                state.review.selected_overlay_id = overlay.id;
                renderAll();
            }""")
            self.assertTrue(page.locator("#overlay-start").is_disabled())
            self.assertTrue(page.locator("#overlay-end").is_disabled())
            self.assertTrue(page.locator("#manual-spotlight-timing-note").is_visible())
            unchanged = page.evaluate("""() => {
                const input = elements['overlay-start'];
                input.value = '0.8';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return currentOverlay().start;
            }""")
            self.assertEqual(unchanged, 0.2)
            page.locator("#manual-spotlight-timing-open").click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.wait_for_function("() => fxPreviewDraft?.code === 'P5'")
            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            browser.close()

    def test_saved_manual_reopen_clears_editor_reset_but_preserves_pending_add_values(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            page.wait_for_function("() => fxPreviewDraft?.spotlight_path?.keyframes?.length === 2")

            # Leave a real, non-default editor value in the closed dialog so a
            # later saved-manual reopen can prove that lifecycle cleanup clears it.
            for selector, value in (
                ("#spotlight-point-time", "0.900"),
                ("#spotlight-point-x", "0.123456"),
                ("#spotlight-point-y", "0.654321"),
            ):
                page.locator(selector).fill(value)
            page.locator("#spotlight-add-point").click()
            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")

            page.evaluate("""() => {
                const overlay = {
                    id: 'saved-manual-reopen-regression', type: 'fx', start: 0.2, end: 1.2,
                    text: '聚光燈', emphasis: [], visible: true, locked: false, z_index: 25,
                    style: {x: 50, y: 50, width: 100, animation: 'none'},
                    code: 'P5', values: {}, placement: '', scheme: '', font_ids: {},
                    spotlight_path: {version: 1, interpolation: 'smoothstep', keyframes: [
                        {source_at: 0.2, x: 0.5, y: 0.5},
                        {source_at: 1.2, x: 0.5, y: 0.5},
                    ]}, provenance: 'browser test', highlight_id: null,
                };
                state.overlays.push(overlay);
                state.review = state.review || {};
                selectedOverlayId = overlay.id;
                state.review.selected_overlay_id = overlay.id;
                renderAll();
            }""")
            page.locator("#manual-spotlight-timing-open").click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.wait_for_function("() => fxPreviewDraft?.code === 'P5'")

            reopened = page.evaluate("""() => ({
                selected: fxPreviewSpotlightSelectedIndex,
                time: document.querySelector('#spotlight-point-time').value,
                x: document.querySelector('#spotlight-point-x').value,
                y: document.querySelector('#spotlight-point-y').value,
            })""")
            self.assertEqual(reopened, {"selected": -1, "time": "", "x": "", "y": ""})

            # Render paths must not erase values the user has typed while no
            # point is selected; these are still pending values for Add.
            pending = {"#spotlight-point-time": "0.901", "#spotlight-point-x": "0.210000", "#spotlight-point-y": "0.790000"}
            for selector, value in pending.items():
                page.locator(selector).fill(value)
            page.evaluate("""() => {
                fxPreviewSpotlightRenderPanel({renderList: false});
                fxPreviewRenderDialog();
            }""")
            self.assertEqual(
                {selector: page.locator(selector).input_value() for selector in pending},
                pending,
            )

            # With genuinely blank fields, Add uses the actual source currentTime
            # and canonicalizes only that time; X/Y remain the documented center.
            for selector in pending:
                page.locator(selector).fill("")
            page.evaluate("""() => {
                const source = document.querySelector('#fx-preview-source-video');
                source.currentTime = 0.7336;
                source.dispatchEvent(new Event('timeupdate'));
            }""")
            page.locator("#spotlight-add-point").click()
            added = page.evaluate("""() => {
                const source = document.querySelector('#fx-preview-source-video');
                const expected = fxPreviewCanonicalNumber(source.currentTime, 3);
                const point = fxPreviewDraft.spotlight_path.keyframes.find(item => item.source_at === expected);
                return {sourceTime: source.currentTime, expected, point};
            }""")
            self.assertEqual(added["point"], {"source_at": added["expected"], "x": 0.5, "y": 0.5})

            page.locator("#fx-preview-cancel").click()
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            browser.close()

    def test_mode_seed_fixed_label_and_output_crop_contract(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(CHROME), headless=True, args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_default_timeout(5000)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            result = page.evaluate("""() => {
                const p5 = fxEffectByCode('P5');
                const h1 = fxEffectByCode('H1');
                const oldPath = {version: 1, interpolation: 'smoothstep', keyframes: [
                    {source_at: 0, x: 0.2, y: 0.5}, {source_at: 2, x: 0.8, y: 0.5},
                ]};
                const explicitAutomatic = fxPreviewBuildDraft(
                    p5, {spotlight_mode: 'automatic', spotlight_path: oldPath},
                    {start: 0, end: 2, spotlight_path: oldPath}, 0,
                );
                const switchedAutomatic = {...explicitAutomatic, __spotlightModeOverride: 'automatic'};
                const other = fxPreviewBuildDraft(h1, switchedAutomatic, {start: 0, end: 2, spotlight_path: oldPath}, 0);
                const backToP5 = fxPreviewBuildDraft(p5, other, {start: 0, end: 2, spotlight_path: oldPath}, 0);
                state.canvas = {...state.canvas, width: 1080, height: 1920, fit: 'cover'};
                const cover = fxPreviewSpotlightOutputCropState([{x: 0.05, y: 0.5}], {width: 1920, height: 1080});
                state.canvas = {...state.canvas, width: 1080, height: 1920, fit: 'contain'};
                const contain = fxPreviewSpotlightOutputCropState([{x: 0.05, y: 0.5}], {width: 1920, height: 1080});
                return {
                    explicitAutomatic: {
                        mode: explicitAutomatic.spotlight_mode,
                        hasPath: Object.prototype.hasOwnProperty.call(explicitAutomatic, 'spotlight_path') && Boolean(explicitAutomatic.spotlight_path),
                    },
                    backToP5: {mode: backToP5.spotlight_mode, path: backToP5.spotlight_path},
                    coverOutside: cover.outside,
                    containOutside: contain.outside,
                };
            }""")
            self.assertEqual(result["explicitAutomatic"], {"mode": "automatic", "hasPath": False})
            self.assertEqual(result["backToP5"]["mode"], "automatic")
            self.assertIsNone(result["backToP5"]["path"])
            self.assertTrue(result["coverOutside"])
            self.assertFalse(result["containOutside"])
            self.mock_preview_api(page)
            self._open_p5(page)
            page.locator("#spotlight-mode-manual").check()
            validation = page.locator("#spotlight-validation").inner_text()
            self.assertIn("固定位置", validation)
            self.assertIn("中心預設", validation)
            self.assertEqual(
                page.locator("#spotlight-keyframe-list input").first.evaluate("el => getComputedStyle(el).minHeight"),
                "44px",
            )
            self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 390)
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

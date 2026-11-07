from __future__ import annotations

import sys
import copy
import json
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_editor_browser as _editor_browser  # noqa: E402
import test_fx_preview_browser as _fx_preview_browser  # noqa: E402


CHROME = _editor_browser.CHROME
sync_playwright = _editor_browser.sync_playwright


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class FxComparisonBrowserTests(unittest.TestCase):
    """The browser owns the queue: one standard candidate at a time, never auto-apply."""

    setUp = _editor_browser.EditorBrowserSmokeTests.setUp
    write_json = _editor_browser.EditorBrowserSmokeTests.write_json
    stop_server = _editor_browser.EditorBrowserSmokeTests.stop_server

    def mock_comparison_api(self, page, outcomes=None, cancel_states=None, post_delay=0.0, confirm_delay=0.0):
        project = page.evaluate("async () => (await fetch('/api/project')).json()")
        server_state = copy.deepcopy(project["state"])
        outcomes = list(outcomes or ["ready"] * 8)
        cancel_states = list(cancel_states or [])
        jobs = {}
        posted = []
        cancelled = []
        confirmed = []
        active = set()
        metrics = {"max_active": 0}

        def route_handler(route):
            nonlocal server_state
            request = route.request
            path = urlparse(request.url).path
            if not path.startswith("/api/fx-previews"):
                route.fallback()
                return
            if path == "/api/fx-previews" and request.method == "POST":
                body = json.loads(request.post_data or "{}")
                index = len(posted)
                outcome = outcomes[index] if index < len(outcomes) else "ready"
                posted.append(body)
                if outcome.startswith("http409:"):
                    reason = outcome.split(":", 1)[1]
                    route.fulfill(status=409, json={"error": "stale", "error_code": reason, "reason": reason})
                    return
                job_id = f"{index + 1:032x}"
                job = {
                    "id": job_id,
                    "state": "running",
                    "phase": "rendering",
                    "message": "fixture running",
                    "status_url": f"/api/fx-previews/{job_id}",
                    "preview_options": dict(body.get("preview_options") or {}),
                    "_outcome": outcome,
                    "_body": body,
                }
                jobs[job_id] = job
                active.add(job_id)
                metrics["max_active"] = max(metrics["max_active"], len(active))
                if post_delay:
                    time.sleep(post_delay)
                route.fulfill(json={"ok": True, "job": {key: value for key, value in job.items() if not key.startswith("_")}})
                return
            parts = [part for part in path.split("/") if part]
            job_id = parts[2] if len(parts) > 2 else ""
            if request.method == "GET" and job_id in jobs:
                job = jobs[job_id]
                outcome = job["_outcome"]
                if outcome == "still_running":
                    job.update(state="running", phase="cancelling", error={"code": "termination_unverified"})
                elif outcome == "failed_preview_stale":
                    job.update(state="failed", phase="failed", error={"code": "preview_stale", "message": "source changed"})
                    active.discard(job_id)
                elif outcome == "failed":
                    job.update(state="failed", phase="failed", error={"code": "render_failed", "message": "render failed"})
                    active.discard(job_id)
                else:
                    job.update(
                        state="ready", phase="ready", media_url=f"/media/source?compare={job_id}",
                        time_map=[{"output_start_s": 0, "output_end_s": 2, "source_start_s": 0, "source_end_s": 2}],
                    )
                    active.discard(job_id)
                route.fulfill(json={"ok": True, "job": {key: value for key, value in job.items() if not key.startswith("_")}})
                return
            if request.method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                job = jobs.get(job_id, {"id": job_id, "status_url": f"/api/fx-previews/{job_id}"})
                cancel_state = cancel_states.pop(0) if cancel_states else "cancelled"
                cancelled.append(job_id)
                if cancel_state == "running":
                    job.update(state="running", phase="cancelling", error={"code": "termination_unverified"})
                else:
                    job.update(state="cancelled", phase="cancelled")
                    job.pop("error", None)
                    active.discard(job_id)
                route.fulfill(json={"ok": True, "job": {key: value for key, value in job.items() if not key.startswith("_")}})
                return
            if request.method == "POST" and len(parts) == 4 and parts[3] == "confirm":
                confirmed.append(job_id)
                if confirm_delay:
                    time.sleep(confirm_delay)
                server_state = copy.deepcopy(server_state)
                server_state["revision"] = "d" * 64
                route.fulfill(json={"ok": True, "state": server_state, "revision": server_state["revision"], "overlay_id": "fx-confirmed"})
                return
            route.fallback()

        page.route("**/api/fx-previews**", route_handler)
        return posted, cancelled, confirmed, metrics

    def prepare_candidates(self, page, count=3):
        page.locator('[data-add-type="fx"]').click()
        page.locator("#fx-preview-dialog[open]").wait_for()
        page.locator("#fx-preview-compare-toggle").click()
        if not page.locator("#fx-preview-catalog-disclosure").evaluate("node => node.open"):
            page.locator("#fx-preview-catalog-disclosure > summary").click()
        for code in ("T3", "H1", "T1", "T2")[:count]:
            page.locator(f'#fx-preview-grid .fx-preview-card[data-fx-code="{code}"]').click()
            page.locator("#fx-preview-compare-add").click()

    def test_four_candidates_are_serial_capped_and_only_explicit_selection_confirms(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _failed = _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            confirmed = []
            page.on(
                "request",
                lambda request: confirmed.append(urlparse(request.url).path)
                if request.method == "POST" and urlparse(request.url).path.endswith("/confirm")
                else None,
            )
            page.locator('[data-add-type="fx"]').click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.locator("#fx-preview-compare-toggle").click()
            page.wait_for_timeout(450)  # any pre-compare debounce has been drained/cancelled
            page.locator(".fx-preview-advanced > summary").click()
            page.locator("#fx-preview-mode").select_option("timeline")
            before = len(posted)
            for code in ("T3", "H1", "T1", "T2"):
                page.locator(f'#fx-preview-grid .fx-preview-card[data-fx-code="{code}"]').click()
                page.locator("#fx-preview-compare-add").click()
            self.assertEqual(page.locator("#fx-preview-compare-candidates [data-fx-compare-id]").count(), 4)
            self.assertTrue(page.locator("#fx-preview-compare-add").is_disabled())
            page.evaluate("() => fxCompareAdd()")
            self.assertEqual(page.locator("#fx-preview-compare-candidates [data-fx-compare-id]").count(), 4)
            self.assertTrue(page.locator("#fx-preview-confirm").is_disabled())
            self.assertFalse(page.locator("#fx-preview-compare-start").is_disabled())
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("""() => [...document.querySelectorAll('#fx-preview-compare-candidates [data-fx-compare-id]')]
                .every(card => card.dataset.state === 'ready')""")
            batch = posted[before:]
            self.assertEqual(len(batch), 4)
            self.assertTrue(all(item["preview_options"]["quality"] == "standard" for item in batch))
            self.assertTrue(all(item["preview_options"]["mode"] == "timeline" for item in batch))
            self.assertEqual({item["expected_revision"] for item in batch}, {batch[0]["expected_revision"]})
            self.assertEqual({str(item["candidate"]["window"]) for item in batch}, {str(batch[0]["candidate"]["window"])})
            self.assertNotIn("comparison_anchor_job_id", batch[0])
            anchor = page.locator("#fx-preview-compare-candidates [data-fx-compare-id]").first.get_attribute("data-job-id")
            self.assertTrue(anchor)
            self.assertEqual(batch[1]["comparison_anchor_job_id"], anchor)
            self.assertEqual(batch[2]["comparison_anchor_job_id"], anchor)
            self.assertEqual(batch[3]["comparison_anchor_job_id"], anchor)
            page.locator("#fx-preview-compare-candidates [data-fx-compare-id]").nth(1).click()
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            selected_job = page.locator("#fx-preview-compare-candidates [data-fx-compare-id]").nth(1).get_attribute("data-job-id")
            page.locator("#fx-preview-confirm").click()
            page.wait_for_function("() => !fxCompareActive && fxCompareCandidates.length === 0")
            page.locator("#fx-preview-dialog:not([open])").wait_for(state="hidden")
            self.assertEqual(confirmed, [f"/api/fx-previews/{selected_job}/confirm"])
            self.assertEqual(
                page.evaluate(
                    "() => ({active: fxCompareActive, candidates: fxCompareCandidates.length, frozen: fxCompareFrozen, selected: fxCompareSelectedId})"
                ),
                {"active": False, "candidates": 0, "frozen": None, "selected": ""},
            )
            browser.close()

    def test_start_guard_blocks_double_click_before_first_await(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _failed = _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            page.locator('[data-add-type="fx"]').click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.locator("#fx-preview-compare-toggle").click()
            page.wait_for_timeout(450)
            before = len(posted)
            for code in ("T3", "H1", "T1"):
                page.locator(f'#fx-preview-grid .fx-preview-card[data-fx-code="{code}"]').click()
                page.locator("#fx-preview-compare-add").click()
            page.evaluate("() => { fxCompareStart(); fxCompareStart(); }")
            page.wait_for_function(
                "() => fxCompareCandidates.length === 3 && fxCompareCandidates.every(item => item.state === 'ready')"
            )
            time.sleep(0.1)
            self.assertEqual(len(posted[before:]), 3)
            browser.close()

    def test_candidate_status_is_text_not_html(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            page.locator('[data-add-type="fx"]').click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.locator("#fx-preview-compare-toggle").click()
            page.evaluate(
                """() => {
                    fxCompareCandidates = [{
                        id: 'injection', draft: {...fxPreviewDraft}, signature: 'x', state: 'failed', job: null,
                        message: '<img id="fx-compare-injected" src=x onerror="window.__fxCompareInjected=true">'
                    }];
                    fxCompareRender();
                }"""
            )
            self.assertEqual(page.locator("#fx-compare-injected").count(), 0)
            self.assertIn("<img", page.locator(".fx-compare-card span").inner_text())
            self.assertFalse(page.evaluate("() => Boolean(window.__fxCompareInjected)"))
            browser.close()

    def test_failed_candidate_continues_but_context_stale_stops_round(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])

            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _confirmed, metrics = self.mock_comparison_api(page, ["failed", "ready", "ready"])
            self.prepare_candidates(page)
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("() => !fxCompareRunning && fxCompareCandidates.every(item => ['failed','ready'].includes(item.state))")
            self.assertEqual(len(posted), 3)
            self.assertEqual(metrics["max_active"], 1)
            self.assertEqual(posted[1]["comparison_anchor_job_id"], "00000000000000000000000000000001")
            self.assertTrue(page.locator("#fx-preview-confirm").is_disabled())

            page2 = browser.new_page(viewport={"width": 1100, "height": 900})
            page2.goto(f"http://{host}:{port}/", wait_until="networkidle")
            stale_posted, _cancelled2, _confirmed2, _metrics2 = self.mock_comparison_api(
                page2, ["ready", "failed_preview_stale", "ready"]
            )
            self.prepare_candidates(page2)
            page2.locator("#fx-preview-compare-start").click()
            page2.wait_for_function("() => !fxCompareRunning && fxCompareRoundStale")
            self.assertEqual(len(stale_posted), 2)
            self.assertTrue(page2.locator("#fx-preview-confirm").is_disabled())
            self.assertTrue(page2.locator("#fx-preview-compare-start").is_disabled())

            page3 = browser.new_page(viewport={"width": 1100, "height": 900})
            page3.goto(f"http://{host}:{port}/", wait_until="networkidle")
            conflict_posted, _cancelled3, _confirmed3, _metrics3 = self.mock_comparison_api(
                page3, ["http409:revision_conflict", "ready", "ready"]
            )
            self.prepare_candidates(page3)
            page3.locator("#fx-preview-compare-start").click()
            page3.wait_for_function("() => !fxCompareRunning && fxCompareRoundStale")
            self.assertEqual(len(conflict_posted), 1)
            browser.close()

    def test_unverified_cancel_stays_stopping_and_retry_closes(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, cancelled, _confirmed, _metrics = self.mock_comparison_api(
                page, ["still_running", "ready", "ready"], cancel_states=["running"]
            )
            self.prepare_candidates(page)
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("() => fxCompareCandidates[0]?.job?.id && fxCompareCandidates[0].state === 'running'")
            page.locator("#fx-preview-cancel").click()
            page.wait_for_function("() => fxCompareStopping && !fxCompareCleanupPromise")
            self.assertTrue(page.locator("#fx-preview-dialog").evaluate("node => node.open"))
            self.assertEqual(len(posted), 1)
            self.assertGreaterEqual(len(cancelled), 1)
            self.assertTrue(page.locator("#fx-preview-compare-start").is_disabled())
            self.assertIn("停止", page.locator("#fx-preview-compare-note").inner_text())
            page.locator("#fx-preview-compare-discard").click()
            page.wait_for_function("() => !fxCompareActive && !fxCompareStopping")
            page.wait_for_function("() => !document.querySelector('#fx-preview-dialog').open")
            browser.close()

    def test_close_during_late_post_cancels_job_and_never_resumes_queue(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, cancelled, _confirmed, _metrics = self.mock_comparison_api(page, post_delay=0.65)
            self.prepare_candidates(page)
            page.evaluate("() => { fxCompareStart(); setTimeout(() => closeFxPreviewDialog(), 100); }")
            page.wait_for_function("() => !fxCompareActive && !fxCompareStopping", timeout=5000)
            page.wait_for_function("() => !document.querySelector('#fx-preview-dialog').open")
            self.assertEqual(len(posted), 1)
            self.assertEqual(cancelled, ["00000000000000000000000000000001"])
            page.wait_for_timeout(700)
            self.assertEqual(len(posted), 1)
            browser.close()

    def test_first_add_revision_snapshot_rejects_save_rebase(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, _cancelled, _confirmed, _metrics = self.mock_comparison_api(page)
            self.prepare_candidates(page)
            frozen_revision = page.evaluate("() => fxCompareFrozen.revision")
            page.evaluate(
                """() => {
                    stateDirty = true;
                    saveState = async () => {
                        state.revision = 'e'.repeat(64);
                        stateDirty = false;
                        lastSaveSucceeded = true;
                    };
                    void fxCompareStart();
                }"""
            )
            page.wait_for_function("() => !fxCompareStarting && fxCompareRoundStale")
            self.assertEqual(posted, [])
            self.assertEqual(page.evaluate("() => fxCompareFrozen.revision"), frozen_revision)
            self.assertIn("變更", page.locator("#fx-preview-status").inner_text())
            browser.close()

    def test_old_single_late_post_is_cancelled_before_comparison_queue(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            posted, cancelled, _confirmed, metrics = self.mock_comparison_api(page, post_delay=0.65)
            page.locator('[data-add-type="fx"]').click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.evaluate("() => { setTimeout(() => fxCompareEnter(), 400); }")
            page.wait_for_function("() => fxCompareActive && fxPreviewLifecycles.size === 0", timeout=5000)
            self.assertEqual(len(posted), 1)
            self.assertEqual(cancelled, ["00000000000000000000000000000001"])
            for code in ("T3", "H1", "T1"):
                page.locator(f'#fx-preview-grid .fx-preview-card[data-fx-code="{code}"]').click()
                page.locator("#fx-preview-compare-add").click()
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("() => !fxCompareRunning && fxCompareCandidates.every(item => item.state === 'ready')", timeout=7000)
            self.assertEqual(len(posted), 4)
            self.assertEqual(metrics["max_active"], 1)
            self.assertNotIn("comparison_anchor_job_id", posted[1])
            self.assertEqual(posted[2]["comparison_anchor_job_id"], "00000000000000000000000000000002")
            browser.close()

    def test_confirm_lock_selection_token_and_responsive_entry(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _posted, _cancelled, confirmed, _metrics = self.mock_comparison_api(page)
            editor_puts = []
            page.on(
                "request",
                lambda request: editor_puts.append(request.post_data)
                if request.method == "PUT" and urlparse(request.url).path == "/api/editor-state"
                else None,
            )
            self.prepare_candidates(page)
            self.assertTrue(page.locator("#fx-preview-compare-toggle").is_visible())
            page.set_viewport_size({"width": 390, "height": 900})
            if page.locator("#fx-preview-catalog-disclosure").evaluate("node => node.open"):
                page.locator("#fx-preview-catalog-disclosure > summary").click()
            self.assertFalse(page.locator("#fx-preview-catalog-disclosure").evaluate("node => node.open"))
            self.assertTrue(page.locator("#fx-preview-compare-controls").is_visible())
            for width in (390, 1100, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                self.assertTrue(page.locator("#fx-preview-compare-controls").is_visible())
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("() => !fxCompareRunning && fxCompareCandidates.every(item => item.state === 'ready')")
            cards = page.locator("#fx-preview-compare-candidates [data-fx-compare-id]")
            cards.nth(0).click()
            first = page.evaluate("() => ({session: fxCompareSession, token: fxCompareSelectionToken, id: fxCompareSelectedId, job: fxPreviewJob.id})")
            cards.nth(1).click()
            visible_second = page.evaluate(
                """() => {
                    const candidate = fxCompareCandidates[1];
                    const effect = fxEffectByCode(candidate.draft.code);
                    const expectedFields = (effect.vars || [])
                        .filter(item => item.kind === 'text')
                        .map(item => String(candidate.draft.values?.[item.id] ?? item.default ?? ''));
                    return {
                        draftCode: fxPreviewDraft.code,
                        expectedCode: candidate.draft.code,
                        name: document.querySelector('#fx-preview-selected-name').textContent,
                        expectedName: `#${effect.num} ${effect.name}`,
                        fields: [...document.querySelectorAll('#fx-preview-fields input[type="text"]')].map(input => input.value),
                        expectedFields,
                        source: document.querySelector('#fx-preview-source').value,
                        expectedSource: String(candidate.draft.window.start_s),
                    };
                }"""
            )
            self.assertEqual(visible_second["draftCode"], visible_second["expectedCode"])
            self.assertEqual(visible_second["name"], visible_second["expectedName"])
            self.assertEqual(visible_second["fields"], visible_second["expectedFields"])
            self.assertEqual(visible_second["source"], visible_second["expectedSource"])
            cards.nth(0).click()
            current = page.evaluate("() => ({session: fxCompareSession, token: fxCompareSelectionToken, id: fxCompareSelectedId, job: fxPreviewJob.id})")
            self.assertEqual(current["id"], first["id"])
            self.assertEqual(current["job"], first["job"])
            self.assertGreater(current["token"], first["token"])
            self.assertFalse(page.evaluate("snapshot => fxCompareSelectionCurrent(snapshot.session, snapshot.token, snapshot.id, snapshot.job)", first))
            self.assertTrue(page.evaluate("snapshot => fxCompareSelectionCurrent(snapshot.session, snapshot.token, snapshot.id, snapshot.job)", current))
            page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
            page.evaluate(
                """() => {
                    const originalRequest = request;
                    request = async (url, options) => {
                        if (String(url).endsWith('/confirm')) await new Promise(resolve => setTimeout(resolve, 600));
                        return originalRequest(url, options);
                    };
                }"""
            )
            page.evaluate("() => { void confirmFxPreview(); }")
            page.wait_for_function("() => fxCompareConfirming")
            self.assertTrue(page.locator("#fx-preview-close").is_disabled())
            self.assertTrue(cards.nth(1).is_disabled())
            page.keyboard.press("Control+S")
            page.wait_for_timeout(100)
            self.assertEqual(editor_puts, [])
            page.keyboard.press("Escape")
            self.assertTrue(page.locator("#fx-preview-dialog").evaluate("node => node.open"))
            page.wait_for_function("() => !fxCompareActive && !document.querySelector('#fx-preview-dialog').open")
            self.assertEqual(confirmed, [first["job"]])
            browser.close()

    def test_beforeunload_requests_cancel_for_known_running_comparison_job(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _posted, cancelled, _confirmed, _metrics = self.mock_comparison_api(
                page, ["still_running", "ready", "ready"]
            )
            self.prepare_candidates(page)
            page.locator("#fx-preview-compare-start").click()
            page.wait_for_function("() => fxCompareCandidates[0]?.job?.id && fxCompareCandidates[0].state === 'running'")
            page.evaluate("() => window.dispatchEvent(new Event('beforeunload'))")
            page.wait_for_function("() => true", timeout=300)
            for _ in range(20):
                if cancelled:
                    break
                page.wait_for_timeout(50)
            self.assertEqual(cancelled, ["00000000000000000000000000000001"])
            browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

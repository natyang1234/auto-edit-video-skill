from __future__ import annotations

import sys
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import test_editor_browser as _editor_browser  # noqa: E402
import test_fx_preview_browser as _fx_preview_browser  # noqa: E402


CHROME = _editor_browser.CHROME
sync_playwright = _editor_browser.sync_playwright


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class CompositeCheckBrowserTests(unittest.TestCase):
    """Browser contract for the bounded complete-picture receipt UI."""

    setUp = _editor_browser.EditorBrowserSmokeTests.setUp
    write_json = _editor_browser.EditorBrowserSmokeTests.write_json
    stop_server = _editor_browser.EditorBrowserSmokeTests.stop_server

    def open_ready_preview(self, page):
        page.locator('[data-add-type="fx"]').click()
        page.locator("#fx-preview-dialog[open]").wait_for()
        page.wait_for_function("() => fxPreviewJob?.id")
        page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")

    def install_job(self, page, receipt, *, mode="timeline", job_id=None):
        page.evaluate(
            """({receipt, mode, jobId}) => {
                fxPreviewDraft.preview_options.mode = mode;
                const job = {
                    id: jobId || `composition-${mode}`,
                    state: 'ready',
                    media_url: '/media/source',
                    render_duration_s: 1.6,
                    preview_options: {mode, quality: 'standard'},
                    time_map: [
                        {output_start_s: 0, output_end_s: .8, source_start_s: 0, source_end_s: .8},
                        {output_start_s: .8, output_end_s: 1.6, source_start_s: 1.2, source_end_s: 2},
                    ],
                    composition_check: receipt,
                };
                fxPreviewSetVisibleMedia(job);
                const effect = document.querySelector('#fx-preview-video');
                effect.src = '/media/source';
                effect.load();
                fxPreviewPrepareSourceVideo();
            }""",
            {"receipt": receipt, "mode": mode, "jobId": job_id},
        )
        page.wait_for_function(
            "() => document.querySelector('#fx-preview-video').readyState >= 1 && "
            "document.querySelector('#fx-preview-source-video').readyState >= 1",
            timeout=15000,
        )

    def test_warn_issue_jumps_both_videos_without_crossing_deleted_source_gap(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            self.open_ready_preview(page)
            receipt = {
                "version": 1,
                "scope": "full-timeline",
                "measurement": "sampled-local-frames",
                "status": "warn",
                "source_sha256": "a" * 64,
                "media_sha256": "b" * 64,
                "frame_size": {"width": 360, "height": 640},
                "sample_count": 1,
                "measured_count": 1,
                "elapsed_s": 0.4,
                "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
                "samples": [{
                    "output_at": 0.9, "source_at": 1.3, "measured": True,
                    "source_face_count": 1, "reference_face_count": 1,
                    "visible_face_count": 1, "text_box_count": 1,
                }],
                "findings": [{
                    "kind": "text_subject_overlap", "output_at": 0.9, "source_at": 1.3,
                    "subject_box": {"x": 20, "y": 30, "width": 80, "height": 90},
                    "text_box": {"x": 24, "y": 36, "width": 70, "height": 20},
                }, {
                    "kind": "visibility_uncertain", "output_at": 0.9, "source_at": 1.3,
                }],
            }
            self.install_job(page, receipt)
            panel = page.locator("#fx-preview-composition-check")
            self.assertEqual(panel.get_attribute("data-status"), "warn")
            self.assertIn("抽樣 1 個時間點", panel.locator("#fx-preview-composition-check-summary").inner_text())
            jump = panel.locator(".fx-preview-composition-check-list button")
            self.assertEqual(jump.count(), 1)
            self.assertIn("查看原片時間 00:01.300", jump.inner_text())
            issue_row = panel.locator(".fx-preview-composition-check-list li")
            self.assertIn("文字可能遮住人物或臉", issue_row.inner_text())
            self.assertIn("人物是否被遮住，無法確認", issue_row.inner_text())
            jump.click()
            page.wait_for_function(
                "() => Math.abs(document.querySelector('#fx-preview-source-video').currentTime - 1.3) < .12 && "
                "Math.abs(document.querySelector('#fx-preview-video').currentTime - .9) < .12"
            )
            self.assertTrue(page.locator("#fx-preview-source-video").evaluate("video => video.paused"))
            self.assertTrue(page.locator("#fx-preview-video").evaluate("video => video.paused"))

            # A valid pass is explicitly qualified as sampled; it never claims
            # that every rendered frame was checked.
            passed = dict(receipt)
            passed["status"] = "pass"
            passed["findings"] = []
            self.install_job(page, passed)
            pass_panel = page.locator("#fx-preview-composition-check")
            self.assertEqual(pass_panel.get_attribute("data-status"), "pass")
            pass_summary = pass_panel.locator("#fx-preview-composition-check-summary").inner_text()
            self.assertIn("抽樣 1 個時間點", pass_summary)
            self.assertIn("不代表每一格都已檢查", pass_summary)

            # A source time in the deleted [0.8, 1.2] gap is never rendered as
            # a clickable issue, even though the output timestamp is in range.
            invalid = dict(receipt)
            invalid["findings"] = [{"kind": "visibility_uncertain", "output_at": 0.9, "source_at": 1.0}]
            self.install_job(page, invalid)
            self.assertEqual(page.locator("#fx-preview-composition-check").get_attribute("data-status"), "unmeasured")
            self.assertEqual(page.locator("#fx-preview-composition-check-list button").count(), 0)
            browser.close()

    def test_unknown_receipt_cannot_render_pass_and_single_action_is_hidden_in_comparison(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            self.open_ready_preview(page)
            receipt = {
                "version": 1,
                "scope": "single-effect",
                "measurement": "sampled-local-frames",
                "status": "unmeasured",
                "source_sha256": "a" * 64,
                "media_sha256": "b" * 64,
                "frame_size": {"width": 360, "height": 640},
                "sample_count": 0,
                "measured_count": 0,
                "elapsed_s": 0.1,
                "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
                "samples": [],
                "findings": [{"kind": "full_timeline_required"}],
            }
            self.install_job(page, receipt, mode="single")
            panel = page.locator("#fx-preview-composition-check")
            self.assertEqual(panel.get_attribute("data-status"), "unmeasured")
            self.assertIn("不是完整時間軸", panel.locator("#fx-preview-composition-check-summary").inner_text())
            switch = page.locator("#fx-preview-composition-switch-timeline")
            self.assertTrue(switch.is_visible())
            switch.click()
            self.assertEqual(page.evaluate("() => fxPreviewDraft.preview_options.mode"), "timeline")

            # An unknown finding/status is conservatively downgraded, never a
            # pass. Comparison keeps the switch hidden throughout the round.
            result = page.evaluate(
                """() => {
                    const job = {render_duration_s: 1.6, time_map: [
                        {output_start_s: 0, output_end_s: .8, source_start_s: 0, source_end_s: .8},
                        {output_start_s: .8, output_end_s: 1.6, source_start_s: 1.2, source_end_s: 2},
                    ]};
                    const unknown = {
                        version: 1, scope: 'full-timeline', measurement: 'sampled-local-frames',
                        status: 'pass', source_sha256: 'a'.repeat(64), media_sha256: 'b'.repeat(64),
                        frame_size: {width: 360, height: 640}, sample_count: 1, measured_count: 1,
                        elapsed_s: .1, engines: {faces: 'opencv-yunet', ocr: 'macos-vision'},
                        samples: [{output_at: .2, source_at: .2, measured: true,
                            source_face_count: 1, reference_face_count: 1, visible_face_count: 1, text_box_count: 0}],
                        findings: [{kind: 'future_kind'}],
                    };
                    return fxPreviewCompositionReceipt(unknown, job).status;
                }"""
            )
            self.assertEqual(result, "unmeasured")

            # Receipt/time-map values arrive from JSON and must already be
            # numbers. JavaScript coercion of null, booleans, arrays or numeric
            # strings must never manufacture a trusted timestamp or count.
            page.evaluate("""() => {
                for (const id of ['fx-preview-video', 'fx-preview-source-video']) {
                    const video = document.getElementById(id);
                    video.src = '/media/source';
                    video.load();
                }
            }""")
            page.wait_for_function(
                "() => document.querySelector('#fx-preview-video').readyState >= 1 && "
                "document.querySelector('#fx-preview-source-video').readyState >= 1",
                timeout=15000,
            )
            strict = page.evaluate(
                """() => {
                    const job = {render_duration_s: 1.6, time_map: [
                        {output_start_s: 0, output_end_s: .8, source_start_s: 0, source_end_s: .8},
                        {output_start_s: .8, output_end_s: 1.6, source_start_s: 1.2, source_end_s: 2},
                    ]};
                    const valid = {
                        version: 1, scope: 'full-timeline', measurement: 'sampled-local-frames',
                        status: 'warn', source_sha256: 'a'.repeat(64), media_sha256: 'b'.repeat(64),
                        frame_size: {width: 360, height: 640}, sample_count: 1, measured_count: 1,
                        elapsed_s: .1, engines: {faces: 'opencv-yunet', ocr: 'macos-vision'},
                        samples: [{output_at: .2, source_at: .2, measured: true,
                            source_face_count: 1, reference_face_count: 1, visible_face_count: 1,
                            text_box_count: 0}],
                        findings: [{kind: 'visibility_uncertain', output_at: .2, source_at: .2}],
                    };
                    const variants = [
                        receipt => { receipt.findings[0].output_at = null; },
                        receipt => { receipt.findings[0].source_at = []; },
                        receipt => { receipt.samples[0].output_at = ''; },
                        receipt => { receipt.sample_count = true; },
                        receipt => { receipt.measured_count = '1'; },
                        receipt => { receipt.frame_size.width = '360'; },
                        receipt => { receipt.elapsed_s = []; },
                    ];
                    const statuses = variants.map(change => {
                        const receipt = structuredClone(valid);
                        change(receipt);
                        return fxPreviewCompositionReceipt(receipt, job).status;
                    });
                    const malformedMapJob = {render_duration_s: 1.6, time_map: [
                        {output_start_s: '0', output_end_s: .8, source_start_s: 0, source_end_s: .8},
                    ]};
                    const durationFallbackJob = {render_duration_s: null, time_map: job.time_map};
                    fxPreviewSetVisibleMedia(durationFallbackJob);
                    const malformedTimedFinding = structuredClone(valid);
                    malformedTimedFinding.findings[0].output_at = null;
                    malformedTimedFinding.findings[0].source_at = null;
                    fxPreviewRenderCompositionCheck(malformedTimedFinding, job);
                    return {
                        finiteInvalid: [null, '', [], true, '0.2'].map(fxPreviewFiniteNumber),
                        integerInvalid: [null, '', [], true, '1'].map(value => fxPreviewNonNegativeInteger(value, 8)),
                        numericZero: [fxPreviewFiniteNumber(0), fxPreviewNonNegativeInteger(0, 8)],
                        statuses,
                        malformedMapPair: fxPreviewCompositionMapPair(0.2, 0.2, malformedMapJob),
                        durationFallbackPair: fxPreviewCompositionMapPair(0.2, 0.2, durationFallbackJob),
                        visibleDuration: fxPreviewVisibleMeta.render_duration_s,
                        malformedStatus: document.querySelector('#fx-preview-composition-check').dataset.status,
                        malformedButtonCount: document.querySelectorAll(
                            '#fx-preview-composition-check-list button[data-source-at]'
                        ).length,
                    };
                }"""
            )
            self.assertEqual(strict["finiteInvalid"], [None] * 5)
            self.assertEqual(strict["integerInvalid"], [None] * 5)
            self.assertEqual(strict["numericZero"], [0, 0])
            self.assertEqual(strict["statuses"], ["unmeasured"] * 7)
            self.assertIsNone(strict["malformedMapPair"])
            self.assertAlmostEqual(strict["durationFallbackPair"]["outputAt"], 0.2)
            self.assertAlmostEqual(strict["durationFallbackPair"]["sourceAt"], 0.2)
            self.assertIsNone(strict["visibleDuration"])
            self.assertEqual(strict["malformedStatus"], "unmeasured")
            self.assertEqual(strict["malformedButtonCount"], 0)
            page.evaluate("() => { fxCompareActive = true; fxCompareRender(); }")
            self.assertFalse(switch.is_visible())
            browser.close()

    def test_identical_compare_poll_preserves_focus_and_count_bounds_match_server(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            self.open_ready_preview(page)
            receipt = {
                "version": 1,
                "scope": "full-timeline",
                "measurement": "sampled-local-frames",
                "status": "warn",
                "source_sha256": "a" * 64,
                "media_sha256": "b" * 64,
                "frame_size": {"width": 360, "height": 640},
                "sample_count": 1,
                "measured_count": 1,
                "elapsed_s": 0.4,
                "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
                "samples": [{
                    "output_at": 0.9, "source_at": 1.3, "measured": True,
                    "source_face_count": 1, "reference_face_count": 1,
                    "visible_face_count": 1, "text_box_count": 1,
                }],
                "findings": [{"kind": "visibility_uncertain", "output_at": 0.9, "source_at": 1.3}],
            }
            self.install_job(page, receipt, job_id="composition-focus-stable")
            result = page.evaluate(
                """async () => {
                    const list = document.querySelector('#fx-preview-composition-check-list');
                    const panel = document.querySelector('#fx-preview-composition-check');
                    const observeRender = async action => {
                        const records = [];
                        const observer = new MutationObserver(items => records.push(...items));
                        observer.observe(panel, {subtree: true, childList: true, characterData: true});
                        action();
                        await new Promise(resolve => setTimeout(resolve, 0));
                        observer.disconnect();
                        return records.length;
                    };

                    fxCompareActive = true;
                    fxCompareRender();
                    const stableBefore = list.querySelector('button[data-source-at]');
                    stableBefore.focus();
                    const stableMutations = await observeRender(() => fxCompareRender());
                    const stableAfter = list.querySelector('button[data-source-at]');
                    const stableFocusPreserved = document.activeElement === stableAfter;

                    const bindingJob = structuredClone(fxPreviewVisibleJob);
                    const highCounts = structuredClone(fxPreviewVisibleMeta.composition_check);
                    highCounts.status = 'pass';
                    highCounts.findings = [];
                    Object.assign(highCounts.samples[0], {
                        source_face_count: 65,
                        reference_face_count: 65,
                        visible_face_count: 65,
                        text_box_count: 129,
                    });
                    const maximumCounts = structuredClone(highCounts);
                    Object.assign(maximumCounts.samples[0], {
                        source_face_count: 10000,
                        reference_face_count: 10000,
                        visible_face_count: 10000,
                        text_box_count: 10000,
                    });
                    const tooHighStatuses = [
                        'source_face_count', 'reference_face_count', 'visible_face_count', 'text_box_count',
                    ].map(key => {
                        const candidate = structuredClone(highCounts);
                        candidate.samples[0][key] = 10001;
                        return fxPreviewCompositionReceipt(candidate, bindingJob).status;
                    });

                    fxPreviewVisibleMeta.composition_check.findings = [
                        {kind: 'visibility_uncertain', output_at: 1.0, source_at: 1.4},
                    ];
                    const beforeReceiptChange = stableAfter;
                    const receiptMutations = await observeRender(() => fxCompareRender());
                    const afterReceiptChange = list.querySelector('button[data-source-at]');

                    const beforeJobChange = afterReceiptChange;
                    fxPreviewVisibleJob.id = 'composition-focus-changed';
                    await observeRender(() => fxCompareRender());
                    const afterJobChange = list.querySelector('button[data-source-at]');

                    const beforeTokenChange = afterJobChange;
                    fxPreviewVisibleMediaToken += 1;
                    await observeRender(() => fxCompareRender());
                    const afterTokenChange = list.querySelector('button[data-source-at]');

                    const beforeEligibilityChange = afterTokenChange;
                    fxCompareActive = false;
                    await observeRender(() => fxCompareRender());
                    const afterEligibilityChange = list.querySelector('button[data-source-at]');

                    fxPreviewClearVisibleMedia();
                    return {
                        stable: {
                            sameButton: stableBefore === stableAfter,
                            focusPreserved: stableFocusPreserved,
                            mutations: stableMutations,
                        },
                        receiptChange: {
                            replaced: beforeReceiptChange !== afterReceiptChange,
                            mutations: receiptMutations,
                            text: afterReceiptChange?.textContent || '',
                        },
                        jobChanged: beforeJobChange !== afterJobChange,
                        tokenChanged: beforeTokenChange !== afterTokenChange,
                        eligibilityChanged: beforeEligibilityChange !== afterEligibilityChange,
                        highCountStatus: fxPreviewCompositionReceipt(highCounts, bindingJob).status,
                        maximumCountStatus: fxPreviewCompositionReceipt(maximumCounts, bindingJob).status,
                        tooHighStatuses,
                        keyCleared: fxPreviewCompositionRenderKey === '',
                        panelHidden: panel.hidden,
                    };
                }"""
            )
            self.assertEqual(result["stable"], {
                "sameButton": True,
                "focusPreserved": True,
                "mutations": 0,
            })
            self.assertTrue(result["receiptChange"]["replaced"])
            self.assertGreater(result["receiptChange"]["mutations"], 0)
            self.assertIn("00:01.400", result["receiptChange"]["text"])
            self.assertTrue(result["jobChanged"])
            self.assertTrue(result["tokenChanged"])
            self.assertTrue(result["eligibilityChanged"])
            self.assertEqual(result["highCountStatus"], "pass")
            self.assertEqual(result["maximumCountStatus"], "pass")
            self.assertEqual(result["tooHighStatuses"], ["unmeasured"] * 4)
            self.assertTrue(result["keyCleared"])
            self.assertTrue(result["panelHidden"])
            browser.close()

    def test_old_issue_and_late_metadata_cannot_move_new_candidate(self):
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True, args=["--mute-audio"])
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            _fx_preview_browser.FxPreviewBrowserTests.mock_preview_api(self, page)
            self.open_ready_preview(page)
            old_receipt = {
                "version": 1,
                "scope": "full-timeline",
                "measurement": "sampled-local-frames",
                "status": "warn",
                "source_sha256": "a" * 64,
                "media_sha256": "b" * 64,
                "frame_size": {"width": 360, "height": 640},
                "sample_count": 1,
                "measured_count": 1,
                "elapsed_s": 0.4,
                "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
                "samples": [{
                    "output_at": 0.9, "source_at": 1.3, "measured": True,
                    "source_face_count": 1, "reference_face_count": 1,
                    "visible_face_count": 1, "text_box_count": 0,
                }],
                "findings": [{"kind": "visibility_uncertain", "output_at": 0.9, "source_at": 1.3}],
            }
            new_receipt = dict(old_receipt)
            new_receipt["findings"] = [{"kind": "visibility_uncertain", "output_at": 0.2, "source_at": 0.2}]
            self.install_job(page, old_receipt, job_id="composition-old")
            page.evaluate("""() => { window.__oldCompositionIssue = document.querySelector(
                '#fx-preview-composition-check-list button[data-source-at]'); }""")

            # Hold the old loadedmetadata callback, then make it arrive after a
            # different job is visible. The real source remains loaded so time
            # writes below are observable rather than media-load noise.
            captured = page.evaluate(
                """() => {
                    const source = document.querySelector('#fx-preview-source-video');
                    const originalAdd = source.addEventListener;
                    let oldHandler = null;
                    Object.defineProperty(source, 'readyState', {configurable: true, value: 0});
                    source.addEventListener = function(type, callback, options) {
                        if (type === 'loadedmetadata') { oldHandler = callback; return; }
                        return originalAdd.call(this, type, callback, options);
                    };
                    fxPreviewPrepareSourceVideo();
                    source.addEventListener = originalAdd;
                    delete source.readyState;
                    window.__oldCompositionMetadata = oldHandler;
                    return Boolean(oldHandler);
                }"""
            )
            self.assertTrue(captured)
            self.install_job(page, new_receipt, job_id="composition-new")
            result = page.evaluate(
                """() => {
                    const source = document.querySelector('#fx-preview-source-video');
                    const preview = document.querySelector('#fx-preview-video');
                    source.pause();
                    preview.pause();
                    fxPreviewSyncing = true;
                    source.currentTime = 1.4;
                    preview.currentTime = 0.4;
                    fxPreviewSyncing = false;
                    const before = {source: source.currentTime, preview: preview.currentTime,
                        job: fxPreviewVisibleJob?.id};
                    source.addEventListener('loadedmetadata', window.__oldCompositionMetadata, {once: true});
                    source.dispatchEvent(new Event('loadedmetadata'));
                    window.__oldCompositionIssue?.click();
                    return {
                        before,
                        after: {source: source.currentTime, preview: preview.currentTime,
                            job: fxPreviewVisibleJob?.id},
                    };
                }"""
            )
            self.assertEqual(result["before"]["job"], "composition-new")
            self.assertEqual(result["after"]["job"], "composition-new")
            self.assertAlmostEqual(
                result["after"]["source"], result["before"]["source"], delta=0.12,
                msg=f"before={result['before']} after={result['after']}",
            )
            self.assertAlmostEqual(
                result["after"]["preview"], result["before"]["preview"], delta=0.12,
                msg=f"before={result['before']} after={result['after']}",
            )
            browser.close()


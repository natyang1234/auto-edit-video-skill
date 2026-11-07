from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import unittest

from test_fx_preview_browser import FxPreviewBrowserTests, CHROME, sync_playwright

ART = Path(os.environ.get('FX_PANEL_EVIDENCE_DIR', tempfile.mkdtemp(prefix='fx-panel-evidence-')))

@unittest.skipUnless(sync_playwright and CHROME.is_file(), 'Playwright Chrome is unavailable')
class FxPreviewPanelTests(unittest.TestCase):
    setUp = FxPreviewBrowserTests.setUp
    write_json = FxPreviewBrowserTests.write_json
    stop_server = FxPreviewBrowserTests.stop_server
    open_preview = FxPreviewBrowserTests.open_preview
    mock_preview_api = FxPreviewBrowserTests.mock_preview_api

    def test_panel_responsive_keyboard_and_opacity(self):
        ART.mkdir(parents=True, exist_ok=True)
        host, port = self.server.server_address
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True, args=['--mute-audio'])
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.goto(f'http://{host}:{port}/', wait_until='networkidle')
            posted, _, _ = self.mock_preview_api(page)
            for width in (1440, 1100, 390):
                page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 900})
                self.open_preview(page)
                page.wait_for_function("() => !document.querySelector('#fx-preview-confirm').disabled")
                self.assertEqual(page.locator('#fx-preview-selected-name').count(), 1)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertLessEqual(page.locator('#fx-preview-dialog').evaluate('el => el.scrollWidth - el.clientWidth'), 1)
                page.screenshot(path=str(ART / f'after-{width}-initial.png'))
                summary = page.locator('.fx-preview-persistent-summary')
                before = summary.bounding_box()
                page.locator('.fx-preview-editor-scroll').evaluate('(el) => el.scrollTop = el.scrollHeight')
                after = summary.bounding_box()
                self.assertAlmostEqual(before['y'], after['y'], delta=1)
                self.assertTrue(page.locator('#fx-preview-confirm').is_visible())
                page.screenshot(path=str(ART / f'after-{width}.png'))
                (ART / f'after-{width}.html').write_text(page.content(), encoding='utf-8')
                if width == 390:
                    catalog = page.locator('#fx-preview-catalog-disclosure')
                    self.assertFalse(catalog.evaluate('el => el.open'))
                    self.assertEqual(page.evaluate('document.activeElement.tagName'), 'SUMMARY')
                    catalog.locator('summary').press('Enter')
                    self.assertTrue(catalog.evaluate('el => el.open'))
                    catalog.locator('summary').press('Space')
                    self.assertFalse(catalog.evaluate('el => el.open'))
                advanced = page.locator('.fx-preview-advanced')
                self.assertFalse(advanced.evaluate('el => el.open'))
                advanced.locator('summary').press('Enter')
                self.assertTrue(advanced.evaluate('el => el.open'))
                advanced.locator('summary').press('Space')
                self.assertFalse(advanced.evaluate('el => el.open'))
                details = page.locator('.fx-preview-window')
                details.locator('summary').press('Enter')
                self.assertTrue(details.evaluate('el => el.open'))
                page.locator('#fx-preview-apply-duration').fill('1')
                page.locator('#fx-preview-apply-duration').dispatch_event('input')
                self.assertIn('00:01.000', page.locator('#fx-preview-apply-window-label').inner_text())
                details.locator('summary').press('Space')
                self.assertFalse(details.evaluate('el => el.open'))
                for value in (0, 50, 100):
                    slider = page.locator('.fx-preview-opacity input')
                    slider.evaluate('(el, value) => { el.value = value; el.dispatchEvent(new Event("input", {bubbles: true})); }', value)
                    self.assertEqual(page.locator('.fx-preview-opacity output').inner_text(), f'{value}%')
                    page.wait_for_function('v => !document.querySelector("#fx-preview-confirm").disabled && fxPreviewDraft.opacity === v', arg=value / 100)
                    self.assertEqual(posted[-1]['opacity'], value / 100)
                if width == 1440:
                    previous = page.locator('#fx-preview-selected-name').inner_text()
                    page.locator('#fx-preview-search').fill('H1')
                    page.locator('#fx-preview-grid .fx-preview-card').first.click()
                    self.assertNotEqual(previous, page.locator('#fx-preview-selected-name').inner_text())
                page.keyboard.press('Escape')
                self.assertTrue(page.locator('[data-add-type="fx"]').evaluate('el => el === document.activeElement'))
            self.open_preview(page)
            slider = page.locator('.fx-preview-opacity input')
            slider.evaluate('(el) => { el.value = 0; el.dispatchEvent(new Event("input", {bubbles: true})); }')
            page.keyboard.press('Escape')
            self.open_preview(page)
            self.assertEqual(page.locator('.fx-preview-opacity input').input_value(), '0')
            page.keyboard.press('Escape')
            browser.close()

    def test_confirmed_opacity_survives_form_and_replace_draft_is_explicit(self):
        host, port = self.server.server_address
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True, args=['--mute-audio'])
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.goto(f'http://{host}:{port}/', wait_until='networkidle')
            posted, _, _ = self.mock_preview_api(page)
            saved_states = []
            def save_route(route):
                if route.request.method != 'PUT':
                    route.fallback()
                    return
                saved_states.append(json.loads(route.request.post_data))
                route.fulfill(json={'revision': 'd' * 64, 'updated_at': 'test', 'invalidated_gates': []})
            page.route('**/api/editor-state', save_route)
            for percent in (0, 50):
                self.open_preview(page)
                slider = page.locator('.fx-preview-opacity input')
                slider.focus()
                slider.press('Home')
                if percent:
                    for _ in range(5):
                        slider.press('PageUp')
                self.assertEqual(slider.input_value(), str(percent))
                page.wait_for_function('v => !document.querySelector("#fx-preview-confirm").disabled && fxPreviewDraft.opacity === v', arg=percent / 100)
                self.assertEqual(posted[-1]['opacity'], percent / 100)
                page.locator('#fx-preview-confirm').click()
                page.wait_for_function('() => !document.querySelector("#fx-preview-dialog").open')
                self.assertEqual(page.evaluate('currentOverlay().style.opacity'), percent / 100)
                page.locator('#overlay-start').fill('0.2')
                page.locator('#overlay-visible').uncheck()
                self.assertEqual(page.evaluate('currentOverlay().style.opacity'), percent / 100)
                page.locator('#overlay-visible').check()
                self.assertEqual(page.evaluate('currentOverlay().style.opacity'), percent / 100)
                page.evaluate('() => saveState(false)')
                page.wait_for_function('() => !stateDirty')
                saved_fx = next(item for item in saved_states[-1]['overlays'] if item['id'] == page.evaluate('selectedOverlayId'))
                self.assertEqual(saved_fx['style']['opacity'], percent / 100)
                self.assertEqual(saved_fx['style']['width'], 100)
                # Install a same-revision draft which disagrees with the timeline.
                page.evaluate('''() => {
                    const overlay = currentOverlay();
                    const key = `${fxPreviewProjectIdentity()}::${overlay.id}`;
                    const draft = fxPreviewBuildDraft(fxEffectByCode(overlay.code), null, overlay, 0);
                    draft.opacity = 0.75;
                    localStorage.setItem(FX_PREVIEW_DRAFTS_KEY, JSON.stringify({
                        [key]: {revision: fxPreviewCurrentRevision(), draft}
                    }));
                    openFxPreviewDialog(document.querySelector('[data-add-type="fx"]'), overlay.id);
                }''')
                self.assertEqual(page.locator('.fx-preview-opacity input').input_value(), str(percent))
                self.assertTrue(page.locator('#fx-preview-restore-draft').is_visible())
                page.wait_for_function('() => !document.querySelector("#fx-preview-confirm").disabled')
                self.assertEqual(posted[-1]['opacity'], percent / 100)
                page.locator('#fx-preview-restore-draft').click()
                self.assertEqual(page.locator('.fx-preview-opacity input').input_value(), '75')
                page.keyboard.press('Escape')
            # Invalid persisted values never coerce; a valid overlay is fallback, then 1.
            self.assertEqual(page.evaluate('''() => {
                const effect = fxEffects().find(e => e.available !== false);
                return [true, null, '0', -1, 2, NaN, Infinity].map(opacity => [
                    fxPreviewBuildDraft(effect, {opacity}, {style: {opacity: 0}}, 0).opacity,
                    fxPreviewBuildDraft(effect, {opacity}, null, 0).opacity
                ]);
            }'''), [[0, 1]] * 7)
            browser.close()

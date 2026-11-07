from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_editor_browser as base


@unittest.skipUnless(base.sync_playwright and base.CHROME.is_file(), "Playwright Chrome unavailable")
class PresetBrowserTests(unittest.TestCase):
    """Real Chrome and isolated temporary project; preset and FX APIs are mocked."""
    write_json = base.EditorBrowserSmokeTests.write_json
    stop_server = base.EditorBrowserSmokeTests.stop_server

    def setUp(self):
        store = tempfile.TemporaryDirectory(prefix="preset-browser-store-")
        self.addCleanup(store.cleanup)
        self.env = patch.dict(os.environ, {"AUTO_EDIT_PRESETS_DIR": str(Path(store.name).resolve() / "presets")})
        self.env.start()
        self.addCleanup(self.env.stop)
        base.EditorBrowserSmokeTests.setUp(self)

    def test_library_diff_drift_and_fx(self):
        host, port = self.server.server_address
        records = []
        posts = []
        rev = ["lib-1"]
        failed = [False]
        apply_state = [None]
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            def route_handler(route):
                req = route.request
                path = urlparse(req.url).path
                if path.startswith("/api/fx-previews"):
                    if req.method == "POST" and path == "/api/fx-previews":
                        posts.append(json.loads(req.post_data))
                        route.fulfill(json={"ok": True, "job": {"id": "mock", "state": "failed", "error": "mock preview"}})
                    else:
                        route.fulfill(json={"ok": True})
                    return
                if not path.startswith("/api/style-presets"):
                    route.fallback(); return
                body = json.loads(req.post_data or "{}")
                if failed[0] and req.method != "GET":
                    route.fulfill(status=409, json={"ok": False, "error": "conflict"}); return
                if path.endswith("caption-plan"):
                    route.fulfill(json={"ok": True, "plan_digest": "digest", "changes": [{"field": "style.color", "before": "#111111", "after": "#FFFFFF"}], "applied_to": [body["overlay_id"]]}); return
                if path.endswith("caption-apply"):
                    if apply_state[0] is None:
                        route.fulfill(json={"ok": True, "no_change": True})
                    else:
                        updated = copy.deepcopy(apply_state[0])
                        updated["overlays"][0]["style"]["color"] = "#FFFFFF"
                        route.fulfill(json={"ok": True, "state": updated, "revision": updated["revision"], "invalidated_gates": [], "applied_to": [body["overlay_id"]], "no_change": False})
                    return
                if req.method == "POST":
                    record = {"id": "p1", "name": body["name"], "kind": body["kind"], "settings": body["settings"]}
                    records.append(record); rev[0] = "lib-2"
                elif req.method == "PUT":
                    records[0].update({key: body[key] for key in ("name", "kind", "settings")})
                elif req.method == "DELETE":
                    records.clear()
                route.fulfill(json={"ok": True, "revision": rev[0], "presets": records})
            page.route("**/api/**", route_handler)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.locator("#style-presets summary").click()
            for width in (390, 1100, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.screenshot(path=str(Path(os.environ.get("P3_UI_EVIDENCE", self._tmp.name)) / f"library-{width}.png"), full_page=True)
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), width)
            page.set_viewport_size({"width": 1100, "height": 900})
            page.locator("#preset-name").focus()
            page.keyboard.press("Tab")
            self.assertEqual(page.evaluate("() => document.activeElement.id"), "preset-list")
            page.locator("#preset-name").fill("我的字幕")
            page.locator("#preset-create").click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('已儲存預設')")
            self.assertEqual(records[0]["kind"], "caption")
            self.assertNotIn("asset_id", json.dumps(records[0]["settings"]))
            page.locator("#preset-list").select_option("p1")
            page.locator("#preset-apply").click()
            page.locator("#preset-review[open]").wait_for()
            self.assertIn("文字顏色", page.locator("#preset-review-details").inner_text())
            page.locator("#preset-review-cancel").click()
            page.locator("#preset-apply").click(); page.locator("#preset-review[open]").wait_for()
            page.locator("#preset-review-confirm").click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('未新增撤銷')")
            apply_state[0] = page.evaluate("() => JSON.parse(JSON.stringify(state))")
            history_count = page.evaluate("() => history.length")
            page.locator("#preset-apply").click(); page.locator("#preset-review-confirm").click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('可使用撤銷')")
            self.assertEqual(page.evaluate("() => history.length"), history_count + 1)
            page.keyboard.press("Control+z")
            self.assertEqual(page.evaluate("() => state.overlays[0].style.color"), apply_state[0]["overlays"][0]["style"]["color"])
            page.wait_for_timeout(1000)
            page.keyboard.press("Control+Shift+z")
            self.assertEqual(page.evaluate("() => state.overlays[0].style.color"), "#FFFFFF")
            self.assertEqual(page.evaluate("() => history.length"), history_count + 1)
            self.assertEqual(page.evaluate("() => redoStack.length"), 0)
            page.wait_for_timeout(1000)
            page.locator("#preset-apply").click(); page.locator("#preset-review[open]").wait_for()
            page.evaluate("editGeneration += 1")
            page.locator("#preset-review-confirm").click()
            self.assertIn("重新查看差異", page.locator("#preset-status").inner_text())
            failed[0] = True
            page.locator("#preset-name").fill("保留輸入")
            page.locator("#preset-update").click(); page.locator("#preset-review-confirm").click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('conflict')")
            self.assertIn("conflict", page.locator("#preset-status").inner_text())
            self.assertEqual(page.locator("#preset-name").input_value(), "保留輸入")
            failed[0] = False
            page.locator("#preset-delete").click()
            self.assertIn("我的字幕", page.locator("#preset-review-title").inner_text())
            page.locator("#preset-review-cancel").click()
            self.assertEqual(len(records), 1)
            page.locator("#preset-delete").click(); page.locator("#preset-review-confirm").click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('已刪除')")
            self.assertEqual(len(records), 0)
            records[:] = [{"id": "fx", "name": "特效預設", "kind": "fx", "settings": {"code": "T3", "values": {}, "placement": "", "scheme": "", "font_ids": {}, "opacity": 0, "text_fit": True}}]
            page.locator("#style-presets summary").click(); page.locator("#style-presets summary").click()
            page.wait_for_function("() => document.querySelector('#preset-list option[value=fx]')")
            page.locator("#preset-list").select_option("fx")
            page.locator("#preset-apply").click(); page.locator("#preset-review-confirm").click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.wait_for_timeout(700)
            self.assertEqual(posts[-1]["opacity"], 0)
            self.assertNotIn("material", posts[-1]["candidate"])
            self.assertNotIn("subject", posts[-1]["candidate"])
            page.locator("#fx-preview-cancel").click()
            records[0]["settings"]["opacity"] = 0.5
            page.evaluate("""() => {
                const key = `${fxPreviewProjectIdentity()}::new`;
                localStorage.setItem(FX_PREVIEW_DRAFTS_KEY, JSON.stringify({[key]: {revision: state.revision, draft: {code:'H1', opacity:1}}}));
            }""")
            page.locator("#style-presets summary").click(); page.locator("#style-presets summary").click()
            page.wait_for_function("() => document.querySelector('#preset-list option[value=fx]')")
            page.locator("#preset-list").select_option("fx")
            page.locator("#preset-apply").click(); page.locator("#preset-review-confirm").click()
            page.locator("#fx-preview-dialog[open]").wait_for()
            page.wait_for_timeout(700)
            self.assertEqual(posts[-1]["opacity"], 0.5)
            self.assertEqual(posts[-1]["candidate"]["code"], "T3")
            self.assertTrue(page.locator("#fx-preview-restore-draft").is_visible())
            for width in (390, 1100, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.screenshot(path=str(Path(os.environ.get("P3_UI_EVIDENCE", self._tmp.name)) / f"preset-{width}.png"), full_page=True)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
            browser.close()

    def test_422_failures_preserve_input_state_and_history(self):
        host, port = self.server.server_address
        preset_id = "preset_" + "a" * 32
        settings = {"style": {"color": "#FFFFFF"}, "font": {"sha256": "b" * 64, "family": "Test", "style": "Regular", "weight": 400}}
        record = {"id": preset_id, "name": "既有預設", "kind": "caption", "settings": settings}
        failure = ["create"]
        bodies = []
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            def route_handler(route):
                req = route.request
                path = urlparse(req.url).path
                if not path.startswith("/api/style-presets"):
                    route.fallback(); return
                body = json.loads(req.post_data or "{}")
                if req.method == "GET":
                    route.fulfill(json={"ok": True, "revision": "lib-revision", "presets": [record]}); return
                if path.endswith("caption-plan"):
                    action = "plan"
                    self.assertEqual(set(body), {"preset_id", "expected_library_revision", "expected_revision", "overlay_id", "scope"})
                elif path.endswith("caption-apply"):
                    action = "apply"
                    self.assertEqual(set(body), {"preset_id", "expected_library_revision", "expected_revision", "overlay_id", "scope", "plan_digest"})
                    self.assertEqual(body["plan_digest"], "plan-digest")
                elif req.method == "PUT": action = "update"
                else: action = "create"
                bodies.append((action, body))
                if action in ("create", "update"):
                    self.assertEqual(set(body), {"expected_library_revision", "name", "kind", "settings"})
                if action == failure[0]:
                    msg = "目標專案缺少字型，請匯入" if action == "plan" else f"{action} 驗證失敗"
                    route.fulfill(status=422, json={"ok": False, "reason": "font_missing" if action == "plan" else "settings_invalid", "error": msg}); return
                if action == "plan":
                    route.fulfill(json={"ok": True, "preset_id": preset_id, "preset_name": record["name"], "library_revision": "lib-revision", "project_revision": body["expected_revision"], "plan_digest": "plan-digest", "changes": [{"overlay_id": body["overlay_id"], "field": "style.color", "before": "#000000", "after": "#FFFFFF"}], "applied_to": [body["overlay_id"]], "no_change": False}); return
                self.fail(f"Unexpected successful mutation: {action}")
            page.route("**/api/**", route_handler)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.locator("#style-presets summary").click()
            page.wait_for_function("() => document.querySelector('#preset-list option[value^=preset_]')")
            page.wait_for_load_state("networkidle")
            selected_after_load = page.evaluate("""async () => {
                const oldRequest = request;
                let release;
                request = () => new Promise(resolve => { release = resolve; });
                const pending = presetLoad();
                const list = document.querySelector('#preset-list');
                list.value = list.options[1].value;
                release({revision: presetLibrary.revision, presets: presetLibrary.presets});
                await pending;
                request = oldRequest;
                return list.value;
            }""")
            self.assertEqual(selected_after_load, preset_id)
            page.locator("#preset-list").select_option(preset_id)
            page.locator("#preset-name").fill("請保留這個名稱")
            baseline = page.evaluate("() => ({state: JSON.stringify(state), history: JSON.stringify(history), redo: JSON.stringify(redoStack)})")
            for action in ("create", "update", "plan", "apply"):
                failure[0] = action
                page.locator("#preset-create" if action == "create" else "#preset-update" if action == "update" else "#preset-apply").click()
                if action in ("update", "apply"):
                    try:
                        page.locator("#preset-review[open]").wait_for(timeout=4000)
                    except Exception:
                        print("review failure", action, page.locator("#preset-status").inner_text(), page.locator("#preset-list").input_value(), flush=True)
                        raise
                    page.locator("#preset-review-confirm").click()
                expected = "缺少字型" if action == "plan" else f"{action} 驗證失敗"
                page.wait_for_function("message => document.querySelector('#preset-status').textContent.includes(message)", arg=expected)
                self.assertEqual(page.locator("#preset-name").input_value(), "請保留這個名稱")
                self.assertEqual(page.evaluate("() => ({state: JSON.stringify(state), history: JSON.stringify(history), redo: JSON.stringify(redoStack)})"), baseline)
                self.assertFalse(page.locator("#preset-review").evaluate("el => el.open"))
            self.assertEqual([action for action, _ in bodies], ["create", "update", "plan", "plan", "apply"])
            browser.close()

    def test_real_handler_caption_payload_contract(self):
        """Temporary real preset store; no font or media render is exercised."""
        host, port = self.server.server_address
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.locator("#style-presets summary").click()
            page.wait_for_load_state("networkidle")
            page.locator("#preset-name").fill("契約測試")
            with page.expect_response(lambda r: urlparse(r.url).path == "/api/style-presets" and r.request.method == "POST") as pending:
                page.locator("#preset-create").click()
            response = pending.value
            self.assertEqual(response.status, 201, response.text())
            created = response.json()
            self.assertTrue(created["ok"])
            self.assertEqual(created["preset"]["settings"]["font"], None)
            self.assertNotIn("asset_id", json.dumps(created["preset"]["settings"]))
            preset_id = created["preset"]["id"]
            page.locator("#preset-list").select_option(preset_id)
            with page.expect_response(lambda r: urlparse(r.url).path.endswith("caption-plan")) as pending:
                page.locator("#preset-apply").click()
            response = pending.value
            self.assertEqual(response.status, 200, response.text())
            plan = response.json()
            self.assertEqual(set(plan), {"ok", "preset_id", "preset_name", "library_revision", "project_revision", "plan_digest", "changes", "applied_to", "no_change"})
            page.locator("#preset-review[open]").wait_for()
            with page.expect_response(lambda r: urlparse(r.url).path.endswith("caption-apply")) as pending:
                page.locator("#preset-review-confirm").click()
            response = pending.value
            self.assertEqual(response.status, 200, response.text())
            self.assertTrue(response.json()["ok"])
            self.assertIn("no_change", response.json())
            browser.close()

    def test_diff_hides_font_hash_and_uses_readable_labels(self):
        host, port = self.server.server_address
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            values = page.evaluate("""() => {
                const a = {sha256:'a'.repeat(64),family:'Font A',style:'Regular',weight:400};
                const b = {sha256:'b'.repeat(64),family:'Font B',style:'Bold',weight:700};
                const c = {...a,sha256:'c'.repeat(64)};
                const named = presetDiff({font:a},{font:b});
                const hashOnly = presetDiff({font:a},{font:c});
                const fx = presetDiff({code:'T3',opacity:1,text_fit:true,placement:'top'}, {code:'H1',opacity:0.5,text_fit:false,placement:'bottom'});
                document.querySelector('#preset-review-details').textContent = named + '\\n' + hashOnly + '\\n' + fx;
                return {named,hashOnly,fx};
            }""")
            self.assertIn("Font A / Regular / 400", values["named"])
            self.assertIn("Font B / Bold / 700", values["named"])
            self.assertIn("字型檔已更換", values["hashOnly"])
            self.assertIn("特效透明度", values["fx"])
            self.assertIn("文字自動調整", values["fx"])
            self.assertIn("擺位", values["fx"])
            for item in ("a" * 64, "b" * 64, "c" * 64, "sha256", "font /", "text_fit", "opacity"):
                self.assertNotIn(item, page.locator("#preset-review-details").inner_text())
            browser.close()

    def test_unavailable_preset_and_old_font_display(self):
        host, port = self.server.server_address
        fx_id = 'preset_' + '1' * 32
        caption_id = 'preset_' + '2' * 32
        fx = {'id': fx_id, 'name': '舊效果', 'kind': 'fx', 'settings': {'code': 'T3', 'values': {}, 'placement': '', 'scheme': '', 'font_ids': {}, 'opacity': 0.5, 'text_fit': True}, 'availability': {'status': 'unavailable', 'reason': 'font_unavailable', 'message': '缺少字型，請更新或刪除。'}}
        cap = {'id': caption_id, 'name': '字幕', 'kind': 'caption', 'settings': {'style': {'color': '#FFFFFF'}, 'font': {'sha256': 'a' * 64, 'family': '新字型', 'style': 'Regular', 'weight': 400}}, 'availability': {'status': 'available', 'reason': None, 'message': None}}
        records = [fx, cap]
        calls = []
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={'width': 1100, 'height': 900})
            def handler(route):
                req = route.request; path = urlparse(req.url).path
                if not path.startswith('/api/style-presets'):
                    route.fallback(); return
                body = json.loads(req.post_data or '{}'); calls.append((req.method, path, body))
                if path.endswith('caption-plan'):
                    route.fulfill(json={'ok': True, 'plan_digest': 'd', 'changes': [{'overlay_id': body['overlay_id'], 'field': 'font', 'before': None, 'before_unavailable': True, 'after': cap['settings']['font']}], 'applied_to': [body['overlay_id']], 'no_change': False}); return
                if req.method == 'PUT':
                    self.assertNotIn('availability', body)
                    self.assertNotIn('availability', body['settings'])
                    records[0]['name'] = body['name']
                if req.method == 'DELETE': records.pop(0)
                route.fulfill(json={'ok': True, 'revision': 'rev', 'presets': records})
            page.route('**/api/**', handler)
            page.goto(f'http://{host}:{port}/', wait_until='networkidle')
            page.locator('#style-presets summary').click(); page.wait_for_load_state('networkidle')
            page.locator('#preset-list').select_option(fx_id)
            page.evaluate('''async () => {
              const old = request; let release;
              request = () => new Promise(resolve => { release = resolve; });
              const pending = presetLoad();
              release({revision:presetLibrary.revision,presets:presetLibrary.presets});
              await pending; request = old;
            }''')
            self.assertIn('缺字型', page.locator('#preset-list option:checked').inner_text())
            self.assertIn('缺少字型', page.locator('#preset-status').inner_text())
            page.locator('#preset-apply').click()
            self.assertIn('缺少字型', page.locator('#preset-status').inner_text())
            self.assertFalse(page.locator('#fx-preview-dialog').evaluate('el => el.open'))
            self.assertFalse(any('/api/fx-previews' in path for _, path, _ in calls))
            page.evaluate("""() => {
              const o = currentOverlay(); Object.assign(o, {type:'fx', code:'T3', values:{}, placement:'', scheme:'', font_ids:{}, text_fit:true}); o.style.opacity=0.5; renderInspector();
            }""")
            page.locator('#preset-update').click(); page.locator('#preset-review-confirm').click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('已儲存')")
            self.assertTrue(any(method == 'PUT' for method, _, _ in calls))
            page.locator('#preset-list').select_option(fx_id)
            page.locator('#preset-delete').click();page.locator('#preset-review-confirm').click()
            page.wait_for_function("() => document.querySelector('#preset-status').textContent.includes('已刪除')")
            self.assertTrue(any(method == 'DELETE' for method, _, _ in calls))
            page.evaluate("() => {currentOverlay().type='caption'; renderInspector();}")
            page.locator('#preset-list').select_option(caption_id)
            page.locator('#preset-apply').click();page.locator('#preset-review[open]').wait_for()
            diff = page.locator('#preset-review-details').inner_text()
            self.assertIn('目前字型不可用', diff)
            self.assertIn('新字型 / Regular / 400', diff)
            self.assertNotIn('a' * 64, diff)
            page.locator('#preset-review-cancel').click()
            browser.close()

    def test_dark_os_native_highlight_inputs_remain_readable(self):
        host, port = self.server.server_address
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            for theme in ('light', 'dark'):
                page = browser.new_page(color_scheme=theme, viewport={'width': 1440, 'height': 900})
                page.goto(f'http://{host}:{port}/', wait_until='networkidle')
                colors = page.evaluate("""() => ['#highlight-title','#highlight-start','#highlight-end'].map(id => {
                    const s=getComputedStyle(document.querySelector(id));
                    const rgb=(v)=>{const c=document.createElement('canvas');c.width=c.height=1;const x=c.getContext('2d');x.fillStyle=v;x.fillRect(0,0,1,1);return [...x.getImageData(0,0,1,1).data]};
                    const lum=a=>{const v=a.slice(0,3).map(n=>{n/=255;return n<=0.04045?n/12.92:((n+0.055)/1.055)**2.4});return .2126*v[0]+.7152*v[1]+.0722*v[2]};
                    const a=lum(rgb(s.color)),b=lum(rgb(s.backgroundColor));return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
                })""")
                self.assertTrue(all(value >= 4.5 for value in colors), (theme, colors))
                page.close()
            browser.close()

    def test_real_handler_unavailable_catalog_drift_remains_deletable(self):
        """Simulate catalog drift only in a private test store; real handler and UI."""
        host, port = self.server.server_address
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={'width': 1100, 'height': 900})
            page.goto(f'http://{host}:{port}/', wait_until='networkidle')
            saved = page.evaluate("""async () => {
              const lib=await request('/api/style-presets');
              return request('/api/style-presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_library_revision:lib.revision,name:'舊目錄效果',kind:'fx',settings:{code:'T3',values:{},placement:'',scheme:'',font_ids:{},opacity:0.5,text_fit:true}})});
            }""")
            preset_id = saved['preset']['id']
            store = Path(os.environ['AUTO_EDIT_PRESETS_DIR']) / 'presets.json'
            document = json.loads(store.read_text())
            document['presets'][0]['settings']['code'] = 'Z999'
            store.write_text(json.dumps(document, ensure_ascii=False))
            response = page.evaluate("() => request('/api/style-presets')")
            self.assertEqual(response['presets'][0]['availability']['status'], 'unavailable')
            self.assertEqual(response['presets'][0]['availability']['reason'], 'effect_unavailable')
            page.locator('#style-presets summary').click()
            page.wait_for_function("id => [...document.querySelector('#preset-list').options].some(o=>o.value===id)", arg=preset_id)
            page.locator('#preset-list').select_option(preset_id)
            self.assertIn('暫時不可用', page.locator('#preset-list option:checked').inner_text())
            page.locator('#preset-apply').click()
            self.assertFalse(page.locator('#fx-preview-dialog').evaluate('e=>e.open'))
            with page.expect_response(lambda r: r.request.method == 'DELETE' and urlparse(r.url).path.endswith(preset_id)) as pending:
                page.locator('#preset-delete').click(); page.locator('#preset-review-confirm').click()
            self.assertEqual(pending.value.status, 200, pending.value.text())
            self.assertEqual(page.evaluate("async () => (await request('/api/style-presets')).presets.length"), 0)
            browser.close()

    def test_late_load_cannot_overwrite_successful_mutations(self):
        host, port = self.server.server_address
        with base.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(base.CHROME), headless=True)
            page = browser.new_page(viewport={'width': 1100, 'height': 900})
            page.goto(f'http://{host}:{port}/', wait_until='networkidle')
            results = page.evaluate("""async () => {
              const oldRequest = request, oldReview = presetReview;
              const item = {id:'p',name:'舊預設',kind:'caption',settings:{style:{color:'#FFFFFF'},font:null}};
              const fresh = {id:'n',name:'新預設',kind:'caption',settings:{style:{color:'#FFFFFF'},font:null}};
              const results = [];
              presetReview = async () => true;
              for (const method of ['POST','PUT','DELETE']) {
                const old = {revision:'old',presets:[item]};
                const latest = {revision:'new-revision',presets:method==='DELETE'?[]:method==='PUT'?[{...item,name:'更新預設'}]:[item,fresh]};
                presetSetLibrary(old,'p');
                document.querySelector('#preset-name').value='新預設';
                let release;
                request = (url, options={}) => options.method === method
                  ? Promise.resolve(latest)
                  : new Promise(resolve=>{release=resolve});
                const pending = presetLoad();
                await presetMutate(method);
                const list=document.querySelector('#preset-list');
                list.value=latest.presets.at(-1)?.id || '';
                const expectedStatus=document.querySelector('#preset-status').textContent;
                release(old);
                await pending;
                results.push({method,revision:presetLibrary.revision,names:presetLibrary.presets.map(p=>p.name),selection:list.value,status:document.querySelector('#preset-status').textContent,expectedStatus});
              }
              request = oldRequest; presetReview = oldReview;
              return results;
            }""")
            expected = {'POST': (['舊預設', '新預設'], 'n'), 'PUT': (['更新預設'], 'p'), 'DELETE': ([], '')}
            for result in results:
                self.assertEqual(result['revision'], 'new-revision')
                self.assertEqual((result['names'], result['selection']), expected[result['method']])
                self.assertEqual(result['status'], result['expectedStatus'])
            browser.close()

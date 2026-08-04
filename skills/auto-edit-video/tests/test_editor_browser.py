from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
sys.path.insert(0, str(SCRIPTS_DIR))

from editor_server import EditorServer  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional local browser verification
    sync_playwright = None


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class EditorBrowserSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is unavailable")
        self._env_patch = patch.dict(os.environ, {"RUMI_VOICE_SYSTEM": str(RUMI_FIXTURE)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-browser-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        for name in ("source", "working", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        source = self.project / "source/source.mp4"
        rendered = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x3b332d:s=360x640:d=2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.write_json(
            "project.json",
            {
                "schema_version": 1,
                "project_id": "browser-smoke",
                "source": {"staged_path": "source/source.mp4", "duration_s": 2.0},
                "approvals": {},
            },
        )
        self.write_json(
            "working/transcript_words.json",
            {
                "text": "看到 It 就想到 to V",
                "segments": [
                    {"id": "segment-0001", "start": 0.02, "end": 1.8, "text": "看到 It 就想到 to V"}
                ],
            },
        )
        self.write_json(
            "working/emphasis_plan.json",
            {"items": [{"id": "em-1", "start": 0.2, "end": 0.8, "text": "It"}]},
        )
        self.write_json(
            "working/highlight_plan.json",
            {
                "schema_version": 1,
                "plan_revision": "a" * 64,
                "configuration": {"director_profile": "high-energy"},
                "items": [
                    {
                        "id": "highlight-browser",
                        "start": 0.0,
                        "end": 1.9,
                        "title": "It 作虛主詞",
                        "review_status": "approved",
                        "score": 0.9,
                    }
                ],
            },
        )
        self.write_json("working/edit_candidates.json", {"items": []})
        self.write_json("working/edit_decisions.json", {"items": []})
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def write_json(self, relative: str, payload: object) -> None:
        path = self.project / relative
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_inline_effect_position_warning_and_card_size_round_trip(self) -> None:
        host, port = self.server.server_address
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            console_errors: list[str] = []
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")
            page.locator("#layer-form:not([hidden])").wait_for()

            initial_effect_count = page.locator("#effect-span-list .effect-span-row").count()
            self.assertGreaterEqual(initial_effect_count, 1)
            page.locator("#overlay-text").evaluate(
                "el => { el.focus(); el.setSelectionRange(0, 2); el.dispatchEvent(new Event('select', {bubbles:true})); }"
            )
            page.locator("#effect-style").select_option("highlight")
            page.locator("#effect-color").fill("#00cc88")
            page.locator("#add-effect-span").click()
            # addEffectSpan now awaits the server snap endpoint; wait for the
            # new row instead of counting synchronously.
            page.locator("#effect-span-list .effect-span-row").nth(
                initial_effect_count
            ).wait_for(timeout=5000)
            self.assertEqual(
                page.locator("#effect-span-list .effect-span-row").count(),
                initial_effect_count + 1,
            )

            page.locator("#position-x").fill("62")
            page.locator("#preview-video").evaluate(
                # The reviewed full-screen hook owns 0.0–0.8s and intentionally
                # replaces captions there; inspect the caption after that card.
                "el => { el.currentTime = 1.0; el.dispatchEvent(new Event('timeupdate')); }"
            )
            page.locator(".preview-overlay mark.effect-highlight").first.wait_for()
            self.assertEqual(
                page.locator('.preview-overlay[data-overlay-id="caption-0001"]').evaluate("el => el.style.left"),
                "62%",
            )
            self.assertIn("重疊", page.locator("#layout-warning").inner_text())

            # A transformed inline effect must reserve its enlarged width in
            # normal text flow. Otherwise the painted glyphs intrude into the
            # neighbouring caption text even though DOM collision checks pass.
            pop_word = page.locator(
                '.preview-overlay[data-overlay-id="caption-0001"] mark.effect-pop'
            )
            pop_word.wait_for()
            page.wait_for_timeout(320)
            pop_metrics = pop_word.evaluate(
                "el => ({layoutWidth: el.offsetWidth, visualWidth: el.getBoundingClientRect().width})"
            )
            self.assertLessEqual(
                pop_metrics["visualWidth"],
                pop_metrics["layoutWidth"] + 1,
                pop_metrics,
            )

            page.locator('.layer-row:has-text("標題卡 · It 作虛主詞")').click()
            self.assertTrue(page.locator("#card-height-row").is_visible())
            page.locator("#overlay-max-width").fill("72")
            page.locator("#card-height").fill("33")
            page.locator("#position-y").fill("44")
            page.locator('[data-template-group="cutout"]').click()
            cutout_card = page.locator(".template-card:has-text('純色背景')")
            if cutout_card.is_enabled():
                cutout_card.click()
                self.assertTrue(page.locator("#subject-controls").is_visible())
                page.locator("#template-subject-x").fill("61")
                page.locator("#template-subject-scale").fill("1.25")
                page.locator("#template-background-color").fill("#2557a7")
                self.assertIn("定位預覽", page.locator("#template-capability-note").inner_text())
            page.screenshot(path="/private/tmp/auto-edit-gui-phase7-smoke.png", full_page=True)

            time.sleep(0.9)
            browser.close()

        state = json.loads((self.project / "working/editor_state.json").read_text(encoding="utf-8"))
        caption = next(item for item in state["overlays"] if item["id"] == "caption-0001")
        self.assertEqual(caption["style"]["x"], 62)
        self.assertTrue(any(span["style"]["effect"] == "highlight" for span in caption["effect_spans"]))
        self.assertEqual(
            next(span for span in caption["effect_spans"] if span["text"] == "It")["style"]["effect"],
            "pop",
        )
        hook = next(item for item in state["overlays"] if item.get("design_role") == "hook")
        self.assertEqual(hook["layout"]["width"], 72)
        self.assertEqual(hook["layout"]["height"], 33)
        self.assertEqual(hook["layout"]["y"], 44)
        if state["video_template"]["id"] == "cutout-solid":
            self.assertEqual(state["video_template"]["subject"]["x"], 61)
            self.assertEqual(state["video_template"]["subject"]["scale"], 1.25)
            self.assertEqual(state["video_template"]["background"]["color"], "#2557a7")
        self.assertEqual(console_errors, [])

    def test_batch_render_shows_per_clip_qa_and_gates_all_downloads(self) -> None:
        host, port = self.server.server_address
        submitted: dict[str, object] = {}
        restored: dict[str, object] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            console_errors: list[str] = []
            page.on("pageerror", lambda error: console_errors.append(str(error)))

            def accept_batch(route: object) -> None:
                request = route.request
                submitted.update(request.post_data_json)
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "status": {
                                "mode": "batch",
                                "quality": "final",
                                "state": "running",
                                "completed_clips": 0,
                                "total_clips": 1,
                                "message": "準備整批輸出",
                            },
                        },
                        ensure_ascii=False,
                    ),
                )

            def complete_batch(route: object) -> None:
                revision = str(submitted.get("expected_revision") or "")
                item = {
                    "clip_id": "highlight-browser",
                    "status": "pass",
                    "output": "renders/highlight-browser-final.mp4",
                    "download_name": "highlight-browser-final.mp4",
                    "contact_sheet": "qa/highlight-browser-contact.png",
                    "report": "qa/highlight-browser-report.json",
                    "warnings": [],
                }
                receipt = {
                    "schema_version": 2,
                    "delivery_kind": "batch",
                    "state_revision": revision,
                    "status": "pass",
                    "item_count": 1,
                    "items": [item],
                    "archive": "renders/browser-smoke-highlights.zip",
                    "archive_download_name": "browser-smoke-highlights.zip",
                    "human_review_required": True,
                }
                restored["receipt"] = receipt
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "mode": "batch",
                            "quality": "final",
                            "state": "complete",
                            "state_revision": revision,
                            "completed_clips": 1,
                            "total_clips": 1,
                            "message": "整批輸出完成",
                            "items": [item],
                            "output": "/renders/browser-smoke-highlights.zip",
                            "download_name": "browser-smoke-highlights.zip",
                            "qa": receipt,
                        },
                        ensure_ascii=False,
                    ),
                )

            def restore_project(route: object) -> None:
                response = route.fetch()
                payload = response.json()
                receipt = restored.get("receipt")
                if isinstance(receipt, dict):
                    receipt = dict(receipt)
                    receipt["state_revision"] = payload["state"]["revision"]
                    payload["delivery_qa"] = receipt
                    final_revision = payload["approval_revisions"]["final"]
                    payload.setdefault("manifest", {}).setdefault("approvals", {})["final"] = {
                        "approved": True,
                        "state_revision": final_revision,
                    }
                    payload.setdefault("approval_current", {})["final"] = True
                route.fulfill(
                    status=response.status,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            def approve_batch(route: object) -> None:
                payload = route.request.post_data_json
                revision = payload["expected_revision"]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "approval": {"approved": True, "state_revision": revision},
                            "approval_revisions": {"final": revision},
                            "approval_current": {"final": True},
                        }
                    ),
                )

            page.route("**/api/project", restore_project)
            page.route("**/api/render-batch", accept_batch)
            page.route("**/api/render-status", complete_batch)
            page.route("**/api/approve", approve_batch)
            page.goto(f"http://{host}:{port}/", wait_until="networkidle")

            self.assertEqual(page.locator("#batch-retained-count").inner_text(), "已保留 1 段")
            page.locator("#publish-tab").click()
            page.locator("#render-batch-final").click()
            self.assertTrue(page.locator("#render-button").is_disabled())
            self.assertTrue(page.locator("#render-final").is_disabled())
            self.assertTrue(page.locator("#render-batch-final").is_disabled())
            self.assertTrue(page.locator("#approve-final").is_disabled())
            page.locator("#batch-qa-grid .batch-qa-card").wait_for()
            self.assertEqual(submitted["quality"], "final")
            self.assertTrue(bool(submitted["expected_revision"]))
            self.assertEqual(page.locator("#batch-progress-value").inner_text(), "1 / 1")
            self.assertEqual(page.locator("#batch-qa-grid .batch-qa-card").count(), 1)
            self.assertIn("查看 QA 九宮格", page.locator("#batch-qa-grid").inner_text())
            self.assertTrue(page.locator("#batch-downloads").is_hidden())
            self.assertEqual(page.locator("#batch-output-list a").count(), 0)
            page.locator("#batch-qa-grid .batch-playback-button").click()
            self.assertIn(
                "highlight-browser-final.mp4",
                page.locator("#preview-video").get_attribute("src") or "",
            )

            page.locator("#approve-final").click()
            page.locator("#batch-downloads").wait_for(state="visible")
            self.assertTrue(page.locator("#download-batch-archive").is_visible())
            self.assertEqual(page.locator("#batch-output-list a").count(), 1)
            self.assertIn(
                "browser-smoke-highlights.zip",
                page.locator("#download-batch-archive").get_attribute("href") or "",
            )
            self.assertIn(
                "highlight-browser-final.mp4",
                page.locator("#batch-output-list a").get_attribute("href") or "",
            )

            page.reload(wait_until="networkidle")
            page.locator("#publish-tab").click()
            page.locator("#batch-downloads").wait_for(state="visible")
            self.assertEqual(page.locator("#batch-progress-value").inner_text(), "1 / 1")
            self.assertEqual(page.locator("#batch-qa-grid .batch-qa-card").count(), 1)
            self.assertEqual(page.locator("#batch-output-list a").count(), 1)
            browser.close()

        self.assertEqual(console_errors, [])


if __name__ == "__main__":
    unittest.main()

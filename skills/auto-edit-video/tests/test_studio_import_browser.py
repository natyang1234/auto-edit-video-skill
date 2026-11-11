from __future__ import annotations

import hashlib
import shutil
import sys
import time
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
from test_studio_server import StudioHarness  # noqa: E402

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional local browser verification
    sync_playwright = None


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class StudioImportBrowserTests(StudioHarness):
    def setUp(self) -> None:
        super().setUp()
        self.editor_launches: list[Path] = []

        def stub_editor(project_dir: Path) -> str:
            self.editor_launches.append(project_dir)
            return f"http://{self.host}:{self.port}/"

        # Import finalization calls this boundary. Keep the browser on the safe
        # Studio page and avoid opening another editor server/process.
        self.server.start_editor = stub_editor  # type: ignore[method-assign]
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, args=["--mute-audio"]
        )
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.page = self.context.new_page()
        self.page_errors: list[str] = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(f"http://{self.host}:{self.port}/", wait_until="networkidle")
        self.page.wait_for_timeout(100)
        if self.page.locator("#import-director option").count() == 0:
            raise AssertionError(
                f"director options did not initialize; pageerrors={self.page_errors}; "
                f"page={self.page.url}; html={self.page.content()[:300]}"
            )

    def tearDown(self) -> None:
        if hasattr(self, "browser"):
            self.browser.close()
            self.playwright.stop()
        super().tearDown()

    def assert_no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [])

    def set_source_video(self) -> None:
        self.page.locator("#source-video-input").set_input_files(
            {
                "name": self.source.name,
                "mimeType": "video/mp4",
                "buffer": self.source_bytes,
            }
        )

    def test_navigation_guide_and_responsive_import_form(self) -> None:
        page = self.page
        page.locator("#import-project-name").fill("跨頁保留專案")
        page.locator("#import-brief").fill("保留明確結論")
        page.locator(".studio-advanced summary").click()
        page.locator("#import-transcription-glossary").fill("It; to V")
        self.set_source_video()

        expected_views = {
            "start": "建立剪輯專案",
            "workflow": "剪輯流程",
            "styles": "風格與特效",
            "sources": "素材來源",
            "recipes": "教學配方",
        }
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            for view, title in expected_views.items():
                if width <= 768:
                    toggle = page.locator("#studio-nav-toggle")
                    if not toggle.get_attribute("aria-expanded") == "true":
                        toggle.click()
                page.locator(f'[data-studio-nav="{view}"]').click()
                page.locator(f'[data-studio-view="{view}"] h1').wait_for(state="visible")
                self.assertEqual(page.locator(f'[data-studio-view="{view}"] h1').inner_text(), title)
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), width,
                    f"horizontal overflow at {width}px on {view}",
                )
        toggle = page.locator("#studio-nav-toggle")
        if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        page.locator('[data-studio-nav="start"]').click()
        page.locator('[data-studio-view="start"]').wait_for(state="visible")
        self.assertEqual(page.locator("#import-project-name").input_value(), "跨頁保留專案")
        self.assertEqual(page.locator("#import-brief").input_value(), "保留明確結論")
        self.assertEqual(page.locator("#import-transcription-glossary").input_value(), "It; to V")
        self.assertTrue(page.locator(".studio-advanced").evaluate("node => node.open"))
        self.assertTrue(page.locator("#import-file-summary").is_visible())
        self.assertEqual(page.locator("#import-file-name").inner_text(), self.source.name)
        self.assertFalse(page.locator("#start-import").is_disabled())

        if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        page.locator('[data-studio-nav="styles"]').click()
        settings_before = {
            "name": page.locator("#import-project-name").input_value(),
            "language": page.locator("#import-language").input_value(),
            "platform": page.locator("#import-platform").input_value(),
            "duration": page.locator("#import-duration").input_value(),
            "preset": page.locator("#import-preset").input_value(),
            "glossary": page.locator("#import-transcription-glossary").input_value(),
            "calibration": page.locator("#import-transcription-calibration").input_value(),
            "semantic": page.locator("#import-contextual-semantic").is_checked(),
            "director": page.locator("#import-director").input_value(),
        }
        request_text = page.locator("#studio-style-content button").first.inner_text()
        self.assertEqual(request_text, "加入剪輯重點")
        page.locator("#studio-style-content button").first.click()
        updated_brief = page.locator("#import-brief").input_value()
        self.assertTrue(updated_brief.startswith("保留明確結論\n"))
        self.assertGreater(len(updated_brief), len("保留明確結論"))
        for key, selector in (
            ("name", "#import-project-name"), ("language", "#import-language"),
            ("platform", "#import-platform"), ("duration", "#import-duration"),
            ("preset", "#import-preset"), ("glossary", "#import-transcription-glossary"),
            ("calibration", "#import-transcription-calibration"),
            ("director", "#import-director"),
        ):
            self.assertEqual(page.locator(selector).input_value(), settings_before[key])
        self.assertEqual(page.locator("#import-contextual-semantic").is_checked(), settings_before["semantic"])
        self.assert_no_page_errors()

    def test_video_selection_validation_and_folder_summary(self) -> None:
        page = self.page
        start = page.locator("#start-import")
        self.assertTrue(start.is_disabled())
        page.locator("#source-video-input").set_input_files(
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"notes"}
        )
        self.assertTrue(page.locator("#import-error").is_visible())
        self.assertIn("請選擇", page.locator("#import-error").inner_text())
        self.assertTrue(start.is_disabled())
        page.locator("#source-video-input").set_input_files(
            {"name": "not-a-video.mp4", "mimeType": "application/octet-stream", "buffer": b"bad"}
        )
        self.assertTrue(page.locator("#import-error").is_visible())
        self.assertIn("瀏覽器無法確認", page.locator("#import-error").inner_text())
        self.assertTrue(start.is_disabled())

        self.set_source_video()
        self.assertTrue(page.locator("#import-file-summary").is_visible())
        self.assertEqual(page.locator("#import-file-name").inner_text(), self.source.name)
        self.assertEqual(page.locator("#import-project-name").input_value(), self.source.stem)
        self.assertFalse(start.is_disabled())

        folder = Path(self._tmp.name) / "素材測試夾"
        folder.mkdir()
        (folder / self.source.name).write_bytes(self.source_bytes)
        (folder / "notes.txt").write_text("fixture", encoding="utf-8")
        page.locator(".studio-folder-entry summary").click()
        page.locator("#folder-input").set_input_files(str(folder))
        self.assertTrue(page.locator("#folder-summary").is_visible())
        self.assertIn("素材測試夾", page.locator("#folder-name").inner_text())
        self.assertIn("2 個檔案", page.locator("#folder-count").inner_text())
        self.assertFalse(page.locator("#start-folder-import").is_disabled())
        self.assert_no_page_errors()

    def test_upload_posts_advanced_settings_without_starting_pipeline(self) -> None:
        page = self.page
        page.locator("#import-project-name").fill("瀏覽器合成影片測試")
        page.locator("#import-brief").fill("只保留原片中明確的結論")
        page.locator("#import-language").select_option("zh-en")
        page.locator("#import-platform").select_option("youtube-shorts")
        page.locator("#import-duration").select_option("short")
        page.locator("#import-preset").select_option("conservative")
        page.locator(".studio-advanced summary").click()
        page.locator("#import-transcription-glossary").fill("It; to V")
        page.locator("#import-transcription-calibration").fill("例句=音譽句")
        page.locator("#import-contextual-semantic").uncheck()
        director = page.locator("#import-director")
        if director.locator("option").count() > 1:
            director.select_option(label=director.locator("option:not([disabled])").first.inner_text())
        selected_director = director.input_value()
        self.set_source_video()

        posted: list[dict] = []
        page.on(
            "request",
            lambda request: posted.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/imports")
            else None,
        )
        with page.expect_response(
            lambda response: response.request.method == "PUT" and "/api/imports/" in response.url,
            timeout=15000,
        ) as upload_response:
            page.locator("#start-import").click()
        self.assertEqual(upload_response.value.status, 201)
        deadline = time.monotonic() + 3
        while not self.editor_launches and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(posted), 1)
        body = posted[0]
        self.assertEqual(body["project_name"], "瀏覽器合成影片測試")
        self.assertEqual(body["settings"]["source_language"], "zh-en")
        self.assertEqual(body["settings"]["platform"], "youtube-shorts")
        self.assertEqual(body["settings"]["duration_profile"], "short")
        self.assertEqual(body["settings"]["edit_preset"], "conservative")
        self.assertEqual(body["settings"]["transcription_glossary"], "It; to V")
        self.assertEqual(body["settings"]["transcription_calibration"], "例句=音譽句")
        self.assertFalse(body["settings"]["contextual_semantic_calibration"])
        self.assertEqual(body["settings"]["director_profile"], selected_director)
        self.assertEqual(body["settings"]["editing_brief"], "只保留原片中明確的結論")

        self.assertEqual(len(self.editor_launches), 1)
        project = self.editor_launches[0]
        staged = project / "source/original.mp4"
        self.assertTrue(staged.is_file())
        self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertFalse(self.server.auto_process)
        self.assertEqual(self.server.pipeline_threads, [])
        ready_sessions = [session for session in self.server.imports.values() if session["state"] == "ready"]
        self.assertEqual(len(ready_sessions), 1)
        self.assert_no_page_errors()

    def test_bootstrap_failure_disables_video_choice_with_visible_error(self) -> None:
        page = self.page
        page.route(
            "**/api/studio",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body='{"error":"fixture bootstrap unavailable"}',
            ),
        )
        page.reload(wait_until="networkidle")
        self.assertTrue(page.locator("#import-error").is_visible())
        self.assertIn("fixture bootstrap unavailable", page.locator("#import-error").inner_text())
        self.assertTrue(page.locator("#choose-video").is_disabled())
        self.assert_no_page_errors()


if __name__ == "__main__":
    unittest.main()

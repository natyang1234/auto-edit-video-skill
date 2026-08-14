"""Two Studio defects nat hit on real projects, pinned in a real browser.

1. The live canvas squashed a 9:16 source into a near-square box.  The stage
   frame only ever knew how *wide* the preview zone was; when the zone was
   shorter than the frame's declared aspect ratio the ``max-height`` clamp
   silently broke the ratio, so a full-screen template rendered letterboxed on
   all four sides.

2. The import pipeline keeps writing the project after the panel opens.  Its
   writes bump the editor-state revision, so the panel's compare-and-swap save
   409'd and the user was told to reload — losing the edit, and taking the
   director switch (which saves first) down with it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURE_PROJECT = Path(__file__).resolve().parent / "fixtures/nat_studio_project"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
sys.path.insert(0, str(SCRIPTS_DIR))

from editor_server import EditorServer, editor_state_revision  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional local browser verification
    sync_playwright = None


class _StudioBrowserCase(unittest.TestCase):
    project: Path

    def start_server(self) -> None:
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def read_json(self, relative: str) -> dict:
        return json.loads((self.project / relative).read_text(encoding="utf-8"))

    def write_json(self, relative: str, payload: object) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class LiveCanvasAspectRatioTests(_StudioBrowserCase):
    """A 9:16 source under the full-screen template must fill a 9:16 canvas."""

    def setUp(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is unavailable")
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-canvas-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        for name in ("source", "working", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        rendered = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "color=c=0x3b332d:s=360x640:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(self.project / "source/source.mp4"),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.write_json(
            "project.json",
            {
                "schema_version": 1,
                "project_id": "canvas-ratio",
                "source": {
                    "staged_path": "source/source.mp4",
                    "duration_s": 2.0,
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                },
                "approvals": {},
            },
        )
        self.write_json(
            "working/transcript_words.json",
            {
                "text": "全畫面模板",
                "segments": [{"id": "segment-0001", "start": 0.02, "end": 1.8, "text": "全畫面模板"}],
            },
        )
        self.write_json("working/edit_candidates.json", {"items": []})
        self.write_json("working/edit_decisions.json", {"items": []})
        self.start_server()

    def measure(self, page) -> dict:
        return page.evaluate(
            """() => {
              const stage = document.querySelector('#stage-frame');
              const video = document.querySelector('#preview-video');
              const s = stage.getBoundingClientRect();
              const v = video.getBoundingClientRect();
              const zone = document.querySelector('.stage-zone').getBoundingClientRect();
              return {
                stage: {width: s.width, height: s.height, left: s.left, top: s.top},
                video: {width: v.width, height: v.height, left: v.left, top: v.top},
                zone: {width: zone.width, height: zone.height},
              };
            }"""
        )

    def test_full_screen_template_fills_a_nine_by_sixteen_canvas(self) -> None:
        # Two viewports: a short one (where the height clamp used to squash the
        # frame) and a tall one (where width is the binding constraint).
        viewports = ({"width": 1600, "height": 1100}, {"width": 1280, "height": 1700})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            for viewport in viewports:
                page = browser.new_page(viewport=viewport, device_scale_factor=1)
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(self.url, wait_until="networkidle")
                page.locator("#template-grid .template-card").first.wait_for()
                page.locator('#template-group-tabs [data-template-group="fixed"]').click()
                page.locator('#template-grid [data-template-id="fixed-full"]').click()
                page.wait_for_timeout(200)

                box = self.measure(page)
                ratio = box["stage"]["width"] / box["stage"]["height"]
                self.assertAlmostEqual(
                    ratio,
                    9 / 16,
                    delta=0.01,
                    msg=f"live canvas is not 9:16 at {viewport}: {box}",
                )
                # The frame has to stay inside its zone in both axes.
                self.assertLessEqual(box["stage"]["width"], box["zone"]["width"] + 1)
                self.assertLessEqual(box["stage"]["height"], box["zone"]["height"] + 1)
                # Full-screen template: the video covers the whole canvas, so no
                # letterbox on any side.
                # (the frame draws a 1px border, so allow a couple of pixels)
                self.assertAlmostEqual(box["video"]["width"], box["stage"]["width"], delta=3.0)
                self.assertAlmostEqual(box["video"]["height"], box["stage"]["height"], delta=3.0)
                self.assertAlmostEqual(box["video"]["left"], box["stage"]["left"], delta=3.0)
                self.assertAlmostEqual(box["video"]["top"], box["stage"]["top"], delta=3.0)
                self.assertAlmostEqual(
                    box["video"]["width"] / box["video"]["height"], 9 / 16, delta=0.01
                )
                self.assertEqual(errors, [])

                # The width/height sliders still mean "percent of the canvas".
                page.locator("#template-frame-height").fill("50")
                page.locator("#template-frame-height").dispatch_event("input")
                page.wait_for_timeout(150)
                half = self.measure(page)
                self.assertAlmostEqual(
                    half["video"]["height"] / half["stage"]["height"], 0.5, delta=0.02
                )
                self.assertAlmostEqual(
                    half["video"]["width"] / half["stage"]["width"], 1.0, delta=0.02
                )
                page.close()
            browser.close()


@unittest.skipUnless(sync_playwright and CHROME.is_file(), "Playwright Chrome is unavailable")
class PipelineWriteRaceTests(_StudioBrowserCase):
    """Background pipeline writes must not strand the panel on a 409."""

    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {"RUMI_VOICE_SYSTEM": str(RUMI_FIXTURE)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-pipeline-race-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        shutil.copytree(FIXTURE_PROJECT, self.project)
        for name in ("source", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        (self.project / "source/original.mov").write_bytes(b"0123456789abcdef")
        manifest = self.read_json("project.json")
        manifest["project_dir"] = str(self.project)
        self.write_json("project.json", manifest)
        self.set_pipeline_state("running", phase="semantic_calibration")
        self.start_server()

    def set_pipeline_state(self, state: str, phase: str = "idle") -> None:
        self.write_json(
            "working/pipeline_status.json",
            {"state": state, "phase": phase, "message": "正在用整份上下文逐句校準字幕…"},
        )

    def simulate_pipeline_write(self) -> str:
        """Rewrite the transcript the way semantic calibration does, off-panel."""
        state = self.read_json("working/editor_state.json")
        for overlay in state["overlays"]:
            if overlay.get("type") == "caption":
                overlay["text"] = f"{overlay['text']}（已校準）"
                break
        state.pop("revision", None)
        state["revision"] = editor_state_revision(state)
        self.write_json("working/editor_state.json", state)
        return state["revision"]

    def test_pipeline_write_is_merged_and_director_unlocks_when_it_finishes(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1100}, device_scale_factor=1)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(self.url, wait_until="networkidle")
            page.locator("#director-grid .director-card").first.wait_for()

            # (b) While the pipeline is running the director switch is parked
            # with an explanation instead of failing on save.
            self.assertFalse(page.locator("#director-pipeline-note").is_hidden())
            self.assertIn("字幕校準中", page.locator("#director-pipeline-note").inner_text())
            self.assertTrue(
                page.locator("#director-grid .director-card").first.is_disabled(),
                "director cards stayed clickable while the pipeline was writing",
            )

            # (a) The pipeline writes; the panel edit that follows must still save.
            self.simulate_pipeline_write()
            brief = "背景校準期間的手動筆記"
            page.locator("#editing-brief").fill(brief)
            page.wait_for_function(
                "() => document.querySelector('#save-state').textContent.includes('已儲存')",
                timeout=15000,
            )
            saved = self.read_json("working/editor_state.json")
            self.assertEqual(saved["editing_brief"], brief, "the panel edit was dropped")
            self.assertTrue(
                any("（已校準）" in str(item.get("text", "")) for item in saved["overlays"]),
                "the merge clobbered the pipeline's calibrated caption",
            )

            # (c) When the pipeline finishes the panel picks the new content up
            # on its own and the director unlocks.
            self.set_pipeline_state("needs_review")
            page.wait_for_function(
                "() => document.querySelector('#director-pipeline-note').hidden",
                timeout=15000,
            )
            self.assertFalse(page.locator("#director-grid .director-card").first.is_disabled())

            before = self.read_json("working/editor_state.json")["director_style"]
            target = page.locator(
                f'#director-grid .director-card:not([data-director-id="{before}"]):not([disabled])'
            ).first
            target_id = target.get_attribute("data-director-id")
            target.click()
            page.wait_for_function(
                "() => document.querySelector('#save-state').textContent.includes('已重新生成')",
                timeout=20000,
            )
            after = self.read_json("working/editor_state.json")
            self.assertEqual(after["director_style"], target_id)
            self.assertEqual(after["editing_brief"], brief)
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


# Discovery must skip cleanly when the explicitly approved renderer is absent;
# in that case do not import the editor modules and silently bind to shared.
STYLE_PROFILES_DIR = os.environ.get("EDIT_STYLE_PROFILES_SKILL_DIR")
base = None
editor_server = None
CHROME = Path()
sync_playwright = None
if STYLE_PROFILES_DIR:
    TESTS_DIR = Path(__file__).resolve().parent
    SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import test_editor_browser as base  # noqa: E402
    import editor_server  # noqa: E402
    CHROME = base.CHROME
    sync_playwright = base.sync_playwright


class ProjectVersionsRealBrowserIntegrationTests(unittest.TestCase):
    """Run the P7 flow against a real EditorServer and a real Chrome page.

    This intentionally does not inherit ``base.EditorBrowserSmokeTests``.  The
    base module supplies the real EditorServer fixture helpers, while
    inheritance would also collect its unrelated browser tests.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not STYLE_PROFILES_DIR:
            raise unittest.SkipTest("EDIT_STYLE_PROFILES_SKILL_DIR is required for the P7 real-browser integration test")
        if not sync_playwright or not CHROME.is_file():
            raise unittest.SkipTest("Playwright Chrome is unavailable")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-p7-real-ui-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        for relative in ("source", "working", "assets", "renders", "qa"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)

        evidence_env = os.environ.get("P7_UI_EVIDENCE_DIR")
        self._evidence_tmp: tempfile.TemporaryDirectory[str] | None = None
        if evidence_env:
            self.evidence_dir = Path(evidence_env).expanduser().resolve()
            if self.evidence_dir.exists() and any(self.evidence_dir.iterdir()):
                raise AssertionError(
                    f"refusing to overwrite non-empty evidence directory: {self.evidence_dir}"
                )
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._evidence_tmp = tempfile.TemporaryDirectory(prefix="p7-real-ui-evidence-")
            self.addCleanup(self._evidence_tmp.cleanup)
            self.evidence_dir = Path(self._evidence_tmp.name)
        for relative in ("screenshots", "http", "disk"):
            (self.evidence_dir / relative).mkdir(parents=True, exist_ok=True)

        # Keep the fixture entirely synthetic: SMPTE bars plus a 660 Hz tone,
        # with no person, brand, or D-TOWN identity in the source or transcript.
        source = self.project / "source/source.mp4"
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg and ffprobe are required for the synthetic fixture")
        rendered = self._run_bounded(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "smptebars=s=360x640:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:sample_rate=48000",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            timeout=90,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr[-4000:])
        probe = self._run_bounded(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,width,height,r_frame_rate,duration",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(source),
            ],
            timeout=90,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr[-4000:])
        media_probe = json.loads(probe.stdout)
        video = next(
            item for item in media_probe.get("streams", []) if item.get("codec_type") == "video"
        )
        duration = float(video.get("duration") or media_probe.get("format", {}).get("duration") or 2.0)
        fps = self._ratio(video.get("r_frame_rate") or "30/1")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

        self._write_json(
            "project.json",
            {
                "schema_version": 1,
                "project_id": "p7-real-ui-synthetic",
                "source": {
                    "staged_path": "source/source.mp4",
                    "sha256": source_sha,
                    "duration_s": duration,
                    "width": int(video["width"]),
                    "height": int(video["height"]),
                    "fps": fps,
                },
                # Deliberately fake and local-only approval data.  It verifies
                # that restore does not bring human approval back to life.
                "approvals": {
                    gate: {
                        "approved": True,
                        "confirmed_by": "temporary-fixture-only",
                        "at": "2026-11-09T00:00:00+00:00",
                    }
                    for gate in ("destructive_edit", "highlight_selection", "timeline", "final")
                },
                "stages": {
                    "edit_review": "complete",
                    "highlight_plan": "complete",
                    "timeline_review": "complete",
                    "render": "complete",
                    "qa": "complete",
                },
            },
        )
        self._write_json(
            "working/transcript_words.json",
            {
                "text": "P7 synthetic source A",
                "segments": [
                    {"id": "segment-0001", "start": 0.02, "end": 1.8, "text": "P7 synthetic source A"}
                ],
            },
        )
        self._write_json(
            "working/emphasis_plan.json",
            {"items": [{"id": "em-1", "start": 0.2, "end": 0.8, "text": "synthetic"}]},
        )
        self._write_json(
            "working/highlight_plan.json",
            {
                "schema_version": 1,
                "plan_revision": "a" * 64,
                "configuration": {"director_profile": "high-energy"},
                "items": [
                    {
                        "id": "highlight-synthetic",
                        "start": 0.0,
                        "end": min(1.9, duration),
                        "title": "Synthetic segment",
                        "review_status": "approved",
                        "score": 0.9,
                    }
                ],
            },
        )
        self._write_json("working/edit_candidates.json", {"items": []})
        self._write_json("working/edit_decisions.json", {"items": []})
        # Local-only fake rights data is used to verify that restore writes the
        # legal empty assertion instead of resurrecting it.
        self._write_json(
            "working/rights_assertion.json",
            {
                "schema_version": 1,
                "items": [{"asserted": True, "asset_sha256": source_sha, "asserted_by": "temporary-fixture-only"}],
                "revision": "f" * 64,
            },
        )

        # Persist the initial editor state through the product's own fixture
        # builder.  The browser still performs every version and edit action;
        # this only prevents an absent initial state file from disguising the
        # first named save as a successful version operation.
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        initial_state = editor_server.default_editor_state(self.project, manifest)
        initial_state["segments"] = [
            {"id": "segment-0000a001", "source_start": 0.0, "source_end": 0.85, "origin": "manual"},
            {"id": "segment-0000a002", "source_start": 1.05, "source_end": min(1.9, duration), "origin": "manual"},
        ]
        initial_state["overlays"].append(
            {
                "id": "fx-manual-p5-a", "type": "fx", "code": "P5", "start": 0.25,
                "end": 1.25, "values": {}, "placement": "", "scheme": "", "font_ids": {},
                "text": "手動聚光燈路徑", "style": {"opacity": 0.75}, "visible": True, "locked": False, "z_index": 30,
                "spotlight_path": {"version": 1, "interpolation": "smoothstep", "keyframes": [
                    {"source_at": 0.25, "x": 0.2, "y": 0.3},
                    {"source_at": 1.25, "x": 0.8, "y": 0.45},
                ]},
            }
        )
        initial_state["revision"] = editor_server.editor_state_revision(initial_state)
        self._write_json("working/editor_state.json", initial_state)

        self._source_before = self._fingerprint_source()
        self._env_patch = patch.dict(
            os.environ,
            {
                "RUMI_VOICE_SYSTEM": str(base.RUMI_FIXTURE),
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.server = base.EditorServer(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self._page: Any | None = None
        self._page_errors: list[str] = []
        self._console_errors: list[str] = []
        self._caption_status_samples: list[dict[str, Any]] = []
        self._csrf_token = ""

    def tearDown(self) -> None:
        # Browser failures are recorded in the test body while the page is
        # still available; no extra browser work is needed here.
        pass

    @staticmethod
    def _ratio(value: str) -> float:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)

    @staticmethod
    def _run_bounded(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ),
        )

    def _write_json(self, relative: str, payload: Any) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_evidence_json(self, relative: str, payload: Any) -> None:
        path = self.evidence_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _stop_server(self) -> None:
        if getattr(self, "server", None) is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _fingerprint_source(self) -> dict[str, Any]:
        path = self.project / "source/source.mp4"
        stat = path.stat()
        return {
            "path": "source/source.mp4",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    def _read_state_manifest(self) -> tuple[dict[str, Any], dict[str, Any]]:
        state = json.loads((self.project / "working/editor_state.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        return state, manifest

    def _edit_segments_and_manual_p5_via_api(self, page: Any) -> dict[str, Any]:
        """Make B's timeline edit through the authenticated product API, never disk writes."""
        result = page.evaluate(
            """
            async () => {
              const project = await (await fetch('/api/project', {cache: 'no-store'})).json();
              const state = project.state;
              state.segments = [
                {id: 'segment-0000b001', source_start: 0.15, source_end: 0.95, origin: 'manual'},
                {id: 'segment-0000b002', source_start: 1.15, source_end: 1.95, origin: 'manual'},
              ];
              const effect = state.overlays.find((item) => item.id === 'fx-manual-p5-a');
              effect.spotlight_path = {version: 1, interpolation: 'smoothstep', keyframes: [
                {source_at: 0.25, x: 0.35, y: 0.35}, {source_at: 1.25, x: 0.65, y: 0.6},
              ]};
              state.x_expected_revision = state.revision;
              const response = await fetch('/api/editor-state', {
                method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Auto-Edit-CSRF': project.csrf_token},
                body: JSON.stringify(state),
              });
              return {status: response.status, body: await response.json()};
            }
            """
        )
        self.assertEqual(result["status"], 200, result)
        self.assertTrue(result["body"].get("ok"), result)
        return result

    def _put_same_render_nonrender_state(
        self,
        page: Any,
        *,
        label: str,
        asserted: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Persist only fields excluded from editor_state_revision through the real API."""
        result = page.evaluate(
            """
            async ({label, asserted, warnings}) => {
              const projectResponse = await fetch('/api/project', {cache: 'no-store'});
              const project = await projectResponse.json();
              const state = project.state;
              const beforeRevision = state.revision;
              state.editing_brief = `Response-safe ${label} brief`;
              state.publishing = state.publishing || {};
              state.publishing.draft_status = 'local_draft_requires_review';
              state.publishing.title = `Response-safe ${label} title`;
              state.publishing.body = `Response-safe ${label} body`;
              state.review = state.review || {};
              state.review.warnings_acknowledged = warnings;
              state.rights = {
                asserted,
                assertion_revision: asserted ? 'c'.repeat(64) : null,
              };
              state.x_expected_revision = beforeRevision;
              const response = await fetch('/api/editor-state', {
                method: 'PUT',
                headers: {
                  'Content-Type': 'application/json',
                  'X-Auto-Edit-CSRF': project.csrf_token,
                },
                body: JSON.stringify(state),
              });
              return {
                status: response.status,
                body: await response.json(),
                before_revision: beforeRevision,
              };
            }
            """,
            {"label": label, "asserted": asserted, "warnings": warnings},
        )
        self.assertEqual(result["status"], 200, result)
        self.assertTrue(result["body"].get("ok"), result)
        return result

    def _put_pending_semantic_candidate_fixture(self, page: Any) -> dict[str, Any]:
        """Install a pending candidate through the authenticated fixture API."""
        result = page.evaluate(
            """
            async () => {
              const projectResponse = await fetch('/api/project', {cache: 'no-store'});
              const project = await projectResponse.json();
              const state = project.state;
              const caption = state.overlays.find((item) => item.type === 'caption');
              if (!caption) throw new Error('compatibility fixture needs a caption');
              const original = caption.text;
              caption.semantic_review = {
                status: 'pending',
                unit_id: 'segment-0000a001',
                candidates: [{source: original, replacement: 'P7 semantic corrected'}],
                reviewer: 'temporary-fixture-only',
                approved_at: null,
                checked_at: null,
              };
              state.x_expected_revision = state.revision;
              const response = await fetch('/api/editor-state', {
                method: 'PUT',
                headers: {
                  'Content-Type': 'application/json',
                  'X-Auto-Edit-CSRF': project.csrf_token,
                },
                body: JSON.stringify(state),
              });
              return {
                status: response.status,
                body: await response.json(),
                original,
              };
            }
            """
        )
        self.assertEqual(result["status"], 200, result)
        self.assertTrue(result["body"].get("ok"), result)
        self.assertEqual(result.get("original"), "P7 synthetic source A")
        return result

    def _assert_manual_p5_ui(self, page: Any, expected_x: str) -> None:
        row = page.locator(".layer-row").filter(has_text="手動聚光燈路徑")
        row.click()
        self.assertIn("FX · P5", page.locator("#selected-type").inner_text())
        page.locator("#manual-spotlight-timing-open").click()
        page.locator("#fx-preview-dialog[open]").wait_for(timeout=30_000)
        self.assertEqual(page.locator("#fx-preview-grid [data-fx-code='P5']").get_attribute("aria-checked"), "true")
        self.assertTrue(page.locator("#spotlight-mode-manual").is_checked())
        self.assertEqual(page.locator("#spotlight-keyframe-list input[aria-label='第 2 點 X 座標']").input_value(), expected_x)
        page.locator("#fx-preview-close").click()

    def _save_state_manifest_evidence(self, label: str, expected_text: str) -> None:
        state, manifest = self._read_state_manifest()
        texts = [
            item.get("text")
            for item in state.get("overlays", [])
            if isinstance(item, dict) and item.get("type") == "caption"
        ]
        self.assertIn(expected_text, texts, {"label": label, "caption_texts": texts})
        # Exactly these two keys are retained; in particular this is not the
        # full GET /api/project payload (which contains csrf_token).
        self._write_evidence_json(f"state-{label}.json", {"state": state, "manifest": manifest})

    def _safe_response(self, response: Any, *, keys: tuple[str, ...]) -> dict[str, Any]:
        payload = response.json()
        safe = {key: payload[key] for key in keys if key in payload}
        safe["http_status"] = response.status
        return safe

    def _wait_initialized(self, page: Any) -> None:
        page.wait_for_function(
            """
            () => {
              const name = document.querySelector('#project-name')?.textContent?.trim() || '';
              const meta = document.querySelector('#source-meta')?.textContent?.trim() || '';
              const fileName = document.querySelector('#source-file-name')?.textContent?.trim() || '';
              const video = document.querySelector('#preview-video');
              return name && name !== '讀取專案中'
                && meta && !meta.includes('?')
                && fileName && fileName !== '讀取中'
                && video && video.readyState >= 1;
            }
            """,
            timeout=90_000,
        )
        self.assertNotEqual(page.locator("#project-name").inner_text(), "讀取專案中")
        self.assertNotIn("?", page.locator("#source-meta").inner_text())
        page.locator("#layer-list .layer-row").first.wait_for(timeout=30_000)

    def _capture_version_detail_viewport(
        self,
        page: Any,
        *,
        width: int,
        height: int,
        target: str,
    ) -> dict[str, Any]:
        """Capture one asserted, non-full-page detail viewport for review evidence."""
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_function(
            "({width, height}) => window.innerWidth === width && window.innerHeight === height",
            arg={"width": width, "height": height}, timeout=30_000,
        )
        page.evaluate("""async () => { if (document.fonts?.ready) await document.fonts.ready;
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))); }""")
        # The child div holds the rendered detail content, but the surrounding
        # section is the actual max-height/overflow scroller.
        detail = page.locator("section.project-version-detail")
        caption_row = detail.locator("li").filter(has_text="圖層文字").first
        reset_summary = detail.locator(".project-version-reset-summary")
        target_locator = caption_row if target == "caption" else reset_summary
        target_locator.wait_for(state="visible", timeout=30_000)
        target_locator.scroll_into_view_if_needed(timeout=30_000)
        page.evaluate("""() => new Promise((done) => requestAnimationFrame(
          () => requestAnimationFrame(done)))""")
        diagnostics = page.evaluate(
            """
            () => {
              const rect = (node) => node && Object.fromEntries(['top', 'right', 'bottom', 'left', 'width', 'height']
                .map((key) => [key, node.getBoundingClientRect()[key]]));
              const clientRect = (node) => {
                if (!node) return null;
                const bounds = node.getBoundingClientRect();
                return {
                  top: bounds.top + node.clientTop,
                  left: bounds.left + node.clientLeft,
                  right: bounds.left + node.clientLeft + node.clientWidth,
                  bottom: bounds.top + node.clientTop + node.clientHeight,
                  width: node.clientWidth,
                  height: node.clientHeight,
                };
              };
              const includes = (outer, inner) => Boolean(outer && inner && inner.top >= outer.top
                && inner.bottom <= outer.bottom && inner.left >= outer.left && inner.right <= outer.right);
              const dialogNodes = document.querySelectorAll('#project-versions-dialog');
              const dialog = dialogNodes[0] || null;
              const detail = document.querySelector('section.project-version-detail');
              const detailContent = document.querySelector('#project-version-detail');
              const caption = [...(detailContent?.querySelectorAll('li') || [])].find((item) =>
                item.querySelector('strong')?.textContent?.includes('圖層文字')
              ) || null;
              const reset = detailContent?.querySelector('.project-version-reset-summary') || null;
              const viewport = {top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight};
              const dialogBounds = clientRect(dialog), detailBounds = rect(detail);
              const detailClientBounds = clientRect(detail);
              const captionBounds = rect(caption), resetBounds = rect(reset);
              let negativeClippingProbe = null;
              if (detail && reset) {
                const before = detail.scrollTop;
                const outer = clientRect(detail);
                const target = rect(reset);
                const maxScrollTop = Math.max(0, detail.scrollHeight - detail.clientHeight);
                const candidates = [
                  before - Math.max(1, outer.bottom - target.bottom + 1),
                  before + Math.max(1, target.top - outer.top + 1),
                ];
                for (const candidate of candidates) {
                  if (candidate < 0 || candidate > maxScrollTop) continue;
                  detail.scrollTop = candidate;
                  const clippedOuter = clientRect(detail);
                  const clippedTarget = rect(reset);
                  const inViewport = includes(viewport, clippedTarget);
                  const inDetail = includes(clippedOuter, clippedTarget);
                  if (inViewport && !inDetail) {
                    negativeClippingProbe = {
                      target: 'reset',
                      scroll_top_before: before,
                      scroll_top_candidate: detail.scrollTop,
                      target_bounds: clippedTarget,
                      detail_client_bounds: clippedOuter,
                      target_in_viewport: inViewport,
                      target_in_detail: inDetail,
                    };
                    break;
                  }
                }
                detail.scrollTop = before;
                if (negativeClippingProbe) {
                  negativeClippingProbe.restored_scroll_top = detail.scrollTop;
                }
              }
              return {
                dialog_count: dialogNodes.length,
                dialog_open: Boolean(dialog?.open),
                dialog_bounds: dialogBounds,
                detail_bounds: detailBounds,
                detail_client_bounds: detailClientBounds,
                detail_content_bounds: rect(detailContent),
                caption_bounds: captionBounds,
                reset_bounds: resetBounds,
                detail_scroll_top: detail?.scrollTop ?? null,
                detail_content_scroll_top: detailContent?.scrollTop ?? null,
                window_scroll_y: window.scrollY,
                viewport: {width: window.innerWidth, height: window.innerHeight},
                caption_in_viewport: includes(viewport, captionBounds),
                caption_in_detail: includes(detailClientBounds, captionBounds),
                caption_in_dialog: includes(dialogBounds, captionBounds),
                reset_in_viewport: includes(viewport, resetBounds),
                reset_in_detail: includes(detailClientBounds, resetBounds),
                reset_in_dialog: includes(dialogBounds, resetBounds),
                negative_clipping_probe: negativeClippingProbe,
              };
            }
            """
        )
        self.assertEqual(diagnostics["dialog_count"], 1, diagnostics)
        self.assertTrue(diagnostics["dialog_open"], diagnostics)
        self.assertEqual(diagnostics["viewport"], {"width": width, "height": height}, diagnostics)
        self.assertTrue(diagnostics[f"{target}_in_viewport"], diagnostics)
        self.assertTrue(diagnostics[f"{target}_in_detail"], diagnostics)
        self.assertTrue(diagnostics[f"{target}_in_dialog"], diagnostics)
        if target == "reset":
            negative = diagnostics["negative_clipping_probe"]
            self.assertIsNotNone(negative, diagnostics)
            self.assertTrue(negative["target_in_viewport"], diagnostics)
            self.assertFalse(negative["target_in_detail"], diagnostics)
            self.assertEqual(negative["restored_scroll_top"], diagnostics["detail_scroll_top"], diagnostics)
        capture_name = f"detail-A-current-B-{width}x{height}-{target}-viewport"
        self._write_evidence_json(f"ui/{capture_name}.json", diagnostics)
        page.screenshot(path=str(self.evidence_dir / f"screenshots/{capture_name}-page.png"), full_page=False)
        page.locator("#project-versions-dialog").screenshot(path=str(self.evidence_dir / f"screenshots/{capture_name}-dialog.png"))
        return diagnostics

    def _assert_project_payload(self, payload: dict[str, Any]) -> None:
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(payload.get("manifest"), dict)
        self.assertIsInstance(payload.get("state"), dict)
        self.assertEqual(payload["manifest"].get("project_id"), "p7-real-ui-synthetic")
        self.assertTrue(payload["state"].get("project_id"))
        source = payload["manifest"].get("source") or {}
        self.assertRegex(str(source.get("sha256")), r"^[0-9a-f]{64}$")
        self.assertEqual(payload["state"].get("source_sha256"), source.get("sha256"))
        self._csrf_token = str(payload.get("csrf_token") or "")
        self.assertTrue(self._csrf_token)

    def _open_versions(self, page: Any) -> dict[str, Any]:
        # Version CAS tokens are only taken after the live caption writer has
        # left rendering; DOM readiness alone is not sufficient.
        self._wait_caption_quiescent(page)
        with page.expect_response(
            lambda response: response.request.method == "GET"
            and response.url.endswith("/api/project-versions"),
            timeout=90_000,
        ) as listing_info:
            page.locator("#project-versions-open").click()
        page.locator("#project-versions-dialog[open]").wait_for(timeout=30_000)
        listing = self._safe_response(
            listing_info.value,
            keys=("ok", "versions", "current", "capacity", "reason", "error"),
        )
        self.assertEqual(listing["http_status"], 200, listing)
        return listing

    def _wait_caption_quiescent(self, page: Any) -> None:
        """Wait on the server's live caption job, not a fixed sleep."""
        page.wait_for_function(
            """
            async () => {
              const response = await fetch('/api/captions/status', {cache: 'no-store'});
              if (!response.ok) return false;
              const payload = await response.json();
              return payload?.job?.state !== 'rendering';
            }
            """,
            timeout=90_000,
        )
        status = page.evaluate(
            """
            async () => {
              const response = await fetch('/api/captions/status', {cache: 'no-store'});
              const payload = await response.json();
              return {
                ok: Boolean(payload?.ok),
                engine_status: payload?.engine?.status || null,
                job_state: payload?.job?.state || null,
                job_sequence: payload?.job?.sequence ?? null,
                ready: Boolean(payload?.ready),
              };
            }
            """
        )
        self._caption_status_samples.append(status)

    def _save_named_version(self, page: Any, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        page.locator("#project-version-name").fill(name)
        busy_recovery: dict[str, Any] | None = None
        for attempt in range(2):
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/project-versions"),
                timeout=90_000,
            ) as save_info:
                page.locator("#project-version-save").click()
            response = save_info.value
            payload = response.json()
            if response.status == 201:
                break
            # A caption render can legitimately hold the project lock.  Wait
            # for the live job to leave rendering, then perform one new UI
            # click.  This path is intentionally limited to save; restore is
            # never retried after an error.
            if (
                attempt == 0
                and response.status == 409
                and payload.get("reason") == "project_busy"
            ):
                busy_recovery = {
                    "first_attempt_status": response.status,
                    "first_attempt_reason": payload.get("reason"),
                    "busy_reasons": payload.get("busy_reasons", []),
                }
                self._wait_caption_quiescent(page)
                page.locator("#project-version-name").fill(name)
                continue
            self.assertEqual(response.status, 201, {"status": response.status, "payload": payload})
        else:  # pragma: no cover - the assertion above handles all responses
            self.fail("version save loop exhausted")
        if busy_recovery:
            busy_recovery["recovered"] = True
            self._write_evidence_json("http/save-busy-recovery.json", busy_recovery)
        self.assertEqual(response.status, 201, {"status": response.status, "payload": payload})
        self.assertTrue(payload.get("ok"))
        version = payload.get("version") or {}
        self.assertEqual(version.get("name"), name)
        self.assertEqual(version.get("kind"), "named")
        self.assertRegex(str(version.get("version_id")), r"^pv_[0-9a-f]{32}$")
        page.wait_for_function(
            "() => document.querySelector('#project-versions-status')?.textContent.includes('版本已儲存')",
            timeout=30_000,
        )
        return (
            {
                "http_status": response.status,
                "ok": payload.get("ok"),
                "version": version,
                "current": payload.get("current"),
            },
            version,
        )

    def _redact(self, value: str) -> str:
        text = str(value)
        if self._csrf_token:
            text = text.replace(self._csrf_token, "<redacted-csrf>")
        return re.sub(r"(?i)(csrf[_-]?token\s*[=:]\s*)[^,\s}]+", r"\1<redacted-csrf>", text)

    def _write_failure_evidence(self, error: BaseException) -> None:
        page = self._page
        failure = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": self._redact(str(error)),
            "page_errors": [self._redact(item) for item in self._page_errors],
            "console_errors": [self._redact(item) for item in self._console_errors],
        }
        self._write_evidence_json("failure.json", failure)
        if page:
            try:
                page.screenshot(path=str(self.evidence_dir / "screenshots/failure-viewport.png"), full_page=False)
            except Exception:
                pass

    def _copy_project_for_cli(self) -> None:
        target = self.evidence_dir / "retained-project"
        if target.exists():
            raise AssertionError(f"refusing to overwrite existing evidence project: {target}")
        ignored = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
        shutil.copytree(self.project, target, ignore=ignored)

    def test_real_ui_save_edit_diff_restore_and_persist(self) -> None:
        stage = lambda name: print(f"P7_STAGE {name}", flush=True)
        host, port = self.server.server_address
        page: Any | None = None
        browser: Any | None = None
        context: Any | None = None
        try:
            stage("launch-chrome")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(CHROME),
                    headless=True,
                    args=["--mute-audio"],
                )
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    device_scale_factor=1,
                )
                page = context.new_page()
                self._page = page
                page.set_default_timeout(30_000)
                page.set_default_navigation_timeout(90_000)
                page.on("pageerror", lambda error: self._page_errors.append(str(error)))
                page.on("console", lambda message: self._console_errors.append(f"{message.type}: {message.text}") if message.type == "error" else None)

                stage("editor-get-and-initialize")
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith("/api/project"),
                    timeout=90_000,
                ) as project_info:
                    page.goto(f"http://{host}:{port}/", wait_until="domcontentloaded")
                project_response = project_info.value
                self.assertEqual(project_response.status, 200)
                project_payload = project_response.json()
                self._assert_project_payload(project_payload)
                self._wait_initialized(page)
                page.screenshot(
                    path=str(self.evidence_dir / "screenshots/initialized-1440x1000.png"),
                    full_page=False,
                )
                self._write_evidence_json(
                    "initialization.json",
                    {
                        "project_id": project_payload["manifest"]["project_id"],
                        "state_valid": True,
                        "project_name": page.locator("#project-name").inner_text(),
                        "source_meta": page.locator("#source-meta").inner_text(),
                        "source_file_name": page.locator("#source-file-name").inner_text(),
                        "video_ready_state": page.locator("#preview-video").evaluate("el => el.readyState"),
                    },
                )

                stage("save-version-a")
                listing_a = self._open_versions(page)
                self._write_evidence_json(
                    "http/caption-status-before-version-A.json",
                    self._caption_status_samples[-1],
                )
                self.assertEqual(listing_a["versions"], [])
                save_a, version_a = self._save_named_version(page, "P7 Version A")
                self._write_evidence_json("http/save-A.json", save_a)
                self._save_state_manifest_evidence("A", "P7 synthetic source A")
                page.screenshot(
                    path=str(self.evidence_dir / "screenshots/version-A-1440x1000.png"),
                    full_page=False,
                )
                page.locator("#project-versions-close").click()
                page.locator("#project-versions-dialog").wait_for(state="hidden", timeout=30_000)

                stage("edit-caption-b-and-await-autosave")
                caption_row = page.locator(".layer-row").filter(has_text="字幕").first
                caption_row.wait_for(timeout=30_000)
                caption_row.click()
                caption_input = page.locator("#overlay-text")
                caption_input.wait_for(timeout=30_000)
                self.assertEqual(caption_input.input_value(), "P7 synthetic source A")
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ) as editor_save_info:
                    caption_input.fill("P7 synthetic source B")
                self.assertEqual(editor_save_info.value.status, 200)
                page.wait_for_function(
                    "() => document.querySelector('#save-state')?.classList.contains('is-saved') && document.querySelector('#save-state')?.textContent.includes('已儲存')",
                    timeout=90_000,
                )
                state_b, _manifest_b = self._read_state_manifest()
                caption_texts_b = [
                    item.get("text")
                    for item in state_b.get("overlays", [])
                    if isinstance(item, dict) and item.get("type") == "caption"
                ]
                self.assertIn("P7 synthetic source B", caption_texts_b)
                self.assertNotIn("P7 synthetic source A", caption_texts_b)

                stage("edit-b-segments-and-manual-p5-via-api")
                api_edit_b = self._edit_segments_and_manual_p5_via_api(page)
                self._write_evidence_json("http/editor-state-B-timeline-edit.json", api_edit_b)
                state_b, _manifest_b = self._read_state_manifest()
                self.assertEqual([item["id"] for item in state_b["segments"]], ["segment-0000b001", "segment-0000b002"])
                p5_b = next(item for item in state_b["overlays"] if item.get("id") == "fx-manual-p5-a")
                self.assertEqual(p5_b["spotlight_path"]["keyframes"][-1]["x"], 0.65)
                page.reload(wait_until="domcontentloaded")
                self._wait_initialized(page)
                self._assert_manual_p5_ui(page, "0.65")

                stage("save-version-b")
                self._open_versions(page)
                save_b, version_b = self._save_named_version(page, "P7 Version B")
                self._write_evidence_json("http/save-B.json", save_b)
                self._save_state_manifest_evidence("B", "P7 synthetic source B")
                page.screenshot(
                    path=str(self.evidence_dir / "screenshots/version-B-1440x1000.png"),
                    full_page=False,
                )

                stage("open-a-diff")
                row_a = page.locator(".project-version-row").filter(has_text="P7 Version A").first
                row_a.wait_for(timeout=30_000)
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith(f"/api/project-versions/{version_a['version_id']}"),
                    timeout=90_000,
                ) as detail_info:
                    row_a.click()
                detail_response = detail_info.value
                self.assertEqual(detail_response.status, 200)
                detail_payload = detail_response.json()
                self.assertEqual(detail_payload.get("diff", {}).get("direction"), {"from": "current", "to": "saved"})
                changes = detail_payload.get("diff", {}).get("changes", [])
                self.assertTrue(
                    any(
                        "P7 synthetic source A" in str(item.get("saved_preview"))
                        and "P7 synthetic source B" in str(item.get("current_preview"))
                        for item in changes
                    ),
                    changes,
                )
                detail_safe = {
                    "http_status": detail_response.status,
                    "ok": detail_payload.get("ok"),
                    "version": detail_payload.get("version"),
                    "current": detail_payload.get("current"),
                    "diff": detail_payload.get("diff"),
                }
                self._write_evidence_json("http/detail-A-current-B.json", detail_safe)
                detail_text = page.locator("#project-version-detail").inner_text()
                self._write_evidence_json(
                    "ui/detail-A-current-B-text.json",
                    {"detail_text": detail_text},
                )
                self.assertIn("目前：", detail_text)
                self.assertIn("還原為：", detail_text)
                self.assertIn("圖層文字", detail_text)
                self.assertIn("P7 synthetic source A", detail_text)
                self.assertIn("P7 synthetic source B", detail_text)
                self.assertLess(detail_text.index("剪輯內容"), detail_text.index("重新確認"))
                self.assertNotIn("Invalidated", detail_text)
                self.assertNotRegex(detail_text, r"\b[a-f0-9]{64}\b")
                self._capture_version_detail_viewport(
                    page, width=1440, height=1000, target="caption"
                )
                self._capture_version_detail_viewport(
                    page, width=1100, height=900, target="caption"
                )
                self._capture_version_detail_viewport(
                    page, width=390, height=900, target="caption"
                )
                self._capture_version_detail_viewport(
                    page, width=390, height=900, target="reset"
                )
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.wait_for_function(
                    "() => window.innerWidth === 1440 && window.innerHeight === 1000",
                    timeout=30_000,
                )
                page.evaluate("""async () => { if (document.fonts?.ready) await document.fonts.ready;
                  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))); }""")

                stage("confirm-restore-a")
                page.locator("#project-version-detail").get_by_role("button", name="還原此版本").click()
                restore_dialog = page.locator("#project-version-restore-dialog[open]")
                restore_dialog.wait_for(timeout=30_000)
                self.assertIn("P7 Version A", page.locator("#project-version-restore-name").inner_text())
                restore_capture_key = "p7-real-ui-safe-restore-response"
                page.evaluate(
                    """
                    ([captureKey, expectedPath]) => {
                      const nativeFetch = window.fetch.bind(window);
                      window.fetch = async (...args) => {
                        const response = await nativeFetch(...args);
                        const request = args[0];
                        const options = args[1] || {};
                        const url = typeof request === 'string' ? request : request.url;
                        const method = String(options.method || request?.method || 'GET').toUpperCase();
                        const target = new URL(url, window.location.href);
                        if (
                          method === 'POST'
                          && target.origin === window.location.origin
                          && target.pathname === expectedPath
                        ) {
                          const payload = await response.clone().json();
                          sessionStorage.setItem(captureKey, JSON.stringify({
                            http_status: response.status,
                            payload: {
                              ok: payload?.ok,
                              restored_version: payload?.restored_version,
                              automatic_backup: payload?.automatic_backup,
                              current: payload?.current,
                              approvals_invalidated: payload?.approvals_invalidated,
                              reload_required: payload?.reload_required,
                            },
                          }));
                        }
                        return response;
                      };
                    }
                    """,
                    [
                        restore_capture_key,
                        f"/api/project-versions/{version_a['version_id']}/restore",
                    ],
                )
                with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(f"/api/project-versions/{version_a['version_id']}/restore"),
                        timeout=90_000,
                    ) as restore_info:
                        page.locator("#project-version-restore-confirm").click()
                restore_response = restore_info.value
                restore_capture = page.evaluate(
                    """
                    captureKey => {
                      const raw = sessionStorage.getItem(captureKey);
                      sessionStorage.removeItem(captureKey);
                      return raw ? JSON.parse(raw) : null;
                    }
                    """,
                    restore_capture_key,
                )
                self.assertIsInstance(restore_capture, dict)
                self.assertEqual(restore_capture.get("http_status"), restore_response.status)
                restore_payload = restore_capture.get("payload") or {}
                self.assertEqual(restore_response.status, 200, restore_payload)
                self.assertTrue(restore_payload.get("ok"))
                self.assertTrue(restore_payload.get("reload_required"))
                automatic_backup = restore_payload.get("automatic_backup") or {}
                self.assertEqual(automatic_backup.get("kind"), "auto_before_restore")
                self.assertRegex(str(automatic_backup.get("version_id")), r"^pv_[0-9a-f]{32}$")
                self._write_evidence_json(
                    "http/restore-A.json",
                    {
                        "http_status": restore_response.status,
                        "ok": restore_payload.get("ok"),
                        "restored_version": restore_payload.get("restored_version"),
                        "automatic_backup": automatic_backup,
                        "current": restore_payload.get("current"),
                        "approvals_invalidated": restore_payload.get("approvals_invalidated"),
                        "reload_required": restore_payload.get("reload_required"),
                    },
                )
                # The UI's same-page navigation has completed before the
                # captured, explicitly whitelisted response fields are read.
                self._wait_initialized(page)
                self.assertEqual(page.locator("#overlay-text").input_value(), "P7 synthetic source A")
                self.assertNotEqual(page.locator("#overlay-text").input_value(), "P7 synthetic source B")
                self._save_state_manifest_evidence("restored-A", "P7 synthetic source A")
                page.screenshot(
                    path=str(self.evidence_dir / "screenshots/restored-A-1440x1000.png"),
                    full_page=False,
                )

                state_restored, manifest_restored = self._read_state_manifest()
                self.assertEqual(self._fingerprint_source(), self._source_before)
                self.assertEqual([item["id"] for item in state_restored["segments"]], ["segment-0000a001", "segment-0000a002"])
                restored_p5 = next(item for item in state_restored["overlays"] if item.get("id") == "fx-manual-p5-a")
                self.assertEqual(restored_p5["spotlight_path"]["keyframes"][-1]["x"], 0.8)
                self._assert_manual_p5_ui(page, "0.8")
                self.assertFalse((state_restored.get("rights") or {}).get("asserted"))
                rights = json.loads((self.project / "working/rights_assertion.json").read_text(encoding="utf-8"))
                self.assertFalse(rights.get("asserted", False))
                self.assertFalse(any(item.get("asserted") for item in rights.get("items", []) if isinstance(item, dict)))
                for approval in (manifest_restored.get("approvals") or {}).values():
                    if isinstance(approval, dict) and "approved" in approval:
                        self.assertFalse(approval.get("approved"), approval)

                stage("reopen-list-and-check-auto-backup")
                listing_after_restore = self._open_versions(page)
                self._write_evidence_json("http/list-after-restore.json", listing_after_restore)
                names_after_restore = [item.get("name") for item in listing_after_restore.get("versions", [])]
                self.assertIn("P7 Version A", names_after_restore)
                self.assertIn("P7 Version B", names_after_restore)
                self.assertTrue(
                    any(item.get("kind") == "auto_before_restore" for item in listing_after_restore.get("versions", [])),
                    listing_after_restore,
                )
                self.assertEqual(
                    next(item.get("version_id") for item in listing_after_restore["versions"] if item.get("kind") == "auto_before_restore"),
                    automatic_backup.get("version_id"),
                )
                page.set_viewport_size({"width": 390, "height": 900})
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 390)
                page.screenshot(
                    path=str(self.evidence_dir / "screenshots/versions-reopened-390x900.png"),
                    full_page=False,
                )
                page.locator("#project-versions-close").click()

                stage("reload-and-check-list-persistence")
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith("/api/project"),
                    timeout=90_000,
                ) as reload_info:
                    page.reload(wait_until="domcontentloaded", timeout=90_000)
                reload_response = reload_info.value
                self.assertEqual(reload_response.status, 200)
                reload_payload = reload_response.json()
                self._assert_project_payload(reload_payload)
                self._wait_initialized(page)
                self.assertEqual(page.locator("#overlay-text").input_value(), "P7 synthetic source A")
                listing_after_reload = self._open_versions(page)
                self._write_evidence_json("http/list-after-reload.json", listing_after_reload)
                names_after_reload = [item.get("name") for item in listing_after_reload.get("versions", [])]
                self.assertIn("P7 Version A", names_after_reload)
                self.assertIn("P7 Version B", names_after_reload)
                self.assertTrue(
                    any(item.get("kind") == "auto_before_restore" for item in listing_after_reload.get("versions", [])),
                    listing_after_reload,
                )

                records_dir = self.project / "working/project_versions/records"
                records = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(records_dir.glob("pv_*.json"))
                ]
                auto_records = [record for record in records if record.get("kind") == "auto_before_restore"]
                self.assertEqual(len(auto_records), 1, records)
                backup_record = auto_records[0]
                backup_texts = [
                    item.get("text")
                    for item in (backup_record.get("snapshot_state") or {}).get("overlays", [])
                    if isinstance(item, dict) and item.get("type") == "caption"
                ]
                self.assertIn("P7 synthetic source B", backup_texts, backup_record)
                self.assertNotIn("P7 synthetic source A", backup_texts, backup_record)
                self.assertEqual(backup_record.get("source_binding", {}).get("sha256"), self._source_before["sha256"])
                self._write_evidence_json(
                    "disk/version-records.json",
                    {
                        "schema_version": 1,
                        "records": records,
                        "automatic_backup_version_id": backup_record.get("version_id"),
                    },
                )
                self._write_evidence_json(
                    "disk/source-fingerprint.json",
                    {"before": self._source_before, "after": self._fingerprint_source(), "unchanged": True},
                )
                self._copy_project_for_cli()
                self._write_evidence_json(
                    "result.json",
                    {
                        "outcome": "CONFIRMED_REPOSITORY_UI",
                        "flow": [
                            "real Chrome /api/project initialization",
                            "UI named save A",
                            "real subtitle edit B and auto-save",
                            "UI named save B",
                            "UI A diff current B to saved A",
                            "UI confirmed server restore A with automatic B backup",
                            "reload and persisted version-list verification",
                        ],
                        "version_ids": {
                            "A": version_a.get("version_id"),
                            "B": version_b.get("version_id"),
                            "automatic_backup": automatic_backup.get("version_id"),
                        },
                        "source_unchanged": self._fingerprint_source() == self._source_before,
                        "rights_and_approvals_reset": True,
                    },
                )

                stage("real-409-refusal-after-retained-snapshots")
                records_before_refusal = list(records_dir.glob("pv_*.json"))
                refusal_restore_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: refusal_restore_requests.append(request.url)
                    if request.method == "POST"
                    and request.url.endswith(
                        f"/api/project-versions/{version_a['version_id']}/restore"
                    )
                    else None,
                )
                refusal_row_a = page.locator(".project-version-row").filter(
                    has_text="P7 Version A"
                ).first
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith(
                        f"/api/project-versions/{version_a['version_id']}"
                    ),
                    timeout=90_000,
                ):
                    refusal_row_a.click()
                page.locator("#project-version-detail").get_by_role(
                    "button", name="還原此版本"
                ).click()
                page.locator("#project-version-restore-dialog[open]").wait_for(
                    timeout=30_000
                )
                refusal_edit = self._put_same_render_nonrender_state(
                    page,
                    label="409-latest",
                    asserted=False,
                    warnings=[],
                )
                self.assertEqual(refusal_edit["status"], 200, refusal_edit)
                refusal_capture_key = "p7-real-409-refusal-capture"
                page.evaluate(
                    """
                    ([expectedPath, captureKey]) => {
                      const nativeFetch = window.fetch.bind(window);
                      window.fetch = async (...args) => {
                        const response = await nativeFetch(...args);
                        const request = args[0];
                        const options = args[1] || {};
                        const url = typeof request === 'string' ? request : request.url;
                        const method = String(options.method || request?.method || 'GET').toUpperCase();
                        const target = new URL(url, window.location.href);
                        if (method === 'POST' && target.origin === window.location.origin
                            && target.pathname === expectedPath) {
                          const payload = await response.clone().json();
                          sessionStorage.setItem(captureKey, JSON.stringify({
                            http_status: response.status,
                            reason: payload?.reason,
                            error_code: payload?.error_code,
                          }));
                        }
                        return response;
                      };
                    }
                    """,
                    [
                        f"/api/project-versions/{version_a['version_id']}/restore",
                        refusal_capture_key,
                    ],
                )
                with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=90_000
                ):
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(
                            f"/api/project-versions/{version_a['version_id']}/restore"
                        ),
                        timeout=90_000,
                    ) as refusal_info:
                        page.locator("#project-version-restore-confirm").click()
                refusal_response = refusal_info.value
                refusal_payload = page.evaluate(
                    """captureKey => {
                      const raw = sessionStorage.getItem(captureKey);
                      sessionStorage.removeItem(captureKey);
                      return raw ? JSON.parse(raw) : null;
                    }""",
                    refusal_capture_key,
                )
                self.assertIsInstance(refusal_payload, dict)
                self.assertEqual(refusal_response.status, 409, refusal_payload)
                self.assertEqual(
                    refusal_payload.get("reason") or refusal_payload.get("error_code"),
                    "revision_conflict",
                    refusal_payload,
                )
                self._wait_initialized(page)
                page.locator("#project-versions-dialog[open]").wait_for(timeout=30_000)
                refusal_notice = page.locator("#project-versions-status").inner_text()
                self.assertIn("專案內容已變更", refusal_notice)
                self.assertIn("已重新載入目前專案", refusal_notice)
                self.assertNotIn("還原回應未完整收到", refusal_notice)
                self.assertEqual(len(refusal_restore_requests), 1, refusal_restore_requests)
                self.assertEqual(
                    len(list(records_dir.glob("pv_*.json"))),
                    len(records_before_refusal),
                )
                self.assertEqual(
                    page.locator("#editing-brief").input_value(),
                    "Response-safe 409-latest brief",
                )
                page.locator("#project-versions-close").click()
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ):
                    page.locator("#editing-brief").fill(
                        "Response-safe 409-latest remains editable"
                    )
                self._open_versions(page)
                self._write_evidence_json(
                    "restore-refusal/result.json",
                    {
                        "http_status": refusal_response.status,
                        "reason": refusal_payload.get("reason")
                        or refusal_payload.get("error_code"),
                        "restore_post_count": len(refusal_restore_requests),
                        "record_count_delta": len(list(records_dir.glob("pv_*.json")))
                        - len(records_before_refusal),
                        "specific_notice": refusal_notice,
                        "latest_state_editable": True,
                    },
                )

                stage("response-loss-same-render-setup-a")
                page.locator("#project-versions-close").click()
                self._put_same_render_nonrender_state(
                    page,
                    label="A",
                    asserted=False,
                    warnings=[],
                )
                page.reload(wait_until="domcontentloaded", timeout=90_000)
                self._wait_initialized(page)
                self._open_versions(page)
                loss_save_a, loss_version_a = self._save_named_version(
                    page, "P7 Response Loss A"
                )
                self._write_evidence_json(
                    "response-loss/http/save-A.json", loss_save_a
                )
                page.locator("#project-versions-close").click()

                stage("response-loss-same-render-setup-b")
                loss_edit_b = self._put_same_render_nonrender_state(
                    page,
                    label="B",
                    asserted=True,
                    warnings=["stale-B-warning"],
                )
                self._write_evidence_json(
                    "response-loss/http/editor-state-B.json",
                    {
                        "http_status": loss_edit_b["status"],
                        "ok": loss_edit_b["body"].get("ok"),
                        "revision": loss_edit_b["body"].get("revision"),
                        "before_revision": loss_edit_b["before_revision"],
                    },
                )
                page.reload(wait_until="domcontentloaded", timeout=90_000)
                self._wait_initialized(page)
                self.assertEqual(
                    page.locator("#editing-brief").input_value(),
                    "Response-safe B brief",
                )
                loss_listing_b = self._open_versions(page)
                self.assertEqual(
                    loss_listing_b["current"]["render_revision"],
                    loss_version_a["render_revision"],
                    loss_listing_b,
                )
                self.assertNotEqual(
                    loss_listing_b["current"]["state_digest"],
                    loss_version_a["state_digest"],
                    loss_listing_b,
                )
                loss_row_a = page.locator(".project-version-row").filter(
                    has_text="P7 Response Loss A"
                ).first
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith(
                        f"/api/project-versions/{loss_version_a['version_id']}"
                    ),
                    timeout=90_000,
                ):
                    loss_row_a.click()
                page.locator("#project-version-detail").get_by_role(
                    "button", name="還原此版本"
                ).click()
                page.locator("#project-version-restore-dialog[open]").wait_for(
                    timeout=30_000
                )

                records_before_loss = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(records_dir.glob("pv_*.json"))
                ]
                auto_before_loss = sum(
                    record.get("kind") == "auto_before_restore"
                    for record in records_before_loss
                )
                loss_restore_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: loss_restore_requests.append(request.url)
                    if request.method == "POST"
                    and request.url.endswith(
                        f"/api/project-versions/{loss_version_a['version_id']}/restore"
                    )
                    else None,
                )
                loss_capture_key = "p7-response-loss-safe-restore-capture"
                page.evaluate(
                    """
                    ([expectedPath, captureKey]) => {
                      const nativeFetch = window.fetch.bind(window);
                      window.fetch = async (...args) => {
                        const response = await nativeFetch(...args);
                        const request = args[0];
                        const options = args[1] || {};
                        const url = typeof request === 'string' ? request : request.url;
                        const method = String(options.method || request?.method || 'GET').toUpperCase();
                        const target = new URL(url, window.location.href);
                        if (
                          method === 'POST'
                          && target.origin === window.location.origin
                          && target.pathname === expectedPath
                        ) {
                          const payload = await response.clone().json();
                          sessionStorage.setItem(captureKey, JSON.stringify({
                            http_status: response.status,
                            reload_required: payload?.reload_required,
                            automatic_backup: payload?.automatic_backup,
                          }));
                          throw new TypeError('synthetic restore response lost after commit');
                        }
                        return response;
                      };
                    }
                    """,
                    [
                        f"/api/project-versions/{loss_version_a['version_id']}/restore",
                        loss_capture_key,
                    ],
                )

                stage("response-loss-commit-and-drop-response")
                with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=15_000
                ):
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(
                            f"/api/project-versions/{loss_version_a['version_id']}/restore"
                        ),
                        timeout=90_000,
                    ) as loss_restore_info:
                        page.locator("#project-version-restore-confirm").click()
                loss_restore_response = loss_restore_info.value
                loss_restore_capture = page.evaluate(
                    """
                    captureKey => {
                      const raw = sessionStorage.getItem(captureKey);
                      sessionStorage.removeItem(captureKey);
                      return raw ? JSON.parse(raw) : null;
                    }
                    """,
                    loss_capture_key,
                )
                self.assertIsInstance(loss_restore_capture, dict)
                self.assertEqual(loss_restore_response.status, 200, loss_restore_capture)
                self.assertTrue(
                    loss_restore_capture.get("reload_required"), loss_restore_capture
                )
                self.assertEqual(len(loss_restore_requests), 1, loss_restore_requests)

                stage("response-loss-reloaded-a")
                self._wait_initialized(page)
                page.locator("#project-versions-dialog[open]").wait_for(timeout=30_000)
                page.wait_for_function(
                    "() => document.querySelector('#project-versions-status')?.textContent.includes('還原結果尚無法確認，已重新載入目前專案，請確認內容後再繼續。')",
                    timeout=30_000,
                )
                self.assertEqual(
                    page.locator("#editing-brief").input_value(),
                    "Response-safe A brief",
                )
                restored_loss_state, restored_loss_manifest = self._read_state_manifest()
                self.assertEqual(
                    restored_loss_state["publishing"]["title"],
                    "Response-safe A title",
                )
                self.assertEqual(
                    restored_loss_state["review"]["warnings_acknowledged"], []
                )
                self.assertFalse(restored_loss_state["rights"]["asserted"])
                self.assertTrue(
                    all(
                        not approval.get("approved")
                        for approval in restored_loss_manifest["approvals"].values()
                        if isinstance(approval, dict)
                    )
                )
                restored_loss_rights = json.loads(
                    (self.project / "working/rights_assertion.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(restored_loss_rights.get("items"), [])
                records_after_loss = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(records_dir.glob("pv_*.json"))
                ]
                auto_after_loss = [
                    record
                    for record in records_after_loss
                    if record.get("kind") == "auto_before_restore"
                ]
                self.assertEqual(len(auto_after_loss), auto_before_loss + 1)
                loss_backup = next(
                    record
                    for record in auto_after_loss
                    if record.get("version_id")
                    == loss_restore_capture["automatic_backup"]["version_id"]
                )
                self.assertEqual(
                    loss_backup["snapshot_state"]["editing_brief"],
                    "Response-safe B brief",
                )
                self.assertEqual(
                    loss_backup["snapshot_state"]["publishing"]["title"],
                    "Response-safe B title",
                )

                stage("response-loss-edit-autosave-and-undo")
                page.locator("#project-versions-close").click()
                title_count_before = page.locator(".layer-row").filter(
                    has_text="影片標題"
                ).count()
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ):
                    page.locator("[data-add-type='title']").click()
                state_after_loss_edit, _ = self._read_state_manifest()
                self.assertEqual(
                    state_after_loss_edit["editing_brief"],
                    "Response-safe A brief",
                )
                self.assertEqual(
                    state_after_loss_edit["publishing"]["title"],
                    "Response-safe A title",
                )
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ):
                    page.keyboard.press("Meta+Z")
                self.assertEqual(
                    page.locator(".layer-row").filter(has_text="影片標題").count(),
                    title_count_before,
                )
                state_after_loss_undo, _ = self._read_state_manifest()
                self.assertEqual(
                    state_after_loss_undo["editing_brief"],
                    "Response-safe A brief",
                )
                self.assertEqual(
                    state_after_loss_undo["publishing"]["title"],
                    "Response-safe A title",
                )
                self.assertNotEqual(
                    state_after_loss_undo["editing_brief"],
                    "Response-safe B brief",
                )
                self._write_evidence_json(
                    "response-loss/result.json",
                    {
                        "outcome": "CONFIRMED_RESPONSE_LOSS_RESYNC",
                        "restore_http_status": loss_restore_response.status,
                        "restore_post_count": len(loss_restore_requests),
                        "same_render_revision": True,
                        "different_full_state_digest": True,
                        "reloaded_nonrender_state": "A",
                        "automatic_backup_count_delta": len(auto_after_loss)
                        - auto_before_loss,
                        "authority_reset": True,
                        "post_reload_edit_and_undo_preserved_a": True,
                    },
                )

                stage("state-compat-generate-copy")
                page.locator("#publish-tab").click()
                page.locator("#publish-panel:not([hidden])").wait_for(timeout=30_000)
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and response.url.endswith("/api/copy-draft")
                    and response.status == 200,
                    timeout=90_000,
                ) as copy_info:
                    with page.expect_response(
                        lambda response: response.request.method == "PUT"
                        and response.url.endswith("/api/editor-state")
                        and response.status == 200,
                        timeout=90_000,
                    ) as copy_save_info:
                        page.locator("#generate-copy").click()
                copy_response = copy_info.value
                copy_payload = copy_response.json()
                self.assertTrue(copy_payload.get("ok"), copy_payload)
                self.assertEqual(copy_save_info.value.status, 200)
                self._write_evidence_json(
                    "state-compat/http/copy-draft.json",
                    {
                        "http_status": copy_response.status,
                        "ok": copy_payload.get("ok"),
                        "draft": {
                            "draft_status": (copy_payload.get("draft") or {}).get("draft_status"),
                            "generator": (copy_payload.get("draft") or {}).get("generator"),
                        },
                        "editor_state_http_status": copy_save_info.value.status,
                    },
                )
                page.wait_for_function(
                    "() => document.querySelector('#save-state')?.classList.contains('is-saved') && document.querySelector('#save-state')?.textContent.includes('已儲存')",
                    timeout=90_000,
                )
                state_after_copy, _manifest_after_copy = self._read_state_manifest()
                self.assertEqual(
                    state_after_copy.get("publishing", {}).get("generator"),
                    "deterministic-transcript-draft-v1",
                )
                self._write_evidence_json(
                    "state-compat/publishing-after-copy.json",
                    {
                        "generator": state_after_copy.get("publishing", {}).get("generator"),
                        "draft_status": state_after_copy.get("publishing", {}).get("draft_status"),
                        "title": state_after_copy.get("publishing", {}).get("title"),
                    },
                )

                stage("state-compat-install-pending-candidate")
                pending_fixture = self._put_pending_semantic_candidate_fixture(page)
                self._write_evidence_json(
                    "state-compat/http/pending-candidate-fixture.json",
                    {
                        "http_status": pending_fixture["status"],
                        "ok": pending_fixture["body"].get("ok"),
                        "caption_original": pending_fixture.get("original"),
                    },
                )
                page.reload(wait_until="domcontentloaded", timeout=90_000)
                self._wait_initialized(page)
                pending_state, _pending_manifest = self._read_state_manifest()
                pending_caption = next(
                    item
                    for item in pending_state.get("overlays", [])
                    if item.get("type") == "caption"
                )
                self.assertEqual(
                    pending_caption.get("semantic_review", {}).get("status"),
                    "pending",
                )

                stage("state-compat-edit-semantic-via-ui")
                caption_row = page.locator(".layer-row").filter(has_text="字幕").first
                caption_row.wait_for(timeout=30_000)
                caption_row.click()
                caption_input = page.locator("#overlay-text")
                caption_input.wait_for(timeout=30_000)
                self.assertEqual(caption_input.input_value(), "P7 synthetic source A")
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ) as semantic_save_info:
                    caption_input.fill("P7 semantic corrected")
                self.assertEqual(semantic_save_info.value.status, 200)
                page.wait_for_function(
                    "() => document.querySelector('#save-state')?.classList.contains('is-saved') && document.querySelector('#save-state')?.textContent.includes('已儲存')",
                    timeout=90_000,
                )
                compatibility_state, _compatibility_manifest = self._read_state_manifest()
                compatibility_caption = next(
                    item
                    for item in compatibility_state.get("overlays", [])
                    if item.get("type") == "caption"
                )
                compatibility_semantic = compatibility_caption.get("semantic_review") or {}
                self.assertEqual(compatibility_caption.get("text"), "P7 semantic corrected")
                self.assertEqual(compatibility_semantic.get("status"), "resolved_manual")
                self.assertEqual(compatibility_semantic.get("candidates"), [])
                self.assertTrue(compatibility_semantic.get("resolved_at"))
                self.assertEqual(
                    compatibility_state.get("publishing", {}).get("generator"),
                    "deterministic-transcript-draft-v1",
                )
                self._write_evidence_json(
                    "state-compat/state-before-version.json",
                    {
                        "caption": {
                            "text": compatibility_caption.get("text"),
                            "semantic_review": compatibility_semantic,
                        },
                        "publishing": {
                            "generator": compatibility_state.get("publishing", {}).get("generator"),
                            "draft_status": compatibility_state.get("publishing", {}).get("draft_status"),
                        },
                    },
                )

                stage("state-compat-save-named-version")
                compat_listing = self._open_versions(page)
                self._write_evidence_json(
                    "state-compat/http/list-before-save.json",
                    compat_listing,
                )
                page.locator("#project-version-name").fill("P7 State Compatibility")
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and response.url.endswith("/api/project-versions"),
                    timeout=90_000,
                ) as compat_save_info:
                    page.locator("#project-version-save").click()
                compat_save_response = compat_save_info.value
                compat_save_payload = compat_save_response.json()
                compat_save_safe = {
                    "http_status": compat_save_response.status,
                    "ok": compat_save_payload.get("ok"),
                    "reason": compat_save_payload.get("reason"),
                    "code": compat_save_payload.get("code"),
                    "error": compat_save_payload.get("error"),
                    "version": compat_save_payload.get("version"),
                }
                self._write_evidence_json(
                    "state-compat/http/save-version.json",
                    compat_save_safe,
                )
                self.assertEqual(compat_save_response.status, 201, compat_save_safe)
                self.assertTrue(compat_save_payload.get("ok"), compat_save_safe)
                compatibility_version = compat_save_payload.get("version") or {}
                compatibility_version_id = compatibility_version.get("version_id")
                self.assertRegex(str(compatibility_version_id), r"^pv_[0-9a-f]{32}$")
                page.wait_for_function(
                    "() => document.querySelector('#project-versions-status')?.textContent.includes('版本已儲存')",
                    timeout=30_000,
                )

                records_dir = self.project / "working/project_versions/records"
                records_before_compat_edit = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(records_dir.glob("pv_*.json"))
                ]
                compatibility_record = next(
                    record
                    for record in records_before_compat_edit
                    if record.get("version_id") == compatibility_version_id
                )
                saved_snapshot = compatibility_record.get("snapshot_state") or {}
                saved_caption = next(
                    item
                    for item in saved_snapshot.get("overlays", [])
                    if item.get("type") == "caption"
                )
                self.assertEqual(
                    saved_snapshot.get("publishing", {}).get("generator"),
                    "deterministic-transcript-draft-v1",
                )
                self.assertEqual(
                    (saved_caption.get("semantic_review") or {}).get("status"),
                    "resolved_manual",
                )
                self.assertTrue((saved_caption.get("semantic_review") or {}).get("resolved_at"))
                self._write_evidence_json(
                    "state-compat/disk/saved-record.json",
                    {
                        "version_id": compatibility_record.get("version_id"),
                        "kind": compatibility_record.get("kind"),
                        "publishing_generator": saved_snapshot.get("publishing", {}).get("generator"),
                        "caption_semantic_review": saved_caption.get("semantic_review"),
                    },
                )

                stage("state-compat-edit-after-save")
                page.locator("#project-versions-close").click()
                page.locator("#project-versions-dialog").wait_for(state="hidden", timeout=30_000)
                caption_row = page.locator(".layer-row").filter(has_text="字幕").first
                caption_row.click()
                caption_input = page.locator("#overlay-text")
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/editor-state")
                    and response.status == 200,
                    timeout=90_000,
                ):
                    caption_input.fill("P7 compatibility changed after save")
                page.wait_for_function(
                    "() => document.querySelector('#save-state')?.classList.contains('is-saved') && document.querySelector('#save-state')?.textContent.includes('已儲存')",
                    timeout=90_000,
                )
                compatibility_changed_state, _compatibility_changed_manifest = self._read_state_manifest()
                changed_caption = next(
                    item
                    for item in compatibility_changed_state.get("overlays", [])
                    if item.get("type") == "caption"
                )
                self.assertEqual(changed_caption.get("text"), "P7 compatibility changed after save")

                stage("state-compat-restore-and-remained-cleared")
                compat_listing_before_restore = self._open_versions(page)
                auto_before_compat = sum(
                    item.get("kind") == "auto_before_restore"
                    for item in [
                        json.loads(path.read_text(encoding="utf-8"))
                        for path in sorted(records_dir.glob("pv_*.json"))
                    ]
                )
                compat_row = page.locator(".project-version-row").filter(
                    has_text="P7 State Compatibility"
                ).first
                compat_row.wait_for(timeout=30_000)
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and response.url.endswith(f"/api/project-versions/{compatibility_version_id}"),
                    timeout=90_000,
                ):
                    compat_row.click()
                page.locator("#project-version-detail").get_by_role(
                    "button", name="還原此版本"
                ).click()
                page.locator("#project-version-restore-dialog[open]").wait_for(timeout=30_000)
                compat_restore_capture_key = "p7-state-compat-restore-capture"
                page.evaluate(
                    """
                    ([captureKey, expectedPath]) => {
                      const nativeFetch = window.fetch.bind(window);
                      window.fetch = async (...args) => {
                        const response = await nativeFetch(...args);
                        const request = args[0];
                        const options = args[1] || {};
                        const url = typeof request === 'string' ? request : request.url;
                        const method = String(options.method || request?.method || 'GET').toUpperCase();
                        const target = new URL(url, window.location.href);
                        if (
                          method === 'POST'
                          && target.origin === window.location.origin
                          && target.pathname === expectedPath
                        ) {
                          const payload = await response.clone().json();
                          sessionStorage.setItem(captureKey, JSON.stringify({
                            http_status: response.status,
                            payload: {
                              ok: payload?.ok,
                              automatic_backup: payload?.automatic_backup,
                              reload_required: payload?.reload_required,
                            },
                          }));
                        }
                        return response;
                      };
                    }
                    """,
                    [
                        compat_restore_capture_key,
                        f"/api/project-versions/{compatibility_version_id}/restore",
                    ],
                )
                with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(f"/api/project-versions/{compatibility_version_id}/restore"),
                        timeout=90_000,
                    ) as compat_restore_info:
                        page.locator("#project-version-restore-confirm").click()
                compat_restore_response = compat_restore_info.value
                compat_restore_capture = page.evaluate(
                    """
                    captureKey => {
                      const raw = sessionStorage.getItem(captureKey);
                      sessionStorage.removeItem(captureKey);
                      return raw ? JSON.parse(raw) : null;
                    }
                    """,
                    compat_restore_capture_key,
                )
                self.assertIsInstance(compat_restore_capture, dict)
                self.assertEqual(
                    compat_restore_capture.get("http_status"),
                    compat_restore_response.status,
                    compat_restore_capture,
                )
                compat_restore_payload = compat_restore_capture.get("payload") or {}
                self.assertEqual(compat_restore_response.status, 200, compat_restore_payload)
                self.assertTrue(compat_restore_payload.get("ok"), compat_restore_payload)
                self.assertTrue(compat_restore_payload.get("reload_required"), compat_restore_payload)
                compat_backup = compat_restore_payload.get("automatic_backup") or {}
                self.assertEqual(compat_backup.get("kind"), "auto_before_restore")
                self.assertRegex(str(compat_backup.get("version_id")), r"^pv_[0-9a-f]{32}$")
                self._wait_initialized(page)
                compatibility_state_restored, compatibility_manifest_restored = self._read_state_manifest()
                restored_caption = next(
                    item
                    for item in compatibility_state_restored.get("overlays", [])
                    if item.get("type") == "caption"
                )
                self.assertEqual(restored_caption.get("text"), "P7 semantic corrected")
                self.assertEqual(
                    (restored_caption.get("semantic_review") or {}).get("status"),
                    "resolved_manual",
                )
                self.assertTrue((restored_caption.get("semantic_review") or {}).get("resolved_at"))
                self.assertEqual(
                    compatibility_state_restored.get("publishing", {}).get("generator"),
                    "deterministic-transcript-draft-v1",
                )
                # The earlier response-loss restore already cleared authority;
                # this flow proves the compatibility restore does not resurrect it.
                self.assertFalse((compatibility_state_restored.get("rights") or {}).get("asserted"))
                self.assertTrue(
                    all(
                        not approval.get("approved")
                        for approval in compatibility_manifest_restored.get("approvals", {}).values()
                        if isinstance(approval, dict)
                    )
                )
                compatibility_rights = json.loads(
                    (self.project / "working/rights_assertion.json").read_text(encoding="utf-8")
                )
                self.assertEqual(compatibility_rights.get("items"), [])
                records_after_compat_restore = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(records_dir.glob("pv_*.json"))
                ]
                auto_after_compat = [
                    record
                    for record in records_after_compat_restore
                    if record.get("kind") == "auto_before_restore"
                ]
                self.assertEqual(len(auto_after_compat), auto_before_compat + 1)
                compatibility_backup_record = next(
                    record
                    for record in auto_after_compat
                    if record.get("version_id") == compat_backup.get("version_id")
                )
                self.assertEqual(
                    (compatibility_backup_record.get("snapshot_state") or {})
                    .get("publishing", {})
                    .get("generator"),
                    "deterministic-transcript-draft-v1",
                )
                self._write_evidence_json(
                    "state-compat/result.json",
                    {
                        "outcome": "CONFIRMED_STATE_COMPATIBILITY_UI",
                        "copy_generator_persisted": True,
                        "resolved_manual_persisted": True,
                        "named_version_saved": compatibility_version_id,
                        "restore_http_status": compat_restore_response.status,
                        "automatic_backup": compat_backup.get("version_id"),
                        "rights_and_approvals_remained_cleared": True,
                    },
                )
                stage("confirmed")
        except Exception as error:
            self._write_failure_evidence(error)
            raise
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()

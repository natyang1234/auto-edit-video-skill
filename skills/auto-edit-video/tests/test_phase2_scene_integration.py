"""Real primary-renderer smoke for the complete Phase 2 scene vocabulary."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import asset_registry  # noqa: E402
import editor_server  # noqa: E402
import qa_video  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402
import visual_director  # noqa: E402
from visual_quality import rendered_visual_quality_report  # noqa: E402
from visual_motion_probe import measure_declared_motion  # noqa: E402


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "needs local ffmpeg",
)
class Phase2SceneIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="phase2-scenes-")
        self.project = Path(self.temp.name)
        (self.project / "source").mkdir(parents=True)
        (self.project / "working").mkdir()
        (self.project / "assets").mkdir()
        self.source = self.project / "source/source.mp4"
        result = subprocess.run(
            [
                renderer.ffmpeg_path(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=180x320:r=15:d=88",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "88",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(self.source),
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _evidence(
        kind: str, literal: str, start: float, end: float, tag: str
    ) -> dict:
        return {
            "id": f"evidence-{tag * 4}",
            "kind": kind,
            "literal": literal,
            "start": start,
            "end": end,
            "confidence": 1.0,
            "review_status": "approved",
        }

    def _approved_assets(self) -> list[dict]:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        digest = hashlib.sha256(png).hexdigest()
        items = []
        for index in (1, 2, 3):
            relative = f"assets/owned-{index}.png"
            (self.project / relative).write_bytes(png)
            items.append(
                {
                    "asset_id": f"owned-{index}",
                    "path": relative,
                    "sha256": digest,
                    "origin": "user-upload",
                    "provider_id": None,
                    "source_url": None,
                    "license": {
                        "spdx": "CC0-1.0",
                        "attribution_required": False,
                        "attribution_text": "",
                        "verified_at": "2026-08-12T03:00:00+08:00",
                    },
                    "review_status": "approved",
                }
            )
        asset_registry.save_registry(
            self.project, {"schema_version": 1, "items": items}
        )
        return items

    def test_all_seven_families_render_one_primary_mp4_and_contact_sheet(self) -> None:
        segments = [
            {
                "id": f"segment-{index}",
                "source_start": index * 8.0,
                "source_end": (index + 1) * 8.0,
            }
            for index in range(11)
        ]
        evidence = [
            self._evidence("quote", "完整流程從這裡開始", 0.5, 2.0, "1011"),
            self._evidence("quote", "首先整理資料", 8.5, 9.5, "2011"),
            self._evidence("quote", "其次確認內容", 10.5, 11.5, "3011"),
            self._evidence("quote", "最後輸出成片", 12.5, 13.5, "4011"),
            self._evidence("number", "12%", 16.5, 17.0, "5011"),
            self._evidence("number", "34%", 18.5, 19.0, "6011"),
            self._evidence("number", "56%", 20.5, 21.0, "7011"),
            self._evidence("quote", "這裡保留人像呼吸", 24.5, 25.5, "8011"),
            self._evidence("number", "87%", 32.5, 33.0, "9011"),
            self._evidence("quote", "目前已完成 75% 進度", 40.2, 41.2, "a011"),
            self._evidence("number", "75%", 40.5, 40.8, "b011"),
            self._evidence("quote", "這裡再留一次人像呼吸", 48.5, 49.5, "c011"),
            self._evidence("quote", "指令：npm run build", 56.5, 57.5, "d011"),
            self._evidence("quote", "接下來看看這三張範例圖片", 64.5, 65.5, "e011"),
            self._evidence("quote", "接下來談最後總結", 72.5, 73.5, "f011"),
            self._evidence("quote", "最後讓畫面回到講者", 80.5, 81.5, "ab11"),
        ]
        planned = visual_director.plan_visuals(
            segments,
            evidence,
            visual_density="dense",
            kinetic_scene_vocabulary=True,
            project_assets=self._approved_assets(),
        )
        self.assertEqual(visual_director.validate(planned), [])
        families = {
            item.get("family")
            for item in planned["visual_plan"]["items"]
            if item.get("eligibility") == "eligible"
        }
        self.assertEqual(
            families,
            {
                "title_reveal",
                "staggered_list",
                "analytics_dashboard",
                "count_stat",
                "asset_mosaic",
                "grid_progress",
                "typed_prompt",
            },
        )
        breathing = [
            item
            for item in planned["visual_plan"]["items"]
            if item["beat"] == "a_roll_breathing"
        ]
        breathing_share = sum(
            item["end"] - item["start"] for item in breathing
        ) / 88.0
        self.assertGreaterEqual(len(breathing), 2)
        self.assertGreaterEqual(
            sum(item["end"] - item["start"] >= 2.0 for item in breathing),
            2,
        )
        self.assertGreaterEqual(breathing_share, 0.25)
        self.assertLessEqual(breathing_share, 0.55)
        editor_server.publish_layer_bundle(
            self.project,
            planned["structured_layers"],
            planned["visual_plan"],
        )
        manifest = {
            "schema_version": 1,
            "project_id": "phase2-scene-smoke",
            "source": {
                "staged_path": "source/source.mp4",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "duration_s": 88.0,
                "width": 180,
                "height": 320,
                "has_audio": True,
            },
        }
        state = {
            "schema_version": 2,
            "project_id": "phase2-scene-smoke",
            "source_sha256": manifest["source"]["sha256"],
            "director_style": "kinetic-explainer",
            "style_pack": {
                "project_default": "kinetic-social",
                "per_highlight": {},
            },
            "canvas": {"width": 540, "height": 960, "fps": 15, "fit": "cover"},
            "segments": [
                {"id": "source-all", "source_start": 0.0, "source_end": 88.0}
            ],
            "overlays": [],
        }
        (self.project / "project.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.project / "working/editor_state.json").write_text(
            json.dumps(state, sort_keys=True) + "\n", encoding="utf-8"
        )
        output = self.project / "phase2-seven-families.mp4"
        motion_base = self.project / "working/phase2-motion-base.mkv"
        raw_evidence: dict = {}
        command = renderer.build_render_command(
            self.project,
            state,
            manifest,
            output,
            "final",
            visual_evidence=raw_evidence,
            motion_base_output=motion_base,
        )
        delivered_breathing = raw_evidence["a_roll_breathing_intervals"]
        self.assertEqual(len(delivered_breathing), len(breathing))
        self.assertEqual(
            sum(item["end"] - item["start"] for item in delivered_breathing),
            sum(item["end"] - item["start"] for item in breathing),
        )
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertTrue(output.is_file())
        self.assertTrue(motion_base.is_file())
        raw_evidence["motion_input"] = {
            "base_path": str(motion_base),
            "base_sha256": hashlib.sha256(motion_base.read_bytes()).hexdigest(),
            "canvas_width": 540,
            "canvas_height": 960,
            "fps": 15,
        }
        probes = measure_declared_motion(output, raw_evidence)
        raw_evidence["motion_probes"] = probes
        self.assertEqual(len(probes), 8)
        self.assertTrue(all(probe["detected"] for probe in probes.values()), probes)
        report = rendered_visual_quality_report(raw_evidence)
        self.assertEqual(report["status"], "pass", report["failures"])
        delivered_families = {item.get("family") for item in report["items"]}
        self.assertEqual(delivered_families, families)
        self.assertTrue(all(item.get("source") == "structured_card" for item in report["items"]))
        self.assertFalse(
            (self.project / "working/graphic_packages").exists(),
            "the canonical scene plan must not invoke the legacy second-renderer package",
        )
        contact = self.project / "phase2-seven-families-contact.png"
        qa_video.contact_sheet(output, contact, 88.0)
        self.assertEqual(contact.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        preserved = os.environ.get("PHASE2_SCENE_ARTIFACT_DIR", "").strip()
        if preserved:
            destination = Path(preserved)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, destination / output.name)
            shutil.copy2(contact, destination / contact.name)
            (destination / "renderer-evidence.json").write_text(
                json.dumps(raw_evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()

"""Phase 1a M2: whole-video analysis, OCR sampling and checkpoint cache."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_registry  # noqa: E402
import video_analyzer  # noqa: E402
import vision_ocr  # noqa: E402
from video_analyzer import sample_timestamps  # noqa: E402


class SamplingTests(unittest.TestCase):
    def test_dedupes_shot_and_grid_timestamps(self) -> None:
        stamps = sample_timestamps(30.0, [0.0, 0.2, 10.1], interval_s=10.0, dedupe_s=0.5)
        self.assertEqual(stamps, [0.0, 10.0, 20.0])

    def test_total_frame_cap(self) -> None:
        shots = [float(i) for i in range(0, 1200)]
        stamps = sample_timestamps(1200.0, shots, max_per_minute=1000, max_frames=300)
        self.assertEqual(len(stamps), 300)

    def test_per_minute_cap(self) -> None:
        shots = [i * 0.6 for i in range(100)]  # 100 shots inside the first minute
        stamps = sample_timestamps(60.0, shots, max_per_minute=30)
        self.assertLessEqual(len(stamps), 30)

    def test_ignores_out_of_range(self) -> None:
        stamps = sample_timestamps(5.0, [-1.0, 4.9, 9.0], interval_s=10.0)
        self.assertTrue(all(0.0 <= t < 5.0 for t in stamps))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class BurnedInVerdictTests(unittest.TestCase):
    """Position alone cannot tell a caption row from station furniture."""

    @staticmethod
    def frames(texts: list[str], center_y: float = 0.16) -> dict:
        # One qualifying line per sampled frame, all in the caption band.
        return {
            float(index) * 4.0: [
                {
                    "text": text,
                    "box": {"x": 0.25, "y": center_y - 0.02, "width": 0.5, "height": 0.04},
                }
            ]
            for index, text in enumerate(texts)
        }

    def test_a_changing_caption_row_is_detected(self) -> None:
        verdict = video_analyzer.burned_in_verdict(
            self.frames(
                ["週末別窩在家", "中央公園三號出口", "旁邊巷子", "上二樓", "跟我進來", "訂位"]
            )
        )
        self.assertEqual(verdict["status"], "detected")

    def test_a_station_watermark_is_not_a_caption_row(self) -> None:
        # A phone-number banner sits low, sits centred and never moves — the
        # only thing that separates it from subtitles is that it never
        # changes. Calling it a caption row silently drops every subtitle.
        verdict = video_analyzer.burned_in_verdict(self.frames(["加倍"] * 12))
        self.assertEqual(verdict["status"], "absent")
        self.assertEqual(verdict["distinct_texts"], 1)

    def test_a_watermark_with_ocr_noise_is_still_not_a_caption_row(self) -> None:
        # Real OCR reads the same banner slightly differently frame to frame.
        noisy = ["加倍", "加", "成效加倍", "加倍"] * 3
        verdict = video_analyzer.burned_in_verdict(self.frames(noisy))
        self.assertEqual(verdict["status"], "absent")

    def test_scenery_text_that_drifts_is_not_a_caption_row(self) -> None:
        frames = {}
        for index, text in enumerate(["多采", "LOUNGE", "營業中", "多采", "招牌", "小門"]):
            offset = 0.10 + index * 0.03  # the camera moves; the sign moves
            frames[float(index) * 4.0] = [
                {"text": text,
                 "box": {"x": 0.3, "y": offset, "width": 0.4, "height": 0.04}}
            ]
        self.assertEqual(video_analyzer.burned_in_verdict(frames)["status"], "absent")

    def test_no_boxes_at_all_is_not_a_conclusion(self) -> None:
        verdict = video_analyzer.burned_in_verdict({})
        self.assertEqual(verdict["status"], "not_configured")


class AnalyzeVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-analyze-tests-")
        base = Path(cls._tmp.name)
        cls.folder = base / "素材"
        cls.folder.mkdir()
        video = cls.folder / "main.mp4"
        # Two visually distinct halves (shot boundary) + burned-in text + tone.
        subprocess.run(
            [
                video_analyzer.ffmpeg_path(), "-y",
                "-f", "lavfi",
                "-i",
                "testsrc=size=480x270:rate=30:duration=1.0,"
                "drawtext=text='OCR TEST 123':fontsize=48:fontcolor=white:"
                "box=1:boxcolor=black:x=(w-text_w)/2:y=(h-text_h)/2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0",
                "-shortest", "-pix_fmt", "yuv420p", str(video),
            ],
            check=True, capture_output=True,
        )
        cls.project = base / "project"
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "auto_edit.py"), "ingest-folder",
                "--folder", str(cls.folder), "--project-dir", str(cls.project),
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_analysis_contract_cache_and_resume(self) -> None:
        analysis, stats = video_analyzer.analyze(self.project)
        self.assertEqual(contract_registry.validate_artifact("video_analysis", analysis), [])
        self.assertGreater(analysis["duration_s"], 0.9)
        self.assertEqual(analysis["width"], 480)
        self.assertGreaterEqual(len(analysis["shots"]), 1)
        self.assertTrue(all(shot["kind"] == "unknown" for shot in analysis["shots"]))
        self.assertEqual(analysis["engines"]["asr"]["status"], "not_configured")
        self.assertIn("gt(scene,0.4)", analysis["engines"]["shot_detector"]["version"])
        first_computed = {name for name, state in stats.items() if state == "computed"}
        self.assertIn("probe", first_computed)

        # Second run: every stage must come from cache.
        _analysis, stats2 = video_analyzer.analyze(self.project)
        recomputed = {name for name, state in stats2.items() if state == "computed"}
        self.assertEqual(recomputed, set(), f"expected full cache hit, got {stats2}")

        # Simulated interruption: drop one stage; only that stage recomputes.
        (self.project / "working/analysis_cache/shots.json").unlink()
        _analysis, stats3 = video_analyzer.analyze(self.project)
        recomputed = {name for name, state in stats3.items() if state == "computed"}
        self.assertIn("shots", recomputed)
        self.assertNotIn("probe", recomputed)
        self.assertNotIn("silence", recomputed)

        # Torn cache entries must not count as hits.
        cache = self.project / "working/analysis_cache/silence.json"
        entry = json.loads(cache.read_text("utf-8"))
        entry["payload"] = [{"start": 0.0, "end": 0.1}]  # tampered, hash mismatch
        cache.write_text(json.dumps(entry), "utf-8")
        _analysis, stats4 = video_analyzer.analyze(self.project)
        self.assertEqual(stats4.get("silence"), "computed")

    def test_ocr_disabled_yields_not_configured_and_empty_spans(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"AUTO_EDIT_DISABLE_VISION": "1"}):
            vision_ocr._VISION_MODULES = None
            try:
                engine = vision_ocr.vision_engine()
                self.assertEqual(engine["status"], "not_configured")
                for entry in (self.project / "working/analysis_cache").glob("ocr.json"):
                    entry.unlink()
                analysis, stats = video_analyzer.analyze(self.project)
            finally:
                vision_ocr._VISION_MODULES = None
        self.assertEqual(analysis["engines"]["ocr"]["status"], "not_configured")
        self.assertEqual(analysis["ocr_spans"], [])
        self.assertEqual(stats.get("ocr"), "not_configured")
        self.assertEqual(contract_registry.validate_artifact("video_analysis", analysis), [])

    @unittest.skipUnless(sys.platform == "darwin" and vision_ocr._load_vision(), "needs macOS Vision")
    def test_vision_ocr_reads_burned_in_text(self) -> None:
        for entry in (self.project / "working/analysis_cache").glob("ocr.json"):
            entry.unlink()
        analysis, _stats = video_analyzer.analyze(self.project)
        self.assertEqual(analysis["engines"]["ocr"]["status"], "present")
        joined = " ".join(span["text"] for span in analysis["ocr_spans"])
        self.assertTrue(
            "OCR" in joined or "123" in joined,
            f"burned-in text not recognised: {joined!r}",
        )


import unittest.mock  # noqa: E402


if __name__ == "__main__":
    unittest.main()

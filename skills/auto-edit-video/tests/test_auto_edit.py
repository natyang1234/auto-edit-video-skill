from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts/auto_edit.py"
RUMI_FIXTURE = Path(__file__).resolve().parent / "fixtures/rumi_voice_system.py"


class AutoEditVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-video-tests-")
        cls.root = Path(cls._tmp.name)
        cls.source = cls.root / "source.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise unittest.SkipTest("ffmpeg is unavailable")
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.4",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                str(cls.source),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["RUMI_VOICE_SYSTEM"] = str(RUMI_FIXTURE)
        result = subprocess.run(
            ["python3", str(CLI), *args],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def init_project(self, name: str, language: str, gender: str) -> tuple[Path, dict]:
        project = self.root / name
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            language,
            "--subtitle-mode",
            "source",
            "--voice-language",
            language,
            "--voice-gender",
            gender,
        )
        manifest_path = project / "project.json"
        return manifest_path, json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_chinese_female_defaults_to_rumi_clone(self) -> None:
        manifest_path, manifest = self.init_project("zh-female", "zh-TW", "female")
        voice = manifest["voiceover"]
        self.assertEqual(voice["provider"], "rumi")
        self.assertEqual(voice["engine"], "rumi-voice-system")
        self.assertEqual(voice["voice_id"], "rumi")
        self.run_cli("validate", "--manifest", str(manifest_path))

    def test_chinese_male_defaults_to_shared_fish_voice(self) -> None:
        _, manifest = self.init_project("zh-male", "zh-TW", "male")
        voice = manifest["voiceover"]
        self.assertEqual(voice["provider"], "rumi")
        self.assertEqual(voice["voice_id"], "溫暖磁性男聲旁白")

    def test_english_defaults_to_edge(self) -> None:
        _, manifest = self.init_project("en-female", "en-US", "female")
        voice = manifest["voiceover"]
        self.assertEqual(voice["provider"], "edge")
        self.assertEqual(voice["voice_id"], "en-US-AvaMultilingualNeural")

    def test_catalog_exposes_rumi_as_chinese_default(self) -> None:
        result = self.run_cli("voices", "--language", "zh-TW", "--provider", "rumi")
        payload = json.loads(result.stdout)
        rumi = next(item for item in payload["voices"] if item["voice_id"] == "rumi")
        self.assertTrue(rumi["default"])
        self.assertEqual(rumi["backend"], "fish")

    def test_rumi_dry_run_redacts_narration(self) -> None:
        manifest_path, _ = self.init_project("dry-run", "zh-TW", "female")
        script = manifest_path.parent / "voice/narration.txt"
        secret_text = "這是一段不應出現在 dry-run 輸出的未公開文案"
        script.write_text(secret_text, encoding="utf-8")
        result = self.run_cli(
            "synthesize-rumi",
            "--manifest",
            str(manifest_path),
            "--script",
            str(script),
            "--dry-run",
        )
        self.assertNotIn(secret_text, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["backend"], "fish")
        self.assertIn("<narration text redacted>", payload["command"])

    def test_gender_mismatch_is_rejected(self) -> None:
        manifest_path, manifest = self.init_project("gender-mismatch", "zh-TW", "female")
        manifest["voiceover"]["voice_id"] = "溫暖磁性男聲旁白"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = self.run_cli(
            "validate",
            "--manifest",
            str(manifest_path),
            expected=2,
        )
        payload = json.loads(result.stdout)
        self.assertIn(
            "selected Rumi-system voice does not match the requested gender",
            payload["errors"],
        )

    def test_import_whisper_and_analyze_low_risk_edits(self) -> None:
        project = self.root / "whisper-analysis"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-TW",
            "--subtitle-mode",
            "source",
        )
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["duration_s"] = 0.4
        manifest["editing"]["silence_threshold_s"] = 0.08
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        whisper = {
            "language": "zh",
            "text": "嗯 我 我 喜歡",
            "segments": [
                {
                    "start": 0.02,
                    "end": 0.38,
                    "text": "嗯 我 我 喜歡",
                    "words": [
                        {"word": "嗯", "start": 0.02, "end": 0.07, "probability": 0.91},
                        {"word": "我", "start": 0.18, "end": 0.22, "probability": 0.95},
                        {"word": "我", "start": 0.24, "end": 0.28, "probability": 0.96},
                        {"word": "喜歡", "start": 0.30, "end": 0.38, "probability": 0.94},
                    ],
                }
            ],
        }
        whisper_path = project / "working/whisper.json"
        whisper_path.write_text(
            json.dumps(whisper, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        srt_path = project / "working/whisper.srt"
        srt_path.write_text(
            "1\n00:00:00,020 --> 00:00:00,380\n嗯 我 我 喜歡\n",
            encoding="utf-8",
        )

        imported = self.run_cli(
            "import-whisper",
            "--manifest",
            str(manifest_path),
            "--whisper-json",
            str(whisper_path),
            "--srt",
            str(srt_path),
            "--model",
            "base",
        )
        imported_payload = json.loads(imported.stdout)
        self.assertEqual(imported_payload["words"], 4)
        self.assertTrue((project / "subtitles/source.srt").is_file())

        analyzed = self.run_cli("analyze-edits", "--manifest", str(manifest_path))
        analyzed_payload = json.loads(analyzed.stdout)
        self.assertTrue(analyzed_payload["review_required"])
        self.assertEqual(analyzed_payload["counts"], {"filler": 1, "silence": 1, "stutter": 1})
        candidates = json.loads(
            (project / "working/edit_candidates.json").read_text(encoding="utf-8")
        )["items"]
        self.assertEqual({item["type"] for item in candidates}, {"filler", "silence", "stutter"})
        self.assertTrue(all(item["review_status"] == "pending" for item in candidates))
        updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_manifest["stages"]["edit_review"], "needs_review")
        self.assertFalse(updated_manifest["approvals"]["destructive_edit"]["approved"])

    def test_plan_overlays_creates_reviewable_emphasis_and_cards(self) -> None:
        project = self.root / "overlay-planning"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-TW",
            "--subtitle-mode",
            "source",
        )
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["duration_s"] = 8.0
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (project / "working/transcript_words.json").write_text(
            json.dumps(
                {
                    "text": "先說結論 最大的差別是 3 倍",
                    "segments": [
                        {"id": "segment-1", "start": 0.0, "end": 1.5, "text": "先說結論"},
                        {
                            "id": "segment-2",
                            "start": 3.2,
                            "end": 5.4,
                            "text": "最大的差別是 3 倍",
                        },
                    ],
                    "words": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (project / "working/editor_state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "overlay-planning",
                    "canvas": {
                        "platform_id": "instagram-reels",
                        "width": 1080,
                        "height": 1920,
                        "fps": 30,
                        "fit": "cover",
                    },
                    "director_style": "teacher-punch",
                    "caption_defaults": {
                        "font_size": 58,
                        "color": "#f7f2e8",
                        "emphasis_color": "#ffd447",
                        "stroke_color": "#17130f",
                        "stroke_width": 5,
                        "x": 50,
                        "y": 76,
                        "max_width": 86,
                        "animation": "pop",
                    },
                    "overlays": [
                        {
                            "id": "manual-caption",
                            "type": "caption",
                            "start": 0.0,
                            "end": 1.5,
                            "text": "先說結論",
                            "source": "manual",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_cli("plan-overlays", "--manifest", str(manifest_path))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["emphasis_items"], 1)
        self.assertEqual(payload["visual_items"], 2)
        self.assertEqual(payload["synced_editor_overlays"], 3)
        emphasis = json.loads(
            (project / "working/emphasis_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(emphasis["items"][0]["text"], "3 倍")
        visuals = json.loads(
            (project / "working/visual_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual({item["type"] for item in visuals["items"]}, {"title_card", "data_card"})
        state = json.loads(
            (project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {item["type"] for item in state["overlays"]},
            {"caption", "emphasis", "title", "card"},
        )
        updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_manifest["stages"]["timeline_review"], "needs_review")


if __name__ == "__main__":
    unittest.main()

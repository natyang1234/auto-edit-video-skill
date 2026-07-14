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

    def run_cli(
        self,
        *args: str,
        expected: int = 0,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["RUMI_VOICE_SYSTEM"] = str(RUMI_FIXTURE)
        env.update(extra_env or {})
        result = subprocess.run(
            ["python3", str(CLI), *args],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def test_local_transcription_and_highlight_planning_sync_editor(self) -> None:
        project = self.root / "local-pipeline"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-TW",
            "--duration-profile",
            "auto",
        )
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = {
            "schema_version": 1,
            "project_id": manifest["project_id"],
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "fit": "cover",
                "show_safe_zones": True,
            },
            "director_style": "high-energy",
            "editing_brief": "保留三倍這個結論",
            "caption_defaults": {
                "font_size": 58,
                "x": 50,
                "y": 76,
                "max_width": 86,
            },
            "overlays": [],
            "publishing": {},
            "review": {"selected_overlay_id": None},
        }
        (project / "working/editor_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        fake_whisper = self.root / "fake-whisper"
        fake_whisper.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
payload = {
    'text': '你以為只是運氣嗎？其實完整看完的人增加三倍。',
    'language': 'zh',
    'segments': [
        {
            'start': 0.02,
            'end': 0.36,
            'text': '你以為只是運氣嗎？其實完整看完的人增加三倍。',
            'words': [
                {'word': '你以為只是運氣嗎？', 'start': 0.02, 'end': 0.16, 'probability': 0.95},
                {'word': '其實完整看完的人增加三倍。', 'start': 0.17, 'end': 0.36, 'probability': 0.93},
            ],
        }
    ],
}
(output / f'{source.stem}.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
(output / f'{source.stem}.srt').write_text('1\\n00:00:00,020 --> 00:00:00,360\\n你以為只是運氣嗎？其實完整看完的人增加三倍。\\n', encoding='utf-8')
""",
            encoding="utf-8",
        )
        fake_whisper.chmod(0o755)

        transcribed = self.run_cli(
            "transcribe-local",
            "--manifest",
            str(manifest_path),
            "--model",
            "base",
            "--timeout",
            "30",
            extra_env={"WHISPER_BIN": str(fake_whisper)},
        )
        transcript_payload = json.loads(transcribed.stdout)
        self.assertEqual(transcript_payload["words"], 2)
        self.assertEqual(transcript_payload["synced_editor_captions"], 1)
        synced_state = json.loads(
            (project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synced_state["overlays"][0]["type"], "caption")

        planned = self.run_cli(
            "plan-highlights",
            "--manifest",
            str(manifest_path),
            "--director",
            "high-energy",
            "--count",
            "3",
            "--brief",
            "保留三倍這個結論",
        )
        plan_payload = json.loads(planned.stdout)
        self.assertEqual(plan_payload["status"], "needs_review")
        self.assertGreaterEqual(plan_payload["items"], 1)
        plan = json.loads((project / "working/highlight_plan.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["review_status"] == "pending" for item in plan["items"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["highlight_plan"], "needs_review")
        self.assertFalse(manifest["approvals"]["highlight_selection"]["approved"])
        synced_state = json.loads(
            (project / "working/editor_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synced_state["highlight_plan_revision"], plan["plan_revision"])
        self.assertGreaterEqual(len(synced_state["highlights"]), 1)
        self.assertEqual(synced_state["canvas"]["fit"], "contain")
        self.assertEqual(synced_state["visual_quality_mode"], "designed")
        for highlight in synced_state["highlights"]:
            cards = [
                item
                for item in synced_state["overlays"]
                if item.get("highlight_id") == highlight["id"] and item.get("design_role")
            ]
            self.assertEqual(len(cards), 5)
            self.assertEqual(
                {item["design_role"] for item in cards},
                {"hook", "concept", "rule", "memory", "recap"},
            )

    def test_mixed_zh_en_transcription_preserves_glossary_terms(self) -> None:
        project = self.root / "mixed-language-pipeline"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "It; to V; cigar; cigarette",
        )
        manifest_path = project / "project.json"
        fake_whisper = self.root / "fake-whisper-mixed"
        fake_whisper.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
prompt = sys.argv[sys.argv.index('--initial_prompt') + 1] if '--initial_prompt' in sys.argv else ''
language = sys.argv[sys.argv.index('--language') + 1] if '--language' in sys.argv else 'auto'
preserved = language == 'zh' and all(term.lower() in prompt.lower() for term in ('It', 'to V', 'cigar', 'cigarette'))
text = 'It 作虛主詞，看到 It，想到 to V。' if preserved else 'Ed 作虛主詞，看到 Ed，想到圖筆。'
payload = {
    'text': text,
    'language': 'zh',
    'segments': [{
        'start': 0.02,
        'end': 0.36,
        'text': text,
        'words': [
            {'word': 'It', 'start': 0.02, 'end': 0.09, 'probability': 0.95},
            {'word': ' 作虛主詞，', 'start': 0.09, 'end': 0.20, 'probability': 0.95},
            {'word': '看到 It，想到 to V。', 'start': 0.20, 'end': 0.36, 'probability': 0.94},
        ],
    }],
}
(output / f'{source.stem}.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
(output / f'{source.stem}.srt').write_text(f'1\\n00:00:00,020 --> 00:00:00,360\\n{text}\\n', encoding='utf-8')
""",
            encoding="utf-8",
        )
        fake_whisper.chmod(0o755)

        result = self.run_cli(
            "transcribe-local",
            "--manifest",
            str(manifest_path),
            "--model",
            "base",
            "--timeout",
            "30",
            extra_env={"WHISPER_BIN": str(fake_whisper)},
        )
        payload = json.loads(result.stdout)
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertIn("It 作虛主詞", transcript["text"])
        self.assertIn("to V", transcript["text"])
        self.assertNotIn("圖筆", transcript["text"])
        self.assertTrue(payload["code_switching"])
        self.assertEqual(manifest["subtitles"]["source_language"], "zh-en")
        self.assertEqual(
            manifest["subtitles"]["glossary"],
            ["It", "to V", "cigar", "cigarette"],
        )

    def test_mixed_language_long_glossary_uses_bounded_prompt_to_protect_chinese(self) -> None:
        project = self.root / "mixed-language-bounded-prompt"
        glossary = (
            "It; to V; for example; It is fun to play with puppies; "
            "play with puppies; cigar; cigarette; "
            "It is bad for you to smoke a cigar; "
            "It is bad for you to smoke a cigarette; "
            "to get good grades; good grades; I study hard; "
            "to go to bed early; to sleep well; to lose weight; "
            "stay away from the smoke; I walked away"
        )
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            glossary,
        )
        manifest_path = project / "project.json"
        fake_whisper = self.root / "fake-whisper-bounded-prompt"
        fake_whisper.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
prompt = sys.argv[sys.argv.index('--initial_prompt') + 1] if '--initial_prompt' in sys.argv else ''
(output / 'captured-prompt.txt').write_text(prompt, encoding='utf-8')
safe = (
    len(prompt) <= 180
    and 'It' in prompt
    and 'to V' in prompt
    and 'cigar' in prompt
    and 'cigarette' in prompt
    and 'It is bad for you to smoke a cigarette' not in prompt
)
text = (
    'It is fun to play with puppies，這個怎麼翻譯呢？玩小狗很有趣。'
    if safe
    else 'It is fun to play with puppies，這個怎麼翻譯呢？玩小狗和柚去。'
)
payload = {
    'text': text,
    'language': 'zh',
    'segments': [{
        'start': 0.02,
        'end': 0.60,
        'text': text,
        'words': [{'word': text, 'start': 0.02, 'end': 0.60, 'probability': 0.92}],
    }],
}
(output / f'{source.stem}.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
(output / f'{source.stem}.srt').write_text(f'1\\n00:00:00,020 --> 00:00:00,600\\n{text}\\n', encoding='utf-8')
""",
            encoding="utf-8",
        )
        fake_whisper.chmod(0o755)

        self.run_cli(
            "transcribe-local",
            "--manifest",
            str(manifest_path),
            "--model",
            "base",
            "--timeout",
            "30",
            extra_env={"WHISPER_BIN": str(fake_whisper)},
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )
        prompt_path = Path(manifest["transcription"]["source_json"]).with_name(
            "captured-prompt.txt"
        )
        prompt = prompt_path.read_text(encoding="utf-8")

        self.assertLessEqual(len(prompt), 180)
        self.assertNotIn("It is bad for you to smoke a cigarette", prompt)
        self.assertIn("玩小狗很有趣", transcript["text"])
        self.assertNotIn("和柚去", transcript["text"])

    def test_auto_language_preserves_incidental_english_without_glossary(self) -> None:
        project = self.root / "auto-language-code-switch"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "auto",
        )
        manifest_path = project / "project.json"
        fake_whisper = self.root / "fake-whisper-auto-code-switch"
        fake_whisper.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
prompt = sys.argv[sys.argv.index('--initial_prompt') + 1] if '--initial_prompt' in sys.argv else ''
text = '看到 It，想到 to V。' if '中文音譯' in prompt and '英文' in prompt else '看到 Ed，想到圖筆。'
payload = {
    'text': text,
    'language': 'zh',
    'segments': [{
        'start': 0.02,
        'end': 0.36,
        'text': text,
        'words': [{'word': text, 'start': 0.02, 'end': 0.36, 'probability': 0.92}],
    }],
}
(output / f'{source.stem}.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
(output / f'{source.stem}.srt').write_text(f'1\\n00:00:00,020 --> 00:00:00,360\\n{text}\\n', encoding='utf-8')
""",
            encoding="utf-8",
        )
        fake_whisper.chmod(0o755)

        self.run_cli(
            "transcribe-local",
            "--manifest",
            str(manifest_path),
            "--model",
            "base",
            "--timeout",
            "30",
            extra_env={"WHISPER_BIN": str(fake_whisper)},
        )
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )
        self.assertIn("It", transcript["text"])
        self.assertIn("to V", transcript["text"])
        self.assertNotIn("圖筆", transcript["text"])

    def test_mixed_transcript_flags_low_confidence_and_missing_terms(self) -> None:
        project = self.root / "mixed-language-review"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "It; to V",
        )
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["approvals"]["destructive_edit"] = {
            "approved": True,
            "confirmed_by": "test",
            "at": "2026-01-01T00:00:00+00:00",
            "note": "approval tied to the prior transcript",
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        whisper_json = self.root / "mixed-language-review.json"
        whisper_json.write_text(
            json.dumps(
                {
                    "text": "Ex 作虛主詞，想到圖筆。",
                    "language": "zh",
                    "segments": [
                        {
                            "start": 0.02,
                            "end": 0.36,
                            "text": "Ex 作虛主詞，想到圖筆。",
                            "words": [
                                {
                                    "word": "Ex",
                                    "start": 0.02,
                                    "end": 0.09,
                                    "probability": 0.08,
                                },
                                {
                                    "word": " 作虛主詞，想到圖筆。",
                                    "start": 0.09,
                                    "end": 0.36,
                                    "probability": 0.91,
                                },
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "import-whisper",
            "--manifest",
            str(project / "project.json"),
            "--whisper-json",
            str(whisper_json),
            "--model",
            "base",
        )
        payload = json.loads(result.stdout)
        review_path = Path(payload["transcript_review"])
        review = json.loads(review_path.read_text(encoding="utf-8"))
        codes = {item["code"] for item in review["issues"]}

        self.assertEqual(review["status"], "needs_review")
        self.assertIn("low_confidence_latin_token", codes)
        self.assertIn("missing_glossary_term", codes)
        self.assertGreaterEqual(payload["transcript_review_issues"], 3)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["approvals"]["destructive_edit"]["approved"])

    def test_glossary_fuzzy_correction_repairs_close_english_spellings(self) -> None:
        project = self.root / "mixed-language-fuzzy-glossary"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "cigarette; good grades",
        )
        whisper_json = self.root / "mixed-language-fuzzy-glossary.json"
        tokens = ["s", "ig", "arette", "，", "good", " grace"]
        words = [
            {
                "word": token,
                "start": 0.02 + index * 0.05,
                "end": 0.07 + index * 0.05,
                "probability": 0.92,
            }
            for index, token in enumerate(tokens)
        ]
        whisper_json.write_text(
            json.dumps(
                {
                    "text": "sigarette，good grace",
                    "language": "zh",
                    "segments": [
                        {
                            "start": 0.02,
                            "end": 0.36,
                            "text": "sigarette，good grace",
                            "words": words,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        whisper_srt = self.root / "mixed-language-fuzzy-glossary.srt"
        whisper_srt.write_text(
            "1\n00:00:00,020 --> 00:00:00,360\nsigarette，good grace\n",
            encoding="utf-8",
        )

        self.run_cli(
            "import-whisper",
            "--manifest",
            str(project / "project.json"),
            "--whisper-json",
            str(whisper_json),
            "--srt",
            str(whisper_srt),
            "--model",
            "base",
        )
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )
        corrected_words = " ".join(item["text"] for item in transcript["words"])

        self.assertIn("cigarette", transcript["text"])
        self.assertIn("good grades", transcript["text"])
        self.assertNotIn("sigarette", corrected_words)
        self.assertNotIn("grace", corrected_words)
        canonical_srt = (project / "subtitles/source.srt").read_text(encoding="utf-8")
        self.assertIn("cigarette", canonical_srt)
        self.assertIn("good grades", canonical_srt)
        self.assertNotIn("sigarette", canonical_srt)

    def test_glossary_repairs_split_first_word_in_multiword_phrase(self) -> None:
        project = self.root / "mixed-language-split-phrase"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "stay away from the smoke",
        )
        whisper_json = self.root / "mixed-language-split-phrase.json"
        tokens = ["St", "ay", " away", " from", " the", " smoke"]
        words = [
            {
                "word": token,
                "start": 0.02 + index * 0.05,
                "end": 0.07 + index * 0.05,
                "probability": 0.95,
            }
            for index, token in enumerate(tokens)
        ]
        whisper_json.write_text(
            json.dumps(
                {
                    "text": "Stay away from the smoke",
                    "language": "zh",
                    "segments": [
                        {
                            "start": 0.02,
                            "end": 0.32,
                            "text": "Stay away from the smoke",
                            "words": words,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "import-whisper",
            "--manifest",
            str(project / "project.json"),
            "--whisper-json",
            str(whisper_json),
            "--model",
            "base",
        )
        payload = json.loads(result.stdout)
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )

        self.assertEqual(transcript["text"].casefold(), "stay away from the smoke")
        self.assertNotIn("st stay", transcript["text"].casefold())
        self.assertEqual(payload["glossary_corrections"], 1)
        self.assertEqual(payload["transcript_review_issues"], 0)

    def test_glossary_repairs_low_confidence_it_alias_without_rewriting_names(self) -> None:
        project = self.root / "mixed-language-it-alias"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "It",
        )
        whisper_json = self.root / "mixed-language-it-alias.json"
        whisper_json.write_text(
            json.dumps(
                {
                    "text": "Ed 作虛主詞，Ed 是人名。",
                    "language": "zh",
                    "segments": [
                        {
                            "start": 0.02,
                            "end": 0.60,
                            "text": "Ed 作虛主詞，Ed 是人名。",
                            "words": [
                                {
                                    "word": "Ed",
                                    "start": 0.02,
                                    "end": 0.09,
                                    "probability": 0.08,
                                },
                                {
                                    "word": " 作虛主詞，",
                                    "start": 0.09,
                                    "end": 0.30,
                                    "probability": 0.95,
                                },
                                {
                                    "word": "Ed",
                                    "start": 0.30,
                                    "end": 0.38,
                                    "probability": 0.96,
                                },
                                {
                                    "word": " 是人名。",
                                    "start": 0.38,
                                    "end": 0.60,
                                    "probability": 0.95,
                                },
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "import-whisper",
            "--manifest",
            str(project / "project.json"),
            "--whisper-json",
            str(whisper_json),
            "--model",
            "base",
        )
        payload = json.loads(result.stdout)
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )

        self.assertIn("It 作虛主詞", transcript["text"])
        self.assertIn("Ed 是人名", transcript["text"])
        self.assertEqual(payload["glossary_corrections"], 1)

    def test_glossary_repairs_it_alias_only_in_local_grammar_context(self) -> None:
        project = self.root / "mixed-language-it-context"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "It; to V",
        )
        whisper_json = self.root / "mixed-language-it-context.json"
        words = [
            {"word": "Ed", "start": 0.02, "end": 0.09, "probability": 0.96},
            {"word": " 是虛主詞，", "start": 0.09, "end": 0.25, "probability": 0.95},
            {"word": "Ed", "start": 0.25, "end": 0.33, "probability": 0.96},
            {"word": " 是人名。", "start": 0.33, "end": 0.50, "probability": 0.95},
        ]
        whisper_json.write_text(
            json.dumps(
                {
                    "text": "Ed 是虛主詞，Ed 是人名。",
                    "language": "zh",
                    "segments": [
                        {
                            "start": 0.02,
                            "end": 0.50,
                            "text": "Ed 是虛主詞，Ed 是人名。",
                            "words": words,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "import-whisper",
            "--manifest",
            str(project / "project.json"),
            "--whisper-json",
            str(whisper_json),
            "--model",
            "base",
        )
        payload = json.loads(result.stdout)
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )

        self.assertIn("It 是虛主詞", transcript["text"])
        self.assertIn("Ed 是人名", transcript["text"])
        self.assertEqual(payload["glossary_corrections"], 1)

    def test_long_whisper_segment_becomes_readable_gui_captions(self) -> None:
        project = self.root / "mixed-caption-chunks"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--source-language",
            "zh-en",
            "--transcription-glossary",
            "It; to V; play with puppies",
        )
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = {
            "schema_version": 1,
            "project_id": manifest["project_id"],
            "canvas": {"platform_id": "instagram-reels", "width": 1080, "height": 1920},
            "director_style": "teacher-punch",
            "editing_brief": "",
            "caption_defaults": {"font_size": 58, "x": 50, "y": 76, "max_width": 86},
            "overlays": [],
            "publishing": {},
            "review": {"selected_overlay_id": None},
        }
        (project / "working/editor_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        fake_whisper = self.root / "fake-whisper-long-mixed"
        fake_whisper.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
tokens = ['It', ' 作', ' 虛', ' 主詞，', ' 看到', ' It，', ' 想到', ' to', ' V，', ' 後面', ' 真正', ' 主詞', ' 是', ' to', ' play', ' with', ' puppies。']
words = []
for index, token in enumerate(tokens):
    words.append({'word': token, 'start': index * 0.5, 'end': (index + 1) * 0.5, 'probability': 0.95})
text = 'It 作虛主詞，看到 It，想到 to V，後面真正主詞是 to play with puppies。'
payload = {'text': text, 'language': 'zh', 'segments': [{'start': 0.0, 'end': 8.5, 'text': text, 'words': words}]}
(output / f'{source.stem}.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
(output / f'{source.stem}.srt').write_text(f'1\\n00:00:00,000 --> 00:00:08,500\\n{text}\\n', encoding='utf-8')
""",
            encoding="utf-8",
        )
        fake_whisper.chmod(0o755)

        result = self.run_cli(
            "transcribe-local",
            "--manifest",
            str(manifest_path),
            "--model",
            "base",
            "--timeout",
            "30",
            extra_env={"WHISPER_BIN": str(fake_whisper)},
        )
        payload = json.loads(result.stdout)
        state = json.loads((project / "working/editor_state.json").read_text(encoding="utf-8"))
        captions = [item for item in state["overlays"] if item["type"] == "caption"]

        self.assertGreater(len(captions), 1)
        self.assertTrue(all(item["end"] - item["start"] <= 5.5 for item in captions))
        self.assertEqual(payload["synced_editor_captions"], len(captions))
        expected = "It 作虛主詞，看到 It，想到 to V，後面真正主詞是 to play with puppies。"
        self.assertEqual(
            "".join(item["text"].replace(" ", "") for item in captions),
            expected.replace(" ", ""),
        )

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

    def test_cli_approval_requires_current_revision_and_gate_order(self) -> None:
        manifest_path, _manifest = self.init_project("approval-cas", "zh-TW", "female")
        status = json.loads(
            self.run_cli("status", "--manifest", str(manifest_path)).stdout
        )
        self.run_cli(
            "approve",
            "--manifest",
            str(manifest_path),
            "--gate",
            "timeline",
            "--expected-revision",
            status["approval_revisions"]["timeline"],
            "--confirmed-by",
            "unit-test",
            expected=2,
        )
        self.run_cli(
            "approve",
            "--manifest",
            str(manifest_path),
            "--gate",
            "destructive_edit",
            "--expected-revision",
            "0" * 64,
            "--confirmed-by",
            "unit-test",
            expected=2,
        )
        approved = self.run_cli(
            "approve",
            "--manifest",
            str(manifest_path),
            "--gate",
            "destructive_edit",
            "--expected-revision",
            status["approval_revisions"]["destructive_edit"],
            "--confirmed-by",
            "unit-test",
        )
        self.assertTrue(json.loads(approved.stdout)["approval"]["approved"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["edit_review"], "complete")

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

    def test_duration_presets_cover_platform_profiles(self) -> None:
        result = self.run_cli("duration-presets")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["profiles"], ["short", "medium", "long"])
        self.assertEqual(
            payload["presets"]["youtube-shorts"]["profiles"]["long"],
            {"min_seconds": 90, "target_seconds": 180, "max_seconds": 180},
        )
        self.assertEqual(
            payload["presets"]["youtube-landscape"]["profiles"]["medium"][
                "target_seconds"
            ],
            480,
        )

    def test_init_stores_platform_duration_profile(self) -> None:
        project = self.root / "youtube-shorts-long"
        result = self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--platform",
            "youtube-shorts",
            "--duration-profile",
            "long",
        )
        payload = json.loads(result.stdout)
        target = payload["output_target"]
        self.assertEqual(target["platform"], "youtube-shorts")
        self.assertEqual(target["duration_profile"], "long")
        self.assertEqual(target["target_seconds"], 180)
        self.assertFalse(target["publishing_in_scope"])
        self.run_cli("validate", "--manifest", str(project / "project.json"))

    def test_agent_can_resolve_auto_or_custom_target_after_transcription(self) -> None:
        project = self.root / "target-resolution"
        self.run_cli(
            "init",
            "--input",
            str(self.source),
            "--project-dir",
            str(project),
            "--platform",
            "tiktok",
            "--duration-profile",
            "auto",
        )
        manifest_path = project / "project.json"
        pending = json.loads(manifest_path.read_text(encoding="utf-8"))["output_target"]
        self.assertEqual(pending["selection"], "agent_after_transcript")
        self.assertIsNone(pending["target_seconds"])

        resolved = self.run_cli(
            "set-target",
            "--manifest",
            str(manifest_path),
            "--platform",
            "tiktok",
            "--duration-profile",
            "medium",
        )
        target = json.loads(resolved.stdout)["output_target"]
        self.assertEqual(target["selection"], "user_profile")
        self.assertEqual(
            (
                target["min_seconds"],
                target["target_seconds"],
                target["max_seconds"],
            ),
            (45, 60, 90),
        )

        custom = self.run_cli(
            "set-target",
            "--manifest",
            str(manifest_path),
            "--target-duration",
            "75",
        )
        custom_target = json.loads(custom.stdout)["output_target"]
        self.assertEqual(custom_target["duration_profile"], "custom")
        self.assertEqual(custom_target["platform"], "tiktok")
        self.assertEqual(custom_target["target_seconds"], 75.0)
        self.run_cli("validate", "--manifest", str(manifest_path))

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

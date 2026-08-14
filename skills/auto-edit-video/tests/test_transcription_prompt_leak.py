"""Whisper's own initial prompt must never come back as a caption.

Evidence: an 11 second restaurant clip with no speech was imported through
Studio.  Whisper hallucinated on the silence and returned four "segments"
that were the initial prompt fed to it in ``transcription_initial_prompt``
("繁體中文，台灣用語。中英逐字稿。英文請保留拼寫，不要中文音譯。") verbatim
or in fragments.  Every downstream stage trusted them: caption segments,
title cards and the English translation were all built out of the prompt.

The filter runs after transcription and before captions are chunked, reads
the prompt text from the same constants the transcriber sends, and records
what it removed the way the fragment chunker already does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import auto_edit  # noqa: E402
from studio_server import StudioServer  # noqa: E402

ZH_TW_PROMPT = "繁體中文，台灣用語。中英逐字稿。英文請保留拼寫，不要中文音譯。"


def whisper_data(texts: list[tuple[str, float, float]]) -> dict:
    segments = []
    for index, (text, start, end) in enumerate(texts, start=1):
        span = (end - start) / max(1, len(text))
        words = [
            {
                "word": character,
                "start": round(start + span * offset, 3),
                "end": round(start + span * (offset + 1), 3),
                "probability": 0.9,
            }
            for offset, character in enumerate(text)
        ]
        segments.append(
            {
                "id": index,
                "start": start,
                "end": end,
                "text": text,
                "words": words,
            }
        )
    return {
        "engine": "openai-whisper",
        "language": "zh",
        "text": "".join(item[0] for item in texts),
        "segments": segments,
    }


def caption_texts(transcript: dict) -> list[str]:
    return [str(item["text"]) for item in transcript["caption_segments"]]


class PromptConstantIsSingleSourceOfTruth(unittest.TestCase):
    def test_transcriber_sends_the_shared_constant(self) -> None:
        prompt = auto_edit.transcription_initial_prompt("zh-TW", [])
        self.assertEqual(prompt, auto_edit.TRANSCRIPTION_PROMPT_ZH_TW)
        self.assertIn(
            auto_edit.TRANSCRIPTION_PROMPT_ZH_TW,
            auto_edit.TRANSCRIPTION_PROMPT_TEXTS,
        )

    def test_filter_follows_the_constant_it_is_given(self) -> None:
        for prompt in auto_edit.TRANSCRIPTION_PROMPT_TEXTS:
            with self.subTest(prompt=prompt):
                self.assertTrue(auto_edit.is_transcription_prompt_leak(prompt))


class PromptLeakIsDropped(unittest.TestCase):
    def test_whole_prompt_echo_never_reaches_captions(self) -> None:
        data = whisper_data([(ZH_TW_PROMPT, 0.5, 4.0)])
        transcript, _ = auto_edit.whisper_payload(data, 11.0)
        self.assertEqual(caption_texts(transcript), [])
        self.assertEqual(transcript["prompt_leak_dropped"]["count"], 1)
        self.assertEqual(
            transcript["prompt_leak_dropped"]["items"][0]["reason"],
            "transcription_prompt_leak",
        )

    def test_prompt_fragments_are_dropped_too(self) -> None:
        data = whisper_data(
            [
                ("英文請保留拼寫，不要中文音譯。", 0.4, 3.0),
                ("繁體中文，台灣用語。", 3.2, 5.6),
                ("中英逐字稿。", 5.8, 7.4),
            ]
        )
        transcript, _ = auto_edit.whisper_payload(data, 11.0)
        self.assertEqual(caption_texts(transcript), [])
        self.assertEqual(transcript["prompt_leak_dropped"]["count"], 3)

    def test_nat_silent_restaurant_clip_yields_no_words(self) -> None:
        data = whisper_data(
            [
                (ZH_TW_PROMPT, 0.0, 2.8),
                ("中英逐字稿。英文請保留拼寫。", 2.8, 5.4),
                (ZH_TW_PROMPT, 5.4, 8.2),
                ("不要中文音譯。", 8.2, 10.9),
            ]
        )
        transcript, _ = auto_edit.whisper_payload(data, 11.0)
        self.assertEqual(transcript["caption_segments"], [])
        self.assertEqual(transcript["segments"], [])
        self.assertEqual(transcript["words"], [])
        self.assertEqual(transcript["text"], "")
        self.assertEqual(transcript["prompt_leak_dropped"]["count"], 4)


class RealSpeechSurvives(unittest.TestCase):
    def test_natural_sentence_sharing_a_few_words_is_kept(self) -> None:
        kept = [
            "今天要聊的是英文學習的三個方法，第一個最重要。",
            "我把品牌名稱的拼寫直接留原文，唸起來比較自然。",
            "台灣的觀眾其實很習慣中英夾雜的講法。",
            "這段影片的重點只有一句話，先做再說。",
        ]
        data = whisper_data(
            [(text, index * 3.0, index * 3.0 + 2.6) for index, text in enumerate(kept)]
        )
        transcript, _ = auto_edit.whisper_payload(data, 14.0)
        joined = "".join(caption_texts(transcript))
        for text in kept:
            for chunk in text.split("，"):
                self.assertIn(chunk.strip("。"), joined)
        self.assertNotIn("prompt_leak_dropped", transcript)

    def test_mixed_take_keeps_speech_and_drops_the_echo(self) -> None:
        data = whisper_data(
            [
                (ZH_TW_PROMPT, 0.0, 2.4),
                ("這家店的排隊人潮從中午就沒有斷過。", 2.6, 5.2),
                ("英文請保留拼寫，不要中文音譯。", 5.4, 7.6),
                ("我們等了四十分鐘才坐下來吃第一口。", 7.8, 10.6),
            ]
        )
        transcript, _ = auto_edit.whisper_payload(data, 11.0)
        joined = "".join(caption_texts(transcript))
        self.assertIn("排隊人潮", joined)
        self.assertIn("四十分鐘", joined)
        self.assertNotIn("逐字稿", joined)
        self.assertNotIn("音譯", joined)
        self.assertEqual(transcript["prompt_leak_dropped"]["count"], 2)
        self.assertEqual(len(transcript["segments"]), 2)
        self.assertTrue(all(word["text"] for word in transcript["words"]))

    def test_word_ids_stay_contiguous_after_dropping(self) -> None:
        data = whisper_data(
            [
                (ZH_TW_PROMPT, 0.0, 2.4),
                ("這家店的排隊人潮從中午就沒有斷過。", 2.6, 5.2),
            ]
        )
        transcript, compatibility = auto_edit.whisper_payload(data, 6.0)
        ids = [word["id"] for word in transcript["words"]]
        self.assertEqual(ids, [f"word-{index:05d}" for index in range(1, len(ids) + 1)])
        spoken = [item for item in compatibility if not item["isGap"]]
        self.assertEqual([item["id"] for item in spoken], ids)
        self.assertEqual(transcript["segments"][0]["id"], "segment-0001")
        self.assertTrue(
            all(word["segment_id"] == "segment-0001" for word in transcript["words"])
        )


FAKE_WHISPER_PROMPT_DOMINATED = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[sys.argv.index('--output_dir') + 1])
output.mkdir(parents=True, exist_ok=True)
has_prompt = '--initial_prompt' in sys.argv
Path(sys.argv[sys.argv.index('--output_dir') + 1]).parent.parent.joinpath(
    'fake-whisper-calls.log'
).open('a', encoding='utf-8').write(('prompt' if has_prompt else 'clean') + '\\n')
if has_prompt:
    text = '英文請保留拼寫，不要中文音譯。英文請保留拼寫，不要中文音譯。'
else:
    text = '我們等了四十分鐘才坐下來吃第一口。'
payload = {
    'text': text,
    'language': 'zh',
    'segments': [{
        'start': 0.02,
        'end': 0.36,
        'text': text,
        'words': [{'word': text, 'start': 0.02, 'end': 0.36, 'probability': 0.9}],
    }],
}
(output / f'{source.stem}.json').write_text(
    json.dumps(payload, ensure_ascii=False), encoding='utf-8'
)
(output / f'{source.stem}.srt').write_text(
    f'1\\n00:00:00,020 --> 00:00:00,360\\n{text}\\n', encoding='utf-8'
)
"""


class PromptDominationIsRetriedWithoutThePrompt(unittest.TestCase):
    """The prompt is a hint; when it takes over the decode it has to go.

    Forensics on nat's IMG_7766.MOV: audio extraction is intact (11.35s,
    RMS -21 dBFS, no silence), and running the same audio through the same
    whisper twice separates the hypotheses — with ``--initial_prompt`` both
    ``base`` and ``large-v3`` return only the prompt, without it both return
    the real conversation.  ``--condition_on_previous_text False`` changes
    nothing, because the echo starts in the first window.  So the transcriber
    retries once without the hint instead of keeping a transcript that is
    entirely its own instructions.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("ffmpeg is unavailable")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-prompt-retry-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.4",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(self.source),
            ],
            check=True,
            capture_output=True,
        )
        self.whisper = self.root / "fake-whisper"
        self.whisper.write_text(FAKE_WHISPER_PROMPT_DOMINATED, encoding="utf-8")
        self.whisper.chmod(0o755)

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WHISPER_BIN"] = str(self.whisper)
        result = subprocess.run(
            ["python3", str(SKILL_DIR / "scripts/auto_edit.py"), *args],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def test_prompt_dominated_transcript_is_retranscribed_without_the_prompt(self) -> None:
        project = self.root / "prompt-dominated"
        self.run_cli(
            "init",
            "--input", str(self.source),
            "--project-dir", str(project),
            "--source-language", "auto",
        )
        self.run_cli(
            "transcribe-local",
            "--manifest", str(project / "project.json"),
            "--model", "base",
            "--timeout", "60",
        )
        transcript = json.loads(
            (project / "working/transcript_words.json").read_text(encoding="utf-8")
        )
        joined = "".join(str(item["text"]) for item in transcript["caption_segments"])
        self.assertIn("四十分鐘", joined)
        self.assertNotIn("音譯", joined)
        calls = (project / "working/fake-whisper-calls.log").read_text(encoding="utf-8")
        self.assertEqual(calls.split(), ["prompt", "clean"])
        manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["transcription"]["prompt_leak_recovery"],
            "retranscribed_without_prompt",
        )

    def test_a_healthy_transcript_is_never_transcribed_twice(self) -> None:
        project = self.root / "healthy"
        healthy = self.root / "fake-whisper-healthy"
        healthy.write_text(
            FAKE_WHISPER_PROMPT_DOMINATED.replace(
                "'英文請保留拼寫，不要中文音譯。英文請保留拼寫，不要中文音譯。'",
                "'這家店的排隊人潮從中午就沒有斷過。'",
            ),
            encoding="utf-8",
        )
        healthy.chmod(0o755)
        self.whisper = healthy
        self.run_cli(
            "init",
            "--input", str(self.source),
            "--project-dir", str(project),
            "--source-language", "auto",
        )
        self.run_cli(
            "transcribe-local",
            "--manifest", str(project / "project.json"),
            "--model", "base",
            "--timeout", "60",
        )
        calls = (project / "working/fake-whisper-calls.log").read_text(encoding="utf-8")
        self.assertEqual(calls.split(), ["prompt"])
        manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertNotIn("prompt_leak_recovery", manifest["transcription"])


class StudioReportsSilence(unittest.TestCase):
    """A Studio import with nothing to transcribe has to say so out loud.

    On the CLI an empty transcript keeps the existing needs_transcript path.
    In Studio the operator only ever sees the pipeline status, so the silence
    has to arrive there as its own state instead of surfacing three steps
    later as "not enough speech to build highlights".
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-prompt-leak-")
        self.addCleanup(self._tmp.cleanup)
        self.projects_root = Path(self._tmp.name) / "projects"
        self.projects_root.mkdir(parents=True)
        self.server = StudioServer(("127.0.0.1", 0), self.projects_root)
        self.addCleanup(self.server.server_close)
        self.server.auto_process = True

    def run_pipeline(self, caption_segments: list[dict]) -> dict:
        project = self.projects_root / "silent-project"
        (project / "working").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(project_dir, arguments, timeout=None):
            calls.append(arguments)
            if arguments[0] == "transcribe-local":
                (project_dir / "working/transcript_words.json").write_text(
                    json.dumps(
                        {
                            "caption_segments": caption_segments,
                            "segments": caption_segments,
                            "words": [],
                            "prompt_leak_dropped": {"count": 4, "items": []},
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(self.server, "run_pipeline_command", side_effect=fake_run):
            self.server.start_local_pipeline(project, "high-energy", "")
            self.server.pipeline_threads[-1].join(timeout=5)
        status = json.loads(
            (project / "working/pipeline_status.json").read_text(encoding="utf-8")
        )
        status["_calls"] = [arguments[0] for arguments in calls]
        return status

    def test_empty_transcript_stops_the_run_with_a_spoken_reason(self) -> None:
        status = self.run_pipeline([])
        self.assertEqual(status["state"], "needs_attention")
        self.assertEqual(status["phase"], "transcribe")
        self.assertEqual(status["error_code"], "no_speech_detected")
        self.assertIn("無可用語音", status["message"])
        self.assertEqual(status["_calls"], ["transcribe-local"])

    def test_transcribed_speech_still_runs_the_whole_pipeline(self) -> None:
        status = self.run_pipeline(
            [{"id": "caption-0001", "text": "這家店的排隊人潮沒有斷過。", "start": 0.2, "end": 2.4}]
        )
        self.assertEqual(status["state"], "needs_review")
        self.assertEqual(
            status["_calls"],
            ["transcribe-local", "analyze-edits", "plan-overlays", "plan-highlights"],
        )


if __name__ == "__main__":
    unittest.main()

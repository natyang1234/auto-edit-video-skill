from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from traditional_chinese import (  # noqa: E402
    normalize_whisper_orthography,
    should_normalize_taiwan_traditional,
    to_taiwan_traditional,
)


class TaiwanTraditionalChineseTests(unittest.TestCase):
    def test_conversion_is_taiwan_localized_idempotent_and_preserves_english(self) -> None:
        source = "老师说这个软件在互联网很好，cigar 还是 cigar。"
        expected = "老師說這個軟體在網際網路很好，cigar 還是 cigar。"

        converted = to_taiwan_traditional(source)

        self.assertEqual(converted, expected)
        self.assertEqual(to_taiwan_traditional(converted), expected)

    def test_mixed_script_phrase_is_canonicalized_to_taiwan_usage(self) -> None:
        self.assertEqual(
            to_taiwan_traditional("老師和我們聯系，cigar 不變。"),
            "老師和我們聯絡，cigar 不變。",
        )

    def test_split_whisper_words_keep_phrase_context_and_english(self) -> None:
        whisper = {
            "text": "我們聯系，It is fine",
            "segments": [
                {
                    "text": "我們聯系，It is fine",
                    "words": [
                        {"word": "我們"},
                        {"word": "联"},
                        {"word": "系，"},
                        {"word": "It"},
                        {"word": " is"},
                        {"word": " fine"},
                    ],
                }
            ],
        }

        normalize_whisper_orthography(whisper)

        words = [word["word"] for word in whisper["segments"][0]["words"]]
        self.assertEqual("".join(words), "我們聯絡，It is fine")
        self.assertEqual(words[-3:], ["It", " is", " fine"])

    def test_split_whisper_words_align_taiwan_phrase_expansion(self) -> None:
        whisper = {
            "text": "互联网",
            "segments": [
                {
                    "text": "互联网",
                    "words": [
                        {"word": "互", "start": 1.0, "end": 1.1},
                        {"word": "联网", "start": 1.1, "end": 1.4},
                    ],
                }
            ],
        }

        normalize_whisper_orthography(whisper)

        words = whisper["segments"][0]["words"]
        self.assertEqual("".join(word["word"] for word in words), "網際網路")
        self.assertEqual(
            [(word["start"], word["end"]) for word in words],
            [(1.0, 1.1), (1.1, 1.4)],
        )

    def test_explicit_mainland_source_is_not_normalized(self) -> None:
        manifest = {
            "subtitles": {
                "source_language": "zh-CN",
                "target_language": "zh-TW",
            }
        }

        self.assertFalse(should_normalize_taiwan_traditional(manifest, "zh"))

    def test_auto_detected_chinese_defaults_to_taiwan_traditional(self) -> None:
        manifest = {"subtitles": {"source_language": "auto"}}

        self.assertTrue(should_normalize_taiwan_traditional(manifest, "zh"))
        self.assertFalse(should_normalize_taiwan_traditional(manifest, "en"))


if __name__ == "__main__":
    unittest.main()

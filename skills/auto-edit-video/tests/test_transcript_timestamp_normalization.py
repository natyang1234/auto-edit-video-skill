"""Collapsed ASR word timestamps are repaired at the transcript intake layer.

Evidence: a 75s single-speaker screen recording transcribed by Breeze ASR-25
returned 58 of 203 words with ``start == end`` (25 of them stacked on 4.62s,
10 on 17.34s).  The zero-length words produced a zero-length caption, and the
run died inside caption delivery with "caption end must exceed start" — an
error that blames caption segmentation for a defect that arrived from the
recogniser.  The recogniser's bytes stay untouched; the repair happens after
the raw source revision is captured and before captions are chunked.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import auto_edit  # noqa: E402
import caption_delivery  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures/collapsed_asr"
COLLAPSED_ASR = FIXTURES / "breeze_collapsed_original.json"
COLLAPSED_SOURCE = FIXTURES / "transcript_source.json"
COLLAPSED_DURATION_S = 75.033


def load_collapsed_asr() -> dict:
    return json.loads(COLLAPSED_ASR.read_text(encoding="utf-8"))


class CollapsedTimestampFixtureTests(unittest.TestCase):
    def test_fixture_really_carries_collapsed_recogniser_timestamps(self) -> None:
        data = load_collapsed_asr()
        words = [
            word
            for segment in data["segments"]
            for word in segment.get("words", [])
        ]
        collapsed = [word for word in words if word["end"] <= word["start"]]
        self.assertEqual(len(words), 203)
        self.assertEqual(len(collapsed), 58)


class CollapsedTimestampIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        for name in ("working", "working/transcript_sources"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        source_payload = json.loads(COLLAPSED_SOURCE.read_text(encoding="utf-8"))
        revision = str(source_payload["revision"])
        source_path = (
            self.project / "working/transcript_sources" / f"{revision}.json"
        )
        source_path.write_bytes(COLLAPSED_SOURCE.read_bytes())
        (self.project / "working/transcript_source_current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "revision": revision,
                    "path": f"working/transcript_sources/{revision}.json",
                    "artifact_sha256": hashlib.sha256(
                        source_path.read_bytes()
                    ).hexdigest(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_collapsed_words_are_spread_before_captions_are_built(self) -> None:
        transcript, _ = auto_edit.whisper_payload(
            load_collapsed_asr(), COLLAPSED_DURATION_S
        )
        self.assertEqual(len(transcript["words"]), 203)
        offenders = [
            word
            for word in transcript["words"]
            if float(word["end"]) <= float(word["start"])
        ]
        self.assertEqual(offenders, [])
        previous_end = 0.0
        for word in transcript["words"]:
            self.assertGreaterEqual(float(word["start"]), previous_end - 1e-9)
            previous_end = float(word["end"])
        self.assertLessEqual(previous_end, COLLAPSED_DURATION_S + 1e-9)

    def test_no_caption_segment_is_zero_length(self) -> None:
        transcript, _ = auto_edit.whisper_payload(
            load_collapsed_asr(), COLLAPSED_DURATION_S
        )
        zero_length = [
            caption
            for caption in transcript["caption_segments"]
            if float(caption["end"]) <= float(caption["start"])
        ]
        self.assertEqual(zero_length, [])

    def test_caption_delivery_accepts_the_repaired_transcript(self) -> None:
        transcript, _ = auto_edit.whisper_payload(
            load_collapsed_asr(), COLLAPSED_DURATION_S
        )
        segmentation, _sources = caption_delivery.build_segmentation(
            self.project, transcript
        )
        self.assertTrue(segmentation["spans"])

    def test_word_ids_and_text_survive_the_repair(self) -> None:
        data = load_collapsed_asr()
        transcript, _ = auto_edit.whisper_payload(data, COLLAPSED_DURATION_S)
        original_text = [
            str(word["word"]).strip()
            for segment in data["segments"]
            for word in segment.get("words", [])
            if str(word["word"]).strip()
        ]
        self.assertEqual([word["text"] for word in transcript["words"]], original_text)
        self.assertEqual(
            [word["id"] for word in transcript["words"]],
            [f"word-{index + 1:05d}" for index in range(len(original_text))],
        )


class HealthyTimestampsAreUntouchedTests(unittest.TestCase):
    def test_sound_transcript_is_returned_byte_identical(self) -> None:
        data = {
            "text": "第一句 第二句",
            "language": "zh",
            "engine": "openai-whisper",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.2,
                    "text": "第一句",
                    "words": [
                        {"word": "第一", "start": 0.0, "end": 0.6, "probability": 0.9},
                        {"word": "句", "start": 0.6, "end": 1.2, "probability": 0.9},
                    ],
                },
                {
                    "id": 1,
                    "start": 1.4,
                    "end": 2.4,
                    "text": "第二句",
                    "words": [
                        {"word": "第二", "start": 1.4, "end": 2.0, "probability": 0.9},
                        {"word": "句", "start": 2.0, "end": 2.4, "probability": 0.9},
                    ],
                },
            ],
        }
        transcript, _ = auto_edit.whisper_payload(data, 3.0)
        self.assertEqual(
            [(word["start"], word["end"]) for word in transcript["words"]],
            [(0.0, 0.6), (0.6, 1.2), (1.4, 2.0), (2.0, 2.4)],
        )
        self.assertEqual(
            [(segment["start"], segment["end"]) for segment in transcript["segments"]],
            [(0.0, 1.2), (1.4, 2.4)],
        )
        self.assertNotIn("timestamp_normalization", transcript)


class PartialCollapseTests(unittest.TestCase):
    def test_single_zero_length_word_is_clamped_inside_its_gap(self) -> None:
        data = {
            "text": "一 二 三",
            "language": "zh",
            "engine": "breeze-asr-25",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 3.0,
                    "text": "一二三",
                    "words": [
                        {"word": "一", "start": 0.0, "end": 1.0},
                        {"word": "二", "start": 1.0, "end": 1.0},
                        {"word": "三", "start": 2.0, "end": 3.0},
                    ],
                }
            ],
        }
        transcript, _ = auto_edit.whisper_payload(data, 3.0)
        second = transcript["words"][1]
        self.assertEqual(second["start"], 1.0)
        self.assertEqual(second["end"], 1.08)
        self.assertEqual(
            (transcript["words"][0]["start"], transcript["words"][0]["end"]),
            (0.0, 1.0),
        )
        self.assertEqual(
            (transcript["words"][2]["start"], transcript["words"][2]["end"]),
            (2.0, 3.0),
        )
        self.assertEqual(
            transcript["timestamp_normalization"],
            {"collapsed_words": 1, "clusters": 1, "adjusted_words": 1},
        )

    def test_cluster_is_spread_by_character_weight_without_crossing_neighbours(
        self,
    ) -> None:
        data = {
            "text": "開場 塌陷 收尾",
            "language": "zh",
            "engine": "breeze-asr-25",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 4.0,
                    "text": "開場塌陷收尾",
                    "words": [
                        {"word": "開場", "start": 0.0, "end": 1.0},
                        {"word": "塌", "start": 1.0, "end": 1.0},
                        {"word": "陷住", "start": 1.0, "end": 1.0},
                        {"word": "收尾", "start": 2.0, "end": 4.0},
                    ],
                }
            ],
        }
        transcript, _ = auto_edit.whisper_payload(data, 4.0)
        first, second = transcript["words"][1], transcript["words"][2]
        self.assertEqual((first["start"], first["end"]), (1.0, 1.333))
        self.assertEqual((second["start"], second["end"]), (1.333, 2.0))
        self.assertEqual(
            (transcript["words"][3]["start"], transcript["words"][3]["end"]),
            (2.0, 4.0),
        )

    def test_cluster_absorbs_a_neighbour_that_starts_on_the_collapse_point(
        self,
    ) -> None:
        data = {
            "text": "一 二 三 四",
            "language": "zh",
            "engine": "breeze-asr-25",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 4.0,
                    "text": "一二三四",
                    "words": [
                        {"word": "一", "start": 0.0, "end": 1.0},
                        {"word": "二", "start": 1.0, "end": 1.0},
                        {"word": "三", "start": 1.0, "end": 1.02},
                        {"word": "四", "start": 3.0, "end": 4.0},
                    ],
                }
            ],
        }
        transcript, _ = auto_edit.whisper_payload(data, 4.0)
        second, third = transcript["words"][1], transcript["words"][2]
        self.assertGreater(second["end"], second["start"])
        self.assertGreater(third["end"], third["start"])
        self.assertEqual(second["start"], 1.0)
        self.assertEqual(second["end"], third["start"])
        self.assertLessEqual(third["end"], 3.0)

    def test_degenerate_segment_bounds_follow_their_repaired_words(self) -> None:
        data = {
            "text": "一 二",
            "language": "zh",
            "engine": "breeze-asr-25",
            "segments": [
                {
                    "id": 0,
                    "start": 1.0,
                    "end": 1.0,
                    "text": "一二",
                    "words": [
                        {"word": "一", "start": 1.0, "end": 1.0},
                        {"word": "二", "start": 1.0, "end": 1.0},
                    ],
                },
                {
                    "id": 1,
                    "start": 2.0,
                    "end": 3.0,
                    "text": "三",
                    "words": [{"word": "三", "start": 2.0, "end": 3.0}],
                },
            ],
        }
        transcript, _ = auto_edit.whisper_payload(data, 3.0)
        segment = transcript["segments"][0]
        self.assertGreater(segment["end"], segment["start"])
        self.assertEqual(segment["start"], transcript["words"][0]["start"])
        self.assertEqual(segment["end"], transcript["words"][1]["end"])


class TotalCollapseTests(unittest.TestCase):
    def test_transcript_collapsed_onto_one_instant_names_the_upstream_defect(
        self,
    ) -> None:
        data = {
            "text": "一 二",
            "language": "zh",
            "engine": "breeze-asr-25",
            "segments": [
                {
                    "id": 0,
                    "start": 5.0,
                    "end": 5.0,
                    "text": "一二",
                    "words": [
                        {"word": "一", "start": 5.0, "end": 5.0},
                        {"word": "二", "start": 5.0, "end": 5.0},
                    ],
                }
            ],
        }
        with self.assertRaises(auto_edit.TranscriptTimestampError) as caught:
            auto_edit.whisper_payload(data, 5.0)
        self.assertEqual(caught.exception.code, "transcript_timestamps_collapsed")
        message = str(caught.exception)
        self.assertIn("transcript_timestamps_collapsed", message)
        self.assertIn("word timestamps", message)
        self.assertNotIn("caption", message)


if __name__ == "__main__":
    unittest.main()

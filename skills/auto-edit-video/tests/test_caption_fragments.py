"""A one-character caption is not a line: fragments merge into a neighbour.

The material is the real 日本旅遊 run (generalize batch C), where the word 「了」
was stamped 12.02–15.60 and landed alone on screen while the sentence it
belongs to, 「更老是腳痠」, had already been flushed by the 5.5s duration cap.
Nobody can read a bare particle, and the translator dutifully turned it into
"was".  So the chunker now refuses to emit such a caption: it merges the
fragment into whichever neighbour is closer in time, and only drops it when it
has no neighbour to join.
"""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402
from auto_edit import readable_caption_segments  # noqa: E402


# The transcript exactly as the recogniser left it on the batch C source.
OUT_C_WORDS = [
    ("word-00001", "哈", 1.02, 1.92, "segment-0001"),
    ("word-00002", "不能", 1.92, 2.38, "segment-0001"),
    ("word-00003", "停", 2.38, 2.94, "segment-0001"),
    ("word-00004", "好", 2.94, 4.30, "segment-0001"),
    ("word-00005", "我", 4.30, 4.48, "segment-0002"),
    ("word-00006", "看到", 4.48, 4.78, "segment-0002"),
    ("word-00007", "了", 4.78, 5.66, "segment-0002"),
    ("word-00008", "大家", 5.66, 6.04, "segment-0002"),
    ("word-00009", "停", 6.04, 6.30, "segment-0002"),
    ("word-00010", "哪裡", 6.30, 6.66, "segment-0002"),
    ("word-00011", "呢", 6.66, 9.26, "segment-0002"),
    ("word-00012", "更", 9.26, 10.84, "segment-0003"),
    ("word-00013", "老", 10.84, 11.14, "segment-0003"),
    ("word-00014", "是", 11.14, 11.34, "segment-0003"),
    ("word-00015", "腳", 11.34, 11.78, "segment-0003"),
    ("word-00016", "痠", 11.78, 12.02, "segment-0003"),
    ("word-00017", "了", 12.02, 15.60, "segment-0003"),
    ("word-00018", "就是", 15.60, 16.42, "segment-0004"),
    ("word-00019", "這裡", 16.42, 16.72, "segment-0004"),
    ("word-00020", "啦", 16.72, 19.10, "segment-0004"),
    ("word-00021", "腳", 20.07, 20.52, "segment-0005"),
    ("word-00022", "好", 20.52, 20.76, "segment-0005"),
    ("word-00023", "痛", 20.76, 20.96, "segment-0005"),
    ("word-00024", "喔", 20.96, 23.44, "segment-0005"),
]


def words(rows) -> list[dict]:
    return [
        {"id": row[0], "text": row[1], "start": row[2], "end": row[3], "segment_id": row[4]}
        for row in rows
    ]


def segments_of(rows) -> list[dict]:
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[4], []).append(row)
    return [
        {
            "id": key,
            "start": items[0][2],
            "end": items[-1][3],
            "text": "".join(item[1] for item in items),
            "words": words(items),
        }
        for key, items in grouped.items()
    ]


class FragmentCaptionTests(unittest.TestCase):
    def chunk(self, rows, dropped=None) -> list[dict]:
        return readable_caption_segments(segments_of(rows), words(rows), dropped=dropped)

    def texts(self, rows, dropped=None) -> list[str]:
        return [item["text"] for item in self.chunk(rows, dropped=dropped)]

    def test_the_real_batch_c_transcript_never_puts_了_on_its_own_line(self) -> None:
        captions = self.chunk(OUT_C_WORDS)
        self.assertNotIn("了", [item["text"] for item in captions])
        self.assertEqual(
            [item["text"] for item in captions],
            ["哈不能停好", "我看到了大家停哪裡呢", "更老是腳痠了", "就是這裡啦", "腳好痛喔"],
        )
        # The fragment joins the sentence it fell off, and the merged caption
        # owns its timing and its words.
        merged = captions[2]
        self.assertEqual(merged["start"], 9.26)
        self.assertEqual(merged["end"], 15.6)
        self.assertEqual(merged["word_ids"][-1], "word-00017")
        self.assertEqual(
            [item["id"] for item in captions],
            [f"caption-segment-{index:04d}" for index in range(1, 6)],
        )

    def test_captions_without_fragments_are_untouched(self) -> None:
        rows = [
            ("word-1", "週末別窩在家", 0.0, 1.0, "segment-0001"),
            ("word-2", "跟我走", 1.0, 2.0, "segment-0001"),
            ("word-3", "中央公園", 2.4, 4.0, "segment-0002"),
        ]
        self.assertEqual(self.texts(rows), ["週末別窩在家跟我走", "中央公園"])

    def test_a_fragment_merges_into_the_nearer_previous_caption(self) -> None:
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "了", 2.2, 3.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 4.4, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows), ["我們就先走到這裡了", "然後回頭再看一次"])

    def test_a_fragment_merges_into_the_nearer_next_caption(self) -> None:
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "那", 3.4, 4.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 4.2, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows), ["我們就先走到這裡", "那然後回頭再看一次"])

    def test_an_equally_spaced_fragment_joins_the_sentence_it_fell_off(self) -> None:
        # 「了」 in batch C sat zero seconds from both neighbours.  A tie goes
        # backwards: a dangling particle belongs to what was just said.
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "了", 2.5, 3.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 3.5, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows), ["我們就先走到這裡了", "然後回頭再看一次"])

    def test_an_isolated_fragment_is_dropped_and_reported(self) -> None:
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "嗯", 6.0, 7.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 12.0, 14.0, "segment-0003"),
        ]
        dropped: list[dict] = []
        self.assertEqual(self.texts(rows, dropped=dropped), ["我們就先走到這裡", "然後回頭再看一次"])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["text"], "嗯")
        self.assertEqual(dropped[0]["start"], 6.0)
        self.assertEqual(dropped[0]["end"], 7.0)
        self.assertEqual(dropped[0]["reason"], "isolated_fragment")

    def test_a_two_character_chinese_line_is_a_sentence_and_stays(self) -> None:
        # 「好喔」 is a complete answer, not a leftover: the Chinese fragment
        # threshold is one character, so two-character lines are left alone.
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "好喔", 2.2, 3.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 4.4, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows), ["我們就先走到這裡", "好喔", "然後回頭再看一次"])

    def test_punctuation_does_not_rescue_a_one_character_line(self) -> None:
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "了。", 2.2, 3.0, "segment-0002"),
            ("word-3", "然後回頭再看一次", 4.4, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows)[0], "我們就先走到這裡了。")

    def test_a_two_letter_latin_line_is_a_fragment_but_a_three_letter_word_is_not(self) -> None:
        rows = [
            ("word-1", "we walked all the way there", 0.0, 2.0, "segment-0001"),
            ("word-2", "ok", 2.2, 3.0, "segment-0002"),
            ("word-3", "then we turned around again", 4.4, 6.0, "segment-0003"),
        ]
        self.assertEqual(
            self.texts(rows),
            ["we walked all the way there ok", "then we turned around again"],
        )
        kept = list(rows)
        kept[1] = ("word-2", "yes", 2.2, 3.0, "segment-0002")
        self.assertEqual(
            self.texts(kept),
            ["we walked all the way there", "yes", "then we turned around again"],
        )

    def test_a_caption_shorter_than_a_blink_is_a_fragment_too(self) -> None:
        # 133ms of 「然後回去掃」 is legible on paper and invisible on screen.
        rows = [
            ("word-1", "我們就先走到這裡", 0.0, 2.0, "segment-0001"),
            ("word-2", "然後回去掃", 2.2, 2.333, "segment-0002"),
            ("word-3", "把東西都收一收", 4.4, 6.0, "segment-0003"),
        ]
        self.assertEqual(self.texts(rows), ["我們就先走到這裡然後回去掃", "把東西都收一收"])

    def test_the_only_caption_there_is_survives_even_as_a_fragment(self) -> None:
        rows = [("word-1", "了", 0.0, 1.0, "segment-0001")]
        dropped: list[dict] = []
        self.assertEqual(self.texts(rows, dropped=dropped), ["了"])
        self.assertEqual(dropped, [])


class FragmentIdentityLifecycleTests(unittest.TestCase):
    """Merging is a re-chunk, so it moves the segmentation revision and the ids."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / "working/transcript_sources").mkdir(parents=True)
        raw_words = [
            {"source_word_index": index, "start_us": start, "end_us": end, "text": text, "speaker": None}
            for index, (text, start, end) in enumerate(
                [("更老是腳痠", 9_260_000, 12_020_000), ("了", 12_020_000, 15_600_000)]
            )
        ]
        payload = {
            "schema_version": 1,
            "revision": "",
            "source_media_sha256": "a" * 64,
            "audio_stream_index": 0,
            "decoded_pcm": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_format": "s16le",
                "sha256": "b" * 64,
            },
            "engine": "openai-whisper",
            "engine_version": "1.0",
            "model": "base",
            "language": "zh",
            "decoding_params": {},
            "source_generation": 0,
            "raw_words": raw_words,
        }
        material = dict(payload)
        material.pop("revision")
        payload["revision"] = contract_registry.canonical_hash(material)
        path = self.project / f"working/transcript_sources/{payload['revision']}.json"
        caption_delivery._atomic_write(path, payload)
        caption_delivery._atomic_write(
            self.project / "working/transcript_source_current.json",
            {
                "schema_version": 1,
                "revision": payload["revision"],
                "path": f"working/transcript_sources/{payload['revision']}.json",
                "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        self.transcript = {
            "words": [
                {"id": "word-00001", "text": "更老是腳痠", "start": 9.26, "end": 12.02},
                {"id": "word-00002", "text": "了", "start": 12.02, "end": 15.6},
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "text": "更老是腳痠",
                    "start": 9.26,
                    "end": 12.02,
                    "word_ids": ["word-00001"],
                },
                {
                    "id": "caption-segment-0002",
                    "text": "了",
                    "start": 12.02,
                    "end": 15.6,
                    "word_ids": ["word-00002"],
                },
            ],
        }
        self.state = {
            "schema_version": 2,
            "project_id": "caption-fragment-test",
            "segments": [
                {
                    "id": "full",
                    "source_start": 0.0,
                    "source_end": 20.0,
                    "origin": "default_full_source",
                }
            ],
            "overlays": [],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_merging_a_fragment_retires_the_old_caption_ids(self) -> None:
        before = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        merged = copy.deepcopy(self.transcript)
        merged["caption_segments"] = [
            {
                "id": "caption-segment-0001",
                "text": "更老是腳痠了",
                "start": 9.26,
                "end": 15.6,
                "word_ids": ["word-00001", "word-00002"],
            }
        ]
        after = caption_delivery.expected_instances(self.project, merged, self.state)
        self.assertNotEqual(
            before["segmentation"]["segmentation_revision"],
            after["segmentation"]["segmentation_revision"],
        )
        old_ids = {item["caption_source_id"] for item in before["sources"]}
        new_ids = {item["caption_source_id"] for item in after["sources"]}
        self.assertEqual(len(new_ids), 1)
        self.assertFalse(new_ids & old_ids)
        self.assertEqual(len(after["instances"]), 1)
        self.assertEqual(after["instances"][0]["corrected_source"], "更老是腳痠了")


if __name__ == "__main__":
    unittest.main()

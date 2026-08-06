"""A second caption line: what can be checked is alignment, not wording."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_translator as translator  # noqa: E402

LINES = [
    {"n": 1, "text": "週末別窩在家跟我走", "id": "segment-0001"},
    {"n": 2, "text": "中央公園三號出口", "id": "segment-0002"},
    {"n": 3, "text": "上二樓就是 lounge", "id": "segment-0003"},
]


class AlignmentTests(unittest.TestCase):
    def test_a_full_reply_aligns_by_number(self) -> None:
        aligned, notes = translator.align(LINES, [
            {"n": 1, "text": "don't stay in tonight, come with me"},
            {"n": 2, "text": "Central Park exit 3"},
            {"n": 3, "text": "second floor is the lounge"},
        ])
        self.assertEqual(sorted(aligned), [1, 2, 3])
        self.assertEqual(notes, [])

    def test_the_reply_order_does_not_matter_because_numbers_do(self) -> None:
        aligned, _ = translator.align(LINES, [
            {"n": 3, "text": "second floor is the lounge"},
            {"n": 1, "text": "come with me"},
            {"n": 2, "text": "exit 3"},
        ])
        self.assertEqual(aligned[1], "come with me")
        self.assertEqual(aligned[3], "second floor is the lounge")

    def test_a_missing_line_is_left_missing_and_reported(self) -> None:
        # Sliding the next line up would caption one moment with another
        # moment's words, which is worse than no second line.
        aligned, notes = translator.align(LINES, [
            {"n": 1, "text": "come with me"},
            {"n": 3, "text": "second floor is the lounge"},
        ])
        self.assertNotIn(2, aligned)
        self.assertEqual(aligned[3], "second floor is the lounge")
        self.assertTrue(any("line 2" in note for note in notes))

    def test_a_line_echoed_back_unchanged_is_not_a_translation(self) -> None:
        aligned, notes = translator.align(LINES, [
            {"n": 1, "text": "週末別窩在家跟我走"},
            {"n": 2, "text": "exit 3"},
        ])
        self.assertNotIn(1, aligned)
        self.assertTrue(any("unchanged" in note for note in notes))

    def test_punctuation_only_differences_still_count_as_unchanged(self) -> None:
        aligned, _ = translator.align(LINES, [{"n": 1, "text": "週末別窩在家，跟我走！"}])
        self.assertNotIn(1, aligned)

    def test_a_blank_translation_is_dropped(self) -> None:
        aligned, notes = translator.align(LINES, [{"n": 1, "text": "   "}])
        self.assertEqual(aligned, {})
        self.assertTrue(any("no translation" in note for note in notes))

    def test_translations_for_lines_that_do_not_exist_are_reported(self) -> None:
        _, notes = translator.align(LINES, [
            {"n": 1, "text": "come with me"},
            {"n": 99, "text": "a line nobody asked for"},
        ])
        self.assertTrue(any("do not exist" in note for note in notes))

    def test_a_reply_that_is_not_a_list_of_numbered_items_yields_nothing(self) -> None:
        aligned, _ = translator.align(LINES, [{"text": "no number here"}])
        self.assertEqual(aligned, {})


class CaptionLineTests(unittest.TestCase):
    def test_the_reading_split_is_preferred_over_recogniser_segments(self) -> None:
        # Same preference every other caption path makes; disagreeing would
        # translate lines the viewer never sees.
        lines = translator.caption_lines({
            "caption_segments": [{"text": "讀起來的那一版"}],
            "segments": [{"text": "辨識器原始的那一版"}],
        })
        self.assertEqual([line["text"] for line in lines], ["讀起來的那一版"])

    def test_blank_segments_are_skipped_without_shifting_numbers(self) -> None:
        lines = translator.caption_lines({
            "caption_segments": [{"text": "甲"}, {"text": "  "}, {"text": "丙"}]
        })
        self.assertEqual([(line["n"], line["text"]) for line in lines], [(1, "甲"), (3, "丙")])


if __name__ == "__main__":
    unittest.main()

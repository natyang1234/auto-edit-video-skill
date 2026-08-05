"""Where a caption breaks, and when it shrinks instead of breaking."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as cc  # noqa: E402


def monospace(width_per_char: float = 10.0):
    """Every character the same width, so widths are predictable in a test."""
    return lambda text: len(text) * width_per_char


class BreakPointTests(unittest.TestCase):
    def test_a_latin_word_is_never_split(self) -> None:
        text = "叫做 cigar 它是"
        inside = text.index("cigar") + 2
        self.assertFalse(cc._breakable(text, inside))

    def test_a_number_is_never_split(self) -> None:
        text = "大概 3000 分鐘"
        self.assertFalse(cc._breakable(text, text.index("3000") + 2))

    def test_a_space_is_always_a_break(self) -> None:
        text = "SO GO 旁邊"
        self.assertTrue(cc._breakable(text, text.index("GO")))

    def test_chinese_may_break_between_characters(self) -> None:
        self.assertTrue(cc._breakable("今晚別宅在家", 3))

    def test_closing_punctuation_never_starts_a_line(self) -> None:
        text = "跟我走，然後呢"
        self.assertFalse(cc._breakable(text, text.index("，")))

    def test_opening_punctuation_never_ends_a_line(self) -> None:
        text = "他說「今晚別宅在家」"
        self.assertFalse(cc._breakable(text, text.index("「") + 1))


class WrapTests(unittest.TestCase):
    def test_text_that_fits_stays_on_one_line(self) -> None:
        self.assertEqual(cc.wrap_lines("今晚別宅在家", monospace(), 100.0), ["今晚別宅在家"])

    def test_lines_are_balanced_rather_than_greedily_filled(self) -> None:
        # Greedy filling packs the first line and strands the remainder;
        # that is where the single dangling character comes from.
        lines = cc.wrap_lines("一二三四五六七八九十甲乙", monospace(), 100.0)
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(abs(len(lines[0]) - len(lines[1])), 1)

    def test_no_line_is_left_with_a_couple_of_characters(self) -> None:
        for text in ("一二三四五六七八九十甲", "今晚別宅在家跟我走好嗎"):
            for line in cc.wrap_lines(text, monospace(), 100.0) or []:
                self.assertGreaterEqual(len(line), cc.MIN_TAIL_CHARS, text)

    def test_a_break_never_lands_inside_an_english_word(self) -> None:
        lines = cc.wrap_lines(
            "旁邊巷子到 downtown 右手邊小門就是", monospace(), 100.0
        ) or []
        joined = "|".join(lines)
        self.assertIn("downtown", joined.replace("|", "")[: len(joined)])
        for line in lines:
            self.assertFalse(line.endswith("down"))
            self.assertFalse(line.startswith("town"))

    def test_text_with_nowhere_to_break_gives_up_rather_than_forcing_one(self) -> None:
        # Better to hand it to the framesetter than to invent a worse break.
        self.assertIsNone(cc.wrap_lines("abcdefghijklmnop", monospace(), 50.0))

    def test_a_very_long_line_takes_as_many_lines_as_it_needs(self) -> None:
        lines = cc.wrap_lines("一二三四五六七八九十" * 4, monospace(), 100.0)
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            self.assertLessEqual(len(line) * 10.0, 100.0)


class AutofitTests(unittest.TestCase):
    @staticmethod
    def measure_at(size: float):
        return lambda text: len(text) * size

    def test_a_line_that_fits_keeps_its_size(self) -> None:
        lines, size = cc.fit_caption_text("今晚別宅", self.measure_at, 10.0, 100.0)
        self.assertEqual(lines, ["今晚別宅"])
        self.assertEqual(size, 10.0)

    def test_a_near_miss_shrinks_instead_of_wrapping(self) -> None:
        # Dropping a point to stay on one line reads better than keeping the
        # size and wrapping with two characters left over.
        lines, size = cc.fit_caption_text("一二三四五六七八九十甲", self.measure_at, 10.0, 100.0)
        self.assertEqual(len(lines), 1)
        self.assertLess(size, 10.0)

    def test_shrinking_stops_before_the_text_becomes_unreadable(self) -> None:
        lines, size = cc.fit_caption_text("一二三四五六七八九十" * 3, self.measure_at, 10.0, 100.0)
        self.assertGreaterEqual(size, 10.0 * cc.MIN_AUTOFIT_SCALE)
        self.assertGreater(len(lines), 1)

    def test_an_explicit_newline_is_honoured(self) -> None:
        lines, _ = cc.fit_caption_text("今晚別宅\n在家跟我", self.measure_at, 10.0, 100.0)
        self.assertEqual(lines, ["今晚別宅", "在家跟我"])

class CaptionSegmentTests(unittest.TestCase):
    """Which words share a caption line."""

    @staticmethod
    def split(segments, words):
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from auto_edit import readable_caption_segments

        return readable_caption_segments(segments, words)

    @staticmethod
    def words(*items) -> list[dict]:
        return [
            {"id": f"word-{index:04d}", "text": text, "start": start, "end": end,
             "segment_id": segment}
            for index, (text, start, end, segment) in enumerate(items, start=1)
        ]

    def test_a_line_ends_where_the_recogniser_ended_its_segment(self) -> None:
        segments = [{"start": 0.0, "end": 2.0, "text": "今晚別宅在家"},
                    {"start": 2.0, "end": 4.0, "text": "跟我走"}]
        words = self.words(
            ("今晚", 0.0, 1.0, "segment-0001"),
            ("別宅在家", 1.0, 2.0, "segment-0001"),
            ("跟我走", 2.0, 4.0, "segment-0002"),
        )
        self.assertEqual(
            [item["text"] for item in self.split(segments, words)],
            ["今晚別宅在家", "跟我走"],
        )

    def test_a_word_ending_on_another_segments_timestamp_does_not_end_a_line(self) -> None:
        # Deciding boundaries by comparing timestamps means any word that
        # happens to end on the same rounded second as a segment elsewhere
        # in the video ends a line. On a real lesson that fired hundreds of
        # times and left captions one character long.
        segments = [{"start": 0.0, "end": 4.0, "text": "今晚別宅在家跟我走"},
                    {"start": 4.0, "end": 8.0, "text": "忠孝復興四號出口"}]
        words = self.words(
            ("今晚", 0.0, 1.0, "segment-0001"),
            # This one ends exactly when segment one ends, but belongs to it.
            ("別宅在家", 1.0, 4.0, "segment-0001"),
            ("跟我走", 4.0, 4.4, "segment-0001"),
            ("忠孝復興", 4.4, 8.0, "segment-0002"),
        )
        lines = [item["text"] for item in self.split(segments, words)]
        self.assertEqual(lines, ["今晚別宅在家跟我走", "忠孝復興"])

    def test_words_without_a_segment_id_still_split_on_gaps(self) -> None:
        # Older transcripts predate the field; length and silence still apply.
        words = [
            {"id": "word-1", "text": "今晚別宅在家", "start": 0.0, "end": 1.0},
            {"id": "word-2", "text": "跟我走", "start": 3.0, "end": 4.0},
        ]
        lines = [item["text"] for item in self.split([], words)]
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()

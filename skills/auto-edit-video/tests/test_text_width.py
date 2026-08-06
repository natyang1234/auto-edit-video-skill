"""Text capped by how much room it takes, not by how many characters it has.

A Chinese character is drawn about twice as wide as a Latin letter, so a
limit written as a character count means two different things depending on
what is in the string. That has now cost work twice: an English word thrown
away by an eight-character rule ("cigarette"), and a Chinese title passed by
a thirty-six-character rule, which the compositor then shrank onto one
unreadable line across the whole frame. Seen on a real cut on 2026-08-06.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import editorial_planner  # noqa: E402
import highlight_planner  # noqa: E402
import text_joining  # noqa: E402

# The title from the cut that exposed this: thirty-six characters of
# unpunctuated transcript, seventy-two wide.
RUN_ON = "例如說我有小孩我帶著寶寶的時候我不希望他們被煙汙染到所以呢怎麼樣為了遠離"


class WidthTests(unittest.TestCase):
    def test_a_chinese_character_counts_double(self) -> None:
        self.assertEqual(text_joining.display_width("無煙家庭"), 8.0)

    def test_a_latin_letter_counts_once(self) -> None:
        self.assertEqual(text_joining.display_width("cigarette"), 9.0)

    def test_a_mixed_line_adds_both(self) -> None:
        self.assertEqual(text_joining.display_width("抽 cigar"), 2 + 1 + 5)

    def test_full_width_punctuation_counts_double(self) -> None:
        self.assertEqual(text_joining.display_width("好，"), 4.0)


class TrimTests(unittest.TestCase):
    def test_text_that_fits_is_returned_whole(self) -> None:
        self.assertEqual(text_joining.trim_to_width("無煙家庭", 20), "無煙家庭")

    def test_trimming_never_lands_half_way_into_a_character(self) -> None:
        # An odd budget cannot take half of a double-width character.
        trimmed = text_joining.trim_to_width("無煙家庭的健康承諾", 7)
        self.assertEqual(trimmed, "無煙家")
        self.assertLessEqual(text_joining.display_width(trimmed), 7)

    def test_a_bracket_left_dangling_by_the_cut_is_dropped(self) -> None:
        # A title ending on a quotation mark the cut opened reads as though
        # the render failed half way through.
        self.assertEqual(text_joining.trim_to_width("我們說「無煙家庭」", 8), "我們說")

    def test_a_bracket_that_was_already_there_is_left_alone(self) -> None:
        # Nothing was truncated, so nothing is second-guessed: trimming is
        # this function's job, editing the text is not.
        self.assertEqual(text_joining.trim_to_width("我們說「", 8), "我們說「")


class TitleLengthTests(unittest.TestCase):
    def test_a_chinese_run_on_is_cut_to_something_a_card_can_hold(self) -> None:
        title = highlight_planner.transcript_title_excerpt(RUN_ON)
        self.assertLessEqual(
            text_joining.display_width(title), highlight_planner.MAX_TITLE_WIDTH
        )
        self.assertEqual(len(title), 24, "twenty-four characters, forty-eight wide")

    def test_an_english_title_keeps_its_old_room(self) -> None:
        # Narrowing for Chinese must not shorten English, which was never
        # the problem: forty-eight letters still fit.
        latin = "the quick brown fox jumps over the lazy dog and keeps on"
        title = highlight_planner.transcript_title_excerpt(latin)
        self.assertGreaterEqual(len(title), 36)

    def test_a_cut_inside_an_english_word_backs_up_to_a_boundary(self) -> None:
        title = highlight_planner.transcript_title_excerpt(
            "supercalifragilistic expialidocious antidisestablishmentarianism"
        )
        self.assertFalse(title.endswith("antidis"), title)
        self.assertTrue(title.endswith("expialidocious"), title)

    def test_a_short_title_is_left_exactly_alone(self) -> None:
        self.assertEqual(
            highlight_planner.transcript_title_excerpt("無煙家庭的健康承諾"),
            "無煙家庭的健康承諾",
        )


class OneRuleForWhatCountsAsChineseTests(unittest.TestCase):
    """Both limits ask "is this Chinese"; only one place answers.

    The keyword rule and the title rule stay deliberately different — a term
    is not a clause, and neither question is "how much room does it take".
    What they must not disagree about is which characters are wide, and that
    predicate had already been written twice.
    """

    CASES = ["無煙家庭", "cigarette", "抽 cigar", "ＡＢＣ", "カタカナ", "123"]

    def test_the_keyword_rule_uses_the_shared_predicate(self) -> None:
        for term in self.CASES:
            with self.subTest(term):
                by_shared = text_joining.has_wide(term)
                # Eight is the CJK ceiling, twenty the Latin one; a nine
                # character string is accepted only if it is not wide.
                nine = (term * 9)[:9]
                self.assertEqual(
                    editorial_planner.keyword_length_ok(nine),
                    not text_joining.has_wide(nine),
                    f"{nine!r} wide={by_shared}",
                )

    def test_cigarette_survives_and_a_chinese_clause_does_not(self) -> None:
        # The two cases that motivated splitting the rule in the first place.
        self.assertTrue(editorial_planner.keyword_length_ok("cigarette"))
        self.assertFalse(editorial_planner.keyword_length_ok("我帶著寶寶的時候我不希望"))


if __name__ == "__main__":
    unittest.main()

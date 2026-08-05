"""One decision, one answer — the failure this codebase keeps repeating.

Six times in a day, the same decision was implemented in two places and the
two drifted: how a transcript splits into lines, what a card is called, how
emphasis is applied, which fields cross into editor state, how long a card
must last, where a caption line ends. Each was found by watching a rendered
video, never by a test.

These tests ask the paths the same question and require the same answer.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import text_joining  # noqa: E402
import visual_director  # noqa: E402
import visual_quality  # noqa: E402
from auto_edit import join_caption_words  # noqa: E402
from editor_server import active_editorial_title, highlight_card_title  # noqa: E402
from highlight_planner import join_transcript_parts  # noqa: E402

TRANSCRIPT = {"caption_segments": [{"start": 0.0, "end": 3.0, "text": "逐字稿原話開頭是這樣"}]}
EVIDENCE = [{
    "id": "evidence-aaaa1111", "kind": "quote",
    "literal": "逐字稿原話開頭是這樣", "start": 0.2, "end": 3.0,
}]


def highlight(**overrides) -> dict:
    base = {
        "id": "highlight-abc123def456", "start": 0.0, "end": 10.0,
        "title": "逐字稿抽出來的標題", "review_status": "pending", "score": 0.5,
    }
    base.update(overrides)
    return base


class CardTitleTests(unittest.TestCase):
    """Both card paths name a cut the same thing."""

    @staticmethod
    def director_title(item: dict) -> str:
        state = {"active_highlight_id": item["id"], "highlights": [item]}
        planned = visual_director.plan_visuals(
            [{"id": item["id"], "source_start": item["start"], "source_end": item["end"]}],
            EVIDENCE,
            editorial_title=active_editorial_title(state),
        )
        layers = planned["structured_layers"]["items"]
        return layers[0]["payload"]["title"] if layers else ""

    @staticmethod
    def design_title(item: dict) -> str:
        overlays = visual_quality.build_highlight_design_overlays(
            TRANSCRIPT, item, {"font_size": 58}, "teacher-punch"
        )
        hooks = [o for o in overlays if o.get("design_role") == "hook"]
        return hooks[0]["text"] if hooks else ""

    def test_both_paths_agree_without_editorial_copy(self) -> None:
        # They did not: one fell back to a quote from the transcript, the
        # other to the highlight's own title, at different truncations.
        item = highlight()
        self.assertEqual(self.director_title(item), self.design_title(item))

    def test_both_paths_agree_with_editorial_copy(self) -> None:
        item = highlight(editorial={"title": "模型下的標題", "is_editorial_copy": True})
        self.assertEqual(self.director_title(item), self.design_title(item))
        self.assertEqual(self.design_title(item), "模型下的標題")

    def test_editorial_copy_that_is_blank_does_not_blank_the_card(self) -> None:
        item = highlight(editorial={"title": "   ", "is_editorial_copy": True})
        self.assertEqual(highlight_card_title(item), "逐字稿抽出來的標題")

    def test_copy_not_marked_editorial_is_not_used(self) -> None:
        # Unmarked wording could be a verbatim quote; the contract requires
        # condensed copy to say so.
        item = highlight(editorial={"title": "沒有標記的字"})
        self.assertEqual(highlight_card_title(item), "逐字稿抽出來的標題")


class TokenJoiningTests(unittest.TestCase):
    """Every path that turns transcript tokens into text spaces them alike."""

    CASES = [
        ["今晚", "去", "downtown", "，", "好嗎"],
        ["it", "is", "fun"],
        ["雪茄", "叫做", "cigar"],
        ["（", "註", "）", "所以"],
        ["30", "分鐘", "。"],
    ]

    def test_caption_and_highlight_text_are_spaced_identically(self) -> None:
        # The calibration path searches for text these two produce; a
        # punctuation set updated in one place and not the others makes that
        # search quietly find nothing.
        for tokens in self.CASES:
            words = [{"text": token} for token in tokens]
            self.assertEqual(
                join_caption_words(words), join_transcript_parts(tokens), tokens
            )

    def test_both_go_through_the_shared_rule(self) -> None:
        for tokens in self.CASES:
            words = [{"text": token} for token in tokens]
            self.assertEqual(
                join_caption_words(words), text_joining.join_tokens(tokens), tokens
            )

    def test_closing_punctuation_never_takes_a_space(self) -> None:
        self.assertFalse(text_joining.needs_space("n", "，"))
        self.assertFalse(text_joining.needs_space("n", "."))

    def test_opening_punctuation_never_gives_one(self) -> None:
        self.assertFalse(text_joining.needs_space("（", "註"))

    def test_latin_beside_chinese_takes_a_space(self) -> None:
        self.assertTrue(text_joining.needs_space("去", "d"))
        self.assertTrue(text_joining.needs_space("n", "好"))

    def test_chinese_beside_chinese_does_not(self) -> None:
        self.assertFalse(text_joining.needs_space("今", "晚"))


if __name__ == "__main__":
    unittest.main()

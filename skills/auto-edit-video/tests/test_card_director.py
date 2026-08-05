"""A model proposes cards; the transcript decides which survive."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import card_director as director  # noqa: E402
import card_plan  # noqa: E402

SPOKEN = [
    {"start": 0.0, "end": 5.0, "text": "我就把自己錄起來大概三十分鐘"},
    {"start": 5.0, "end": 10.0, "text": "然後帶兒子從游泳池回來"},
    {"start": 10.0, "end": 16.0, "text": "透過 AI 批量發布安排"},
    {"start": 16.0, "end": 24.0, "text": "用力盡力享受生活"},
]


def proposal(**overrides) -> dict:
    base = {
        "at": 1.0,
        "seconds": 3,
        "kind": "note",
        "payload": {"icon": "🎙", "title": "採訪自己"},
        "quote": "我就把自己錄起來",
        "reason": "把抽象動作變成看得見的東西",
    }
    base.update(overrides)
    return base


class GroundingTests(unittest.TestCase):
    def ground(self, *items: dict, budget: int = 8):
        return director.ground_cards(
            list(items), SPOKEN, duration_s=24.0, budget=budget
        )

    def test_a_supported_card_survives(self) -> None:
        cards, _ = self.ground(proposal())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["kind"], "note")
        self.assertEqual(cards[0]["origin"], "model")
        self.assertTrue(cards[0]["editorial"])

    def test_a_card_placed_at_a_moment_that_was_invented_is_dropped(self) -> None:
        # The wording on a card may be condensed, but the moment it points
        # at has to be real.
        cards, notes = self.ground(proposal(quote="老師說今天不用上課"))
        self.assertEqual(cards, [])
        self.assertTrue(any("nothing like that is said" in note for note in notes))

    def test_a_real_line_from_elsewhere_does_not_justify_this_moment(self) -> None:
        cards, notes = self.ground(proposal(at=18.0, quote="我就把自己錄起來"))
        self.assertEqual(cards, [])
        self.assertTrue(any("nothing like that is said" in note for note in notes))

    def test_a_kind_that_cannot_be_drawn_is_dropped(self) -> None:
        cards, notes = self.ground(proposal(kind="hologram"))
        self.assertEqual(cards, [])
        self.assertTrue(any("not a card this can draw" in note for note in notes))

    def test_a_card_with_no_payload_is_dropped(self) -> None:
        cards, notes = self.ground(proposal(payload={}))
        self.assertEqual(cards, [])
        self.assertTrue(any("no payload" in note for note in notes))

    def test_a_card_past_the_end_of_the_video_is_dropped(self) -> None:
        cards, notes = self.ground(proposal(at=900.0))
        self.assertEqual(cards, [])
        self.assertTrue(any("outside the video" in note for note in notes))

    def test_cards_too_close_together_are_thinned(self) -> None:
        # Cards compete with the speaker; one after another is a slideshow
        # with a person behind it.
        cards, notes = self.ground(
            proposal(at=1.0),
            proposal(at=2.0, payload={"text": "太近"}, kind="chip"),
            proposal(at=6.0, kind="chip", payload={"text": "🏊 → 🚗"},
                     quote="帶兒子從游泳池回來"),
        )
        self.assertEqual([card["start"] for card in cards], [1.0, 6.0])
        self.assertTrue(any("within" in note for note in notes))

    def test_the_budget_is_a_ceiling_and_the_cut_is_reported(self) -> None:
        cards, notes = self.ground(
            proposal(at=1.0),
            proposal(at=6.0, quote="帶兒子從游泳池回來"),
            proposal(at=11.0, quote="透過 AI 批量發布"),
            budget=2,
        )
        self.assertEqual(len(cards), 2)
        self.assertTrue(any("to leave the speaker room" in note for note in notes))

    def test_time_on_screen_is_clamped_to_something_readable(self) -> None:
        cards, _ = self.ground(proposal(seconds=0.1))
        self.assertGreaterEqual(
            cards[0]["end"] - cards[0]["start"], card_plan.MIN_CARD_SECONDS
        )

    def test_every_drop_is_reported(self) -> None:
        _, notes = self.ground(
            proposal(at=900.0), proposal(kind="hologram"), proposal(payload={})
        )
        self.assertEqual(len(notes), 3)


class BudgetTests(unittest.TestCase):
    def test_a_short_clip_still_gets_one_card(self) -> None:
        self.assertEqual(director.card_budget(5.0), 1)

    def test_the_budget_grows_with_length_but_stops(self) -> None:
        self.assertLess(director.card_budget(60.0), director.card_budget(300.0))
        self.assertLessEqual(director.card_budget(6000.0), director.MAX_CARDS)


if __name__ == "__main__":
    unittest.main()

"""Editorial highlight selection: the model judges, the transcript decides."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import editorial_planner as ep  # noqa: E402


def clauses(*spans: tuple[float, float, str]) -> list[dict]:
    return [{"start": start, "end": end, "text": text} for start, end, text in spans]


LESSON = clauses(
    (0.0, 6.0, "好同學這一課教你 it 做虛主詞"),
    (6.0, 14.0, "你不管跟兩千隻還是一隻狗玩它都是一件事情"),
    (14.0, 22.0, "所以它表示單數老師如果我頭再大一點就會翻過去"),
    (22.0, 30.0, "抽小隻煙跟抽大隻煙都一樣不好"),
)


def proposal(**overrides) -> dict:
    base = {
        "title": "頭重腳輕的神比喻",
        "start": 6.0,
        "end": 22.0,
        "hook": "你不管跟兩千隻還是一隻狗玩",
        "reason": "比喻誇張，記憶點強",
    }
    base.update(overrides)
    return base


class GroundingTests(unittest.TestCase):
    def ground(self, *items: dict, count: int = 8) -> tuple[list[dict], list[str]]:
        return ep.ground_proposals(
            list(items), LESSON, duration_s=30.0,
            min_duration=8.0, max_duration=60.0, count=count,
        )

    def test_a_grounded_proposal_keeps_its_editorial_title(self) -> None:
        kept, _ = self.ground(proposal())
        self.assertEqual(len(kept), 1)
        editorial = kept[0]["editorial"]
        self.assertEqual(editorial["title"], "頭重腳輕的神比喻")
        self.assertTrue(editorial["is_editorial_copy"])

    def test_a_hook_the_transcript_never_says_is_a_fabrication(self) -> None:
        # The whole point of grounding: a model can write a plausible quote
        # for a moment that does not exist. Repairing it would launder the
        # fabrication, so the cut is dropped and the reason is reported.
        kept, warnings = self.ground(proposal(hook="老師說抽菸其實對身體很好"))
        self.assertEqual(kept, [])
        self.assertTrue(any("not spoken inside the window" in w for w in warnings))

    def test_a_hook_from_a_different_part_of_the_video_does_not_count(self) -> None:
        # Real sentence, wrong window — the model moved a quote to a cut it
        # does not belong to.
        kept, warnings = self.ground(proposal(start=0.0, end=14.0, hook="抽小隻煙跟抽大隻煙都一樣不好"))
        self.assertEqual(kept, [])
        self.assertTrue(any("not spoken" in w for w in warnings))

    def test_punctuation_drift_between_quote_and_transcript_is_tolerated(self) -> None:
        kept, _ = self.ground(proposal(hook="你不管跟兩千隻，還是一隻狗玩！"))
        self.assertEqual(len(kept), 1)

    def test_a_window_outside_the_source_is_dropped(self) -> None:
        kept, warnings = self.ground(proposal(start=100.0, end=120.0))
        self.assertEqual(kept, [])
        self.assertTrue(any("outside the source" in w for w in warnings))

    def test_overlapping_cuts_keep_the_earlier_one(self) -> None:
        kept, warnings = self.ground(
            proposal(title="第一段", start=0.0, end=14.0, hook="好同學這一課教你"),
            proposal(title="重疊段", start=6.0, end=22.0),
        )
        self.assertEqual([item["editorial"]["title"] for item in kept], ["第一段"])
        self.assertTrue(any("overlaps" in w for w in warnings))

    def test_every_rejection_is_reported(self) -> None:
        # A silently dropped proposal reads as "the model only found one".
        _, warnings = self.ground(
            proposal(title="好的"),
            proposal(title="壞的", start=200.0, end=260.0),
        )
        self.assertTrue(any("壞的" in w for w in warnings))

    def test_more_cuts_than_asked_for_are_trimmed_and_said_so(self) -> None:
        kept, warnings = self.ground(
            proposal(title="甲", start=0.0, end=14.0, hook="好同學這一課教你"),
            proposal(title="乙", start=14.0, end=30.0, hook="所以它表示單數"),
            count=1,
        )
        self.assertEqual(len(kept), 1)
        self.assertTrue(any("kept the first 1" in w for w in warnings))


class PlanItemTests(unittest.TestCase):
    def test_the_item_title_stays_an_exact_transcript_extract(self) -> None:
        # The highlight-plan contract requires it; the editorial wording is
        # carried alongside, labelled, never passed off as a quotation.
        kept, _ = ep.ground_proposals(
            [proposal()], LESSON, duration_s=30.0,
            min_duration=8.0, max_duration=60.0, count=8,
        )
        item = ep.to_highlight_items(kept)[0]
        self.assertIn(item["title"], item["evidence"]["text"])
        self.assertTrue(item["evidence"]["exact_transcript_extract"])
        self.assertEqual(item["title_source"], "transcript_extract")
        self.assertNotEqual(item["editorial"]["title"], item["title"])

    def test_scores_stay_inside_the_contract_range(self) -> None:
        many = [
            proposal(title=f"第{index}段", start=index * 8.0, end=index * 8.0 + 8.0,
                     hook=LESSON[index]["text"][:6])
            for index in range(4)
        ]
        kept, _ = ep.ground_proposals(
            many, LESSON, duration_s=32.0, min_duration=6.0, max_duration=60.0, count=8
        )
        for item in ep.to_highlight_items(kept):
            self.assertGreaterEqual(item["score"], 0.0)
            self.assertLessEqual(item["score"], 1.0)


class ResponseParsingTests(unittest.TestCase):
    def test_a_fenced_array_is_read(self) -> None:
        payload = '```json\n[{"title":"甲"}]\n```'
        self.assertEqual(ep.parse_json_payload(payload), [{"title": "甲"}])

    def test_a_chatty_wrapper_around_the_array_is_read(self) -> None:
        payload = '好的，以下是我選的段落：\n[{"title":"甲"}]\n希望有幫助'
        self.assertEqual(ep.parse_json_payload(payload), [{"title": "甲"}])

    def test_prose_with_no_array_is_an_error_not_an_empty_plan(self) -> None:
        with self.assertRaises(ep.EditorialUnavailable):
            ep.parse_json_payload("我沒辦法幫你做這件事")


if __name__ == "__main__":
    unittest.main()

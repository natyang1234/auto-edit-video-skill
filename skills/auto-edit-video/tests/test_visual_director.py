"""The director decides what goes on screen, using only what was said."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import visual_director as vd  # noqa: E402


def evidence(kind: str, literal: str, start: float, end: float, tag: str) -> dict:
    return {
        "id": f"evidence-{tag * 4}",
        "kind": kind,
        "literal": literal,
        "start": start,
        "end": end,
    }


def segments(count: int, length: float = 4.0) -> list[dict]:
    # The field names the editor stores, not a shape invented for the test.
    return [
        {
            "id": f"h{index}",
            "source_start": index * length,
            "source_end": (index + 1) * length,
        }
        for index in range(count)
    ]


class VisualDirectorTests(unittest.TestCase):
    def beats(self, result: dict) -> list[str]:
        return [item["beat"] for item in result["visual_plan"]["items"]]

    def test_a_spoken_number_becomes_a_stat(self) -> None:
        result = vd.plan_visuals(
            segments(2), [evidence("number", "87%", 5.0, 5.5, "ab12")]
        )
        self.assertEqual(self.beats(result)[1], "stat")
        self.assertEqual(vd.validate(result), [])
        layer = result["structured_layers"]["items"][0]
        self.assertEqual(layer["payload"]["source_literal"], "87%")
        self.assertEqual(layer["payload"]["evidence_id"], "evidence-ab12ab12ab12ab12")

    def test_nothing_is_put_on_screen_without_evidence_for_it(self) -> None:
        # Prose with no figures and no enumeration keeps the picture it has.
        result = vd.plan_visuals(
            segments(3),
            [evidence("quote", "這件事其實沒那麼複雜", 5.0, 6.0, "cd34")],
        )
        self.assertEqual(self.beats(result)[1:], ["keep_aroll", "keep_aroll"])
        self.assertEqual(result["structured_layers"]["items"], [])

    def test_several_numbers_become_one_chart_not_several_stats(self) -> None:
        result = vd.plan_visuals(
            segments(2),
            [
                evidence("number", "12%", 4.2, 4.4, "aa11"),
                evidence("number", "34%", 5.2, 5.4, "bb22"),
                evidence("number", "56%", 6.2, 6.4, "cc33"),
            ],
        )
        self.assertEqual(self.beats(result)[1], "chart")
        datums = result["structured_layers"]["items"][0]["payload"]["datums"]
        self.assertEqual([datum["value"] for datum in datums], [12.0, 34.0, 56.0])
        self.assertEqual(vd.validate(result), [])

    def test_enumeration_becomes_a_list(self) -> None:
        result = vd.plan_visuals(
            segments(2),
            [
                evidence("quote", "首先是成本", 4.1, 4.5, "aa11"),
                evidence("quote", "其次是時間", 5.1, 5.5, "bb22"),
                evidence("quote", "最後是品質", 6.1, 6.5, "cc33"),
            ],
        )
        self.assertEqual(self.beats(result)[1], "dynamic_list")
        items = result["structured_layers"]["items"][0]["payload"]["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(entry["evidence_id"] for entry in items))

    def test_cards_do_not_run_back_to_back(self) -> None:
        # Two adjacent segments both carrying figures: the second waits.
        result = vd.plan_visuals(
            segments(3),
            [
                evidence("number", "10%", 0.5, 0.9, "aa11"),
                evidence("number", "20%", 4.5, 4.9, "bb22"),
                evidence("number", "30%", 8.5, 8.9, "cc33"),
            ],
        )
        beats = self.beats(result)
        for first, second in zip(beats, beats[1:]):
            self.assertFalse(
                first != "keep_aroll" and second != "keep_aroll",
                f"two cards in a row: {beats}",
            )

    def test_cards_stay_a_minority_of_the_cut(self) -> None:
        many = segments(10)
        found = [
            evidence("number", f"{index * 10}%", index * 4 + 0.5, index * 4 + 0.9, f"{index:04d}")
            for index in range(10)
        ]
        result = vd.plan_visuals(many, found)
        decorated = [beat for beat in self.beats(result) if beat != "keep_aroll"]
        self.assertLessEqual(len(decorated), 5)

    def test_a_segment_without_a_usable_window_is_skipped(self) -> None:
        result = vd.plan_visuals(
            [{"id": "h0"}, {"id": "h1", "source_start": 0.0, "source_end": 4.0}], []
        )
        self.assertEqual(len(result["visual_plan"]["items"]), 1)
        self.assertEqual(vd.validate(result), [])

    def test_a_number_that_is_not_a_measurement_gets_no_card(self) -> None:
        # "exit 4" and "the second floor" are numbers, not statistics.
        for literal in ("4", "二樓", "8"):
            with self.subTest(literal):
                result = vd.plan_visuals(
                    segments(2), [evidence("number", literal, 5.0, 5.4, "aa11")]
                )
                self.assertEqual(self.beats(result)[1], "keep_aroll")

    def test_a_single_take_can_still_carry_one_card(self) -> None:
        # A timeline that has not been cut into highlights is one segment;
        # a budget proportional to segment count would allow it nothing.
        result = vd.plan_visuals(
            segments(1, length=17.0), [evidence("number", "87%", 5.0, 5.4, "aa11")]
        )
        self.assertEqual(self.beats(result), ["stat"])
        self.assertEqual(vd.validate(result), [])

    def test_the_same_input_plans_the_same_video(self) -> None:
        found = [evidence("number", "87%", 5.0, 5.5, "ab12")]
        first = vd.plan_visuals(segments(2), found)
        again = vd.plan_visuals(segments(2), found)
        self.assertEqual(first, again)

    def test_a_card_does_not_sit_there_for_the_whole_take(self) -> None:
        # One 17s single-take segment is the state every project starts in.
        # The card used to inherit that span and park over the speaker's face.
        one_long_take = [{"id": "h0", "source_start": 0.0, "source_end": 17.233}]
        result = vd.plan_visuals(
            one_long_take, [evidence("quote", "今晚別宅在家", 0.2, 2.6, "aa11")]
        )
        item = result["visual_plan"]["items"][0]
        self.assertEqual(item["beat"], "title")
        self.assertLessEqual(
            item["end"] - item["start"],
            vd.CARD_DWELL_SECONDS["title"],
            "a title card must not run the length of its segment",
        )
        self.assertEqual(vd.validate(result), [])

    def test_a_short_segment_is_never_stretched_to_the_dwell(self) -> None:
        brief = [{"id": "h0", "source_start": 0.0, "source_end": 1.2}]
        result = vd.plan_visuals(
            brief, [evidence("quote", "跟我走", 0.1, 1.0, "bb22")]
        )
        item = result["visual_plan"]["items"][0]
        self.assertEqual(item["end"], 1.2, "the cap shortens; it never extends")

    def test_every_plan_satisfies_the_contracts(self) -> None:
        result = vd.plan_visuals(
            segments(4),
            [
                evidence("quote", "今天講三件事", 0.2, 1.0, "aa11"),
                evidence("number", "87%", 4.2, 4.6, "bb22"),
                evidence("quote", "首先是成本", 8.2, 8.6, "cc33"),
                evidence("quote", "其次是時間", 9.2, 9.6, "dd44"),
                evidence("quote", "最後是品質", 10.2, 10.6, "ee55"),
            ],
        )
        self.assertEqual(vd.validate(result), [])


if __name__ == "__main__":
    unittest.main()

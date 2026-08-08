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
            one_long_take, [evidence("quote", "週末別窩在家", 0.2, 2.6, "aa11")],
            editorial_title="今晚就出門",
        )
        item = result["visual_plan"]["items"][0]
        self.assertEqual(item["beat"], "title")
        self.assertLessEqual(
            item["end"] - item["start"],
            vd.CARD_DWELL_SECONDS["title"],
            "a title card must not run the length of its segment",
        )
        self.assertEqual(vd.validate(result), [])

    def test_a_big_round_number_used_rhetorically_is_not_a_stat(self) -> None:
        # "whether you play with 2000 dogs or one dog" — the figure is
        # hyperbole. A stat card would assert somebody measured it.
        result = vd.plan_visuals(
            segments(1),
            [evidence("number", "2000", 1.0, 1.4, "aa11"),
             evidence("quote", "你不管跟 2000 隻還是一隻狗玩", 0.5, 3.0, "bb22")],
        )
        self.assertNotIn("stat", self.beats(result))

    def test_a_figure_with_a_unit_is_still_a_stat(self) -> None:
        result = vd.plan_visuals(
            segments(1), [evidence("number", "2000 人", 1.0, 1.4, "cc33")]
        )
        self.assertEqual(self.beats(result), ["stat"])

    def test_a_title_card_requires_editorial_copy(self) -> None:
        # With the model unavailable the fallback was a transcript sentence,
        # and a KTV clip shipped its own mis-heard transcript as a prominent
        # card. The words are already on screen as captions; without a
        # written name there is no nameplate.
        one_take = [{"id": "h0", "source_start": 0.0, "source_end": 17.0}]
        found = [evidence("quote", "週末別窩在家跟我走", 0.2, 2.6, "aa11")]
        plain = vd.plan_visuals(one_take, found)
        named = vd.plan_visuals(one_take, found, editorial_title="台北今晚約會路線")
        self.assertNotIn("title", [i["beat"] for i in plain["visual_plan"]["items"]])
        self.assertEqual(
            named["structured_layers"]["items"][0]["payload"]["title"],
            "台北今晚約會路線",
        )

    def test_a_blank_editorial_title_is_no_title_at_all(self) -> None:
        one_take = [{"id": "h0", "source_start": 0.0, "source_end": 17.0}]
        found = [evidence("quote", "週末別窩在家跟我走", 0.2, 2.6, "aa11")]
        result = vd.plan_visuals(one_take, found, editorial_title="   ")
        self.assertNotIn("title", [i["beat"] for i in result["visual_plan"]["items"]])

    def test_a_short_segment_is_never_stretched_to_the_dwell(self) -> None:
        brief = [{"id": "h0", "source_start": 0.0, "source_end": 1.2}]
        result = vd.plan_visuals(
            brief, [evidence("quote", "跟我走", 0.1, 1.0, "bb22")],
            editorial_title="出發",
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


class MoreKindsOfCardTests(unittest.TestCase):
    """Four more card kinds, each built only out of words that were said.

    A quote or a question is the line itself. A comparison or a definition
    has two halves, so each pattern requires the connective that separates
    them: the split is found in the sentence, never guessed. Without one the
    segment keeps its picture.
    """

    def plan(self, literal: str, count: int = 2) -> dict:
        return vd.plan_visuals(
            segments(count), [evidence("quote", literal, 5.0, 6.0, "aa11")]
        )

    def beat_of(self, literal: str) -> str:
        return [item["beat"] for item in self.plan(literal)["visual_plan"]["items"]][1]

    def payload_of(self, literal: str) -> dict:
        return self.plan(literal)["structured_layers"]["items"][0]["payload"]

    def test_a_definition_becomes_a_term_card(self) -> None:
        self.assertEqual(self.beat_of("所謂的虛主詞就是代替不定詞片語"), "term")
        payload = self.payload_of("所謂的虛主詞就是代替不定詞片語")
        self.assertEqual(payload["term"], "虛主詞")
        self.assertEqual(payload["meaning"], "代替不定詞片語")

    def test_a_naming_sentence_is_read_the_right_way_round(self) -> None:
        # "這個東西叫做虛主詞" names the thing second.
        payload = self.payload_of("這個東西叫做虛主詞")
        self.assertEqual(payload["term"], "虛主詞")
        self.assertEqual(payload["meaning"], "這個東西")

    def test_a_contrast_becomes_a_comparison_card(self) -> None:
        for literal, left, right in (
            ("不是抽菸而是抽雪茄", "抽菸", "抽雪茄"),
            ("雪茄跟香菸的差別", "雪茄", "香菸"),
        ):
            with self.subTest(literal):
                self.assertEqual(self.beat_of(literal), "comparison")
                payload = self.payload_of(literal)
                self.assertEqual((payload["left"], payload["right"]), (left, right))

    def test_both_halves_are_words_the_sentence_contains(self) -> None:
        literal = "不是抽菸而是抽雪茄"
        payload = self.payload_of(literal)
        for half in (payload["left"], payload["right"]):
            self.assertIn(half, literal, "a card half nobody said is a fabrication")

    def test_a_question_becomes_a_question_card(self) -> None:
        self.assertEqual(self.beat_of("為什麼大家都做錯"), "question")
        self.assertEqual(
            self.payload_of("為什麼大家都做錯")["question"], "為什麼大家都做錯"
        )

    def test_a_verbal_tic_is_not_a_question(self) -> None:
        # 對不對 and 好不好 end half the sentences in spoken teaching and ask
        # nothing; a card on each would be a card on every other cut.
        for literal in ("這樣對不對", "我們繼續好不好"):
            with self.subTest(literal):
                self.assertEqual(self.beat_of(literal), "keep_aroll")

    def test_a_landed_point_becomes_a_pull_quote(self) -> None:
        self.assertEqual(self.beat_of("重點根本不在努力"), "quote")
        self.assertEqual(self.payload_of("重點根本不在努力")["quote"], "重點根本不在努力")

    def test_ordinary_speech_is_left_alone(self) -> None:
        # 其實, 說真的 and 你會發現 open a third of ordinary Taiwanese
        # sentences. A marker that fires on narration does not select.
        for literal in (
            "這件事其實沒那麼複雜",
            "其實我覺得還好",
            "說真的我也不知道",
            "今天天氣很好我們出門走走",
        ):
            with self.subTest(literal):
                self.assertEqual(self.beat_of(literal), "keep_aroll")

    def test_a_contrast_without_a_connective_is_not_split(self) -> None:
        # Two nouns in one sentence are not a comparison; without the word
        # that separates them there is nothing to put on either side.
        self.assertEqual(self.beat_of("我今天買了雪茄和香菸還有打火機"), "keep_aroll")

    def test_a_quote_too_long_to_read_is_not_pulled(self) -> None:
        long_line = "重點是" + "很長的句子" * 8
        self.assertEqual(self.beat_of(long_line), "keep_aroll")

    def test_every_new_kind_satisfies_the_contracts(self) -> None:
        for literal in (
            "所謂的虛主詞就是代替不定詞片語",
            "不是抽菸而是抽雪茄",
            "為什麼大家都做錯",
            "重點根本不在努力",
        ):
            with self.subTest(literal):
                self.assertEqual(vd.validate(self.plan(literal)), [])

    def test_each_new_kind_has_a_time_on_screen(self) -> None:
        # Without one the card inherits its whole segment and parks over the
        # speaker, which is the first defect ever reported against this tool.
        for kind in ("quote", "question", "comparison", "term"):
            with self.subTest(kind):
                self.assertIn(kind, vd.CARD_DWELL_SECONDS)


class EnumerationAcrossTheClipTests(unittest.TestCase):
    """A spoken list is a clip-level structure, found once and drawn once.

    「...更多的錢。第二個願望...第三個願望...」 spreads its items across ten
    seconds: no single planning window ever held three, so nothing was ever
    drawn. Found on a real birthday clip whose three wishes are the whole
    point of the video.
    """

    @staticmethod
    def beats(result: dict) -> list[str]:
        return [item["beat"] for item in result["visual_plan"]["items"]]

    WISHES = [
        evidence("quote", "讓今年大家都賺更多更多的錢", 0.9, 3.2, "a111"),
        evidence("quote", "第二個願望就是大家的感情都有個好歸宿", 4.6, 8.4, "b222"),
        evidence("quote", "第三個願望", 8.8, 10.5, "c333"),
    ]

    def windows(self):
        # Two 9s planning windows, splitting the wishes 2/1.
        return [
            {"id": "h0-w0", "source_start": 0.58, "source_end": 9.5},
            {"id": "h0-w1", "source_start": 9.5, "source_end": 18.42},
        ]

    def test_a_list_split_across_windows_is_still_drawn(self) -> None:
        result = vd.plan_visuals(self.windows(), list(self.WISHES))
        self.assertIn("dynamic_list", self.beats(result))
        items = result["structured_layers"]["items"][-1]["payload"]["items"]
        self.assertEqual(len(items), 3)

    def test_the_unannounced_first_item_is_the_line_before_第二(self) -> None:
        listed = vd.enumerated_quotes(list(self.WISHES))
        self.assertEqual(listed[0]["literal"], "讓今年大家都賺更多更多的錢")

    def test_an_ordinal_in_a_noun_phrase_is_not_a_list_item(self) -> None:
        # 第五顆蛋糕 carries 第五, spoken eight seconds after 第三; an
        # enumeration does not skip a number.
        with_cake = [*self.WISHES,
                     evidence("quote", "今天的第五顆蛋糕", 16.8, 18.4, "d444")]
        listed = [item["literal"] for item in vd.enumerated_quotes(with_cake)]
        self.assertNotIn("今天的第五顆蛋糕", listed)
        self.assertEqual(len(listed), 3)

    def test_the_opening_window_stays_the_nameplate(self) -> None:
        # An enumeration starting at the top used to claim the opening
        # window, and the title never appeared; the list lands later.
        result = vd.plan_visuals(
            self.windows(), list(self.WISHES), editorial_title="三個生日願望"
        )
        beats = self.beats(result)
        self.assertEqual(beats[0], "title")
        self.assertEqual(beats[1], "dynamic_list")

    def test_a_short_clip_still_gets_a_card_after_its_title(self) -> None:
        # round(), not int(): two windows at a half share is one, which the
        # title consumed — so no clip under ~24s could ever carry a second
        # card. The nameplate no longer spends the decoration budget.
        result = vd.plan_visuals(
            self.windows(), list(self.WISHES), editorial_title="三個生日願望"
        )
        decorated = [beat for beat in self.beats(result) if beat != "keep_aroll"]
        self.assertEqual(len(decorated), 2)


if __name__ == "__main__":
    unittest.main()

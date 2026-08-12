"""Emphasis has to move when the line does.

Line breaks are decided in the compositor, after the emphasis spans were
measured against the unbroken line. Drawn as given, every span after a break
sits one character too far left — one per break. Seen on a real caption:
「它呢代替的就是後面這個叫做真正的主詞也就是」 broke into two lines and the
keyword 「真正的主詞」 lit up 「做真正的主」 instead.

The translation range beside it has always been re-found after wrapping, for
exactly this reason. The spans were not.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as compositor  # noqa: E402

LINE = "它呢代替的就是後面這個叫做真正的主詞也就是"
BROKEN = "它呢代替的就是後面這\n個叫做真正的主詞也就是"


def span(text: str, phrase: str, **style) -> dict:
    start = text.index(phrase)
    return {
        "text": phrase, "start_char": start, "end_char": start + len(phrase),
        "style": style or {"effect": "pop"},
    }


def lit(text: str, moved: dict) -> str:
    return text[moved["start_char"]:moved["end_char"]]


class MovedWithTheBreakTests(unittest.TestCase):
    def test_a_span_after_a_break_still_covers_its_own_words(self) -> None:
        moved = compositor.rewrapped_effect_spans(
            [span(LINE, "真正的主詞")], LINE, BROKEN
        )
        self.assertEqual(lit(BROKEN, moved[0]), "真正的主詞")

    def test_a_span_before_the_break_does_not_move(self) -> None:
        original = span(LINE, "代替")
        moved = compositor.rewrapped_effect_spans([original], LINE, BROKEN)
        self.assertEqual(moved[0]["start_char"], original["start_char"])
        self.assertEqual(lit(BROKEN, moved[0]), "代替")

    def test_every_break_shifts_by_one_more(self) -> None:
        # Two breaks, so the last span is two characters out if nothing moves.
        three = "第一段文字第二段文字第三段文字"
        wrapped = "第一段文字\n第二段文字\n第三段文字"
        moved = compositor.rewrapped_effect_spans(
            [span(three, "第三段文字")], three, wrapped
        )
        self.assertEqual(lit(wrapped, moved[0]), "第三段文字")

    def test_a_line_that_did_not_break_is_left_exactly_alone(self) -> None:
        given = [span(LINE, "真正的主詞")]
        self.assertEqual(compositor.rewrapped_effect_spans(given, LINE, LINE), given)

    def test_a_latin_word_survives_the_space_the_break_ate(self) -> None:
        # Wrapping strips the space it broke on, so the text loses a
        # character rather than only gaining one.
        line = "today we play with puppies and it is fun"
        wrapped = "today we play with\npuppies and it is fun"
        moved = compositor.rewrapped_effect_spans([span(line, "puppies")], line, wrapped)
        self.assertEqual(lit(wrapped, moved[0]), "puppies")

    def test_emphasis_on_nothing_but_a_dropped_space_is_dropped(self) -> None:
        # Pointing it somewhere else would emphasise a word nobody chose.
        line = "today we play with puppies"
        wrapped = "today we play with\npuppies"
        moved = compositor.rewrapped_effect_spans(
            [{"start_char": 18, "end_char": 19, "style": {}}], line, wrapped
        )
        self.assertEqual(moved, [])

    def test_the_style_travels_with_the_span(self) -> None:
        moved = compositor.rewrapped_effect_spans(
            [span(LINE, "真正的主詞", effect="highlight", color="#FF5533")],
            LINE, BROKEN,
        )
        self.assertEqual(moved[0]["style"]["effect"], "highlight")
        self.assertEqual(moved[0]["style"]["color"], "#FF5533")

    def test_no_spans_is_not_an_error(self) -> None:
        self.assertEqual(compositor.rewrapped_effect_spans(None, LINE, BROKEN), [])
        self.assertEqual(compositor.rewrapped_effect_spans([], LINE, BROKEN), [])


class BothDrawingPassesUseTheMovedSpansTests(unittest.TestCase):
    """One remap, read by both passes — not a second answer per pass."""

    def test_the_compositor_draws_from_the_moved_spans(self) -> None:
        # Two drawing sites — the attributed string and the highlight pills —
        # and both read the remap rather than each redoing it.
        import inspect

        source = inspect.getsource(compositor.render_caption_png)
        self.assertEqual(source.count("for span in drawn_spans"), 2)

    def test_only_the_pre_wrap_scale_pass_reads_the_given_spans(self) -> None:
        # It runs before the breaks are decided and reads font_scale, which
        # has no position in it — the wrap width depends on that scale, so it
        # cannot wait for the remap. Any other pre-wrap reader would be
        # addressing the unbroken line again.
        import inspect

        # The measuring context is entirely pre-wrap by construction — it is
        # what the wrap is decided with — so it counts as "before the wrap"
        # in full, wherever the scale pass physically lives.
        source = inspect.getsource(compositor.render_caption_png)
        before_wrap = inspect.getsource(compositor._measure_context) + source.split(
            'text = "\\n".join(spoken_lines)'
        )[0]
        self.assertEqual(
            before_wrap.count('overlay.get("effect_spans")'), 1, before_wrap[-400:]
        )
        self.assertIn("emphasis_scale", before_wrap)


if __name__ == "__main__":
    unittest.main()

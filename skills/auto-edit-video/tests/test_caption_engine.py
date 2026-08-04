"""Phase 1b N0: grapheme boundary authority, UTF-16 helpers, span snapping."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_engine as ce  # noqa: E402


class Utf16HelperTests(unittest.TestCase):
    def test_lengths(self) -> None:
        self.assertEqual(ce.utf16_length("abc"), 3)
        self.assertEqual(ce.utf16_length("你好"), 2)
        self.assertEqual(ce.utf16_length("👍"), 2)
        self.assertEqual(ce.utf16_length("👩‍👩‍👧‍👦"), 11)

    def test_slice_utf16(self) -> None:
        text = "a👍b"
        self.assertEqual(ce.slice_utf16(text, 0, 1), "a")
        self.assertEqual(ce.slice_utf16(text, 1, 3), "👍")
        self.assertEqual(ce.slice_utf16(text, 3, 4), "b")

    def test_surrogate_split_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ce.slice_utf16("👍", 0, 1)
        with self.assertRaises(ValueError):
            ce.codepoint_index_for_utf16("a👍", 2)


@unittest.skipUnless(ce.available(), "needs the macOS caption engine")
class BoundaryTests(unittest.TestCase):
    def test_corpus_v2(self) -> None:
        corpus = json.loads(
            (SKILL_DIR / "contracts/fixtures/grapheme_corpus.json").read_text("utf-8")
        )
        self.assertEqual(corpus["x_schema_version"], 2)
        for case in corpus["cases"]:
            clusters = ce.boundary_map(case["text"])
            self.assertEqual(
                len(clusters), case["clusters"], f"cluster count: {case['note']}"
            )
            if "utf16_boundaries" in case:
                self.assertEqual(
                    clusters, case["utf16_boundaries"], f"boundaries: {case['note']}"
                )

    def test_snap_expands_partial_emoji_selection(self) -> None:
        text = "我愛👩‍👩‍👧‍👦你"
        snapped = ce.snap_span(text, 3, 5)  # inside the ZWJ family
        self.assertEqual(snapped, (2, 13))

    def test_snap_keeps_boundary_aligned_selection(self) -> None:
        text = "你好世界"
        self.assertEqual(ce.snap_span(text, 1, 3), (1, 3))

    def test_snap_returns_none_for_empty(self) -> None:
        self.assertIsNone(ce.snap_span("你好", 0, 0))

    def test_span_on_boundaries(self) -> None:
        text = "a👍b"
        self.assertTrue(ce.span_on_boundaries(text, 1, 3))
        self.assertFalse(ce.span_on_boundaries(text, 1, 2))


if __name__ == "__main__":
    unittest.main()

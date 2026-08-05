"""What the plan asks for has to reach the frame, or say why not.

The costly failures here are silent ones: a plan holding a picture, a render
that reports success, and nothing on screen. Nobody sees it unless they
pull a frame and look.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import card_plan  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402


class AssetOnlyPlanTests(unittest.TestCase):
    """A plan of nothing but pictures still has to be placed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="asset-only-")
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.project / "working").mkdir(parents=True)

    def plan_with(self, *cards: dict) -> tuple[dict, dict]:
        (self.project / "working/card_plan.json").write_text(
            json.dumps({
                "schema_version": 1, "source_sha256": "c" * 64,
                "revision": "d" * 64, "items": list(cards),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        bundle = renderer.card_plan_bundle(self.project)
        self.assertIsNotNone(bundle)
        return bundle

    @staticmethod
    def picture(start: float = 2.0, end: float = 5.0) -> dict:
        return {
            "id": "card-aaaa1111bbbb", "start": start, "end": end, "kind": "image",
            "payload": {"asset": "assets/imported/aa-店門口.png"}, "origin": "model",
        }

    @staticmethod
    def card(start: float = 8.0, end: float = 11.0) -> dict:
        return {
            "id": "card-cccc2222dddd", "start": start, "end": end, "kind": "title",
            "payload": {"title": "標題"}, "origin": "manual",
        }

    def test_a_plan_of_only_pictures_still_yields_beats(self) -> None:
        # The beat loop used to live inside the branch that composes cards,
        # so a plan with no card was skipped whole: no picture drawn, no
        # error, a render that reported success.
        layers, visual_plan = self.plan_with(self.picture())
        self.assertEqual(layers["items"], [])
        self.assertEqual(len(visual_plan["items"]), 1)
        self.assertEqual(visual_plan["items"][0]["beat"], "image")
        self.assertTrue(visual_plan["items"][0]["selected_asset"])

    def test_pictures_and_cards_together_both_survive(self) -> None:
        layers, visual_plan = self.plan_with(self.picture(), self.card())
        self.assertEqual(len(layers["items"]), 1)
        self.assertEqual(len(visual_plan["items"]), 2)
        beats = sorted(item["beat"] for item in visual_plan["items"])
        self.assertEqual(beats, ["image", "title"])

    def test_a_picture_beat_carries_no_layer_to_compose(self) -> None:
        _, visual_plan = self.plan_with(self.picture())
        item = visual_plan["items"][0]
        self.assertIsNone(item["structured_layer_id"])
        self.assertFalse(item["conceptual_only"])

    def test_a_picture_card_naming_no_file_is_refused(self) -> None:
        # An empty asset would plan a beat pointing nowhere, which the
        # renderer draws as nothing at all.
        with self.assertRaises(ValueError):
            card_plan.to_layer_bundle({
                "items": [dict(self.picture(), payload={})],
            })


if __name__ == "__main__":
    unittest.main()

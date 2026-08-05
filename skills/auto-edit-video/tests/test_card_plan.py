"""Phase A: Independent card planning as a single source of truth."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import card_plan  # noqa: E402
import contract_registry  # noqa: E402


class CardPlanTests(unittest.TestCase):
    """Phase A: Cards are managed as a single list in working/card_plan.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="card-plan-")
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_empty_plan_is_valid(self) -> None:
        plan = card_plan.empty_plan("")
        errors = contract_registry.validate_artifact("card_plan", plan)
        self.assertEqual(errors, [])

    def test_add_one_title_card(self) -> None:
        plan, notes = card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "第一個卡片"},
        )
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["kind"], "title")
        self.assertEqual(plan["items"][0]["payload"]["title"], "第一個卡片")
        self.assertEqual(plan["items"][0]["origin"], "manual")
        errors = contract_registry.validate_artifact("card_plan", plan)
        self.assertEqual(errors, [])

    def test_plan_persists_to_disk(self) -> None:
        card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "持久化卡片"},
        )
        path = self.project / card_plan.CARD_PLAN_REL
        self.assertTrue(path.is_file())
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded["items"]), 1)
        self.assertEqual(loaded["items"][0]["payload"]["title"], "持久化卡片")

    def test_add_multiple_cards_in_order(self) -> None:
        card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "第一個"},
        )
        card_plan.add(
            self.project,
            "",
            start=10.0,
            end=13.0,
            kind="stat",
            payload={"value": "42%", "label": "成功率"},
        )
        plan = card_plan.load(self.project, "")
        self.assertEqual(len(plan["items"]), 2)
        self.assertEqual(plan["items"][0]["start"], 5.0)
        self.assertEqual(plan["items"][1]["start"], 10.0)

    def test_manual_card_prevents_collision(self) -> None:
        # A manual card placed first should prevent an overlapping card.
        card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "手動"},
            origin="manual",
        )
        # Try to add an overlapping model proposal; it should fail.
        with self.assertRaises(ValueError):
            card_plan.add(
                self.project,
                "",
                start=6.0,
                end=9.0,
                kind="stat",
                payload={"value": "99%", "label": "比例"},
                origin="model",
            )

    def test_card_too_short_is_rejected(self) -> None:
        # Cards need at least MIN_CARD_SECONDS to be read.
        with self.assertRaises(ValueError):
            card_plan.add(
                self.project,
                "",
                start=5.0,
                end=5.2,
                kind="title",
                payload={"title": "太短"},
            )

    def test_manual_outranks_model(self) -> None:
        # When cards collide, manual > model.
        card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "模型"},
            origin="model",
        )
        plan, notes = card_plan.add(
            self.project,
            "",
            start=6.0,
            end=9.0,
            kind="stat",
            payload={"value": "手動", "label": ""},
            origin="manual",
        )
        # The model card should be dropped.
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["origin"], "manual")

    def test_plan_can_be_converted_to_layer_bundle(self) -> None:
        card_plan.add(
            self.project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "標題卡", "title_kind": "section"},
        )
        plan = card_plan.load(self.project, "")
        layers, visual_plan = card_plan.to_layer_bundle(plan)
        self.assertEqual(len(layers["items"]), 1)
        self.assertEqual(len(visual_plan["items"]), 1)
        self.assertEqual(layers["items"][0]["type"], "title")
        self.assertEqual(visual_plan["items"][0]["start"], 5.0)
        self.assertEqual(visual_plan["items"][0]["end"], 8.0)


class CardMergeTests(unittest.TestCase):
    """The merge policy: manual outranks model/director; director is lowest."""

    def test_no_collision_keeps_all_cards(self) -> None:
        existing = [
            {
                "id": "card-1",
                "start": 5.0,
                "end": 8.0,
                "kind": "title",
                "origin": "manual",
                "payload": {},
            }
        ]
        incoming = [
            {
                "id": "card-2",
                "start": 10.0,
                "end": 13.0,
                "kind": "stat",
                "origin": "model",
                "payload": {},
            }
        ]
        merged, notes = card_plan.merge(existing, incoming)
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(notes), 0)

    def test_collision_keeps_higher_rank(self) -> None:
        existing = [
            {
                "id": "card-manual",
                "start": 5.0,
                "end": 8.0,
                "kind": "title",
                "origin": "manual",
                "payload": {},
            }
        ]
        incoming = [
            {
                "id": "card-model",
                "start": 6.0,
                "end": 9.0,
                "kind": "stat",
                "origin": "model",
                "payload": {},
            }
        ]
        merged, notes = card_plan.merge(existing, incoming)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "card-manual")
        self.assertEqual(len(notes), 1)

    def test_replace_origin_preserves_others(self) -> None:
        # replace_origin() swaps all cards from one producer without
        # touching cards from other sources.
        project = Path(tempfile.mkdtemp())
        card_plan.add(
            project,
            "",
            start=5.0,
            end=8.0,
            kind="title",
            payload={"title": "manual"},
            origin="manual",
        )
        card_plan.add(
            project,
            "",
            start=10.0,
            end=13.0,
            kind="stat",
            payload={"value": "old model"},
            origin="model",
        )
        # Replace all model cards with a new proposal.
        new_id = card_plan.card_id(10.0, 13.0, "stat", {"value": "new model"})
        new_cards = [
            {
                "id": new_id,
                "start": 10.0,
                "end": 13.0,
                "kind": "stat",
                "origin": "model",
                "payload": {"value": "new model"},
            }
        ]
        plan, notes = card_plan.replace_origin(project, "", "model", new_cards)
        self.assertEqual(len(plan["items"]), 2)
        manual = next(c for c in plan["items"] if c["origin"] == "manual")
        model = next(c for c in plan["items"] if c["origin"] == "model")
        self.assertEqual(manual["payload"]["title"], "manual")
        self.assertEqual(model["payload"]["value"], "new model")


if __name__ == "__main__":
    unittest.main()

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
    {"start": 5.0, "end": 10.0, "text": "然後從市場買菜回來"},
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
                     quote="從市場買菜回來"),
        )
        self.assertEqual([card["start"] for card in cards], [1.0, 6.0])
        self.assertTrue(any("within" in note for note in notes))

    def test_the_budget_is_a_ceiling_and_the_cut_is_reported(self) -> None:
        cards, notes = self.ground(
            proposal(at=1.0),
            proposal(at=6.0, quote="從市場買菜回來"),
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


class AssetCardTests(unittest.TestCase):
    """A picture card can only show a picture the project owns."""

    # {what the author calls it: what the renderer opens}
    ASSETS = {
        "店門口.png": "assets/imported/aaaa-店門口.png",
        "招牌.jpg": "assets/imported/bbbb-招牌.jpg",
    }

    def ground(self, *items: dict):
        return director.ground_cards(
            list(items), SPOKEN, duration_s=24.0, budget=8,
            available_assets=self.ASSETS,
        )

    def test_a_picture_the_project_has_is_kept(self) -> None:
        cards, _ = self.ground(proposal(kind="image", payload={"asset": "店門口.png"}))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["kind"], "image")

    def test_the_plan_points_at_the_copy_the_renderer_can_open(self) -> None:
        # The model names the picture the way the author does; ingest copied
        # it under a content-addressed name. Passing the author's name
        # straight through left the plan pointing at a file that is not in
        # the project, and the renderer drew nothing without complaining.
        cards, _ = self.ground(proposal(kind="image", payload={"asset": "店門口.png"}))
        self.assertEqual(cards[0]["payload"]["asset"], self.ASSETS["店門口.png"])
        self.assertEqual(cards[0]["payload"]["name"], "店門口.png")

    def test_a_filename_the_model_invented_is_dropped(self) -> None:
        # It would render as a missing file at best, and as somebody else's
        # picture at worst.
        cards, notes = self.ground(
            proposal(kind="image", payload={"asset": "我編的.png"})
        )
        self.assertEqual(cards, [])
        self.assertTrue(any("not in this project" in note for note in notes))

    def test_a_picture_card_with_no_asset_named_is_dropped(self) -> None:
        cards, notes = self.ground(proposal(kind="image", payload={"title": "沒說是哪張"}))
        self.assertEqual(cards, [])
        self.assertTrue(any("not in this project" in note for note in notes))

    def test_a_picture_still_has_to_quote_the_moment(self) -> None:
        # Same bargain as every other card: the wording may be chosen, the
        # moment may not be invented.
        cards, notes = self.ground(
            proposal(kind="image", payload={"asset": "店門口.png"},
                     quote="這句話沒有人說過")
        )
        self.assertEqual(cards, [])
        self.assertTrue(any("nothing like that is said" in note for note in notes))

    def test_with_no_assets_a_picture_card_cannot_be_placed(self) -> None:
        cards, _ = director.ground_cards(
            [proposal(kind="image", payload={"asset": "店門口.png"})],
            SPOKEN, duration_s=24.0, budget=8, available_assets={},
        )
        self.assertEqual(cards, [])


class ProjectAssetTests(unittest.TestCase):
    """Reading the folder inventory as it is actually written."""

    def setUp(self) -> None:
        import tempfile
        self.project = Path(tempfile.mkdtemp())
        (self.project / "working").mkdir(parents=True)
        (self.project / "assets/imported").mkdir(parents=True)

    def write(self, files: list[dict], main: str = "", landed: list[dict] | None = None) -> None:
        import json
        (self.project / "working/folder_inventory.json").write_text(
            json.dumps({"files": files, "main_video_path": main}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.project / "working/asset_provenance.json").write_text(
            json.dumps({"items": landed or []}, ensure_ascii=False), encoding="utf-8"
        )
        for item in landed or []:
            (self.project / item["path"]).write_bytes(b"x")

    def test_pictures_are_read_from_the_key_the_inventory_uses(self) -> None:
        # It is "files". Reading "items" — the name most artifacts here use —
        # returned nothing and silently offered the model no pictures at all,
        # which looks identical to a folder that had none.
        self.write(
            [
                {"path": "店門口.png", "kind": "image", "sha256": "aa"},
                {"path": "說明.pdf", "kind": "document", "sha256": "bb"},
            ],
            landed=[{"sha256": "aa", "path": "assets/imported/aa-店門口.png"}],
        )
        self.assertEqual(
            director.project_assets(self.project),
            {"店門口.png": "assets/imported/aa-店門口.png"},
        )

    def test_the_footage_being_cut_is_not_offered_as_a_cutaway(self) -> None:
        self.write(
            [
                {"path": "original.mp4", "kind": "video", "sha256": "aa"},
                {"path": "廚房.mp4", "kind": "video", "sha256": "bb"},
            ],
            main="original.mp4",
            landed=[
                {"sha256": "aa", "path": "assets/imported/aa-original.mp4"},
                {"sha256": "bb", "path": "assets/imported/bb-廚房.mp4"},
            ],
        )
        self.assertEqual(
            director.project_assets(self.project),
            {"廚房.mp4": "assets/imported/bb-廚房.mp4"},
        )

    def test_a_picture_the_project_did_not_keep_is_not_offered(self) -> None:
        # Inventoried but never copied in: naming it would plan a card
        # around a file the renderer cannot open.
        self.write([{"path": "店門口.png", "kind": "image", "sha256": "aa"}], landed=[])
        self.assertEqual(director.project_assets(self.project), {})

    def test_no_inventory_means_no_pictures_rather_than_an_error(self) -> None:
        self.assertEqual(director.project_assets(self.project), {})

    def test_an_unreadable_inventory_means_no_pictures(self) -> None:
        (self.project / "working/folder_inventory.json").write_text(
            "{ not json", encoding="utf-8"
        )
        (self.project / "working/asset_provenance.json").write_text(
            "{}", encoding="utf-8"
        )
        self.assertEqual(director.project_assets(self.project), {})


if __name__ == "__main__":
    unittest.main()

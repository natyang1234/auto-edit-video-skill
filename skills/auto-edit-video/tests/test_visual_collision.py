"""What sits on top of what, once everything has been placed.

The first defect reported against a finished cut was a card parked on the
speaker's face. Placement learned to avoid the head; nothing ever looked at
the result. These tests are the looking.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import visual_collision as vc  # noqa: E402

# Instagram Reels, from the platform preset registry.
REELS_SAFE = {"top": 8, "right": 8, "bottom": 18, "left": 8}


def placed(name: str, *, x=0.5, y=0.5, width=0.6, height=0.1, start=0.0, end=4.0):
    return {
        "id": name, "kind": "image",
        "x": x, "y": y, "width": width, "height": height,
        "start": start, "end": end,
    }


class GeometryTests(unittest.TestCase):
    def test_a_rect_is_read_from_its_centre(self) -> None:
        for got, want in zip(
            vc.rect(placed("a", x=0.5, y=0.4, width=0.8, height=0.2)),
            (0.1, 0.3, 0.9, 0.5),
        ):
            self.assertAlmostEqual(got, want, places=9)

    def test_an_overlay_nobody_measured_has_no_rect(self) -> None:
        for missing in ({"x": 0.5, "y": 0.5, "width": 0.5}, {"x": 0.5, "y": 0.5}):
            with self.subTest(missing):
                self.assertIsNone(vc.rect(missing))

    def test_a_zero_sized_overlay_has_no_rect(self) -> None:
        self.assertIsNone(vc.rect(placed("a", width=0.0)))

    def test_a_non_finite_size_has_no_rect(self) -> None:
        self.assertIsNone(vc.rect(placed("a", height=float("nan"))))


class CollisionTests(unittest.TestCase):
    def test_two_cards_holding_the_same_moment_and_place_collide(self) -> None:
        findings = vc.find_collisions([
            placed("first", y=0.40), placed("second", y=0.42),
        ])
        self.assertEqual(len(findings), 1)
        self.assertIn("first", findings[0]["detail"])

    def test_the_same_place_at_different_times_does_not(self) -> None:
        self.assertEqual(
            vc.find_collisions([
                placed("first", start=0.0, end=3.0),
                placed("second", start=3.0, end=6.0),
            ]),
            [],
        )

    def test_the_same_time_in_different_places_does_not(self) -> None:
        self.assertEqual(
            vc.find_collisions([placed("card", y=0.2), placed("caption", y=0.8)]), []
        )

    def test_a_handover_between_two_cards_is_not_a_collision(self) -> None:
        # One card leaving as the next arrives shares a few frames by design.
        self.assertEqual(
            vc.find_collisions([
                placed("leaving", start=0.0, end=3.10),
                placed("arriving", start=3.0, end=6.0),
            ]),
            [],
        )

    def test_a_grazing_edge_is_not_a_collision(self) -> None:
        # Rects that touch along a sliver are not covering each other.
        self.assertEqual(
            vc.find_collisions([
                placed("card", y=0.40, height=0.10),
                placed("caption", y=0.4999, height=0.10),
            ]),
            [],
        )

    def test_an_unmeasured_overlay_cannot_be_cleared_by_accident(self) -> None:
        # It produces no finding, and `review` names it so the silence is
        # not mistaken for a clean result.
        report = vc.review([placed("card"), {"id": "photo", "start": 0, "end": 4}])
        self.assertEqual(report["collisions"], [])
        self.assertEqual(report["unmeasured"], ["overlay photo"])
        self.assertEqual(report["checked"], 1)


class OffFrameTests(unittest.TestCase):
    def test_text_running_past_the_edge_is_reported(self) -> None:
        findings = vc.find_off_frame([placed("wide", width=1.4)])
        self.assertEqual(len(findings), 1)

    def test_a_rounding_hair_over_the_edge_is_not(self) -> None:
        self.assertEqual(vc.find_off_frame([placed("full", width=1.002)]), [])

    def test_an_overlay_inside_the_frame_is_not(self) -> None:
        self.assertEqual(vc.find_off_frame([placed("card")]), [])


class SafeAreaTests(unittest.TestCase):
    def test_a_card_under_the_platform_chrome_is_reported(self) -> None:
        # Measured on a real delivery: a title card at y=0.094 with height
        # 0.057 has its top edge at 0.065, inside the 8% Reels reserves.
        findings = vc.find_safe_area_intrusions(
            [placed("title", y=0.094, width=0.77, height=0.057)], REELS_SAFE
        )
        self.assertEqual(findings[0]["sides"], ["top"])

    def test_a_full_width_caption_reaches_the_button_column(self) -> None:
        findings = vc.find_safe_area_intrusions(
            [placed("caption", y=0.72, width=0.874, height=0.058)], REELS_SAFE
        )
        self.assertEqual(findings[0]["sides"], ["left", "right"])

    def test_an_overlay_within_the_margins_is_not_reported(self) -> None:
        self.assertEqual(
            vc.find_safe_area_intrusions(
                [placed("card", y=0.45, width=0.7, height=0.1)], REELS_SAFE
            ),
            [],
        )

    def test_without_margins_nothing_is_claimed(self) -> None:
        # A platform the registry does not carry means unchecked, and the
        # report says which of the two it was.
        report = vc.review([placed("card", y=0.02, height=0.1)], None)
        self.assertEqual(report["safe_area"], [])
        self.assertFalse(report["safe_area_available"])


class WhatStopsARenderTests(unittest.TestCase):
    def test_overlays_on_top_of_each_other_stop_it(self) -> None:
        report = vc.review([placed("a", y=0.4), placed("b", y=0.41)], REELS_SAFE)
        self.assertTrue(vc.blocking(report))

    def test_text_off_the_frame_stops_it(self) -> None:
        report = vc.review([placed("wide", width=1.5)], REELS_SAFE)
        self.assertTrue(vc.blocking(report))

    def test_a_platform_margin_does_not(self) -> None:
        # Where it will be posted is a judgement, not a defect in the frame:
        # a delivery that never goes to Reels is not broken by Reels' buttons.
        report = vc.review(
            [placed("title", y=0.094, width=0.77, height=0.057)], REELS_SAFE
        )
        self.assertTrue(report["safe_area"])
        self.assertEqual(vc.blocking(report), [])


class MeasuredDeliveriesTests(unittest.TestCase):
    """Two real cuts, so the blocking rule cannot quietly reject good work."""

    def test_the_deliveries_that_shipped_are_not_condemned(self) -> None:
        # Placements taken from the 2026-08-05 lesson cut and the one-command
        # test cut: 31 overlays between them, nothing colliding, nothing off
        # frame. A rule that fails these fails everything that has shipped.
        shipped = [
            placed("visual-beat", y=0.094, width=0.770, height=0.057,
                   start=0.0, end=3.0),
            placed("caption-0001", y=0.720, width=0.500, height=0.058,
                   start=0.0, end=3.2),
            placed("caption-0003", y=0.720, width=0.874, height=0.058,
                   start=6.54, end=10.80),
        ]
        report = vc.review(shipped, REELS_SAFE)
        self.assertEqual(vc.blocking(report), [])
        self.assertEqual(report["unmeasured"], [])


if __name__ == "__main__":
    unittest.main()

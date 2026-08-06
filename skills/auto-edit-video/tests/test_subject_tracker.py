"""Following the speaker through a vertical crop."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import subject_tracker as tracker  # noqa: E402


def box(x: float, width: float = 0.1, height: float = 0.2) -> dict:
    return {"x": x, "y": 0.4, "width": width, "height": height}


class PrimarySubjectTests(unittest.TestCase):
    def test_no_boxes_means_no_subject(self) -> None:
        self.assertIsNone(tracker.primary_center_x([]))

    def test_one_box_gives_its_centre(self) -> None:
        self.assertAlmostEqual(tracker.primary_center_x([box(0.75)]), 0.80, places=5)

    def test_a_small_bystander_does_not_pull_the_frame(self) -> None:
        # The teacher fills the shot; someone's head at the edge should not
        # drag the crop halfway across the room.
        speaker = box(0.75, width=0.2, height=0.4)
        bystander = box(0.02, width=0.03, height=0.05)
        self.assertAlmostEqual(
            tracker.primary_center_x([speaker, bystander]), 0.85, places=5
        )

    def test_two_comparable_subjects_are_framed_between_them(self) -> None:
        left = box(0.20, width=0.2, height=0.4)
        right = box(0.60, width=0.2, height=0.4)
        self.assertAlmostEqual(
            tracker.primary_center_x([left, right]), 0.50, places=5
        )


class SmoothingTests(unittest.TestCase):
    WINDOW = 0.5  # a 9:16 window over 16:9 footage covers about half the width

    def test_an_empty_track_yields_no_path(self) -> None:
        self.assertEqual(tracker.smooth_track([], self.WINDOW), [])

    def test_small_wobble_does_not_move_the_window(self) -> None:
        # Detection jitters by a percent or two every frame. A window that
        # answers each one shakes, which reads worse than not tracking.
        jitter = [(index * 0.5, 0.50 + (0.01 if index % 2 else -0.01)) for index in range(10)]
        path = tracker.smooth_track(jitter, self.WINDOW)
        self.assertEqual(len({round(value, 4) for _, value in path}), 1)

    def test_a_real_move_is_followed_gradually(self) -> None:
        walk = [(index * 0.5, 0.25 if index < 3 else 0.75) for index in range(20)]
        path = tracker.smooth_track(walk, self.WINDOW)
        centres = [value for _, value in path]
        self.assertLess(centres[0], centres[-1], "the window must follow")
        steps = [abs(b - a) for a, b in zip(centres, centres[1:])]
        self.assertLessEqual(max(steps), tracker.MAX_STEP + 1e-9, "no jump cuts")

    def test_the_window_never_leaves_the_frame(self) -> None:
        extremes = [(index * 0.5, 0.0 if index % 2 else 1.0) for index in range(12)]
        for _, centre in tracker.smooth_track(extremes, self.WINDOW):
            self.assertGreaterEqual(centre, self.WINDOW / 2 - 1e-9)
            self.assertLessEqual(centre, 1.0 - self.WINDOW / 2 + 1e-9)

    def test_a_window_as_wide_as_the_frame_has_nothing_to_track(self) -> None:
        self.assertEqual(tracker.smooth_track([(0.0, 0.5)], 1.0), [])


class CropExpressionTests(unittest.TestCase):
    def test_no_path_yields_no_expression(self) -> None:
        self.assertIsNone(
            tracker.crop_x_expression([], scaled_width=3413.0, window_width=1080.0)
        )

    def test_a_still_subject_collapses_to_a_constant(self) -> None:
        path = [(index * 0.5, 0.5) for index in range(8)]
        expression = tracker.crop_x_expression(
            path, scaled_width=3413.0, window_width=1080.0
        )
        self.assertNotIn("if(", expression, "a static window needs no branches")
        self.assertEqual(int(expression), int(round(3413.0 * 0.5 - 540.0)))

    def test_the_left_edge_never_goes_out_of_bounds(self) -> None:
        path = [(0.0, 0.0), (1.0, 1.0)]
        expression = tracker.crop_x_expression(
            path, scaled_width=3413.0, window_width=1080.0
        )
        values = [int(token) for token in _numbers_after_commas(expression)]
        for value in values:
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, int(3413.0 - 1080.0))

    def test_a_window_that_already_covers_the_frame_is_not_tracked(self) -> None:
        self.assertIsNone(
            tracker.crop_x_expression(
                [(0.0, 0.5)], scaled_width=1080.0, window_width=1080.0
            )
        )


def _numbers_after_commas(expression: str) -> list[str]:
    import re

    return re.findall(r"(?<=,)(\d+)(?=[,)]|$)", expression)


class CardPlacementTests(unittest.TestCase):
    """Put the card where the speaker is not."""

    CARD = 0.06      # a hook card is about six percent of the frame
    CAPTIONS = 0.70  # captions start here

    def place(self, head_top):
        return tracker.card_y_percent(
            head_top, card_height_fraction=self.CARD,
            caption_top=self.CAPTIONS, default=46.0,
        )

    def test_room_above_the_head_is_used(self) -> None:
        y, reason = self.place(0.21)
        self.assertEqual(reason, "above_subject")
        self.assertLess(y / 100 + self.CARD / 2, 0.21, "must clear the head")

    def test_a_head_near_the_top_pushes_the_card_below_them(self) -> None:
        y, reason = self.place(0.02)
        self.assertEqual(reason, "above_captions")
        self.assertLess(y / 100 + self.CARD / 2, self.CAPTIONS, "must clear captions")
        self.assertGreater(y / 100, 0.02, "must clear the subject")

    def test_no_detection_keeps_the_fixed_position_and_says_so(self) -> None:
        # A caller that cannot tell a deliberate placement from an
        # unchanged default will report a collision as a layout choice.
        self.assertEqual(self.place(None), (46.0, "no_subject_found"))

    def test_a_card_taller_than_every_gap_falls_back(self) -> None:
        # Subject high in frame, captions high too: nowhere for a card this
        # tall to sit without landing on one of them.
        y, reason = tracker.card_y_percent(
            0.05, card_height_fraction=0.30, caption_top=0.30, default=46.0
        )
        self.assertEqual((y, reason), (46.0, "no_clear_band"))


class PlatformChromeTests(unittest.TestCase):
    """Clearing the speaker is not enough on a phone.

    A card tucked under the top edge clears the head and lands behind the
    app's own controls. Measured on a delivery on 2026-08-06: placed at
    9.22%, top edge 6.4%, inside the 8% Instagram Reels reserves.
    """

    CARD = 0.06
    RESERVED = 0.08

    def place(self, head_top, card=CARD):
        return tracker.card_y_percent(
            head_top, card_height_fraction=card, caption_top=0.70,
            default=46.0, reserved_top=self.RESERVED,
        )

    def test_a_card_that_would_sit_under_the_chrome_is_pushed_below_it(self) -> None:
        # A head 18% down leaves a narrow band, and centring the card in it
        # puts the top edge at 6% — inside the reserved 8%.
        y, reason = self.place(0.18)
        self.assertGreaterEqual(
            round(y / 100 - self.CARD / 2, 6), self.RESERVED, "top edge clears it"
        )
        self.assertTrue(reason.endswith("below_chrome"), reason)

    def test_a_card_already_clear_of_it_is_left_alone(self) -> None:
        # A head 40% down leaves a wide band; the card centres at 20% and
        # was never near the chrome. Compared against the same call with no
        # reserved band, to show the move only happens when it is demanded.
        with_chrome, reason = self.place(0.40)
        without, plain = tracker.card_y_percent(
            0.40, card_height_fraction=self.CARD, caption_top=0.70, default=46.0
        )
        self.assertEqual((with_chrome, reason), (without, plain))

    def test_no_platform_margin_behaves_exactly_as_before(self) -> None:
        for head in (0.21, 0.02, None):
            with self.subTest(head=head):
                self.assertEqual(
                    tracker.card_y_percent(
                        head, card_height_fraction=self.CARD,
                        caption_top=0.70, default=46.0, reserved_top=0.0,
                    ),
                    tracker.card_y_percent(
                        head, card_height_fraction=self.CARD,
                        caption_top=0.70, default=46.0,
                    ),
                )

    def test_the_chrome_never_pushes_the_card_onto_the_speaker(self) -> None:
        # A head 11% down: the card's top edge lands at 2.5%, inside the
        # reserved band, but moving it clear would put its bottom edge at
        # 14% — on the face. Covering the speaker is the defect this whole
        # placement path exists to avoid, and it is worse than a button.
        y, reason = self.place(0.11)
        self.assertLess(y / 100 - self.CARD / 2, self.RESERVED, "left in the band")
        self.assertLess(y / 100 + self.CARD / 2, 0.11, "and still off the face")
        self.assertEqual(reason, "above_subject")


if __name__ == "__main__":
    unittest.main()

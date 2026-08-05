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


if __name__ == "__main__":
    unittest.main()

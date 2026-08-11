from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from visual_scene_coverage import evaluate_scene_coverage  # noqa: E402


class VisualSceneCoverageTests(unittest.TestCase):
    def test_long_curated_timeline_has_bounded_aroll_breathing_and_no_long_gap(self) -> None:
        breathing = [
            {
                "id": f"breathing-{index}",
                "start": start,
                "end": end,
                "role": "a_roll_breathing",
                "major_graphic": False,
            }
            for index, (start, end) in enumerate(
                ((24.0, 32.0), (48.0, 56.0), (80.0, 88.0))
            )
        ]
        major = [
            {"id": f"scene-{index}", "start": start, "end": end, "major_graphic": True}
            for index, (start, end) in enumerate(
                (
                    (0.0, 5.5),
                    (8.0, 13.5),
                    (16.0, 21.5),
                    (32.0, 37.5),
                    (40.0, 45.5),
                    (56.0, 61.5),
                    (64.0, 72.0),
                    (72.0, 77.5),
                )
            )
        ]

        report = evaluate_scene_coverage(88.0, breathing, major)

        self.assertEqual(report["status"], "pass", report["failures"])
        self.assertEqual(report["qualifying_breathing_interval_count"], 3)
        self.assertAlmostEqual(report["breathing_share"], 24.0 / 88.0, places=6)
        self.assertEqual(report["uncovered_gaps_over_12s"], [])

    def test_long_output_cannot_claim_the_whole_timeline_as_breathing(self) -> None:
        report = evaluate_scene_coverage(
            60.0,
            [
                {
                    "id": "all-aroll",
                    "start": 0.0,
                    "end": 60.0,
                    "role": "a_roll_breathing",
                    "major_graphic": False,
                }
            ],
            [],
        )

        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("25%" in item for item in report["failures"]))
        self.assertTrue(any("at least two" in item for item in report["failures"]))

    def test_breathing_time_cannot_overlap_a_delivered_major_graphic(self) -> None:
        report = evaluate_scene_coverage(
            20.0,
            [
                {
                    "id": "aroll",
                    "start": 0.0,
                    "end": 10.0,
                    "role": "a_roll_breathing",
                    "major_graphic": False,
                }
            ],
            [{"id": "scene", "start": 5.0, "end": 15.0, "major_graphic": True}],
        )

        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("overlaps" in item for item in report["failures"]))

    def test_unexplained_visual_gap_over_twelve_seconds_fails(self) -> None:
        report = evaluate_scene_coverage(
            30.0,
            [
                {
                    "id": "opening-breath",
                    "start": 0.0,
                    "end": 2.0,
                    "role": "a_roll_breathing",
                    "major_graphic": False,
                }
            ],
            [{"id": "late-scene", "start": 20.0, "end": 25.0, "major_graphic": True}],
        )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(
            report["uncovered_gaps_over_12s"],
            [{"start": 2.0, "end": 20.0, "duration": 18.0}],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from editor_server import editor_state_revision, validate_editor_state  # noqa: E402
from highlight_planner import (  # noqa: E402
    DIRECTOR_PROFILES,
    build_highlight_plan,
    validate_highlight_plan,
)


class HighlightPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        texts = [
            "你以為流量只靠運氣嗎？",
            "其實最大的差別，是前三秒先講結論。",
            "第一步先把觀眾真正的問題寫下來。",
            "第二步用一個實際案例證明方法有效。",
            "根據這次測試，完整看完的人增加三倍。",
            "但是很多人反而先講背景，結果觀眾直接離開。",
            "我走進現場時，第一眼就看到大家盯著同一個畫面。",
            "我親手按下發布鍵，才發現真實反應和想像完全不同。",
            "所以關鍵不是更用力，而是更早給出明確答案。",
        ]
        words = []
        segments = []
        for index, text in enumerate(texts):
            start = index * 5.0
            end = start + 4.2
            word = {
                "id": f"word-{index + 1:05d}",
                "text": text,
                "start": start,
                "end": end,
                "confidence": 0.93,
                "segment_id": f"segment-{index + 1:04d}",
            }
            words.append(word)
            segments.append(
                {
                    "id": f"segment-{index + 1:04d}",
                    "start": start,
                    "end": end,
                    "text": text,
                    "words": [word],
                }
            )
        self.transcript = {
            "schema_version": 1,
            "language": "zh",
            "duration_s": 45.0,
            "text": "".join(texts),
            "segments": segments,
            "words": words,
        }
        self.manifest = {
            "schema_version": 1,
            "source": {
                "staged_path": "source/original.mp4",
                "duration_s": 45.0,
                "fps": 30,
                "sha256": "a" * 64,
            },
            "output_target": {
                "platform": "youtube-shorts",
                "duration_profile": "short",
                "min_seconds": 10,
                "target_seconds": 18,
                "max_seconds": 30,
            },
        }

    def test_plans_are_deterministic_bounded_and_profile_specific(self) -> None:
        plans = {}
        for profile in DIRECTOR_PROFILES:
            plan = build_highlight_plan(
                self.transcript,
                self.manifest,
                director_profile=profile,
                requested_count=10,
                editing_brief="保留實際案例、三倍結果與現場感",
            )
            plans[profile] = plan
            self.assertEqual(plan["status"], "needs_review")
            self.assertGreaterEqual(len(plan["items"]), 1)
            self.assertLessEqual(len(plan["items"]), 10)
            self.assertTrue(all(item["review_status"] == "pending" for item in plan["items"]))
            self.assertEqual(validate_highlight_plan(plan, 45.0), [])
            for item in plan["items"]:
                self.assertGreaterEqual(item["start"], 0)
                self.assertLessEqual(item["end"], 45.0)
                self.assertIn(item["title"], item["evidence"]["text"])
                self.assertTrue(item["evidence"]["exact_transcript_extract"])

            repeated = build_highlight_plan(
                self.transcript,
                self.manifest,
                director_profile=profile,
                requested_count=10,
                editing_brief="保留實際案例、三倍結果與現場感",
            )
            self.assertEqual(plan["plan_revision"], repeated["plan_revision"])
            self.assertEqual(plan["items"], repeated["items"])

        preferred = {
            profile: plan["configuration"]["preferred_duration_s"]
            for profile, plan in plans.items()
        }
        self.assertGreater(len(set(preferred.values())), 2)
        self.assertNotEqual(
            plans["teacher-punch"]["items"][0]["id"],
            plans["minimal"]["items"][0]["id"],
        )

    def test_missing_transcript_is_explicit_and_never_fabricates_ranges(self) -> None:
        plan = build_highlight_plan(
            {"schema_version": 1, "segments": [], "words": [], "text": ""},
            self.manifest,
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertEqual(plan["status"], "needs_transcript")
        self.assertEqual(plan["items"], [])
        self.assertIn("transcript", " ".join(plan["warnings"]).lower())

    def test_segment_only_timing_is_not_interpolated(self) -> None:
        transcript = {
            "schema_version": 1,
            "text": "第一段完整句子。第二段完整句子。",
            "segments": [
                {"id": "segment-0001", "start": 2.0, "end": 8.0, "text": "第一段完整句子。"},
                {"id": "segment-0002", "start": 9.0, "end": 15.0, "text": "第二段完整句子。"},
            ],
            "words": [],
        }
        manifest = {
            **self.manifest,
            "source": {**self.manifest["source"], "duration_s": 20.0},
            "output_target": {**self.manifest["output_target"], "min_seconds": 4, "target_seconds": 8, "max_seconds": 16},
        }
        plan = build_highlight_plan(
            transcript,
            manifest,
            director_profile="editorial-clean",
            requested_count=2,
            editing_brief="",
        )
        valid_boundaries = {2.0, 8.0, 9.0, 15.0}
        for item in plan["items"]:
            self.assertIn(item["boundary"]["raw_start"], valid_boundaries)
            self.assertIn(item["boundary"]["raw_end"], valid_boundaries)

    def test_invalid_count_and_plan_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_highlight_plan(
                self.transcript,
                self.manifest,
                director_profile="teacher-punch",
                requested_count=11,
                editing_brief="",
            )
        plan = build_highlight_plan(
            self.transcript,
            self.manifest,
            director_profile="teacher-punch",
            requested_count=1,
            editing_brief="",
        )
        plan["items"][0]["start"] = math.nan
        self.assertTrue(validate_highlight_plan(plan, 45.0))

    def test_editor_revision_and_validation_include_highlights_and_finite_values(self) -> None:
        state = {
            "schema_version": 1,
            "project_id": "test",
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "fit": "cover",
            },
            "director_style": "teacher-punch",
            "caption_defaults": {},
            "overlays": [],
            "highlight_plan_revision": "b" * 64,
            "source_sha256": "a" * 64,
            "highlights": [
                {
                    "id": "highlight-0001",
                    "start": 10.0,
                    "end": 20.0,
                    "title": "第一段",
                    "review_status": "pending",
                }
            ],
        }
        first = editor_state_revision(state)
        state["highlights"][0]["start"] = 12.0
        self.assertNotEqual(first, editor_state_revision(state))
        self.assertEqual(validate_editor_state(state, 45.0), [])

        state["highlights"][0]["start"] = math.nan
        self.assertTrue(validate_editor_state(state, 45.0))
        state["highlights"] = [
            {"id": f"highlight-{index:04d}", "start": index, "end": index + 0.5, "title": "x", "review_status": "pending"}
            for index in range(11)
        ]
        self.assertTrue(validate_editor_state(state, 45.0))
        state["highlights"] = []
        state["canvas"]["fps"] = 1_000_000_000
        self.assertTrue(validate_editor_state(state, 45.0))


if __name__ == "__main__":
    unittest.main()

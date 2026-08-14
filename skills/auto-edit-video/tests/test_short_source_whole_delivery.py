"""A source shorter than a clip is the clip — in Studio, not only in `cut`.

The defect, from a real Studio run (project
`20260814t085038z-2025-11-25-1149-img-7766-ce64b1b0-ffd4`): an 11.34 second
video transcribed into four real spoken segments covering 0.0–11.32s, and the
highlight proposal came back "轉錄完成，但沒有足夠語音內容可建立精華" with zero
clips. There was plenty of speech. What there was not, was ten contiguous
seconds of it: the two natural pauses (0.62s and 1.72s) split the transcript,
the profile's max pause is 1.3s, and the default minimum clip length for a
source with no numeric duration bounds is ten seconds. Every window was too
short, so nothing was proposed at all.

`cut` never had this problem because it collapses the plan to the whole source
whenever the source is no longer than the requested clip length. Studio's
proposal path never learned that rule. It does now, at the planner, so both
routes read the same threshold from the same constant.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import highlight_planner  # noqa: E402
import studio_server  # noqa: E402
from auto_edit import build_parser  # noqa: E402
from highlight_planner import (  # noqa: E402
    WHOLE_SOURCE_MAX_SECONDS,
    build_highlight_plan,
    validate_highlight_plan,
)


# Exactly what Whisper returned for the real 11.34s clip, with word timings
# rebuilt evenly inside each segment; the pauses between segments are what
# matter and they are the recogniser's own.
REAL_SEGMENTS: list[tuple[str, float, float, str]] = [
    ("segment-0001", 0.0, 2.02, "我總不能過太久再說一遍"),
    ("segment-0002", 2.02, 4.38, "然後他的訓室隨便"),
    ("segment-0003", 5.0, 8.46, "我是因為你米跟那個同不太可惜"),
    ("segment-0004", 10.18, 11.32, "對因為那會"),
]


def transcript_from(
    rows: list[tuple[str, float, float, str]], duration_s: float
) -> dict[str, object]:
    segments = []
    words = []
    counter = 0
    for identifier, start, end, text in rows:
        span = (end - start) / max(1, len(text))
        segment_words = []
        for index, character in enumerate(text):
            counter += 1
            word = {
                "id": f"word-{counter:05d}",
                "text": character,
                "start": round(start + index * span, 3),
                "end": round(start + (index + 1) * span, 3),
                "confidence": 0.92,
                "segment_id": identifier,
            }
            segment_words.append(word)
            words.append(word)
        segments.append(
            {
                "id": identifier,
                "start": start,
                "end": end,
                "text": text,
                "words": segment_words,
            }
        )
    return {
        "schema_version": 1,
        "language": "zh",
        "duration_s": duration_s,
        "text": "".join(row[3] for row in rows),
        "segments": segments,
        "words": words,
    }


def manifest_for(duration_s: float, *, bounds: dict[str, object] | None = None) -> dict:
    output_target = {
        "platform": "instagram-reels",
        "preset_platform": "instagram-reels",
        "duration_profile": "full",
        "selection": "full_cleanup",
        "basis": "source-duration",
        "publishing_in_scope": False,
        "min_seconds": None,
        "target_seconds": None,
        "max_seconds": None,
    }
    if bounds:
        output_target.update(bounds)
    return {
        "schema_version": 1,
        "source": {
            "staged_path": "source/original.mov",
            "duration_s": duration_s,
            "fps": 59.9401,
            "sha256": "c" * 64,
        },
        "output_target": output_target,
    }


def gappy_speech() -> list[tuple[str, float, float, str]]:
    """The defect's own shape: short spoken runs split by long pauses.

    No window survives — the runs are shorter than the default ten second
    minimum and the pauses are longer than the profile will cross — so
    windowed selection comes back empty however much talking there is.
    """
    return list(REAL_SEGMENTS)


def evenly_spoken(duration_s: float, count: int) -> list[tuple[str, float, float, str]]:
    """A transcript that keeps a long source proposing several clips."""
    texts = [
        "你以為流量只靠運氣嗎？",
        "其實最大的差別，是前三秒先講結論。",
        "第一步先把觀眾真正的問題寫下來。",
        "第二步用一個實際案例證明方法有效。",
        "根據這次測試，完整看完的人增加三倍。",
        "但是很多人反而先講背景，結果觀眾直接離開。",
        "我走進現場時，第一眼就看到大家盯著同一個畫面。",
        "所以關鍵不是更用力，而是更早給出明確答案。",
    ]
    step = duration_s / count
    rows = []
    for index in range(count):
        start = round(index * step, 3)
        rows.append(
            (
                f"segment-{index + 1:04d}",
                start,
                round(min(duration_s, start + step * 0.9), 3),
                texts[index % len(texts)],
            )
        )
    return rows


class RealStudioShortSourceTests(unittest.TestCase):
    def plan(self, director: str = "teacher-punch") -> dict:
        return build_highlight_plan(
            transcript_from(REAL_SEGMENTS, 11.34),
            manifest_for(11.34),
            director_profile=director,
            requested_count=10,
            editing_brief="",
        )

    def test_the_real_eleven_second_clip_is_proposed_whole(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["status"], "needs_review")
        self.assertEqual(len(plan["items"]), 1)
        item = plan["items"][0]
        self.assertEqual(item["start"], 0.0)
        self.assertAlmostEqual(item["end"], 11.34, places=2)
        self.assertEqual(item["review_status"], "pending")
        self.assertEqual(validate_highlight_plan(plan, 11.34), [])

    def test_every_spoken_segment_is_inside_the_proposed_clip(self) -> None:
        item = self.plan()["items"][0]
        for _, start, end, text in REAL_SEGMENTS:
            self.assertGreaterEqual(end, item["start"])
            self.assertLessEqual(start, item["end"])
            self.assertIn(text, item["evidence"]["text"])

    def test_whole_delivery_is_stated_in_the_plan_not_inferred(self) -> None:
        plan = self.plan()
        self.assertIs(plan["whole_source_delivery"], True)
        self.assertTrue(
            any("整支" in warning or "whole" in warning for warning in plan["warnings"]),
            plan["warnings"],
        )

    def test_no_director_profile_is_left_proposing_nothing(self) -> None:
        # Profiles differ in how long a pause they will cross; none of them
        # may answer a source full of speech with an empty proposal.
        for director in highlight_planner.DIRECTOR_PROFILES:
            with self.subTest(director=director):
                plan = self.plan(director)
                self.assertGreaterEqual(len(plan["items"]), 1, director)
                self.assertEqual(plan["status"], "needs_review", director)

    def test_the_plan_stays_deterministic(self) -> None:
        self.assertEqual(self.plan()["plan_revision"], self.plan()["plan_revision"])


class SilenceStillStopsTheProposalTests(unittest.TestCase):
    """The short-source rule must not invent a clip out of no speech."""

    def test_a_short_silent_source_still_reports_no_transcript(self) -> None:
        plan = build_highlight_plan(
            {"schema_version": 1, "text": "", "segments": [], "words": []},
            manifest_for(11.34),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertEqual(plan["status"], "needs_transcript")
        self.assertEqual(plan["items"], [])
        self.assertFalse(plan.get("whole_source_delivery", False))

    def test_blank_segments_are_not_speech(self) -> None:
        plan = build_highlight_plan(
            transcript_from([("segment-0001", 0.0, 8.0, "   ")], 11.34),
            manifest_for(11.34),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertEqual(plan["status"], "needs_transcript")
        self.assertEqual(plan["items"], [])

    def test_studio_still_stops_a_silent_project_before_planning(self) -> None:
        # The no_speech_detected route is upstream of the planner and must not
        # have moved: silence is still reported as silence.
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / "working").mkdir()
            (project / "working/transcript_words.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "segments": [],
                        "caption_segments": [],
                        "words": [],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(studio_server.StudioServer.transcript_has_speech(project))


class ThresholdHasTwoSidesTests(unittest.TestCase):
    def test_the_threshold_is_the_one_the_cut_command_uses(self) -> None:
        seconds = next(
            action
            for action in build_parser()._subparsers._group_actions[0]  # noqa: SLF001
            .choices["cut"]
            ._actions
            if action.dest == "seconds"
        )
        self.assertEqual(seconds.default, WHOLE_SOURCE_MAX_SECONDS)

    def unchooseable(self, duration: float) -> dict:
        return build_highlight_plan(
            transcript_from(gappy_speech(), duration),
            manifest_for(duration),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )

    def test_a_source_exactly_at_the_threshold_ships_whole(self) -> None:
        duration = WHOLE_SOURCE_MAX_SECONDS
        plan = self.unchooseable(duration)
        self.assertIs(plan["whole_source_delivery"], True)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["start"], 0.0)
        self.assertAlmostEqual(plan["items"][0]["end"], duration, places=2)

    def test_a_source_past_the_threshold_keeps_the_old_answer(self) -> None:
        # Longer than a clip means there is something to choose between, so a
        # transcript nothing can be chosen from still proposes nothing rather
        # than quietly shipping half an hour of raw footage.
        plan = self.unchooseable(WHOLE_SOURCE_MAX_SECONDS + 15.0)
        self.assertFalse(plan.get("whole_source_delivery", False))
        self.assertEqual(plan["items"], [])
        self.assertEqual(plan["status"], "needs_transcript")

    def test_a_long_source_that_can_be_cut_is_cut_as_before(self) -> None:
        duration = WHOLE_SOURCE_MAX_SECONDS + 60.0
        plan = build_highlight_plan(
            transcript_from(evenly_spoken(duration, 8), duration),
            manifest_for(duration),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertFalse(plan.get("whole_source_delivery", False))
        self.assertGreaterEqual(len(plan["items"]), 1)
        self.assertFalse(
            any(
                item["start"] == 0.0 and item["end"] >= duration - 0.05
                for item in plan["items"]
            ),
            plan["items"],
        )

    def test_a_short_source_that_can_be_cut_is_still_cut(self) -> None:
        # The rule is a floor under empty proposals, not a ceiling on
        # selection: a short source the picker can work with is untouched.
        duration = 20.0
        plan = build_highlight_plan(
            transcript_from(evenly_spoken(duration, 4), duration),
            manifest_for(duration),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertFalse(plan.get("whole_source_delivery", False))
        self.assertGreaterEqual(len(plan["items"]), 1)

    def test_an_asked_for_clip_length_is_still_honoured_on_a_short_source(self) -> None:
        # Explicit numeric bounds mean somebody asked for a length; a ten
        # second source with a four second target still gets edited.
        duration = 10.0
        plan = build_highlight_plan(
            transcript_from(evenly_spoken(duration, 4), duration),
            manifest_for(
                duration,
                bounds={"min_seconds": 2, "target_seconds": 4, "max_seconds": 6},
            ),
            director_profile="teacher-punch",
            requested_count=10,
            editing_brief="",
        )
        self.assertFalse(plan.get("whole_source_delivery", False))
        self.assertTrue(
            all(item["end"] - item["start"] <= 6.05 for item in plan["items"]),
            plan["items"],
        )


class StudioSaysWhatHappenedTests(unittest.TestCase):
    """The panel reports a whole-source delivery as a delivery, not a defect."""

    def project_with(self, plan: dict, caption_segments: int) -> Path:
        raw = tempfile.mkdtemp(prefix="auto-edit-short-source-")
        project = Path(raw)
        (project / "working").mkdir()
        (project / "working/highlight_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (project / "working/transcript_words.json").write_text(
            json.dumps(
                transcript_from(
                    evenly_spoken(11.34, caption_segments) if caption_segments else [],
                    11.34,
                )
            ),
            encoding="utf-8",
        )
        return project

    def message(self, project: Path) -> str:
        server = studio_server.StudioServer.__new__(studio_server.StudioServer)
        return studio_server.StudioServer.highlight_review_message(server, project)

    def test_a_whole_source_delivery_reads_as_delivered(self) -> None:
        message = self.message(
            self.project_with({"whole_source_delivery": True, "items": [{}]}, 4)
        )
        self.assertIn("整支交付", message)
        self.assertIn("4", message)
        for defect in ("沒有足夠", "無法", "失敗"):
            self.assertNotIn(defect, message)

    def test_the_pipeline_writes_the_delivered_message_operators_see(self) -> None:
        # The panel reads pipeline_status.json and nothing else, so the
        # wording has to arrive through the worker that writes it.
        project = self.project_with({"whole_source_delivery": True, "items": [{}]}, 4)
        server = studio_server.StudioServer.__new__(studio_server.StudioServer)
        server.stop_event = threading.Event()
        server.pipeline_lock = threading.Lock()
        server.active_pipeline_project = project
        server.run_pipeline_command = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "", ""
        )
        studio_server.StudioServer.pipeline_worker(
            server, project, "teacher-punch", ""
        )
        status = json.loads(
            (project / "working/pipeline_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["state"], "needs_review")
        self.assertIn("整支交付", status["message"])
        self.assertIn("4", status["message"])
        self.assertNotIn("沒有足夠", status["message"])

    def test_an_ordinary_plan_keeps_the_ordinary_message(self) -> None:
        message = self.message(self.project_with({"items": [{}, {}]}, 6))
        self.assertIn("精華提案", message)
        self.assertNotIn("整支交付", message)


if __name__ == "__main__":
    unittest.main()

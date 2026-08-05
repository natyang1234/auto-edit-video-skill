"""Phase 1a M3: evidence authority, frozen analysis, router and re-anchor."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
import narrative_engine  # noqa: E402
from narrative_engine import NarrativeError  # noqa: E402


def make_words() -> list[dict]:
    tokens = [
        ("我們", 0.0, 0.4), ("的", 0.4, 0.5), ("留存", 0.5, 0.9), ("是", 0.9, 1.0),
        ("87%", 1.0, 1.6), ("。", 1.6, 1.7),
        ("其實", 2.0, 2.4), ("大部分", 2.4, 2.9), ("人", 2.9, 3.0), ("都", 3.0, 3.1),
        ("做", 3.1, 3.3), ("錯", 3.3, 3.5), ("了", 3.5, 3.6), ("。", 3.6, 3.7),
        ("方法", 4.0, 4.4), ("很", 4.4, 4.5), ("簡單", 4.5, 5.0), ("。", 5.0, 5.1),
    ]
    return [
        {"id": f"w{i:04d}", "text": text, "start": start, "end": end}
        for i, (text, start, end) in enumerate(tokens)
    ]


class NarrativeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-narrative-tests-")
        self.project = Path(self._tmp.name) / "project"
        (self.project / "working").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        (self.project / "project.json").write_text(
            json.dumps(
                {
                    "project_id": "narrative-test",
                    "source": {"sha256": "a" * 64, "duration_s": 60.0},
                }
            ),
            "utf-8",
        )
        (self.project / "working/transcript_words.json").write_text(
            json.dumps({"schema_version": 1, "engine": "openai-whisper", "words": make_words()}),
            "utf-8",
        )

    def build_index(self) -> dict:
        return narrative_engine.build_evidence_index(self.project)

    def make_draft(self, evidence_map: dict, evidence_ids: list[str] | None = None) -> dict:
        if evidence_ids is None:
            evidence_ids = [evidence_map["items"][0]["id"]]
        item = lambda text: {"text": text, "evidence_ids": list(evidence_ids)}  # noqa: E731
        return {
            "truth_map": {
                "main_topics": [item("留存策略")],
                "target_audience": item("內容創作者"),
                "promises": [item("學會留存方法")],
                "methods": [item("方法很簡單")],
                "proofs": [item("留存 87%")],
                "stories": [item("大部分人做錯")],
                "cta": [item("留言索取")],
            },
            "retention_risks": [],
            "idea_candidates": [
                {
                    "id": "idea-abcdef01",
                    "thesis": "其實大部分人的留存方法都做錯了",
                    "source_ranges": [{"start": 0.0, "end": 5.1}],
                    "payoff": "一個能立即用的留存方法",
                    "evidence_ids": list(evidence_ids),
                }
            ],
        }

    def freeze(self, draft: dict) -> dict:
        return narrative_engine.freeze_content_analysis(
            self.project, draft, "claude-agent", "content-analysis-guide-v1",
            "2026-08-04T08:00:00",
        )

    def test_index_is_deterministic_and_typed(self) -> None:
        first = self.build_index()
        second = self.build_index()
        self.assertEqual(first["revision"], second["revision"])
        kinds = {item["kind"] for item in first["items"]}
        self.assertEqual(kinds, {"quote", "number"})
        number = next(item for item in first["items"] if item["kind"] == "number")
        self.assertEqual(number["literal"], "87%")
        self.assertEqual(contract_registry.validate_artifact("evidence_map", first), [])

    def test_an_unpunctuated_transcript_still_yields_one_quote_per_line(self) -> None:
        # Breeze — the recogniser zh-TW projects use — emits no punctuation.
        # Sentence-break-only splitting turned the whole clip into one quote,
        # and every card built from it read as a run-on cut off mid-word.
        words = [
            {"id": "word-1", "text": "今晚", "start": 0.0, "end": 0.9,
             "segment_id": "segment-0001"},
            {"id": "word-2", "text": "別宅在家", "start": 0.9, "end": 2.0,
             "segment_id": "segment-0001"},
            {"id": "word-3", "text": "忠孝復興", "start": 3.9, "end": 4.8,
             "segment_id": "segment-0002"},
            {"id": "word-4", "text": "四號出口", "start": 4.8, "end": 5.9,
             "segment_id": "segment-0002"},
        ]
        quotes = [
            item
            for item in narrative_engine.derive_evidence_items(words)
            if item["kind"] == "quote"
        ]
        self.assertEqual(
            [item["literal"] for item in quotes],
            ["今晚別宅在家", "忠孝復興四號出口"],
        )

    def test_tampered_evidence_map_is_rejected(self) -> None:
        evidence_map = self.build_index()
        evidence_map["items"][0]["literal"] = "留存是 99.9%（捏造）"
        (self.project / "working/evidence_map.json").write_text(
            json.dumps(evidence_map, ensure_ascii=False), "utf-8"
        )
        with self.assertRaises(NarrativeError):
            narrative_engine.verify_evidence_map(self.project)

    def test_freeze_rejects_unknown_evidence_and_out_of_range(self) -> None:
        evidence_map = self.build_index()
        draft = self.make_draft(evidence_map, ["evidence-deadbeef0000"])
        with self.assertRaises(NarrativeError):
            self.freeze(draft)
        draft = self.make_draft(evidence_map)
        draft["idea_candidates"][0]["source_ranges"] = [{"start": 0.0, "end": 999.0}]
        with self.assertRaises(NarrativeError):
            self.freeze(draft)

    def test_freeze_router_plan_determinism_and_low_risk(self) -> None:
        evidence_map = self.build_index()
        number_id = next(i["id"] for i in evidence_map["items"] if i["kind"] == "number")
        quote_id = next(i["id"] for i in evidence_map["items"] if i["kind"] == "quote")
        draft = self.make_draft(evidence_map, [number_id, quote_id])
        frozen = self.freeze(draft)
        self.assertTrue(frozen["frozen"])

        structure_a = narrative_engine.route_formulas(self.project)
        structure_b = narrative_engine.route_formulas(self.project)
        self.assertEqual(structure_a["plan_hash"], structure_b["plan_hash"])
        self.assertEqual(
            contract_registry.validate_artifact("viral_structure_plan", structure_a), []
        )
        self.assertEqual(structure_a["selected"]["idea_id"], "idea-abcdef01")

        plan = narrative_engine.build_narrative_plan(self.project)
        self.assertFalse(plan["reorder"])
        self.assertEqual(plan["risk"], "low")
        starts = [segment["source_start"] for segment in plan["segments"]]
        self.assertEqual(starts, sorted(starts), "low-risk plan must keep source order")
        self.assertEqual(
            contract_registry.validate_artifact("narrative_edit_plan", plan), []
        )

    def test_policy_change_changes_plan_hash(self) -> None:
        evidence_map = self.build_index()
        number_id = next(i["id"] for i in evidence_map["items"] if i["kind"] == "number")
        self.freeze(self.make_draft(evidence_map, [number_id]))
        baseline = narrative_engine.route_formulas(self.project)

        policy = json.loads(narrative_engine.POLICY_PATH.read_text("utf-8"))
        policy["policy_version"] = "p2-test"
        altered = Path(self._tmp.name) / "policy.json"
        altered.write_text(json.dumps(policy, ensure_ascii=False), "utf-8")
        with patch.object(narrative_engine, "POLICY_PATH", altered):
            changed = narrative_engine.route_formulas(self.project)
        self.assertNotEqual(baseline["plan_hash"], changed["plan_hash"])
        self.assertNotEqual(baseline["policy_hash"], changed["policy_hash"])

    def test_integrity_gate_blocks_evidence_free_candidates(self) -> None:
        evidence_map = self.build_index()
        draft = self.make_draft(evidence_map)
        draft["idea_candidates"][0]["evidence_ids"] = []
        draft["truth_map"] = {
            key: (
                {"text": value["text"], "evidence_ids": []}
                if isinstance(value, dict) and "text" in value
                else [{"text": entry["text"], "evidence_ids": []} for entry in value]
            )
            for key, value in draft["truth_map"].items()
        }
        self.freeze(draft)
        with self.assertRaises(NarrativeError):
            narrative_engine.route_formulas(self.project)

    def test_reanchor_states(self) -> None:
        evidence_map = self.build_index()
        number_id = next(i["id"] for i in evidence_map["items"] if i["kind"] == "number")
        self.freeze(self.make_draft(evidence_map, [number_id]))
        narrative_engine.route_formulas(self.project)
        narrative_engine.build_narrative_plan(self.project)

        good = {"words": [{"text": "留存", "start": 0, "end": 1}, {"text": "87%", "start": 1, "end": 2}]}
        plan = narrative_engine.reanchor(self.project, good)
        self.assertEqual(plan["reanchor"]["status"], "anchored")

        missing = {"words": [{"text": "完全", "start": 0, "end": 1}, {"text": "無關", "start": 1, "end": 2}]}
        plan = narrative_engine.reanchor(self.project, missing)
        self.assertEqual(plan["reanchor"]["status"], "stale")

        plan = narrative_engine.reanchor(self.project, {"words": []})
        self.assertEqual(plan["reanchor"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

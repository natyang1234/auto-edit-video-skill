"""Phase 1c P1: static structured-card compositor."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
import structured_card_compositor as scc  # noqa: E402

H = "a" * 64


def make_layers() -> dict:
    return {
        "schema_version": 1,
        "items": [
            {
                "id": "structured-layer-11111111",
                "visual_plan_item_id": "visual-beat-11111111",
                "type": "title", "revision": 1, "evidence_revision": H,
                "payload": {"title_kind": "section", "title": "It 虛主詞",
                            "kicker": "GRAMMAR", "subtitle": "有虛就有真"},
                "review_status": "pending",
            },
            {
                "id": "structured-layer-22222222",
                "visual_plan_item_id": "visual-beat-22222222",
                "type": "stat", "revision": 1, "evidence_revision": H,
                "payload": {"value": "87%", "label": "留存率",
                            "evidence_id": "evidence-abcdef01",
                            "source_literal": "我們的留存是87%"},
                "review_status": "pending",
            },
            {
                "id": "structured-layer-33333333",
                "visual_plan_item_id": "visual-beat-33333333",
                "type": "chart", "revision": 1, "evidence_revision": H,
                "payload": {"chart_kind": "bar", "datums": [
                    {"label": "Q1", "value": 40, "evidence_id": "e", "source_literal": "s"},
                    {"label": "Q2", "value": -15, "evidence_id": "e", "source_literal": "s"},
                    {"label": "Q3", "value": 87, "evidence_id": "e", "source_literal": "s"},
                ]},
                "review_status": "pending",
            },
            {
                "id": "structured-layer-44444444",
                "visual_plan_item_id": "visual-beat-44444444",
                "type": "dynamic_list", "revision": 1, "evidence_revision": H,
                "payload": {"items": [
                    {"text": "看到 It 想到 to V", "conceptual": True},
                    {"text": "真主詞在後面", "conceptual": True},
                ]},
                "review_status": "pending",
            },
        ],
    }


@unittest.skipUnless(scc.compositor_available(), "needs macOS CoreText")
class StructuredCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="structured-cards-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.pack = json.loads(
            (SKILL_DIR / "contracts/instances/style_pack__dark_data_presenter.json")
            .read_text("utf-8")
        )
        self.state = {"canvas": {"width": 1080, "height": 1920},
                      "director_style": "teacher-punch"}

    def test_all_four_types_render_and_validate(self) -> None:
        index = scc.build_structured_artifacts(
            self.project, self.state, make_layers(), self.pack
        )
        self.assertEqual(
            contract_registry.validate_artifact("structured_layer_artifacts", index), []
        )
        self.assertEqual(len(index["items"]), 4)
        for item in index["items"]:
            png = self.project / item["artifact_id"]
            self.assertTrue(png.is_file())
            self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(item["compiler_version"], scc.COMPILER_VERSION)

    def test_cache_is_stable_and_payload_change_rerenders(self) -> None:
        layers = make_layers()
        first = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        second = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        self.assertEqual(
            contract_registry.canonical_hash(first),
            contract_registry.canonical_hash(second),
        )
        layers["items"][1]["payload"]["value"] = "92%"
        layers["items"][1]["revision"] = 2
        third = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        stat_before = next(i for i in first["items"] if i["layer_id"].endswith("22222222"))
        stat_after = next(i for i in third["items"] if i["layer_id"].endswith("22222222"))
        self.assertNotEqual(stat_before["structured_layer_hash"], stat_after["structured_layer_hash"])
        self.assertNotEqual(stat_before["artifact_hash"], stat_after["artifact_hash"])

    def test_pack_change_makes_artifacts_stale(self) -> None:
        layers = make_layers()
        first = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        repainted = json.loads(json.dumps(self.pack))
        repainted["tokens"]["palette"]["accent"] = "#00CC88"
        second = scc.build_structured_artifacts(self.project, self.state, layers, repainted)
        self.assertNotEqual(
            first["items"][0]["style_pack_hash"], second["items"][0]["style_pack_hash"]
        )

    def test_label_overflow_fails_closed(self) -> None:
        layers = make_layers()
        layers["items"][2]["payload"]["datums"][0]["label"] = "超長標籤" * 30
        with self.assertRaises(ValueError):
            scc.build_structured_artifacts(self.project, self.state, layers, self.pack)

    def test_tampered_card_artifact_rerenders(self) -> None:
        layers = make_layers()
        first = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        target = self.project / first["items"][0]["artifact_id"]
        target.write_bytes(b"\x89PNG\r\n\x1a\ntampered")
        second = scc.build_structured_artifacts(self.project, self.state, layers, self.pack)
        import hashlib

        fresh = self.project / second["items"][0]["artifact_id"]
        self.assertEqual(
            hashlib.sha256(fresh.read_bytes()).hexdigest(),
            second["items"][0]["artifact_hash"],
        )


if __name__ == "__main__":
    unittest.main()

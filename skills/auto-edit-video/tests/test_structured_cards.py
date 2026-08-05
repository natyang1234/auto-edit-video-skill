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

    def test_every_pack_component_renders_and_differs(self) -> None:
        # The pack declares how each component looks; a component that draws
        # the same pixels as another is not being honoured.
        cases = [
            ("title", "prompt-card", {"title": "資料會說話", "kicker": "PROMPT"}),
            ("title", "kinetic-title", {"title": "資料會說話"}),
            ("title", "title-lockup", {"title": "資料會說話"}),
            ("stat", "hero-stat", {"value": "87%", "label": "完成率"}),
            ("stat", "progress", {"value": "87%", "label": "完成率", "ratio": 0.87}),
            ("chart", "dashboard",
             {"chart_kind": "bar", "datums": [{"label": "一", "value": 3}]}),
            ("dynamic_list", "dynamic-list", {"items": [{"text": "甲"}, {"text": "乙"}]}),
            ("dynamic_list", "warning-checklist",
             {"items": [{"text": "甲"}, {"text": "乙", "severity": "warning"}]}),
            ("dynamic_list", "carousel-grid",
             {"items": [{"text": "甲"}, {"text": "乙"}, {"text": "丙"}, {"text": "丁"}]}),
            ("dynamic_list", "calendar-reveal",
             {"items": [{"text": "8/1"}, {"text": "8/2"}, {"text": "8/3"}, {"text": "8/4"}]}),
        ]
        digests: dict[str, str] = {}
        for layer_type, component_id, payload in cases:
            with self.subTest(component_id):
                layer = {
                    "id": f"layer-{component_id}",
                    "type": layer_type,
                    "component_id": component_id,
                    "payload": payload,
                }
                _path, digest, width, height = scc.render_card(
                    self.project, layer, self.pack, self.state["canvas"], 1.0
                )
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                digests[component_id] = digest
        for group in (
            ("prompt-card", "kinetic-title", "title-lockup"),
            ("hero-stat", "progress"),
            ("dynamic-list", "warning-checklist", "carousel-grid", "calendar-reveal"),
        ):
            rendered = {digests[name] for name in group}
            self.assertEqual(
                len(rendered), len(group), f"{group} must not render identically"
            )

    def test_component_must_be_able_to_render_the_item(self) -> None:
        # The type says what the data is; a component that cannot draw that
        # payload is a configuration error, not a fallback.
        for layer_type, component_id in (
            ("title", "progress"),
            ("stat", "kinetic-title"),
            ("chart", "dynamic-list"),
            ("title", "no-such-component"),
        ):
            with self.subTest(f"{layer_type}/{component_id}"):
                with self.assertRaises(ValueError):
                    scc.resolve_component(self.pack, layer_type, component_id)
        # Absent means the type's default, which every type must have.
        for layer_type in ("title", "stat", "chart", "dynamic_list"):
            self.assertIn(
                scc.resolve_component(self.pack, layer_type, None)["kind"],
                scc.COMPONENTS_BY_TYPE[layer_type],
            )

    def test_progress_component_requires_a_ratio(self) -> None:
        layer = {
            "id": "layer-progress",
            "type": "stat",
            "component_id": "progress",
            "payload": {"value": "87%", "label": "完成率"},
        }
        with self.assertRaises(ValueError):
            scc.render_card(self.project, layer, self.pack, self.state["canvas"], 1.0)

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


class HookCardShapeTests(unittest.TestCase):
    """A full-screen hook is not a lower-third band."""

    def setUp(self) -> None:
        if not scc.compositor_available():
            self.skipTest("CoreText compositor unavailable")
        self.pack = scc.load_default_pack()
        self.project = Path(tempfile.mkdtemp())

    def card(self, title: str, title_kind: str) -> tuple[int, int]:
        _rel, _digest, width, height = scc.render_card(
            self.project,
            {
                "id": f"structured-layer-{abs(hash(title_kind)) % 10**12:012x}",
                "type": "title",
                "payload": {"title": title, "title_kind": title_kind},
            },
            self.pack,
            {"width": 1080, "height": 1920},
            0.5,
        )
        return width, height

    def test_a_hook_card_is_sized_to_its_words(self) -> None:
        # The width used to be a fixed share of the canvas, so a six-word
        # title sat in a slab built for a sentence, with its text in the
        # left corner and the rest empty.
        short, _ = self.card("台北今晚約會路線", "full-screen-hook")
        canvas_share = int(1080 * 0.5 * 0.84)
        self.assertLess(short, canvas_share)

    def test_a_longer_hook_still_stops_at_the_canvas_share(self) -> None:
        wide, _ = self.card(
            "今晚別宅在家跟我走忠孝復興四號出口旁邊巷子右手邊小門", "full-screen-hook"
        )
        self.assertLessEqual(wide, int(1080 * 0.5 * 0.84))

    def test_a_lower_third_keeps_the_band_width(self) -> None:
        band, _ = self.card("台北今晚約會路線", "lower-third")
        self.assertEqual(band, int(1080 * 0.5 * 0.84))


class ReferenceStyleCardTests(unittest.TestCase):
    """The three looks the reference footage uses."""

    def setUp(self) -> None:
        if not scc.compositor_available():
            self.skipTest("CoreText compositor unavailable")
        self.pack = scc.load_default_pack()
        self.project = Path(tempfile.mkdtemp())

    def card(self, kind: str, payload: dict) -> tuple[int, int]:
        _rel, _digest, width, height = scc.render_card(
            self.project,
            {"id": f"structured-layer-{kind}0001", "type": kind, "payload": payload},
            self.pack,
            {"width": 1080, "height": 1920},
            0.5,
        )
        return width, height

    def test_a_note_card_draws_icon_title_and_meta(self) -> None:
        width, height = self.card(
            "note", {"icon": "🎙", "title": "採訪自己", "meta": "21:47"}
        )
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)

    def test_a_note_with_a_waveform_is_taller_than_one_without(self) -> None:
        plain = self.card("note", {"title": "採訪自己"})[1]
        recorded = self.card("note", {"title": "採訪自己", "waveform": True})[1]
        self.assertGreater(recorded, plain)

    def test_a_chip_is_sized_to_its_line(self) -> None:
        # A chip stretched to a fixed width stops reading as a chip.
        short = self.card("chip", {"text": "這間"})[0]
        long = self.card("chip", {"text": "🥐 → 🎙 → 這支 Reel 很長很長"})[0]
        self.assertLess(short, long)
        self.assertLess(long, int(1080 * 0.5 * 0.84))

    def test_a_statement_leaves_room_for_its_number(self) -> None:
        without = self.card("statement", {"text": "批量發布安排"})[0]
        with_lead = self.card("statement", {"lead": "03", "text": "批量發布安排"})[0]
        self.assertGreater(with_lead, without)

    def test_a_chip_with_no_text_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.card("chip", {})

    def test_a_statement_with_no_text_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.card("statement", {"lead": "03"})

    def test_a_component_cannot_render_a_type_it_does_not_fit(self) -> None:
        with self.assertRaises(ValueError):
            scc.resolve_component(self.pack, "chip", "hero-stat")

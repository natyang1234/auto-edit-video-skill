from __future__ import annotations

import html
import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from graphic_package import (  # noqa: E402
    build_composition_html,
    package_captions,
    package_cards,
)
from visual_quality import (  # noqa: E402
    build_highlight_design_overlays,
    overlays_for_clip,
    visual_quality_report,
)


class HighlightVisualPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.highlight = {
            "id": "highlight-teaching",
            "start": 4.0,
            "end": 33.0,
            "title": "It 作虛主詞：看到 It，想到 to V",
            "review_status": "approved",
        }
        self.transcript = {
            "segments": [
                {"id": "s1", "start": 4.2, "end": 7.0, "text": "這一課要學 It 作虛主詞"},
                {"id": "s2", "start": 7.0, "end": 11.0, "text": "It 代替後面的不定詞片語"},
                {"id": "s3", "start": 11.0, "end": 16.0, "text": "看到 It 就想到 to V"},
                {"id": "s4", "start": 16.0, "end": 21.0, "text": "有虛主詞就有真正的主詞"},
                {"id": "s5", "start": 21.0, "end": 26.0, "text": "真正的主詞放在句子後面"},
                {"id": "s6", "start": 26.0, "end": 30.0, "text": "這是最重要的意思"},
                {"id": "s7", "start": 30.0, "end": 32.8, "text": "接著直接看例句"},
            ]
        }
        self.caption_style = {
            "font_family": "PingFang TC",
            "font_size": 68,
            "font_weight": 900,
            "color": "#fff5e6",
            "emphasis_color": "#ffb000",
            "stroke_color": "#17110d",
            "stroke_width": 6,
            "x": 50,
            "y": 72,
            "max_width": 90,
            "animation": "slide-up",
        }

    def test_generates_five_editable_cards_inside_selected_highlight(self) -> None:
        overlays = build_highlight_design_overlays(
            self.transcript,
            self.highlight,
            self.caption_style,
            "high-energy",
        )
        self.assertEqual(len(overlays), 5)
        self.assertEqual(
            {item["design_role"] for item in overlays},
            {"hook", "concept", "rule", "memory", "recap"},
        )
        self.assertTrue(all(item["highlight_id"] == self.highlight["id"] for item in overlays))
        self.assertTrue(all(item["start"] >= self.highlight["start"] for item in overlays))
        self.assertTrue(all(item["end"] <= self.highlight["end"] for item in overlays))
        self.assertEqual(overlays[0]["type"], "title")
        self.assertIn("to V", overlays[0]["text"])

    def test_visual_quality_rejects_subtitle_only_false_green(self) -> None:
        state = {
            "visual_quality_mode": "designed",
            "canvas": {"width": 1080, "height": 1920, "fit": "cover"},
            "overlays": [
                {
                    "id": "caption-1",
                    "type": "caption",
                    "start": 4.0,
                    "end": 33.0,
                    "text": "只有字幕",
                    "visible": True,
                },
                {
                    "id": "title-1",
                    "type": "title",
                    "start": 4.1,
                    "end": 7.0,
                    "text": "單一標題",
                    "visible": True,
                },
            ],
        }
        manifest = {
            "source": {"duration_s": 40.0, "width": 1920, "height": 1080},
        }
        report = visual_quality_report(state, manifest, self.highlight)
        self.assertEqual(report["status"], "fail")
        self.assertLess(report["designed_card_count"], 5)
        self.assertTrue(any("five" in error for error in report["failures"]))
        self.assertTrue(any("contain" in error for error in report["failures"]))

    def test_five_scoped_cards_and_safe_fit_pass_visual_contract(self) -> None:
        design = build_highlight_design_overlays(
            self.transcript,
            self.highlight,
            self.caption_style,
            "high-energy",
        )
        captions = [
            {
                "id": f"caption-{index}",
                "type": "caption",
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"],
                "visible": True,
            }
            for index, segment in enumerate(self.transcript["segments"], start=1)
        ]
        state = {
            "visual_quality_mode": "designed",
            "canvas": {"width": 1080, "height": 1920, "fit": "contain"},
            "overlays": captions + design,
        }
        manifest = {
            "source": {"duration_s": 40.0, "width": 1920, "height": 1080},
        }
        report = visual_quality_report(state, manifest, self.highlight)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["designed_card_count"], 5)
        self.assertEqual(
            report["designed_roles"],
            ["concept", "hook", "memory", "recap", "rule"],
        )
        self.assertGreaterEqual(report["designed_coverage_ratio"], 0.35)
        self.assertGreaterEqual(len(report["designed_types"]), 3)

    def test_duplicate_cards_cannot_replace_the_five_required_roles(self) -> None:
        design = build_highlight_design_overlays(
            self.transcript,
            self.highlight,
            self.caption_style,
            "high-energy",
        )
        duplicated = [dict(design[0], id=f"duplicate-{index}") for index in range(5)]
        state = {
            "visual_quality_mode": "designed",
            "canvas": {"width": 1080, "height": 1920, "fit": "contain"},
            "overlays": duplicated,
        }
        report = visual_quality_report(
            state,
            {"source": {"duration_s": 40.0, "width": 1920, "height": 1080}},
            self.highlight,
        )
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("missing required card roles" in item for item in report["failures"]))

    def test_clip_filter_excludes_other_highlight_design_layers(self) -> None:
        own = build_highlight_design_overlays(
            self.transcript,
            self.highlight,
            self.caption_style,
            "high-energy",
        )
        other = dict(own[0])
        other.update({"id": "other-card", "highlight_id": "highlight-other"})
        state = {"overlays": own + [other]}
        active = overlays_for_clip(state, self.highlight)
        self.assertEqual(len(active), 5)
        self.assertNotIn("other-card", {item["id"] for item in active})


class GraphicPackageTemplateTests(unittest.TestCase):
    def test_package_uses_five_cards_and_escapes_transcript_text(self) -> None:
        clip = {
            "id": "highlight-x",
            "start": 10.0,
            "end": 39.0,
            "title": "安全 <script>alert(1)</script>",
        }
        overlays = []
        roles = ("hook", "concept", "rule", "memory", "recap")
        for index, role in enumerate(roles):
            overlays.append(
                {
                    "id": f"design-{index}",
                    "type": "title" if index == 0 else "card",
                    "highlight_id": clip["id"],
                    "design_role": role,
                    "kicker": f"Kicker {index}",
                    "text": clip["title"] if index == 0 else f"內容 {index}",
                    "start": 10.0 + index * 5.0,
                    "end": min(39.0, 13.0 + index * 5.0),
                    "visible": True,
                }
            )
        state = {"director_style": "high-energy", "overlays": overlays}
        cards = package_cards(state, clip)
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0]["start"], 0.0)
        document = build_composition_html(cards, 29.0, "high-energy")
        self.assertEqual(document.count('class="card-host clip"'), 5)
        self.assertIn('window.__timelines["talking-head-recut"]', document)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn(html.escape("安全 <script>alert(1)</script>"), document)
        self.assertNotIn("https://", document)

    def test_package_brand_is_derived_and_escaped_instead_of_hardcoded(self) -> None:
        cards = []
        for index, role in enumerate(("hook", "concept", "rule", "memory", "recap")):
            cards.append(
                {
                    "id": f"card-{index}",
                    "role": role,
                    "start": index * 2.0,
                    "end": index * 2.0 + 1.5,
                    "kicker": "重點",
                    "text": "內容",
                    "detail": "說明",
                }
            )
        document = build_composition_html(cards, 12.0, "high-energy", "品牌 <測試>")
        self.assertIn("品牌 &lt;測試&gt; · QUICK CLASS", document)
        self.assertIn("品牌 &lt;測試&gt; · 精華片段", document)
        self.assertNotIn("CRYSTAL", document)

    def test_package_preserves_editable_card_layout(self) -> None:
        clip = {"id": "highlight-x", "start": 10.0, "end": 39.0}
        overlays = []
        for index, role in enumerate(("hook", "concept", "rule", "memory", "recap")):
            overlays.append(
                {
                    "id": f"design-{index}",
                    "type": "title" if role == "hook" else "card",
                    "highlight_id": clip["id"],
                    "design_role": role,
                    "text": f"內容 {index}",
                    "start": 10.0 + index * 5.0,
                    "end": min(39.0, 13.0 + index * 5.0),
                    "visible": True,
                    "layout": {"x": 42, "y": 31, "width": 70, "height": 24},
                }
            )
        cards = package_cards({"overlays": overlays}, clip)
        self.assertEqual(
            cards[1]["bounds"],
            {"x": 76, "y": 365, "width": 756, "height": 461},
        )
        document = build_composition_html(cards, 29.0, "high-energy")
        self.assertIn("left:76px;top:365px;width:756px;height:461px", document)

    def test_caption_effect_spans_and_position_are_baked_into_package(self) -> None:
        clip = {"id": "highlight-x", "start": 10.0, "end": 39.0}
        caption = {
            "id": "caption-1",
            "type": "caption",
            "highlight_id": clip["id"],
            "start": 11.0,
            "end": 13.5,
            "text": "看到 It 就想到 to V",
            "visible": True,
            "style": {
                "x": 48,
                "y": 74,
                "max_width": 76,
                "font_size": 70,
                "color": "#fff5e6",
                "stroke_color": "#17110d",
                "stroke_width": 6,
                "animation": "slide-up",
            },
            "effect_spans": [
                {
                    "id": "fx-it",
                    "text": "It",
                    "start_char": 3,
                    "end_char": 5,
                    "style": {
                        "effect": "pop",
                        "color": "#ffb000",
                        "font_scale": 1.2,
                    },
                }
            ],
        }
        captions = package_captions({"overlays": [caption]}, clip)
        self.assertEqual(captions[0]["style"]["x"], 48)
        self.assertEqual(captions[0]["effect_spans"][0]["text"], "It")

        cards = []
        for index, role in enumerate(("hook", "concept", "rule", "memory", "recap")):
            cards.append(
                {
                    "id": f"card-{index}",
                    "role": role,
                    "start": index * 5.0,
                    "end": index * 5.0 + 3.0,
                    "kicker": "重點",
                    "text": "內容",
                    "detail": "說明",
                }
            )
        document = build_composition_html(cards, 29.0, "high-energy", captions=captions)
        self.assertIn('class="caption-host clip motion-slide-up"', document)
        self.assertIn('data-effect="pop"', document)
        self.assertIn('left:48.000%;top:74.000%;max-width:76.000%', document)
        self.assertIn("看到 ", document)
        self.assertIn(">It</span>", document)

    def test_full_screen_hook_replaces_caption_instead_of_overlapping_it(self) -> None:
        clip = {"id": "highlight-x", "start": 10.0, "end": 20.0}
        state = {
            "overlays": [
                {
                    "id": "hook-card",
                    "type": "title",
                    "design_role": "hook",
                    "highlight_id": clip["id"],
                    "start": 10.0,
                    "end": 13.0,
                    "text": "本段重點",
                    "visible": True,
                    "layout": {"x": 50, "y": 50, "width": 100, "height": 100},
                },
                {
                    "id": "caption-1",
                    "type": "caption",
                    "highlight_id": clip["id"],
                    "start": 11.0,
                    "end": 15.0,
                    "text": "完整字幕",
                    "visible": True,
                    "style": {"x": 50, "y": 78, "max_width": 84},
                },
            ]
        }
        captions = package_captions(state, clip)
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0]["start"], 3.0)
        self.assertEqual(captions[0]["end"], 5.0)


if __name__ == "__main__":
    unittest.main()

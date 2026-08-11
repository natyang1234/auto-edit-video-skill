from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import asset_registry  # noqa: E402
from render_editor_timeline import (  # noqa: E402
    build_render_command,
    font_path,
    project_font_binding,
    render_project,
)


class ProjectFontResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="renderer-font-")
        self.project = Path(self.temp.name)
        (self.project / "assets/fonts").mkdir(parents=True)
        for suffix, payload in (("a", b"font-a"), ("b", b"font-b")):
            (self.project / f"assets/fonts/{suffix}.ttf").write_bytes(payload)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_asset_id_selects_exact_same_family_file(self) -> None:
        ids = {
            "font-google-fonts-0123456789abcdef-0123456789abcdef": "a",
            "font-google-fonts-fedcba9876543210-fedcba9876543210": "b",
        }

        def resolve(_project: Path, asset_id: str, required_text: str = "") -> dict:
            suffix = ids[asset_id]
            payload = (self.project / f"assets/fonts/{suffix}.ttf").read_bytes()
            return {
                "asset_id": asset_id,
                "path": f"assets/fonts/{suffix}.ttf",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "family": "Same Family",
            }

        with patch.object(asset_registry, "resolve_project_font", side_effect=resolve):
            first = project_font_binding(self.project, font_asset_id=next(iter(ids)), required_text="甲")
            second = project_font_binding(self.project, font_asset_id=list(ids)[1], required_text="乙")
        self.assertEqual(first["path"].name, "a.ttf")
        self.assertEqual(second["path"].name, "b.ttf")
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_selected_missing_font_never_uses_legacy_fallback(self) -> None:
        asset_id = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        with patch.object(
            asset_registry,
            "resolve_project_font",
            side_effect=asset_registry.AssetRegistryError("receipt missing"),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                font_path(self.project, {"caption_defaults": {"font_asset_id": asset_id}})


class CardMotionTests(unittest.TestCase):
    """Cards arrive the way their component says they should."""

    def setUp(self) -> None:
        import structured_card_compositor

        self.pack = structured_card_compositor.load_default_pack()
        self.layers = {
            "items": [
                {"id": "L-progress", "type": "stat", "component_id": "progress"},
                {"id": "L-carousel", "type": "dynamic_list", "component_id": "carousel-grid"},
                {"id": "L-lockup", "type": "title", "component_id": "title-lockup"},
            ]
        }

    def test_each_component_gets_the_motion_its_pack_declares(self) -> None:
        from render_editor_timeline import motion_for_layer

        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-progress"), "slide-in")
        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-carousel"), "pan")
        self.assertEqual(motion_for_layer(self.pack, self.layers, "L-lockup"), "slide-up")

    def test_an_unknown_layer_still_renders(self) -> None:
        from render_editor_timeline import motion_for_layer

        self.assertEqual(motion_for_layer(self.pack, self.layers, "nope"), "fade")

    def test_content_animations_are_approximated_and_marked_as_such(self) -> None:
        # Digits counting up or a page turning cannot be done by moving a
        # finished image; those take the nearest entrance and are not claimed
        # to be faithful.
        from render_editor_timeline import resolve_motion

        for preset in ("count-up", "word-cascade", "staggered-reveal", "flip", "fill"):
            with self.subTest(preset):
                animation, faithful = resolve_motion(preset)
                self.assertIn(animation, {"fade", "pop", "slide-in", "slide-up", "pan"})
                self.assertFalse(faithful)
        for preset in ("slide-in", "slide-up", "pan", "check-pop"):
            with self.subTest(preset):
                self.assertTrue(resolve_motion(preset)[1])

    def test_horizontal_motion_reaches_the_filter(self) -> None:
        from render_editor_timeline import image_filter

        overlay = {
            "id": "o1",
            "type": "image",
            "source": "card.png",
            "start": 1.0,
            "end": 4.0,
            "style": {"width": 84.0, "x": 50, "y": 46, "animation": "slide-in"},
        }
        built = image_filter("in", "out", "asset", overlay, 1080, 1920)
        self.assertIn("overlay=x=", built)
        self.assertIn("if(lt(t,", built.split("overlay=x=")[1][:80])

    def test_text_overlay_evidence_uses_the_actual_drawtext_default(self) -> None:
        from render_editor_timeline import overlay_visual_evidence
        from visual_quality import rendered_visual_quality_report

        item = overlay_visual_evidence(
            {
                "id": "plain-card",
                "type": "card",
                "start": 0.0,
                "end": 2.0,
                "style": {},
            },
            0.5,
        )
        report = rendered_visual_quality_report(
            {
                "schema_version": 1,
                "duration_s": 2.0,
                "motion_intensity": "low",
                "expected_visual_beat_count": 1,
                "items": [item],
            }
        )

        self.assertTrue(item["font_evidence_required"])
        self.assertEqual(item["minimum_primary_font_px"], 26.0)
        self.assertEqual(report["status"], "fail")

    def test_image_overlay_does_not_require_font_evidence(self) -> None:
        from render_editor_timeline import overlay_visual_evidence

        item = overlay_visual_evidence(
            {
                "id": "planned-image",
                "type": "image",
                "start": 0.0,
                "end": 2.0,
                "style": {},
            },
            0.5,
        )

        self.assertFalse(item["font_evidence_required"])
        self.assertIsNone(item["minimum_primary_font_px"])

    def test_real_content_animation_is_recorded_as_faithful(self) -> None:
        import structured_card_compositor
        from render_editor_timeline import card_visual_evidence

        pack = structured_card_compositor.load_style_pack("kinetic-social")
        layers = {
            "items": [{
                "id": "L-list",
                "type": "dynamic_list",
                "component_id": "dynamic-list",
            }]
        }
        evidence = card_visual_evidence(
            pack,
            layers,
            "L-list",
            1.0,
            "working/structured_cards/anim-L-list.mov",
        )
        self.assertEqual(evidence["minimum_primary_font_px"], 36.0)
        self.assertEqual(evidence["motion"]["requested"], "staggered-reveal")
        self.assertEqual(evidence["motion"]["delivered"], "staggered-reveal")
        self.assertTrue(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "rendered")

    def test_static_content_fallback_is_never_claimed_faithful(self) -> None:
        import structured_card_compositor
        from render_editor_timeline import card_visual_evidence

        pack = structured_card_compositor.load_style_pack("kinetic-social")
        layers = {
            "items": [{
                "id": "L-list",
                "type": "dynamic_list",
                "component_id": "dynamic-list",
            }]
        }
        evidence = card_visual_evidence(pack, layers, "L-list", 1.0, None)
        self.assertEqual(evidence["motion"]["delivered"], "fade")
        self.assertFalse(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "fallback")

    def test_native_entrance_is_faithful_without_browser_animation(self) -> None:
        from render_editor_timeline import card_visual_evidence

        evidence = card_visual_evidence(
            self.pack, self.layers, "L-carousel", 1.0, None
        )
        self.assertEqual(evidence["motion"]["requested"], "pan")
        self.assertEqual(evidence["motion"]["delivered"], "pan")
        self.assertTrue(evidence["motion"]["faithful"])
        self.assertEqual(evidence["motion"]["status"], "native")


class Phase0dRenderCommandTests(unittest.TestCase):
    """The mixed SFX route must have one unambiguous audio graph."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="renderer-sfx-")
        self.project = Path(self.temp.name)
        (self.project / "source").mkdir()
        (self.project / "assets").mkdir()
        (self.project / "working").mkdir()
        (self.project / "source/source.mp4").write_bytes(b"source")
        (self.project / "assets/card.png").write_bytes(b"image")
        self.stem = self.project / "working/stem.wav"
        self.stem.write_bytes(b"stem")
        self.manifest = {
            "source": {"staged_path": "source/source.mp4", "duration_s": 2.0, "has_audio": True}
        }
        self.state = {
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "overlays": [{
                "id": "card", "type": "image", "source": "assets/card.png",
                "start": 0.0, "end": 1.0, "visible": True,
                "style": {"width": 50, "x": 50, "y": 50},
            }],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @patch("render_editor_timeline.source_has_audible_signal", return_value=True)
    def test_sfx_input_follows_image_assets_and_graph_normalizes_once(self, _audible: object) -> None:
        command = build_render_command(
            self.project, self.state, self.manifest, self.project / "out.mp4", "final",
            sfx_stem=self.stem,
        )
        inputs = [command[index + 1] for index, item in enumerate(command) if item == "-i"]
        self.assertEqual(inputs, [
            str(self.project / "source/source.mp4"),
            str((self.project / "assets/card.png").resolve()),
            str(self.stem),
        ])
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[2:a]", graph)
        self.assertIn("atrim=0:2.000000", graph)
        self.assertEqual(graph.count("loudnorm="), 1)
        self.assertIn("LRA=11,aresample=48000[aout]", graph)
        self.assertEqual(" ".join(command).count("loudnorm="), 1)

    def test_sfx_rejects_multi_cut_timeline(self) -> None:
        self.state["segments"] = [
            {"source_start": 0.0, "source_end": 0.8},
            {"source_start": 1.0, "source_end": 1.8},
        ]
        with self.assertRaisesRegex(ValueError, "single-cut"):
            build_render_command(
                self.project, self.state, self.manifest, self.project / "out.mp4", "final",
                sfx_stem=self.stem,
            )

    def test_sfx_rejects_missing_dialogue(self) -> None:
        self.manifest["source"]["has_audio"] = False
        with self.assertRaisesRegex(ValueError, "dialogue"):
            build_render_command(
                self.project, self.state, self.manifest, self.project / "out.mp4", "final",
                sfx_stem=self.stem,
            )

    @patch("render_editor_timeline.subprocess.run")
    def test_direct_qa_passes_complete_sfx_bindings(self, run: object) -> None:
        from types import SimpleNamespace
        from render_editor_timeline import qa_direct_final_output

        candidate = self.project / "candidate.mp4"
        candidate.write_bytes(b"candidate")
        evidence = self.project / "evidence.json"
        evidence.write_text("{}", encoding="utf-8")
        plan = self.project / "plan.json"
        catalog = self.project / "catalog.json"
        plan.write_text("{}", encoding="utf-8")
        catalog.write_text("{}", encoding="utf-8")
        report = self.project / "report.json"
        contact = self.project / "contact.png"

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            report_arg = Path(command[command.index("--report") + 1])
            contact_arg = Path(command[command.index("--contact") + 1])
            report_arg.write_text(json.dumps({
                "visual_delivery": {"source": "renderer_evidence", "status": "pass"},
                "sfx_delivery": {"source": "independent_sfx_evidence", "status": "pass"},
            }), encoding="utf-8")
            contact_arg.write_bytes(b"contact")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        run.side_effect = fake_run  # type: ignore[attr-defined]
        qa_direct_final_output(
            self.project, candidate, "direct-final-test", evidence, {}, report, contact,
            plan, catalog, self.stem, "timeline-revision", "a" * 64,
        )
        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[command.index("--audio-event-plan") + 1], str(plan))
        self.assertEqual(command[command.index("--audio-catalog") + 1], str(catalog))
        self.assertEqual(command[command.index("--sfx-stem") + 1], str(self.stem))
        self.assertEqual(command[command.index("--expected-timeline-revision") + 1], "timeline-revision")
        self.assertEqual(command[command.index("--expected-cut-map-sha256") + 1], "a" * 64)


class DirectActiveHighlightRenderTests(unittest.TestCase):
    """A direct render must carry the selected highlight into the renderer."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="renderer-active-highlight-")
        self.project = Path(self.temp.name)
        (self.project / "source").mkdir()
        (self.project / "working").mkdir()
        (self.project / "source/source.mp4").write_bytes(b"source")
        self.manifest = {
            "project_id": "project-active-highlight",
            "source": {
                "staged_path": "source/source.mp4",
                "duration_s": 10.0,
                "has_audio": False,
            },
        }
        self.state = {
            "schema_version": 2,
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "fit": "contain",
            },
            "subject_tracking": False,
            "segments": [{"source_start": 0.0, "source_end": 10.0}],
            "highlights": [{
                "id": "highlight-active",
                "start": 2.0,
                "end": 6.0,
                "title": "Active title",
                "review_status": "approved",
            }],
            "active_highlight_id": "highlight-active",
            "overlays": [{
                "id": "scoped-title",
                "type": "title",
                "start": 2.0,
                "end": 4.0,
                "text": "Scoped title",
                "visible": True,
                "highlight_id": "highlight-active",
                "style": {"font_size": 52, "x": 50, "y": 40},
            }],
        }
        (self.project / "project.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        (self.project / "working/editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _render_preview(self, output: Path) -> object:
        from types import SimpleNamespace

        self.render_commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            self.render_commands.append(command)
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with (
            patch("render_editor_timeline.font_path", return_value=Path("font.ttf")),
            patch("render_editor_timeline.source_has_audible_signal", return_value=False),
            patch("render_editor_timeline.ffprobe_has_visual_stream", return_value=True),
            patch("render_editor_timeline.subprocess.run", side_effect=fake_run),
            patch("render_editor_timeline.build_render_command", wraps=build_render_command) as build,
        ):
            render_project(self.project, output, "preview")
        return build

    def test_direct_render_passes_active_clip_and_keeps_scoped_title(self) -> None:
        output = self.project / "preview.mp4"
        build = self._render_preview(output)

        self.assertTrue(output.is_file())
        clip = build.call_args.args[5]  # type: ignore[union-attr]
        self.assertEqual(clip["id"], "highlight-active")
        command = self.render_commands[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("drawtext=", graph)
        self.assertIn("scoped-title.txt", graph)

    def test_direct_render_without_active_id_keeps_full_timeline_behavior(self) -> None:
        self.state["active_highlight_id"] = None
        (self.project / "working/editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        build = self._render_preview(self.project / "preview.mp4")

        self.assertIsNone(build.call_args.args[5])  # type: ignore[union-attr]
        graph = self.render_commands[0][self.render_commands[0].index("-filter_complex") + 1]
        self.assertNotIn("scoped-title.txt", graph)

    def _assert_active_id_rejected(self) -> None:
        from types import SimpleNamespace

        def fake_build(
            _project_dir: Path,
            _state: dict,
            _manifest: dict,
            output: Path,
            *_args: object,
            **_kwargs: object,
        ) -> list[str]:
            return ["fake-render", str(output)]

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with (
            patch("render_editor_timeline.build_render_command", side_effect=fake_build),
            patch("render_editor_timeline.subprocess.run", side_effect=fake_run),
            patch("render_editor_timeline.ffprobe_has_visual_stream", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "active_highlight_id"):
                render_project(self.project, self.project / "preview.mp4", "preview")

    def test_direct_render_rejects_dangling_active_highlight(self) -> None:
        self.state["active_highlight_id"] = "missing-highlight"
        (self.project / "working/editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        self._assert_active_id_rejected()

    def test_direct_render_rejects_ambiguous_active_highlight(self) -> None:
        self.state["highlights"].append(dict(self.state["highlights"][0]))
        (self.project / "working/editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        self._assert_active_id_rejected()

    def test_direct_render_rejects_malformed_active_identity(self) -> None:
        for identity in (0, False, "", "   "):
            with self.subTest(identity=identity):
                self.state["active_highlight_id"] = identity
                (self.project / "working/editor_state.json").write_text(
                    json.dumps(self.state), encoding="utf-8"
                )
                self._assert_active_id_rejected()

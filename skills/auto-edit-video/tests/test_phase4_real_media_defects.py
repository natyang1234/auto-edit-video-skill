"""Phase 4: the three ways a real kinetic cut still died on real media.

Each class here is one failure taken off a real run of `cut` over customer
footage, reduced to the smallest fixture that still reproduces it. None of
them relaxes a check: the frozen-scene case keeps the one-window
requirement and stops mis-reading a pause-cut scene as two scenes, the
caption-generation case replaces a lie about overlay counts with the reason
the overlays are not there, and the provider-count case gives a
non-deterministic model the same bounded second chance every other
contract violation already gets.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import caption_compositor as cc  # noqa: E402
import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402
import qa_video  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402
import sfx_delivery  # noqa: E402
from editor_server import gate_revision  # noqa: E402
from test_phase3_preservation_mutations import _DeliveryRoundtripCase  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs local ffmpeg")
class FrozenSceneAcrossRemovedPauseTests(unittest.TestCase):
    """A scene that spans a removed pause is still one scene.

    Pause removal cuts the source axis, so a card planned over 0.5s-1.8s of
    source lands on the final axis in two pieces whenever the silence
    between them is dropped. The renderer read that as "this scene does not
    map to exactly one final window" and killed the cut — on real footage
    this is not an exception, it is what trimming pauses does. The two
    pieces are adjacent by construction (the material between them is
    gone), so the scene occupies one contiguous final interval and the
    binding is exactly that interval.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="phase4-frozen-scene-")
        self.addCleanup(self.tmp.cleanup)
        self.project = (Path(self.tmp.name) / "project").resolve()
        for name in ("source", "working", "renders"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        self.source = self.project / "source" / "talking-head.mp4"
        result = subprocess.run([
            renderer.ffmpeg_path(), "-y",
            "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2.4",
            "-f", "lavfi", "-i", "sine=f=160:r=48000:d=2.4",
            "-filter:a", "volume=0.025",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            str(self.source),
        ], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        # Two kept segments with 1.0s-1.4s of source dropped between them:
        # the shape `trim_pauses` produces.
        self.state = {
            "schema_version": 2,
            "project_id": "phase4-frozen-scene",
            "source_sha256": self.source_sha,
            "segments": [
                {
                    "id": "segment-phase4-0001",
                    "source_start": 0.0,
                    "source_end": 1.0,
                    "origin": "pause_trimmed",
                },
                {
                    "id": "segment-phase4-0002",
                    "source_start": 1.4,
                    "source_end": 2.4,
                    "origin": "pause_trimmed",
                },
            ],
            "variants": [],
            "rights": {"asserted": False, "assertion_revision": None},
            "canvas": {
                "platform_id": "instagram-reels", "width": 360, "height": 640,
                "fps": 30, "fit": "cover",
            },
            "director_style": "kinetic-explainer",
            "style_pack": {"project_default": "kinetic-social", "per_highlight": {}},
            "qa_policy": {"profile": "strict"},
            "caption_defaults": {},
            "highlights": [],
            "asset_digests": {},
            "overlays": [{
                "id": "phase4-title", "type": "title", "text": "REAL MIX",
                "start": 0.5, "end": 1.8, "visible": True,
                "design_role": "hook",
                "style": {
                    "font_size": 52, "color": "#FFFFFF", "stroke_width": 2,
                    "stroke_color": "#111111", "x": 50, "y": 45,
                    "animation": "pop",
                },
            }],
        }
        self._write("working/editor_state.json", self.state)
        self._write("working/structured_layers.json", {
            "schema_version": 1,
            "items": [{
                "id": "structured-layer-04040401",
                "visual_plan_item_id": "visual-beat-04040401",
                "type": "title",
                "revision": 1,
                "evidence_revision": self.source_sha,
                "payload": {"title_kind": "full-screen-hook", "title": "REAL MIX"},
                "review_status": "approved",
                "component_id": "kinetic-title",
            }],
        })
        # 0.5s-1.8s of source spans the removed 1.0s-1.4s pause.
        visual_items = [{
            "id": "visual-beat-04040401",
            "highlight_id": "highlight-04040404",
            "start": 0.5,
            "end": 1.8,
            "beat": "title",
            "structured_layer_id": "structured-layer-04040401",
            "selected_asset": None,
            "conceptual_only": False,
            "evidence_ids": [],
            "review_status": "approved",
            "eligibility": "eligible",
            "eligibility_reason": None,
            "family": "title_reveal",
            "role": "opening_title",
            "importance": "high",
            "major_graphic": True,
            "micro_silent": False,
            "stage": "full_screen_graphic",
            "trigger_role": "title_enter",
        }]
        self._write("working/visual_plan_v2.json", {
            "schema_version": 1,
            "highlight_plan_revision": self.source_sha,
            "items": visual_items,
            "revision": contract_registry.canonical_hash(visual_items),
        })
        self._write("project.json", {
            "schema_version": 1,
            "project_id": "phase4-frozen-scene",
            "source": {
                "staged_path": "source/talking-head.mp4", "duration_s": 2.4,
                "sha256": self.source_sha, "has_audio": True,
            },
            "approvals": {"timeline": {
                "approved": True,
                "state_revision": gate_revision(self.project, "timeline", self.state),
            }},
        })

    def _write(self, relative: str, payload: dict) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_a_pause_cut_scene_freezes_as_one_final_window(self) -> None:
        output = self.project / "renders" / "phase4-final.mp4"
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        render_id = renderer.direct_final_render_id(self.state, output)
        stage = delivery_envelope.begin_staging(
            self.project, render_id, expected_output=output
        )
        try:
            authority = renderer.freeze_visual_authority(
                self.project, self.state, manifest, None, stage
            )
        finally:
            delivery_envelope.discard_staging(
                self.project, render_id, authority=stage
            )
        items = authority.public["items"]
        self.assertEqual(len(items), 1)
        # 0.5s-1.0s and 1.4s-1.8s of source, with the 0.4s between them
        # gone: 0.5s-1.4s of the final cut, bound once.
        self.assertAlmostEqual(items[0]["start"], 0.5, places=3)
        self.assertAlmostEqual(items[0]["end"], 1.4, places=3)

    def test_the_render_no_longer_stops_on_the_scene_mapping(self) -> None:
        # Neither of the two stops that a pause-trimmed kinetic timeline used
        # to hit may fire: not "frozen structured scene" (the scene is one
        # contiguous final window) and not the Phase 0d single-cut boundary
        # (the SFX mix now takes a concatenated dialogue track).
        output = self.project / "renders" / "phase4-final.mp4"
        result = subprocess.run([
            sys.executable, str(SCRIPTS / "render_editor_timeline.py"),
            "--project-dir", str(self.project), "--output", str(output),
            "--quality", "final",
        ], text=True, capture_output=True, timeout=180)
        combined = result.stderr + result.stdout
        self.assertNotIn("frozen structured scene", combined)
        self.assertNotIn("single-cut timelines", combined)
        self.assertEqual(result.returncode, 0, combined)
        self.assertTrue(output.is_file())

    def test_the_two_pieces_of_a_pause_cut_scene_are_adjacent(self) -> None:
        # Why merging is not a fudge: the second piece starts exactly where
        # the first ends, because the source between them was removed.
        segments = [(0.0, 1.0), (1.4, 2.4)]
        windows = renderer.map_source_range_to_post_cut(segments, 0.5, 1.8)
        self.assertEqual(len(windows), 2)
        self.assertAlmostEqual(windows[0][1], windows[1][0], places=6)
        self.assertEqual(
            renderer.merge_contiguous_windows(windows), [(0.5, 1.4)]
        )

    def test_genuinely_disjoint_windows_still_fail_closed(self) -> None:
        # Merging only joins pieces that touch. Anything else is still two
        # scenes and still refused.
        self.assertEqual(
            renderer.merge_contiguous_windows([(0.0, 1.0), (2.0, 3.0)]),
            [(0.0, 1.0), (2.0, 3.0)],
        )


class CaptionGenerationDisabledTests(unittest.TestCase):
    """A project that decided not to draw captions has none to bind.

    `caption_render_decision` turns caption generation off when the source
    already shows burned-in subtitles, and the editor state records that
    with its reason. The transcript still has caption segments, so caption
    delivery compared 15 transcript sources against 0 overlays and reported
    `caption overlay count mismatch` — a true statement about numbers and a
    false one about what happened. Two real D-Town cuts died on it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase4-caption-decision-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        (self.project / "working/transcript_sources").mkdir(parents=True)
        self.state = {
            "schema_version": 2,
            "project_id": "phase4-caption-decision",
            "caption_generation": {
                "enabled": False,
                "reason": "source already shows subtitles in 9/12 sampled frames",
            },
            "segments": [{
                "id": "full", "source_start": 0.0, "source_end": 4.0,
                "origin": "default_full_source",
            }],
            "overlays": [],
        }

    def test_the_recorded_decision_is_read_from_the_state(self) -> None:
        enabled, reason = caption_delivery.caption_generation_decision(self.state)
        self.assertFalse(enabled)
        self.assertEqual(reason, self.state["caption_generation"]["reason"])

    def test_a_state_without_the_key_still_generates_captions(self) -> None:
        # Older projects predate the recorded decision; captions stay on.
        enabled, _reason = caption_delivery.caption_generation_decision({})
        self.assertTrue(enabled)

    def test_delivery_names_the_decision_instead_of_the_overlay_count(self) -> None:
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.require_caption_overlays(self.state)
        self.assertEqual(caught.exception.code, "caption_generation_disabled")

    def test_a_disabled_project_with_stale_overlays_is_not_waved_through(self) -> None:
        # The skip is only available when there is genuinely nothing to
        # bind. Overlays left behind by an earlier run must still be bound.
        state = copy.deepcopy(self.state)
        state["overlays"] = [{
            "id": "caption-0001", "type": "caption", "start": 0.0, "end": 1.0,
            "text": "殘留字幕", "visible": True,
            "source": "working/transcript_words.json",
        }]
        caption_delivery.require_caption_overlays(state)


class KineticApprovalWithoutCaptionsTests(unittest.TestCase):
    """The gate after the skip has to know about the skip too.

    Skipping delivery for a source that carries its own subtitles moved the
    cut exactly one step further, to a timeline approval that demands an
    adopted caption v2. The exemption is the same one, checked the same
    way, and it closes the moment anything was ever adopted.
    """

    def _project(self, *, adopted: dict | None = None, overlays: list | None = None) -> Path:
        import director_resolver

        tmp = tempfile.TemporaryDirectory(prefix="phase4-kinetic-approval-")
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        (project / "working").mkdir()
        state = {
            "schema_version": 2,
            "director_style": "kinetic-explainer",
            "segments": [{"source_start": 0.0, "source_end": 1.0}],
            "caption_generation": {
                "enabled": False,
                "reason": "source already shows subtitles in 9/12 sampled frames",
            },
            "overlays": overlays or [],
        }
        if adopted is not None:
            state["caption_delivery"] = adopted
        (project / "working/editor_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        (project / "working/resolved_director_profile.json").write_text(
            json.dumps(director_resolver.resolve_director_profile("kinetic-explainer")),
            encoding="utf-8",
        )
        (project / "project.json").write_text(
            json.dumps({"approvals": {"timeline": {"approved": False}}}),
            encoding="utf-8",
        )
        return project

    def test_a_project_that_draws_no_captions_can_still_be_approved(self) -> None:
        import auto_edit

        project = self._project()
        auto_edit.approve_kinetic_timeline(project)
        manifest = json.loads((project / "project.json").read_text("utf-8"))
        self.assertTrue(manifest["approvals"]["timeline"]["approved"])

    def test_an_adopted_delivery_is_still_bound_before_approval(self) -> None:
        import auto_edit

        project = self._project(
            adopted={
                "artifact": "working/caption_delivery_v2.json",
                "artifact_sha256": "a" * 64,
            }
        )
        with self.assertRaises(ValueError):
            auto_edit.approve_kinetic_timeline(project)
        manifest = json.loads((project / "project.json").read_text("utf-8"))
        self.assertFalse(manifest["approvals"]["timeline"]["approved"])

    def test_captions_on_screen_are_still_bound_before_approval(self) -> None:
        import auto_edit

        project = self._project(overlays=[{
            "id": "caption-0001", "type": "caption", "start": 0.0, "end": 1.0,
            "text": "殘留字幕", "visible": True,
            "source": "working/transcript_words.json",
        }])
        with self.assertRaises(ValueError):
            auto_edit.approve_kinetic_timeline(project)
        manifest = json.loads((project / "project.json").read_text("utf-8"))
        self.assertFalse(manifest["approvals"]["timeline"]["approved"])


class ProviderItemCountRetryTests(_DeliveryRoundtripCase):
    """A short answer is a bad sample, not a broken provider.

    qwen2.5:7b answered one of two captions and returned a one-item list;
    `translation_incomplete: provider item count mismatch` raised on the
    spot, with none of the two retries every other contract violation gets,
    and the cut died. The caption it did answer was fine. So: keep the
    answers that can be attributed to a caption, ask again about the ones
    that came back missing, same ceiling of two rounds, then fail closed.
    """

    SOURCES = ("第一句中文 42km", "第二句中文 Notion")

    def _scripted(self, rounds: list[list[str]], plan):
        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append([item["source"] for item in requested])
            return plan(len(rounds), requested)

        return model_call

    def test_a_missing_item_is_asked_about_again_and_the_delivery_completes(self) -> None:
        rounds: list[list[str]] = []

        def plan(round_number: int, requested: list[dict]) -> dict:
            items = [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": (
                        "first line 42km"
                        if item["source"].startswith("第一句")
                        else "second line Notion"
                    ),
                }
                for item in requested
            ]
            # Round one drops the second caption from the list entirely.
            return {"items": items[:1] if round_number == 1 else items}

        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True,
            model_call=self._scripted(rounds, plan),
        )
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0], list(self.SOURCES))
        self.assertEqual(rounds[1], [self.SOURCES[1]])
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            ["first line 42km", "second line Notion"],
        )
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 1)

    def test_an_empty_answer_is_not_retried_into_a_delivery(self) -> None:
        # Nothing to attribute and nothing to keep: the second round drops
        # the only caption it was asked about, and the delivery fails
        # closed there rather than spending the rest of the ceiling on a
        # provider that has stopped answering.
        rounds: list[list[str]] = []

        def plan(_round_number: int, requested: list[dict]) -> dict:
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": "first line 42km",
                    }
                    for item in requested
                    if item["source"].startswith("第一句")
                ]
            }

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project, "en", required=True,
                model_call=self._scripted(rounds, plan),
            )
        self.assertEqual(caught.exception.code, "translation_incomplete")
        self.assertEqual(len(rounds), 2)
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())

    def test_an_unattributable_answer_is_not_retried_into_a_delivery(self) -> None:
        # Items with no instance id cannot be matched to a caption; there is
        # no per-item verdict to give and nothing safe to keep.
        rounds: list[list[str]] = []

        def plan(_round_number: int, _requested: list[dict]) -> dict:
            return {"items": [{"translated_text": "first line 42km"}]}

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project, "en", required=True,
                model_call=self._scripted(rounds, plan),
            )
        self.assertEqual(caught.exception.code, "translation_incomplete")
        self.assertEqual(len(rounds), 1)


class ProviderItemCountCeilingTests(_DeliveryRoundtripCase):
    """The second chance is two rounds here too, not a loop."""

    SOURCES = (
        "第一句中文 42km",
        "第二句中文 Notion",
        "第三句中文 Figma",
        "第四句中文 Slack",
    )

    def test_a_provider_that_keeps_dropping_captions_fails_closed_at_the_ceiling(
        self,
    ) -> None:
        rounds: list[list[str]] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append([item["source"] for item in requested])
            # Only ever answers the first caption it was asked about.
            first = requested[0]
            return {
                "items": [{
                    "caption_instance_id": first["caption_instance_id"],
                    "translated_text": f"line {first['source'].split()[-1]}",
                }]
            }

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project, "en", required=True, model_call=model_call
            )
        self.assertEqual(caught.exception.code, "translation_incomplete")
        # The first ask plus the two retries every other violation gets.
        self.assertEqual(len(rounds), caption_delivery.VALIDATION_MAX_ROUNDS + 1)
        self.assertEqual(rounds[1], list(self.SOURCES[1:]))
        self.assertEqual(rounds[2], list(self.SOURCES[2:]))
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())


class WholeAnswerWithoutIdsTests(_DeliveryRoundtripCase):
    """Twelve answers, right count, right order, not one id echoed back.

    Measured on a real delivery: qwen2.5:7b was asked about twelve captions
    and answered twelve, in order, every one of them a good translation and
    every one of them missing `caption_instance_id`. Three rounds in a row,
    then `translation_order_mismatch: item 0` and the cut died with twelve
    usable answers in hand. The one-item carve-out below it did not reach.

    The delivery does not die on that any more, and it does not read the
    list by position either — order is not a claim about which answer goes
    with which caption. It asks again, one caption per question, where an
    answer has only one caption it could be about. Twelve extra provider
    calls, and no way to adopt a line under the wrong caption.
    """

    TOKENS = (
        "Notion", "Figma", "Slack", "Linear", "Vercel", "Stripe",
        "Docker", "Kafka", "Redis", "Nginx", "Grafana", "Ansible",
    )
    NUMERALS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")
    SOURCES = tuple(
        f"這是第{numeral}句中文 {token}"
        for numeral, token in zip(NUMERALS, TOKENS, strict=True)
    )

    @staticmethod
    def _answer(source: str) -> str:
        return f"this is caption {source.rsplit(' ', 1)[-1]}"

    def _run(self, plan, rounds: list[list[str]] | None = None):
        rounds = [] if rounds is None else rounds

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append([item["source"] for item in requested])
            return plan(len(rounds), requested)

        return caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        ), rounds

    def test_twelve_answers_with_no_ids_are_reasked_one_at_a_time(self) -> None:
        def plan(_round: int, requested: list[dict]) -> dict:
            return {
                "items": [
                    {"translated_text": self._answer(item["source"])}
                    for item in requested
                ]
            }

        artifact, rounds = self._run(plan)
        # The batch ask, then one question naming a single caption each.
        self.assertEqual(rounds[0], list(self.SOURCES))
        self.assertEqual(rounds[1:], [[source] for source in self.SOURCES])
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            [self._answer(source) for source in self.SOURCES],
        )
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)
        # Single asks are not a validation failure — nothing was refused.
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 0)

    def test_a_delivery_that_echoed_its_ids_is_answered_in_one_round(self) -> None:
        def plan(_round: int, requested: list[dict]) -> dict:
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": self._answer(item["source"]),
                    }
                    for item in requested
                ]
            }

        artifact, rounds = self._run(plan)
        # Ids present, so nothing has to be re-asked: one batch, no extra
        # provider calls, and the receipt says the cheap path was taken.
        self.assertEqual(len(rounds), 1)
        self.assertIs(artifact["provider_receipt"]["individual_reask"], False)

    def test_a_partly_identified_answer_keeps_the_strict_id_match(self) -> None:
        # One id present means the provider was tracking ids for at least
        # that item, so the absent ones are missing information rather than
        # a uniform omission. That is not the shape single asks are for, and
        # the strict id match stands: the delivery fails closed.
        def plan(_round: int, requested: list[dict]) -> dict:
            return {
                "items": [
                    {
                        "translated_text": self._answer(item["source"]),
                        **(
                            {"caption_instance_id": item["caption_instance_id"]}
                            if index == 5
                            else {}
                        ),
                    }
                    for index, item in enumerate(requested)
                ]
            }

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._run(plan)
        self.assertEqual(caught.exception.code, "translation_order_mismatch")
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())

    def test_a_wrong_length_answer_is_still_an_incomplete_answer(self) -> None:
        # Without ids there is nothing to say which caption was dropped, so
        # a short list stays what it always was: an incomplete answer. It is
        # not the full-length id-less shape single asks are for either.
        def plan(_round: int, requested: list[dict]) -> dict:
            return {
                "items": [
                    {"translated_text": self._answer(item["source"])}
                    for item in requested[:-1]
                ]
            }

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._run(plan)
        self.assertEqual(caught.exception.code, "translation_incomplete")
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())

    def test_a_singly_asked_item_that_fails_validation_is_asked_about_again(self) -> None:
        # A single ask earns no exemption from the per-item rules: the
        # answer to the sixth caption drops that caption's source token the
        # first time it is asked about on its own, so the sixth caption
        # alone is re-asked, under the same ceiling as any other violation.
        asked: list[str] = []

        def plan(_round: int, requested: list[dict]) -> dict:
            items = []
            for item in requested:
                source = item["source"]
                text = self._answer(source)
                if source.endswith("Stripe") and asked.count(source) == 1:
                    # Second sighting: the batch ask, then this one alone.
                    text = "this is caption six"
                asked.append(source)
                items.append({"translated_text": text})
            return {"items": items}

        artifact, rounds = self._run(plan)
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            [self._answer(source) for source in self.SOURCES],
        )
        # One batch, twelve single asks, then the refused caption re-asked:
        # once as a one-caption batch and once as the single ask that shape
        # always turns into.
        self.assertEqual(rounds[0], list(self.SOURCES))
        self.assertEqual(rounds[1:13], [[source] for source in self.SOURCES])
        self.assertEqual(rounds[13:], [[self.SOURCES[5]], [self.SOURCES[5]]])
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 1)
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)


class ShorteningAnswerWithoutIdsTests(_DeliveryRoundtripCase):
    """The shortening round is the same provider, dropping ids the same way.

    Asking for a shorter line is a second question to the model, and it can
    come back id-less even when the first answer did not. The same rule
    applies in both places — an id-less batch is asked about one caption at
    a time — and the shortened answers still face the same per-item
    validation before they replace anything.
    """

    SOURCES = ("這是第一句中文 Notion", "這是第二句中文 Figma")

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_shortened_answer_with_no_ids_is_reasked_one_at_a_time(self) -> None:
        prompts: list[str] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            prompts.append(prompt)
            first_round = len(prompts) == 1
            items = []
            for item in requested:
                token = item["source"].rsplit(" ", 1)[-1]
                text = (
                    " ".join(["overlong"] * 60) + f" {token}"
                    if first_round
                    else f"concise line {token}"
                )
                entry = {"translated_text": text}
                if first_round:
                    entry["caption_instance_id"] = item["caption_instance_id"]
                items.append(entry)
            return {"items": items}

        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        # The first ask carried ids, so it stood. The shortening ask did
        # not, so it became one question per overflowing caption.
        self.assertEqual(len(prompts), 4)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 1)
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            ["concise line Notion", "concise line Figma"],
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs local ffmpeg")
class SfxCuesSurviveTheSegmentJoinTests(unittest.TestCase):
    """A cue either side of a removed pause lands where it was planned.

    The multi-cut dialogue track is trimmed and concatenated, so the join is
    the one place a cue could slide: everything after it would move by the
    length of whatever the join got wrong. The stem is authored on the final
    axis and enters the mix unshifted, so nothing should move at all. This
    renders two clicks straddling the join through the real graph and reads
    them back out of the encoded file.
    """

    SAMPLE_RATE = 48000
    JOIN_S = 0.8
    CLICK_BEFORE_S = 0.75
    CLICK_AFTER_S = 0.85

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase4-sfx-join-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        for name in ("source", "working"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        self.source = self.project / "source" / "source.mp4"
        made = subprocess.run([
            renderer.ffmpeg_path(), "-y",
            "-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=2.4",
            "-f", "lavfi", "-i", "sine=f=220:r=48000:d=2.4",
            "-filter:a", "volume=0.02",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(self.source),
        ], text=True, capture_output=True, timeout=90)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.stem = self.project / "working" / "stem.wav"
        self._write_stem(self.stem)

    def _write_stem(self, path: Path) -> None:
        total = int(round(1.6 * self.SAMPLE_RATE))
        frames = array("h", [0]) * (total * 2)
        for centre in (self.CLICK_BEFORE_S, self.CLICK_AFTER_S):
            start = int(round(centre * self.SAMPLE_RATE))
            for offset in range(int(0.004 * self.SAMPLE_RATE)):
                value = 22000 if offset % 2 == 0 else -22000
                frames[(start + offset) * 2] = value
                frames[(start + offset) * 2 + 1] = value
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(self.SAMPLE_RATE)
            handle.writeframes(frames.tobytes())

    def _click_positions(self, rendered: Path) -> list[int]:
        decoded = subprocess.run([
            renderer.ffmpeg_path(), "-v", "error", "-i", str(rendered),
            "-map", "0:a", "-ac", "1", "-ar", str(self.SAMPLE_RATE),
            "-f", "s16le", "-",
        ], capture_output=True, timeout=90)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode("utf-8", "replace"))
        mono = array("h")
        mono.frombytes(decoded.stdout)
        window = 240
        energies = [
            (max(abs(value) for value in mono[start:start + window]), start)
            for start in range(0, len(mono) - window, window)
        ]
        energies.sort(reverse=True)
        picked: list[int] = []
        for _level, start in energies:
            if all(abs(start - taken) > self.SAMPLE_RATE // 20 for taken in picked):
                picked.append(start)
            if len(picked) == 2:
                break
        return sorted(picked)

    def test_both_cues_land_where_the_plan_put_them(self) -> None:
        state = {
            "canvas": {"width": 180, "height": 320, "fps": 30},
            "segments": [
                {"source_start": 0.0, "source_end": self.JOIN_S},
                {"source_start": 1.0, "source_end": 1.8},
            ],
            "overlays": [],
            "subject_tracking": False,
        }
        manifest = {"source": {
            "staged_path": "source/source.mp4", "duration_s": 2.4, "has_audio": True,
        }}
        output = self.project / "joined.mp4"
        command = renderer.build_render_command(
            self.project, state, manifest, output, "final", sfx_stem=self.stem,
        )
        rendered = subprocess.run(command, text=True, capture_output=True, timeout=180)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        before, after = self._click_positions(output)
        expected_before = int(round(self.CLICK_BEFORE_S * self.SAMPLE_RATE))
        expected_after = int(round(self.CLICK_AFTER_S * self.SAMPLE_RATE))
        # One window of slack either side, well inside the 3840-sample
        # alignment tolerance Phase 0d verifies cues against.
        self.assertLess(abs(before - expected_before), 480)
        self.assertLess(abs(after - expected_after), 480)
        # The cue after the join did not drift relative to the one before it:
        # the join added no time and removed none.
        self.assertLess(
            abs((after - before)
                - int(round((self.CLICK_AFTER_S - self.CLICK_BEFORE_S) * self.SAMPLE_RATE))),
            240,
        )


class TrailingAacToleranceIsDirectionalTests(unittest.TestCase):
    """The final's missing tail is a codec artefact; a surplus tail is not.

    Every real AAC final measured in Phase 4 decoded 3.1k-3.3k samples
    *short* of the planned stem, because the encoder drops the trailing
    partial frame instead of padding it out. The symmetric 1024-sample
    window called that a delivery defect and failed every real kinetic cut.
    The window is now signed: up to four AAC frames may go missing off the
    end, while extra samples stay bounded at the original 1024 — nothing in
    the pipeline can lengthen the mix, so surplus is still evidence of a
    different render. Both the deliverer and QA read this one predicate.
    """

    def test_the_deficit_side_is_four_aac_frames(self) -> None:
        self.assertEqual(sfx_delivery.CANDIDATE_SAMPLE_COUNT_TOLERANCE, 1024)
        self.assertEqual(
            sfx_delivery.CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING, 4 * 1024
        )

    def test_one_predicate_decides_both_sides(self) -> None:
        within = sfx_delivery.candidate_sample_count_within_tolerance
        for accepted in (-4096, -4000, -3300, -3100, 0, 900, 1024):
            with self.subTest(delta=accepted):
                self.assertTrue(within(accepted))
        for rejected in (-4200, -4097, 1025, 1100):
            with self.subTest(delta=rejected):
                self.assertFalse(within(rejected))

    @staticmethod
    def _sfx_report(delta: int) -> dict:
        return {
            "schema_version": 1,
            "source": "independent_sfx_evidence",
            "status": "pass",
            "expected_event_count": 1,
            "delivered_event_count": 1,
            "events": [{
                "id": "sfx-title-enter-0001",
                "expected_transient_sample": 12000,
                "status": "pass",
            }],
            "failures": [],
            "warnings": [],
            "candidate_output_sha256": "a" * 64,
            "output_audio_evidence": {
                "sample_rate": 48000,
                "channels": 2,
                "sample_width_bytes": 4,
                "sample_count": 96000 + delta,
                "expected_sample_count": 96000,
                "sample_count_delta": delta,
                "sample_count_tolerance_samples":
                    sfx_delivery.CANDIDATE_SAMPLE_COUNT_TOLERANCE,
                "sample_count_tolerance_trailing_samples":
                    sfx_delivery.CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING,
                "decoded_pcm_sha256": "b" * 64,
            },
            "observed_cue_evidence": [{
                "event_id": "sfx-title-enter-0001",
                "evidence_source": "candidate_output_audio",
                "status": "pass",
                "aligned": True,
                "correlation": 0.99,
            }],
        }

    def test_qa_reads_the_same_predicate_as_delivery(self) -> None:
        # Naming the same module is not reading the same rule: QA used to
        # re-derive the signed window inline from the two constants, which
        # agreed with the predicate today and would drift the day either
        # side gained a case. So the predicate is swapped out and QA's
        # verdict has to follow it — both ways, and on the deltas it is
        # actually asked about.
        seen: list[int] = []

        def refuse_everything(delta: int) -> bool:
            seen.append(delta)
            return False

        original = sfx_delivery.candidate_sample_count_within_tolerance
        sfx_delivery.candidate_sample_count_within_tolerance = refuse_everything
        self.addCleanup(
            setattr,
            sfx_delivery,
            "candidate_sample_count_within_tolerance",
            original,
        )
        with self.assertRaisesRegex(ValueError, "within codec tolerance"):
            qa_video.validate_sfx_report(self._sfx_report(0))
        self.assertEqual(seen, [0])

        sfx_delivery.candidate_sample_count_within_tolerance = lambda delta: True
        qa_video.validate_sfx_report(self._sfx_report(999_999))

    def test_the_declared_tolerances_are_still_checked_against_policy(self) -> None:
        # The predicate decides the window; the report still has to declare
        # the policy it was judged under.
        forged = self._sfx_report(0)
        forged["output_audio_evidence"]["sample_count_tolerance_trailing_samples"] = 8192
        with self.assertRaisesRegex(ValueError, "independent policy"):
            qa_video.validate_sfx_report(forged)


class TargetLanguageScriptTests(unittest.TestCase):
    """A caption answered in the source language is not a translation.

    SPEC Phase 3 v1 §4 (v1.5). On the real 38-second Tainan cut, qwen2.5:7b
    answered every one of twelve `en` captions in Chinese — eight of them
    merely converted from traditional to simplified and stamped
    `identity_preserved=true, identity_reason=proper_name`, the other four
    stamped `translated`. Every existing rule passed them: the identity
    exemption excused the echoes, and simplification made the rest differ
    from the source, so `translation_unchanged` never fired. The bilingual
    final shipped Chinese under Chinese.

    The verdict is deterministic and made here rather than asked of the
    model: for a Latin-script target, a translation whose letters are
    mostly CJK is `translation_wrong_language`, which is a contract
    violation like any other and gets the same bounded retry.
    """

    # Verbatim from working/caption_delivery_v2.json of that run.
    REAL_ITEMS = (
        ("我這邊臺南沒有風也沒有雨", "I这边台南没有风也没有雨", "proper_name"),
        ("天氣超晴朗", "天气超级晴朗", None),
        ("你在哪", "你在哪", "proper_name"),
        ("臺南剛開的那一間", "台南刚开的那一间", "proper_name"),
        ("你不是說颱風不能喝", "你不是说台风不能喝", "proper_name"),
        ("北部不能喝", "北部不能喝", "proper_name"),
        ("那我們就來臺南喝呀", "那我们就好台南喝吧", None),
        ("別跟颱風硬碰硬", "别跟台风硬碰硬", "proper_name"),
        ("跟高鐵硬碰硬就好啦", "跟高铁硬碰硬就好啦", "proper_name"),
        ("臺北下來只要 90 分鐘", "台北下来只要 90 分钟", None),
        ("訂一杯還來得及", "订一杯还来得及", None),
    )

    def _judge(
        self,
        source: str,
        translated: str,
        reason: str | None = None,
        target: str = "en",
    ):
        instances = [
            {
                "caption_instance_id": "caption-instance-0000000000000001",
                "corrected_source": source,
            }
        ]
        item = {
            "caption_instance_id": instances[0]["caption_instance_id"],
            "translated_text": translated,
        }
        if reason is not None:
            item["identity_preserved"] = True
            item["identity_reason"] = reason
        return caption_delivery._validate_translations(
            instances, {"items": [item]}, [], target=target
        )

    def _rejects(self, source: str, translated: str, reason: str | None = None) -> None:
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._judge(source, translated, reason)
        self.assertEqual(caught.exception.code, "translation_wrong_language", translated)

    def _accepts(self, source: str, translated: str, reason: str | None = None) -> None:
        result = self._judge(source, translated, reason)
        self.assertEqual(result[0]["translated_text"], translated)

    def test_every_caption_of_the_real_delivery_is_rejected(self) -> None:
        for source, translated, reason in self.REAL_ITEMS:
            with self.subTest(source=source):
                self._rejects(source, translated, reason)

    def test_the_identity_stamp_no_longer_excuses_a_chinese_sentence(self) -> None:
        # The exact escape hatch: a whole sentence carried over under
        # `proper_name`. Nothing about "你在哪" is a proper name.
        self._rejects("你在哪", "你在哪", "proper_name")

    def test_a_normal_english_translation_is_accepted(self) -> None:
        self._accepts("我這邊臺南沒有風也沒有雨", "No wind and no rain here in Tainan")

    def test_a_real_proper_name_kept_verbatim_is_accepted(self) -> None:
        # What the exemption is for: a Latin name the model was right to
        # leave alone, source and answer character for character the same.
        self._accepts("Notion", "Notion", "brand")
        self._accepts("S outh bound", "S outh bound", "number_unit")

    def test_an_english_sentence_carrying_one_chinese_word_is_accepted(self) -> None:
        self._accepts("我們約在臺南見面", "We are meeting in 臺南 tonight")

    def test_a_neutral_answer_with_no_letters_at_all_is_accepted(self) -> None:
        self._accepts("90", "90", "number_unit")
        self._accepts("2026/8/13", "2026/8/13", "number_unit")
        self._accepts("好耶", "🔥🔥🔥")

    def test_a_chinese_target_is_left_alone(self) -> None:
        # The rule is about writing the answer in the target's script, not
        # about disliking Chinese: a zh delivery is untouched by it.
        source, translated = "NEO", "NEO 是一個關於動態說明的節目"
        result = self._judge(source, translated, target="zh")
        self.assertEqual(result[0]["translated_text"], translated)
        # The same pair, asked for in en, is the failure this slice is about.
        self._rejects(source, translated)

    def test_the_code_is_retried_like_any_other_violation(self) -> None:
        self.assertIn(
            "translation_wrong_language", caption_delivery.CONTRACT_VIOLATION_CODES
        )


class TargetLanguageRetryTests(_DeliveryRoundtripCase):
    """Wrong language is worth one more ask, and then it is fatal."""

    SOURCES = ("臺南今天沒有下雨",)

    def test_a_chinese_answer_is_asked_about_again_and_then_completes(self) -> None:
        rounds: list[str] = []
        artifact, rounds = self._create(
            [
                {
                    "translated_text": "台南今天没有下雨",
                    "identity_preserved": True,
                    "identity_reason": "proper_name",
                },
                {"translated_text": "It is not raining in Tainan today"},
            ],
            rounds,
        )
        self.assertEqual(len(rounds), 2)
        self.assertIn("translation_wrong_language", rounds[1])
        self.assertEqual(
            artifact["items"][0]["translated_text"],
            "It is not raining in Tainan today",
        )

    def test_a_provider_that_stays_in_chinese_fails_closed(self) -> None:
        chinese = {
            "translated_text": "台南今天没有下雨",
            "identity_preserved": True,
            "identity_reason": "proper_name",
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create([chinese, chinese, chinese, chinese])
        self.assertEqual(caught.exception.code, "translation_wrong_language")


class ReversedIdlessAnswersAreNeverAdoptedTests(_DeliveryRoundtripCase):
    """A provider that answers in the wrong order must not be believed.

    Pure-Chinese captions with no Latin word and no digit give the
    validator an empty required set, and an empty required set is satisfied
    by every string: a reversed pair passed every rule and each line was
    adopted under the wrong caption, silently. The delivery no longer reads
    an id-less answer by position at all, whatever the captions look like,
    so a reversal has nowhere to land — the captions are asked about one at
    a time, where a single-caption answer cannot be misattributed, and the
    ones that still fail fail closed.
    """

    SOURCES = ("早安大家", "晚安大家")
    ENGLISH = {"早安大家": "Good morning everyone", "晚安大家": "Good night everyone"}

    def _reversed_provider(self, rounds: list[list[str]]):
        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append([item["source"] for item in requested])
            items = [{"translated_text": self.ENGLISH[item["source"]]} for item in requested]
            items.reverse()
            return {"items": items}

        return model_call

    def test_a_reversed_answer_without_anchors_is_never_adopted(self) -> None:
        rounds: list[list[str]] = []
        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=self._reversed_provider(rounds)
        )
        # Whatever the delivery does, it must not hand back the swap.
        self.assertEqual(
            [(item["corrected_source"], item["translated_text"]) for item in artifact["items"]],
            [(source, self.ENGLISH[source]) for source in self.SOURCES],
        )
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)
        # The batch ask, then one ask per caption on its own.
        self.assertEqual(rounds[0], list(self.SOURCES))
        self.assertEqual(rounds[1:], [[self.SOURCES[0]], [self.SOURCES[1]]])

    def test_a_rotation_of_three_unanchored_captions_is_never_adopted(self) -> None:
        sources = ("早安大家", "晚安大家", "午安大家")
        english = {
            "早安大家": "Good morning everyone",
            "晚安大家": "Good night everyone",
            "午安大家": "Good afternoon everyone",
        }

        class _Rotated(ReversedIdlessAnswersAreNeverAdoptedTests):
            SOURCES = sources
            ENGLISH = english

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            items = [{"translated_text": english[item["source"]]} for item in requested]
            return {"items": items[1:] + items[:1]}

        case = _Rotated("test_a_reversed_answer_without_anchors_is_never_adopted")
        case.setUp()
        self.addCleanup(case.doCleanups)
        artifact = caption_delivery.create_delivery(
            case.project, "en", required=True, model_call=model_call
        )
        self.assertEqual(
            [(item["corrected_source"], item["translated_text"]) for item in artifact["items"]],
            [(source, english[source]) for source in sources],
        )
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)

    def test_a_single_ask_that_still_fails_fails_closed(self) -> None:
        # The fallback is not a way through: a caption the provider will not
        # translate is refused however it is asked.
        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return {"items": [{"translated_text": item["source"] + "簡體"} for item in requested]}

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project, "en", required=True, model_call=model_call
            )
        self.assertEqual(caught.exception.code, "translation_wrong_language")
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())

    def test_the_single_ask_fallback_is_bounded(self) -> None:
        # One batch ask plus one ask per caption, per validation round, and
        # the existing ceiling still ends it.
        calls: list[int] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            calls.append(len(requested))
            return {"items": [{"translated_text": item["source"] + "簡體"} for item in requested]}

        with self.assertRaises(caption_delivery.CaptionDeliveryError):
            caption_delivery.create_delivery(
                self.project, "en", required=True, model_call=model_call
            )
        ceiling = (caption_delivery.VALIDATION_MAX_ROUNDS + 1) * (1 + len(self.SOURCES))
        self.assertLessEqual(len(calls), ceiling)


class SharedTokenAnswersAreNeverPlacedByPositionTests(_DeliveryRoundtripCase):
    """A token every caption shares verifies nothing about which one it is.

    The delivery used to read an id-less answer by position whenever every
    caption in the batch carried a Latin or numeric token, on the argument
    that a shifted answer loses that token and is refused. That argument
    only holds while the tokens tell the captions apart. When the batch
    shares one — a brand carried through every line, a single letter, the
    same number said twice — a reversed answer keeps the token it needs and
    passes every rule, and each line was adopted under the wrong caption
    with nothing but `positional_attribution: true` to show for it.

    Three shapes of that, measured against the real validator. None of them
    may come back swapped: an id-less batch is asked about one caption at a
    time, where an answer has only one caption it could belong to.
    """

    SOURCES = ("早安大家 NEO", "晚安大家 NEO")
    ENGLISH = {"早安大家 NEO": "Good morning everyone NEO", "晚安大家 NEO": "Good night everyone NEO"}

    def _reversing_provider(self, english: dict[str, str], rounds: list[list[str]]):
        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append([item["source"] for item in requested])
            items = [{"translated_text": english[item["source"]]} for item in requested]
            items.reverse()
            return {"items": items}

        return model_call

    def _assert_not_swapped(self, sources, english) -> None:
        class _Case(SharedTokenAnswersAreNeverPlacedByPositionTests):
            SOURCES = tuple(sources)

        case = _Case("test_a_brand_carried_by_every_caption_is_not_an_anchor")
        case.setUp()
        self.addCleanup(case.doCleanups)
        rounds: list[list[str]] = []
        artifact = caption_delivery.create_delivery(
            case.project, "en", required=True, model_call=self._reversing_provider(english, rounds)
        )
        self.assertEqual(
            [(item["corrected_source"], item["translated_text"]) for item in artifact["items"]],
            [(source, english[source]) for source in sources],
        )
        self.assertIs(artifact["provider_receipt"]["individual_reask"], True)
        # The batch ask, then one ask naming a single caption each.
        self.assertEqual(rounds[0], list(sources))
        self.assertEqual(rounds[1:], [[source] for source in sources])

    def test_a_brand_carried_by_every_caption_is_not_an_anchor(self) -> None:
        self._assert_not_swapped(self.SOURCES, self.ENGLISH)

    def test_a_single_shared_letter_is_not_an_anchor(self) -> None:
        self._assert_not_swapped(
            ("早安大家 A", "晚安大家 A"),
            {"早安大家 A": "Good morning A", "晚安大家 A": "Good night A"},
        )

    def test_the_same_number_said_twice_is_not_an_anchor(self) -> None:
        self._assert_not_swapped(
            ("上半場 90 分鐘超讚", "下半場 90 分鐘超爛"),
            {
                "上半場 90 分鐘超讚": "First half 90 minutes was great",
                "下半場 90 分鐘超爛": "Second half 90 minutes was awful",
            },
        )

    def test_distinct_tokens_are_not_an_anchor_either(self) -> None:
        # Tokens that do tell the captions apart are not a licence to read
        # by position; they only mean the swap would have been caught after
        # the fact. It is not attempted at all, so nothing has to be caught.
        self._assert_not_swapped(
            ("第一句中文 42km", "第二句中文 Notion"),
            {"第一句中文 42km": "first 42km", "第二句中文 Notion": "second Notion"},
        )


if __name__ == "__main__":
    unittest.main()

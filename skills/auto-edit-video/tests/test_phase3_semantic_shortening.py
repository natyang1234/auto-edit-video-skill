"""Phase 3 SPEC v1 sub-slice 3: long translations are shortened, not squeezed.

`docs/SPEC-phase3-bilingual-typography-v1.md` §3 step 3 and §4 second point.
Steps 1 (wrap) and 2 (autofit to the floor) already exist, and step 4 already
fails closed when even the floor leaves a third line. The step in between is
this one: when the translation does not fit in two lines at its floor size,
the caption instance is asked for again with a character budget measured by
the compositor — once, never twice — and the answer still has to survive the
same identity validation as the first one.

The budget is measured, not counted: a line's capacity comes from the same
CoreText measurement that decides the raster's breaks, because a budget
derived from a different ruler than the one the frame is cut with is not a
budget, it is a guess that fails closed later.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import caption_compositor as cc  # noqa: E402
import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402


def monospace(width_per_char: float = 1.0):
    return lambda text: len(text) * width_per_char


class MeasuredCharacterBudgetTests(unittest.TestCase):
    """SPEC §3 step 3: 2 lines x measured line capacity x 0.95."""

    def test_line_capacity_is_the_longest_prefix_that_fits(self) -> None:
        self.assertEqual(
            cc.measured_line_capacity("x" * 40, monospace(10.0), 100.0), 10
        )

    def test_line_capacity_of_text_that_already_fits_is_its_whole_length(self) -> None:
        self.assertEqual(cc.measured_line_capacity("abc", monospace(10.0), 100.0), 3)

    def test_line_capacity_of_empty_text_is_zero(self) -> None:
        self.assertEqual(cc.measured_line_capacity("", monospace(10.0), 100.0), 0)

    def test_the_budget_is_two_lines_of_capacity_with_the_safety_margin(self) -> None:
        self.assertEqual(cc.CHARACTER_BUDGET_SAFETY, 0.95)
        self.assertEqual(cc.MAX_CAPTION_LINES, 2)
        # capacity 10 -> 2 * 10 * 0.95 = 19
        self.assertEqual(
            cc.character_budget("x" * 40, monospace(10.0), 100.0), 19
        )

    def test_the_budget_is_never_zero(self) -> None:
        self.assertGreaterEqual(
            cc.character_budget("x" * 40, monospace(1000.0), 100.0), 1
        )


class TranslationFitTests(unittest.TestCase):
    """The same CoreText ruler the raster uses, asked one question early."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-shortening-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _state(self, translation: str) -> dict:
        overlay = {
            "id": "caption-0001",
            "type": "caption",
            "start": 0.0,
            "end": 2.0,
            "text": "看到 想到 為什麼",
            "translation": translation,
            "visible": True,
            "style": {"font_size": 52, "max_width": 84},
        }
        return {"overlays": [overlay], "canvas": {"width": 1080, "height": 1920}}

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_that_fits_reports_no_budget(self) -> None:
        state = self._state("a short line")
        fit = cc.translation_fit(
            self.project, state["overlays"][0], state["canvas"], 1.0, state
        )
        self.assertTrue(fit["fits"])
        self.assertIsNone(fit["character_budget"])
        self.assertLessEqual(fit["line_count"], cc.MAX_CAPTION_LINES)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_that_needs_three_lines_reports_a_budget(self) -> None:
        long_translation = " ".join(["overlong"] * 60)
        state = self._state(long_translation)
        fit = cc.translation_fit(
            self.project, state["overlays"][0], state["canvas"], 1.0, state
        )
        self.assertFalse(fit["fits"])
        self.assertEqual(fit["reason"], "secondary")
        self.assertGreater(fit["character_budget"], 0)
        self.assertLess(fit["character_budget"], len(long_translation))

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_single_unbreakable_run_too_wide_for_the_frame_does_not_fit(self) -> None:
        # Counting lines alone calls this one line and therefore fine. It is
        # not fine: nothing can break it, so the one line it makes is wider
        # than the frame and the safe-area check throws the cut away later.
        # "Two lines cannot hold it" has to mean the pixels, not the count.
        run = "unbreakabletranslationrunwithnospacesorhyphensanywhereinsideitatallwhatsoever"
        state = self._state(run)
        fit = cc.translation_fit(
            self.project, state["overlays"][0], state["canvas"], 1.0, state
        )
        self.assertEqual(fit["line_count"], 1)
        self.assertFalse(fit["fits"])
        self.assertEqual(fit["reason"], "secondary")
        self.assertGreater(fit["character_budget"], 0)
        self.assertLess(fit["character_budget"], len(run))

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_no_translation_at_all_fits_trivially(self) -> None:
        state = self._state("")
        fit = cc.translation_fit(
            self.project, state["overlays"][0], state["canvas"], 1.0, state
        )
        self.assertTrue(fit["fits"])
        self.assertIsNone(fit["character_budget"])


LONG_TRANSLATION = " ".join(["overlong"] * 60)
SHORT_TRANSLATION = "a concise second line"


class ShorteningRetryTests(unittest.TestCase):
    """Delivery asks again, once, with the measured budget attached."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-shortening-delivery-")
        self.project = Path(self._tmp.name)
        (self.project / "working/transcript_sources").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self.manifest = {
            "schema_version": 1,
            "project_id": "phase3-shortening",
            "subtitles": {
                "glossary": [],
                "contextual_semantic_calibration": {"model": "qwen2.5:7b"},
            },
            "approvals": {
                "timeline": {"approved": True},
                "final": {"approved": True},
            },
        }
        self.transcript = {
            "words": [
                {"id": "word-00001", "text": "第一句中文", "start": 0.0, "end": 1.0},
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "text": "第一句中文",
                    "start": 0.0,
                    "end": 1.0,
                    "word_ids": ["word-00001"],
                },
            ],
        }
        self.state = {
            "schema_version": 2,
            "project_id": "phase3-shortening",
            "canvas": {"width": 1080, "height": 1920},
            "segments": [
                {
                    "id": "full",
                    "source_start": 0.0,
                    "source_end": 2.0,
                    "origin": "default_full_source",
                }
            ],
            "overlays": [
                {
                    "id": "caption-0001",
                    "type": "caption",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "第一句中文",
                    "visible": True,
                    "source": "working/transcript_words.json",
                    "style": {"font_size": 52, "max_width": 84},
                },
            ],
        }
        self._write_source_revision()
        self._write_project()

    def _write_source_revision(self) -> None:
        payload = {
            "schema_version": 1,
            "revision": "",
            "source_media_sha256": "a" * 64,
            "audio_stream_index": 0,
            "decoded_pcm": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_format": "s16le",
                "sha256": "b" * 64,
            },
            "engine": "openai-whisper",
            "engine_version": "1.0",
            "model": "large-v3",
            "language": "zh",
            "decoding_params": {},
            "source_generation": 0,
            "raw_words": [
                {
                    "source_word_index": 0,
                    "start_us": 0,
                    "end_us": 1_000_000,
                    "text": "第一句中文",
                    "speaker": None,
                }
            ],
        }
        material = dict(payload)
        material.pop("revision")
        payload["revision"] = contract_registry.canonical_hash(material)
        path = self.project / f"working/transcript_sources/{payload['revision']}.json"
        caption_delivery._atomic_write(path, payload)
        caption_delivery._atomic_write(
            self.project / "working/transcript_source_current.json",
            {
                "schema_version": 1,
                "revision": payload["revision"],
                "path": f"working/transcript_sources/{payload['revision']}.json",
                "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )

    def _write_project(self) -> None:
        caption_delivery._atomic_write(self.project / "project.json", self.manifest)
        caption_delivery._atomic_write(
            self.project / "working/transcript_words.json", self.transcript
        )
        caption_delivery._atomic_write(
            self.project / "working/editor_state.json", self.state
        )

    def _recorder(self, replies: list[str]):
        """A provider that answers from `replies`, recording every prompt."""
        prompts: list[str] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            prompts.append(prompt)
            text = replies[min(len(prompts), len(replies)) - 1]
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": text,
                    }
                    for item in requested
                ]
            }

        return model_call, prompts

    def _create(self, replies: list[str]):
        model_call, prompts = self._recorder(replies)
        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        return artifact, prompts

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_that_fits_never_asks_the_provider_twice(self) -> None:
        artifact, prompts = self._create([SHORT_TRANSLATION])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 0)
        self.assertEqual(artifact["provider_receipt"]["shortening_character_budgets"], {})
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_an_overlong_translation_is_asked_again_with_a_measured_budget(self) -> None:
        artifact, prompts = self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        self.assertEqual(len(prompts), 2)
        instance_id = artifact["items"][0]["caption_instance_id"]
        budgets = artifact["provider_receipt"]["shortening_character_budgets"]
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 1)
        self.assertIn(instance_id, budgets)
        budget = budgets[instance_id]
        self.assertGreater(budget, 0)
        self.assertLess(budget, len(LONG_TRANSLATION))
        self.assertIn(str(budget), prompts[1])
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_retry_prompt_forbids_dropping_numbers_units_and_names(self) -> None:
        _artifact, prompts = self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        retry = prompts[1].casefold()
        for phrase in ("numbers", "units", "brands", "proper names", "must not be dropped"):
            self.assertIn(phrase, retry)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_retry_is_asked_at_most_once_even_if_it_is_still_too_long(self) -> None:
        artifact, prompts = self._create([LONG_TRANSLATION, LONG_TRANSLATION])
        self.assertEqual(len(prompts), 2)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 1)
        # Still too long: adopted as-is so the compositor fails closed on it
        # (SPEC §4), rather than being asked a third time.
        self.assertEqual(artifact["items"][0]["translated_text"], LONG_TRANSLATION)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_shortened_reply_still_faces_identity_validation(self) -> None:
        self.transcript["caption_segments"][0]["text"] = "第一句中文 42km"
        self.transcript["words"][0]["text"] = "第一句中文 42km"
        self.state["overlays"][0]["text"] = "第一句中文 42km"
        self._write_project()
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create([LONG_TRANSLATION + " 42km", SHORT_TRANSLATION])
        self.assertEqual(caught.exception.code, "translation_token_missing")

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_adopted_hash_binds_the_shortened_text_not_the_first_answer(self) -> None:
        artifact, _prompts = self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        raw = (self.project / caption_delivery.CAPTION_REL).read_bytes()
        live = json.loads(raw.decode("utf-8"))
        self.assertEqual(live["items"][0]["translated_text"], SHORT_TRANSLATION)
        self.assertEqual(live, artifact)
        state = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        overlay = state["overlays"][0]
        self.assertEqual(overlay["translation"], SHORT_TRANSLATION)
        self.assertEqual(
            overlay["caption_delivery_artifact_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            state["caption_delivery"]["artifact_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_shortened_delivery_still_validates_for_render(self) -> None:
        self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        artifact, _bound = caption_delivery.validate_for_render(
            self.project, state, manifest
        )
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_forged_shortening_record_does_not_dodge_the_receipt_check(self) -> None:
        artifact, _prompts = self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        forged = copy.deepcopy(artifact)
        forged["provider_receipt"]["model"] = "someone-elses-model"
        for item in forged["items"]:
            item["provider_receipt"] = forged["provider_receipt"]
        caption_delivery._atomic_write(
            self.project / caption_delivery.CAPTION_REL, forged
        )
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        state = json.loads(
            (self.project / "working/editor_state.json").read_text("utf-8")
        )
        with self.assertRaises(caption_delivery.CaptionDeliveryError):
            caption_delivery.validate_for_render(self.project, state, manifest)


if __name__ == "__main__":
    unittest.main()

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


class ShorteningRoundIsRevalidatedTests(ShorteningRetryTests):
    """The second question earns the same second chance as the first.

    The provider is a 7B local model sampling a new answer every time, and
    the first round already knows that: a violation is fed back and asked
    again, twice, before the delivery fails closed. The shortening round was
    a single roll of that same die — and the moment the fit measurement was
    taken of the right frame, real cuts started dying there instead:
    `translation_wrong_language`, answered in simplified Chinese, on a
    caption whose first-round answer had been correct English. Same rules,
    same ceiling, same fail-closed; only the second chance was missing.

    Narrowly that one code, though. An answer that bought its budget by
    dropping the brand or converting the unit is not sampling noise, it is
    the trade-off §4 refuses, and `ShorteningRetryPreservationTests` pins
    that those still cost exactly one round and then fail closed.
    """

    WRONG_LANGUAGE = "这个阶段它代表商品价格持续上涨"

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_violated_shortening_answer_is_asked_again(self) -> None:
        artifact, prompts = self._create(
            [LONG_TRANSLATION, self.WRONG_LANGUAGE, SHORT_TRANSLATION]
        )
        self.assertEqual(len(prompts), 3)
        self.assertIn("translation_wrong_language", prompts[2])
        # Still a shortening ask: the budget travels with the second chance.
        instance_id = artifact["items"][0]["caption_instance_id"]
        budget = artifact["provider_receipt"]["shortening_character_budgets"][instance_id]
        self.assertIn(str(budget), prompts[2])
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 1)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_shortening_round_still_fails_closed_at_the_ceiling(self) -> None:
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create([LONG_TRANSLATION, self.WRONG_LANGUAGE])
        self.assertEqual(caught.exception.code, "translation_wrong_language")

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_shortening_answer_that_passes_costs_no_extra_round(self) -> None:
        _artifact, prompts = self._create([LONG_TRANSLATION, SHORT_TRANSLATION])
        self.assertEqual(len(prompts), 2)


class ShorteningIdentityFieldTypeTests(ShorteningRetryTests):
    """A field of the wrong *type* is the same sampling noise, not a verdict.

    Verbatim from the ATQT cut's third consecutive run (replay of the live
    7B provider): asked to shorten two captions, it answered
    `"identity_preserved": []` and `"identity_reason": []` — lists where a
    boolean and a word belong. The validator is right to reject that, but
    the rejection it raises is `translation_identity_invalid`, and only
    `translation_wrong_language` was worth a resample, so the delivery died
    on the first roll of the die with zero re-asks — the very failure the
    wrong-language second chance exists to prevent, wearing a different
    code. A field the model could not type is a broken answer, not the
    budget trade-off §4 refuses; it belongs with wrong_language.

    The ceiling does not move: still `VALIDATION_MAX_ROUNDS` re-asks and
    then the last verdict stands, and the preservation family
    (`translation_token_missing` and friends) still costs exactly one
    shortening round before failing closed.
    """

    # Both fields as the live model actually returned them.
    TYPE_CONFUSED = {
        "translated_text": SHORT_TRANSLATION,
        "identity_preserved": [],
        "identity_reason": [],
    }

    def _recorder(self, replies: list):
        """As the parent's, but an entry may be a whole item dict.

        The defect is in fields other than `translated_text`, so the
        recorder has to be able to put them on the wire.
        """
        prompts: list[str] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            prompts.append(prompt)
            reply = replies[min(len(prompts), len(replies)) - 1]
            if isinstance(reply, str):
                reply = {"translated_text": reply}
            return {
                "items": [
                    {"caption_instance_id": item["caption_instance_id"], **reply}
                    for item in requested
                ]
            }

        return model_call, prompts

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_mistyped_identity_field_is_asked_again(self) -> None:
        artifact, prompts = self._create(
            [LONG_TRANSLATION, self.TYPE_CONFUSED, SHORT_TRANSLATION]
        )
        self.assertEqual(len(prompts), 3)
        self.assertIn("translation_identity_invalid", prompts[2])
        # Still a shortening ask: the budget travels with the second chance.
        instance_id = artifact["items"][0]["caption_instance_id"]
        budget = artifact["provider_receipt"]["shortening_character_budgets"][
            instance_id
        ]
        self.assertIn(str(budget), prompts[2])
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 1)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_second_ask_says_what_shape_the_identity_fields_take(self) -> None:
        # Naming the code is not enough for a model that answered `[]`: the
        # re-ask has to say the fields are a boolean and a single word.
        _artifact, prompts = self._create(
            [LONG_TRANSLATION, self.TYPE_CONFUSED, SHORT_TRANSLATION]
        )
        retry = prompts[2].casefold()
        self.assertIn("identity_preserved must be the boolean true or false", retry)
        self.assertIn("never a list", retry)
        self.assertIn("identity_reason must be a single one of those words", retry)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_ceiling_does_not_move_and_it_still_fails_closed(self) -> None:
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create([LONG_TRANSLATION, self.TYPE_CONFUSED])
        self.assertEqual(caught.exception.code, "translation_identity_invalid")
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_wire_count_is_the_same_ceiling_as_wrong_language(self) -> None:
        model_call, prompts = self._recorder([LONG_TRANSLATION, self.TYPE_CONFUSED])
        with self.assertRaises(caption_delivery.CaptionDeliveryError):
            caption_delivery.create_delivery(
                self.project, "en", required=True, model_call=model_call
            )
        # One first-round ask, one shortening ask, VALIDATION_MAX_ROUNDS re-asks.
        self.assertEqual(
            len(prompts), 2 + caption_delivery.VALIDATION_MAX_ROUNDS
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_well_typed_identity_claim_still_means_what_it_meant(self) -> None:
        artifact, prompts = self._create(
            [
                LONG_TRANSLATION,
                {
                    "translated_text": SHORT_TRANSLATION,
                    "identity_preserved": True,
                    "identity_reason": "brand",
                },
            ]
        )
        self.assertEqual(len(prompts), 2)
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)
        self.assertIs(artifact["items"][0]["identity_preserved"], True)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_dropped_unit_still_costs_exactly_one_round(self) -> None:
        # The §4 trade-off is untouched: buying the budget by dropping the
        # unit is asked once and then fails closed, no resample.
        self.transcript["caption_segments"][0]["text"] = "第一句中文 42km"
        self.transcript["words"][0]["text"] = "第一句中文 42km"
        self.state["overlays"][0]["text"] = "第一句中文 42km"
        self._write_project()
        model_call, prompts = self._recorder(
            [LONG_TRANSLATION + " 42km", SHORT_TRANSLATION]
        )
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project, "en", required=True, model_call=model_call
            )
        self.assertEqual(caught.exception.code, "translation_token_missing")
        self.assertEqual(len(prompts), 2)


class DeliveryMeasuresTheFrameTheRenderWillCutTests(unittest.TestCase):
    """SPEC §3 v1.2 gap: delivery must measure the frame render actually cuts.

    A caption wraps to the director's `max_width` until the platform's own
    margins narrow it, and the narrowing happens on the render path. Measuring
    the fit at the declared width answers a question about a frame nobody will
    ever draw: the twelve-caption Tainan cut had every translation reported as
    fitting, then died at render with a third line "even at the 36px floor",
    because the two measurements were taken of different columns. The delivery
    measures with the render's own numbers or its verdict means nothing.
    """

    # Twelve of these wrap into two lines at max_width 84 and need three at
    # TikTok's 78 — the whole width of the gap between the two measurements.
    SAFE_AREA_ONLY_OVERFLOW = " ".join(["overlong"] * 12)
    FITS_EVEN_NARROWED = " ".join(["overlong"] * 9)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-safe-area-fit-")
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _state(self, platform_id: str | None) -> dict:
        canvas = {"width": 1080, "height": 1920}
        if platform_id:
            canvas["platform_id"] = platform_id
        return {
            "canvas": canvas,
            "overlays": [
                {
                    "id": "caption-0001",
                    "type": "caption",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "看到 想到 為什麼",
                    "caption_source_id": "caption-source-0001",
                    "visible": True,
                    "source": "working/transcript_words.json",
                    "style": {"font_size": 52, "max_width": 84},
                },
            ],
        }

    def _budgets(self, platform_id: str | None, translation: str) -> dict:
        state = self._state(platform_id)
        instances = [
            {
                "caption_instance_id": "caption-instance-0001",
                "caption_source_id": "caption-source-0001",
            }
        ]
        return caption_delivery._overflowing_budgets(
            self.project, state, instances, [translation]
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_only_the_platform_margin_overflows_is_still_measured(
        self,
    ) -> None:
        budgets = self._budgets("tiktok", self.SAFE_AREA_ONLY_OVERFLOW)
        self.assertIn("caption-instance-0001", budgets)
        self.assertGreater(budgets["caption-instance-0001"], 0)
        self.assertLess(
            budgets["caption-instance-0001"], len(self.SAFE_AREA_ONLY_OVERFLOW)
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_budget_is_the_one_the_narrowed_column_holds(self) -> None:
        # Not the declared column's budget: a budget measured 6% too wide
        # buys back a translation the frame rejects a second time.
        state = self._state("tiktok")
        overlay = state["overlays"][0]
        declared = cc.translation_fit(
            self.project,
            {**overlay, "style": {**overlay["style"], "max_width": 84}},
            state["canvas"],
            1.0,
            state,
            translation=self.SAFE_AREA_ONLY_OVERFLOW,
        )
        narrowed = cc.translation_fit(
            self.project,
            {**overlay, "style": {**overlay["style"], "max_width": 78}},
            state["canvas"],
            1.0,
            state,
            translation=self.SAFE_AREA_ONLY_OVERFLOW,
        )
        self.assertTrue(declared["fits"])
        self.assertFalse(narrowed["fits"])
        budgets = self._budgets("tiktok", self.SAFE_AREA_ONLY_OVERFLOW)
        self.assertEqual(
            budgets["caption-instance-0001"], narrowed["character_budget"]
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_that_fits_the_narrowed_column_is_left_alone(self) -> None:
        self.assertEqual(self._budgets("tiktok", self.FITS_EVEN_NARROWED), {})

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_platform_that_narrows_nothing_measures_exactly_as_before(self) -> None:
        # Instagram Reels leaves 84 clear and the caption asks for 84. No
        # narrowing means no new verdict, and no provider round it never
        # needed: the regression this fix must not cause.
        self.assertEqual(self._budgets("instagram-reels", LONG_TRANSLATION).keys(),
                         {"caption-instance-0001"})
        self.assertEqual(self._budgets("instagram-reels", SHORT_TRANSLATION), {})
        self.assertEqual(self._budgets(None, SHORT_TRANSLATION), {})

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_fit_is_measured_with_the_scale_the_render_will_use(self) -> None:
        import render_editor_timeline

        state = self._state("tiktok")
        state["canvas"]["width"] = 1081
        seen: list[tuple[float, float]] = []
        original = cc.translation_fit

        def recording(project_dir, overlay, canvas, render_scale=1.0, st=None, **kw):
            seen.append(
                (float((overlay.get("style") or {}).get("max_width", 0)), render_scale)
            )
            return original(project_dir, overlay, canvas, render_scale, st, **kw)

        cc.translation_fit = recording
        self.addCleanup(setattr, cc, "translation_fit", original)
        caption_delivery._overflowing_budgets(
            self.project,
            state,
            [
                {
                    "caption_instance_id": "caption-instance-0001",
                    "caption_source_id": "caption-source-0001",
                }
            ],
            [SHORT_TRANSLATION],
        )
        expected_scale = render_editor_timeline.even(1081) / 1081
        self.assertEqual(seen, [(78.0, expected_scale)])


class ShorteningTriggersOnTheRenderedWidthTests(ShorteningRetryTests):
    """The same delivery, on a platform whose margins narrow the column.

    Inherits every rule of the delivery above and changes one thing: the
    project targets TikTok, so the render wraps at 78 rather than 84. A
    translation that only overflows because of that narrowing has to reach
    the provider's second round here exactly as an obviously long one does.
    """

    def setUp(self) -> None:
        super().setUp()
        self.state["canvas"]["platform_id"] = "tiktok"
        self._write_project()

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_platform_margin_overflow_is_asked_again_with_a_budget(self) -> None:
        overflow = DeliveryMeasuresTheFrameTheRenderWillCutTests.SAFE_AREA_ONLY_OVERFLOW
        artifact, prompts = self._create([overflow, SHORT_TRANSLATION])
        self.assertEqual(len(prompts), 2)
        receipt = artifact["provider_receipt"]
        self.assertEqual(receipt["shortening_rounds"], 1)
        budget = receipt["shortening_character_budgets"][
            artifact["items"][0]["caption_instance_id"]
        ]
        self.assertIn(str(budget), prompts[1])
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_translation_the_narrowed_column_holds_costs_no_second_round(
        self,
    ) -> None:
        fits = DeliveryMeasuresTheFrameTheRenderWillCutTests.FITS_EVEN_NARROWED
        artifact, prompts = self._create([fits])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(artifact["provider_receipt"]["shortening_rounds"], 0)


class RetryRoundsAreCountedHonestlyTests(ShorteningRetryTests):
    """The receipt's round ceiling has to be the one the code can reach.

    `validation_retry_rounds` is a sum of two independently capped budgets:
    the first-round re-asks (at most `VALIDATION_MAX_ROUNDS`) plus the
    shortening round's re-asks (capped by the same constant, on its own
    counter, and then added in). Its reachable maximum is therefore twice
    `VALIDATION_MAX_ROUNDS`, but the schema wrote down one of the two — so
    a delivery that legitimately spent three rounds and came back correct
    died at the contract as `caption_contract_invalid`, having done exactly
    what it was allowed to do. Observed on the ATQT cut.

    Nothing is loosened here: each wire-level ceiling is untouched and the
    delivery still fails closed at it. Only the bookkeeping stops lying
    about how high the two of them can add up to.
    """

    #: What the code can actually reach: two independently capped budgets.
    THEORETICAL_MAX = 2 * caption_delivery.VALIDATION_MAX_ROUNDS

    def _receipt_schemas(self) -> dict[str, dict]:
        schema = contract_registry.load_schemas()["caption_delivery"]
        return {
            "root": schema["properties"]["provider_receipt"]["properties"][
                "validation_retry_rounds"
            ],
            "items": schema["properties"]["items"]["items"]["properties"][
                "provider_receipt"
            ]["properties"]["validation_retry_rounds"],
        }

    def test_the_schema_ceiling_is_the_sum_the_code_can_reach(self) -> None:
        for where, node in self._receipt_schemas().items():
            with self.subTest(receipt=where):
                self.assertEqual(node["maximum"], self.THEORETICAL_MAX)
                self.assertEqual(node["minimum"], 0)

    def test_a_receipt_that_spent_every_round_validates(self) -> None:
        artifact = json.loads(
            (
                SKILL_DIR / "contracts/fixtures/caption_delivery/valid.json"
            ).read_text("utf-8")
        )
        for rounds in range(self.THEORETICAL_MAX + 1):
            with self.subTest(rounds=rounds):
                artifact["provider_receipt"]["validation_retry_rounds"] = rounds
                artifact["items"][0]["provider_receipt"] = artifact[
                    "provider_receipt"
                ]
                self.assertEqual(
                    contract_registry.validate_artifact("caption_delivery", artifact),
                    [],
                )

    def test_a_round_count_beyond_the_sum_is_still_rejected(self) -> None:
        artifact = json.loads(
            (
                SKILL_DIR / "contracts/fixtures/caption_delivery/valid.json"
            ).read_text("utf-8")
        )
        artifact["provider_receipt"]["validation_retry_rounds"] = (
            self.THEORETICAL_MAX + 1
        )
        artifact["items"][0]["provider_receipt"] = artifact["provider_receipt"]
        self.assertTrue(
            contract_registry.validate_artifact("caption_delivery", artifact)
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_delivery_that_spent_both_budgets_is_not_a_contract_violation(
        self,
    ) -> None:
        # Two first-round re-asks (the wire ceiling), then an overlong but
        # otherwise correct answer, then one shortening re-ask: three rounds,
        # every one of them permitted, ending in an answer that passes.
        wrong_language = "这个阶段它代表商品价格持续上涨"
        artifact, prompts = self._create(
            [
                wrong_language,
                wrong_language,
                LONG_TRANSLATION,
                wrong_language,
                SHORT_TRANSLATION,
            ]
        )
        self.assertEqual(len(prompts), 5)
        receipt = artifact["provider_receipt"]
        self.assertEqual(
            receipt["validation_retry_rounds"],
            caption_delivery.VALIDATION_MAX_ROUNDS + 1,
        )
        self.assertEqual(receipt["shortening_rounds"], 1)
        self.assertEqual(artifact["items"][0]["translated_text"], SHORT_TRANSLATION)


if __name__ == "__main__":
    unittest.main()

"""Phase 3 sub-slice 4: adversarial mutations of a translated caption.

`docs/SPEC-phase3-bilingual-typography-v1.md` §4 and PRD Phase 3's
terminology/name/number preservation: numbers, units, percentages, brands,
product names and proper nouns are carried into the translation verbatim —
not converted, not rounded, not spelled out, not transliterated, not
localised. The contract that enforces this already exists
(`caption_delivery._validate_translations`); what did not exist is proof
that it holds against a provider that is wrong in the plausible ways.

Every test here is one mutation of a correct answer, and asserts the exact
error code that catches it. They are written from the outside: a provider is
free to return anything, so each of these is a thing a real qwen answer has
a real chance of being. A GREEN here is the evidence that the rule has teeth,
and a mutation that survives is a defect, not a test to relax.

Two rules do the catching, and it is worth naming which is which:

* `translation_token_missing` — every Latin/number/unit token of the source
  must reappear, casefolded, in the translation. This is what makes
  "42km -> 26mi", "87% -> about ninety percent" and "Nvidia -> 輝達"
  impossible: the mutated answer no longer carries the source's token.
* `translation_identity_invalid` — `identity_preserved` is only a claim
  about the four reasons in `IDENTITY_REASONS`, and it exempts an answer
  from nothing except the "you just echoed the source" check.
"""
from __future__ import annotations

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


def _instances(*sources: str) -> list[dict[str, str]]:
    """Just enough of an expected instance for the validator to judge one."""
    return [
        {
            "caption_instance_id": f"caption-instance-{index:016x}",
            "corrected_source": source,
        }
        for index, source in enumerate(sources)
    ]


def _response(
    instances: list[dict[str, str]],
    *texts: str,
    identity: bool | None = None,
    reason: str | None = None,
) -> dict:
    items = []
    for instance, text in zip(instances, texts, strict=True):
        item = {
            "caption_instance_id": instance["caption_instance_id"],
            "translated_text": text,
        }
        if identity is not None:
            item["identity_preserved"] = identity
        if reason is not None:
            item["identity_reason"] = reason
        items.append(item)
    return {"items": items}


class _OneCaptionCase(unittest.TestCase):
    """One source, one answer, and the verdict on it.

    Shared rather than inherited from the class below so that the later
    classes get the helpers without re-running its tests under their own
    names.
    """

    def _rejects(self, source: str, translated: str, code: str, **kwargs) -> None:
        instances = _instances(source)
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(
                instances, _response(instances, translated, **kwargs), []
            )
        self.assertEqual(caught.exception.code, code, translated)

    def _accepts(self, source: str, translated: str, **kwargs) -> dict:
        instances = _instances(source)
        result = caption_delivery._validate_translations(
            instances, _response(instances, translated, **kwargs), []
        )
        self.assertEqual(result[0]["translated_text"], translated)
        return result[0]


class PreservationMutationTests(_OneCaptionCase):
    """One correct translation, mutated one way at a time."""

    # --- the unmutated answers, so the mutations mean something -----------

    def test_a_faithful_translation_that_carries_every_token_is_accepted(self) -> None:
        self._accepts("這段路限速 42km", "this stretch is limited to 42km")
        self._accepts("成長了 87%", "it grew 87%")
        self._accepts("我們用 Notion 記錄", "we keep our notes in Notion")

    # --- numbers ----------------------------------------------------------

    def test_a_changed_number_is_rejected(self) -> None:
        # The single most damaging mutation and the least visible one: the
        # sentence still reads perfectly, it is just no longer true.
        self._rejects(
            "這段路限速 42km",
            "this stretch is limited to 40km",
            "translation_token_missing",
        )

    def test_a_converted_unit_is_rejected(self) -> None:
        # Helpful-looking and still wrong: the caption on screen must say
        # what the speaker said, and the conversion is not the translator's
        # to make.
        self._rejects(
            "這段路限速 42km",
            "this stretch is limited to 26mi",
            "translation_token_missing",
        )

    def test_a_rounded_decimal_is_rejected(self) -> None:
        self._rejects(
            "電池只剩 1.5 小時",
            "only 2 hours of battery left",
            "translation_token_missing",
        )

    def test_a_number_written_out_as_a_word_is_rejected(self) -> None:
        self._rejects(
            "只要 3 個步驟",
            "it only takes three steps",
            "translation_token_missing",
        )

    def test_a_deleted_number_is_rejected(self) -> None:
        self._rejects(
            "只要 3 個步驟",
            "it only takes a few steps",
            "translation_token_missing",
        )

    def test_a_reformatted_thousands_separator_is_accepted(self) -> None:
        # Reversed deliberately, and it is the only rule in this file that
        # moved. It used to read "1,200 and 1200 are the same quantity and
        # not the same caption" — which is a defensible line to draw about
        # typography and an indefensible one to enforce here, because this
        # check exists to catch *facts* being changed. Digit grouping is
        # not a fact: it is a convention the target language sets, and en
        # groups thousands where zh often does not.
        #
        # It was not theoretical. A real 30-second cut died on exactly this
        # (`translation_token_missing: 2000`, source 2000, answer "2,000")
        # — a correct translation, refused, no video. A rule that fires on
        # correct answers gets switched off, and then it stops catching the
        # mutations it was written for. So separators are normalised away
        # on both sides before the comparison, in both directions.
        self._accepts("現場有 1,200 人", "there were 1200 people there")
        self._accepts("現場有 1200 人", "there were 1,200 people there")

    def test_a_fullwidth_number_is_the_same_number(self) -> None:
        # Same argument, different convention: ４２ is 42 written in the
        # width the input method produced, and whisper transcribing Chinese
        # emits fullwidth digits routinely. Refusing the ASCII answer would
        # refuse every one of them.
        self._accepts("這段路限速 ４２km", "this stretch is limited to 42km")
        self._accepts("成長了 ８７%", "it grew 87%")

    def test_normalising_separators_does_not_normalise_away_the_number(self) -> None:
        # The guard on the two above: making the comparison forgiving about
        # how a number is *written* must not make it forgiving about which
        # number it is. 1,200 -> 1,300 is still a changed fact.
        self._rejects(
            "現場有 1,200 人",
            "there were 1,300 people there",
            "translation_token_missing",
        )
        self._rejects(
            "現場有 1,200 人",
            "there were 12,00 people there",
            "translation_token_missing",
        )

    # --- percentages ------------------------------------------------------

    def test_a_percentage_paraphrased_into_words_is_rejected(self) -> None:
        self._rejects(
            "轉換率成長了 87%",
            "conversion grew by about ninety percent",
            "translation_token_missing",
        )

    def test_a_percentage_that_loses_its_sign_is_rejected(self) -> None:
        # "87 percent" keeps the digits and drops the token: the source said
        # 87%, and 87% is what has to survive.
        self._rejects(
            "轉換率成長了 87%",
            "conversion grew 87 percent",
            "translation_token_missing",
        )

    def test_a_percentage_rounded_to_a_neater_one_is_rejected(self) -> None:
        self._rejects(
            "轉換率成長了 87%",
            "conversion grew 90%",
            "translation_token_missing",
        )

    # --- brands, products, proper names -----------------------------------

    def test_a_brand_replaced_by_a_description_is_rejected(self) -> None:
        self._rejects(
            "我們用 Notion 記錄",
            "we keep our notes in a note taking app",
            "translation_token_missing",
        )

    def test_a_brand_translated_into_chinese_is_rejected(self) -> None:
        self._rejects(
            "Nvidia 昨天發表新卡",
            "輝達 announced a new card yesterday",
            "translation_token_missing",
        )

    def test_a_product_name_partly_dropped_is_rejected(self) -> None:
        self._rejects(
            "這是 iPhone 15 Pro",
            "this is the iPhone Pro",
            "translation_token_missing",
        )

    def test_a_code_that_gains_a_character_is_rejected(self) -> None:
        self._rejects(
            "折扣碼是 NEO2026",
            "the discount code is NEO-2026",
            "translation_token_missing",
        )

    def test_a_glossary_term_dropped_from_the_translation_is_rejected(self) -> None:
        instances = _instances("這集聊 Kinetic Explainer 的做法")
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(
                instances,
                _response(instances, "this episode is about motion explainers"),
                ["Kinetic Explainer"],
            )
        self.assertEqual(caught.exception.code, "translation_token_missing")

    def test_case_is_not_treated_as_a_mutation(self) -> None:
        # The counterpart to the rules above: a brand whose capitalisation
        # the target language changed is still the brand. Rejecting it would
        # make the contract fire on correct answers, which is how a rule
        # like this ends up switched off.
        self._accepts("我們用 Notion 記錄", "WE KEEP OUR NOTES IN NOTION")

    # --- identity_preserved, used as a licence -----------------------------

    def test_an_identity_claim_does_not_license_a_changed_number(self) -> None:
        # The interesting abuse: claim the line was deliberately left alone
        # and mutate it anyway. The token rule runs regardless of the claim.
        self._rejects(
            "這段路限速 42km",
            "this stretch is limited to 40km",
            "translation_token_missing",
            identity=True,
            reason="number_unit",
        )

    def test_an_identity_claim_does_not_license_a_converted_unit(self) -> None:
        self._rejects(
            "這段路限速 42km",
            "26mi limit",
            "translation_token_missing",
            identity=True,
            reason="number_unit",
        )

    def test_an_identity_claim_does_not_license_a_rewritten_brand(self) -> None:
        self._rejects(
            "我們用 Notion 記錄",
            "we use 輝達 notes",
            "translation_token_missing",
            identity=True,
            reason="brand",
        )

    def test_an_invented_identity_reason_is_rejected(self) -> None:
        self._rejects(
            "重複中文",
            "重複中文",
            "translation_identity_invalid",
            identity=True,
            reason="sentence",
        )

    def test_an_identity_reason_without_the_claim_is_rejected(self) -> None:
        self._rejects(
            "重複中文",
            "something else entirely",
            "translation_identity_invalid",
            identity=False,
            reason="brand",
        )

    def test_an_empty_identity_reason_next_to_no_claim_is_not_a_contradiction(
        self,
    ) -> None:
        # Observed on every round of a real qwen2.5:7b delivery: the model
        # fills the field with "" instead of leaving it out, alongside
        # identity_preserved=false. That says exactly what null says. It
        # used to fail the whole cut, and no protection depends on reading
        # it as a claim — the exemption hangs on identity_preserved alone.
        item = self._accepts(
            "第一句中文", "the first line", identity=False, reason=""
        )
        self.assertIsNone(item["identity_reason"])
        self.assertEqual(item["translation_status"], "translated")

    def test_an_empty_identity_reason_does_not_stand_in_for_a_real_one(self) -> None:
        # The other half: a claim still has to name one of the four
        # reasons, and "" names nothing.
        self._rejects(
            "重複中文",
            "重複中文",
            "translation_identity_invalid",
            identity=True,
            reason="",
        )

    def test_a_missing_identity_reason_is_rejected(self) -> None:
        self._rejects(
            "重複中文",
            "重複中文",
            "translation_identity_invalid",
            identity=True,
        )

    def test_echoing_the_source_without_an_identity_claim_is_rejected(self) -> None:
        # No claim, no exemption: an answer identical to a Chinese source is
        # a provider that did not translate.
        self._rejects(
            "第一句中文",
            "第一句中文",
            "translation_unchanged",
        )

    def test_punctuation_only_edits_do_not_count_as_translating(self) -> None:
        self._rejects("第一句中文", "第一句，中文！", "translation_unchanged")

    def test_a_whole_chinese_line_echoed_under_a_valid_reason_is_rejected(
        self,
    ) -> None:
        """The gap this file recorded as open, closed by SPEC §4 v1.5.

        Until 2026-08-13 this was a characterisation test: `identity_
        preserved` with one of the four reasons bought the "you echoed the
        source" exemption, nothing bounded *how much* source could be
        echoed under it, and a whole Chinese sentence labelled
        `identity_reason: brand` was accepted. It was recorded as nat's
        call rather than this slice's — and then a real cut made the call,
        shipping twelve `en` captions in Chinese, eight of them through
        exactly this door.

        v1.5 narrows the exemption to what it was written for: a source
        that is itself mostly Latin. A Chinese sentence is not a name left
        alone, whatever the stamp says, and it now fails as
        `translation_wrong_language` — a contract violation with the same
        bounded retry as every other.
        """
        self._rejects(
            "今天要講的是一個完整的中文句子",
            "今天要講的是一個完整的中文句子",
            "translation_wrong_language",
            identity=True,
            reason="brand",
        )
        # The exemption itself still works where it belongs.
        item = self._accepts("Notion", "Notion", identity=True, reason="brand")
        self.assertEqual(item["translation_status"], "identity_preserved")
        self.assertEqual(item["identity_reason"], "brand")

    def test_a_lone_answer_may_leave_out_the_id_it_could_not_confuse(self) -> None:
        # One caption asked about, one answer back: there is no other
        # caption the answer could belong to, so the id it omitted was not
        # carrying any information. qwen2.5:7b echoes ids in a list of six
        # and drops them from a list of one, which is exactly the shape a
        # per-caption retry sends — three correct answers in a row were
        # thrown away for it and the cut died.
        instances = _instances("這段路限速 42km")
        response = {"items": [{"translated_text": "limited to 42km"}]}
        result = caption_delivery._validate_translations(instances, response, [])
        self.assertEqual(result[0]["translated_text"], "limited to 42km")

    def test_a_lone_answer_with_the_wrong_id_is_still_a_mismatch(self) -> None:
        # The line: silence about which caption this is, from a request
        # that named exactly one, is unambiguous. Naming a different
        # caption is the provider saying it answered something else.
        instances = _instances("這段路限速 42km")
        response = {
            "items": [
                {
                    "caption_instance_id": "caption-instance-ffffffffffffffff",
                    "translated_text": "limited to 42km",
                }
            ]
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(instances, response, [])
        self.assertEqual(caught.exception.code, "translation_order_mismatch")

    def test_a_missing_id_among_several_answers_is_still_a_mismatch(self) -> None:
        # Nothing is guessed once there is more than one candidate.
        instances = _instances("限速 42km", "成長了 87%")
        response = {
            "items": [
                {"translated_text": "limited to 42km"},
                {
                    "caption_instance_id": instances[1]["caption_instance_id"],
                    "translated_text": "it grew 87%",
                },
            ]
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(instances, response, [])
        self.assertEqual(caught.exception.code, "translation_order_mismatch")

    def test_one_bad_item_does_not_pass_because_its_neighbour_is_good(self) -> None:
        instances = _instances("限速 42km", "成長了 87%")
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(
                instances,
                _response(instances, "limited to 42km", "grew about 90%"),
                [],
            )
        self.assertEqual(caught.exception.code, "translation_token_missing")


class _DeliveryRoundtripCase(unittest.TestCase):
    """A whole delivery on disk, answered by a scripted provider.

    Split out from the shortening tests below so the contract-violation
    retry tests can drive the same wire without inheriting their cases.
    """

    LONG = " ".join(["overlong"] * 60)
    # One caption unless a subclass asks for more; the first is the one the
    # existing cases speak about, so it keeps its exact text.
    SOURCES = ("第一句中文 42km Notion",)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase3-preservation-")
        self.project = Path(self._tmp.name)
        (self.project / "working/transcript_sources").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self.sources = list(self.SOURCES)
        self.source_text = self.sources[0]
        self.manifest = {
            "schema_version": 1,
            "project_id": "phase3-preservation",
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
                {
                    "id": f"word-{index + 1:05d}",
                    "text": text,
                    "start": float(index),
                    "end": index + 1.0,
                }
                for index, text in enumerate(self.sources)
            ],
            "caption_segments": [
                {
                    "id": f"caption-segment-{index + 1:04d}",
                    "text": text,
                    "start": float(index),
                    "end": index + 1.0,
                    "word_ids": [f"word-{index + 1:05d}"],
                }
                for index, text in enumerate(self.sources)
            ],
        }
        self.state = {
            "schema_version": 2,
            "project_id": "phase3-preservation",
            "canvas": {"width": 1080, "height": 1920},
            "segments": [
                {
                    "id": "full",
                    "source_start": 0.0,
                    "source_end": len(self.sources) + 1.0,
                    "origin": "default_full_source",
                }
            ],
            "overlays": [
                {
                    "id": f"caption-{index + 1:04d}",
                    "type": "caption",
                    "start": float(index),
                    "end": index + 1.0,
                    "text": text,
                    "visible": True,
                    "source": "working/transcript_words.json",
                    "style": {"font_size": 52, "max_width": 84},
                }
                for index, text in enumerate(self.sources)
            ],
        }
        self._write_source_revision()
        caption_delivery._atomic_write(self.project / "project.json", self.manifest)
        caption_delivery._atomic_write(
            self.project / "working/transcript_words.json", self.transcript
        )
        caption_delivery._atomic_write(
            self.project / "working/editor_state.json", self.state
        )

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
                    "source_word_index": index,
                    "start_us": index * 1_000_000,
                    "end_us": (index + 1) * 1_000_000,
                    "text": text,
                    "speaker": None,
                }
                for index, text in enumerate(self.sources)
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

    def _create(self, replies: list[dict], rounds: list[str] | None = None):
        """Answer round N from `replies[N]`; each reply is item fields."""
        rounds = [] if rounds is None else rounds

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            rounds.append(prompt)
            reply = replies[min(len(rounds), len(replies)) - 1]
            return {
                "items": [
                    {"caption_instance_id": item["caption_instance_id"], **reply}
                    for item in requested
                ]
            }

        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        return artifact, rounds


class ShorteningRetryPreservationTests(_DeliveryRoundtripCase):
    """A shorter answer is validated exactly like the first one.

    SPEC §4 second point. The retry is where a provider is most tempted to
    buy characters with the unit, so the mutations that matter most are the
    ones that only appear in the second round: the first answer is clean,
    the shortened one is not.
    """

    def _retry_rejects(self, shortened: dict, code: str) -> None:
        first = {"translated_text": f"{self.LONG} 42km Notion"}
        rounds: list[str] = []
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create([first, shortened], rounds)
        self.assertEqual(caught.exception.code, code)
        # The first answer was clean and merely too long, so the rejection
        # under test has to be the shortened one: two rounds, not one.
        self.assertEqual(len(rounds), 2, rounds)
        self.assertIn("character_budget", rounds[1])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_clean_shortened_answer_is_adopted(self) -> None:
        # The control: the retry that keeps everything is the one that must
        # get through, or the rejections below prove nothing.
        artifact, rounds = self._create(
            [
                {"translated_text": f"{self.LONG} 42km Notion"},
                {"translated_text": "short line 42km Notion"},
            ]
        )
        self.assertEqual(len(rounds), 2)
        self.assertEqual(
            artifact["items"][0]["translated_text"], "short line 42km Notion"
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_that_converts_the_unit_to_save_characters_is_rejected(self) -> None:
        self._retry_rejects(
            {"translated_text": "short line 26mi Notion"}, "translation_token_missing"
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_that_drops_the_brand_to_save_characters_is_rejected(self) -> None:
        self._retry_rejects(
            {"translated_text": "short line 42km"}, "translation_token_missing"
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_that_abbreviates_the_number_is_rejected(self) -> None:
        self._retry_rejects(
            {"translated_text": "short line 42 Notion"}, "translation_token_missing"
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_cannot_buy_its_budget_with_an_identity_claim(self) -> None:
        self._retry_rejects(
            {
                "translated_text": "42km",
                "identity_preserved": True,
                "identity_reason": "number_unit",
            },
            "translation_token_missing",
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_with_an_invented_identity_reason_is_rejected(self) -> None:
        self._retry_rejects(
            {
                "translated_text": "short line 42km Notion",
                "identity_preserved": True,
                "identity_reason": "budget",
            },
            "translation_identity_invalid",
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_rejected_retry_leaves_no_delivery_behind(self) -> None:
        # Fail closed means nothing adopted: no artifact, and the editor
        # state untouched, so a rejected mutation cannot reach a frame.
        state_before = (self.project / "working/editor_state.json").read_bytes()
        self._retry_rejects(
            {"translated_text": "short line 26mi Notion"}, "translation_token_missing"
        )
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())
        self.assertEqual(
            (self.project / "working/editor_state.json").read_bytes(), state_before
        )


class ContractViolationRetryTests(_DeliveryRoundtripCase):
    """Asking again, bounded, before giving up on a whole cut.

    The preservation rules above are worth having and were, until this
    class, wired straight to a dead cut: one bad answer from a 7B local
    model and `create_delivery` raised, `cut` exited 2, and nothing was
    rendered. That is the right verdict for a *provider that cannot* meet
    the contract and the wrong one for a provider that is non-deterministic
    — six consecutive real 30-second cuts died this way, each on a
    different caption and a different code, and each would have been fine
    on a second sample.

    So the same shape as the shortening retry next door: say what was
    wrong, ask again, and keep the ceiling low enough that a provider which
    genuinely cannot comply still fails closed quickly. Two retries, then
    the original verdict stands. Nothing about validation is relaxed to
    make room for this — the retried answer faces exactly the checks the
    first one failed.
    """

    CLEAN = "first line 42km Notion"

    def _replies(self, *texts: str) -> list[dict]:
        return [{"translated_text": text} for text in texts]

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_clean_answer_costs_no_extra_round(self) -> None:
        # The control, and the cost ceiling: a provider that gets it right
        # first time must still be asked exactly once.
        rounds: list[str] = []
        artifact, rounds = self._create(self._replies(self.CLEAN), rounds)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 0)
        self.assertEqual(artifact["items"][0]["translated_text"], self.CLEAN)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_violated_contract_is_asked_again_and_the_good_answer_is_adopted(
        self,
    ) -> None:
        rounds: list[str] = []
        artifact, rounds = self._create(
            self._replies(self.source_text, self.CLEAN), rounds
        )
        self.assertEqual(len(rounds), 2)
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 1)
        self.assertEqual(artifact["items"][0]["translated_text"], self.CLEAN)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_retry_prompt_says_what_was_wrong_with_the_last_answer(self) -> None:
        # A retry that repeats the original prompt is a second roll of the
        # same dice. The provider is told the code, the caption it applies
        # to, and the answer that earned it.
        rounds: list[str] = []
        self._create(self._replies(self.source_text, self.CLEAN), rounds)
        retry_prompt = rounds[1]
        self.assertIn("translation_unchanged", retry_prompt)
        self.assertIn("rejected", retry_prompt)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_retry_prompt_still_asks_for_the_same_thing(self) -> None:
        # The rejection is a preface, not a rewrite. A retry prompt written
        # from scratch is a different instruction, and a small model
        # answers it differently for reasons unrelated to the rejection —
        # the first version of this one talked so much about keeping names
        # and numbers exactly as they appear that qwen2.5:7b started
        # returning the untranslated Chinese source. So the original task
        # text has to survive verbatim inside the retry.
        rounds: list[str] = []
        self._create(self._replies(self.source_text, self.CLEAN), rounds)
        instances = caption_delivery.expected_instances(
            self.project, self.transcript, self.state
        )["instances"]
        original = caption_delivery._translation_prompt(instances, "en")
        self.assertEqual(rounds[0], original)
        self.assertTrue(rounds[1].endswith(original), rounds[1])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_each_round_tells_the_provider_which_try_it_is(self) -> None:
        # Without this the retry is not a second sample: the local provider
        # is pinned to a fixed seed at temperature 0 and answers an
        # equivalent prompt identically, so the same rejected translation
        # comes back and the rounds are spent for nothing.
        attempts: list[int] = []

        def model_call(prompt: str, _stage: str, **kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            attempts.append(kwargs.get("attempt"))
            text = self.source_text if len(attempts) == 1 else self.CLEAN
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": text,
                    }
                    for item in requested
                ]
            }

        caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        self.assertEqual(attempts, [0, 1])

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_second_retry_is_still_allowed(self) -> None:
        rounds: list[str] = []
        artifact, rounds = self._create(
            self._replies(self.source_text, self.source_text, self.CLEAN), rounds
        )
        self.assertEqual(len(rounds), 3)
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 2)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_third_retry_is_not(self) -> None:
        # The bound, asserted as a number and not as a policy: a provider
        # that keeps failing is asked three times in total and then the
        # delivery fails with the code from the last attempt. Without a
        # ceiling here a stuck model is an unbounded loop against the
        # user's own machine.
        rounds: list[str] = []
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create(self._replies(*([self.source_text] * 6)), rounds)
        self.assertEqual(caught.exception.code, "translation_unchanged")
        self.assertEqual(len(rounds), 3)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_retry_that_keeps_failing_leaves_no_delivery_behind(self) -> None:
        rounds: list[str] = []
        state_before = (self.project / "working/editor_state.json").read_bytes()
        with self.assertRaises(caption_delivery.CaptionDeliveryError):
            self._create(self._replies(*([self.source_text] * 6)), rounds)
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())
        self.assertEqual(
            (self.project / "working/editor_state.json").read_bytes(), state_before
        )

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_dropped_token_earns_a_retry_too(self) -> None:
        # Not just the unchanged check: the four contract codes a
        # non-deterministic provider actually produces all get the same
        # second chance. This one dropped the brand.
        rounds: list[str] = []
        artifact, rounds = self._create(
            self._replies("first line 42km", self.CLEAN), rounds
        )
        self.assertEqual(len(rounds), 2)
        self.assertEqual(artifact["items"][0]["translated_text"], self.CLEAN)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_broken_identity_claim_earns_a_retry_too(self) -> None:
        rounds: list[str] = []
        replies = [
            {
                "translated_text": self.CLEAN,
                "identity_preserved": True,
                "identity_reason": "budget",
            },
            {"translated_text": self.CLEAN},
        ]
        artifact, rounds = self._create(replies, rounds)
        self.assertEqual(len(rounds), 2)
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 1)

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_the_retry_count_is_bookkeeping_not_provider_identity(self) -> None:
        # The receipt is compared against one derived from the manifest to
        # detect a swapped provider. How many times this delivery had to
        # ask is not part of who the provider is, exactly as the shortening
        # rounds next to it are not — leaving it in would make every
        # retried delivery look like a provider change.
        receipt = {
            "provider_id": "ollama",
            "mode": "local_loopback",
            "model": "qwen2.5:7b",
            "config_sha256": "f" * 64,
            "consent_mode": "not_required_local",
            "consent_sha256": None,
            "shortening_rounds": 1,
            "shortening_character_budgets": {"caption-instance-0": 20},
            "validation_retry_rounds": 2,
        }
        self.assertEqual(
            caption_delivery._receipt_identity(receipt),
            {
                "provider_id": "ollama",
                "mode": "local_loopback",
                "model": "qwen2.5:7b",
                "config_sha256": "f" * 64,
                "consent_mode": "not_required_local",
                "consent_sha256": None,
            },
        )


class RetryAsksOnlyAboutTheBadCaptionTests(_DeliveryRoundtripCase):
    """The retry is a question about one caption, not about the delivery.

    Measured on a real delivery: qwen2.5:7b answered nine captions well and
    two badly, and a retry that re-asked all eleven came back with eleven
    untranslated Chinese lines — the rejection in the prompt pushed the
    model off the task for the captions that had nothing wrong with them.
    Re-asking is only worth doing if a good answer cannot be lost by it.
    """

    SOURCES = ("第一句中文 42km", "第二句中文 Notion")

    def test_only_the_rejected_caption_is_asked_about_again(self) -> None:
        asked: list[list[str]] = []

        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            asked.append([item["source"] for item in requested])
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        # Round 1 fails the second caption only (the brand
                        # is gone); round 2 answers whatever it is asked.
                        "translated_text": (
                            "first line 42km"
                            if item["source"].startswith("第一句")
                            else ("second line" if len(asked) == 1 else "second line Notion")
                        ),
                    }
                    for item in requested
                ]
            }

        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        self.assertEqual(len(asked), 2)
        self.assertEqual(asked[0], list(self.SOURCES))
        self.assertEqual(asked[1], [self.SOURCES[1]])
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            ["first line 42km", "second line Notion"],
        )
        self.assertEqual(artifact["provider_receipt"]["validation_retry_rounds"], 1)

    def test_the_good_answer_from_the_first_round_is_the_one_adopted(self) -> None:
        # The retry cannot overwrite a caption it was not asked about, even
        # if the provider volunteers a different answer for it.
        def model_call(prompt: str, _stage: str, **_kwargs) -> dict:
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return {
                "items": [
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
            }

        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=model_call
        )
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            ["first line 42km", "second line Notion"],
        )


class CaptionsOutsideTheCutTests(_DeliveryRoundtripCase):
    """A caption the cut does not use is not a caption to translate.

    The transcript covers the whole recording and the timeline covers the
    part that was kept, so an editor state routinely holds caption overlays
    that no segment touches — six of fifteen on the 30-second cut this was
    found on. `expected_instances` correctly asks the provider about the
    nine that are in the cut, and then the binding loop demanded a
    translation for all fifteen and died on the first one it could not find
    (`KeyError`, exit 1, a traceback instead of a message).

    Nothing had reached this line before: every earlier run failed during
    validation and never got as far as binding.
    """

    SOURCES = ("第一句中文 42km", "第二句中文 Notion")

    def setUp(self) -> None:
        super().setUp()
        # Keep only the first caption's second of source: the timeline is
        # what decides which captions exist in the cut.
        self.state["segments"] = [
            {
                "id": "kept",
                "source_start": 0.0,
                "source_end": 1.0,
                "origin": "default_full_source",
            }
        ]
        caption_delivery._atomic_write(
            self.project / "working/editor_state.json", self.state
        )

    def _model_call(self, prompt: str, _stage: str, **_kwargs) -> dict:
        requested = json.loads(prompt.rsplit("\n", 1)[-1])
        return {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": "first line 42km",
                }
                for item in requested
            ]
        }

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_a_caption_outside_the_timeline_does_not_break_the_delivery(self) -> None:
        artifact = caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=self._model_call
        )
        self.assertEqual(len(artifact["items"]), 1)
        self.assertEqual(artifact["items"][0]["translated_text"], "first line 42km")

    @unittest.skipUnless(cc.compositor_available(), "needs macOS CoreText")
    def test_only_the_captions_in_the_cut_are_given_a_translation(self) -> None:
        caption_delivery.create_delivery(
            self.project, "en", required=True, model_call=self._model_call
        )
        state = caption_delivery._load_json(
            self.project / "working/editor_state.json"
        )
        overlays = caption_delivery._caption_overlays(state)
        self.assertEqual(len(overlays), 2)
        self.assertEqual(overlays[0]["translation"], "first line 42km")
        # Not an empty translation and not a stale one: the caption the cut
        # never shows carries no bilingual line at all. It still carries the
        # artifact hash, because the render gate checks every overlay
        # against the adopted delivery.
        self.assertNotIn("translation", overlays[1])
        self.assertEqual(
            overlays[1]["caption_delivery_artifact_sha256"],
            overlays[0]["caption_delivery_artifact_sha256"],
        )


class ChineseNumberConversionTests(unittest.TestCase):
    """The converter on its own, before anything is built on top of it.

    It decides what counts as a number in the source, so its own mistakes
    become either a fact that slips through or a correct caption refused.
    Both directions are pinned here: what it must read, and what it must
    refuse to read as a number at all.
    """

    def _value(self, text: str):
        return caption_delivery.chinese_number_value(text)

    def test_it_reads_the_ordinary_compounds(self) -> None:
        self.assertEqual(self._value("三十"), "30")
        self.assertEqual(self._value("二十四"), "24")
        self.assertEqual(self._value("一百二十三"), "123")
        self.assertEqual(self._value("兩千"), "2000")
        self.assertEqual(self._value("一萬"), "10000")
        self.assertEqual(self._value("三萬五千"), "35000")
        self.assertEqual(self._value("十五"), "15")

    def test_a_bare_ten_reads_as_ten(self) -> None:
        # 十 with nothing in front is 10, not 1: the leading one is implied
        # in Chinese and dropping it would demand the wrong number.
        self.assertEqual(self._value("十八"), "18")

    def test_it_refuses_single_characters(self) -> None:
        # Deliberately blind, and this is the load-bearing decision in the
        # class. 一 is the most common character in Chinese subtitles and
        # almost never a quantity — 一起, 一定, 一直, 第一次. Reading it as
        # "1" would demand a digit in the translation of ordinary speech
        # and refuse every correct answer. A single character carries too
        # little signal to spend a fail-closed on.
        self.assertIsNone(self._value("一"))
        self.assertIsNone(self._value("十"))
        self.assertIsNone(self._value("兩"))

    def test_it_refuses_small_compounds_that_are_usually_idioms(self) -> None:
        # Same reasoning one step out: 萬一 ("in case") and 一一 ("one by
        # one") parse arithmetically to 1, and a compound worth enforcing
        # is one that reached ten. Under ten the idiom rate is higher than
        # the quantity rate, so these stay unread.
        self.assertIsNone(self._value("萬一"))
        self.assertIsNone(self._value("一一"))

    def test_it_refuses_text_that_is_not_a_numeral_run(self) -> None:
        self.assertIsNone(self._value(""))
        self.assertIsNone(self._value("公里"))

    def test_percentages_carry_their_sign(self) -> None:
        self.assertEqual(caption_delivery.chinese_percent_value("百分之八十七"), "87%")
        self.assertEqual(caption_delivery.chinese_percent_value("百分之五"), "5%")
        self.assertIsNone(caption_delivery.chinese_percent_value("八十七"))

    def test_the_source_scan_returns_values_in_source_order(self) -> None:
        self.assertEqual(
            caption_delivery.source_number_sequence("三十公里，開了二十四小時"),
            ["30", "24"],
        )
        self.assertEqual(
            caption_delivery.source_number_sequence("12 小時賺 24 美元"),
            ["12", "24"],
        )
        self.assertEqual(
            caption_delivery.source_number_sequence("成長 1,200 到百分之八十七"),
            ["1200", "87"],
        )

    def test_the_source_scan_skips_the_characters_the_converter_refused(self) -> None:
        self.assertEqual(caption_delivery.source_number_sequence("我們一起走"), [])
        self.assertEqual(caption_delivery.source_number_sequence("十分重要"), [])
        self.assertEqual(caption_delivery.source_number_sequence("萬一失敗"), [])


class ChineseNumberBestEffortTests(_OneCaptionCase):
    """Numbers the source spelled in Chinese: read, reported, not enforced.

    This class used to require a matching digit in the answer whenever the
    source spelled a number in Chinese, and the requirement was reversed on
    2026-08-13 (SPEC v1.4). The reason is in the acceptance cases below:
    ordinary, correct English does not put the source's digit where Chinese
    put its numeral, and the rule refused so much good work that its real
    effect was to make the whole preservation gate unusable on Chinese
    speech. What survives is the half that can be read reliably — digits in
    the source — plus an advisory list for a human.

    nat reviews the cut. A best-effort reading is a fine thing to show a
    reviewer and a bad thing to fail a delivery on.
    """

    def test_a_chinese_number_carried_over_correctly_is_accepted(self) -> None:
        self._accepts("時速三十公里", "thirty kilometres per hour: 30 km/h")
        self._accepts("時速三十公里", "30 kilometres per hour")

    def test_a_chinese_number_changed_in_translation_is_no_longer_blocking(self) -> None:
        # Was a rejection (translation_token_missing). It is a real defect
        # and it is now nat's to catch on review: the alternative was
        # failing every caption in the acceptance set below.
        self._accepts("時速三十公里", "fifty kilometres per hour")

    def test_a_chinese_number_dropped_entirely_is_no_longer_blocking(self) -> None:
        self._accepts("我們花了兩千塊", "we spent quite a lot")

    def test_a_chinese_percentage_is_no_longer_blocking(self) -> None:
        self._accepts("轉換率成長百分之八十七", "conversion grew 87%")
        self._accepts("轉換率成長百分之八十七", "conversion grew 90%")

    def test_the_advisory_still_names_what_it_could_not_find(self) -> None:
        # Downgraded, not deleted: the reading is still available to
        # whatever wants to put it in front of a person.
        self.assertEqual(
            caption_delivery.chinese_number_advisories(
                "時速三十公里", "fifty kilometres per hour"
            ),
            ["30"],
        )
        self.assertEqual(
            caption_delivery.chinese_number_advisories(
                "時速三十公里", "30 kilometres per hour"
            ),
            [],
        )

    def test_ordinary_chinese_without_numbers_is_left_alone(self) -> None:
        # The mis-kill guard for the whole class: these are the sentences a
        # real transcript is made of, and none of them may start demanding
        # digits.
        self._accepts("我們一起來看看", "let us take a look together")
        self._accepts("這件事十分重要", "this matters a great deal")
        self._accepts("萬一失敗了怎麼辦", "what if it does not work out")


class ChineseNumberFalsePositiveTests(_OneCaptionCase):
    """The answers the old hard rule refused, every one of them correct.

    These are the counterexamples the 2026-08-13 ruling was decided on.
    They are kept as a set because the failure mode they describe is
    collective: any one of them looks like an edge case worth paying for,
    and all of them together are simply "English".
    """

    def test_a_chinese_numeral_translated_as_a_digit_is_accepted(self) -> None:
        self._accepts("十年前我還在上班", "10 years ago I still had a job")
        self._accepts("擠進前十名", "Top 10")

    def test_a_digit_the_source_never_spelled_out_is_accepted(self) -> None:
        # 二十四小時 -> "24/7" reorganises the number entirely, and 十點
        # 三十分 -> "10:30" splits one clock time into two tokens.
        self._accepts("我們二十四小時營業", "Open 24/7")
        self._accepts("十點三十分開會", "Meeting at 10:30")

    def test_a_fraction_is_accepted(self) -> None:
        self._accepts("大概三分之一的人", "About 1/3 of people")

    def test_an_idiom_that_parses_as_a_number_is_accepted(self) -> None:
        # 千萬 is "whatever you do", not ten million.
        self._accepts("千萬別忘記", "Do not forget")

    def test_a_percentage_that_changes_shape_is_accepted(self) -> None:
        # 零點五 needs a decimal point the numeral run does not contain,
        # and 百分之八十七 is just as correctly written "87 percent".
        self._accepts("成功率百分之零點五", "The rate is 0.5%")
        self._accepts("百分之八十七的人", "87 percent of people")


class ArabicNumberEnforcementSurvivesTests(_OneCaptionCase):
    """The downgrade did not reach the source's own digits.

    A source that writes its numbers in digits is readable, so every
    accusation stays available against it. This is the boundary of the
    2026-08-13 ruling and the test that would catch it being widened.
    """

    def test_an_invented_number_is_still_rejected(self) -> None:
        self._rejects(
            "他 3 天賺 300 元",
            "he made 300 dollars in 3 days, up 500%",
            "translation_number_invented",
        )

    def test_a_reordered_pair_of_digits_is_still_rejected(self) -> None:
        self._rejects(
            "12 小時賺 24 美元",
            "24 hours for 12 dollars",
            "translation_number_order",
        )

    def test_a_dropped_digit_is_still_rejected(self) -> None:
        self._rejects(
            "他 3 天賺 300 元",
            "he made a lot in 3 days",
            "translation_token_missing",
        )


class NumberFidelityTests(_OneCaptionCase):
    """What the set comparison could not see.

    The token rule compared casefolded *sets*, which loses three things at
    once: the case that distinguishes a unit (5mW is a phone charger, 5MW
    is a power station), how many times a value appears and in what order,
    and anything the translation added that the source never said. Each of
    those is a wrong caption that rendered exit 0.
    """

    def test_a_unit_that_changes_case_is_rejected(self) -> None:
        self._rejects(
            "這座電廠只有 5mW",
            "this plant is only 5MW",
            "translation_token_missing",
        )

    def test_a_unit_that_keeps_its_case_is_accepted(self) -> None:
        self._accepts("這座電廠只有 5mW", "this plant is only 5mW")

    def test_brand_case_is_still_not_a_mutation(self) -> None:
        # The line between the two: case matters where it changes what a
        # symbol means, and a brand shouted at the start of a sentence
        # still means the brand.
        self._accepts("我們用 Notion 記錄", "WE KEEP OUR NOTES IN NOTION")

    def test_swapped_numbers_are_rejected(self) -> None:
        # Both values survive, so a set comparison sees a perfect answer;
        # the caption says the pay is the hours and the hours are the pay.
        self._rejects(
            "12 小時賺 24 美元",
            "24 hours for 12 dollars",
            "translation_number_order",
        )

    def test_numbers_in_the_same_order_are_accepted(self) -> None:
        self._accepts("12 小時賺 24 美元", "12 hours for 24 dollars")

    def test_a_repeated_number_must_stay_repeated(self) -> None:
        self._rejects(
            "3 天 3 夜",
            "3 days and 4 nights",
            "translation_token_missing",
        )

    def test_a_number_the_source_never_said_is_rejected(self) -> None:
        # The direction nothing checked: a 7B model padding a translation
        # with a plausible statistic. Every number on screen has to come
        # from the speaker.
        self._rejects(
            "轉換率成長了",
            "conversion grew by 300%",
            "translation_number_invented",
        )

    def test_an_invented_number_is_caught_even_next_to_a_real_one(self) -> None:
        self._rejects(
            "成長了 87%",
            "it grew 87% in 2026",
            "translation_number_invented",
        )

    def test_a_number_traceable_to_a_chinese_numeral_is_not_invented(self) -> None:
        # The interaction with the class above: the digits in the answer
        # have no ASCII counterpart in the source, and they are still the
        # speaker's own number.
        self._accepts("時速三十公里", "30 kilometres per hour")

    def test_fullwidth_and_ascii_digits_are_the_same_number_both_ways(self) -> None:
        # The interaction with the normalising above: the reverse check
        # must not call an ASCII answer invented because the source wrote
        # the same number fullwidth.
        self._accepts("成長了 ８７%", "it grew 87%")


class SingleLetterInsideAChineseWordTests(_OneCaptionCase):
    """A lone Latin letter glued to Chinese is part of the Chinese word.

    Found on a real cut: 「三根K棒找結構高點」 is one Chinese noun, K棒 =
    candlestick, and every natural English translation of it says
    "candlestick" — the letter K has no counterpart to carry. Requiring it
    made the caption an unsatisfiable contract: no correct answer exists,
    so the delivery could only fail closed or be turned off.

    The exemption is deliberately narrow: one letter, no digits, and
    touching Chinese on at least one side. Everything the rule was written
    to catch — multi-letter brands, units like 5mW, and a lone letter in a
    Latin sentence where it really is a token of its own — is untouched.
    """

    def test_a_letter_fused_into_a_chinese_word_need_not_survive(self) -> None:
        self._accepts(
            "三根K棒找結構高點", "Find structure highs with three candlesticks"
        )

    def test_the_same_letter_may_still_be_carried_over(self) -> None:
        # Exempt means "not required", not "not allowed": an answer that
        # does keep the letter is still a good answer.
        self._accepts("三根K棒找結構高點", "Find structure highs with three K bars")

    def test_a_letter_touching_chinese_on_one_side_only_is_exempt(self) -> None:
        self._accepts("這是 A 級的", "this one is top grade")
        self._accepts("看 K 就知道", "you can tell from the chart")

    # --- the rule still has teeth ----------------------------------------

    def test_a_multi_letter_token_inside_a_chinese_word_is_still_required(self) -> None:
        self._rejects(
            "用RSI指標判斷",
            "judge it with the momentum indicator",
            "translation_token_missing",
        )

    def test_a_unit_next_to_chinese_is_still_required(self) -> None:
        self._rejects(
            "這顆充電器只有5mW",
            "this charger is only five milliwatts",
            "translation_token_missing",
        )

    def test_a_lone_letter_in_a_latin_sentence_is_still_required(self) -> None:
        self._rejects(
            "我們改用 plan B 執行",
            "we switched to the backup plan",
            "translation_token_missing",
        )

    def test_a_letter_with_a_digit_glued_to_chinese_is_still_required(self) -> None:
        self._rejects(
            "先看B1區的量",
            "look at the volume in the lower zone first",
            "translation_token_missing",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from contextual_semantic_calibration import (  # noqa: E402
    build_context_units,
    ollama_json_model_call,
    propose_contextual_corrections,
    validate_contextual_proposals,
)


class ContextualSemanticCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transcript = {
            "words": [
                {
                    "id": "word-00001",
                    "text": "後面的不定詞片語才是真正主詞。",
                    "start": 0.0,
                    "end": 1.0,
                },
                {
                    "id": "word-00002",
                    "text": "這句很長，",
                    "start": 1.0,
                    "end": 2.0,
                },
                {
                    "id": "word-00003",
                    "text": "不要頭重小琴。",
                    "start": 2.0,
                    "end": 3.0,
                },
                {
                    "id": "word-00004",
                    "text": "如果頭再大一點就會翻過來。",
                    "start": 3.0,
                    "end": 4.0,
                },
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "後面的不定詞片語才是真正主詞。",
                    "word_ids": ["word-00001"],
                },
                {
                    "id": "caption-segment-0002",
                    "start": 1.0,
                    "end": 2.0,
                    "text": "這句很長，",
                    "word_ids": ["word-00002"],
                },
                {
                    "id": "caption-segment-0003",
                    "start": 2.0,
                    "end": 3.0,
                    "text": "不要頭重小琴。",
                    "word_ids": ["word-00003"],
                },
                {
                    "id": "caption-segment-0004",
                    "start": 3.0,
                    "end": 4.0,
                    "text": "如果頭再大一點就會翻過來。",
                    "word_ids": ["word-00004"],
                },
            ],
        }

    def test_every_unit_has_bounded_previous_and_next_context(self) -> None:
        units = build_context_units(self.transcript, context_radius=2)

        self.assertEqual(len(units), 4)
        self.assertEqual(units[0]["previous"], [])
        self.assertEqual(
            [item["id"] for item in units[0]["next"]],
            ["caption-segment-0002", "caption-segment-0003"],
        )
        self.assertEqual(
            [item["id"] for item in units[2]["previous"]],
            ["caption-segment-0001", "caption-segment-0002"],
        )
        self.assertEqual(
            [item["id"] for item in units[2]["next"]],
            ["caption-segment-0004"],
        )

    def test_only_verified_minimal_asr_patches_are_scoped_for_application(self) -> None:
        payload = {
            "reviewed_unit_ids": [
                "caption-segment-0001",
                "caption-segment-0002",
                "caption-segment-0003",
                "caption-segment-0004",
            ],
            "items": [
                {
                    "unit_id": "caption-segment-0003",
                    "source": "小琴",
                    "replacement": "腳輕",
                    "category": "idiom",
                    "reason": "後文說頭太大會翻過來，固定用語應為頭重腳輕。",
                    "confidence": 0.98,
                    "verifier_decision": "accept",
                    "verifier_confidence": 0.97,
                },
                {
                    "unit_id": "caption-segment-0002",
                    "source": "這句",
                    "replacement": "此句",
                    "category": "style",
                    "reason": "只是改成書面語。",
                    "confidence": 0.99,
                    "verifier_decision": "accept",
                    "verifier_confidence": 0.99,
                },
                {
                    "unit_id": "caption-segment-0004",
                    "source": "一點",
                    "replacement": "兩點",
                    "category": "number",
                    "reason": "沒有音訊證據的數字改寫。",
                    "confidence": 0.99,
                    "verifier_decision": "accept",
                    "verifier_confidence": 0.99,
                },
                {
                    "unit_id": "caption-segment-0004",
                    "source": "翻",
                    "replacement": "倒",
                    "category": "homophone",
                    "reason": "兩種說法都可能。",
                    "confidence": 0.75,
                    "verifier_decision": "uncertain",
                    "verifier_confidence": 0.60,
                },
            ],
        }

        result = validate_contextual_proposals(
            self.transcript,
            payload,
            glossary=["It", "to V"],
            minimum_confidence=0.92,
        )

        self.assertEqual(result["coverage_status"], "complete")
        self.assertEqual(result["reviewed_unit_count"], 4)
        self.assertEqual(result["total_unit_count"], 4)
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["rejected_count"], 2)
        self.assertEqual(
            result["rules"],
            [
                {
                    "canonical": "腳輕",
                    "aliases": ["小琴"],
                    "start": 2.0,
                    "end": 3.0,
                    "unit_id": "caption-segment-0003",
                    "provenance": "contextual_semantic_calibration",
                }
            ],
        )
        accepted = result["accepted"][0]
        self.assertEqual(accepted["source"], "小琴")
        self.assertEqual(accepted["replacement"], "腳輕")
        self.assertEqual(accepted["source_word_ids"], ["word-00003"])

    def test_rewrites_punctuation_and_word_choice_cannot_auto_apply(self) -> None:
        result = validate_contextual_proposals(
            self.transcript,
            {
                "reviewed_unit_ids": [
                    "caption-segment-0001",
                    "caption-segment-0002",
                    "caption-segment-0003",
                    "caption-segment-0004",
                ],
                "items": [
                    {
                        "unit_id": "caption-segment-0003",
                        "source": "不要頭重小琴。",
                        "replacement": "不要頭重腳輕。",
                        "category": "idiom",
                        "reason": "把未變動上下文包進 patch。",
                        "confidence": 0.99,
                        "verifier_decision": "accept",
                        "verifier_confidence": 0.99,
                    },
                    {
                        "unit_id": "caption-segment-0002",
                        "source": "，",
                        "replacement": "。",
                        "category": "typo",
                        "reason": "只改標點。",
                        "confidence": 1.0,
                        "verifier_decision": "accept",
                        "verifier_confidence": 1.0,
                    },
                    {
                        "unit_id": "caption-segment-0002",
                        "source": "很長",
                        "replacement": "冗贅",
                        "category": "word_choice",
                        "reason": "只是潤飾用字。",
                        "confidence": 0.99,
                        "verifier_decision": "accept",
                        "verifier_confidence": 0.99,
                    },
                ],
            },
            glossary=[],
        )

        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["pending"][0]["pending_reason"], "word_choice_requires_human")
        self.assertEqual(
            {item["reject_reason"] for item in result["rejected"]},
            {"patch_contains_unchanged_context", "punctuation_changed"},
        )

    def test_incomplete_review_cannot_claim_full_context_coverage(self) -> None:
        result = validate_contextual_proposals(
            self.transcript,
            {
                "reviewed_unit_ids": ["caption-segment-0003"],
                "items": [],
            },
            glossary=[],
            minimum_confidence=0.92,
        )

        self.assertEqual(result["coverage_status"], "partial")
        self.assertEqual(result["reviewed_unit_count"], 1)
        self.assertEqual(result["total_unit_count"], 4)

    def test_verifier_rejection_is_not_downgraded_to_pending_by_low_confidence(self) -> None:
        result = validate_contextual_proposals(
            self.transcript,
            {
                "reviewed_unit_ids": ["caption-segment-0003"],
                "items": [
                    {
                        "unit_id": "caption-segment-0003",
                        "source": "小琴",
                        "replacement": "腳輕",
                        "category": "idiom",
                        "reason": "候選遭複核拒絕。",
                        "confidence": 0.8,
                        "verifier_decision": "reject",
                        "verifier_confidence": 0.8,
                    }
                ],
            },
            glossary=[],
        )

        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["rejected"][0]["reject_reason"], "verifier_rejected")

    def test_english_patch_keeps_caption_spacing_and_word_timing(self) -> None:
        transcript = {
            "words": [
                {"id": "word-00001", "text": "老師用", "start": 0.0, "end": 0.4},
                {"id": "word-00002", "text": "It", "start": 0.4, "end": 0.7},
                {"id": "word-00003", "text": "is", "start": 0.7, "end": 1.0},
                {"id": "word-00004", "text": "當虛主詞", "start": 1.0, "end": 1.5},
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "start": 0.0,
                    "end": 1.5,
                    "text": "老師用 It is 當虛主詞",
                    "word_ids": [
                        "word-00001",
                        "word-00002",
                        "word-00003",
                        "word-00004",
                    ],
                }
            ],
        }
        result = validate_contextual_proposals(
            transcript,
            {
                "reviewed_unit_ids": ["caption-segment-0001"],
                "items": [
                    {
                        "unit_id": "caption-segment-0001",
                        "source": "It is",
                        "replacement": "It's",
                        "category": "grammar_term",
                        "reason": "術語表與句法上下文均支持縮寫。",
                        "confidence": 0.99,
                        "verifier_decision": "accept",
                        "verifier_confidence": 0.98,
                    }
                ],
            },
            glossary=["It's"],
        )

        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["accepted"][0]["start"], 0.4)
        self.assertEqual(result["accepted"][0]["end"], 1.0)
        self.assertEqual(
            result["accepted"][0]["source_word_ids"],
            ["word-00002", "word-00003"],
        )

    def test_model_pass_reviews_every_unit_then_verifies_each_patch(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_model_call(prompt: str, stage: str) -> dict[str, object]:
            calls.append((stage, prompt))
            if stage == "propose":
                self.assertIn("後面的不定詞片語才是真正主詞", prompt)
                self.assertIn("如果頭再大一點就會翻過來", prompt)
                return {
                    "items": [
                        {
                            "unit_id": "caption-segment-0003",
                            "source": "頭重小琴",
                            "replacement": "頭重腳輕",
                            "category": "idiom",
                            "reason": "前後文是固定成語與失衡比喻。",
                            "confidence": 0.98,
                        }
                    ]
                }
            self.assertEqual(stage, "verify")
            return {
                "items": [
                    {
                        "unit_id": "caption-segment-0003",
                        "source": "頭重小琴",
                        "replacement": "頭重腳輕",
                        "decision": "accept",
                        "confidence": 0.97,
                        "reason": "固定成語且由後句支持。",
                    }
                ]
            }

        payload = propose_contextual_corrections(
            self.transcript,
            glossary=["It", "to V"],
            model_call=fake_model_call,
            batch_size=10,
        )

        self.assertEqual(
            payload["reviewed_unit_ids"],
            [
                "caption-segment-0001",
                "caption-segment-0002",
                "caption-segment-0003",
                "caption-segment-0004",
            ],
        )
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["verifier_decision"], "accept")
        self.assertEqual(payload["items"][0]["verifier_confidence"], 0.97)
        self.assertEqual([stage for stage, _prompt in calls], ["propose", "verify"])

    def test_each_batch_receives_whole_document_context(self) -> None:
        prompts: list[str] = []
        progress: list[dict[str, int]] = []

        def fake_model_call(prompt: str, stage: str) -> dict[str, object]:
            self.assertEqual(stage, "propose")
            prompts.append(prompt)
            return {"items": []}

        payload = propose_contextual_corrections(
            self.transcript,
            glossary=[],
            model_call=fake_model_call,
            batch_size=1,
            progress_callback=progress.append,
        )

        self.assertEqual(len(prompts), 4)
        self.assertIn("後面的不定詞片語才是真正主詞", prompts[-1])
        self.assertIn("如果頭再大一點就會翻過來", prompts[0])
        self.assertEqual(len(payload["reviewed_unit_ids"]), 4)
        self.assertEqual(
            [item["reviewed_unit_count"] for item in progress],
            [1, 2, 3, 4],
        )

    def test_failed_second_review_cannot_claim_batch_coverage(self) -> None:
        def fake_model_call(_prompt: str, stage: str) -> dict[str, object]:
            if stage == "propose":
                return {
                    "items": [
                        {
                            "unit_id": "caption-segment-0003",
                            "source": "小琴",
                            "replacement": "腳輕",
                            "category": "idiom",
                            "reason": "全文上下文支持固定成語。",
                            "confidence": 0.98,
                        }
                    ]
                }
            raise RuntimeError("local verifier stopped")

        payload = propose_contextual_corrections(
            self.transcript,
            glossary=[],
            model_call=fake_model_call,
            batch_size=10,
        )
        result = validate_contextual_proposals(
            self.transcript,
            payload,
            glossary=[],
        )

        self.assertEqual(payload["reviewed_unit_ids"], [])
        self.assertEqual(payload["errors"][0]["stage"], "verify")
        self.assertEqual(result["coverage_status"], "partial")
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["rejected"][0]["reject_reason"], "unit_not_reviewed")

    def test_patch_cannot_invent_duplicate_at_caption_boundary(self) -> None:
        transcript = {
            "words": [
                {"id": "word-00001", "text": "好再來往", "start": 0.0, "end": 1.0},
                {"id": "word-00002", "text": "下看", "start": 1.0, "end": 2.0},
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "好再來往",
                    "word_ids": ["word-00001"],
                },
                {
                    "id": "caption-segment-0002",
                    "start": 1.0,
                    "end": 2.0,
                    "text": "下看",
                    "word_ids": ["word-00002"],
                },
            ],
        }
        result = validate_contextual_proposals(
            transcript,
            {
                "reviewed_unit_ids": [
                    "caption-segment-0001",
                    "caption-segment-0002",
                ],
                "items": [
                    {
                        "unit_id": "caption-segment-0001",
                        "source": "往",
                        "replacement": "下",
                        "category": "typo",
                        "reason": "模型忽略了下一個字幕已由下字開頭。",
                        "confidence": 0.99,
                        "verifier_decision": "accept",
                        "verifier_confidence": 0.99,
                    }
                ],
            },
            glossary=[],
        )

        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(
            result["rejected"][0]["reject_reason"],
            "cross_caption_duplicate_created",
        )

    def test_zero_duration_source_word_gets_a_bounded_scope(self) -> None:
        transcript = {
            "words": [
                {
                    "id": "word-00001",
                    "text": "cigarette",
                    "start": 1.0,
                    "end": 1.0,
                }
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "start": 0.5,
                    "end": 1.5,
                    "text": "cigarette",
                    "word_ids": ["word-00001"],
                }
            ],
        }
        result = validate_contextual_proposals(
            transcript,
            {
                "reviewed_unit_ids": ["caption-segment-0001"],
                "items": [
                    {
                        "unit_id": "caption-segment-0001",
                        "source": "cigarette",
                        "replacement": "cigar",
                        "category": "domain_term",
                        "reason": "相鄰完整例句與原始術語均支持 cigar。",
                        "confidence": 0.99,
                        "verifier_decision": "accept",
                        "verifier_confidence": 0.99,
                    }
                ],
            },
            glossary=["cigar", "cigarette"],
        )

        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["accepted"][0]["start"], 0.999)
        self.assertEqual(result["accepted"][0]["end"], 1.001)

    def test_ollama_provider_rejects_non_loopback_hosts_before_network(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_HOST": "https://example.com:11434"}):
            with self.assertRaisesRegex(ValueError, "only permits loopback"):
                ollama_json_model_call("{}", "propose", model="qwen2.5:7b")

    def test_ollama_request_bounds_context_and_generated_output(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"message": {"content": json.dumps({"items": []})}}
                ).encode("utf-8")

        with patch.dict("os.environ", {"OLLAMA_HOST": "http://127.0.0.1:11434"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as opened:
                result = ollama_json_model_call(
                    "{}",
                    "propose",
                    model="qwen2.5:7b",
                )

        request = opened.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"items": []})
        self.assertEqual(body["options"]["num_ctx"], 16384)
        self.assertEqual(body["options"]["num_predict"], 1536)

    def test_the_first_attempt_is_still_pinned_and_a_retry_is_not(self) -> None:
        # Determinism is the default the rest of the pipeline is built on,
        # so attempt 0 does not move. A retry has to: re-asking a model
        # pinned to temperature 0 and seed 42 returns the previous answer
        # word for word, which makes a bounded retry a bill rather than a
        # second sample. Observed — two retries, three identical rejected
        # answers, one dead cut.
        first = self._options_for(attempt=0)
        self.assertEqual(first["temperature"], 0)
        self.assertEqual(first["seed"], 42)
        retry = self._options_for(attempt=1)
        self.assertNotEqual(retry["seed"], first["seed"])
        self.assertGreater(retry["temperature"], 0)
        self.assertNotEqual(self._options_for(attempt=2)["seed"], retry["seed"])

    @classmethod
    def _options_for(cls, *, attempt: int) -> dict:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"message": {"content": json.dumps({"items": []})}}
                ).encode("utf-8")

        with patch.dict("os.environ", {"OLLAMA_HOST": "http://127.0.0.1:11434"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as opened:
                ollama_json_model_call(
                    "{}", "caption_translation", model="qwen2.5:7b", attempt=attempt
                )
        return json.loads(opened.call_args.args[0].data.decode("utf-8"))["options"]

    @staticmethod
    def _system_message_for(stage: str) -> str:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"message": {"content": json.dumps({"items": []})}}
                ).encode("utf-8")

        with patch.dict("os.environ", {"OLLAMA_HOST": "http://127.0.0.1:11434"}):
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as opened:
                ollama_json_model_call("{}", stage, model="qwen2.5:7b")
        body = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        return body["messages"][0]["content"]

    def test_the_correction_stages_keep_their_preserve_the_source_system_prompt(
        self,
    ) -> None:
        # This is the right instruction for the job it was written for:
        # proposing spelling corrections, where anything not explicitly
        # corrected must come back untouched.
        system = self._system_message_for("propose")
        self.assertIn("Preserve", system)
        self.assertIn("source wording", system)

    def test_the_translation_stage_is_not_told_to_preserve_the_source_wording(
        self,
    ) -> None:
        # The same system prompt was being sent for translation, where it
        # says the opposite of the task: "preserve Taiwan Traditional
        # Chinese, source wording, numbers, punctuation ... unless
        # explicitly corrected" is an instruction to hand the Chinese back.
        #
        # That is exactly what qwen2.5:7b did on a real cut — every caption
        # returned verbatim, `translation_unchanged`, no video — and it is
        # why the delivery's own retry could not save it: re-asking with a
        # system prompt that contradicts the request only buys the same
        # answer again.
        system = self._system_message_for("caption_translation")
        self.assertNotIn("source wording", system)
        self.assertIn("translat", system.casefold())


if __name__ == "__main__":
    unittest.main()


class ScriptConversionGuardTests(unittest.TestCase):
    """A model may not quietly restyle the project into another script."""

    def test_a_simplified_rewrite_is_not_a_correction(self) -> None:
        from contextual_semantic_calibration import _is_script_conversion

        # Observed from a local run: the model reported a Traditional spelling
        # as a typo and offered the Simplified form at 0.95 confidence.
        self.assertTrue(_is_script_conversion("復興 4 號", "复兴 4 号"))
        self.assertTrue(_is_script_conversion("開啟", "开启"))

    def test_a_real_mishearing_still_gets_through(self) -> None:
        from contextual_semantic_calibration import _is_script_conversion

        for source, replacement in (
            ("別摘在家", "別宅在家"),
            ("中校復興", "捷運復興"),
            ("台灣", "臺灣"),
        ):
            with self.subTest(f"{source}->{replacement}"):
                self.assertFalse(_is_script_conversion(source, replacement))

    def test_an_unchanged_patch_is_not_treated_as_conversion(self) -> None:
        from contextual_semantic_calibration import _is_script_conversion

        self.assertFalse(_is_script_conversion("復興", "復興"))

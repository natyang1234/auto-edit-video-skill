"""Phase 0c public caption-delivery-v2 contract tests."""
from __future__ import annotations

import copy
import hashlib
import http.server
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts/auto_edit.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import auto_edit  # noqa: E402
import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402


class PublicCaptionDeliveryParserTests(unittest.TestCase):
    def test_public_translate_captions_command_is_registered(self) -> None:
        args = auto_edit.build_parser().parse_args(
            [
                "translate-captions",
                "--project-dir",
                "/tmp/caption-delivery-fixture",
                "--language",
                "en",
                "--required",
            ]
        )
        self.assertEqual(args.command, "translate-captions")
        self.assertTrue(args.required)


class _FakePopenStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.close_calls = 0

    def read(self, _size: int) -> bytes:
        return next(self._chunks)

    def close(self) -> None:
        self.close_calls += 1


class _FakePopen:
    def __init__(self, *, returncode: int = 0, timeout: bool = False) -> None:
        self.stdout = _FakePopenStdout([b"decoded pcm", b""])
        self._returncode = returncode
        self._timeout = timeout
        self._running = True
        self.wait_calls: list[int | None] = []
        self.kill_calls = 0

    def poll(self) -> int | None:
        return None if self._running else self._returncode

    def wait(self, timeout: int | None = None) -> int:
        self.wait_calls.append(timeout)
        if timeout is not None and self._timeout:
            raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
        self._running = False
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9


class CaptionDeliveryPcmProcessCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.source = self.project / "source.media"
        self.source.write_bytes(b"source media")
        self.manifest = {
            "source": {
                "staged_path": self.source.name,
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            }
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _decode(self, process: _FakePopen) -> str:
        with patch.object(caption_delivery.shutil, "which", return_value="ffmpeg"), patch.object(
            caption_delivery.subprocess, "Popen", return_value=process
        ):
            return caption_delivery.decoded_pcm_sha256(self.project, self.manifest)

    def test_success_closes_stdout_after_reaping_process(self) -> None:
        process = _FakePopen()

        digest = self._decode(process)

        self.assertEqual(digest, hashlib.sha256(b"decoded pcm").hexdigest())
        self.assertEqual(process.stdout.close_calls, 1)
        self.assertEqual(process.wait_calls, [3600])
        self.assertEqual(process.kill_calls, 0)

    def test_nonzero_exit_closes_stdout_and_preserves_error(self) -> None:
        process = _FakePopen(returncode=1)

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as raised:
            self._decode(process)

        self.assertEqual(raised.exception.code, "pcm_decode_failed")
        self.assertEqual(process.stdout.close_calls, 1)
        self.assertEqual(process.wait_calls, [3600])
        self.assertEqual(process.kill_calls, 0)

    def test_timeout_kills_reaps_and_closes_stdout(self) -> None:
        process = _FakePopen(timeout=True)

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as raised:
            self._decode(process)

        self.assertEqual(raised.exception.code, "pcm_decode_failed")
        self.assertEqual(process.stdout.close_calls, 1)
        self.assertEqual(process.wait_calls, [3600, None])
        self.assertEqual(process.kill_calls, 1)


class CaptionDeliveryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / "working/transcript_sources").mkdir(parents=True)
        self.manifest = {
            "schema_version": 1,
            "project_id": "caption-v2-test",
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
                {"id": "word-00001", "text": "重複中文", "start": 0.0, "end": 1.0},
                {"id": "word-00002", "text": "重複中文", "start": 2.0, "end": 3.0},
            ],
            "caption_segments": [
                {
                    "id": "caption-segment-0001",
                    "text": "重複中文",
                    "start": 0.0,
                    "end": 1.0,
                    "word_ids": ["word-00001"],
                },
                {
                    "id": "caption-segment-0002",
                    "text": "重複中文",
                    "start": 2.0,
                    "end": 3.0,
                    "word_ids": ["word-00002"],
                },
            ],
        }
        self.state = {
            "schema_version": 2,
            "project_id": "caption-v2-test",
            "segments": [
                {
                    "id": "full",
                    "source_start": 0.0,
                    "source_end": 4.0,
                    "origin": "default_full_source",
                }
            ],
            "overlays": [
                {
                    "id": "caption-0001",
                    "type": "caption",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "重複中文",
                    "visible": True,
                    "source": "working/transcript_words.json",
                },
                {
                    "id": "caption-0002",
                    "type": "caption",
                    "start": 2.0,
                    "end": 3.0,
                    "text": "重複中文",
                    "visible": True,
                    "source": "working/transcript_words.json",
                },
            ],
        }
        self._write_source_revision("a" * 64)
        self._write_project()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source_revision(self, revision_seed: str) -> None:
        payload = {
            "schema_version": 1,
            "revision": "",
            "source_media_sha256": revision_seed,
            "audio_stream_index": 0,
            "decoded_pcm": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_format": "s16le",
                "sha256": "b" * 64,
            },
            "engine": "openai-whisper",
            "engine_version": "1.0",
            "model": "base",
            "language": "zh",
            "decoding_params": {},
            "source_generation": 0,
            "raw_words": [
                {"source_word_index": 0, "start_us": 0, "end_us": 1_000_000, "text": "重複中文", "speaker": None},
                {"source_word_index": 1, "start_us": 2_000_000, "end_us": 3_000_000, "text": "重複中文", "speaker": None},
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

    @staticmethod
    def _fake_model(prompt: str, stage: str, **_kwargs):
        requested = json.loads(prompt.rsplit("\n", 1)[-1])
        return {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": f"English line {chr(ord('A') + index)}",
                }
                for index, item in enumerate(requested)
            ]
        }

    def _create(self) -> dict:
        return caption_delivery.create_delivery(
            self.project,
            "en",
            required=True,
            model_call=self._fake_model,
        )

    def test_duplicate_source_text_keeps_distinct_source_and_instance_ids(self) -> None:
        artifact = self._create()
        self.assertEqual(len(artifact["items"]), 2)
        self.assertEqual(len({item["caption_source_id"] for item in artifact["items"]}), 2)
        self.assertEqual(len({item["caption_instance_id"] for item in artifact["items"]}), 2)
        self.assertEqual(
            [item["translated_text"] for item in artifact["items"]],
            ["English line A", "English line B"],
        )
        self.assertEqual(artifact["provider_receipt"]["mode"], "local_loopback")
        self.assertEqual(artifact["provider_receipt"]["consent_mode"], "not_required_local")
        self.assertIsNone(artifact["provider_receipt"]["consent_sha256"])

    def test_correction_only_keeps_ids_but_changes_corrected_source_hash(self) -> None:
        first = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        corrected = copy.deepcopy(self.transcript)
        corrected["caption_segments"][0]["text"] = "修正中文"
        second = caption_delivery.expected_instances(self.project, corrected, self.state)
        self.assertEqual(
            [item["caption_source_id"] for item in first["sources"]],
            [item["caption_source_id"] for item in second["sources"]],
        )
        self.assertEqual(
            first["segmentation"]["segmentation_revision"],
            second["segmentation"]["segmentation_revision"],
        )
        self.assertNotEqual(
            first["sources"][0]["corrected_source_sha256"],
            second["sources"][0]["corrected_source_sha256"],
        )

    def test_rechunk_and_source_revision_change_caption_ids(self) -> None:
        first = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        rechunked = copy.deepcopy(self.transcript)
        rechunked["caption_segments"] = [
            {
                "id": "combined",
                "text": "重複中文重複中文",
                "start": 0.0,
                "end": 3.0,
                "word_ids": ["word-00001", "word-00002"],
            }
        ]
        second = caption_delivery.expected_instances(self.project, rechunked, self.state)
        self.assertNotEqual(
            first["segmentation"]["segmentation_revision"],
            second["segmentation"]["segmentation_revision"],
        )
        self.assertNotEqual(first["sources"][0]["caption_source_id"], second["sources"][0]["caption_source_id"])
        self._write_source_revision("c" * 64)
        third = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        self.assertNotEqual(first["sources"][0]["caption_source_id"], third["sources"][0]["caption_source_id"])

    def test_repeated_occurrence_and_reorder_have_final_domain_identity(self) -> None:
        repeated = copy.deepcopy(self.state)
        repeated["segments"] = [
            {"source_start": 2.0, "source_end": 3.0},
            {"source_start": 0.0, "source_end": 1.0},
            {"source_start": 2.0, "source_end": 3.0},
        ]
        result = caption_delivery.expected_instances(self.project, self.transcript, repeated)
        self.assertEqual([item["final_start_us"] for item in result["instances"]], [0, 1_000_000, 2_000_000])
        repeated_source = [
            item for item in result["instances"] if item["corrected_source"] == "重複中文"
            and item["caption_source_id"] == result["sources"][1]["caption_source_id"]
        ]
        self.assertEqual([item["occurrence_ordinal"] for item in repeated_source], [0, 1])
        self.assertNotEqual(repeated_source[0]["caption_instance_id"], repeated_source[1]["caption_instance_id"])

    def test_missing_overlay_fails_render_binding_with_stable_code(self) -> None:
        self._create()
        manifest = caption_delivery._load_json(self.project / "project.json")
        state = caption_delivery._load_json(self.project / "working/editor_state.json")
        state["overlays"].pop()
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.validate_for_render(self.project, state, manifest)
        self.assertEqual(caught.exception.code, "caption_binding_missing")

    def test_stale_order_timing_and_duplicate_instance_fail_closed(self) -> None:
        self._create()
        manifest = caption_delivery._load_json(self.project / "project.json")
        state = caption_delivery._load_json(self.project / "working/editor_state.json")
        original = caption_delivery._load_json(self.project / caption_delivery.CAPTION_REL)
        mutations = []
        reordered = copy.deepcopy(original)
        reordered["items"].reverse()
        mutations.append(reordered)
        timed = copy.deepcopy(original)
        timed["items"][0]["final_end_us"] += 1
        mutations.append(timed)
        duplicate = copy.deepcopy(original)
        duplicate["items"][1]["caption_instance_id"] = duplicate["items"][0]["caption_instance_id"]
        mutations.append(duplicate)
        for artifact in mutations:
            with self.subTest(artifact=artifact["items"]):
                caption_delivery._atomic_write(self.project / caption_delivery.CAPTION_REL, artifact)
                with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
                    caption_delivery.validate_for_render(self.project, state, manifest)
                self.assertEqual(caught.exception.code, "caption_binding_missing")
        caption_delivery._atomic_write(self.project / caption_delivery.CAPTION_REL, original)

    def test_live_artifact_bytes_must_match_state_adopted_hash(self) -> None:
        protected_source = "中文 Pro 30kg"
        self.transcript["words"][0]["text"] = protected_source
        self.transcript["caption_segments"][0]["text"] = protected_source
        self.state["overlays"][0]["text"] = protected_source
        self._write_project()

        def preserving_model(prompt: str, _stage: str, **_kwargs):
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": (
                            "Chinese Pro 30kg"
                            if index == 0
                            else f"English line {chr(ord('A') + index)}"
                        ),
                    }
                    for index, item in enumerate(requested)
                ]
            }

        caption_delivery.create_delivery(
            self.project,
            "en",
            required=True,
            model_call=preserving_model,
        )
        manifest = caption_delivery._load_json(self.project / "project.json")
        state = caption_delivery._load_json(self.project / "working/editor_state.json")
        artifact = caption_delivery._load_json(self.project / caption_delivery.CAPTION_REL)
        artifact["items"][0]["translated_text"] = "Plan"
        caption_delivery._atomic_write(self.project / caption_delivery.CAPTION_REL, artifact)

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.validate_for_render(self.project, state, manifest)
        self.assertEqual(caught.exception.code, "caption_binding_missing")
        self.assertIn("artifact hash", str(caught.exception))

    def test_mutually_forged_receipt_is_rejected_against_current_provider_config(self) -> None:
        self._create()
        manifest = caption_delivery._load_json(self.project / "project.json")
        state = caption_delivery._load_json(self.project / "working/editor_state.json")
        artifact = caption_delivery._load_json(self.project / caption_delivery.CAPTION_REL)
        forged = copy.deepcopy(artifact["provider_receipt"])
        forged["config_sha256"] = "f" * 64
        artifact["provider_receipt"] = forged
        for item in artifact["items"]:
            item["provider_receipt"] = forged
        caption_delivery._atomic_write(self.project / caption_delivery.CAPTION_REL, artifact)
        forged_hash = hashlib.sha256(
            (self.project / caption_delivery.CAPTION_REL).read_bytes()
        ).hexdigest()
        state["caption_delivery"]["artifact_sha256"] = forged_hash
        for overlay in state["overlays"]:
            if overlay.get("type") == "caption":
                overlay["caption_delivery_artifact_sha256"] = forged_hash
        manifest["subtitles"]["translation"]["provider_receipt"] = forged

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.validate_for_render(self.project, state, manifest)
        self.assertEqual(caught.exception.code, "caption_binding_missing")
        self.assertIn("provider receipt", str(caught.exception))

    def test_provider_origin_change_after_adoption_is_rejected(self) -> None:
        self._create()
        manifest = caption_delivery._load_json(self.project / "project.json")
        state = caption_delivery._load_json(self.project / "working/editor_state.json")
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://127.0.0.1:11435"}):
            with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
                caption_delivery.validate_for_render(self.project, state, manifest)
        self.assertEqual(caught.exception.code, "caption_binding_missing")
        self.assertIn("provider receipt", str(caught.exception))

    def test_retranslation_updates_adopted_hash_and_invalidates_approvals(self) -> None:
        self._create()
        first_state = caption_delivery._load_json(
            self.project / "working/editor_state.json"
        )
        manifest = caption_delivery._load_json(self.project / "project.json")
        manifest["approvals"]["timeline"] = {"approved": True, "state_revision": "x"}
        manifest["approvals"]["final"] = {"approved": True, "state_revision": "y"}
        caption_delivery._atomic_write(self.project / "project.json", manifest)

        def revised_model(prompt: str, _stage: str, **_kwargs):
            requested = json.loads(prompt.rsplit("\n", 1)[-1])
            return {
                "items": [
                    {
                        "caption_instance_id": item["caption_instance_id"],
                        "translated_text": f"Revised English {chr(ord('A') + index)}",
                    }
                    for index, item in enumerate(requested)
                ]
            }

        caption_delivery.create_delivery(
            self.project,
            "en",
            required=True,
            model_call=revised_model,
        )
        second_state = caption_delivery._load_json(
            self.project / "working/editor_state.json"
        )
        second_manifest = caption_delivery._load_json(self.project / "project.json")
        self.assertNotEqual(
            first_state["caption_delivery"]["artifact_sha256"],
            second_state["caption_delivery"]["artifact_sha256"],
        )
        self.assertFalse(second_manifest["approvals"]["timeline"]["approved"])
        self.assertFalse(second_manifest["approvals"]["final"]["approved"])

    def test_unsupported_provider_fails_before_project_mutation(self) -> None:
        self.manifest["subtitles"]["translation"] = {"provider": "cloud", "model": "remote"}
        self._write_project()
        before = {
            path: path.read_bytes()
            for path in (
                self.project / "project.json",
                self.project / "working/editor_state.json",
            )
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, "translation_provider_unsupported")
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_provider_period_working_inode_swap_cannot_redirect_adoption(self) -> None:
        original_working = self.project / "working-original"
        outside = self.project / "outside-adoption"
        state_before = (self.project / "working/editor_state.json").read_bytes()
        project_before = (self.project / "project.json").read_bytes()

        def swapping_model(prompt: str, stage: str, **kwargs):
            response = self._fake_model(prompt, stage, **kwargs)
            (self.project / "working").rename(original_working)
            outside.mkdir()
            (self.project / "working").symlink_to(outside, target_is_directory=True)
            return response

        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                self.project,
                "en",
                required=True,
                model_call=swapping_model,
            )
        self.assertEqual(caught.exception.code, "caption_project_changed")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(
            (original_working / "editor_state.json").read_bytes(), state_before
        )
        self.assertEqual((self.project / "project.json").read_bytes(), project_before)

    def test_non_loopback_ollama_host_is_rejected_without_mutation(self) -> None:
        tracked = (
            self.project / "project.json",
            self.project / "working/editor_state.json",
        )
        before = {path: path.read_bytes() for path in tracked}
        with patch.dict(os.environ, {"OLLAMA_HOST": "https://api.example.com"}):
            with self.assertRaises(ValueError) as caught:
                caption_delivery.create_delivery(
                    self.project,
                    "en",
                    required=True,
                )
        self.assertIn("loopback Ollama", str(caught.exception))
        self.assertFalse((self.project / caption_delivery.CAPTION_REL).exists())
        self.assertEqual(before, {path: path.read_bytes() for path in tracked})

    def test_symlinked_working_directory_is_rejected(self) -> None:
        unsafe = self.project / "unsafe-project"
        outside = self.project / "outside-working"
        unsafe.mkdir()
        outside.mkdir()
        (unsafe / "working").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery.create_delivery(
                unsafe,
                "en",
                required=True,
                model_call=self._fake_model,
            )
        self.assertEqual(caught.exception.code, "caption_project_invalid")

    def test_identity_exception_is_item_scoped_and_reason_limited(self) -> None:
        # Sources changed to Latin brands under SPEC §4 v1.5 (2026-08-13).
        # This test says what an adopted identity claim looks like and which
        # reasons are allowed; it used to say it over a Chinese sentence
        # echoed back verbatim, which v1.5 rejects as
        # `translation_wrong_language` — that shape is exactly the escape
        # hatch a real cut shipped Chinese captions through, and it is now
        # covered by `TargetLanguageScriptTests` in the Phase 4 tests. The
        # claim being made here is unchanged; only the line it is made about
        # is now one the exemption was written for.
        expected = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        for instance, source in zip(expected["instances"], ("Notion", "Figma"), strict=True):
            instance["corrected_source"] = source
        response = {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": item["corrected_source"],
                    "identity_preserved": True,
                    "identity_reason": "brand",
                }
                for item in expected["instances"]
            ]
        }
        translated = caption_delivery._validate_translations(expected["instances"], response, [])
        self.assertTrue(all(item["translation_status"] == "identity_preserved" for item in translated))
        response["items"][0]["identity_reason"] = "sentence"
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(expected["instances"], response, [])
        self.assertEqual(caught.exception.code, "translation_identity_invalid")

    def test_chinese_punctuation_only_change_is_not_a_translation(self) -> None:
        expected = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        response = {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": "重複，中文！",
                }
                for item in expected["instances"]
            ]
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(expected["instances"], response, [])
        self.assertEqual(caught.exception.code, "translation_unchanged")

    def test_source_latin_number_and_unit_tokens_must_survive(self) -> None:
        expected = caption_delivery.expected_instances(self.project, self.transcript, self.state)
        expected["instances"][0]["corrected_source"] = "方案 Pro 30kg"
        response = {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": "Plan",
                }
                for item in expected["instances"]
            ]
        }
        with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
            caption_delivery._validate_translations(expected["instances"], response, ["Pro"])
        self.assertEqual(caught.exception.code, "translation_token_missing")

    def test_bool_and_nonfinite_times_are_rejected(self) -> None:
        for invalid in (True, math.nan, math.inf):
            state = copy.deepcopy(self.state)
            state["segments"][0]["source_start"] = invalid
            with self.subTest(invalid=invalid):
                with self.assertRaises(caption_delivery.CaptionDeliveryError):
                    caption_delivery.expected_instances(self.project, self.transcript, state)

    def test_canonical_hash_vector_is_stable(self) -> None:
        self.assertEqual(
            contract_registry.canonical_hash({"中文字": [2, 1], "a": True}),
            "5207290b825aee81f17a172a6d0884941f3077a2aecfe096d67c6a98587d27af",
        )

    def test_raw_source_revision_is_stable_and_force_is_the_only_generation_increment(self) -> None:
        raw = {
            "engine": "openai-whisper",
            "engine_version": "2026.1",
            "language": "zh",
            "decoding_params": {"temperature": 0},
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "words": [
                        {"word": "原始字", "start": 0.0, "end": 1.0, "speaker": "S1"}
                    ],
                }
            ],
        }
        manifest = {"source": {"sha256": "d" * 64}}
        with patch.object(caption_delivery, "decoded_pcm_sha256", return_value="e" * 64):
            first = caption_delivery.capture_transcript_source(
                self.project, manifest, copy.deepcopy(raw), model="base"
            )
            second = caption_delivery.capture_transcript_source(
                self.project, manifest, copy.deepcopy(raw), model="base"
            )
            forced = caption_delivery.capture_transcript_source(
                self.project,
                manifest,
                copy.deepcopy(raw),
                model="base",
                force_retranscription=True,
            )
            changed_model = caption_delivery.capture_transcript_source(
                self.project, manifest, copy.deepcopy(raw), model="large-v3"
            )
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["source_generation"], 0)
        self.assertEqual(forced["source_generation"], 1)
        self.assertNotEqual(first["revision"], forced["revision"])
        self.assertEqual(changed_model["source_generation"], 1)
        self.assertNotEqual(forced["revision"], changed_model["revision"])
        immutable = self.project / f"working/transcript_sources/{first['revision']}.json"
        self.assertEqual(
            immutable.read_bytes(), caption_delivery.canonical_bytes(first)
        )

    def test_pcm_decode_period_working_swap_cannot_redirect_source_adoption(self) -> None:
        raw = {
            "engine": "openai-whisper",
            "engine_version": "2026.1",
            "language": "zh",
            "decoding_params": {"temperature": 0},
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "words": [{"word": "原始字", "start": 0.0, "end": 1.0}],
                }
            ],
        }
        manifest = {"source": {"sha256": "d" * 64}}
        original_working = self.project / "working-original"
        outside = self.project / "outside-capture"
        current_before = (
            self.project / caption_delivery.SOURCE_CURRENT_REL
        ).read_bytes()
        revisions_before = {
            path.name: path.read_bytes()
            for path in (self.project / caption_delivery.SOURCE_VERSIONS_REL).iterdir()
        }

        def swapping_decode(_project_dir: Path, _manifest: dict) -> str:
            (self.project / "working").rename(original_working)
            outside.mkdir()
            (self.project / "working").symlink_to(outside, target_is_directory=True)
            return "e" * 64

        with patch.object(
            caption_delivery, "decoded_pcm_sha256", side_effect=swapping_decode
        ):
            with self.assertRaises(caption_delivery.CaptionDeliveryError) as caught:
                caption_delivery.capture_transcript_source(
                    self.project, manifest, raw, model="base"
                )

        self.assertEqual(caught.exception.code, "caption_project_changed")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(
            (original_working / caption_delivery.SOURCE_CURRENT_REL.name).read_bytes(),
            current_before,
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (
                    original_working / caption_delivery.SOURCE_VERSIONS_REL.name
                ).iterdir()
            },
            revisions_before,
        )

    def test_public_tracer_completes_through_fake_loopback_ollama(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(handler) -> None:  # noqa: N802
                length = int(handler.headers.get("Content-Length", "0"))
                request = json.loads(handler.rfile.read(length).decode("utf-8"))
                prompt = request["messages"][-1]["content"]
                requested = json.loads(prompt.rsplit("\n", 1)[-1])
                content = {
                    "items": [
                        {
                            "caption_instance_id": item["caption_instance_id"],
                            "translated_text": f"Public English {chr(ord('A') + index)}",
                        }
                        for index, item in enumerate(requested)
                    ]
                }
                payload = json.dumps(
                    {"message": {"content": json.dumps(content, ensure_ascii=False)}}
                ).encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["OLLAMA_HOST"] = f"http://127.0.0.1:{server.server_port}"
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "translate-captions",
                    "--project-dir",
                    str(self.project),
                    "--language",
                    "en",
                    "--required",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"], 2)
        self.assertEqual(len(set(payload["caption_source_ids"])), 2)
        self.assertEqual(len(set(payload["caption_instance_ids"])), 2)
        self.assertEqual(payload["provider_receipt"]["mode"], "local_loopback")


if __name__ == "__main__":
    unittest.main()

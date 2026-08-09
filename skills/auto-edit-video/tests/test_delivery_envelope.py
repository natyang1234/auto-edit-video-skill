"""Phase 0b direct delivery-envelope tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402


class DeliveryEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.external_output = root / "published" / "final.mp4"
        self.render_id = "direct-final-test"
        self.state = {"schema_version": 2, "project_id": "test", "segments": []}
        self.ffmpeg = Path(shutil.which("ffmpeg") or "/bin/sh")
        self._attempts: list[delivery_envelope.StagingAttempt] = []

    def tearDown(self) -> None:
        for attempt in reversed(self._attempts):
            try:
                delivery_envelope.discard_staging(
                    self.project,
                    attempt.render_id,
                    authority=attempt,
                )
            except delivery_envelope.DeliveryEnvelopeError:
                pass
        self._tmp.cleanup()

    def _begin(self, render_id: str | None = None) -> delivery_envelope.StagingAttempt:
        attempt = delivery_envelope.begin_staging(
            self.project,
            render_id or self.render_id,
            expected_output=self.external_output,
        )
        self._attempts.append(attempt)
        return attempt

    def _simulate_crash(self, attempt: delivery_envelope.StagingAttempt) -> None:
        delivery_envelope._release_staging_lease(
            self.project,
            attempt.render_id,
            attempt._owner_token,
        )

    def _child_begin(self, *, crash_after_begin: bool) -> subprocess.CompletedProcess[str]:
        script = (
            "import os,sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(SCRIPTS_DIR)!r});import delivery_envelope as d;"
            "project=Path(sys.argv[1]);output=Path(sys.argv[2]);render_id=sys.argv[3];"
            "\ntry:\n stage=d.begin_staging(project,render_id,expected_output=output)"
            "\nexcept d.DeliveryEnvelopeError:\n raise SystemExit(23)"
            "\n(stage/'child-candidate.mp4').write_bytes(b'child-active')"
            + (
                "\nos._exit(0)"
                if crash_after_begin
                else "\nd.discard_staging(project,render_id,authority=stage)"
            )
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.project),
                str(self.external_output),
                self.render_id,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def _stage(self) -> tuple[Path, dict[str, Path], dict]:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
        }
        sources["output"].write_bytes(b"new-output")
        sources["qa_report"].write_bytes(b"new-qa")
        sources["contact_sheet"].write_bytes(b"new-contact")
        sources["visual_evidence"].write_bytes(b"new-evidence")
        prepared = delivery_envelope.build_prepared_envelope(
            self.project,
            self.render_id,
            self.external_output,
            self.state,
            sources,
            renderer_script=Path(__file__).resolve(),
            ffmpeg_executable=self.ffmpeg,
        )
        delivery_envelope.write_prepared_envelope(stage, prepared)
        return stage, sources, prepared

    def test_publish_binds_all_core_artifacts_and_cleans_staging(self) -> None:
        stage, sources, prepared = self._stage()
        finalized = delivery_envelope.publish_direct_delivery(
            self.project,
            stage,
            staged_sources=sources,
            expected_output=self.external_output,
        )
        self.assertEqual(finalized["state"], "finalized")
        self.assertEqual(
            finalized["prepared_envelope_hash"], contract_registry.canonical_hash(prepared)
        )
        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertEqual(
            (self.project / "qa/direct-final-test.json").read_bytes(), b"new-qa"
        )
        self.assertEqual(
            (self.project / "working/render_visual_evidence/direct-final-test.json").read_bytes(),
            b"new-evidence",
        )
        self.assertFalse(stage.exists())

    def test_caption_v2_is_hash_bound_at_its_fixed_canonical_destination(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
            "caption_v2": stage / "caption_v2.json",
        }
        for name, path in sources.items():
            path.write_bytes(("caption-bound-" + name).encode("utf-8"))
        prepared = delivery_envelope.build_prepared_envelope(
            self.project,
            self.render_id,
            self.external_output,
            self.state,
            sources,
            renderer_script=Path(__file__).resolve(),
            ffmpeg_executable=self.ffmpeg,
        )
        self.assertEqual(
            prepared["artifacts"]["caption_v2"]["path"],
            "working/caption_delivery_v2.json",
        )
        self.assertIsNone(prepared["artifacts"]["audio_event_plan"])
        self.assertIsNone(prepared["artifacts"]["audio_catalog"])
        self.assertIsNone(prepared["artifacts"]["sfx_stem"])
        delivery_envelope.write_prepared_envelope(stage, prepared)
        expected_caption_bytes = sources["caption_v2"].read_bytes()
        finalized = delivery_envelope.publish_direct_delivery(
            self.project,
            stage,
            staged_sources=sources,
            expected_output=self.external_output,
        )
        canonical = self.project / "working/caption_delivery_v2.json"
        self.assertEqual(canonical.read_bytes(), expected_caption_bytes)
        self.assertEqual(
            finalized["artifacts"]["caption_v2"]["sha256"],
            delivery_envelope._sha256(canonical),
        )

    def test_caption_v2_destination_override_is_rejected(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
            "caption_v2": stage / "caption_v2.json",
        }
        for name, path in sources.items():
            path.write_bytes(name.encode("utf-8"))
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.build_prepared_envelope(
                self.project,
                self.render_id,
                self.external_output,
                self.state,
                sources,
                renderer_script=Path(__file__).resolve(),
                ffmpeg_executable=self.ffmpeg,
                destinations={"caption_v2": "working/not-canonical.json"},
            )

    def test_caption_v2_participates_in_stale_journal_recovery(self) -> None:
        prior_output = b"prior-output"
        prior_caption = b"prior-caption"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior_output)
        canonical_caption = self.project / "working/caption_delivery_v2.json"
        canonical_caption.parent.mkdir(parents=True)
        canonical_caption.write_bytes(prior_caption)
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
            "caption_v2": stage / "caption_v2.json",
        }
        for name, path in sources.items():
            path.write_bytes(("new-" + name).encode("utf-8"))
        prepared = delivery_envelope.build_prepared_envelope(
            self.project,
            self.render_id,
            self.external_output,
            self.state,
            sources,
            renderer_script=Path(__file__).resolve(),
            ffmpeg_executable=self.ffmpeg,
        )
        delivery_envelope.write_prepared_envelope(stage, prepared)
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(
            self.project, prepared, finalized, stage
        )
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {"schema_version": 1, "render_id": self.render_id, "entries": entries},
        )
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        delivery_envelope._copy_atomic(sources["caption_v2"], canonical_caption)
        self._simulate_crash(stage)
        delivery_envelope.recover_stale_staging(
            self.project,
            self.render_id,
            expected_output=self.external_output,
        )
        self.assertEqual(self.external_output.read_bytes(), prior_output)
        self.assertEqual(canonical_caption.read_bytes(), prior_caption)
        self.assertFalse(stage.exists())

    def test_candidate_mutation_after_prepared_is_rejected_before_publication(self) -> None:
        stage, sources, _prepared = self._stage()
        sources["output"].write_bytes(b"tampered-output")
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.publish_direct_delivery(
                self.project,
                stage,
                staged_sources=sources,
                expected_output=self.external_output,
            )
        self.assertFalse(self.external_output.exists())
        self.assertFalse(delivery_envelope.finalized_path(self.project, self.render_id).exists())

    def test_qa_mutation_after_prepared_preserves_prior_output(self) -> None:
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(b"prior-output")
        stage, sources, _prepared = self._stage()
        sources["qa_report"].write_bytes(b"tampered-qa")
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.publish_direct_delivery(
                self.project,
                stage,
                staged_sources=sources,
                expected_output=self.external_output,
            )
        self.assertEqual(self.external_output.read_bytes(), b"prior-output")

    def test_visual_evidence_mutation_after_prepared_is_rejected(self) -> None:
        stage, sources, _prepared = self._stage()
        sources["visual_evidence"].write_bytes(b"tampered-evidence")
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.publish_direct_delivery(
                self.project,
                stage,
                staged_sources=sources,
                expected_output=self.external_output,
            )
        self.assertFalse(self.external_output.exists())

    def test_stale_journal_restores_prior_destination(self) -> None:
        prior = b"prior-output"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior)
        stage, sources, prepared = self._stage()
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(self.project, prepared, finalized, stage)
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {"schema_version": 1, "render_id": self.render_id, "entries": entries},
        )
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        self._simulate_crash(stage)
        delivery_envelope.recover_stale_staging(
            self.project,
            self.render_id,
            expected_output=self.external_output,
        )
        self.assertEqual(self.external_output.read_bytes(), prior)
        self.assertFalse(stage.exists())

    def test_recovery_rejects_a_journal_target_outside_current_delivery(self) -> None:
        prior = b"prior-output"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior)
        stage, sources, prepared = self._stage()
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(self.project, prepared, finalized, stage)
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        victim = self.external_output.parent / "unrelated.mp4"
        victim.write_bytes(b"do-not-delete")
        entries[1] = {
            "destination": str(victim.resolve()),
            "external": True,
            "new_sha256": delivery_envelope._sha256(victim),
            "prior_exists": False,
            "prior_sha256": None,
            "backup": None,
        }
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {
                "schema_version": 1,
                "render_id": self.render_id,
                "entries": entries,
            },
        )
        self._simulate_crash(stage)

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertEqual(victim.read_bytes(), b"do-not-delete")
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_recovery_rejects_wrong_render_id_without_restoring_any_entry(self) -> None:
        prior = b"prior-output"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior)
        stage, sources, prepared = self._stage()
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(self.project, prepared, finalized, stage)
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {"schema_version": 1, "render_id": "other-render", "entries": entries},
        )
        self._simulate_crash(stage)

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_recovery_rejects_duplicate_destination_without_restoring_any_entry(self) -> None:
        prior = b"prior-output"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior)
        stage, sources, prepared = self._stage()
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(self.project, prepared, finalized, stage)
        entries[1] = json.loads(json.dumps(entries[0]))
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {"schema_version": 1, "render_id": self.render_id, "entries": entries},
        )
        self._simulate_crash(stage)

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_cleanup_failure_after_finalization_does_not_report_false_failure(self) -> None:
        stage, sources, _prepared = self._stage()

        with patch.object(
            delivery_envelope,
            "_remove_stage",
            side_effect=OSError("simulated cleanup failure"),
        ):
            finalized = delivery_envelope.publish_direct_delivery(
                self.project,
                stage,
                staged_sources=sources,
                expected_output=self.external_output,
            )

        self.assertEqual(finalized["state"], "finalized")
        self.assertTrue(delivery_envelope.finalized_path(self.project, self.render_id).is_file())
        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertTrue(stage.is_dir())
        delivery_envelope.recover_stale_staging(
            self.project,
            self.render_id,
            expected_output=self.external_output,
        )
        self.assertFalse(stage.exists())

    def test_begin_rejects_aliased_staging_parent_without_touching_external_tree(self) -> None:
        outside = Path(self._tmp.name) / "outside-staging"
        aliased_stage = outside / self.render_id
        aliased_stage.mkdir(parents=True)
        marker = aliased_stage / "marker.bin"
        marker.write_bytes(b"keep-me")
        delivery_root = self.project / "working/delivery_envelopes"
        delivery_root.mkdir(parents=True)
        (delivery_root / ".staging").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.begin_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(marker.read_bytes(), b"keep-me")
        self.assertTrue((delivery_root / ".staging").is_symlink())

    def test_begin_rejects_aliased_render_stage_without_touching_target(self) -> None:
        initial = self._begin()
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=initial,
        )
        outside = Path(self._tmp.name) / "outside-render-stage"
        outside.mkdir()
        marker = outside / "marker.bin"
        marker.write_bytes(b"keep-stage")
        initial.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.begin_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(marker.read_bytes(), b"keep-stage")
        self.assertTrue(initial.is_symlink())

    def test_render_id_cannot_escape_the_lock_directory(self) -> None:
        victim = Path(self._tmp.name) / "victim.lock"
        victim.write_bytes(b"untouched")
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.begin_staging(
                self.project,
                "../../victim",
                expected_output=self.external_output,
            )
        self.assertEqual(victim.read_bytes(), b"untouched")

    def test_second_active_begin_is_rejected_and_preserves_first_candidate(self) -> None:
        stage = self._begin()
        candidate = stage / "candidate.mp4"
        candidate.write_bytes(b"active-render")

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.begin_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )
        self.assertEqual(candidate.read_bytes(), b"active-render")
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=stage,
        )

    def test_same_process_cleanup_without_attempt_authority_cannot_delete_live_stage(self) -> None:
        stage = self._begin()
        marker = stage / "live-attempt.bin"
        marker.write_bytes(b"keep-live")

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.discard_staging(self.project, self.render_id)
        self.assertEqual(marker.read_bytes(), b"keep-live")
        self.assertIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_wrong_attempt_token_cannot_discard_or_publish_active_stage(self) -> None:
        stage, sources, _prepared = self._stage()
        marker = stage / "wrong-token.bin"
        marker.write_bytes(b"keep-owner")
        forged = delivery_envelope.StagingAttempt(
            project_dir=stage.project_dir,
            render_id=stage.render_id,
            stage_dir=stage.stage_dir,
            _owner_token=object(),
        )

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.discard_staging(
                self.project,
                self.render_id,
                authority=forged,
            )
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.publish_direct_delivery(
                self.project,
                stage.stage_dir,
                staged_sources=sources,
                expected_output=self.external_output,
            )
        self.assertEqual(marker.read_bytes(), b"keep-owner")
        lease = delivery_envelope._ACTIVE_STAGING_LEASES[
            delivery_envelope._lease_key(self.project, self.render_id)
        ]
        self.assertIs(lease.owner_token, stage._owner_token)

    def test_stale_attempt_token_cannot_delete_a_new_attempt(self) -> None:
        stale = self._begin()
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=stale,
        )
        current = self._begin()
        marker = current / "current-attempt.bin"
        marker.write_bytes(b"current-owner")

        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.discard_staging(
                self.project,
                self.render_id,
                authority=stale,
            )
        self.assertEqual(marker.read_bytes(), b"current-owner")
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=current,
        )
        self.assertFalse(current.exists())

    def test_attempt_owner_can_discard_and_release_its_own_stage(self) -> None:
        stage = self._begin()
        marker = stage / "owner-cleanup.bin"
        marker.write_bytes(b"remove-me")

        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=stage,
        )
        self.assertFalse(stage.exists())
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_other_process_cannot_enter_an_active_render_lease(self) -> None:
        stage = self._begin()
        candidate = stage / "candidate.mp4"
        candidate.write_bytes(b"parent-active")

        child = self._child_begin(crash_after_begin=False)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.assertEqual(candidate.read_bytes(), b"parent-active")
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=stage,
        )

    def test_process_death_releases_lease_and_next_begin_recovers_stage(self) -> None:
        child = self._child_begin(crash_after_begin=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        stale = delivery_envelope.staging_path(self.project, self.render_id)
        self.assertEqual((stale / "child-candidate.mp4").read_bytes(), b"child-active")

        replacement = self._begin()
        self.assertEqual(replacement.stage_dir, stale)
        self.assertFalse((replacement / "child-candidate.mp4").exists())
        delivery_envelope.discard_staging(
            self.project,
            self.render_id,
            authority=replacement,
        )

    def test_publish_rechecks_all_prior_state_after_journal_is_durable(self) -> None:
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(b"prior-output")
        stage, sources, _prepared = self._stage()
        original_write = delivery_envelope._atomic_write_json

        def inject_external_edit(path: Path, payload: object) -> None:
            original_write(path, payload)
            if path == stage / delivery_envelope.JOURNAL_NAME:
                self.external_output.write_bytes(b"external-edit")

        with patch.object(
            delivery_envelope,
            "_atomic_write_json",
            side_effect=inject_external_edit,
        ):
            with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
                delivery_envelope.publish_direct_delivery(
                    self.project,
                    stage,
                    staged_sources=sources,
                    expected_output=self.external_output,
                )

        self.assertEqual(self.external_output.read_bytes(), b"external-edit")
        self.assertFalse((self.project / f"qa/{self.render_id}.json").exists())
        self.assertFalse(delivery_envelope.finalized_path(self.project, self.render_id).exists())
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_later_destination_edit_rolls_back_already_published_output(self) -> None:
        qa_path = self.project / f"qa/{self.render_id}.json"
        self.external_output.parent.mkdir(parents=True)
        qa_path.parent.mkdir(parents=True)
        self.external_output.write_bytes(b"prior-output")
        qa_path.write_bytes(b"prior-qa")
        stage, sources, _prepared = self._stage()
        original_copy = delivery_envelope._copy_atomic
        injected = False

        def edit_qa_after_output_publish(source: Path, destination: Path) -> None:
            nonlocal injected
            original_copy(source, destination)
            if not injected and destination == self.external_output.resolve():
                injected = True
                qa_path.write_bytes(b"external-qa-edit")

        with patch.object(
            delivery_envelope,
            "_copy_atomic",
            side_effect=edit_qa_after_output_publish,
        ):
            with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
                delivery_envelope.publish_direct_delivery(
                    self.project,
                    stage,
                    staged_sources=sources,
                    expected_output=self.external_output,
                )

        self.assertEqual(self.external_output.read_bytes(), b"prior-output")
        self.assertEqual(qa_path.read_bytes(), b"external-qa-edit")
        self.assertFalse(delivery_envelope.finalized_path(self.project, self.render_id).exists())
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_recovery_compensates_earlier_restore_when_later_entry_races(self) -> None:
        qa_path = self.project / f"qa/{self.render_id}.json"
        self.external_output.parent.mkdir(parents=True)
        qa_path.parent.mkdir(parents=True)
        self.external_output.write_bytes(b"prior-output")
        qa_path.write_bytes(b"prior-qa")
        stage, sources, prepared = self._stage()
        finalized = json.loads(json.dumps(prepared))
        finalized["state"] = "finalized"
        finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        entries = delivery_envelope._journal_entries(self.project, prepared, finalized, stage)
        delivery_envelope._atomic_write_json(
            stage / delivery_envelope.JOURNAL_NAME,
            {"schema_version": 1, "render_id": self.render_id, "entries": entries},
        )
        delivery_envelope._copy_atomic(sources["output"], self.external_output)
        delivery_envelope._copy_atomic(sources["qa_report"], qa_path)
        self._simulate_crash(stage)
        original_copy = delivery_envelope._copy_atomic
        raced = False

        def race_after_output_restore(source: Path, destination: Path) -> None:
            nonlocal raced
            original_copy(source, destination)
            if not raced and destination == self.external_output.resolve():
                raced = True
                qa_path.write_bytes(b"external-qa-edit")

        with patch.object(delivery_envelope, "_copy_atomic", side_effect=race_after_output_restore):
            with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
                delivery_envelope.recover_stale_staging(
                    self.project,
                    self.render_id,
                    expected_output=self.external_output,
                )

        self.assertEqual(self.external_output.read_bytes(), b"new-output")
        self.assertEqual(qa_path.read_bytes(), b"external-qa-edit")
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

    def test_final_envelope_write_failure_restores_every_prior_artifact(self) -> None:
        prior_artifacts = {
            self.external_output: b"prior-output",
            self.project / f"qa/{self.render_id}.json": b"prior-qa",
            self.project / f"qa/{self.render_id}-contact.png": b"prior-contact",
            self.project
            / f"working/render_visual_evidence/{self.render_id}.json": b"prior-evidence",
        }
        for path, payload in prior_artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        stage, sources, _prepared = self._stage()
        final_path = delivery_envelope.finalized_path(self.project, self.render_id)
        original_write = delivery_envelope._atomic_write_json

        def fail_final_write(path: Path, payload: object) -> None:
            if path == final_path:
                raise OSError("simulated finalized-envelope failure")
            original_write(path, payload)

        with patch.object(
            delivery_envelope,
            "_atomic_write_json",
            side_effect=fail_final_write,
        ):
            with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
                delivery_envelope.publish_direct_delivery(
                    self.project,
                    stage,
                    staged_sources=sources,
                    expected_output=self.external_output,
                )

        for path, payload in prior_artifacts.items():
            self.assertEqual(path.read_bytes(), payload)
        self.assertFalse(final_path.exists())
        self.assertFalse(stage.exists())

    def test_schema_and_semantic_validation_rejects_extra_and_null_core(self) -> None:
        _stage, _sources, prepared = self._stage()
        errors = contract_registry.validate_artifact("delivery_envelope", prepared)
        self.assertEqual(errors, [])
        extra = json.loads(json.dumps(prepared))
        extra["unexpected"] = True
        self.assertTrue(contract_registry.validate_artifact("delivery_envelope", extra))
        missing_core = json.loads(json.dumps(prepared))
        missing_core["artifacts"]["qa_report"] = None
        self.assertTrue(contract_registry.validate_artifact("delivery_envelope", missing_core))
        bad_hash = json.loads(json.dumps(prepared))
        bad_hash["artifacts"]["output"]["sha256"] = "not-a-sha"
        self.assertTrue(contract_registry.validate_artifact("delivery_envelope", bad_hash))
        bad_state = json.loads(json.dumps(prepared))
        bad_state["state"] = "published"
        self.assertTrue(contract_registry.validate_artifact("delivery_envelope", bad_state))

    def test_finalized_prepared_hash_is_recomputed_not_merely_well_formed(self) -> None:
        stage, sources, _prepared = self._stage()
        finalized = delivery_envelope.publish_direct_delivery(
            self.project,
            stage,
            staged_sources=sources,
            expected_output=self.external_output,
        )
        finalized["prepared_envelope_hash"] = "0" * 64

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "prepared envelope hash",
        ):
            delivery_envelope.validate_envelope(
                self.project,
                finalized,
                expected_state="finalized",
            )


if __name__ == "__main__":
    unittest.main()

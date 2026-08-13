"""Phase 0b direct delivery-envelope tests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402
import auto_edit  # noqa: E402
from render_editor_timeline import direct_final_render_id  # noqa: E402


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

    def _publish_contextual_delivery(
        self, *, defer_commit: bool = False
    ) -> tuple[Path, Path, dict]:
        self.state = {
            "schema_version": 2,
            "project_id": "snapshot-test",
            "director_style": "kinetic-explainer",
            "segments": [{"source_start": 0.0, "source_end": 1.0}],
        }
        working = self.project / "working"
        working.mkdir(exist_ok=True)
        (working / "editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        self.render_id = direct_final_render_id(self.state, self.external_output)
        stage = self._begin(self.render_id)
        sources: dict[str, Path] = {}
        payloads = {
            "output": b"contextual-output",
            "qa_report": b'{"status":"pass"}\n',
            "contact_sheet": b"contextual-contact",
            "visual_evidence": b'{"items":[]}\n',
            "caption_v2": b'{"required":true}\n',
            "audio_event_plan": b'{"events":[]}\n',
            "audio_catalog": b'{"items":[]}\n',
            "sfx_stem": b"contextual-sfx",
        }
        for name, payload in payloads.items():
            path = stage / delivery_envelope.STAGE_FILENAMES[name]
            path.write_bytes(payload)
            sources[name] = path
        sources["motion_evidence"] = sources["visual_evidence"]
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
        publication = delivery_envelope.publish_direct_delivery(
            self.project,
            stage,
            staged_sources=sources,
            expected_output=self.external_output,
            defer_commit=defer_commit,
        )
        self._last_publication = publication
        finalized = publication.finalized if defer_commit else publication
        return (
            delivery_envelope.finalized_path(self.project, self.render_id),
            self.project / finalized["artifacts"]["qa_report"]["path"],
            finalized,
        )

    def _deferred_contextual_delivery(self):
        finalized_path, qa_path, finalized = self._publish_contextual_delivery(
            defer_commit=True
        )
        return self._last_publication, finalized_path, qa_path, finalized

    def _snapshot(self):
        return delivery_envelope.snapshot_finalized_delivery(
            self.project,
            self.external_output,
            expected_profile_id="kinetic-explainer",
            required_artifacts=(
                "caption_v2",
                "audio_event_plan",
                "audio_catalog",
                "sfx_stem",
                "visual_evidence",
                "motion_evidence",
            ),
        )

    def test_orphan_staging_sweep_recovers_a_stage_no_render_repeats(self) -> None:
        """A crashed one-off render left its stage stranded forever.

        Recovery only ran when the same render id was staged again, so the
        rollback material for a render nobody repeats was never applied.
        """
        stage, sources, _prepared = self._stage()
        self._simulate_crash(stage)
        stage_dir = delivery_envelope.staging_path(self.project, self.render_id)
        self.assertTrue(stage_dir.is_dir())
        self.assertTrue(sources["output"].is_file())

        skipped = delivery_envelope.sweep_orphan_staging(self.project)

        self.assertEqual(skipped, [])
        self.assertFalse(
            stage_dir.exists(), "the stranded stage survived the startup sweep"
        )
        self._attempts.clear()

    def test_orphan_staging_sweep_keeps_a_stage_it_cannot_identify(self) -> None:
        staging = self.project / delivery_envelope.STAGING_REL
        stranded = staging / "unidentifiable-render"
        stranded.mkdir(parents=True)
        (stranded / "candidate.mp4").write_bytes(b"no prepared envelope here")

        skipped = delivery_envelope.sweep_orphan_staging(self.project)

        self.assertTrue(any("unidentifiable-render" in item for item in skipped), skipped)
        self.assertTrue(stranded.is_dir(), "an unidentifiable stage must not be deleted")

    def test_direct_published_output_verifies_against_its_finalized_envelope(self) -> None:
        """Phase 4 final slice: re-verify a published pointer after the run.

        Publication binds bytes to one envelope; every later consumer has to
        ask the same question again instead of trusting a pointer.
        """
        _finalized_path, _qa_path, finalized = self._publish_contextual_delivery()
        published = str(finalized["artifacts"]["output"]["path"])
        record = delivery_envelope.verify_published_output(
            self.project,
            self.render_id,
            published,
            expected_sha256=finalized["artifacts"]["output"]["sha256"],
            expected_prepared_hash=finalized["prepared_envelope_hash"],
        )
        self.assertEqual(record["sha256"], finalized["artifacts"]["output"]["sha256"])

    def test_direct_published_output_refuses_a_missing_finalized_envelope(self) -> None:
        finalized_path, _qa_path, finalized = self._publish_contextual_delivery()
        published = str(finalized["artifacts"]["output"]["path"])
        finalized_path.unlink()
        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "finalized delivery envelope"
        ):
            delivery_envelope.verify_published_output(
                self.project, self.render_id, published
            )

    def test_direct_published_output_refuses_forged_and_swapped_bytes(self) -> None:
        finalized_path, _qa_path, finalized = self._publish_contextual_delivery()
        published = Path(str(finalized["artifacts"]["output"]["path"]))
        # Bytes swapped under a still-valid envelope.
        published.write_bytes(b"swapped-after-publication")
        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "do not match"
        ):
            delivery_envelope.verify_published_output(
                self.project, self.render_id, str(published)
            )
        # An envelope re-forged around the swapped bytes still fails: the
        # receipt-side prepared hash and the declared digest disagree.
        forged = json.loads(json.dumps(finalized))
        forged["artifacts"]["output"]["sha256"] = hashlib.sha256(
            published.read_bytes()
        ).hexdigest()
        forged["artifacts"]["output"]["bytes"] = published.stat().st_size
        finalized_path.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
            delivery_envelope.verify_published_output(
                self.project,
                self.render_id,
                str(published),
                expected_sha256=finalized["artifacts"]["output"]["sha256"],
                expected_prepared_hash=finalized["prepared_envelope_hash"],
            )

    def test_direct_published_output_refuses_a_path_the_envelope_never_published(
        self,
    ) -> None:
        _finalized_path, qa_path, _finalized = self._publish_contextual_delivery()
        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "does not publish"
        ):
            delivery_envelope.verify_published_output(
                self.project, self.render_id, str(qa_path)
            )

    def test_contextual_snapshot_rejects_symlinked_finalized_envelope(self) -> None:
        finalized_path, _qa_path, _finalized = self._publish_contextual_delivery()
        outside = self.project.parent / "outside-envelope.json"
        outside.write_bytes(finalized_path.read_bytes())
        finalized_path.unlink()
        finalized_path.symlink_to(outside)

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "finalized envelope"
        ):
            self._snapshot()

    def test_contextual_snapshot_rejects_self_consistent_wrong_profile(self) -> None:
        finalized_path, _qa_path, finalized = self._publish_contextual_delivery()
        forged = json.loads(json.dumps(finalized))
        forged["profile"] = {
            "id": "teacher-punch",
            "resolved_profile_hash": "a" * 64,
        }
        prepared = json.loads(json.dumps(forged))
        prepared["state"] = "prepared"
        prepared["prepared_envelope_hash"] = None
        forged["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
        delivery_envelope._atomic_write_json(finalized_path, forged)

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "profile"
        ):
            self._snapshot()

    def test_contextual_snapshot_rejects_initial_resolved_hash_drift(self) -> None:
        self._publish_contextual_delivery()

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "hash changed"
        ):
            delivery_envelope.snapshot_finalized_delivery(
                self.project,
                self.external_output,
                expected_profile_id="kinetic-explainer",
                expected_profile_hash="f" * 64,
            )

    def test_finalized_handoff_excludes_repo_publisher_for_same_render(self) -> None:
        self._publish_contextual_delivery()
        snapshot = self._snapshot()

        with delivery_envelope.finalized_delivery_handoff(snapshot):
            with self.assertRaisesRegex(
                delivery_envelope.DeliveryEnvelopeError,
                "already active",
            ):
                delivery_envelope.begin_staging(
                    self.project,
                    self.render_id,
                    expected_output=self.external_output,
                )

    def test_deferred_commit_keeps_publication_and_cleans_transaction(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        self.assertTrue(stage.is_dir())
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())

        delivery_envelope.commit_deferred_publication(authority)

        self.assertEqual(self.external_output.read_bytes(), b"contextual-output")
        self.assertTrue(finalized_path.is_file())
        self.assertFalse(stage.exists())

    def test_stdout_success_commits_before_best_effort_stage_cleanup(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        snapshot = self._snapshot()
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        writes = []
        payload = {
            "ok": True,
            "clips": [{"file": str(self.external_output)}],
            "problems": [],
        }

        with (
            patch.object(auto_edit, "_prepare_stdout_writer", return_value=writes.append),
            patch.object(
                delivery_envelope,
                "_remove_stage",
                side_effect=OSError("simulated committed cleanup failure"),
            ),
        ):
            committed = auto_edit._emit_final_delivery_handoff(
                payload,
                [snapshot],
                [authority],
            )

        self.assertTrue(committed)
        self.assertEqual(len(writes), 1)
        self.assertTrue(json.loads(writes[0])["ok"])
        self.assertEqual(self.external_output.read_bytes(), b"contextual-output")
        self.assertTrue(finalized_path.is_file())
        self.assertTrue(stage.is_dir())
        marker = json.loads(
            (stage / delivery_envelope.DEFERRED_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["state"], "committed")
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

        delivery_envelope.recover_stale_staging(
            self.project,
            self.render_id,
            expected_output=self.external_output,
        )
        self.assertEqual(self.external_output.read_bytes(), b"contextual-output")
        self.assertTrue(finalized_path.is_file())
        self.assertFalse(stage.exists())

    def test_committed_recovery_refuses_changed_current_publication(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        with patch.object(
            delivery_envelope,
            "_remove_stage",
            side_effect=OSError("simulated committed cleanup failure"),
        ):
            delivery_envelope.commit_deferred_publication(authority)

        replacement = self.external_output.with_name(".committed-external-change.mp4")
        replacement.write_bytes(b"external-change-after-commit")
        os.replace(replacement, self.external_output)

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "committed publication changed",
        ):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )

        self.assertEqual(
            self.external_output.read_bytes(), b"external-change-after-commit"
        )
        self.assertTrue(finalized_path.is_file())
        self.assertTrue(stage.is_dir())

    def test_committed_recovery_rejects_mismatched_marker_binding(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        with patch.object(
            delivery_envelope,
            "_remove_stage",
            side_effect=OSError("simulated committed cleanup failure"),
        ):
            delivery_envelope.commit_deferred_publication(authority)
        marker_path = stage / delivery_envelope.DEFERRED_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["journal_sha256"] = "f" * 64
        delivery_envelope._atomic_write_json(marker_path, marker)

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "marker.*binding|marker.*invalid",
        ):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )

        self.assertEqual(self.external_output.read_bytes(), b"contextual-output")
        self.assertTrue(finalized_path.is_file())
        self.assertTrue(stage.is_dir())

    def test_pending_marker_state_flip_without_rebinding_is_rejected(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        marker_path = stage / delivery_envelope.DEFERRED_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["state"] = "committed"
        delivery_envelope._atomic_write_json(marker_path, marker)
        delivery_envelope._release_staging_lease(
            self.project,
            self.render_id,
            authority._owner_token,
        )

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "marker binding is invalid",
        ):
            delivery_envelope.recover_stale_staging(
                self.project,
                self.render_id,
                expected_output=self.external_output,
            )

        self.assertEqual(self.external_output.read_bytes(), b"contextual-output")
        self.assertTrue(finalized_path.is_file())
        self.assertTrue(stage.is_dir())

    def test_post_publication_authority_validation_failure_removes_new_delivery(
        self,
    ) -> None:
        with patch.object(
            delivery_envelope,
            "_validate_deferred_publication",
            side_effect=delivery_envelope.DeliveryEnvelopeError(
                "forced post-publication authority validation failure"
            ),
        ):
            with self.assertRaisesRegex(
                delivery_envelope.DeliveryEnvelopeError,
                "forced post-publication authority validation failure",
            ):
                self._publish_contextual_delivery(defer_commit=True)

        final_path = delivery_envelope.finalized_path(
            self.project,
            self.render_id,
        )
        self.assertFalse(self.external_output.exists())
        self.assertFalse(final_path.exists())
        self.assertFalse(
            delivery_envelope.staging_path(self.project, self.render_id).exists()
        )
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_post_publication_authority_validation_failure_restores_exact_prior(
        self,
    ) -> None:
        prior_output = b"prior-output-before-authority-validation"
        prior_envelope = b"prior-envelope-before-authority-validation\n"
        self.state = {
            "schema_version": 2,
            "project_id": "snapshot-test",
            "director_style": "kinetic-explainer",
            "segments": [{"source_start": 0.0, "source_end": 1.0}],
        }
        working = self.project / "working"
        working.mkdir(exist_ok=True)
        (working / "editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        self.render_id = direct_final_render_id(self.state, self.external_output)
        final_path = delivery_envelope.finalized_path(self.project, self.render_id)
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior_output)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(prior_envelope)

        with patch.object(
            delivery_envelope,
            "_validate_deferred_publication",
            side_effect=delivery_envelope.DeliveryEnvelopeError(
                "forced post-publication authority validation failure"
            ),
        ):
            with self.assertRaisesRegex(
                delivery_envelope.DeliveryEnvelopeError,
                "forced post-publication authority validation failure",
            ):
                self._publish_contextual_delivery(defer_commit=True)

        self.assertEqual(self.external_output.read_bytes(), prior_output)
        self.assertEqual(final_path.read_bytes(), prior_envelope)
        self.assertFalse(
            delivery_envelope.staging_path(self.project, self.render_id).exists()
        )
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_post_publication_acquisition_rollback_preserves_external_conflict(
        self,
    ) -> None:
        def replace_output_then_fail(_authority) -> None:
            replacement = self.external_output.with_name(
                ".authority-gap-external-conflict.mp4"
            )
            replacement.write_bytes(b"external-conflict-in-authority-gap")
            os.replace(replacement, self.external_output)
            raise delivery_envelope.DeliveryEnvelopeError(
                "forced post-publication authority validation failure"
            )

        with patch.object(
            delivery_envelope,
            "_validate_deferred_publication",
            side_effect=replace_output_then_fail,
        ):
            with self.assertRaisesRegex(
                delivery_envelope.DeliveryEnvelopeError,
                "locked rollback is blocked; recovery state kept",
            ):
                self._publish_contextual_delivery(defer_commit=True)

        stage = delivery_envelope.staging_path(self.project, self.render_id)
        self.assertEqual(
            self.external_output.read_bytes(),
            b"external-conflict-in-authority-gap",
        )
        self.assertTrue(stage.is_dir())
        self.assertTrue((stage / delivery_envelope.JOURNAL_NAME).is_file())
        self.assertTrue((stage / delivery_envelope.DEFERRED_NAME).is_file())
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, self.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_deferred_abort_quarantines_conflict_and_removes_new_publication(self) -> None:
        authority, finalized_path, qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        replacement = self.external_output.with_name(".handoff-conflict.mp4")
        replacement.write_bytes(b"changed-before-handoff")
        replacement.replace(self.external_output)

        delivery_envelope.abort_deferred_publication(authority)

        self.assertFalse(self.external_output.exists())
        self.assertFalse(finalized_path.exists())
        self.assertFalse(qa_path.exists())
        self.assertFalse(delivery_envelope.staging_path(self.project, self.render_id).exists())
        quarantined = list(
            (self.project / delivery_envelope.QUARANTINE_REL).glob("*/*.conflict")
        )
        self.assertEqual(
            [path.read_bytes() for path in quarantined],
            [b"changed-before-handoff"],
        )

    def test_deferred_abort_quarantines_symlink_without_touching_target(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        outside = self.project.parent / "outside-handoff-conflict.mp4"
        outside.write_bytes(b"outside-must-survive")
        self.external_output.unlink()
        self.external_output.symlink_to(outside)

        delivery_envelope.abort_deferred_publication(authority)

        self.assertFalse(self.external_output.exists())
        self.assertFalse(self.external_output.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside-must-survive")
        self.assertFalse(finalized_path.exists())
        quarantined = list(
            (self.project / delivery_envelope.QUARANTINE_REL).glob("*/*.conflict")
        )
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0].is_symlink())

    def test_deferred_abort_restores_prior_output_and_envelope_exactly(self) -> None:
        prior_output = b"prior-output-sentinel"
        prior_envelope = b"prior-envelope-sentinel\n"
        self.state = {
            "schema_version": 2,
            "project_id": "snapshot-test",
            "director_style": "kinetic-explainer",
            "segments": [{"source_start": 0.0, "source_end": 1.0}],
        }
        working = self.project / "working"
        working.mkdir(exist_ok=True)
        (working / "editor_state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )
        self.render_id = direct_final_render_id(self.state, self.external_output)
        prior_finalized_path = delivery_envelope.finalized_path(
            self.project, self.render_id
        )
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior_output)
        prior_finalized_path.parent.mkdir(parents=True, exist_ok=True)
        prior_finalized_path.write_bytes(prior_envelope)

        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        delivery_envelope.abort_deferred_publication(authority)

        self.assertEqual(self.external_output.read_bytes(), prior_output)
        self.assertEqual(finalized_path.read_bytes(), prior_envelope)
        self.assertFalse(delivery_envelope.staging_path(self.project, self.render_id).exists())

    def test_deferred_abort_restores_prior_after_same_bytes_atomic_replace(self) -> None:
        prior_output = b"prior-output-before-same-bytes-replace"
        self.external_output.parent.mkdir(parents=True)
        self.external_output.write_bytes(prior_output)
        authority, _finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        replacement = self.external_output.with_name(".same-bytes-replacement.mp4")
        replacement.write_bytes(self.external_output.read_bytes())
        replacement.replace(self.external_output)

        delivery_envelope.abort_deferred_publication(authority)

        self.assertEqual(self.external_output.read_bytes(), prior_output)
        self.assertFalse(delivery_envelope.staging_path(self.project, self.render_id).exists())

    def test_wrong_and_stale_deferred_authority_cannot_change_new_attempt(self) -> None:
        authority, _finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        forged = replace(authority, _owner_token=object())
        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "stale or invalid"
        ):
            delivery_envelope.abort_deferred_publication(forged)
        self.assertTrue(self.external_output.is_file())

        delivery_envelope.commit_deferred_publication(authority)
        next_authority, _finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError, "stale or invalid"
        ):
            delivery_envelope.abort_deferred_publication(authority)
        self.assertTrue(self.external_output.is_file())
        delivery_envelope.abort_deferred_publication(next_authority)

    def test_stdout_body_failure_aborts_deferred_publication(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        snapshot = self._snapshot()

        def fail_writer(_payload: bytes) -> None:
            raise OSError("simulated stdout failure")

        payload = {
            "ok": True,
            "clips": [{"file": str(self.external_output)}],
            "problems": [],
        }
        with patch.object(
            auto_edit, "_prepare_stdout_writer", return_value=fail_writer
        ):
            with self.assertRaisesRegex(OSError, "stdout failure"):
                auto_edit._emit_final_delivery_handoff(
                    payload,
                    [snapshot],
                    [authority],
                )

        self.assertFalse(self.external_output.exists())
        self.assertFalse(finalized_path.exists())
        self.assertFalse(delivery_envelope.staging_path(self.project, self.render_id).exists())

    def test_process_death_recovery_aborts_deferred_publication(self) -> None:
        authority, finalized_path, _qa_path, _finalized = (
            self._deferred_contextual_delivery()
        )
        stage = delivery_envelope.staging_path(self.project, self.render_id)
        self.assertTrue((stage / delivery_envelope.DEFERRED_NAME).is_file())
        delivery_envelope._release_staging_lease(
            self.project,
            self.render_id,
            authority._owner_token,
        )

        delivery_envelope.recover_stale_staging(
            self.project,
            self.render_id,
            expected_output=self.external_output,
        )

        self.assertFalse(self.external_output.exists())
        self.assertFalse(finalized_path.exists())
        self.assertFalse(stage.exists())

    def _assert_snapshot_race_rejected(self, target: str) -> None:
        finalized_path, qa_path, _finalized = self._publish_contextual_delivery()
        real_snapshot = delivery_envelope._snapshot_regular_file
        changed = False

        def mutate_after_snapshot(path: Path, *, label: str, capture_bytes: bool = False):
            nonlocal changed
            snapshot = real_snapshot(path, label=label, capture_bytes=capture_bytes)
            if not changed and label == target:
                changed = True
                if label == "artifact output":
                    path.write_bytes(b"changed-output")
                elif label == "artifact qa_report":
                    qa_path.write_text('{"status":"fail"}\n', encoding="utf-8")
                else:
                    finalized_path.write_text("{}\n", encoding="utf-8")
            return snapshot

        with patch.object(
            delivery_envelope,
            "_snapshot_regular_file",
            side_effect=mutate_after_snapshot,
        ):
            with self.assertRaisesRegex(
                delivery_envelope.DeliveryEnvelopeError, "changed"
            ):
                self._snapshot()
        self.assertTrue(changed)

    def test_contextual_snapshot_rejects_output_single_hook_race(self) -> None:
        self._assert_snapshot_race_rejected("artifact output")

    def test_contextual_snapshot_rejects_qa_single_hook_race(self) -> None:
        self._assert_snapshot_race_rejected("artifact qa_report")

    def test_contextual_snapshot_rejects_envelope_single_hook_race(self) -> None:
        self._assert_snapshot_race_rejected("finalized envelope")

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

    def test_publication_revalidates_visual_authority_before_final_envelope(self) -> None:
        stage, sources, _prepared = self._stage()
        calls = 0

        def revalidate() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise delivery_envelope.DeliveryEnvelopeError(
                    "visual authority changed during publication"
                )

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "visual authority changed",
        ):
            delivery_envelope.publish_direct_delivery(
                self.project,
                stage,
                staged_sources=sources,
                expected_output=self.external_output,
                revalidate_authority=revalidate,
            )
        self.assertEqual(calls, 2)
        self.assertFalse(self.external_output.exists())
        self.assertFalse(delivery_envelope.finalized_path(self.project, self.render_id).exists())

    def test_prepared_envelope_rejects_staged_visual_authority_mismatch(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
        }
        authority = {
            "schema_version": 1,
            "source": "frozen_visual_authority",
            "visual_plan_revision": "a" * 64,
            "visual_plan_sha256": "b" * 64,
            "structured_layers_sha256": "c" * 64,
            "artifact_index_sha256": "d" * 64,
            "a_roll_breathing_intervals": [],
            "items": [],
        }
        authority["authority_hash"] = contract_registry.canonical_hash(authority)
        mismatched = json.loads(json.dumps(authority))
        mismatched["visual_plan_revision"] = "e" * 64
        mismatched["authority_hash"] = contract_registry.canonical_hash(
            {key: value for key, value in mismatched.items() if key != "authority_hash"}
        )
        sources["output"].write_bytes(b"candidate")
        sources["qa_report"].write_text('{"status":"pass"}\n', encoding="utf-8")
        sources["contact_sheet"].write_bytes(b"contact")
        sources["visual_evidence"].write_text(
            json.dumps({"authority": mismatched}) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            delivery_envelope.DeliveryEnvelopeError,
            "staged visual authority",
        ):
            delivery_envelope.build_prepared_envelope(
                self.project,
                self.render_id,
                self.external_output,
                self.state,
                sources,
                renderer_script=Path(__file__).resolve(),
                ffmpeg_executable=self.ffmpeg,
                visual_authority=authority,
            )

    def test_prepared_envelope_requires_complete_canonical_staged_authority(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
        }
        authority = {
            "schema_version": 1,
            "source": "frozen_visual_authority",
            "visual_plan_revision": "a" * 64,
            "visual_plan_sha256": "b" * 64,
            "structured_layers_sha256": "c" * 64,
            "artifact_index_sha256": "d" * 64,
            "a_roll_breathing_intervals": [],
            "items": [],
        }
        authority["authority_hash"] = contract_registry.canonical_hash(authority)
        sources["output"].write_bytes(b"candidate")
        sources["qa_report"].write_text('{"status":"pass"}\n', encoding="utf-8")
        sources["contact_sheet"].write_bytes(b"contact")
        invalid_hash = json.loads(json.dumps(authority))
        invalid_hash["authority_hash"] = "0" * 64
        for report in ({}, {"authority": invalid_hash}):
            with self.subTest(report=report):
                sources["visual_evidence"].write_text(
                    json.dumps(report) + "\n", encoding="utf-8"
                )
                with self.assertRaises(delivery_envelope.DeliveryEnvelopeError):
                    delivery_envelope.build_prepared_envelope(
                        self.project,
                        self.render_id,
                        self.external_output,
                        self.state,
                        sources,
                        renderer_script=Path(__file__).resolve(),
                        ffmpeg_executable=self.ffmpeg,
                        visual_authority=authority,
                    )

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

    def test_sfx_artifacts_are_all_or_none(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
            "audio_event_plan": stage / "audio_event_plan.json",
        }
        for name, path in sources.items():
            path.write_bytes(name.encode("utf-8"))
        with self.assertRaisesRegex(delivery_envelope.DeliveryEnvelopeError, "all-or-none"):
            delivery_envelope.build_prepared_envelope(
                self.project, self.render_id, self.external_output, self.state, sources,
                renderer_script=Path(__file__).resolve(), ffmpeg_executable=self.ffmpeg,
            )

    def test_sfx_artifacts_publish_to_canonical_destinations(self) -> None:
        stage = self._begin()
        sources = {
            "output": stage / "candidate.mp4",
            "qa_report": stage / "qa_report.json",
            "contact_sheet": stage / "contact_sheet.png",
            "visual_evidence": stage / "visual_evidence.json",
            "motion_evidence": stage / "visual_evidence.json",
            "audio_event_plan": stage / "audio_event_plan.json",
            "audio_catalog": stage / "audio_catalog.json",
            "sfx_stem": stage / "sfx_stem.wav",
        }
        for name, path in sources.items():
            path.write_bytes(("sfx-" + name).encode("utf-8"))
        prepared = delivery_envelope.build_prepared_envelope(
            self.project, self.render_id, self.external_output, self.state, sources,
            renderer_script=Path(__file__).resolve(), ffmpeg_executable=self.ffmpeg,
        )
        self.assertEqual(
            prepared["artifacts"]["audio_event_plan"]["path"],
            f"working/audio_event_plans/{self.render_id}.json",
        )
        delivery_envelope.write_prepared_envelope(stage, prepared)
        delivery_envelope.publish_direct_delivery(
            self.project, stage, staged_sources=sources, expected_output=self.external_output,
        )
        self.assertEqual(
            (self.project / f"working/audio_catalogs/{self.render_id}.json").read_bytes(),
            b"sfx-audio_catalog",
        )
        self.assertEqual(
            (self.project / f"working/sfx_stems/{self.render_id}.wav").read_bytes(),
            b"sfx-sfx_stem",
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

    def test_publish_rechecks_all_prior_state_after_journal_is_persisted(self) -> None:
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

"""Phase 6: Codex review fixes — path-escape hardening, fixed-stride pipeline-lag
evidence reserve, and version-gated contract fields.

Covers four independent fixes:

* ``sfx_import.cmd_verify`` now rejects approval/source evidence paths that
  escape the SFX library root, matching the guard ``load_sfx_library``
  already enforces on the production read path.
* ``_apply_density_policy_v2`` keeps a "lag evidence reserve": a candidate
  is dropped with reason ``lag_evidence_reserve`` if keeping it would starve
  the fixed-stride pipeline-lag evidence floor that
  ``_estimate_dialogue_pipeline_lag`` requires, computed through the same
  shared exclusion-window helper the verifier uses so the two sides can
  never disagree.
* ``contract_registry``'s ``audio_event_plan`` semantic validator
  version-gates ``sfx_hints_sha256`` the same way it already gates
  ``catalog_revision_sha256``: optional 64-hex for schema v3, forbidden for
  v1/v2.
* v1/v2 event and dropped-candidate evidence may not carry a ``hint`` block
  -- only schema v3 (trigger-or-hint) artifacts may.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_registry  # noqa: E402
import sfx_delivery  # noqa: E402
import sfx_import as si  # noqa: E402

from test_sfx_import import make_full_record  # noqa: E402

FIXTURE_DIR = SKILL_DIR / "contracts" / "fixtures" / "audio_event_plan"


def _load_and_validate(name: str) -> list[str]:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    artifact = contract_registry.load_artifact_text(text)
    return contract_registry.validate_artifact("audio_event_plan", artifact)


class SfxImportEvidenceEscapeTests(unittest.TestCase):
    """``verify`` must reject evidence paths that escape the library root."""

    def test_verify_rejects_approval_evidence_relative_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            outside = sfx_root.parent / "outside.md"
            outside.write_text("not part of the library\n", encoding="utf-8")
            record["approval"]["evidence_file"] = "../outside.md"
            record["record_sha256"] = si.record_hash(record)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertNotEqual(rc, 0)

    def test_verify_rejects_source_evidence_relative_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            outside = sfx_root.parent / "outside2.md"
            outside.write_text("not part of the library either\n", encoding="utf-8")
            record["approval"]["source_evidence_file"] = "../outside2.md"
            record["record_sha256"] = si.record_hash(record)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertNotEqual(rc, 0)

    def test_verify_rejects_absolute_path_evidence_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            record["approval"]["evidence_file"] = "/etc/hosts"
            record["record_sha256"] = si.record_hash(record)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertNotEqual(rc, 0)


class PipelineLagReserveTests(unittest.TestCase):
    """Fixed-stride evidence floor: short windows fail closed, production
    reserves enough untouched dialogue span that it never has to."""

    def test_short_safe_window_yields_none_and_a_machine_readable_reason(self) -> None:
        max_lag_samples = sfx_delivery._CANDIDATE_MAX_PIPELINE_LAG_SAMPLES
        total = 5160
        event = {"event_start_sample": 1024, "duration_samples": 2000}
        dialogue_samples = [(0.0, 0.0)] * total
        output_samples = [(0.0, 0.0)] * total

        lag_match = sfx_delivery._estimate_dialogue_pipeline_lag(
            output_samples, dialogue_samples, [event], max_lag_samples=max_lag_samples,
        )
        self.assertIsNone(lag_match)

        reason = sfx_delivery._dialogue_pipeline_lag_unavailable_reason(
            output_samples, dialogue_samples, [event], max_lag_samples=max_lag_samples,
        )
        self.assertRegex(reason, r"^insufficient untouched dialogue span for pipeline lag: ")

    def test_production_reserve_rule_drops_only_the_overflowing_candidate(self) -> None:
        """A candidate long enough to starve the shared evidence floor is
        dropped for ``lag_evidence_reserve`` while an earlier, cheaper
        candidate is kept -- and the resulting kept-only plan leaves the
        verifier enough untouched dialogue span to actually run its own
        pipeline-lag check, via the very same shared helper."""
        total_samples = 192000
        cheap_candidate = {
            "trigger_id": "title-a", "trigger_onset_sample": 1000,
            "event_start_sample": 1000, "expected_transient_sample": 1000,
            "duration_samples": 2000, "event_end_sample": 3000,
            "asset_transient_anchor_sample": 0, "role": "title_enter",
            "asset_id": "soft-ui-tick-v1", "category": "click_pop",
            "evidence": {"trigger": {"id": "title-a"}}, "out_of_range": False,
        }
        overflowing_candidate = {
            "trigger_id": "transition-b", "trigger_onset_sample": 15000,
            "event_start_sample": 15000, "expected_transient_sample": 15000,
            "duration_samples": 154000, "event_end_sample": 169000,
            "asset_transient_anchor_sample": 0, "role": "transition",
            "asset_id": "short-whoosh-v1", "category": "whoosh",
            "evidence": {"trigger": {"id": "transition-b"}}, "out_of_range": False,
        }
        with patch.object(
            sfx_delivery, "_resolve_density_proposal",
            side_effect=lambda proposal, *a, **k: proposal,
        ), patch.object(
            sfx_delivery, "_density_catalog_by_asset_id_v2", return_value={},
        ):
            result = sfx_delivery._apply_density_policy_v2(
                [cheap_candidate, overflowing_candidate], {}, total_samples,
            )

        self.assertEqual([event["trigger_id"] for event in result["kept"]], ["title-a"])
        self.assertEqual(len(result["dropped"]), 1)
        self.assertEqual(result["dropped"][0]["trigger_id"], "transition-b")
        self.assertEqual(result["dropped"][0]["reason"], "lag_evidence_reserve")
        self.assertEqual(result["policy_evidence"]["dropped_reasons"], {"lag_evidence_reserve": 1})

        final_span = sfx_delivery._dialogue_untouched_span_samples(
            result["kept"], sfx_delivery._CANDIDATE_MAX_PIPELINE_LAG_SAMPLES, total_samples,
        )
        self.assertGreaterEqual(final_span, sfx_delivery._PIPELINE_LAG_RESERVE_SAMPLES)


class ContractSfxHintsShaVersionGateTests(unittest.TestCase):
    """``sfx_hints_sha256`` is optional 64-hex under v3, forbidden under v1/v2."""

    def test_v1_forbids_sfx_hints_sha256(self) -> None:
        errors = _load_and_validate("invalid_v1_has_sfx_hints_sha256.json")
        self.assertTrue(
            any("sfx_hints_sha256" in error and "must not contain" in error for error in errors),
            errors,
        )

    def test_v3_accepts_sfx_hints_sha256(self) -> None:
        self.assertEqual(_load_and_validate("valid_v3_sfx_hints_sha256.json"), [])


class ContractEvidenceHintVersionGateTests(unittest.TestCase):
    """v1/v2 events and dropped candidates may only carry trigger-shaped
    evidence -- a ``hint`` block is a schema v3-only concept."""

    def test_v1_event_evidence_rejects_hint(self) -> None:
        errors = _load_and_validate("invalid_v1_evidence_has_hint.json")
        self.assertTrue(
            any(
                "evidence.hint" in error and "does not support hint evidence" in error
                for error in errors
            ),
            errors,
        )

    def test_v2_event_evidence_rejects_hint(self) -> None:
        errors = _load_and_validate("invalid_v2_evidence_has_hint.json")
        self.assertTrue(
            any(
                "evidence.hint" in error and "does not support hint evidence" in error
                for error in errors
            ),
            errors,
        )


if __name__ == "__main__":
    unittest.main()

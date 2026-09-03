"""Phase 4c: wire SFX v2 into the production ``auto_edit.py cut`` flow.

Covers: ``stage_phase0d_sfx`` director-profile-driven catalog_profile
resolution (v2 for kinetic-explainer, default v1 for other directors,
disabled-mode unchanged behavior), ``working/sfx_hints.json`` hard-error
validation, ``sfx_hints_sha256`` plan provenance, ``cut --sfx-hints`` landing
the file for both the in-process (final) and subprocess (non-final) render
routes, and ``scripts/sfx_suggest.py``'s advisory keyword scan (hit
matching, longest-keyword-wins, same-onset merging, never writing
``sfx_hints.json``, and never running at final quality).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import auto_edit  # noqa: E402
import contract_registry  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402
import sfx_delivery  # noqa: E402
import sfx_suggest  # noqa: E402
from test_sfx_delivery_v2 import _multi_event_visual_evidence  # noqa: E402

REVISION = "a" * 64
CUT_HASH = "b" * 64


def _hints(*entries: tuple[int, str, str]) -> dict:
    return {
        "schema_version": 1,
        "hints": [
            {"onset_sample": onset, "category": category, "reason": reason}
            for onset, category, reason in entries
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class StagePhase0dSfxDirectorProfileTests(unittest.TestCase):
    def test_kinetic_director_resolves_catalog_profile_v2_and_content_hint_lands(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_dir:
            project = Path(project_dir)
            hints = _hints((150000, "cash_coin", "coin cue detected"))
            _write_json(project / "working/sfx_hints.json", hints)
            state = {"director_style": "kinetic-explainer"}
            plan_path, catalog_path, stem_path = renderer.stage_phase0d_sfx(
                project, state, "render-1", Path(stage_dir),
                _multi_event_visual_evidence(),
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 3)
            hint_events = [e for e in plan["events"] if e["role"] == "content_hint"]
            self.assertEqual(len(hint_events), 1)
            self.assertEqual(hint_events[0]["category"], "cash_coin")
            self.assertIn("sfx_hints_sha256", plan)
            self.assertEqual(
                plan["sfx_hints_sha256"], contract_registry.canonical_hash(hints)
            )
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path,
                _multi_event_visual_evidence(),
                renderer.editor_base_state_revision(state),
                sfx_delivery.effective_cut_map_sha256(project, state),
            )
            self.assertEqual(report["status"], "pass", report.get("failures"))

    def test_non_kinetic_director_defaults_to_catalog_profile_v1(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_dir:
            project = Path(project_dir)
            state = {"director_style": "teacher-punch"}
            plan_path, _catalog_path, _stem_path = renderer.stage_phase0d_sfx(
                project, state, "render-1", Path(stage_dir),
                _multi_event_visual_evidence(),
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 2)
            self.assertNotIn("sfx_hints_sha256", plan)

    def test_disabled_mode_with_hints_present_keeps_v1_behavior_unchanged_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_dir:
            project = Path(project_dir)
            hints = _hints((150000, "cash_coin", "ignored under v1"))
            _write_json(project / "working/sfx_hints.json", hints)
            state = {"director_style": "teacher-punch"}
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                plan_path, _catalog_path, _stem_path = renderer.stage_phase0d_sfx(
                    project, state, "render-1", Path(stage_dir),
                    _multi_event_visual_evidence(),
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 2)
            self.assertNotIn("sfx_hints_sha256", plan)
            self.assertIn("sfx_hints ignored under catalog_profile=v1 (1)", stderr.getvalue())

    def test_default_profile_matches_v1_golden_bytes_when_no_hints_present(self) -> None:
        # test_sfx_phase4b.py already pins the literal golden hashes for a
        # direct v1 ``stage_multi_event_delivery`` call under REVISION/CUT_HASH
        # fixtures.  Here we prove the new director-profile-driven wrapper is
        # byte-for-byte identical to that same direct call for its own
        # (real, non-fixture) revision/cut_hash inputs -- i.e. unchanged v1
        # behavior.
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_a, \
                tempfile.TemporaryDirectory() as stage_b:
            project = Path(project_dir)
            state = {"director_style": "teacher-punch"}
            staged = renderer.stage_phase0d_sfx(
                project, state, "render-1", Path(stage_a),
                _multi_event_visual_evidence(),
            )
            timeline_revision = renderer.editor_base_state_revision(state)
            cut_hash = sfx_delivery.effective_cut_map_sha256(project, state)
            direct = sfx_delivery.stage_multi_event_delivery(
                Path(stage_b), _multi_event_visual_evidence(),
                timeline_revision, cut_hash,
            )
            hashes_via_wrapper = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in staged
            }
            hashes_direct = {
                Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for path in direct
            }
        self.assertEqual(hashes_via_wrapper, hashes_direct)

    def test_invalid_hints_file_hard_errors_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_dir:
            project = Path(project_dir)
            hints_path = project / "working/sfx_hints.json"
            hints_path.parent.mkdir(parents=True, exist_ok=True)
            hints_path.write_text("{not valid json", encoding="utf-8")
            state = {"director_style": "kinetic-explainer"}
            with self.assertRaises(ValueError) as raised:
                renderer.stage_phase0d_sfx(
                    project, state, "render-1", Path(stage_dir),
                    _multi_event_visual_evidence(),
                )
            self.assertIn(str(hints_path), str(raised.exception))

    def test_schema_invalid_hints_file_hard_errors_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, \
                tempfile.TemporaryDirectory() as stage_dir:
            project = Path(project_dir)
            hints_path = project / "working/sfx_hints.json"
            _write_json(hints_path, {"schema_version": 1})
            state = {"director_style": "kinetic-explainer"}
            with self.assertRaises(ValueError) as raised:
                renderer.stage_phase0d_sfx(
                    project, state, "render-1", Path(stage_dir),
                    _multi_event_visual_evidence(),
                )
            self.assertIn(str(hints_path), str(raised.exception))


class ContractRegistryFixtureTests(unittest.TestCase):
    def test_valid_v3_sfx_hints_sha256_fixture_passes(self) -> None:
        path = (
            SKILL_DIR / "contracts/fixtures/audio_event_plan/valid_v3_sfx_hints_sha256.json"
        )
        artifact = contract_registry.load_artifact_text(path.read_text("utf-8"))
        self.assertEqual(contract_registry.validate_artifact("audio_event_plan", artifact), [])

    def test_non_hex_sfx_hints_sha256_is_rejected(self) -> None:
        path = (
            SKILL_DIR / "contracts/fixtures/audio_event_plan/valid_v3_sfx_hints_sha256.json"
        )
        artifact = contract_registry.load_artifact_text(path.read_text("utf-8"))
        artifact["sfx_hints_sha256"] = "not-hex"
        errors = contract_registry.validate_artifact("audio_event_plan", artifact)
        self.assertTrue(any("sfx_hints_sha256" in error for error in errors))


class SfxSuggestScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / "working").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_transcript(self, segments: list[dict]) -> None:
        _write_json(
            self.project / "working/transcript_words.json",
            {"schema_version": 1, "caption_segments": segments},
        )

    def test_hit_matches_longest_keyword_across_categories(self) -> None:
        self._write_transcript([
            {"id": "caption-segment-0001", "start": 1.0, "end": 2.0, "text": "訂單成交確認"},
        ])
        table = sfx_suggest.load_keyword_table(
            SKILL_DIR / "references/sfx_keywords.json"
        )
        hints = sfx_suggest.build_hints(
            sfx_suggest._caption_segments(self.project), [], table
        )
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["category"], "cash_coin")
        self.assertEqual(hints[0]["matched_keyword"], "成交")
        self.assertEqual(hints[0]["reason"], "keyword:成交")

    def test_no_match_produces_no_hint_for_that_segment(self) -> None:
        self._write_transcript([
            {"id": "caption-segment-0001", "start": 1.0, "end": 2.0, "text": "今天天氣很好"},
        ])
        table = sfx_suggest.load_keyword_table(
            SKILL_DIR / "references/sfx_keywords.json"
        )
        hints = sfx_suggest.build_hints(
            sfx_suggest._caption_segments(self.project), [], table
        )
        self.assertEqual(hints, [])

    def test_same_onset_dedupes_keeping_first(self) -> None:
        table = {"cash_coin": ["營收"], "click_pop": ["清單"]}
        segments = [
            {"id": "a", "start": 1.0, "end": 1.5, "text": "營收成長"},
            {"id": "b", "start": 1.0, "end": 1.5, "text": "檢查清單"},
        ]
        hints = sfx_suggest.build_hints(segments, [], table)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["category"], "cash_coin")

    def test_run_never_writes_sfx_hints_json(self) -> None:
        self._write_transcript([
            {"id": "caption-segment-0001", "start": 1.0, "end": 2.0, "text": "訂單成交確認"},
        ])
        code = sfx_suggest.main([
            "--project-dir", str(self.project),
            "--keywords", str(SKILL_DIR / "references/sfx_keywords.json"),
        ])
        self.assertEqual(code, 0)
        self.assertFalse((self.project / "working/sfx_hints.json").is_file())
        out = json.loads((self.project / "working/sfx_suggestions.json").read_text("utf-8"))
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(len(out["hints"]), 1)


class CutSfxHintsLandingTests(unittest.TestCase):
    """The copy happens before the in-process/subprocess render fork, so both
    ``--quality final`` (in-process route) and ``--quality preview``
    (subprocess route) must land the file identically."""

    def _project(self, root: Path) -> Path:
        project = root / "project"
        (project / "working").mkdir(parents=True)
        _write_json(project / "project.json", {"source": {"duration_s": 10.0}})
        return project

    def _run_cut_until_hints_copied(self, project: Path, root: Path, hints_source: Path, quality: str) -> int:
        input_path = root / "input.mp4"
        input_path.write_bytes(b"not-a-real-video")
        args = auto_edit.build_parser().parse_args([
            "cut", "--input", str(input_path),
            "--project-dir", str(project),
            "--out", str(root / "out"),
            "--sfx-hints", str(hints_source),
            "--director", "high-energy",
            "--quality", quality,
        ])
        stdout = io.StringIO()
        with patch.object(auto_edit, "run_cut_stage", return_value=1):
            with contextlib.redirect_stdout(stdout):
                return auto_edit.cmd_cut(args)

    def test_cut_lands_sfx_hints_via_in_process_final_route(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            hints_source = root / "external_hints.json"
            hints = _hints((48000, "success_ding", "closing beat"))
            _write_json(hints_source, hints)
            project = self._project(root)
            code = self._run_cut_until_hints_copied(project, root, hints_source, "final")
            self.assertEqual(code, 2)
            landed = project / "working/sfx_hints.json"
            self.assertTrue(landed.is_file())
            self.assertEqual(json.loads(landed.read_text("utf-8")), hints)

    def test_cut_lands_sfx_hints_via_subprocess_preview_route(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            hints_source = root / "external_hints.json"
            hints = _hints((48000, "success_ding", "closing beat"))
            _write_json(hints_source, hints)
            project = self._project(root)
            code = self._run_cut_until_hints_copied(project, root, hints_source, "preview")
            self.assertEqual(code, 2)
            landed = project / "working/sfx_hints.json"
            self.assertTrue(landed.is_file())
            self.assertEqual(json.loads(landed.read_text("utf-8")), hints)

    def test_cut_rejects_contract_invalid_sfx_hints_file(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            hints_source = root / "bad_hints.json"
            _write_json(hints_source, {"schema_version": 1})
            project = root / "project"
            (project / "working").mkdir(parents=True)
            args = auto_edit.build_parser().parse_args([
                "cut", "--project-dir", str(project),
                "--out", str(root / "out"),
                "--sfx-hints", str(hints_source),
                "--director", "high-energy",
            ])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = auto_edit.cmd_cut(args)
            self.assertEqual(code, 2)
            self.assertFalse((project / "working/sfx_hints.json").is_file())


if __name__ == "__main__":
    unittest.main()

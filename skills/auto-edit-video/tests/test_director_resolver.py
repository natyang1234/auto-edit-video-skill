"""Phase 0a public director selector contract."""
from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts/auto_edit.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from director_resolver import (  # noqa: E402
    DirectorResolutionError,
    persist_director_selection,
    resolve_director_selection,
)


class DirectorResolverArtifactTests(unittest.TestCase):
    def test_persistence_binds_request_overrides_to_resolved_profile(self) -> None:
        resolved, request = resolve_director_selection(
            director="high-energy", extra_overrides={"quality": "preview"}
        )
        request["overrides"] = {"quality": "final"}

        with tempfile.TemporaryDirectory(prefix="director-persist-request-") as directory:
            with self.assertRaises(DirectorResolutionError) as raised:
                persist_director_selection(directory, resolved, request)

            self.assertEqual(raised.exception.code, "profile_conflict")
            self.assertEqual(raised.exception.conflicts, ["overrides"])
            self.assertFalse((Path(directory) / "working").exists())

    def test_persistence_recomputes_resolved_hash_before_writing(self) -> None:
        resolved, request = resolve_director_selection(director="teacher-punch")
        resolved["profile_id"] = "documentary"
        request["resolved_profile_hash"] = resolved["resolved_hash"]

        with tempfile.TemporaryDirectory(prefix="director-persist-hash-") as directory:
            with self.assertRaises(DirectorResolutionError) as raised:
                persist_director_selection(directory, resolved, request)

            self.assertEqual(raised.exception.code, "profile_conflict")
            self.assertEqual(raised.exception.conflicts, ["resolved_hash"])
            self.assertFalse((Path(directory) / "working").exists())


class DirectorResolverCliTests(unittest.TestCase):
    def test_bad_enum_non_object_and_unknown_override_are_registry_invalid(self) -> None:
        requests = (
            {
                "schema_version": 1,
                "profile_id": "kinetic-explainer",
                "selection_reason": "not-a-reason",
                "evidence": "profile",
                "overrides": {},
            },
            {
                "schema_version": 1,
                "profile_id": "kinetic-explainer",
                "selection_reason": "explicit_profile",
                "evidence": "profile",
                "overrides": {"private_capability": True},
            },
            {
                "schema_version": 1,
                "profile_id": "kinetic-explainer",
                "selection_reason": "explicit_profile",
                "evidence": "profile",
                "overrides": {"quality": "potato"},
            },
            {
                "schema_version": 1,
                "profile_id": "kinetic-explainer",
                "selection_reason": "explicit_profile",
                "evidence": "profile",
                "overrides": {"framing": "sideways", "burned_in": "maybe"},
            },
            {
                "schema_version": 1,
                "profile_id": " kinetic-explainer ",
                "selection_reason": "explicit_profile",
                "evidence": "profile",
                "overrides": {},
            },
            [],
        )
        with tempfile.TemporaryDirectory(prefix="director-contract-errors-") as directory:
            for index, request in enumerate(requests):
                with self.subTest(request=request):
                    request_path = Path(directory) / f"request-{index}.json"
                    request_path.write_text(json.dumps(request), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(CLI),
                            "resolve-director",
                            "--selection-request",
                            str(request_path),
                        ],
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(
                        json.loads(result.stdout)["error_code"], "registry_invalid"
                    )

    def test_incompatible_overrides_return_sorted_profile_conflicts(self) -> None:
        cases = (
            ({"translate": "zh-TW"}, ["translate"]),
            ({"no_cards": True}, ["no-cards"]),
            ({"no-editorial": True}, ["no-editorial"]),
            ({"burned_in": "yes"}, ["burned-in"]),
        )
        with tempfile.TemporaryDirectory(prefix="director-conflicts-") as directory:
            for index, (overrides, expected_conflicts) in enumerate(cases):
                with self.subTest(overrides=overrides):
                    request = {
                        "schema_version": 1,
                        "profile_id": "kinetic-explainer",
                        "selection_reason": "explicit_profile",
                        "evidence": "profile",
                        "overrides": overrides,
                    }
                    request_path = Path(directory) / f"request-{index}.json"
                    request_path.write_text(json.dumps(request), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(CLI),
                            "resolve-director",
                            "--selection-request",
                            str(request_path),
                        ],
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr, "")
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["error_code"], "profile_conflict")
                    self.assertEqual(payload["conflicts"], expected_conflicts)

    def test_compatible_and_redundant_overrides_are_normalized(self) -> None:
        request = {
            "schema_version": 1,
            "profile_id": "kinetic-explainer",
            "selection_reason": "explicit_profile",
            "evidence": "profile",
            "overrides": {
                "project_dir": "/tmp/project",
                "keep_pauses": True,
                "translate": "en",
                "cards_from_model": True,
                "glossary": ["API"],
            },
        }
        with tempfile.TemporaryDirectory(prefix="director-overrides-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "resolve-director",
                    "--selection-request",
                    str(request_path),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        expected = {
            "cards-from-model": True,
            "glossary": ["API"],
            "keep-pauses": True,
            "project-dir": "/tmp/project",
            "translate": "en",
        }
        self.assertEqual(payload["selection_request"]["overrides"], expected)
        self.assertEqual(payload["profile"]["overrides"], expected)

    def test_selection_reason_does_not_change_profile_hash(self) -> None:
        base = {
            "schema_version": 1,
            "profile_id": "kinetic-explainer",
            "evidence": "same profile",
            "overrides": {},
        }
        requests = [
            {**base, "selection_reason": "explicit_profile"},
            {**base, "selection_reason": "reference_style_match"},
        ]
        hashes = []
        with tempfile.TemporaryDirectory(prefix="director-reason-hash-") as directory:
            for index, request in enumerate(requests):
                request_path = Path(directory) / f"request-{index}.json"
                request_path.write_text(json.dumps(request), encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        "resolve-director",
                        "--selection-request",
                        str(request_path),
                    ],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                hashes.append(json.loads(result.stdout)["profile"]["resolved_hash"])
        self.assertEqual(hashes[0], hashes[1])

    def test_director_and_request_profile_mismatch_is_profile_conflict(self) -> None:
        request = {
            "schema_version": 1,
            "profile_id": "kinetic-explainer",
            "selection_reason": "explicit_profile",
            "evidence": "explicit profile",
            "overrides": {},
        }
        with tempfile.TemporaryDirectory(prefix="director-mismatch-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "resolve-director",
                    "--director",
                    "teacher-punch",
                    "--selection-request",
                    str(request_path),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error_code"], "profile_conflict")
        self.assertEqual(payload["conflicts"], ["director", "profile_id"])

    def test_selection_request_shape_errors_are_registry_invalid(self) -> None:
        request = {
            "schema_version": 1,
            "profile_id": "kinetic-explainer",
            "selection_reason": "explicit_profile",
            "overrides": {},
            "unexpected": True,
        }
        with tempfile.TemporaryDirectory(prefix="director-invalid-request-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "resolve-director",
                    "--selection-request",
                    str(request_path),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error_code"], "registry_invalid")

    def test_selection_request_returns_hash_bound_normalized_envelope(self) -> None:
        request = {
            "schema_version": 1,
            "profile_id": "kinetic-explainer",
            "selection_reason": "explicit_kinetic_bundle",
            "evidence": "動畫圖卡＋中英字幕＋配對音效",
            "overrides": {},
        }
        with tempfile.TemporaryDirectory(prefix="director-request-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "resolve-director",
                    "--selection-request",
                    str(request_path),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["profile"]["profile_id"], "kinetic-explainer")
        self.assertEqual(
            payload["selection_request"]["selection_reason"],
            "explicit_kinetic_bundle",
        )
        self.assertEqual(
            payload["selection_request"]["resolved_profile_hash"],
            payload["profile"]["resolved_hash"],
        )

    def test_public_kinetic_selector_emits_hashed_canonical_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "resolve-director",
                "--director",
                "kinetic-explainer",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["profile_id"], "kinetic-explainer")
        self.assertEqual(payload["schema_version"], 1)
        self.assertRegex(payload["resolved_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(list(payload), sorted(payload))
        unsigned = {key: value for key, value in payload.items() if key != "resolved_hash"}
        expected_hash = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(payload["resolved_hash"], expected_hash)

    def test_unknown_director_returns_stable_json_exit_two(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "resolve-director",
                "--director",
                "does-not-exist",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"error_code": "unknown_director"})

    def test_public_resolution_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="director-resolve-") as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "resolve-director",
                    "--director",
                    "kinetic-explainer",
                ],
                cwd=directory,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

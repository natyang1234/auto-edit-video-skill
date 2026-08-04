from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from svg_sanitizer import (  # noqa: E402
    POLICY_VERSION,
    SANITIZER_VERSION,
    SvgRejected,
    sanitize_svg,
)

CORPUS_PATH = SKILL_DIR / "contracts" / "fixtures" / "svg_threat_corpus.json"


def _corpus() -> list[dict]:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return data["cases"]


class ThreatCorpusTests(unittest.TestCase):
    """Every corpus case must land on its declared verdict, fail closed."""

    def test_reject_cases_all_fail_closed(self) -> None:
        for case in _corpus():
            if case["expect"] != "reject":
                continue
            with self.subTest(case=case["id"]):
                with self.assertRaises(SvgRejected) as ctx:
                    sanitize_svg(case["svg"])
                if "reason" in case:
                    self.assertEqual(ctx.exception.reason, case["reason"])

    def test_accept_cases_all_pass(self) -> None:
        for case in _corpus():
            if case["expect"] != "accept":
                continue
            with self.subTest(case=case["id"]):
                result = sanitize_svg(case["svg"])
                self.assertTrue(result.canonical_svg.startswith("<svg"))
                self.assertEqual(len(result.canonical_hash), 64)
                self.assertEqual(
                    result.receipt["canonical_sha256"], result.canonical_hash
                )


class ReceiptTests(unittest.TestCase):
    def test_receipt_binds_versions_and_metrics(self) -> None:
        result = sanitize_svg("<svg viewBox=\"0 0 24 24\"><path d=\"M4 4h4v4H4z\"/></svg>")
        receipt = result.receipt
        self.assertEqual(receipt["sanitizer_version"], SANITIZER_VERSION)
        self.assertEqual(receipt["policy_version"], POLICY_VERSION)
        self.assertEqual(receipt["canonical_sha256"], result.canonical_hash)
        self.assertEqual(receipt["byte_length"], len(result.canonical_svg.encode("utf-8")))
        self.assertGreaterEqual(receipt["node_count"], 2)

    def test_canonical_hash_is_stable_across_cosmetic_variation(self) -> None:
        a = sanitize_svg("<svg viewBox=\"0 0 24 24\"><path d=\"M4 4h4v4H4z\"/></svg>")
        b = sanitize_svg(
            "<svg   viewBox='0 0 24 24'>\n  <path d='M4 4h4v4H4z'></path>\n</svg>"
        )
        self.assertEqual(a.canonical_hash, b.canonical_hash)

    def test_canonical_hash_differs_when_geometry_differs(self) -> None:
        a = sanitize_svg("<svg viewBox=\"0 0 24 24\"><path d=\"M4 4h4v4H4z\"/></svg>")
        b = sanitize_svg("<svg viewBox=\"0 0 24 24\"><path d=\"M5 5h4v4H5z\"/></svg>")
        self.assertNotEqual(a.canonical_hash, b.canonical_hash)

    def test_root_svg_gets_namespace_injected(self) -> None:
        result = sanitize_svg("<svg viewBox=\"0 0 24 24\"><rect/></svg>")
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', result.canonical_svg)


class LimitTests(unittest.TestCase):
    def test_oversized_input_rejected(self) -> None:
        payload = "<svg>" + ("<rect/>" * 400_000) + "</svg>"
        with self.assertRaises(SvgRejected) as ctx:
            sanitize_svg(payload)
        self.assertIn(ctx.exception.reason, {"byte-limit", "node-limit"})

    def test_deep_nesting_rejected(self) -> None:
        payload = "<svg>" + ("<g>" * 500) + ("</g>" * 500) + "</svg>"
        with self.assertRaises(SvgRejected) as ctx:
            sanitize_svg(payload)
        self.assertEqual(ctx.exception.reason, "depth-limit")

    def test_bytes_input_accepted(self) -> None:
        result = sanitize_svg(b"<svg viewBox=\"0 0 24 24\"><rect/></svg>")
        self.assertEqual(len(result.canonical_hash), 64)

    def test_empty_input_rejected(self) -> None:
        with self.assertRaises(SvgRejected):
            sanitize_svg("")


if __name__ == "__main__":
    unittest.main()

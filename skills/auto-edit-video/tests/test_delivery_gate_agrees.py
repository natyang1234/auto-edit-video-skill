"""The three delivery gates apply the same check to the same file.

Single, batch and variant downloads each wrote out the same sequence —
refuse a symlink, confine to the scope, require the file, require the digest
— and had already drifted on which exceptions they caught. A gate that
decides whether a video can leave the machine is the worst place for three
slightly different answers.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from editor_server import verified_receipt_file  # noqa: E402


class VerifiedReceiptFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gate-")
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.project / "qa").mkdir()
        self.report = self.project / "qa/report.json"
        self.report.write_text('{"status": "pass"}', encoding="utf-8")
        self.digest = hashlib.sha256(self.report.read_bytes()).hexdigest()

    def check(self, relative: str, digest: str | None = None):
        return verified_receipt_file(
            self.project, relative, self.digest if digest is None else digest,
            "qa", "the report",
        )

    def test_a_real_file_with_the_declared_digest_passes(self) -> None:
        path, failure = self.check("qa/report.json")
        self.assertEqual(failure, "")
        self.assertEqual(path, self.report.resolve())

    def test_a_missing_file_is_refused(self) -> None:
        _, failure = self.check("qa/not-there.json")
        self.assertIn("missing", failure)

    def test_a_file_changed_since_verification_is_refused(self) -> None:
        self.report.write_text('{"status": "fail"}', encoding="utf-8")
        _, failure = self.check("qa/report.json")
        self.assertIn("changed after verification", failure)

    def test_a_symlinked_report_is_refused(self) -> None:
        # Following it would let a receipt vouch for one file and deliver
        # another.
        link = self.project / "qa/link.json"
        os.symlink(self.report, link)
        _, failure = self.check("qa/link.json")
        self.assertIn("escapes its project scope", failure)

    def test_a_path_outside_the_scope_is_refused(self) -> None:
        elsewhere = self.project / "renders"
        elsewhere.mkdir()
        (elsewhere / "report.json").write_text('{"status": "pass"}', encoding="utf-8")
        _, failure = self.check("renders/report.json")
        self.assertIn("escapes its project scope", failure)

    def test_a_path_climbing_out_of_the_project_is_refused(self) -> None:
        _, failure = self.check("../../etc/passwd")
        self.assertIn("escapes its project scope", failure)

    def test_a_receipt_with_no_digest_is_incomplete(self) -> None:
        _, failure = self.check("qa/report.json", digest="")
        self.assertIn("incomplete", failure)

    def test_a_receipt_with_a_malformed_digest_is_incomplete(self) -> None:
        _, failure = self.check("qa/report.json", digest="not-a-sha")
        self.assertIn("incomplete", failure)

    def test_a_receipt_naming_no_file_is_incomplete(self) -> None:
        _, failure = self.check("")
        self.assertIn("incomplete", failure)

    def test_the_label_is_carried_into_the_message(self) -> None:
        # Each gate names its own item; the check must not flatten that or a
        # batch failure stops saying which item failed.
        _, failure = verified_receipt_file(
            self.project, "qa/gone.json", self.digest, "qa", "batch item 3 report"
        )
        self.assertTrue(failure.startswith("batch item 3 report"))


if __name__ == "__main__":
    unittest.main()

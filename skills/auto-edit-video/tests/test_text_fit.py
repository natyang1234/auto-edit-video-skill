import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from text_fit import layout_report
from text_fit import measure_values


class TextFitEvidenceTests(unittest.TestCase):
    def test_only_dom_boxes_can_pass(self):
        self.assertEqual(layout_report([])["status"], "warn")
        self.assertEqual(layout_report([{"id": "title", "x": 150, "y": 300, "width": 500, "height": 100}])["status"], "pass")

    def test_safe_and_subject_warnings_are_advisory(self):
        report = layout_report([{"id": "title", "x": 0, "y": 500, "width": 300, "height": 100}], subject_box={"x": 100, "y": 480, "width": 300, "height": 300})
        self.assertEqual(report["status"], "warn")
        self.assertEqual({x["kind"] for x in report["findings"]}, {"safe_area", "subject_overlap"})

    def test_values_without_composition_never_claim_measurement(self):
        report = measure_values({"text": "很長的文字" * 80, "number": "1234"})
        self.assertEqual(report["measurement"], "unavailable")
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["boxes"], [])

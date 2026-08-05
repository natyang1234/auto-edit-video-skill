"""Loudness and sample peak are different quantities and need separate knobs."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import qa_video  # noqa: E402


class ShortClipPeakTests(unittest.TestCase):
    def test_the_two_thresholds_are_separate_fields(self) -> None:
        # A clip too short for R128 is judged on sample peak. Real speech
        # measures around -16 LUFS with peaks near -2 dBFS, so the two
        # numbers are not interchangeable even when they happen to match.
        policy = qa_video.QaPolicy()
        self.assertTrue(hasattr(policy, "min_short_clip_peak_dbfs"))
        self.assertTrue(hasattr(policy, "min_integrated_lufs"))

    def test_tightening_loudness_leaves_the_peak_gate_alone(self) -> None:
        # Sharing one number meant tightening the loudness gate silently
        # tightened the peak gate too, and a peak threshold at speech level
        # rejects ordinary quiet talking.
        policy = qa_video.QaPolicy(min_integrated_lufs=-20.0)
        self.assertEqual(policy.min_short_clip_peak_dbfs, QaDefaults.PEAK)

    def test_tightening_the_peak_gate_leaves_loudness_alone(self) -> None:
        policy = qa_video.QaPolicy(min_short_clip_peak_dbfs=-20.0)
        self.assertEqual(policy.min_integrated_lufs, QaDefaults.LUFS)

    def test_a_non_finite_peak_threshold_is_refused(self) -> None:
        # NaN compares false against everything, which would disable the
        # gate rather than loosen it.
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                qa_video.QaPolicy(min_short_clip_peak_dbfs=value)

    def test_a_changed_peak_threshold_is_reported_as_relaxed(self) -> None:
        # A report that does not say which thresholds moved cannot be
        # audited against the policy the project authorised.
        relaxed = qa_video.QaPolicy(min_short_clip_peak_dbfs=-60.0).relaxed_fields()
        self.assertIn("min_short_clip_peak_dbfs", relaxed)
        self.assertEqual(relaxed["min_short_clip_peak_dbfs"]["used"], -60.0)


class QaDefaults:
    LUFS = qa_video.QaPolicy().min_integrated_lufs
    PEAK = qa_video.QaPolicy().min_short_clip_peak_dbfs


if __name__ == "__main__":
    unittest.main()

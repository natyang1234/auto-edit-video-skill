"""Letterbox bars are composition, not a failed render.

A landscape lesson delivered whole inside a portrait canvas is about 68% bar.
Judging blackness across the padded frame spends most of the budget before the
picture contributes a pixel, so a dark slide, a night shot or a dark-themed
screencast fails a delivery that plays perfectly. Measured on 2026-08-06: a
letterboxed frame carrying a legible bright caption read 99% black over a
continuous 3.97s, tripping both black gates at once.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import qa_video  # noqa: E402

# 1920x1080 laid whole into a 1080x1920 canvas.
LANDSCAPE_IN_PORTRAIT = {"width": 1080, "height": 1920, "fit": "contain"}
LANDSCAPE_SOURCE = {"source": {"width": 1920, "height": 1080}}


class WhereThePictureIsTests(unittest.TestCase):
    def test_a_landscape_source_in_a_portrait_canvas_leaves_bars(self) -> None:
        rect = qa_video.letterbox_content_rect(
            LANDSCAPE_IN_PORTRAIT, LANDSCAPE_SOURCE["source"]
        )
        self.assertIsNotNone(rect)
        x, y, width, height = rect
        self.assertAlmostEqual(width, 1.0, places=6, msg="width fills the canvas")
        self.assertAlmostEqual(height, 0.31640625, places=6)
        self.assertAlmostEqual(x, 0.0, places=6)
        self.assertAlmostEqual(y, (1 - 0.31640625) / 2, places=6, msg="centred")

    def test_a_portrait_source_in_a_landscape_canvas_leaves_side_bars(self) -> None:
        rect = qa_video.letterbox_content_rect(
            {"width": 1920, "height": 1080, "fit": "contain"},
            {"width": 1080, "height": 1920},
        )
        x, _y, width, height = rect
        self.assertAlmostEqual(height, 1.0, places=6)
        self.assertAlmostEqual(width, 0.31640625, places=6)
        self.assertGreater(x, 0.0, "pillarboxed, so the picture is inset")

    def test_a_source_that_already_fits_is_not_padded(self) -> None:
        self.assertIsNone(
            qa_video.letterbox_content_rect(
                {"width": 1080, "height": 1920, "fit": "contain"},
                {"width": 540, "height": 960},
            )
        )

    def test_cropping_to_fill_declares_nothing(self) -> None:
        # A cover fit has no bars to exclude; excluding anything would hide
        # picture the viewer actually sees.
        self.assertIsNone(
            qa_video.letterbox_content_rect(
                {"width": 1080, "height": 1920, "fit": "cover"},
                {"width": 1920, "height": 1080},
            )
        )

    def test_missing_or_nonsense_geometry_declares_nothing(self) -> None:
        for canvas, source in (
            (None, {"width": 1920, "height": 1080}),
            (LANDSCAPE_IN_PORTRAIT, None),
            (LANDSCAPE_IN_PORTRAIT, {"width": 0, "height": 1080}),
            (LANDSCAPE_IN_PORTRAIT, {"width": "wide", "height": 1080}),
            ({"width": 1080, "fit": "contain"}, {"width": 1920, "height": 1080}),
        ):
            with self.subTest(canvas=canvas, source=source):
                self.assertIsNone(qa_video.letterbox_content_rect(canvas, source))


class TheGateIsToldOnceTests(unittest.TestCase):
    """Policy and geometry travel together, so no caller can take one only."""

    def test_the_rect_rides_with_the_policy_flags(self) -> None:
        args = qa_video.qa_policy_args(
            {"canvas": LANDSCAPE_IN_PORTRAIT}, LANDSCAPE_SOURCE
        )
        self.assertIn(qa_video.CONTENT_RECT_ARG, args)

    def test_a_strict_project_still_gets_the_rect(self) -> None:
        # Strict returns no policy flags at all; the geometry is not a
        # relaxation and must survive that.
        args = qa_video.qa_policy_args(
            {"canvas": LANDSCAPE_IN_PORTRAIT, "qa_policy": {"profile": "strict"}},
            LANDSCAPE_SOURCE,
        )
        self.assertEqual(args[0], qa_video.CONTENT_RECT_ARG)

    def test_an_unpadded_project_is_told_nothing(self) -> None:
        self.assertEqual(qa_video.qa_policy_args({"canvas": {"fit": "cover"}}, {}), [])

    def test_no_manifest_means_no_claim_about_geometry(self) -> None:
        self.assertEqual(
            qa_video.qa_policy_args({"canvas": LANDSCAPE_IN_PORTRAIT}, None), []
        )


class WhatTheGateBelievesTests(unittest.TestCase):
    def test_a_plausible_rect_is_accepted(self) -> None:
        self.assertEqual(
            qa_video.parse_content_rect("0.0:0.341797:1.0:0.316406"),
            (0.0, 0.341797, 1.0, 0.316406),
        )

    def test_a_rect_reaching_outside_the_frame_is_refused(self) -> None:
        for value in ("0.5:0.0:0.8:1.0", "-0.1:0.0:1.0:1.0", "0.0:0.0:1.0:1.2"):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    qa_video.parse_content_rect(value)

    def test_a_sliver_is_refused(self) -> None:
        # Otherwise a render that failed to all but one bright patch could
        # nominate that patch as the picture and pass the black gate.
        with self.assertRaises(ValueError):
            qa_video.parse_content_rect("0.4:0.4:0.2:0.2")

    def test_malformed_values_are_refused(self) -> None:
        for value in ("1:2:3", "a:b:c:d", "0:0:nan:1", "0:0:1:1:1"):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    qa_video.parse_content_rect(value)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class MeasuredOnRealPixelsTests(unittest.TestCase):
    """The bug that started this, reproduced and then required to be gone."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-letterbox-")
        cls.dark = Path(cls._tmp.name) / "dark-slide-letterboxed.mp4"
        # A dark lesson slide: 40% of the picture is lit, the rest is a black
        # background. On its own that is 60% dark and passes comfortably. Laid
        # whole into a portrait frame the bars add their 68% share on top and
        # the same picture reads as 87% black.
        cls.render(cls.dark, "1920x432", pad=True)

    @staticmethod
    def render(path: Path, lit_size: str, pad: bool) -> None:
        layout = (
            "scale=540:-2,pad=540:960:(ow-iw)/2:(oh-ih)/2:color=0x171512,setsar=1"
            if pad
            else "scale=540:-2,setsar=1"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=4",
                "-f", "lavfi", "-i", f"color=c=0xd4d4d4:s={lit_size}:d=4",
                "-filter_complex", f"[0][1]overlay=0:324,{layout}",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-r", "30", str(path),
            ],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @staticmethod
    def black_share(video: Path, rect=None) -> float:
        blacks, decoded = qa_video.picture_analysis(video, content_rect=rect)
        return sum(item["duration"] for item in blacks) / decoded

    def rect(self):
        return qa_video.letterbox_content_rect(
            LANDSCAPE_IN_PORTRAIT, LANDSCAPE_SOURCE["source"]
        )

    def test_the_same_picture_unpadded_is_never_in_question(self) -> None:
        # The control: cropped to fill, this picture raises nothing at all.
        # Only the bars make it look like a failure.
        unpadded = Path(self._tmp.name) / "same-picture-no-bars.mp4"
        self.render(unpadded, "1920x432", pad=False)
        self.assertEqual(self.black_share(unpadded), 0.0)

    def test_judging_the_padded_frame_condemns_it(self) -> None:
        self.assertGreater(
            self.black_share(self.dark),
            qa_video.QaPolicy().max_black_ratio,
            "this is the false failure the content rect exists to prevent",
        )

    def test_judging_the_picture_clears_it(self) -> None:
        self.assertEqual(
            self.black_share(self.dark, self.rect()),
            0.0,
            "a dark slide is not a failed render",
        )

    def test_a_delivery_that_really_is_black_still_fails(self) -> None:
        # The picture inside the bars is black too. Excluding the padding must
        # not excuse that, or the rect becomes a way to launder a dead render.
        wholly_black = Path(self._tmp.name) / "nothing-rendered.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=540x960:d=4",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-r", "30", str(wholly_black),
            ],
            check=True, capture_output=True,
        )
        self.assertGreater(
            self.black_share(wholly_black, self.rect()),
            qa_video.QaPolicy().max_black_ratio,
        )

    def test_a_picture_that_is_itself_almost_black_still_fails(self) -> None:
        # Where the exclusion stops: a slide carrying one small caption on
        # black is 96% dark on its own merits. Nothing about the bars caused
        # that, so removing them does not clear it — and should not, because
        # it is not distinguishable from a render that died.
        nearly = Path(self._tmp.name) / "almost-nothing-on-it.mp4"
        self.render(nearly, "700x120", pad=True)
        self.assertGreater(
            self.black_share(nearly, self.rect()),
            qa_video.QaPolicy().max_black_ratio,
        )

    def test_the_report_says_which_area_it_judged(self) -> None:
        work = Path(self._tmp.name)
        rect = qa_video.letterbox_content_rect(
            LANDSCAPE_IN_PORTRAIT, LANDSCAPE_SOURCE["source"]
        )
        payload, _ok = qa_video.inspect(
            self.dark, work / "report.json", work / "contact.png", content_rect=rect
        )
        recorded = payload["black_detection"]["content_rect"]
        self.assertAlmostEqual(recorded["height"], 0.31640625, places=6)

        plain, _ok = qa_video.inspect(
            self.dark, work / "plain.json", work / "plain.png"
        )
        self.assertIsNone(
            plain["black_detection"]["content_rect"],
            "no rect means the whole frame was judged, and the report says so",
        )


if __name__ == "__main__":
    unittest.main()

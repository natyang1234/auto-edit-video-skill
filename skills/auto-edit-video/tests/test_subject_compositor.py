from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from subject_compositor import (  # noqa: E402
    background_video_filter,
    calculate_working_size,
    subject_paste_box,
)
from graphic_package import _owned_background_asset  # noqa: E402
from template_catalog import default_video_template_state  # noqa: E402


class SubjectCompositorTests(unittest.TestCase):
    def test_working_size_preserves_aspect_and_even_dimensions(self) -> None:
        self.assertEqual(calculate_working_size(1920, 1080, 640), (640, 360))
        self.assertEqual(calculate_working_size(1080, 1920, 640), (360, 640))

    def test_background_filter_is_bounded_to_canvas(self) -> None:
        cover = background_video_filter(1080, 1920, "cover", 0)
        contain = background_video_filter(1080, 1920, "contain", 4)
        self.assertIn("force_original_aspect_ratio=increase", cover)
        self.assertIn("crop=1080:1920", cover)
        self.assertIn("force_original_aspect_ratio=decrease", contain)
        self.assertIn("pad=1080:1920", contain)
        self.assertIn("gblur=sigma=4.000", contain)

    def test_subject_position_is_center_based_and_user_scalable(self) -> None:
        box = subject_paste_box(
            crop_size=(300, 600),
            canvas_size=(1080, 1920),
            x_percent=50,
            y_percent=54,
            user_scale=1,
        )
        self.assertEqual(box[3], round(1920 * 0.82))
        self.assertAlmostEqual(box[0] + box[2] / 2, 540, delta=1)
        self.assertAlmostEqual(box[1] + box[3] / 2, 1920 * 0.54, delta=1)

    def test_background_asset_must_be_owned_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            owned = root / "assets/background.png"
            owned.write_bytes(b"png")
            state = default_video_template_state("cutout-image")
            state["background"]["source"] = "assets/background.png"
            self.assertEqual(_owned_background_asset(root, state), owned.resolve())
            owned.unlink()
            outside = root / "outside.png"
            outside.write_bytes(b"outside")
            owned.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                _owned_background_asset(root, state)


if __name__ == "__main__":
    unittest.main()

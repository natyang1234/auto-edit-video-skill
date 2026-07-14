from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from template_catalog import (  # noqa: E402
    DEFAULT_VIDEO_TEMPLATE_ID,
    VIDEO_TEMPLATES,
    default_video_template_state,
    template_readiness_errors,
    upgrade_video_template_state,
    validate_video_template_state,
)


class VideoTemplateCatalogTests(unittest.TestCase):
    def test_catalog_separates_fixed_dynamic_and_cutout_templates(self) -> None:
        groups = [template["group"] for template in VIDEO_TEMPLATES.values()]
        self.assertEqual(groups.count("fixed"), 3)
        self.assertEqual(groups.count("dynamic"), 2)
        self.assertEqual(groups.count("cutout"), 3)
        self.assertEqual(len(VIDEO_TEMPLATES), 8)

        fixed = [item for item in VIDEO_TEMPLATES.values() if item["group"] == "fixed"]
        self.assertTrue(all(item["camera_motion"] == "none" for item in fixed))
        cutout = [item for item in VIDEO_TEMPLATES.values() if item["group"] == "cutout"]
        self.assertTrue(all(item["subject_mode"] == "cutout" for item in cutout))
        self.assertEqual(
            {item["background_mode"] for item in cutout},
            {"solid", "image", "video"},
        )

    def test_every_default_template_state_is_valid(self) -> None:
        for template_id in VIDEO_TEMPLATES:
            with self.subTest(template_id=template_id):
                state = default_video_template_state(template_id)
                self.assertEqual(state["id"], template_id)
                self.assertEqual(validate_video_template_state(state), [])

    def test_old_projects_receive_dynamic_craft_without_overwriting_other_state(self) -> None:
        state = {
            "schema_version": 1,
            "graphic_package_style": "craft-stack",
            "director_style": "teacher-punch",
        }
        self.assertTrue(upgrade_video_template_state(state))
        self.assertEqual(state["video_template"]["id"], DEFAULT_VIDEO_TEMPLATE_ID)
        self.assertEqual(state["director_style"], "teacher-punch")
        self.assertFalse(upgrade_video_template_state(state))

    def test_background_asset_requirements_fail_closed(self) -> None:
        image_state = default_video_template_state("cutout-image")
        image_state["background"]["source"] = None
        self.assertTrue(
            any("image background asset" in error for error in template_readiness_errors(image_state))
        )
        image_state["background"]["source"] = "assets/background.mp4"
        self.assertTrue(
            any("image background asset" in error for error in validate_video_template_state(image_state))
        )
        image_state["background"]["source"] = "assets/background.png"
        self.assertEqual(validate_video_template_state(image_state), [])

        video_state = default_video_template_state("cutout-video")
        video_state["background"]["source"] = "../outside.mov"
        self.assertTrue(
            any("under assets/" in error for error in validate_video_template_state(video_state))
        )
        video_state["background"]["source"] = "assets/background.mov"
        self.assertEqual(validate_video_template_state(video_state), [])

    def test_subject_controls_have_bounded_values(self) -> None:
        state = default_video_template_state("cutout-solid")
        state["subject"]["scale"] = 9
        state["subject"]["mask_stride"] = 0
        errors = validate_video_template_state(state)
        self.assertTrue(any("subject scale" in error for error in errors))
        self.assertTrue(any("mask_stride" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

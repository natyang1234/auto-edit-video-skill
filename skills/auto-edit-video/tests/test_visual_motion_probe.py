from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from render_editor_timeline import image_filter  # noqa: E402
from visual_motion_probe import (  # noqa: E402
    VisualMotionProbeError,
    measure_declared_motion,
)


class VisualMotionProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="visual-motion-probe-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source(self) -> tuple[Path, str]:
        source = self.root / "frozen-card.png"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=white:s=60x60:r=1",
                "-frames:v", "1", "-update", "1", str(source),
            ],
            check=True,
            capture_output=True,
        )
        return source, hashlib.sha256(source.read_bytes()).hexdigest()

    def _low_alpha_source(self) -> tuple[Path, str]:
        source = self.root / "low-alpha-card.png"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "color=c=white@0.06:s=10x10:r=1,format=rgba",
                "-frames:v", "1", "-update", "1", str(source),
            ],
            check=True,
            capture_output=True,
        )
        return source, hashlib.sha256(source.read_bytes()).hexdigest()

    def _base(self, name: str, source: str) -> Path:
        base = self.root / f"{name}-base.mkv"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", source,
                "-an", "-c:v", "ffv1", "-pix_fmt", "yuv420p", str(base),
            ],
            check=True,
            capture_output=True,
        )
        return base

    def _binding(self, source: Path, digest: str, animation: str) -> dict[str, Any]:
        return {
            "artifact_sha256": digest,
            "source_path": str(source),
            "source_sha256": digest,
            "source_kind": "image",
            "canvas_width": 320,
            "canvas_height": 240,
            "source_start_sample": 0,
            "source_end_sample": 96_000,
            "placement": {
                "width_percent": 18.75,
                "x_percent": 25.0,
                "y_percent": 20.833333,
                "animation": animation,
            },
        }

    def _render(
        self,
        name: str,
        base: Path,
        source: Path | None,
        binding: dict[str, Any] | None,
        *,
        fps: int = 30,
        duration: float = 2.0,
    ) -> Path:
        video = self.root / f"{name}.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base)
        ]
        if source is None or binding is None:
            command.extend(
                [
                    "-t", str(duration), "-an", "-c:v", "libx264",
                    "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                    str(video),
                ]
            )
        else:
            command.extend(
                ["-loop", "1", "-framerate", str(fps), "-i", str(source)]
            )
            placement = binding["placement"]
            overlay = {
                "id": "card",
                "type": "image",
                "start": binding["source_start_sample"] / 48_000,
                "end": binding["source_end_sample"] / 48_000,
                "style": {
                    "width": placement["width_percent"],
                    "x": placement["x_percent"],
                    "y": placement["y_percent"],
                    "animation": placement["animation"],
                },
            }
            graph = image_filter(
                "0:v",
                "candidate",
                "1:v",
                overlay,
                binding["canvas_width"],
                binding["canvas_height"],
            )
            command.extend(
                [
                    "-filter_complex", graph, "-map", "[candidate]", "-t", str(duration), "-an",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p", str(video),
                ]
            )
        subprocess.run(command, check=True, capture_output=True)
        return video

    def _evidence(
        self,
        scene_id: str,
        base: Path,
        binding: dict[str, Any],
        *,
        roi: dict[str, float] | None = None,
        fps: int = 30,
    ) -> dict[str, Any]:
        public_binding = {
            key: value for key, value in binding.items() if key != "source_path"
        }
        return {
            "items": [
                {
                    "id": scene_id,
                    "major_graphic": True,
                    "static_fallback": False,
                    "artifact_hash": binding["artifact_sha256"],
                    "motion_window_start_sample": binding["source_start_sample"],
                    "motion_window_end_sample": binding["source_end_sample"],
                    "graphic_roi": roi
                    or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.5},
                }
            ],
            "motion_input": {
                "base_path": str(base),
                "base_sha256": hashlib.sha256(base.read_bytes()).hexdigest(),
                "canvas_width": binding["canvas_width"],
                "canvas_height": binding["canvas_height"],
                "fps": fps,
            },
            "frozen_graphics": {scene_id: binding},
            "motion_attribution": {scene_id: public_binding},
        }

    def test_real_motion_inside_the_declared_graphic_roi_is_detected(self) -> None:
        source, digest = self._source()
        base = self._base("true-motion", "color=c=black:s=320x240:r=30:d=2")
        binding = self._binding(source, digest, "pan")
        video = self._render("true-motion", base, source, binding)

        measured = measure_declared_motion(
            video, self._evidence("moving-scene", base, binding)
        )

        scene = measured["moving-scene"]
        self.assertEqual(scene["sample_positions"], [9_600, 48_000, 86_400])
        self.assertTrue(
            all(sample["matched"] for sample in scene["candidate_matches"]), scene
        )
        self.assertTrue(scene["detected"], scene)

    def test_static_final_pixels_do_not_satisfy_declared_motion(self) -> None:
        source, digest = self._source()
        base = self._base("static", "color=c=gray:s=320x240:r=30:d=2")
        binding = self._binding(source, digest, "none")
        video = self._render("static", base, source, binding)

        measured = measure_declared_motion(
            video, self._evidence("static-scene", base, binding)
        )

        scene = measured["static-scene"]
        self.assertTrue(
            all(sample["matched"] for sample in scene["candidate_matches"]), scene
        )
        self.assertFalse(scene["detected"])

    def test_motion_outside_the_graphic_roi_cannot_satisfy_the_scene(self) -> None:
        source, digest = self._source()
        base = self._base(
            "presenter-motion",
            "color=c=black:s=320x240:r=30:d=2,drawbox=x='20+60*t':y=170:w=60:h=60:c=white:t=fill",
        )
        binding = self._binding(source, digest, "none")
        video = self._render("presenter-motion", base, source, binding)

        measured = measure_declared_motion(
            video,
            self._evidence("graphic-still-presenter-moves", base, binding),
        )

        self.assertFalse(measured["graphic-still-presenter-moves"]["detected"])

    def test_moving_background_cannot_credit_a_static_frozen_graphic(self) -> None:
        source, digest = self._source()
        base = self._base("moving-background", "testsrc2=s=320x240:r=30:d=2")
        binding = self._binding(source, digest, "none")
        video = self._render("moving-background", base, source, binding)

        measured = measure_declared_motion(
            video,
            self._evidence(
                "static-card-moving-background",
                base,
                binding,
                roi={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            ),
        )

        scene = measured["static-card-moving-background"]
        self.assertTrue(
            all(sample["matched"] for sample in scene["candidate_matches"]), scene
        )
        self.assertFalse(scene["detected"])

    def test_missing_card_cannot_earn_motion_from_the_background(self) -> None:
        source, digest = self._source()
        base = self._base("missing-card", "testsrc2=s=320x240:r=30:d=2")
        binding = self._binding(source, digest, "pan")
        video = self._render("missing-card", base, None, None)

        scene = measure_declared_motion(
            video,
            self._evidence("missing-card", base, binding, roi={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
        )["missing-card"]

        self.assertFalse(all(sample["matched"] for sample in scene["candidate_matches"]))
        self.assertFalse(scene["detected"])

    def test_missing_sparse_low_alpha_card_cannot_hide_in_the_full_roi(self) -> None:
        source, digest = self._low_alpha_source()
        base = self._base("low-alpha-missing", "color=c=black:s=320x180:r=30:d=2")
        binding = self._binding(source, digest, "pan")
        binding["canvas_height"] = 180
        binding["placement"].update(
            {"width_percent": 3.125, "x_percent": 50.0, "y_percent": 50.0}
        )
        video = self._render("low-alpha-missing", base, None, None)

        scene = measure_declared_motion(
            video,
            self._evidence(
                "low-alpha-missing",
                base,
                binding,
                roi={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            ),
        )["low-alpha-missing"]

        self.assertFalse(all(sample["matched"] for sample in scene["candidate_matches"]))
        self.assertFalse(scene["detected"])

    def test_empty_alpha_support_fails_closed(self) -> None:
        source = self.root / "transparent-card.png"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "color=c=white@0.0:s=10x10:r=1,format=rgba",
                "-frames:v", "1", "-update", "1", str(source),
            ],
            check=True,
            capture_output=True,
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        base = self._base("transparent", "color=c=black:s=320x240:r=30:d=2")
        binding = self._binding(source, digest, "pan")
        candidate = self._render("transparent", base, None, None)

        with self.assertRaisesRegex(VisualMotionProbeError, "alpha-supported"):
            measure_declared_motion(
                candidate,
                self._evidence("transparent", base, binding),
            )

    def test_boolean_and_nonfinite_placement_values_fail_closed(self) -> None:
        source, digest = self._source()
        base = self._base("invalid-placement", "color=c=black:s=320x240:r=30:d=2")
        candidate = self._render("invalid-placement", base, None, None)
        for invalid in (True, float("nan")):
            with self.subTest(invalid=invalid):
                binding = self._binding(source, digest, "pan")
                binding["placement"]["width_percent"] = invalid
                with self.assertRaisesRegex(VisualMotionProbeError, "finite number"):
                    measure_declared_motion(
                        candidate,
                        self._evidence("invalid-placement", base, binding),
                    )

    def test_static_hold_cannot_impersonate_the_declared_pan_states(self) -> None:
        source, digest = self._source()
        base = self._base("static-hold", "testsrc2=s=320x240:r=30:d=2")
        declared = self._binding(source, digest, "pan")
        rendered = self._binding(source, digest, "none")
        video = self._render("static-hold", base, source, rendered)

        scene = measure_declared_motion(
            video,
            self._evidence("static-hold", base, declared, roi={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
        )["static-hold"]

        self.assertFalse(all(sample["matched"] for sample in scene["candidate_matches"]))
        self.assertFalse(scene["detected"])

    def test_short_slide_uses_authority_fps_for_image_alpha_states(self) -> None:
        source, digest = self._source()
        base = self._base("short-slide", "testsrc2=s=320x240:r=15:d=4")
        binding = self._binding(source, digest, "slide-up")
        binding["source_start_sample"] = 120_000
        binding["source_end_sample"] = 130_560
        video = self._render(
            "short-slide", base, source, binding, fps=15, duration=4.0
        )

        scene = measure_declared_motion(
            video,
            self._evidence("short-slide", base, binding, fps=15),
        )["short-slide"]

        self.assertTrue(
            all(sample["matched"] for sample in scene["candidate_matches"]), scene
        )
        self.assertTrue(scene["detected"], scene)


if __name__ == "__main__":
    unittest.main()

"""Phase 1b N4: flat-background golden acceptance for compositor captions.

PRD 9.6: golden frames must prove MP4 preview and final share glyph runs,
positions, colours and effect timing — not just state hashes. Strategy:
render a caption (with an effect span and fade animation) over a flat
background, sample entry/mid/exit frames, and verify inside the artifact
ROI (span colour present, base colour present, fade progressing) and
outside it (background untouched). Preview and final mid-frames must match
pixel-for-pixel within tolerance — same raster source.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import caption_compositor  # noqa: E402
from editor_server import gate_revision  # noqa: E402
from render_editor_timeline import ffmpeg_path  # noqa: E402

BG = (62, 62, 62)  # 0x3E3E3E flat background
SPAN_COLOR = (255, 85, 51)  # #FF5533
BASE_COLOR = (247, 242, 232)  # #F7F2E8


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError("not a binary PPM")
    fields: list[bytes] = []
    index = 2
    while len(fields) < 3:
        while index < len(data) and data[index : index + 1].isspace():
            index += 1
        if data[index : index + 1] == b"#":
            while data[index : index + 1] != b"\n":
                index += 1
            continue
        start = index
        while index < len(data) and not data[index : index + 1].isspace():
            index += 1
        fields.append(data[start:index])
    width, height, _maxval = (int(f) for f in fields)
    index += 1
    return width, height, data[index : index + width * height * 3]


def extract_frame(video: Path, timestamp: float, destination: Path) -> tuple[int, int, bytes]:
    subprocess.run(
        [
            ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
            "-frames:v", "1", "-f", "image2", "-c:v", "ppm", str(destination),
        ],
        check=True, capture_output=True,
    )
    return read_ppm(destination)


def near(pixel: tuple[int, int, int], target: tuple[int, int, int], tol: int) -> bool:
    return all(abs(p - t) <= tol for p, t in zip(pixel, target))


def roi_stats(
    frame: tuple[int, int, bytes],
    rect: tuple[int, int, int, int],
) -> dict[str, float]:
    width, _height, pixels = frame
    x0, y0, x1, y1 = rect
    span_hits = base_hits = 0
    deviation = 0.0
    count = 0
    for y in range(y0, y1):
        row = y * width * 3
        for x in range(x0, x1):
            offset = row + x * 3
            pixel = (pixels[offset], pixels[offset + 1], pixels[offset + 2])
            if near(pixel, SPAN_COLOR, 60):
                span_hits += 1
            if near(pixel, BASE_COLOR, 40):
                base_hits += 1
            deviation += sum(abs(p - b) for p, b in zip(pixel, BG))
            count += 1
    return {
        "span_hits": span_hits,
        "base_hits": base_hits,
        "mean_deviation": deviation / max(count, 1),
    }


@unittest.skipUnless(
    shutil.which("ffmpeg") and caption_compositor.compositor_available(),
    "needs ffmpeg and macOS CoreText",
)
class CaptionGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="caption-golden-")
        cls.project = Path(cls._tmp.name) / "project"
        for name in ("source", "working", "renders"):
            (cls.project / name).mkdir(parents=True, exist_ok=True)
        source = cls.project / "source/source.mp4"
        subprocess.run(
            [
                ffmpeg_path(), "-y",
                "-f", "lavfi", "-i", "color=c=0x3E3E3E:s=360x640:d=1.2:rate=30",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(source),
            ],
            check=True, capture_output=True,
        )
        (cls.project / "project.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "caption-golden",
                    "source": {
                        "staged_path": "source/source.mp4",
                        "duration_s": 1.2,
                        "sha256": "a" * 64,
                    },
                    "approvals": {},
                }
            ),
            "utf-8",
        )
        cls.state = {
            "schema_version": 2,
            "project_id": "caption-golden",
            "source_sha256": "a" * 64,
            "segments": [
                {
                    "id": "segment-abcdef012345",
                    "source_start": 0.0,
                    "source_end": 1.2,
                    "origin": "default_full_source",
                }
            ],
            "variants": [],
            "rights": {"asserted": False, "assertion_revision": None},
            "canvas": {
                "platform_id": "instagram-reels",
                "width": 360,
                "height": 640,
                "fps": 30,
                "fit": "cover",
            },
            "director_style": "teacher-punch",
            "caption_defaults": {},
            "highlights": [],
            "asset_digests": {},
            "overlays": [
                {
                    "id": "caption-golden",
                    "type": "caption",
                    "text": "看到 It 想到 to V",
                    "start": 0.2,
                    "end": 1.0,
                    "visible": True,
                    "style": {
                        "font_size": 40,
                        "color": "#F7F2E8",
                        # Ship-realistic outline. A centred stroke eats inward,
                        # so anything above 0 is where fill survival is decided.
                        "stroke_width": 5,
                        "stroke_color": "#17130F",
                        "x": 50,
                        "y": 50,
                        "animation": "fade",
                    },
                    "layout": {"x": 10, "y": 40, "width": 80, "height": 20},
                    "effect_spans": [
                        {
                            "id": "fx-golden",
                            "text": "It",
                            "start_char": 3,
                            "end_char": 5,
                            "style": {
                                "effect": "pop",
                                "color": "#FF5533",
                                "font_scale": 1.4,
                            },
                        }
                    ],
                }
            ],
        }
        (cls.project / "working/editor_state.json").write_text(
            json.dumps(cls.state, ensure_ascii=False), "utf-8"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def render(self, quality: str, output: Path) -> None:
        if quality == "final":
            manifest = json.loads((self.project / "project.json").read_text("utf-8"))
            state = json.loads(
                (self.project / "working/editor_state.json").read_text("utf-8")
            )
            manifest["approvals"] = {
                "timeline": {
                    "approved": True,
                    "state_revision": gate_revision(self.project, "timeline", state),
                }
            }
            (self.project / "project.json").write_text(json.dumps(manifest), "utf-8")
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "render_editor_timeline.py"),
                "--project-dir", str(self.project),
                "--output", str(output),
                "--quality", quality,
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def roi_rect(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        plan = json.loads(
            (self.project / "working/caption_render_plan.json").read_text("utf-8")
        )
        artifact = plan["items"][0]["artifact"]
        scale = frame_width / 360.0
        art_w = artifact["width"] * scale
        art_h = artifact["height"] * scale
        cx = frame_width * 0.5
        cy = frame_height * 0.5
        x0 = max(0, int(cx - art_w / 2))
        y0 = max(0, int(cy - art_h / 2))
        return (x0, y0, min(frame_width, int(cx + art_w / 2)), min(frame_height, int(cy + art_h / 2)))

    def test_preview_and_final_share_the_caption_raster_truth(self) -> None:
        preview = self.project / "renders/golden-preview.mp4"
        final = self.project / "renders/golden-final.mp4"
        self.render("preview", preview)
        self.render("final", final)

        scratch = Path(self._tmp.name)
        entry_t, mid_t, exit_t = 0.24, 0.60, 0.97
        frames = {}
        for label, timestamp in (("entry", entry_t), ("mid", mid_t), ("exit", exit_t)):
            frames[label] = extract_frame(final, timestamp, scratch / f"final-{label}.ppm")
        width, height, _ = frames["mid"]
        rect = self.roi_rect(width, height)

        mid = roi_stats(frames["mid"], rect)
        self.assertGreaterEqual(mid["span_hits"], 10, "span colour must appear mid-caption")
        self.assertGreaterEqual(mid["base_hits"], 30, "base text colour must appear mid-caption")

        entry = roi_stats(frames["entry"], rect)
        exit_stats = roi_stats(frames["exit"], rect)
        self.assertLess(entry["mean_deviation"], mid["mean_deviation"],
                        "fade-in: entry frame must be closer to the background")
        self.assertLess(exit_stats["mean_deviation"], mid["mean_deviation"],
                        "fade-out: exit frame must be closer to the background")

        # Outside the ROI the background must stay flat.
        outside = roi_stats(frames["mid"], (0, 0, width, max(1, rect[1] - 8)))
        self.assertLess(outside["mean_deviation"], 12.0, "background above the caption must stay flat")
        self.assertEqual(outside["span_hits"], 0)

        # Preview and final must draw from the same raster truth.
        preview_mid = extract_frame(preview, mid_t, scratch / "preview-mid.ppm")
        self.assertEqual(preview_mid[0], width, "identical canvas ⇒ identical preview dims")
        preview_stats = roi_stats(preview_mid, rect)
        self.assertLessEqual(
            abs(preview_stats["mean_deviation"] - mid["mean_deviation"]), 10.0,
            "preview/final caption ROI must match (same pixel source)",
        )
        self.assertGreaterEqual(preview_stats["span_hits"], 10)


if __name__ == "__main__":
    unittest.main()

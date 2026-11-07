import sys
import shutil
import time
import subprocess
import json
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fx_preview_jobs as jobs
import render_editor_timeline as timeline


@pytest.mark.parametrize("command,current", [
    (["-filter_complex", "graph", "-map", "[v]", "-map", "[aout]", "-map", "[extra]"], "v"),
    (["-filter_complex", "graph", "-map", "[wrong]", "-map", "0:a?"], "v"),
    (["-filter_complex", "graph", "-map", "[v]", "-map", "[wrong]"], "v"),
    (["-filter_complex", "graph", "-filter_complex", "other", "-map", "[v]", "-map", "[aout]"], "v"),
])
def test_window_mapping_rejects_unfamiliar_graph(command, current):
    with pytest.raises(ValueError, match="preview output window"):
        timeline._preview_window_map_indices(command, current)


def test_window_mapping_accepts_expected_streams():
    assert timeline._preview_window_map_indices(["-filter_complex", "graph", "-map", "[v]", "-map", "0:a?"], "v") == (1, 3, 5)
    assert timeline._preview_window_map_indices(["-filter_complex", "graph", "-map", "[v]", "-map", "[aout]"], "v") == (1, 3, 5)


def test_output_window_rejects_final_and_unfrozen_preview(tmp_path):
    for quality, snapshot, fx_quality in [("final", tmp_path / "s.json", "standard"),
                                          ("preview", None, "standard"),
                                          ("preview", tmp_path / "s.json", None)]:
        with pytest.raises(ValueError, match="frozen FX preview"):
            timeline.render_project(tmp_path, tmp_path / "out.mp4", quality, snapshot_path=snapshot,
                                    preview_fx_quality=fx_quality, preview_output_window=(1.0, 2.0))
    with pytest.raises(ValueError, match="only available"):
        timeline.build_render_command(tmp_path, {}, {}, tmp_path / "x.mp4", "final",
                                      preview_output_window=(1.0, 2.0))
    for window in ((float("nan"), 2), (-1, 2), (1, 0), (True, 2)):
        with pytest.raises(ValueError, match="invalid preview output window"):
            timeline.render_project(tmp_path, tmp_path / "x.mp4", "preview", snapshot_path=tmp_path / "s.json",
                                    preview_fx_quality="draft", preview_output_window=window)
        with pytest.raises(ValueError, match="invalid preview output window"):
            timeline.build_render_command(tmp_path, {}, {}, tmp_path / "x.mp4", "preview",
                                          preview_fx_quality="draft", preview_output_window=window)
    with pytest.raises(ValueError, match="frozen FX preview"):
        timeline.render_project(tmp_path, tmp_path / "x.mp4", "preview", snapshot_path=tmp_path / "s.json",
                                variant_id="other", preview_fx_quality="draft", preview_output_window=(1, 1))


FFMPEG_DEPENDENCY_SKIP = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for real media timing coverage",
)


def _probe_video_frames(video: Path) -> tuple[list[bytes], list[float]]:
    stream = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(video),
            ],
            text=True,
            timeout=30,
        )
    )["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    timestamps = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(video),
            ],
            text=True,
            timeout=30,
        )
    )["frames"]
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    frame_size = width * height * 3
    assert frame_size > 0
    assert len(raw) % frame_size == 0
    frames = [raw[index:index + frame_size] for index in range(0, len(raw), frame_size)]
    pts = [float(frame["best_effort_timestamp_time"]) for frame in timestamps]
    assert len(frames) == len(pts)
    return frames, pts


def _probe_duration(video: Path) -> float:
    payload = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(video),
            ],
            text=True,
            timeout=30,
        )
    )
    return float(payload["format"]["duration"])


def _frame_mean_abs_error(left: bytes, right: bytes) -> float:
    assert len(left) == len(right)
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _assert_aligned_frames(
    reference: list[bytes], candidate: list[bytes], offset: int, label: str,
) -> list[float]:
    assert len(candidate) == 10, f"{label}: expected 10 frames, got {len(candidate)}"
    assert offset >= 0 and offset + len(candidate) <= len(reference)
    errors = [
        _frame_mean_abs_error(reference[offset + index], frame)
        for index, frame in enumerate(candidate)
    ]
    assert all(error <= 12 for error in errors), (
        f"{label}: frame content is not aligned; errors={errors}"
    )
    return errors


@FFMPEG_DEPENDENCY_SKIP
@pytest.mark.parametrize("has_audio,multi", [(True, False), (False, False), (True, True), (False, True)])
def test_window_decodes_source_and_silent_audio_routes(tmp_path, has_audio, multi):
    source = tmp_path / "source.mp4"
    make = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x96:r=10:d=3"]
    if has_audio:
        make += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac"]
    subprocess.run(make + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, timeout=30)
    segments = ([{"source_start": 0, "source_end": 1.5}, {"source_start": 2, "source_end": 3}]
                if multi else [{"source_start": 0, "source_end": 3}])
    state = {"canvas": {"width": 64, "height": 96, "fps": 10}, "segments": segments,
             "overlays": [], "subject_tracking": False}
    manifest = {"source": {"staged_path": "source.mp4", "duration_s": 3, "has_audio": has_audio}}
    full = tmp_path / "full.mp4"
    output = tmp_path / "preview.mp4"
    wrong = tmp_path / "wrong-start.mp4"
    window_start = 1.0 if multi else 0.5
    expected_full_duration = 2.5 if multi else 3.0
    with patch.object(timeline, "source_has_audible_signal", return_value=has_audio):
        full_cmd = timeline.build_render_command(
            tmp_path, state, manifest, full, "preview", preview_fx_quality="draft"
        )
        window_cmd = timeline.build_render_command(
            tmp_path,
            state,
            manifest,
            output,
            "preview",
            preview_fx_quality="draft",
            preview_output_window=(window_start, 1.0),
        )
        wrong_cmd = timeline.build_render_command(
            tmp_path,
            state,
            manifest,
            wrong,
            "preview",
            preview_fx_quality="draft",
            preview_output_window=(0.0, 1.0),
        )
    subprocess.run(full_cmd, check=True, capture_output=True, timeout=30)
    subprocess.run(window_cmd, check=True, capture_output=True, timeout=30)
    subprocess.run(wrong_cmd, check=True, capture_output=True, timeout=30)
    for rendered in (full, output, wrong):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(rendered), "-f", "null", "-"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    assert abs(_probe_duration(full) - expected_full_duration) < .15
    assert abs(_probe_duration(output) - 1.0) < .15
    assert abs(_probe_duration(wrong) - 1.0) < .15

    full_frames, full_pts = _probe_video_frames(full)
    preview_frames, preview_pts = _probe_video_frames(output)
    wrong_frames, wrong_pts = _probe_video_frames(wrong)
    assert full_pts and abs(full_pts[0]) < .01
    assert preview_pts and abs(preview_pts[0]) < .01
    assert wrong_pts and abs(wrong_pts[0]) < .01
    offset = round(window_start * 10)
    aligned_errors = _assert_aligned_frames(full_frames, preview_frames, offset, "aligned")
    wrong_errors = [
        _frame_mean_abs_error(full_frames[offset + index], frame)
        for index, frame in enumerate(wrong_frames)
    ]
    aligned_mean = sum(aligned_errors) / len(aligned_errors)
    wrong_mean = sum(wrong_errors) / len(wrong_errors)
    print(
        f"frame alignment has_audio={has_audio} multi={multi} "
        f"aligned_mean_abs={aligned_mean:.3f} wrong_start_mean_abs={wrong_mean:.3f}"
    )
    assert wrong_mean > 15, f"wrong-start control unexpectedly matched: errors={wrong_errors}"
    with pytest.raises(AssertionError, match="wrong-start"):
        _assert_aligned_frames(full_frames, wrong_frames, offset, "wrong-start")


def test_eta_requires_three_matching_samples_and_uses_elapsed():
    manager = object.__new__(jobs.FxPreviewJobManager)
    manager._timing_samples = {}
    now = time.monotonic()
    job = {"id": "test", "state": "running", "phase": "rendering", "message": "working",
           "timing_started": now - 2, "timing_phase_started": now - 1, "phase_timings_s": {},
           "preview_options": {"mode": "single", "quality": "draft"}, "overlay": {"start": 0, "end": 8}}
    group = manager._timing_group(job)
    manager._timing_samples[group] = [10, 12]
    assert manager._public_job_locked(job)["eta_remaining_s"] is None
    manager._timing_samples[group].append(11)
    status = manager._public_job_locked(job)
    assert 8 < status["eta_remaining_s"] < 10
    assert status["phase_timings_s"]["rendering"] >= 1
    job.update(
        phase="cancelling",
        error={"code": "termination_unverified", "message": "retry cancel"},
    )
    assert manager._public_job_locked(job)["eta_remaining_s"] is None
    job.update(phase="rendering")
    assert manager._public_job_locked(job)["eta_remaining_s"] is None
    job["error"] = None
    job["preview_options"]["quality"] = "standard"
    assert manager._public_job_locked(job)["eta_remaining_s"] is None
    job.update(state="ready", phase="ready", timing_finished=now + 3)
    with patch.object(jobs.time, "monotonic", return_value=now + 100):
        assert manager._public_job_locked(job)["elapsed_s"] == 5

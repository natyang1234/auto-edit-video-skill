import sys
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
    output = tmp_path / "preview.mp4"
    with patch.object(timeline, "source_has_audible_signal", return_value=has_audio):
        cmd = timeline.build_render_command(tmp_path, state, manifest, output, "preview",
                                            preview_fx_quality="draft", preview_output_window=(0.5, 1))
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], check=True, timeout=30)
    info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                               "-of", "json", str(output)]))
    assert abs(float(info["format"]["duration"]) - 1) < .15


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
    job["preview_options"]["quality"] = "standard"
    assert manager._public_job_locked(job)["eta_remaining_s"] is None
    job.update(state="ready", phase="ready", timing_finished=now + 3)
    with patch.object(jobs.time, "monotonic", return_value=now + 100):
        assert manager._public_job_locked(job)["elapsed_s"] == 5

"""Renderer/process integration for complete-composite diagnostics."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fx_preview_render as render  # noqa: E402
import fx_preview_jobs as jobs  # noqa: E402
import render_editor_timeline as timeline  # noqa: E402


def _validated_media_at(width: int, height: int):
    def validate(
        _path: Path,
        _expected: float,
        *,
        metadata: dict[str, int] | None = None,
    ) -> bool:
        if metadata is not None:
            metadata.update({"width": width, "height": height})
        return True

    return validate


def test_reference_snapshot_is_derived_without_mutating_frozen_state(tmp_path: Path) -> None:
    snapshot = {
        "render_id": "fx-preview",
        "state_revision": "old",
        "state": {
            "revision": "old",
            "asset_digests": {"assets/title.png": "a" * 64},
            "overlays": [{"id": "fx", "type": "fx", "code": "T3"}],
        },
    }
    original = copy.deepcopy(snapshot)
    fake_timeline = mock.Mock()
    fake_timeline.referenced_asset_digests.return_value = {}
    fake_timeline.editor_state_revision.return_value = "b" * 64

    reference = render._reference_snapshot(snapshot, tmp_path, fake_timeline)

    assert snapshot == original
    assert reference["state"]["overlays"] == []
    assert reference["state"]["asset_digests"] == {}
    assert reference["state"]["revision"] == "b" * 64
    assert reference["state_revision"] == "b" * 64
    assert reference["render_id"] == "fx-preview-reference"
    fake_timeline.referenced_asset_digests.assert_called_once_with(
        tmp_path, reference["state"]
    )


def test_composite_child_timeout_is_hard_and_advisory(tmp_path: Path) -> None:
    composite = tmp_path / "composite.mp4"
    reference = tmp_path / "reference.mp4"
    source = tmp_path / "source.mp4"
    composite.write_bytes(b"finished-video")
    reference.write_bytes(b"reference-video")
    source.write_bytes(b"source-video")
    scratch = tmp_path / "scratch"

    with mock.patch.object(
        render.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["composite-check"], 30),
    ) as run:
        receipt = render._run_composition_check(
            composite=composite,
            reference=reference,
            source=source,
            time_map=[
                {
                    "source_start_s": 0.0,
                    "source_end_s": 1.0,
                    "output_start_s": 0.0,
                    "output_end_s": 1.0,
                }
            ],
            scratch=scratch,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_text=True,
            scope="full-timeline",
            frame_size={"width": 540, "height": 960},
        )

    assert receipt["status"] == "unmeasured"
    assert receipt["frame_size"] == {"width": 540, "height": 960}
    assert receipt["findings"] == [{"kind": "analysis_incomplete"}]
    assert receipt["media_sha256"] == hashlib.sha256(
        composite.read_bytes()
    ).hexdigest()
    assert run.call_args.kwargs["timeout"] == 30.0
    assert "start_new_session" not in run.call_args.kwargs
    assert scratch.is_dir()
    sanitized = jobs._sanitize_composition_check(
        receipt,
        scope="full-timeline",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        media_sha256=hashlib.sha256(composite.read_bytes()).hexdigest(),
        frame_size={"width": 540, "height": 960},
        render_duration_s=1.0,
        time_map=[
            {
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            }
        ],
    )
    assert sanitized == receipt


@pytest.mark.parametrize(
    "failure_mode,clock_values,expected_elapsed",
    (
        ("nonzero", (100.0, 102.5), 2.5),
        ("missing", (100.0, 105.0), 5.0),
        ("invalid", (100.0, 200.5), 60.0),
    ),
)
def test_composite_child_failures_report_bounded_real_elapsed(
    tmp_path: Path,
    failure_mode: str,
    clock_values: tuple[float, float],
    expected_elapsed: float,
) -> None:
    composite = tmp_path / "composite.mp4"
    reference = tmp_path / "reference.mp4"
    source = tmp_path / "source.mp4"
    composite.write_bytes(b"finished-video")
    reference.write_bytes(b"reference-video")
    source.write_bytes(b"source-video")

    def child_result(command, **_kwargs):
        result_path = Path(command[command.index("--result") + 1])
        if failure_mode == "invalid":
            result_path.write_text("{", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1 if failure_mode == "nonzero" else 0)

    with (
        mock.patch.object(render.time, "monotonic", side_effect=clock_values),
        mock.patch.object(render.subprocess, "run", side_effect=child_result),
    ):
        receipt = render._run_composition_check(
            composite=composite,
            reference=reference,
            source=source,
            time_map=[
                {
                    "source_start_s": 0.0,
                    "source_end_s": 1.0,
                    "output_start_s": 0.0,
                    "output_end_s": 1.0,
                }
            ],
            scratch=tmp_path / "scratch",
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_text=False,
            scope="full-timeline",
            frame_size={"width": 1080, "height": 1920},
        )

    assert receipt["status"] == "unmeasured"
    assert receipt["elapsed_s"] == expected_elapsed
    assert receipt["findings"] == [{"kind": "analysis_incomplete"}]


def test_composite_child_success_receipt_elapsed_is_untouched(tmp_path: Path) -> None:
    composite = tmp_path / "composite.mp4"
    reference = tmp_path / "reference.mp4"
    source = tmp_path / "source.mp4"
    composite.write_bytes(b"finished-video")
    reference.write_bytes(b"reference-video")
    source.write_bytes(b"source-video")
    expected = {"status": "unmeasured", "elapsed_s": 7.75, "findings": []}

    def child_result(command, **_kwargs):
        result_path = Path(command[command.index("--result") + 1])
        result_path.write_text(
            json.dumps({"ok": True, "composition_check": expected}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    with (
        mock.patch.object(render.time, "monotonic", return_value=100.0),
        mock.patch.object(render.subprocess, "run", side_effect=child_result),
    ):
        receipt = render._run_composition_check(
            composite=composite,
            reference=reference,
            source=source,
            time_map=[],
            scratch=tmp_path / "scratch",
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_text=False,
            scope="full-timeline",
            frame_size={"width": 1080, "height": 1920},
        )

    assert receipt == expected


def test_timeline_reference_uses_same_shadow_and_preserves_full_layout(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "source/source.mp4"
    cache = project / "working/fx-preview/renderer-cache"
    output = project / "working/fx-preview/runtime/out.mp4"
    source.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = {
        "manifest": {"source": {"duration_s": 6.0}},
        "state": {
            "canvas": {"width": 1080, "height": 1920, "fit": "contain"},
            "segments": [
                {"source_start": 0.0, "source_end": 2.3454},
                {"source_start": 4.0, "source_end": 6.0},
            ],
            "asset_digests": {},
            "overlays": [
                {
                    "id": "fx-preview-candidate",
                    "type": "fx",
                    "code": "T3",
                    "start": 1.0,
                    "end": 5.0,
                    "visible": True,
                }
            ],
        },
    }
    original_snapshot = copy.deepcopy(snapshot)
    dependencies = {
        "source": {"relative": "source/source.mp4", "sha256": source_sha},
        "timeline_project_inputs": {},
    }
    calls = []
    captured = {}

    def fake_render(shadow, destination, _quality, *, snapshot_path, **_kwargs):
        shadow = Path(shadow)
        frozen = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        calls.append(frozen)
        Path(destination).write_bytes(
            b"full-composite" if len(calls) == 1 else b"source-reference"
        )
        layout = shadow / "working/fx_text_layout.json"
        if len(calls) == 1:
            layout.write_text(
                json.dumps(
                    {
                        "fx-preview-candidate": {
                            "version": 2,
                            "measurement": "composition-dom",
                            "status": "pass",
                            "analysis_scope": "single-fx-and-source-frame",
                            "face_measurement": "unavailable",
                            "samples": [],
                            "boxes": [],
                            "findings": [],
                            "subject_samples": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
        else:
            layout.write_text("{}", encoding="utf-8")

    def fake_check(**kwargs):
        captured.update(
            {
                **kwargs,
                "composite_bytes": kwargs["composite"].read_bytes(),
                "reference_bytes": kwargs["reference"].read_bytes(),
                "source_bytes": kwargs["source"].read_bytes(),
            }
        )
        return {
            "version": 1,
            "scope": "full-timeline",
            "measurement": "sampled-local-frames",
            "status": "unmeasured",
            "source_sha256": source_sha,
            "media_sha256": hashlib.sha256(b"full-composite").hexdigest(),
            "frame_size": {"width": 540, "height": 960},
            "sample_count": 0,
            "measured_count": 0,
            "elapsed_s": 0.1,
            "engines": {"faces": "unavailable", "ocr": "unavailable"},
            "samples": [],
            "findings": [{"kind": "analysis_incomplete"}],
        }

    with (
        mock.patch.object(timeline, "render_project", side_effect=fake_render),
        mock.patch.object(timeline, "referenced_asset_digests", return_value={}),
        mock.patch.object(timeline, "editor_state_revision", return_value="c" * 64),
        mock.patch.object(render, "media_ok", side_effect=_validated_media_at(540, 960)),
        mock.patch.object(render, "_run_composition_check", side_effect=fake_check),
    ):
        result = render.timeline_preview(
            project,
            cache,
            output,
            snapshot,
            1.0,
            4.0,
            dependencies,
            scratch_owner="d" * 32,
        )

    assert snapshot == original_snapshot
    assert len(calls) == 2
    assert calls[0]["state"]["overlays"]
    assert calls[1]["state"]["overlays"] == []
    assert captured["composite_bytes"] == b"full-composite"
    assert captured["reference_bytes"] == b"source-reference"
    assert captured["source_bytes"] == b"source"
    assert captured["frame_size"] == {"width": 540, "height": 960}
    assert result["time_map"] == [
        {
            "source_start_s": 1.0,
            "source_end_s": 2.3454,
            "output_start_s": 0.0,
            "output_end_s": 1.3454,
        },
        {
            "source_start_s": 4.0,
            "source_end_s": 5.0,
            "output_start_s": 1.3454,
            "output_end_s": 2.3454,
        },
    ]
    assert result["render_duration_s"] == 2.3454
    assert jobs.FxPreviewJobManager._normalized_time_map(
        result["time_map"],
        {"start_s": 1.0, "duration_s": 4.0},
        result["render_duration_s"],
        mode="timeline",
    ) == result["time_map"]
    assert "fx-preview-candidate" in result["text_layout"]["layers"]
    assert result["composition_check"]["status"] == "unmeasured"
    assert output.read_bytes() == b"full-composite"
    assert not any(cache.glob("timeline-project-*"))


def test_invalid_canvas_after_shadow_creation_still_cleans_owned_scratch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "source/source.mp4"
    cache = project / "working/fx-preview/renderer-cache"
    output = project / "working/fx-preview/runtime/out.mp4"
    source.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    source.write_bytes(b"ordinary-source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = {
        "manifest": {"source": {"duration_s": 2.0}},
        "state": {
            "canvas": {"width": None, "height": 1920, "fit": "contain"},
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "asset_digests": {},
            "overlays": [],
        },
    }
    dependencies = {
        "source": {"relative": "source/source.mp4", "sha256": source_sha},
        "timeline_project_inputs": {},
    }

    with pytest.raises(render.PreviewError, match="完整時間軸預覽無法渲染") as error:
        render.timeline_preview(
            project,
            cache,
            output,
            snapshot,
            0.0,
            1.0,
            dependencies,
            scratch_owner="e" * 32,
        )

    assert error.value.code == "timeline_render_failed"
    assert not any(cache.glob("timeline-project-*"))
    assert not any(cache.glob(".timeline-owner-*.lock"))


def test_reference_system_exit_is_advisory_and_preserves_valid_full_video(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "source/source.mp4"
    cache = project / "working/fx-preview/renderer-cache"
    output = project / "working/fx-preview/runtime/out.mp4"
    source.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = {
        "manifest": {"source": {"duration_s": 2.0}},
        "state": {
            "canvas": {"width": 1080, "height": 1920, "fit": "contain"},
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "asset_digests": {},
            "overlays": [],
        },
    }
    dependencies = {
        "source": {"relative": "source/source.mp4", "sha256": source_sha},
        "timeline_project_inputs": {},
    }
    calls = 0

    def render_then_exit(_shadow, destination, _quality, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(destination).write_bytes(b"valid-full-video")
            return
        raise SystemExit("optional reference renderer unavailable")

    with (
        mock.patch.object(timeline, "render_project", side_effect=render_then_exit),
        mock.patch.object(timeline, "referenced_asset_digests", return_value={}),
        mock.patch.object(timeline, "editor_state_revision", return_value="c" * 64),
        mock.patch.object(render, "media_ok", side_effect=_validated_media_at(540, 960)),
    ):
        result = render.timeline_preview(
            project,
            cache,
            output,
            snapshot,
            0.0,
            1.0,
            dependencies,
            scratch_owner="f" * 32,
        )

    assert calls == 2
    assert output.read_bytes() == b"valid-full-video"
    assert result["composition_check"]["status"] == "unmeasured"
    assert result["composition_check"]["elapsed_s"] == 0.0
    assert result["composition_check"]["frame_size"] == {
        "width": 540,
        "height": 960,
    }
    assert result["composition_check"]["findings"] == [
        {"kind": "reference_unavailable"}
    ]
    sanitized = jobs._sanitize_composition_check(
        result["composition_check"],
        scope="full-timeline",
        source_sha256=source_sha,
        media_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        frame_size={"width": 540, "height": 960},
        render_duration_s=1.0,
        time_map=result["time_map"],
    )
    assert sanitized["findings"] == [{"kind": "reference_unavailable"}]
    assert not any(cache.glob("timeline-project-*"))
    assert not any(cache.glob(".timeline-owner-*.lock"))


def test_unknown_encoded_frame_size_keeps_valid_video_without_guessing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "source/source.mp4"
    cache = project / "working/fx-preview/renderer-cache"
    output = project / "working/fx-preview/runtime/out.mp4"
    source.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = {
        "manifest": {"source": {"duration_s": 2.0}},
        "state": {
            "canvas": {"width": 1080, "height": 1920, "fit": "contain"},
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "asset_digests": {},
            "overlays": [],
        },
    }
    dependencies = {
        "source": {"relative": "source/source.mp4", "sha256": source_sha},
        "timeline_project_inputs": {},
    }
    calls = 0

    def render_once(_shadow, destination, _quality, **_kwargs):
        nonlocal calls
        calls += 1
        Path(destination).write_bytes(b"valid-full-video")

    with (
        mock.patch.object(timeline, "render_project", side_effect=render_once),
        mock.patch.object(render, "media_ok", return_value=True),
    ):
        result = render.timeline_preview(
            project,
            cache,
            output,
            snapshot,
            0.0,
            1.0,
            dependencies,
            scratch_owner="a" * 32,
        )

    assert calls == 1
    assert output.read_bytes() == b"valid-full-video"
    assert result["composition_check"] is None
    assert not any(cache.glob("timeline-project-*"))


def test_wrong_frame_binding_still_loses_advisory_reason() -> None:
    source_sha = "a" * 64
    media_sha = "b" * 64
    receipt = render._composition_unmeasured(
        scope="full-timeline",
        source_sha256=source_sha,
        media_sha256=media_sha,
        frame_size={"width": 1080, "height": 1920},
        finding="reference_unavailable",
    )

    sanitized = jobs._sanitize_composition_check(
        receipt,
        scope="full-timeline",
        source_sha256=source_sha,
        media_sha256=media_sha,
        frame_size={"width": 540, "height": 960},
        render_duration_s=1.0,
        time_map=[
            {
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            }
        ],
    )

    assert sanitized["frame_size"] == {"width": 540, "height": 960}
    assert sanitized["findings"] == [{"kind": "analysis_incomplete"}]


def _measured_overlap_receipt(status: str, scope: str) -> dict:
    source_sha = "a" * 64
    media_sha = "b" * 64
    return {
        "version": 1,
        "scope": scope,
        "measurement": "sampled-local-frames",
        "status": status,
        "source_sha256": source_sha,
        "media_sha256": media_sha,
        "frame_size": {"width": 1080, "height": 1920},
        "sample_count": 1,
        "measured_count": 1,
        "elapsed_s": 1.0,
        "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
        "samples": [
            {
                "output_at": 0.5,
                "source_at": 1.5,
                "measured": True,
                "source_face_count": 1,
                "reference_face_count": 1,
                "visible_face_count": 1,
                "text_box_count": 1,
            }
        ],
        "findings": [
            {
                "kind": "text_subject_overlap",
                "output_at": 0.5,
                "source_at": 1.5,
                "subject_box": {"x": 10, "y": 20, "width": 100, "height": 100},
                "text_box": {"x": 20, "y": 30, "width": 80, "height": 20},
            }
        ],
    }


def _sanitize_bound_receipt(receipt: dict, scope: str) -> dict:
    sanitized = jobs._sanitize_composition_check(
        receipt,
        scope=scope,
        source_sha256="a" * 64,
        media_sha256="b" * 64,
        frame_size={"width": 1080, "height": 1920},
        render_duration_s=1.0,
        time_map=[
            {
                "source_start_s": 1.0,
                "source_end_s": 2.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            }
        ],
    )
    return sanitized


@pytest.mark.parametrize("status", ("pass", "warn", "unmeasured"))
def test_single_effect_receipt_is_always_canonical_unavailable(
    status: str,
) -> None:
    sanitized = _sanitize_bound_receipt(
        _measured_overlap_receipt(status, "single-effect"),
        "single-effect",
    )

    assert sanitized == {
        "version": 1,
        "scope": "single-effect",
        "measurement": "sampled-local-frames",
        "status": "unmeasured",
        "source_sha256": "a" * 64,
        "media_sha256": "b" * 64,
        "frame_size": {"width": 1080, "height": 1920},
        "sample_count": 0,
        "measured_count": 0,
        "elapsed_s": 1.0,
        "engines": {"faces": "unavailable", "ocr": "unavailable"},
        "samples": [],
        "findings": [{"kind": "full_timeline_required"}],
    }


def test_legitimate_single_effect_fallback_is_unchanged() -> None:
    receipt = render._composition_unmeasured(
        scope="single-effect",
        source_sha256="a" * 64,
        media_sha256="b" * 64,
        frame_size={"width": 1080, "height": 1920},
        finding="full_timeline_required",
    )
    assert _sanitize_bound_receipt(receipt, "single-effect") == receipt


def test_malformed_single_effect_binding_cannot_claim_trusted_pass() -> None:
    receipt = _measured_overlap_receipt("pass", "single-effect")
    receipt.update(
        source_sha256="c" * 64,
        media_sha256="d" * 64,
        frame_size={"width": 1, "height": 1},
    )
    sanitized = _sanitize_bound_receipt(receipt, "single-effect")

    assert sanitized["status"] == "unmeasured"
    assert sanitized["source_sha256"] == "a" * 64
    assert sanitized["media_sha256"] == "b" * 64
    assert sanitized["frame_size"] == {"width": 1080, "height": 1920}
    assert sanitized["sample_count"] == 0
    assert sanitized["measured_count"] == 0
    assert sanitized["samples"] == []
    assert sanitized["findings"] == [{"kind": "full_timeline_required"}]


def test_full_timeline_overlap_warn_remains_measured_and_timed() -> None:
    sanitized = _sanitize_bound_receipt(
        _measured_overlap_receipt("warn", "full-timeline"),
        "full-timeline",
    )

    assert sanitized["status"] == "warn"
    assert sanitized["scope"] == "full-timeline"
    assert sanitized["measured_count"] == 1
    assert sanitized["samples"][0]["output_at"] == 0.5
    assert sanitized["samples"][0]["source_at"] == 1.5
    assert sanitized["findings"][0]["kind"] == "text_subject_overlap"
    assert sanitized["findings"][0]["output_at"] == 0.5
    assert sanitized["findings"][0]["source_at"] == 1.5


def test_analysis_elapsed_is_zero_before_analysis_and_capped_without_sleep() -> None:
    assert render._bounded_analysis_elapsed(None) == 0.0
    with mock.patch.object(render.time, "monotonic", return_value=200.5):
        assert render._bounded_analysis_elapsed(100.0) == 60.0

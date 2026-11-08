"""API/cache contracts for optional complete-composite diagnostics."""

from __future__ import annotations

import json

import pytest

import test_fx_preview_api as _api_fixture


jobs = _api_fixture.fx_preview_jobs


def _bound_receipt() -> tuple[dict, dict]:
    source_sha = "a" * 64
    media_sha = "b" * 64
    time_map = [
        {
            "source_start_s": 1.0,
            "source_end_s": 2.0,
            "output_start_s": 0.0,
            "output_end_s": 1.0,
        },
        {
            "source_start_s": 4.0,
            "source_end_s": 5.0,
            "output_start_s": 1.0,
            "output_end_s": 2.0,
        },
    ]
    receipt = {
        "version": 1,
        "scope": "full-timeline",
        "measurement": "sampled-local-frames",
        "status": "pass",
        "source_sha256": source_sha,
        "media_sha256": media_sha,
        "frame_size": {"width": 1080, "height": 1920},
        "sample_count": 2,
        "measured_count": 2,
        "elapsed_s": 1.25,
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
            },
            {
                "output_at": 1.5,
                "source_at": 4.5,
                "measured": True,
                "source_face_count": 1,
                "reference_face_count": 1,
                "visible_face_count": 1,
                "text_box_count": 0,
            },
        ],
        "findings": [],
    }
    binding = {
        "scope": "full-timeline",
        "source_sha256": source_sha,
        "media_sha256": media_sha,
        "frame_size": {"width": 1080, "height": 1920},
        "render_duration_s": 2.0,
        "time_map": time_map,
    }
    return receipt, binding


def test_valid_pass_is_bound_across_deleted_source_gap() -> None:
    receipt, binding = _bound_receipt()
    clean = jobs._sanitize_composition_check(receipt, **binding)
    assert clean["status"] == "pass"
    assert clean["samples"][1]["source_at"] == 4.5


def test_impossible_measured_sample_downgrades_without_losing_time_binding() -> None:
    receipt, binding = _bound_receipt()
    receipt["samples"][0].update(
        source_face_count=0,
        reference_face_count=0,
        visible_face_count=0,
        text_box_count=0,
    )

    clean = jobs._sanitize_composition_check(receipt, **binding)

    assert clean["status"] == "unmeasured"
    assert clean["measured_count"] == 1
    assert clean["samples"][0] == {
        "output_at": 0.5,
        "source_at": 1.5,
        "measured": False,
        "source_face_count": 0,
        "reference_face_count": 0,
        "visible_face_count": 0,
        "text_box_count": 0,
    }
    assert clean["findings"] == [{"kind": "analysis_incomplete"}]


def test_incomplete_warn_downgrades_while_preserving_legitimate_finding() -> None:
    receipt, binding = _bound_receipt()
    receipt["status"] = "warn"
    receipt["samples"][0]["measured"] = False
    receipt["measured_count"] = 1
    receipt["findings"] = [
        {"kind": "analysis_incomplete", "output_at": 0.5, "source_at": 1.5}
    ]

    clean = jobs._sanitize_composition_check(receipt, **binding)

    assert clean["status"] == "unmeasured"
    assert clean["measured_count"] == 1
    assert clean["samples"][0]["measured"] is False
    assert clean["findings"] == receipt["findings"]


def test_complete_text_overlap_warn_remains_warn() -> None:
    receipt, binding = _bound_receipt()
    receipt["status"] = "warn"
    receipt["findings"] = [
        {
            "kind": "text_subject_overlap",
            "output_at": 0.5,
            "source_at": 1.5,
            "subject_box": {"x": 10, "y": 20, "width": 100, "height": 100},
            "text_box": {"x": 20, "y": 30, "width": 80, "height": 20},
        }
    ]

    clean = jobs._sanitize_composition_check(receipt, **binding)

    assert clean["status"] == "warn"
    assert clean["findings"] == receipt["findings"]


@pytest.mark.parametrize("first_output_start,second_output_start", ((0.2, 1.2), (0.0, 1.2)))
def test_normalized_time_map_rejects_output_gaps_but_allows_source_gaps(
    first_output_start: float, second_output_start: float
) -> None:
    with pytest.raises(ValueError, match="out of range"):
        jobs.FxPreviewJobManager._normalized_time_map(
            [
                {
                    "source_start_s": 0.0,
                    "source_end_s": 1.0,
                    "output_start_s": first_output_start,
                    "output_end_s": first_output_start + 1.0,
                },
                {
                    "source_start_s": 2.0,
                    "source_end_s": 3.0,
                    "output_start_s": second_output_start,
                    "output_end_s": 2.2,
                },
            ],
            {"start_s": 0.0, "duration_s": 3.0},
            2.2,
            mode="timeline",
        )

    assert jobs.FxPreviewJobManager._normalized_time_map(
        [
            {
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            },
            {
                "source_start_s": 2.0,
                "source_end_s": 3.0,
                "output_start_s": 1.0,
                "output_end_s": 2.0,
            },
        ],
        {"start_s": 0.0, "duration_s": 3.0},
        2.0,
        mode="timeline",
    )[1]["source_start_s"] == 2.0


@pytest.mark.parametrize(
    "mutation",
    (
        lambda receipt: receipt.update(media_sha256="c" * 64),
        lambda receipt: receipt.update(source_sha256="c" * 64),
        lambda receipt: receipt.update(status=[]),
        lambda receipt: receipt.update(engines={"faces": [], "ocr": "macos-vision"}),
        lambda receipt: receipt.update(engines={"faces": "opencv-yunet", "ocr": {}}),
        lambda receipt: receipt.update(findings=[{"kind": {}}]),
        lambda receipt: receipt["samples"][1].update(source_at=3.5),
        lambda receipt: receipt.update(extra="not-public"),
        lambda receipt: receipt.update(sample_count=9),
        lambda receipt: receipt.update(
            findings=[
                {
                    "kind": "text_subject_overlap",
                    "output_at": 0.5,
                    "source_at": 1.5,
                    "subject_box": {"x": 1000, "y": 10, "width": 100, "height": 20},
                    "text_box": {"x": 10, "y": 10, "width": 20, "height": 20},
                }
            ]
        ),
    ),
)
def test_invalid_or_unbound_receipt_downgrades_without_forging_pass(mutation) -> None:
    receipt, binding = _bound_receipt()
    mutation(receipt)
    clean = jobs._sanitize_composition_check(receipt, **binding)
    assert clean["status"] == "unmeasured"
    assert clean["findings"] == [{"kind": "analysis_incomplete"}]
    assert "extra" not in clean


@pytest.fixture
def api() -> _api_fixture.FxPreviewApiTests:
    case = _api_fixture.FxPreviewApiTests(
        methodName="test_preview_is_non_mutating_then_confirm_uses_cas"
    )
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def test_missing_worker_diagnostic_is_public_unmeasured_and_cache_safe(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    request = api._candidate("T3")
    status, created = api._json_request("POST", "/api/fx-previews", request)
    assert status == 202, created
    ready = api._wait_terminal(created["job"]["id"])
    check = ready["job"]["composition_check"]
    assert check["scope"] == "single-effect"
    assert check["status"] == "unmeasured"
    assert check["findings"] == [{"kind": "full_timeline_required"}]
    assert check["source_sha256"] == api.server.fx_preview_jobs._jobs[
        created["job"]["id"]
    ]["source_identity"]["sha256"]

    with api.server.fx_preview_jobs._lock:
        record = api.server.fx_preview_jobs._jobs[created["job"]["id"]]
        receipt_path = api.server.fx_preview_jobs.cache_dir / f"{record['cache_key']}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["composition_check"] = {
        **check,
        "status": "pass",
        "media_sha256": "f" * 64,
        "sample_count": 1,
        "measured_count": 1,
        "engines": {"faces": "opencv-yunet", "ocr": "macos-vision"},
        "samples": [
            {
                "output_at": 1.0,
                "source_at": 2.0,
                "measured": True,
                "source_face_count": 1,
                "reference_face_count": 1,
                "visible_face_count": 1,
                "text_box_count": 0,
            }
        ],
        "findings": [],
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    status, cached = api._json_request("POST", "/api/fx-previews", request)
    assert status == 200, cached
    assert cached["job"]["cache_hit"] is True
    assert cached["job"]["composition_check"]["status"] == "unmeasured"
    assert cached["job"]["composition_check"]["media_sha256"] != "f" * 64


def test_cache_uses_canonical_render_duration_and_rejects_byte_forgery(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    api.server.fx_preview_jobs.artifact_validator = lambda _path, duration: {
        "duration_s": round(duration - 0.01, 3),
        "width": 1080,
        "height": 1920,
    }
    request = api._candidate("T3")
    status, created = api._json_request("POST", "/api/fx-previews", request)
    assert status == 202, created
    ready = api._wait_terminal(created["job"]["id"])
    assert ready["job"]["state"] == "ready"
    with api.server.fx_preview_jobs._lock:
        record = api.server.fx_preview_jobs._jobs[created["job"]["id"]]
        receipt_path = (
            api.server.fx_preview_jobs.cache_dir / f"{record['cache_key']}.json"
        )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["duration_s"] == receipt["time_map"][-1]["output_end_s"]

    status, cached = api._json_request("POST", "/api/fx-previews", request)
    assert status == 200, cached
    assert cached["job"]["cache_hit"] is True

    receipt["bytes"] += 1
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    status, rerendered = api._json_request("POST", "/api/fx-previews", request)
    assert status == 202, rerendered
    terminal = api._wait_terminal(rerendered["job"]["id"])
    assert terminal["job"]["state"] == "ready"
    assert terminal["job"]["cache_hit"] is False


def test_composition_dependency_identity_includes_optional_code_and_native_versions(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    context = api.server.fx_preview_context()
    normalized = api.server.fx_preview_jobs._normalize_candidate(api._candidate("T3"), context)
    dependencies = api.server.fx_preview_jobs._dependency_snapshot(
        context,
        normalized[0],
        normalized[2],
        preview_options=normalized[4],
        frozen_state=normalized[5],
        frozen_manifest=normalized[6],
    )
    identity = dependencies["composition_analysis"]
    assert set(identity["files"]) == {
        "composite_check.py",
        "composite_check_worker.py",
        "vision_ocr.py",
    }
    assert identity["files"]["composite_check_worker.py"]
    assert identity["files"]["vision_ocr.py"]
    assert set(identity["native"]) == {
        "python", "macos", "opencv", "pyobjc_core", "pyobjc_vision"
    }

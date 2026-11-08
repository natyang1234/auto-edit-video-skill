from __future__ import annotations

import sys
import math
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fx_preview_contract as contract  # noqa: E402
import fx_overlay  # noqa: E402
import style_presets  # noqa: E402


def manual_path(*, start: float = 4.0, end: float = 6.0) -> dict:
    return {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": start, "x": 0.25, "y": 0.3},
            {"source_at": end, "x": 0.75, "y": 0.4},
        ],
    }


def test_temporal_spotlight_path_is_canonicalized_on_source_time_axis() -> None:
    path = {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 4.0004, "x": -0.0, "y": 0.3200004},
            {"source_at": 6.0004, "x": 0.6500004, "y": 0.35},
        ],
    }
    assert contract.normalize_spotlight_path(path, start=4.0, end=6.0) == {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 4.0, "x": 0.0, "y": 0.32},
            {"source_at": 6.0, "x": 0.65, "y": 0.35},
        ],
    }


def test_apply_and_sample_negative_zero_share_the_canonical_zero_identity() -> None:
    sample, apply = contract.normalize_sample_window(
        {"start_s": -0.0, "duration_s": 1.0},
        {"start_s": -0.0, "duration_s": 2.0},
        8.0,
    )
    assert sample["start_s"] == 0.0
    assert apply["start_s"] == 0.0
    assert str(sample["start_s"]) == "0.0"
    assert str(apply["start_s"]) == "0.0"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "future": True},
        lambda value: {**value, "version": True},
        lambda value: {**value, "version": 2},
        lambda value: {**value, "interpolation": "linear"},
        lambda value: {**value, "keyframes": value["keyframes"][:1]},
        lambda value: {
            **value,
            "keyframes": value["keyframes"]
            + [{"source_at": 5.0, "x": 0.5, "y": 0.5}] * 63,
        },
        lambda value: {
            **value,
            "keyframes": [{**value["keyframes"][0], "extra": 1}, value["keyframes"][1]],
        },
    ],
)
def test_spotlight_path_rejects_wrong_schema_and_keyframe_count(mutation) -> None:
    with pytest.raises(contract.FxPreviewContractError) as raised:
        contract.normalize_spotlight_path(mutation(manual_path()), start=4.0, end=6.0)
    assert raised.value.code == "spotlight_path_invalid"


@pytest.mark.parametrize("bad", [True, "4", math.nan, math.inf, -(10**1000)])
@pytest.mark.parametrize("field", ["source_at", "x", "y"])
def test_spotlight_path_rejects_coercion_nonfinite_and_huge_numbers(
    field: str, bad: object
) -> None:
    value = manual_path()
    value["keyframes"][0][field] = bad
    with pytest.raises(contract.FxPreviewContractError):
        contract.normalize_spotlight_path(value, start=4.0, end=6.0)


@pytest.mark.parametrize(
    "keyframes",
    [
        [
            {"source_at": 4.0, "x": 0.2, "y": 0.2},
            {"source_at": 4.0004, "x": 0.3, "y": 0.3},
            {"source_at": 6.0, "x": 0.4, "y": 0.4},
        ],
        [
            {"source_at": 4.0, "x": 0.2, "y": 0.2},
            {"source_at": 5.5, "x": 0.3, "y": 0.3},
            {"source_at": 5.0, "x": 0.4, "y": 0.4},
            {"source_at": 6.0, "x": 0.5, "y": 0.5},
        ],
    ],
)
def test_spotlight_path_requires_strict_order_after_rounding(keyframes: list[dict]) -> None:
    value = manual_path()
    value["keyframes"] = keyframes
    with pytest.raises(contract.FxPreviewContractError, match="嚴格遞增"):
        contract.normalize_spotlight_path(value, start=4.0, end=6.0)


@pytest.mark.parametrize(
    "value,start,end",
    [
        (manual_path(start=4.001), 4.0, 6.0),
        (manual_path(end=5.999), 4.0, 6.0),
        (
            {
                **manual_path(),
                "keyframes": [
                    {"source_at": 4.0, "x": -0.01, "y": 0.2},
                    {"source_at": 6.0, "x": 0.5, "y": 0.5},
                ],
            },
            4.0,
            6.0,
        ),
    ],
)
def test_spotlight_path_requires_exact_endpoints_and_bounded_coordinates(
    value: dict, start: float, end: float
) -> None:
    with pytest.raises(contract.FxPreviewContractError):
        contract.normalize_spotlight_path(value, start=start, end=end)


def test_spotlight_path_changes_visual_and_receipt_identity() -> None:
    first = manual_path()
    second = manual_path()
    second["keyframes"][1]["x"] = 0.8
    overlay = {"code": "P5", "start": 4.0, "end": 6.0, "spotlight_path": first}
    assert contract.visual_overlay_projection(overlay)["spotlight_path"] == first
    assert contract.spotlight_path_sha256(first, start=4.0, end=6.0) != (
        contract.spotlight_path_sha256(second, start=4.0, end=6.0)
    )


def test_canonical_path_hash_fixed_vector_includes_normalized_float_shapes() -> None:
    value = {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 4.0004, "x": -0.0, "y": 0.3200004},
            {"source_at": 5.12349, "x": 0.33333349, "y": 0.66666651},
            {"source_at": 6.0004, "x": 1.0, "y": 0.35},
        ],
    }
    assert contract.spotlight_path_sha256(value, start=4.0, end=6.0) == (
        "9019ef34f07c79398e515a24330b3d182a4672e7914e5bf1c2c0fe2554ccf476"
    )


def test_renderer_request_revalidates_and_canonicalizes_the_same_path(
    tmp_path: Path,
) -> None:
    path = manual_path()
    path["keyframes"][0].update({"source_at": 4.0004, "x": -0.0})
    payload = {
        "overlay": {
            "type": "fx",
            "code": "P5",
            "start": 4.0,
            "end": 6.0,
            "style": {"opacity": 1},
            "subject": None,
            "spotlight_path": path,
        },
        "sample_window": {"start_s": 4.5, "duration_s": 1.0},
        "preview_options": {"mode": "single", "quality": "standard"},
        "state_snapshot": {},
        "manifest_snapshot": {"source": {"duration_s": 8.0}},
    }
    checked = contract.validate_renderer_request(tmp_path, payload)
    assert checked["overlay"]["spotlight_path"]["keyframes"][0] == {
        "source_at": 4.0,
        "x": 0.0,
        "y": 0.3,
    }

    payload["overlay"]["spotlight_path"]["keyframes"][-1]["source_at"] = 5.9
    with pytest.raises(contract.FxPreviewContractError) as raised:
        contract.validate_renderer_request(tmp_path, payload)
    assert raised.value.code == "invalid_request"


def test_manual_subject_track_summary_is_exact_and_does_not_leak_points() -> None:
    path = manual_path()
    overlay = {"code": "P5", "start": 4.0, "end": 6.0, "spotlight_path": path}
    digest = contract.spotlight_path_sha256(path, start=4.0, end=6.0)
    summary = contract.sanitize_subject_track_summary(
        {
            "version": "spotlight-track-v3",
            "mode": "manual",
            "status": "manual",
            "interpolation": "smoothstep",
            "keyframe_count": 2,
            "path_sha256": digest,
            "points": [{"t": 0, "x": 10, "y": 20}],
            "mask_path": "/private/mask.mp4",
        },
        overlay=overlay,
    )
    assert summary == {
        "version": 1,
        "mode": "manual",
        "status": "manual",
        "interpolation": "smoothstep",
        "keyframe_count": 2,
        "path_sha256": digest,
    }


@pytest.mark.parametrize(
    "field,bad,expected_code",
    [
        ("version", 3, "subject_track_invalid"),
        ("status", "tracked", "subject_track_mismatch"),
        ("keyframe_count", 2.0, "subject_track_mismatch"),
        ("keyframe_count", 3, "subject_track_mismatch"),
        ("path_sha256", "0" * 64, "subject_track_mismatch"),
    ],
)
def test_manual_subject_track_summary_rejects_mismatched_renderer_receipt(
    field: str, bad: object, expected_code: str
) -> None:
    path = manual_path()
    overlay = {"code": "P5", "start": 4.0, "end": 6.0, "spotlight_path": path}
    receipt = {
        "version": "spotlight-track-v3",
        "mode": "manual",
        "status": "manual",
        "interpolation": "smoothstep",
        "keyframe_count": 2,
        "path_sha256": contract.spotlight_path_sha256(path, start=4.0, end=6.0),
    }
    receipt[field] = bad
    with pytest.raises(contract.FxPreviewContractError) as raised:
        contract.sanitize_subject_track_summary(receipt, overlay=overlay)
    assert raised.value.code == expected_code


def test_public_manual_subject_track_summary_validates_exact_cache_shape() -> None:
    path = manual_path()
    overlay = {"code": "P5", "start": 4.0, "end": 6.0, "spotlight_path": path}
    summary = {
        "version": 1,
        "mode": "manual",
        "status": "manual",
        "interpolation": "smoothstep",
        "keyframe_count": 2,
        "path_sha256": contract.spotlight_path_sha256(path, start=4.0, end=6.0),
    }
    assert contract.validate_public_subject_track_summary(
        summary, overlay=overlay
    ) == summary


@pytest.mark.parametrize(
    "field,bad",
    [
        ("version", True),
        ("version", "1"),
        ("mode", "automatic"),
        ("status", "tracked"),
        ("interpolation", "automatic"),
        ("keyframe_count", 2.0),
        ("keyframe_count", 3),
        ("path_sha256", "0" * 64),
    ],
)
def test_public_manual_subject_track_summary_rejects_tampering(
    field: str, bad: object
) -> None:
    path = manual_path()
    overlay = {"code": "P5", "start": 4.0, "end": 6.0, "spotlight_path": path}
    summary = {
        "version": 1,
        "mode": "manual",
        "status": "manual",
        "interpolation": "smoothstep",
        "keyframe_count": 2,
        "path_sha256": contract.spotlight_path_sha256(path, start=4.0, end=6.0),
    }
    summary[field] = bad
    with pytest.raises(contract.FxPreviewContractError):
        contract.validate_public_subject_track_summary(summary, overlay=overlay)


def test_public_automatic_subject_track_summary_is_bounded_and_p5_only() -> None:
    summary = {
        "version": 1,
        "mode": "automatic",
        "status": "tracked",
        "interpolation": "automatic",
        "keyframe_count": 2,
        "path_sha256": None,
    }
    overlay = {"code": "P5", "start": 4.0, "end": 6.0}
    assert contract.validate_public_subject_track_summary(
        summary, overlay=overlay
    ) == summary
    with pytest.raises(contract.FxPreviewContractError):
        contract.validate_public_subject_track_summary(
            {**summary, "mask_path": "/private/mask.mp4"}, overlay=overlay
        )
    with pytest.raises(contract.FxPreviewContractError):
        contract.validate_public_subject_track_summary(
            summary, overlay={**overlay, "code": "T3"}
        )


def test_persisted_fx_validation_canonicalizes_manual_path_and_rejects_other_codes() -> None:
    path = manual_path()
    path["keyframes"][0].update({"source_at": 4.0004, "x": -0.0})
    overlay = {
        "id": "fx-manual",
        "type": "fx",
        "code": "P5",
        "start": 4.0,
        "end": 6.0,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1},
        "spotlight_path": path,
    }
    catalog = {
        "effects": [{"code": "P5", "available": True, "vars": []}],
        "schemes": [],
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ):
        assert fx_overlay.validate(overlay) == []
        assert overlay["spotlight_path"]["keyframes"][0] == {
            "source_at": 4.0,
            "x": 0.0,
            "y": 0.3,
        }
        overlay["code"] = "T3"
        catalog["effects"].append({"code": "T3", "available": True, "vars": []})
        assert any("only P5" in error for error in fx_overlay.validate(overlay))


def test_persisted_manual_path_rejects_noncanonical_overlay_bounds_before_render() -> None:
    path = manual_path(start=1.001, end=3.0)
    overlay = {
        "id": "fx-manual-noncanonical-bounds",
        "type": "fx",
        "code": "P5",
        "start": 1.0006,
        "end": 3.0004,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1},
        "spotlight_path": path,
    }
    catalog = {
        "effects": [{"code": "P5", "available": True, "vars": []}],
        "schemes": [],
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ):
        errors = fx_overlay.validate(overlay)

    assert errors == [
        "fx overlay fx-manual-noncanonical-bounds: 手動 spotlight_path 的 "
        "start/end 必須先正規化為 3 位小數，且與路徑端點完全一致"
    ]
    assert overlay["spotlight_path"] is path


@pytest.mark.parametrize(
    "field,bad",
    [
        ("start", True),
        ("start", "4"),
        ("end", math.nan),
        ("end", math.inf),
        ("start", -(10**1000)),
    ],
)
def test_persisted_manual_path_rejects_untrusted_overlay_bounds(
    field: str, bad: object
) -> None:
    overlay = {
        "id": "fx-manual-invalid-bounds",
        "type": "fx",
        "code": "P5",
        "start": 4.0,
        "end": 6.0,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1},
        "spotlight_path": manual_path(),
    }
    overlay[field] = bad
    catalog = {
        "effects": [{"code": "P5", "available": True, "vars": []}],
        "schemes": [],
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ):
        errors = fx_overlay.validate(overlay)

    assert errors
    assert any("必須是有限數字" in error for error in errors)


def test_persisted_manual_path_rejects_untrusted_path_schema() -> None:
    path = {**manual_path(), "future": True}
    overlay = {
        "id": "fx-manual-invalid-schema",
        "type": "fx",
        "code": "P5",
        "start": 4.0,
        "end": 6.0,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1},
        "spotlight_path": path,
    }
    catalog = {
        "effects": [{"code": "P5", "available": True, "vars": []}],
        "schemes": [],
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ):
        errors = fx_overlay.validate(overlay)

    assert errors == [
        "fx overlay fx-manual-invalid-schema: 手動聚光路徑欄位不符合 API 規格"
    ]


def test_nonmanual_persisted_overlay_keeps_existing_submillisecond_bounds_behavior() -> None:
    overlay = {
        "id": "fx-automatic-submillisecond-bounds",
        "type": "fx",
        "code": "P5",
        "start": 1.0006,
        "end": 3.0004,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1},
    }
    catalog = {
        "effects": [{"code": "P5", "available": True, "vars": []}],
        "schemes": [],
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ):
        assert fx_overlay.validate(overlay) == []


def test_style_preset_whitelist_rejects_project_specific_temporal_path() -> None:
    with pytest.raises(style_presets.StylePresetError):
        style_presets.normalize_fx_settings(
            {
                "code": "P5",
                "values": {},
                "placement": "",
                "scheme": "",
                "font_ids": {},
                "text_fit": True,
                "opacity": 1.0,
                "spotlight_path": manual_path(),
            }
        )


"""P6 manual spotlight-path API, cache, persistence and replacement binding."""

from __future__ import annotations

import json

import pytest

import test_fx_preview_api as _api_fixture
import fx_preview_contract as contract


def manual_path(*, x_end: float = 0.75) -> dict:
    return {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 1.0, "x": 0.25, "y": 0.3},
            {"source_at": 3.0, "x": x_end, "y": 0.4},
        ],
    }


def _manual_renderer() -> str:
    before = 'payload=json.dumps({'
    insert = '''spotlight_path=req["overlay"].get("spotlight_path")
subject_track=None
if spotlight_path is not None:
    path_raw=json.dumps(spotlight_path,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    subject_track={
        "version":"spotlight-track-v3-manual",
        "mode":"manual",
        "status":"manual",
        "interpolation":"smoothstep",
        "keyframe_count":len(spotlight_path["keyframes"]),
        "path_sha256":hashlib.sha256(path_raw).hexdigest(),
        "points":[{"t":0,"x":1,"y":2}],
        "private_mask":"/must/not/leak",
    }
elif req["overlay"].get("code") == "P5":
    subject_track={
        "version":"spotlight-track-v3-automatic",
        "mode":"automatic",
        "status":"tracked",
        "interpolation":"automatic",
        "points":[{"t":0,"x":0.2,"y":0.3},{"t":1,"x":0.4,"y":0.5}],
        "private_mask":"/must/not/leak",
    }
payload=json.dumps({'''
    script = _api_fixture.FAST_RENDERER.replace(before, insert, 1)
    return script.replace(
        '    "text_layout":{',
        '    "subject_track":subject_track,\n    "text_layout":{',
        1,
    )


@pytest.fixture
def api() -> _api_fixture.FxPreviewApiTests:
    case = _api_fixture.FxPreviewApiTests(
        methodName="test_preview_is_non_mutating_then_confirm_uses_cas"
    )
    case.setUp()
    case.fast_renderer.write_text(_manual_renderer(), encoding="utf-8")
    try:
        yield case
    finally:
        case.doCleanups()


def _request(api: _api_fixture.FxPreviewApiTests, path: dict, **extra: object) -> dict:
    request = api._candidate("P5")
    request["candidate"]["spotlight_path"] = path
    request.update(extra)
    return request


def _submit_ready(api: _api_fixture.FxPreviewApiTests, request: dict) -> dict:
    status, created = api._json_request("POST", "/api/fx-previews", request)
    assert status in {200, 202}, created
    terminal = api._wait_terminal(created["job"]["id"])
    assert terminal["job"]["state"] == "ready", terminal
    return terminal


def test_spotlight_keyframe_stylesheet_has_a_same_origin_static_route(api) -> None:
    status, headers, body = api._request("GET", "/spotlight-keyframes.css")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    assert body


@pytest.mark.parametrize(
    "mutate",
    [
        lambda path: {**path, "unknown": 1},
        lambda path: {**path, "version": True},
        lambda path: {**path, "keyframes": path["keyframes"][:1]},
        lambda path: {
            **path,
            "keyframes": [
                path["keyframes"][0],
                {"source_at": 1.0004, "x": 0.5, "y": 0.5},
                path["keyframes"][1],
            ],
        },
        lambda path: {
            **path,
            "keyframes": [
                {"source_at": 1.001, "x": 0.25, "y": 0.3},
                path["keyframes"][1],
            ],
        },
        lambda path: {
            **path,
            "keyframes": [
                {"source_at": 1.0, "x": 10**1000, "y": 0.3},
                path["keyframes"][1],
            ],
        },
    ],
)
def test_manual_path_rejected_at_http_trust_boundary(api, mutate) -> None:
    status, payload = api._json_request(
        "POST", "/api/fx-previews", _request(api, mutate(manual_path()))
    )
    assert status == 422, payload
    assert payload["reason"] == "spotlight_path_invalid"


def test_manual_path_is_canonical_persisted_and_public_receipt_is_bounded(api) -> None:
    path = manual_path()
    path["keyframes"][0].update({"source_at": 1.0004, "x": -0.0})
    terminal = _submit_ready(api, _request(api, path))
    summary = terminal["job"]["subject_track"]
    assert summary == {
        "version": 1,
        "mode": "manual",
        "status": "manual",
        "interpolation": "smoothstep",
        "keyframe_count": 2,
        "path_sha256": contract.spotlight_path_sha256(
            {
                **path,
                "keyframes": [
                    {"source_at": 1.0, "x": 0.0, "y": 0.3},
                    path["keyframes"][1],
                ],
            },
            start=1.0,
            end=3.0,
        ),
    }
    assert "points" not in summary and "private_mask" not in summary
    job_id = terminal["job"]["id"]
    with api.server.fx_preview_jobs._lock:
        frozen = api.server.fx_preview_jobs._jobs[job_id]
        assert frozen["overlay"]["spotlight_path"]["keyframes"][0] == {
            "source_at": 1.0,
            "x": 0.0,
            "y": 0.3,
        }
        assert frozen["state_snapshot"]["overlays"][-1]["spotlight_path"] == (
            frozen["overlay"]["spotlight_path"]
        )

    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{job_id}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    persisted = confirmed["state"]["overlays"][-1]
    assert persisted["spotlight_path"] == frozen["overlay"]["spotlight_path"]
    assert confirmed["revision"] != api.revision


def test_distinct_paths_have_distinct_cache_and_selected_candidate_confirm(api) -> None:
    first = _submit_ready(api, _request(api, manual_path(x_end=0.7)))
    second_request = _request(
        api,
        manual_path(x_end=0.8),
        comparison_anchor_job_id=first["job"]["id"],
    )
    second = _submit_ready(api, second_request)
    with api.server.fx_preview_jobs._lock:
        first_job = api.server.fx_preview_jobs._jobs[first["job"]["id"]]
        second_job = api.server.fx_preview_jobs._jobs[second["job"]["id"]]
        assert first_job["cache_key"] != second_job["cache_key"]
        assert first_job["overlay"]["spotlight_path"] != second_job["overlay"][
            "spotlight_path"
        ]
        assert first_job["comparison_fingerprint_hash"] == second_job[
            "comparison_fingerprint_hash"
        ]

    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{second['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    assert confirmed["state"]["overlays"][-1]["spotlight_path"]["keyframes"][-1][
        "x"
    ] == 0.8
    status, stale = api._json_request(
        "POST",
        f"/api/fx-previews/{first['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 409 and stale["reason"] == "preview_stale"


def test_timeline_snapshot_names_and_selects_only_the_manual_candidate(api) -> None:
    background = _submit_ready(api, api._candidate("T3"))
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{background['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    api.revision = confirmed["revision"]

    manual = _submit_ready(api, _request(api, manual_path()))
    with api.server.fx_preview_jobs._lock:
        job = api.server.fx_preview_jobs._jobs[manual["job"]["id"]]
        snapshot = job["timeline_snapshot"]
        candidate_id = snapshot["candidate_overlay_id"]
        matches = [
            item
            for item in snapshot["state"]["overlays"]
            if item.get("id") == candidate_id
        ]
    assert len(matches) == 1
    assert matches[0]["code"] == "P5"
    assert matches[0]["spotlight_path"] == manual_path()
    assert matches[0]["id"] != confirmed["overlay_id"]


def test_identical_manual_request_reuses_cache_without_rendering(api) -> None:
    request = _request(api, manual_path())
    first = _submit_ready(api, request)
    assert first["job"]["cache_hit"] is False

    status, cached = api._json_request("POST", "/api/fx-previews", request)
    assert status == 200, cached
    assert cached["job"]["state"] == "ready"
    assert cached["job"]["cache_hit"] is True
    assert not (
        {"preparing", "rendering", "validating"}
        & set(cached["job"]["phase_timings_s"])
    )
    assert cached["job"]["subject_track"] == first["job"]["subject_track"]


def test_identical_automatic_request_with_track_metadata_reuses_cache(api) -> None:
    request = api._candidate("P5")
    request["candidate"]["subject"] = {"x": 0.2, "y": 0.3}
    first = _submit_ready(api, request)
    assert first["job"]["cache_hit"] is False
    assert first["job"]["subject_track"] == {
        "version": 1,
        "mode": "automatic",
        "status": "tracked",
        "interpolation": "automatic",
        "keyframe_count": 2,
        "path_sha256": None,
    }

    status, cached = api._json_request("POST", "/api/fx-previews", request)
    assert status == 200, cached
    assert cached["job"]["state"] == "ready"
    assert cached["job"]["cache_hit"] is True
    assert cached["job"]["subject_track"] == first["job"]["subject_track"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("path_sha256", "0" * 64),
        ("status", "tracked"),
        ("version", True),
        ("keyframe_count", 3),
    ],
)
def test_manual_cache_receipt_mismatch_is_not_reused(api, field, bad) -> None:
    request = _request(api, manual_path())
    first = _submit_ready(api, request)
    status, positive = api._json_request("POST", "/api/fx-previews", request)
    assert status == 200, positive
    assert positive["job"]["cache_hit"] is True
    with api.server.fx_preview_jobs._lock:
        job = api.server.fx_preview_jobs._jobs[first["job"]["id"]]
        cache_key = job["cache_key"]
    receipt_path = api.server.fx_preview_jobs.cache_dir / f"{cache_key}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["subject_track"][field] = bad
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    status, created = api._json_request("POST", "/api/fx-previews", request)
    assert status == 202, created
    assert created["job"]["cache_hit"] is False
    assert api._wait_terminal(created["job"]["id"])["job"]["state"] == "ready"


def test_renderer_receipt_for_different_manual_path_fails_closed(api) -> None:
    api.fast_renderer.write_text(
        _manual_renderer().replace(
            '"path_sha256":hashlib.sha256(path_raw).hexdigest()',
            '"path_sha256":"0"*64',
        ),
        encoding="utf-8",
    )
    status, created = api._json_request(
        "POST", "/api/fx-previews", _request(api, manual_path())
    )
    assert status == 202, created
    terminal = api._wait_terminal(created["job"]["id"])
    assert terminal["job"]["state"] == "failed", terminal
    assert terminal["job"]["error"]["code"] == "worker_contract_invalid"
    with api.server.fx_preview_jobs._lock:
        cache_key = api.server.fx_preview_jobs._jobs[created["job"]["id"]][
            "cache_key"
        ]
    assert not (api.server.fx_preview_jobs.cache_dir / f"{cache_key}.json").exists()


@pytest.mark.parametrize("replacement_code", ["P5", "T3"])
def test_replacement_without_path_clears_inherited_manual_path(api, replacement_code) -> None:
    manual = _submit_ready(api, _request(api, manual_path()))
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{manual['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    api.revision = confirmed["revision"]

    replacement = api._candidate(replacement_code)
    replacement["replace_overlay_id"] = confirmed["overlay_id"]
    ready = _submit_ready(api, replacement)
    with api.server.fx_preview_jobs._lock:
        overlay = api.server.fx_preview_jobs._jobs[ready["job"]["id"]]["overlay"]
    assert "spotlight_path" not in overlay
    status, replaced = api._json_request(
        "POST",
        f"/api/fx-previews/{ready['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, replaced
    saved = next(
        item
        for item in replaced["state"]["overlays"]
        if item["id"] == confirmed["overlay_id"]
    )
    assert "spotlight_path" not in saved


def test_manual_replacement_clears_inherited_automatic_subject(api) -> None:
    automatic = api._candidate("P5")
    automatic["candidate"]["subject"] = {"x": 0.2, "y": 0.3}
    automatic_ready = _submit_ready(api, automatic)
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{automatic_ready['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    api.revision = confirmed["revision"]

    replacement = _request(api, manual_path())
    replacement["replace_overlay_id"] = confirmed["overlay_id"]
    manual_ready = _submit_ready(api, replacement)
    with api.server.fx_preview_jobs._lock:
        overlay = api.server.fx_preview_jobs._jobs[manual_ready["job"]["id"]][
            "overlay"
        ]
    assert overlay["subject"] is None
    assert overlay["spotlight_path"] == manual_path()


def test_persisted_state_revalidates_path_and_invalidates_revision(api) -> None:
    manual = _submit_ready(api, _request(api, manual_path()))
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{manual['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    state = confirmed["state"]
    prior_revision = confirmed["revision"]
    state["overlays"][-1]["spotlight_path"]["keyframes"][-1]["x"] = 0.9
    status, saved = api._json_request("PUT", "/api/editor-state", state)
    assert status == 200, saved
    assert saved["revision"] != prior_revision

    state = saved["state"] if "state" in saved else state
    state["revision"] = saved["revision"]
    state["overlays"][-1]["spotlight_path"]["keyframes"][-1]["source_at"] = 2.9
    status, invalid = api._json_request("PUT", "/api/editor-state", state)
    assert status == 422, invalid
    assert any("完整套用範圍" in item for item in invalid["errors"])

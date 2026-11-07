"""Comparison-round binding for sequential FX preview candidates."""

from __future__ import annotations

import json

import pytest

import test_fx_preview_api as _api_fixture


@pytest.fixture
def api() -> _api_fixture.FxPreviewApiTests:
    """Reuse the ordinary HTTP/task fixture without collecting its whole suite."""
    case = _api_fixture.FxPreviewApiTests(
        methodName="test_preview_is_non_mutating_then_confirm_uses_cas"
    )
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def _submit_and_wait(api: _api_fixture.FxPreviewApiTests, request: dict) -> dict:
    status, created = api._json_request("POST", "/api/fx-previews", request)
    assert status in {200, 202}, created
    return api._wait_terminal(created["job"]["id"])


def _job_ids(api: _api_fixture.FxPreviewApiTests) -> set[str]:
    with api.server.fx_preview_jobs._lock:
        return set(api.server.fx_preview_jobs._jobs)


def _assert_comparison_stale(
    api: _api_fixture.FxPreviewApiTests, request: dict
) -> dict:
    before = _job_ids(api)
    status, payload = api._json_request("POST", "/api/fx-previews", request)
    assert status == 409, payload
    assert payload["reason"] == "comparison_stale", payload
    assert _job_ids(api) == before
    return payload


def test_unanchored_sequential_jobs_remain_independent(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    """Baseline: the legacy API intentionally has no cross-job group binding."""
    first = _submit_and_wait(api, api._candidate("T3"))
    assert first["job"]["state"] == "ready"

    # A manifest-only context change does not alter editor_state_revision, so
    # an unanchored follow-up remains a fresh, backward-compatible preview.
    manifest_path = api.project / "project.json"
    with api.server.project_lock:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["comparison_baseline_marker"] = "changed-between-jobs"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = _submit_and_wait(api, api._candidate("T1"))
    assert second["job"]["state"] == "ready"
    assert second["job"]["id"] != first["job"]["id"]


def test_sequential_candidates_share_anchor_and_confirm_their_own_candidate(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    first = _submit_and_wait(api, api._candidate("T3"))
    second_request = api._candidate("T1")
    second_request["comparison_anchor_job_id"] = first["job"]["id"]
    second = _submit_and_wait(api, second_request)
    assert second["job"]["state"] == "ready"

    with api.server.fx_preview_jobs._lock:
        first_record = api.server.fx_preview_jobs._jobs[first["job"]["id"]]
        second_record = api.server.fx_preview_jobs._jobs[second["job"]["id"]]
        assert first_record["comparison_fingerprint_hash"] == second_record[
            "comparison_fingerprint_hash"
        ]
        assert first_record["comparison_fingerprint"]["source"] == second_record[
            "comparison_fingerprint"
        ]["source"]
        assert first_record["overlay"]["code"] == "T3"
        assert second_record["overlay"]["code"] == "T1"

    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{second['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    assert confirmed["state"]["overlays"][-1]["code"] == "T1"

    status, stale = api._json_request(
        "POST",
        f"/api/fx-previews/{first['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 409, stale
    assert stale["reason"] == "preview_stale"


def test_anchor_rejects_context_source_and_window_drift_without_new_jobs(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    first = _submit_and_wait(api, api._candidate("T3"))
    anchor_id = first["job"]["id"]
    manifest_path = api.project / "project.json"
    state_path = api.project / "working/editor_state.json"
    original_manifest = manifest_path.read_bytes()
    original_state = state_path.read_bytes()
    original_source = api.source.read_bytes()

    def anchored(code: str = "T1") -> dict:
        request = api._candidate(code)
        request["comparison_anchor_job_id"] = anchor_id
        return request

    try:
        manifest = json.loads(original_manifest)
        manifest["source"]["duration_s"] = 9.0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _assert_comparison_stale(api, anchored())
    finally:
        manifest_path.write_bytes(original_manifest)

    try:
        api.source.write_bytes(original_source + b"-changed")
        _assert_comparison_stale(api, anchored())
    finally:
        api.source.write_bytes(original_source)

    try:
        state = json.loads(original_state)
        state["comparison_test_marker"] = "changed"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        request = anchored()
        request["expected_revision"] = _api_fixture.editor_server.editor_state_revision(
            state
        )
        _assert_comparison_stale(api, request)
    finally:
        state_path.write_bytes(original_state)

    changed_sample = anchored()
    changed_sample["candidate"]["window"] = {"start_s": 2.0, "duration_s": 2.0}
    _assert_comparison_stale(api, changed_sample)

    changed_apply = anchored()
    changed_apply["candidate"]["apply_window"] = {
        "start_s": 0.0,
        "duration_s": 4.0,
    }
    _assert_comparison_stale(api, changed_apply)

    changed_mode = anchored()
    changed_mode["preview_options"] = {"mode": "timeline", "quality": "standard"}
    _assert_comparison_stale(api, changed_mode)

    changed_quality = anchored()
    changed_quality["preview_options"] = {"mode": "single", "quality": "draft"}
    _assert_comparison_stale(api, changed_quality)


def test_anchor_rejects_different_replace_target(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    setup = _submit_and_wait(api, api._candidate("T3"))
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{setup['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    api.revision = confirmed["revision"]

    first_request = api._candidate("T3")
    first = _submit_and_wait(api, first_request)
    second_request = api._candidate("T1")
    second_request["replace_overlay_id"] = confirmed["overlay_id"]
    second_request["comparison_anchor_job_id"] = first["job"]["id"]
    _assert_comparison_stale(api, second_request)


def test_timeline_anchor_binds_only_common_background_fx_dependencies(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    setup = _submit_and_wait(api, api._candidate("T3"))
    status, confirmed = api._json_request(
        "POST",
        f"/api/fx-previews/{setup['job']['id']}/confirm",
        {"expected_revision": api.revision},
    )
    assert status == 200, confirmed
    api.revision = confirmed["revision"]

    first_request = api._candidate("T1")
    first_request["preview_options"] = {"mode": "timeline", "quality": "standard"}
    first = _submit_and_wait(api, first_request)
    second_request = api._candidate("P5")
    second_request["preview_options"] = {"mode": "timeline", "quality": "standard"}
    second_request["comparison_anchor_job_id"] = first["job"]["id"]
    second = _submit_and_wait(api, second_request)

    with api.server.fx_preview_jobs._lock:
        first_fp = api.server.fx_preview_jobs._jobs[first["job"]["id"]][
            "comparison_fingerprint"
        ]
        second_fp = api.server.fx_preview_jobs._jobs[second["job"]["id"]][
            "comparison_fingerprint"
        ]
    assert first_fp == second_fp
    common = first_fp["renderer"]
    assert set(common["timeline_fx_blocks"]) == {"T3"}
    assert len(common["timeline_fonts_digest"]) == 16
    assert common["timeline_material_authority"] == {}

    replace_first = api._candidate("T1")
    replace_first["preview_options"] = {"mode": "timeline", "quality": "standard"}
    replace_first["replace_overlay_id"] = confirmed["overlay_id"]
    replacement_anchor = _submit_and_wait(api, replace_first)
    replace_second = api._candidate("P5")
    replace_second["preview_options"] = {"mode": "timeline", "quality": "standard"}
    replace_second["replace_overlay_id"] = confirmed["overlay_id"]
    replace_second["comparison_anchor_job_id"] = replacement_anchor["job"]["id"]
    replacement_follow_up = _submit_and_wait(api, replace_second)
    with api.server.fx_preview_jobs._lock:
        replacement_fp = api.server.fx_preview_jobs._jobs[
            replacement_follow_up["job"]["id"]
        ]["comparison_fingerprint"]
    assert replacement_fp["renderer"]["timeline_fx_blocks"] == {}


def test_failed_anchor_remains_usable_but_cancelled_anchor_fails_closed(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    manager = api.server.fx_preview_jobs
    manager.artifact_validator = lambda _path, _duration: (_ for _ in ()).throw(
        ValueError("ordinary fixture validation failure")
    )
    failed = _submit_and_wait(api, api._candidate("T3"))
    assert failed["job"]["state"] == "failed"

    manager.artifact_validator = lambda _path, duration: {"duration_s": duration}
    follow_up = api._candidate("T1")
    follow_up["comparison_anchor_job_id"] = failed["job"]["id"]
    ready = _submit_and_wait(api, follow_up)
    assert ready["job"]["state"] == "ready"

    manager.renderer_script = api.slow_renderer.resolve()
    status, created = api._json_request("POST", "/api/fx-previews", api._candidate("T3"))
    assert status == 202, created
    cancelled_id = created["job"]["id"]
    status, cancelled = api._json_request(
        "POST", f"/api/fx-previews/{cancelled_id}/cancel", {}
    )
    assert status == 200, cancelled
    assert cancelled["job"]["state"] == "cancelled"

    cancelled_follow_up = api._candidate("T1")
    cancelled_follow_up["comparison_anchor_job_id"] = cancelled_id
    _assert_comparison_stale(api, cancelled_follow_up)


def test_preview_stale_failed_anchor_cannot_be_reused_after_source_restore(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    manager = api.server.fx_preview_jobs
    manager.renderer_script = api.slow_renderer.resolve()
    original_source = api.source.read_bytes()
    status, created = api._json_request("POST", "/api/fx-previews", api._candidate("T3"))
    assert status == 202, created
    anchor_id = created["job"]["id"]
    try:
        api.source.write_bytes(b"changed-source")
        terminal = api._wait_terminal(anchor_id, timeout=7)
        assert terminal["job"]["state"] == "failed"
        assert terminal["job"]["error"]["code"] == "preview_stale"
    finally:
        api.source.write_bytes(original_source)

    follow_up = api._candidate("T1")
    follow_up["comparison_anchor_job_id"] = anchor_id
    _assert_comparison_stale(api, follow_up)


def test_anchor_identifier_errors_are_typed_and_create_no_job(
    api: _api_fixture.FxPreviewApiTests,
) -> None:
    invalid = api._candidate("T1")
    invalid["comparison_anchor_job_id"] = "not-a-job-id"
    before = _job_ids(api)
    status, payload = api._json_request("POST", "/api/fx-previews", invalid)
    assert status == 422, payload
    assert payload["reason"] == "comparison_anchor_invalid"
    assert _job_ids(api) == before

    missing = api._candidate("T1")
    missing["comparison_anchor_job_id"] = "a" * 32
    _assert_comparison_stale(api, missing)

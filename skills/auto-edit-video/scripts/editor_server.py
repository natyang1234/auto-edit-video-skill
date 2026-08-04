#!/usr/bin/env python3
"""Local review/editor server for an auto-edit-video project.

The server binds to loopback by default, serves media with HTTP Range support,
and only reads/writes inside the selected project directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import caption_engine
from local_http_security import (
    csrf_token_matches,
    host_header_allowed,
    is_loopback_host,
    mutation_origin_allowed,
)
from visual_quality import (
    ROLE_LAYOUTS,
    build_highlight_design_overlays,
    visual_quality_errors,
    visual_quality_report,
)
from template_catalog import (
    cutout_capability,
    default_video_template_state,
    public_template_catalog,
    template_readiness_errors,
    upgrade_video_template_state,
    validate_video_template_state,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
EDITOR_DIR = SKILL_DIR / "editor"
STATE_REL = Path("working/editor_state.json")
LATEST_DELIVERY_QA_REL = Path("working/latest_final_qa.json")
ALLOWED_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov"}
ALLOWED_ASSET_MIME_TYPES = {
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".gif": {"image/gif"},
    ".mp4": {"video/mp4", "application/mp4"},
    ".mov": {"video/quicktime"},
}
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 50 * 1024 * 1024
GATES = ("destructive_edit", "highlight_selection", "timeline", "final")
VOICE_LANGUAGES = {"zh-TW", "zh-CN", "en-US", "en-GB"}
VOICE_GENDERS = {"female", "male"}

def _load_platform_presets() -> dict[str, dict[str, Any]]:
    """Platform presets come from the versioned registry — the ONLY runtime
    source (unified with contracts; hardcoded dicts were retired in Phase 1a).
    A missing or invalid registry fails closed instead of silently falling
    back to stale values."""
    import contract_registry

    registry_path = (
        SKILL_DIR / "contracts/instances/platform_preset__registry.json"
    )
    try:
        payload = contract_registry.load_artifact_text(
            registry_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"platform preset registry unreadable: {exc}") from exc
    errors = contract_registry.validate_artifact("platform_preset", payload)
    if errors:
        raise RuntimeError(
            "platform preset registry failed contract validation: "
            + "; ".join(errors)
        )
    return {preset["id"]: dict(preset) for preset in payload["presets"]}


PLATFORM_PRESETS: dict[str, dict[str, Any]] = _load_platform_presets()

def _load_director_presets() -> dict[str, dict[str, Any]]:
    """Director presets load from the versioned registry (runtime SSOT,
    symmetric with _load_platform_presets); missing/invalid fails closed."""
    import contract_registry

    registry_path = SKILL_DIR / "contracts/instances/director_mode__registry.json"
    try:
        payload = contract_registry.load_artifact_text(
            registry_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"director mode registry unreadable: {exc}") from exc
    errors = contract_registry.validate_artifact("director_mode", payload)
    if errors:
        raise RuntimeError(
            "director mode registry failed contract validation: " + "; ".join(errors)
        )
    return {mode["id"]: dict(mode["constraints"]) for mode in payload["modes"]}


DIRECTOR_PRESETS: dict[str, dict[str, Any]] = _load_director_presets()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def editor_state_revision(state: dict[str, Any]) -> str:
    """Hash only fields that can change the rendered video."""
    canvas = state.get("canvas") if isinstance(state.get("canvas"), dict) else {}
    raw_revision_overlays = state.get("overlays", [])
    if not isinstance(raw_revision_overlays, list):
        raw_revision_overlays = []
    revision_overlays = [
        {
            key: value
            for key, value in overlay.items()
            if key != "semantic_review"
        }
        if isinstance(overlay, dict)
        else overlay
        for overlay in raw_revision_overlays
    ]
    payload = {
        "schema_version": state.get("schema_version"),
        "project_id": state.get("project_id"),
        "segments": state.get("segments"),
        "canvas": {
            key: canvas.get(key)
            for key in ("platform_id", "width", "height", "fps", "fit")
        },
        "director_style": state.get("director_style"),
        "video_template": state.get("video_template"),
        "source_sha256": state.get("source_sha256"),
        "highlight_plan_revision": state.get("highlight_plan_revision"),
        "highlights": state.get("highlights"),
        "asset_digests": state.get("asset_digests"),
        "caption_defaults": state.get("caption_defaults"),
        "overlays": revision_overlays,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


EDITOR_STATE_SCHEMA_VERSION = 2
MIGRATION_REASON = (
    "editor_state v1→v2 migration: approvals must be re-confirmed under the "
    "unified timeline contract"
)


def default_source_segments(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """v2 default: one full-length source segment (unified timeline contract)."""
    source = manifest.get("source", {}) if isinstance(manifest.get("source"), dict) else {}
    try:
        duration = float(source.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    seed = f"{source.get('sha256') or ''}:full"
    segment_id = "segment-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return [
        {
            "id": segment_id,
            "source_start": 0.0,
            "source_end": max(duration, 0.001),
            "origin": "default_full_source",
        }
    ]


def migrate_editor_state_v1_to_v2(
    project_dir: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """One-way v1→v2 upgrade; persists state and explicitly voids every gate.

    Contract: contracts/policies/EDITOR_STATE_V2_MIGRATION.md. Approvals are
    overwritten to approved:false with a migration note — never left to the
    indirect effect of a revision drift.
    """
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return state, False
    # Transaction order matters: void the approvals FIRST. If the state write
    # below fails, the surviving combination is "voided approvals + v1 state",
    # which the next load retries (voiding again is idempotent). The reverse
    # order could leave a v2 state with live v1 approvals — a fake-approval
    # state that no later pass would repair.
    approvals = manifest.setdefault("approvals", {})
    for gate in GATES:
        approvals[gate] = {
            "approved": False,
            "confirmed_by": None,
            "at": None,
            "note": MIGRATION_REASON,
            "invalidated_at": now_utc(),
        }
    manifest["updated_at"] = now_utc()
    atomic_write_json(project_dir / "project.json", manifest)
    previous_revision = str(state.get("revision") or "")
    state["schema_version"] = EDITOR_STATE_SCHEMA_VERSION
    state["segments"] = default_source_segments(manifest)
    state["variants"] = []
    state["rights"] = {"asserted": False, "assertion_revision": None}
    state["migrated_from"] = {
        "schema_version": 1,
        "at": now_utc(),
        "reason": MIGRATION_REASON,
        "previous_revision": previous_revision,
    }
    state["updated_at"] = now_utc()
    state["revision"] = editor_state_revision(state)
    atomic_write_json(project_dir / STATE_REL, state)
    return state, True


def canonical_revision(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def gate_revision(
    project_dir: Path,
    gate: str,
    state: dict[str, Any] | None = None,
) -> str:
    if gate == "destructive_edit":
        return canonical_revision(
            {
                "candidates": read_json(
                    project_dir / "working/edit_candidates.json", {"items": []}
                ),
                "decisions": read_json(
                    project_dir / "working/edit_decisions.json", {"items": []}
                ),
            }
        )
    current_state = state
    if current_state is None:
        current_state = read_json(project_dir / STATE_REL, {}) or {}
    if current_state.get("schema_version") == 1:
        raise ValueError(
            "editor_state schema_version 1 must be migrated first; open the "
            "editor page once to run the v1→v2 migration"
        )
    if gate == "highlight_selection":
        return canonical_revision(
            {
                "source_sha256": current_state.get("source_sha256"),
                "highlight_plan_revision": current_state.get("highlight_plan_revision"),
                "highlights": current_state.get("highlights", []),
            }
        )
    if gate == "timeline":
        return editor_state_revision(current_state)
    if gate == "final":
        receipt = read_json(project_dir / LATEST_DELIVERY_QA_REL, None)
        return canonical_revision(
            {
                "editor_state_revision": editor_state_revision(current_state),
                "delivery_qa": receipt if isinstance(receipt, dict) else None,
            }
        )
    raise ValueError(f"unsupported approval gate: {gate}")


def approval_is_current(
    project_dir: Path,
    manifest: dict[str, Any],
    gate: str,
    state: dict[str, Any] | None = None,
) -> bool:
    approval = manifest.get("approvals", {}).get(gate, {})
    try:
        expected_revision = gate_revision(project_dir, gate, state)
    except ValueError:
        # Un-migrated v1 state: no approval can be considered current.
        return False
    current = bool(
        isinstance(approval, dict)
        and approval.get("approved")
        and approval.get("state_revision") == expected_revision
    )
    if current and gate == "final":
        current = not delivery_qa_errors(
            project_dir,
            state if state is not None else read_json(project_dir / STATE_REL, {}) or {},
        )
    return current


def _delivery_receipt_paths(receipt: Any, keys: tuple[str, ...]) -> set[str]:
    paths: set[str] = set()
    if not isinstance(receipt, dict):
        return paths
    for key in keys:
        value = receipt.get(key)
        if value:
            paths.add(str(value))
    items = receipt.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in keys:
                value = item.get(key)
                if value:
                    paths.add(str(value))
    return paths


def render_receipt_index(project_dir: Path) -> tuple[set[str], set[str]]:
    """Classify render outputs from receipts into (final_paths, preview_paths)."""
    final_paths: set[str] = set()
    preview_paths: set[str] = set()
    receipts_dir = project_dir / "working/render_receipts"
    if receipts_dir.is_dir():
        for entry in sorted(receipts_dir.glob("*.json")):
            receipt = read_json(entry, None)
            if not isinstance(receipt, dict):
                continue
            output = str(receipt.get("output") or "")
            if not output:
                continue
            if receipt.get("quality") == "final":
                final_paths.add(output)
            else:
                preview_paths.add(output)
    delivery_dir = project_dir / "working/delivery_qa"
    if delivery_dir.is_dir():
        for entry in sorted(delivery_dir.glob("*.json")):
            final_paths |= _delivery_receipt_paths(
                read_json(entry, None), ("output", "archive")
            )
    final_paths |= _delivery_receipt_paths(
        read_json(project_dir / LATEST_DELIVERY_QA_REL, None), ("output", "archive")
    )
    return final_paths, preview_paths


def render_download_errors(project_dir: Path, relative: str) -> list[str]:
    """Server-side final download gate (contracts/policies/DOWNLOAD_GATE.md).

    Preview outputs and the cover image stay reachable for review; every
    final artifact requires a current final approval AND membership in the
    current delivery receipt (whose digests approval_is_current re-verifies).
    Files no receipt can vouch for fail closed.
    """
    if relative == "renders/cover.png":
        return []
    final_paths, preview_paths = render_receipt_index(project_dir)
    if relative not in final_paths:
        if relative in preview_paths:
            return []
        return ["download is not covered by any render receipt"]
    manifest = read_json(project_dir / "project.json", {}) or {}
    state = read_json(project_dir / STATE_REL, {}) or {}
    if not approval_is_current(project_dir, manifest, "final", state):
        return ["final output requires a current final approval before download"]
    current = _delivery_receipt_paths(
        read_json(project_dir / LATEST_DELIVERY_QA_REL, None), ("output", "archive")
    )
    if relative not in current:
        return ["final output is not part of the current delivery receipt"]
    return []


def qa_download_errors(project_dir: Path, relative: str) -> list[str]:
    """QA evidence gate: free to read until final approval, then receipt-bound."""
    manifest = read_json(project_dir / "project.json", {}) or {}
    state = read_json(project_dir / STATE_REL, {}) or {}
    if not approval_is_current(project_dir, manifest, "final", state):
        return []
    allowed = _delivery_receipt_paths(
        read_json(project_dir / LATEST_DELIVERY_QA_REL, None),
        ("report", "contact_sheet"),
    )
    if relative in allowed:
        return []
    return ["only current delivery QA evidence can be read after final approval"]


def migrate_caption_spans(state: dict[str, Any]) -> list[str]:
    """N0 migration: normalise caption text and snap legacy spans to clusters.

    Returns human-readable warnings for spans that had to move or be removed.
    Only runs when the macOS caption engine is available; without it legacy
    spans stay untouched (and the effect-span final gate keeps them out of
    final renders).
    """
    if not caption_engine.available():
        return []
    warnings: list[str] = []
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict) or overlay.get("type") not in {"caption", "emphasis"}:
            continue
        text = str(overlay.get("text") or "")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if normalized != text:
            overlay["text"] = normalized
            warnings.append(f"overlay {overlay.get('id')}: caption text CR normalised")
            text = normalized
        spans = overlay.get("effect_spans")
        if not isinstance(spans, list) or not spans:
            continue
        kept: list[dict[str, Any]] = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            try:
                start = int(span.get("start_char"))
                end = int(span.get("end_char"))
            except (TypeError, ValueError):
                warnings.append(
                    f"overlay {overlay.get('id')}: effect span {span.get('id')} removed (invalid range)"
                )
                continue
            snapped = caption_engine.snap_span(text, start, end)
            if snapped is None:
                warnings.append(
                    f"overlay {overlay.get('id')}: effect span {span.get('id')} removed "
                    "(cannot align to grapheme clusters)"
                )
                continue
            new_start, new_end = snapped
            if (new_start, new_end) != (start, end):
                warnings.append(
                    f"overlay {overlay.get('id')}: effect span {span.get('id')} snapped "
                    f"{start}-{end} → {new_start}-{new_end}"
                )
            span["start_char"] = new_start
            span["end_char"] = new_end
            span["text"] = caption_engine.slice_utf16(text, new_start, new_end)
            kept.append(span)
        overlay["effect_spans"] = kept
    return warnings


def effect_span_final_errors(
    state: dict[str, Any],
    clip: dict[str, Any] | None,
) -> list[str]:
    """Interim Phase 1a gate mirroring the renderer's effect-span fail-closed.

    The designed route renders spans through the graphic package; every other
    final route uses drawtext, which drops them silently — block approval so
    the loss is visible before render time.
    """
    spans_present = any(
        overlay.get("effect_spans")
        for overlay in state.get("overlays", [])
        if isinstance(overlay, dict) and overlay.get("visible", True)
    )
    if not spans_present:
        return []
    import caption_compositor

    if caption_compositor.compositor_available():
        # Route table (plan v2): with the compositor every caption route
        # renders spans — nothing to gate here.
        return []
    if clip is not None and state.get("visual_quality_mode") == "designed":
        # Same clip-scoped role resolution the renderer uses to pick the
        # designed route (visual_quality.overlays_for_clip) — global roles
        # from other highlights must not unlock this clip's approval.
        from visual_quality import DESIGN_ROLES, overlays_for_clip

        roles = {
            str(item.get("design_role"))
            for item in overlays_for_clip(state, clip)
            if item.get("design_role")
        }
        if set(DESIGN_ROLES).issubset(roles):
            return []
    return [
        "per-character effect spans are not rendered by the current final route; "
        "use designed mode with a complete design-role set or remove the effects "
        "(caption compositor lands in Phase 1b)"
    ]


def approval_prerequisite_errors(
    project_dir: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    gate: str,
) -> list[str]:
    errors: list[str] = []
    if gate == "destructive_edit":
        candidates = read_json(
            project_dir / "working/edit_candidates.json", {"items": []}
        ) or {"items": []}
        decisions = read_json(
            project_dir / "working/edit_decisions.json", {"items": []}
        ) or {"items": []}
        candidate_ids = {
            str(item.get("id"))
            for item in candidates.get("items", [])
            if isinstance(item, dict)
        }
        approved_decisions = {
            str(item.get("candidate_id"))
            for item in decisions.get("items", [])
            if isinstance(item, dict) and item.get("review_status") == "approved"
        }
        if candidate_ids != approved_decisions:
            errors.append("every edit candidate must have an explicit approved keep/delete decision")
        return errors

    if not approval_is_current(project_dir, manifest, "destructive_edit", state):
        errors.append("destructive_edit must be approved for its current revision first")

    highlights = state.get("highlights", []) if isinstance(state.get("highlights"), list) else []
    if gate == "highlight_selection":
        plan = read_json(project_dir / "working/highlight_plan.json", {}) or {}
        if not highlights:
            errors.append("at least one transcript-grounded highlight is required")
        if state.get("highlight_plan_revision") != plan.get("plan_revision"):
            errors.append("highlight plan revision is stale")
        plan_ids = {
            str(item.get("id"))
            for item in plan.get("items", [])
            if isinstance(item, dict)
        }
        for item in highlights:
            if not isinstance(item, dict) or str(item.get("plan_item_id")) not in plan_ids:
                errors.append("every reviewed highlight must reference the current highlight plan")
                break
        if not any(
            isinstance(item, dict) and item.get("review_status") == "approved"
            for item in highlights
        ):
            errors.append("at least one highlight must be marked approved")
        return errors

    if highlights and not approval_is_current(
        project_dir, manifest, "highlight_selection", state
    ):
        errors.append("highlight_selection must be approved for its current revision first")
    active_id = str(state.get("active_highlight_id") or "")
    active_clip = next(
        (
            item
            for item in highlights
            if isinstance(item, dict) and str(item.get("id")) == active_id
        ),
        None,
    )
    if highlights and active_clip is None:
        errors.append("an active highlight is required for timeline review")
    elif active_clip is not None and active_clip.get("review_status") != "approved":
        errors.append("the active highlight must be approved for timeline review")
    if active_clip is not None:
        errors.extend(visual_quality_errors(state, manifest, active_clip))
    if gate == "timeline":
        return errors
    if gate == "final":
        if not approval_is_current(project_dir, manifest, "timeline", state):
            errors.append("timeline must be approved for its current revision first")
        errors.extend(effect_span_final_errors(state, active_clip))
        if approved_destructive_deletes(project_dir):
            errors.append(
                "reviewed delete decisions are not applied by the page-editor renderer; "
                "set them to keep or use the destructive cut renderer first"
            )
        errors.extend(delivery_qa_errors(project_dir, state))
    return errors


def approval_revisions(project_dir: Path, state: dict[str, Any] | None = None) -> dict[str, str]:
    def safe_revision(gate: str) -> str:
        try:
            return gate_revision(project_dir, gate, state)
        except ValueError:
            # Un-migrated v1 state: expose a non-hex sentinel no approval can
            # ever match instead of failing the whole status route.
            return "unmigrated-editor-state-v1"

    return {
        gate: safe_revision(gate)
        for gate in sorted(GATES)
    }


def approved_destructive_deletes(project_dir: Path) -> list[str]:
    decisions = read_json(
        project_dir / "working/edit_decisions.json",
        {"items": []},
    ) or {"items": []}
    return [
        str(item.get("candidate_id"))
        for item in decisions.get("items", [])
        if isinstance(item, dict)
        and item.get("review_status") == "approved"
        and item.get("action") == "delete"
    ]


def approved_highlights_in_plan_order(
    project_dir: Path,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the server-derived approved batch in the current plan's order."""
    errors: list[str] = []
    highlights = (
        state.get("highlights", [])
        if isinstance(state.get("highlights"), list)
        else []
    )
    approved = [
        item
        for item in highlights
        if isinstance(item, dict) and item.get("review_status") == "approved"
    ]
    plan = read_json(project_dir / "working/highlight_plan.json", {}) or {}
    plan_items = [
        item for item in plan.get("items", []) if isinstance(item, dict)
    ]
    plan_ids = [str(item.get("id") or "") for item in plan_items]
    if not approved:
        return [], ["at least one approved highlight is required for batch render"]
    if any(not item_id for item_id in plan_ids) or len(set(plan_ids)) != len(plan_ids):
        errors.append("highlight plan contains invalid or duplicate clip identities")
        return [], errors

    by_plan_id: dict[str, dict[str, Any]] = {}
    for item in approved:
        plan_id = str(item.get("plan_item_id") or "")
        clip_id = str(item.get("id") or "")
        if plan_id not in plan_ids:
            errors.append(f"approved highlight {clip_id or '<unknown>'} is outside the current plan")
            continue
        if plan_id in by_plan_id:
            errors.append(f"multiple approved highlights reference plan item {plan_id}")
            continue
        by_plan_id[plan_id] = item
    ordered = [by_plan_id[item_id] for item_id in plan_ids if item_id in by_plan_id]
    if len(ordered) != len(approved):
        errors.append("approved highlight set could not be resolved one-to-one from the current plan")
    return ordered, errors


def delivery_qa_errors(project_dir: Path, state: dict[str, Any]) -> list[str]:
    """Verify that the final gate is tied to current, untampered delivery artifacts."""
    receipt = read_json(project_dir / LATEST_DELIVERY_QA_REL, None)
    if not isinstance(receipt, dict):
        return ["a successful delivery QA receipt is required before final approval"]
    if receipt.get("schema_version") == 2:
        return batch_delivery_qa_errors(project_dir, state, receipt)
    return single_delivery_qa_errors(project_dir, state, receipt)


def single_delivery_qa_errors(
    project_dir: Path,
    state: dict[str, Any],
    receipt: dict[str, Any],
) -> list[str]:
    """Validate the backward-compatible schema-v1 single-clip delivery receipt."""
    errors: list[str] = []
    if receipt.get("schema_version") != 1 or receipt.get("status") != "pass":
        errors.append("delivery QA must pass before final approval")
    state_revision = editor_state_revision(state)
    if receipt.get("state_revision") != state_revision:
        errors.append("delivery QA does not match the current editor revision")

    artifact_contracts = (
        ("output", "output_sha256", "renders"),
        ("report", "report_sha256", "qa"),
        ("contact_sheet", "contact_sheet_sha256", "qa"),
        ("render_receipt", "render_receipt_sha256", "working/render_receipts"),
    )
    resolved: dict[str, Path] = {}
    for path_key, digest_key, scope in artifact_contracts:
        relative = str(receipt.get(path_key) or "")
        declared = str(receipt.get(digest_key) or "")
        if not relative or not re.fullmatch(r"[0-9a-f]{64}", declared):
            errors.append(f"delivery QA {path_key} contract is incomplete")
            continue
        try:
            entry = project_dir / Path(relative)
            if entry.is_symlink():
                raise ValueError(f"{path_key} must not be a symlink")
            path = scoped_project_path(project_dir, relative, scope)
        except ValueError:
            errors.append(f"delivery QA {path_key} escapes its project scope")
            continue
        if not path.is_file():
            errors.append(f"delivery QA {path_key} is missing")
            continue
        if file_sha256(path) != declared:
            errors.append(f"delivery QA {path_key} changed after verification")
            continue
        resolved[path_key] = path

    report = read_json(resolved.get("report", Path("/nonexistent")), None)
    if not isinstance(report, dict) or report.get("status") != "pass":
        errors.append("delivery QA report is missing a passing status")
    render_receipt = read_json(resolved.get("render_receipt", Path("/nonexistent")), None)
    if not isinstance(render_receipt, dict):
        errors.append("render receipt is unreadable")
    else:
        if render_receipt.get("render_id") != receipt.get("render_id"):
            errors.append("delivery QA and render receipt identities differ")
        if render_receipt.get("quality") != "final":
            errors.append("delivery QA is not attached to a final render")
        if render_receipt.get("state_revision") != state_revision:
            errors.append("render receipt does not match the current editor revision")
        if render_receipt.get("output_sha256") != receipt.get("output_sha256"):
            errors.append("delivery QA and render receipt output digests differ")
    highlights = state.get("highlights", []) if isinstance(state.get("highlights"), list) else []
    receipt_clip_id = str(receipt.get("clip_id") or state.get("active_highlight_id") or "")
    clip = next(
        (
            item
            for item in highlights
            if isinstance(item, dict) and str(item.get("id")) == receipt_clip_id
        ),
        None,
    )
    expected_visual = visual_quality_report(state, read_json(project_dir / "project.json", {}) or {}, clip)
    receipt_visual = receipt.get("visual_quality")
    if expected_visual.get("contract_applies"):
        if not isinstance(receipt_visual, dict) or receipt_visual.get("status") != "pass":
            errors.append("delivery QA visual-quality contract must pass before final approval")
        elif any(
            receipt_visual.get(key) != expected_visual.get(key)
            for key in (
                "mode",
                "clip_id",
                "designed_card_count",
                "designed_roles",
                "designed_types",
                "designed_coverage_ratio",
            )
        ):
            errors.append("delivery QA visual-quality contract does not match the current timeline")
    return errors


def batch_delivery_qa_errors(
    project_dir: Path,
    state: dict[str, Any],
    receipt: dict[str, Any],
) -> list[str]:
    """Validate every clip and archive member in a schema-v2 batch delivery."""
    errors: list[str] = []
    if (
        receipt.get("kind") != "batch"
        or receipt.get("delivery_kind") != "batch"
        or receipt.get("quality") != "final"
        or receipt.get("status") != "pass"
    ):
        errors.append("batch delivery QA must be a passing final delivery")
    batch_id = str(receipt.get("batch_id") or "")
    if not re.fullmatch(r"batch_[0-9a-f]{32}", batch_id):
        errors.append("batch delivery identity is invalid")
    state_revision = editor_state_revision(state)
    if receipt.get("state_revision") != state_revision:
        errors.append("batch delivery QA does not match the current editor revision")

    expected_clips, clip_errors = approved_highlights_in_plan_order(project_dir, state)
    errors.extend(clip_errors)
    expected_ids = [str(item.get("id") or "") for item in expected_clips]
    declared_ids = receipt.get("clip_ids")
    if not isinstance(declared_ids, list) or declared_ids != expected_ids:
        errors.append("batch delivery clip set does not match all currently approved highlights")

    items = receipt.get("items")
    if not isinstance(items, list):
        errors.append("batch delivery items contract is missing")
        items = []
    if receipt.get("item_count") != len(expected_ids) or len(items) != len(expected_ids):
        errors.append("batch delivery item count does not match the approved clip set")
    item_ids = [str(item.get("clip_id") or "") for item in items if isinstance(item, dict)]
    if len(item_ids) != len(items) or item_ids != expected_ids or len(set(item_ids)) != len(item_ids):
        errors.append("batch delivery items do not match the approved clip order")

    manifest = read_json(project_dir / "project.json", {}) or {}
    clips_by_id = {str(item.get("id") or ""): item for item in expected_clips}
    archive_members: dict[str, str] = {}
    render_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"batch delivery item {index} is invalid")
            continue
        clip_id = str(item.get("clip_id") or "")
        render_id = str(item.get("render_id") or "")
        if (
            item.get("schema_version") != 1
            or item.get("batch_id") != batch_id
            or item.get("quality") != "final"
            or item.get("status") != "pass"
            or item.get("state_revision") != state_revision
        ):
            errors.append(f"batch delivery item {clip_id or index} is not a passing current final render")
        if not render_id or render_id in render_ids:
            errors.append(f"batch delivery item {clip_id or index} has an invalid render identity")
        render_ids.add(render_id)

        artifact_contracts = (
            ("output", "output_sha256", "renders"),
            ("report", "report_sha256", "qa"),
            ("contact_sheet", "contact_sheet_sha256", "qa"),
            ("render_receipt", "render_receipt_sha256", "working/render_receipts"),
        )
        resolved: dict[str, Path] = {}
        for path_key, digest_key, scope in artifact_contracts:
            relative = str(item.get(path_key) or "")
            declared = str(item.get(digest_key) or "")
            if not relative or not re.fullmatch(r"[0-9a-f]{64}", declared):
                errors.append(f"batch item {clip_id or index} {path_key} contract is incomplete")
                continue
            try:
                entry = project_dir / Path(relative)
                if entry.is_symlink():
                    raise ValueError(f"{path_key} must not be a symlink")
                path = scoped_project_path(project_dir, relative, scope)
            except ValueError:
                errors.append(f"batch item {clip_id or index} {path_key} escapes its project scope")
                continue
            if not path.is_file():
                errors.append(f"batch item {clip_id or index} {path_key} is missing")
                continue
            if file_sha256(path) != declared:
                errors.append(f"batch item {clip_id or index} {path_key} changed after verification")
                continue
            resolved[path_key] = path

        report = read_json(resolved.get("report", Path("/nonexistent")), None)
        if not isinstance(report, dict) or report.get("status") != "pass":
            errors.append(f"batch item {clip_id or index} QA report is not passing")
        render_receipt = read_json(
            resolved.get("render_receipt", Path("/nonexistent")),
            None,
        )
        if not isinstance(render_receipt, dict):
            errors.append(f"batch item {clip_id or index} render receipt is unreadable")
        else:
            if render_receipt.get("render_id") != render_id:
                errors.append(f"batch item {clip_id or index} render identities differ")
            if render_receipt.get("batch_id") != batch_id:
                errors.append(f"batch item {clip_id or index} render receipt batch differs")
            if render_receipt.get("clip_id") != clip_id:
                errors.append(f"batch item {clip_id or index} render receipt clip differs")
            if render_receipt.get("quality") != "final":
                errors.append(f"batch item {clip_id or index} is not attached to a final render")
            if render_receipt.get("state_revision") != state_revision:
                errors.append(f"batch item {clip_id or index} render receipt is stale")
            if render_receipt.get("output_sha256") != item.get("output_sha256"):
                errors.append(f"batch item {clip_id or index} output digests differ")
            if render_receipt.get("output") != item.get("output"):
                errors.append(f"batch item {clip_id or index} output paths differ")

        clip = clips_by_id.get(clip_id)
        expected_visual = visual_quality_report(state, manifest, clip)
        receipt_visual = item.get("visual_quality")
        if expected_visual.get("contract_applies"):
            if not isinstance(receipt_visual, dict) or receipt_visual.get("status") != "pass":
                errors.append(f"batch item {clip_id or index} visual-quality contract must pass")
            elif any(
                receipt_visual.get(key) != expected_visual.get(key)
                for key in (
                    "mode",
                    "clip_id",
                    "designed_card_count",
                    "designed_roles",
                    "designed_types",
                    "designed_coverage_ratio",
                )
            ):
                errors.append(f"batch item {clip_id or index} visual-quality contract is stale")

        archive_name = str(item.get("archive_name") or "")
        if (
            not archive_name
            or Path(archive_name).name != archive_name
            or archive_name in archive_members
        ):
            errors.append(f"batch item {clip_id or index} archive member is invalid")
        else:
            archive_members[archive_name] = str(item.get("output_sha256") or "")

    archive_rel = str(receipt.get("archive") or "")
    archive_sha = str(receipt.get("archive_sha256") or "")
    archive_download_name = str(receipt.get("archive_download_name") or "")
    archive_path: Path | None = None
    if not archive_rel or not re.fullmatch(r"[0-9a-f]{64}", archive_sha):
        errors.append("batch delivery archive contract is incomplete")
    elif archive_download_name != Path(archive_rel).name:
        errors.append("batch delivery archive download name is inconsistent")
    else:
        try:
            archive_entry = project_dir / Path(archive_rel)
            if archive_entry.is_symlink():
                raise ValueError("archive must not be a symlink")
            archive_path = scoped_project_path(project_dir, archive_rel, "renders")
        except ValueError:
            errors.append("batch delivery archive escapes its project scope")
        else:
            if not archive_path.is_file():
                errors.append("batch delivery archive is missing")
                archive_path = None
            elif file_sha256(archive_path) != archive_sha:
                errors.append("batch delivery archive changed after verification")
                archive_path = None
    if archive_path is not None:
        try:
            with zipfile.ZipFile(archive_path, "r") as bundle:
                infos = bundle.infolist()
                names = [info.filename for info in infos if not info.is_dir()]
                if len(names) != len(set(names)) or set(names) != set(archive_members):
                    errors.append("batch delivery archive members do not match the receipt")
                for name, expected_digest in archive_members.items():
                    if name not in names:
                        continue
                    digest = hashlib.sha256()
                    with bundle.open(name, "r") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != expected_digest:
                        errors.append(f"batch delivery archive member {name} changed after verification")
        except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
            errors.append("batch delivery archive is unreadable")
    return errors


def project_path(project_dir: Path, relative: str) -> Path:
    candidate = (project_dir / relative).resolve()
    if project_dir.resolve() not in candidate.parents and candidate != project_dir.resolve():
        raise ValueError("path escapes project directory")
    return candidate


def scoped_project_path(project_dir: Path, relative: str, scope: str) -> Path:
    """Resolve a path while keeping it inside one project subdirectory."""
    candidate = project_path(project_dir, relative)
    scope_root = (project_dir / scope).resolve()
    if candidate != scope_root and scope_root not in candidate.parents:
        raise ValueError(f"path escapes {scope}/")
    return candidate


def project_entry_path(project_dir: Path, relative: str) -> Path:
    """Resolve a regular project entry and reject symlink escapes."""
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError("path must be project-relative")
    root = project_dir.resolve()
    entry = project_dir / relative_path
    if entry.is_symlink():
        raise ValueError("project entry must not be a symlink")
    candidate = entry.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("path escapes project directory")
    return candidate


def referenced_asset_digests(project_dir: Path, state: dict[str, Any]) -> dict[str, str]:
    """Hash every renderable user asset referenced by the editor state."""
    digests: dict[str, str] = {}
    sources: list[str] = []
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict) or overlay.get("type") not in {"image", "gif", "video"}:
            continue
        source = str(overlay.get("source", ""))
        if source:
            sources.append(source)
    video_template = state.get("video_template")
    if isinstance(video_template, dict):
        background = video_template.get("background")
        if isinstance(background, dict) and background.get("source"):
            sources.append(str(background["source"]))
    for source in sources:
        if source in digests:
            continue
        entry = project_dir / Path(source)
        if entry.is_symlink():
            raise ValueError(f"asset {source} must be an owned regular file")
        asset = scoped_project_path(project_dir, source, "assets")
        if not asset.is_file():
            raise ValueError(f"asset {source} is missing")
        digest = hashlib.sha256()
        with asset.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digests[source] = digest.hexdigest()
    return dict(sorted(digests.items()))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_magic_matches(data: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if suffix == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return len(data) >= 8 and data[4:8] in {
        b"ftyp",
        b"moov",
        b"wide",
        b"mdat",
        b"free",
        b"skip",
    }


def ffprobe_has_visual_stream(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return False
    return bool(streams and streams[0].get("codec_type") == "video")


def artifact_plan_overlays(
    project_dir: Path,
    caption_style: dict[str, Any],
    duration_s: float,
) -> list[dict[str, Any]]:
    """Convert reviewed-plan artifacts into editable timeline proposals."""
    overlays: list[dict[str, Any]] = []
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json", {"items": []}) or {
        "items": []
    }
    for index, item in enumerate(emphasis_plan.get("items", []), start=1):
        try:
            start = max(0.0, float(item.get("start", 0.0)))
            end = min(duration_s, float(item.get("end", start + 0.5)))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if not text or end <= start:
            continue
        style = dict(caption_style)
        style.update(
            {
                "font_size": max(68, int(float(style.get("font_size", 58))) + 12),
                "color": style.get("emphasis_color", "#ffd447"),
                "y": max(18, float(style.get("y", 76)) - 14),
                "max_width": min(78, float(style.get("max_width", 86))),
                "animation": "pop",
                "box": False,
            }
        )
        overlays.append(
            {
                "id": f"planned-emphasis-{index:04d}",
                "type": "emphasis",
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "emphasis": [],
                "visible": True,
                "locked": False,
                "z_index": 35,
                "style": style,
                "source": "working/emphasis_plan.json",
                "provenance": str(
                    item.get("provenance")
                    or "local transcript-derived proposal; requires transcript review"
                ),
            }
        )

    visual_plan = read_json(project_dir / "working/visual_plan.json", {"items": []}) or {
        "items": []
    }
    type_map = {"title_card": "title", "data_card": "card", "animation": "animation"}
    for index, item in enumerate(visual_plan.get("items", []), start=1):
        planned_type = str(item.get("type", ""))
        overlay_type = type_map.get(planned_type)
        source = str(item.get("source") or "")
        if planned_type in {"asset", "broll"} and source.startswith("assets/"):
            overlay_type = "video" if Path(source).suffix.lower() in {".mp4", ".mov"} else "image"
        if overlay_type is None:
            continue
        try:
            start = max(0.0, float(item.get("start", 0.0)))
            end = min(duration_s, float(item.get("end", start + 1.5)))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(item.get("text") or item.get("transcript_evidence") or "").strip()
        if overlay_type not in {"image", "video"} and not text:
            continue
        style = dict(caption_style)
        if overlay_type in {"image", "video"}:
            style.update({"width": 34, "x": 50, "y": 46, "animation": "fade"})
        else:
            style.update(
                {
                    "font_size": max(54, int(float(style.get("font_size", 58)))),
                    "x": 50,
                    "y": 39 if overlay_type == "title" else 46,
                    "max_width": 82,
                    "animation": "slide-up" if overlay_type == "title" else "fade",
                    "box": True,
                    "box_color": "#201b17",
                }
            )
        overlay = {
            "id": f"planned-visual-{index:04d}",
            "type": overlay_type,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "emphasis": [],
            "visible": True,
            "locked": False,
            "z_index": 30,
            "style": style,
            "source": source if overlay_type in {"image", "video"} else "working/visual_plan.json",
            "provenance": str(
                item.get("provenance")
                or "local transcript-derived proposal; requires transcript review"
            ),
        }
        overlays.append(overlay)
    return overlays


def extract_effect_keywords(values: list[Any]) -> list[str]:
    """Extract reusable exact-match terms from reviewed highlight/card copy."""
    keywords: set[str] = set()
    for value in values:
        text = str(value or "")
        for match in re.finditer(r"(?<![A-Za-z])(?:to\s+[A-Za-z]|[A-Z][A-Za-z]+)(?![A-Za-z])", text):
            keywords.add(match.group(0))
        for chunk in re.findall(r"[\u3400-\u9fff]{3,}", text):
            for size in range(min(6, len(chunk)), 2, -1):
                for start in range(0, len(chunk) - size + 1):
                    phrase = chunk[start : start + size]
                    if phrase in {"什麼意思", "最重要", "這樣記", "直接來看", "看到想到"}:
                        continue
                    keywords.add(phrase)
    return sorted(
        keywords,
        key=lambda item: (0 if re.search(r"[A-Za-z]", item) else 1, -len(item), item.casefold()),
    )[:500]


def effect_keywords_for_state(state: dict[str, Any]) -> list[str]:
    active_id = str(state.get("active_highlight_id") or "")
    sources: list[Any] = [
        item.get("title")
        for item in state.get("highlights", [])
        if isinstance(item, dict)
        and item.get("review_status") != "rejected"
        and (not active_id or str(item.get("id") or "") == active_id)
    ]
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict) or not overlay.get("design_role"):
            continue
        if active_id and str(overlay.get("highlight_id") or "") != active_id:
            continue
        sources.extend((overlay.get("text"), overlay.get("kicker"), overlay.get("detail")))
    return extract_effect_keywords(sources)


def effect_keywords_for_caption(state: dict[str, Any], start: float, end: float) -> list[str]:
    matching = [
        item
        for item in state.get("highlights", [])
        if isinstance(item, dict)
        and item.get("review_status") != "rejected"
        and float(item.get("end", 0.0)) > start
        and float(item.get("start", 0.0)) < end
    ]
    highlight_ids = {str(item.get("id") or "") for item in matching}
    sources: list[Any] = [item.get("title") for item in matching]
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict) or not overlay.get("design_role"):
            continue
        if str(overlay.get("highlight_id") or "") not in highlight_ids:
            continue
        sources.extend((overlay.get("text"), overlay.get("kicker"), overlay.get("detail")))
    return extract_effect_keywords(sources)


def caption_effect_spans(
    emphasis_plan: dict[str, Any],
    text: str,
    start: float,
    end: float,
    color: str,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Map transcript-grounded emphasis proposals onto exact caption character ranges."""
    spans: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for index, item in enumerate(emphasis_plan.get("items", [])[:200], start=1):
        if not isinstance(item, dict):
            continue
        try:
            item_start = float(item.get("start", 0.0))
            item_end = float(item.get("end", item_start))
        except (TypeError, ValueError):
            continue
        phrase = str(item.get("text") or "").strip()
        if not phrase or item_end <= start or item_start >= end:
            continue
        offset = text.find(phrase)
        if offset < 0:
            continue
        span_end = offset + len(phrase)
        if any(offset < used_end and span_end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((offset, span_end))
        spans.append(
            {
                "id": f"planned-fx-{index:04d}",
                "text": phrase,
                "start_char": caption_engine.utf16_length(text[:offset]),
                "end_char": caption_engine.utf16_length(text[:span_end]),
                "style": {
                    "effect": "pop",
                    "color": color,
                    "font_scale": 1.18,
                },
                "source": "working/emphasis_plan.json",
            }
        )
    for keyword_index, phrase in enumerate(keywords or [], start=1):
        if len(spans) >= 2:
            break
        matched_phrase = phrase
        offset = text.find(matched_phrase)
        if offset < 0 and re.fullmatch(r"[\u3400-\u9fff]{3,}", phrase):
            for split in range(1, len(phrase)):
                variant = phrase[:split] + "的" + phrase[split:]
                offset = text.find(variant)
                if offset >= 0:
                    matched_phrase = variant
                    break
        if offset < 0:
            continue
        span_end = offset + len(matched_phrase)
        if any(offset < used_end and span_end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((offset, span_end))
        effect = "pop" if len(spans) % 2 == 0 else "highlight"
        spans.append(
            {
                "id": f"keyword-fx-{keyword_index:04d}",
                "text": matched_phrase,
                "start_char": caption_engine.utf16_length(text[:offset]),
                "end_char": caption_engine.utf16_length(text[:span_end]),
                "style": {
                    "effect": effect,
                    "color": color,
                    "font_scale": 1.18 if effect == "pop" else 1.08,
                },
                "source": "reviewed highlight/card copy exact-match proposal",
            }
        )
    spans.sort(key=lambda item: (item["start_char"], item["end_char"]))
    for index, span in enumerate(spans):
        effect = "pop" if index % 2 == 0 else "highlight"
        if span.get("source") != "working/emphasis_plan.json":
            span["style"]["effect"] = effect
            span["style"]["font_scale"] = 1.18 if effect == "pop" else 1.08
    return spans


def upgrade_editor_state_layout_effects(project_dir: Path, state: dict[str, Any]) -> bool:
    """Add the v2 editable layout/effect model without overwriting reviewed user choices."""
    changed = False
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json", {"items": []}) or {
        "items": []
    }
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict):
            continue
        role = str(overlay.get("design_role") or "")
        if role in ROLE_LAYOUTS and not isinstance(overlay.get("layout"), dict):
            overlay["layout"] = dict(ROLE_LAYOUTS[role])
            changed = True
        if overlay.get("type") != "caption" or "effect_spans" in overlay:
            continue
        text = str(overlay.get("text") or "")
        style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
        effects = caption_effect_spans(
            emphasis_plan,
            text,
            float(overlay.get("start", 0.0)),
            float(overlay.get("end", 0.0)),
            str(style.get("emphasis_color") or "#ffd447"),
            effect_keywords_for_caption(
                state,
                float(overlay.get("start", 0.0)),
                float(overlay.get("end", 0.0)),
            ),
        )
        overlay["effect_spans"] = effects
        if effects and not overlay.get("emphasis"):
            overlay["emphasis"] = [item["text"] for item in effects]
        changed = True
    return changed


def default_editor_state(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    transcript = read_json(project_dir / "working/transcript_words.json", {}) or {}
    highlight_plan = read_json(project_dir / "working/highlight_plan.json", {}) or {}
    plan_configuration = (
        highlight_plan.get("configuration")
        if isinstance(highlight_plan.get("configuration"), dict)
        else {}
    )
    director_id = str(plan_configuration.get("director_profile", "teacher-punch"))
    if director_id not in DIRECTOR_PRESETS:
        director_id = "teacher-punch"
    director = DIRECTOR_PRESETS[director_id]
    caption_style = dict(director["caption"])
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json", {"items": []}) or {
        "items": []
    }
    highlights = [
        {
            "id": str(item.get("id", "")),
            "plan_item_id": str(item.get("id", "")),
            "start": item.get("start"),
            "end": item.get("end"),
            "title": str(item.get("title", "")),
            "review_status": str(item.get("review_status", "pending")),
            "score": item.get("score"),
            "source": "working/highlight_plan.json",
        }
        for item in highlight_plan.get("items", [])[:10]
        if isinstance(item, dict)
    ]
    keyword_state = {"highlights": highlights, "overlays": []}
    overlays: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        segment_start = round(float(segment.get("start", 0.0)), 3)
        segment_end = round(float(segment.get("end", 0.0)), 3)
        effects = caption_effect_spans(
            emphasis_plan,
            text,
            segment_start,
            segment_end,
            str(caption_style.get("emphasis_color") or "#ffd447"),
            effect_keywords_for_caption(keyword_state, segment_start, segment_end),
        )
        overlays.append(
            {
                "id": f"caption-{index:04d}",
                "type": "caption",
                "start": segment_start,
                "end": segment_end,
                "text": text,
                "emphasis": [item["text"] for item in effects],
                "effect_spans": effects,
                "visible": True,
                "locked": False,
                "z_index": 20,
                "style": dict(caption_style),
                "source": "working/transcript_words.json",
                "provenance": "local-whisper draft; requires transcript review",
            }
        )
    design_overlays: list[dict[str, Any]] = []
    if highlights:
        for highlight in highlights:
            design_overlays.extend(
                build_highlight_design_overlays(
                    transcript,
                    highlight,
                    caption_style,
                    director_id,
                )
            )
        overlays.extend(design_overlays)
        atomic_write_json(
            project_dir / "working/highlight_visual_plan.json",
            {
                "schema_version": 1,
                "generator": "highlight-scoped-designed-cards-v1",
                "highlight_plan_revision": highlight_plan.get("plan_revision"),
                "items": design_overlays,
            },
        )
    else:
        overlays.extend(
            artifact_plan_overlays(
                project_dir,
                caption_style,
                float(manifest.get("source", {}).get("duration_s", 0.0)),
            )
        )
    state = {
        "schema_version": 2,
        "updated_at": now_utc(),
        "project_id": manifest.get("project_id"),
        "source_sha256": manifest.get("source", {}).get("sha256"),
        "segments": default_source_segments(manifest),
        "variants": [],
        "rights": {"asserted": False, "assertion_revision": None},
        "highlight_plan_revision": highlight_plan.get("plan_revision"),
        "active_highlight_id": highlights[0]["id"] if highlights else None,
        "highlights": highlights,
        "asset_digests": {},
        "canvas": {
            "platform_id": "instagram-reels",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "fit": "contain",
            "show_safe_zones": True,
        },
        "director_style": director_id,
        "visual_quality_mode": "designed",
        "graphic_package_style": "craft-stack",
        "video_template": default_video_template_state(),
        "editing_brief": str(plan_configuration.get("editing_brief", ""))[:2000],
        "caption_defaults": caption_style,
        "overlays": overlays,
        "publishing": {
            "platform_id": "instagram-reels",
            "draft_status": "not_generated",
            "title": "",
            "body": "",
            "hashtags": [],
            "cover": {
                "time": min(1.0, float(manifest.get("source", {}).get("duration_s", 0.0))),
                "text": "",
                "output": None,
            },
        },
        "review": {
            "selected_overlay_id": overlays[0]["id"] if overlays else None,
            "warnings_acknowledged": [],
        },
    }
    state["asset_digests"] = referenced_asset_digests(project_dir, state)
    state["revision"] = editor_state_revision(state)
    atomic_write_json(project_dir / STATE_REL, state)
    return state


def validate_editor_state(state: Any, duration_s: float) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["editor state must be an object"]
    if state.get("schema_version") == 1:
        return [
            "editor state schema_version 1 must be migrated by the server first; "
            "reload the editor page"
        ]
    if state.get("schema_version") != EDITOR_STATE_SCHEMA_VERSION:
        return [f"editor state schema_version must be {EDITOR_STATE_SCHEMA_VERSION}"]
    segments = state.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("segments must be a non-empty list (unified timeline contract)")
    else:
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                errors.append(f"segment {index} must be an object")
                continue
            start = segment.get("source_start")
            end = segment.get("source_end")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or float(start) < 0
                or float(end) <= float(start)
            ):
                errors.append(f"segment {index} needs 0 <= source_start < source_end")
            if segment.get("origin") not in {
                "narrative",
                "manual",
                "legacy_import",
                "default_full_source",
            }:
                errors.append(f"segment {index} origin is not supported")
    if not isinstance(state.get("variants"), list):
        errors.append("variants must be a list")
    rights = state.get("rights")
    if not isinstance(rights, dict) or not isinstance(rights.get("asserted"), bool):
        errors.append("rights must be an object with a boolean asserted flag")
    canvas = state.get("canvas")
    if not isinstance(canvas, dict):
        errors.append("canvas must be an object")
    else:
        platform = canvas.get("platform_id")
        if platform not in PLATFORM_PRESETS:
            errors.append("canvas platform_id is not supported")
        for key in ("width", "height"):
            value = canvas.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 240 <= value <= 4096:
                errors.append(f"canvas {key} must be an integer between 240 and 4096")
        fps = canvas.get("fps")
        if (
            isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps))
            or not 1 <= float(fps) <= 240
        ):
            errors.append("canvas fps must be finite and between 1 and 240")
        if canvas.get("fit") not in {"cover", "contain"}:
            errors.append("canvas fit must be cover or contain")
    if state.get("director_style") not in DIRECTOR_PRESETS:
        errors.append("director_style is not supported")
    if state.get("video_template") is not None:
        errors.extend(validate_video_template_state(state.get("video_template")))
    if state.get("visual_quality_mode", "basic") not in {"basic", "designed"}:
        errors.append("visual_quality_mode must be basic or designed")
    editing_brief = state.get("editing_brief", "")
    if not isinstance(editing_brief, str) or len(editing_brief) > 2000:
        errors.append("editing_brief must be a string of at most 2000 characters")
    overlays = state.get("overlays")
    if not isinstance(overlays, list):
        errors.append("overlays must be an array")
        return errors
    if len(overlays) > 1000:
        errors.append("overlays cannot exceed 1000 items")
    seen: set[str] = set()
    overlay_highlight_refs: list[tuple[str, str]] = []
    allowed_types = {"caption", "emphasis", "title", "card", "image", "gif", "video", "animation"}
    for index, overlay in enumerate(overlays):
        if not isinstance(overlay, dict):
            errors.append(f"overlay {index} must be an object")
            continue
        overlay_id = str(overlay.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", overlay_id) or overlay_id in seen:
            errors.append(f"overlay {index} has an invalid or duplicate id")
        seen.add(overlay_id)
        if overlay.get("type") not in allowed_types:
            errors.append(f"overlay {overlay_id or index} has an unsupported type")
        try:
            start = float(overlay.get("start"))
            end = float(overlay.get("end"))
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or end > duration_s + 0.05
            ):
                errors.append(f"overlay {overlay_id or index} has invalid timing")
        except (TypeError, ValueError):
            errors.append(f"overlay {overlay_id or index} timing must be numeric")
        overlay_text = str(overlay.get("text", ""))
        if len(overlay_text) > 1000:
            errors.append(f"overlay {overlay_id or index} text is too long")
        if len(str(overlay.get("kicker", ""))) > 120 or len(str(overlay.get("detail", ""))) > 1000:
            errors.append(f"overlay {overlay_id or index} design copy is too long")
        scoped_highlight = overlay.get("highlight_id")
        if scoped_highlight is not None:
            scoped_highlight = str(scoped_highlight)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scoped_highlight):
                errors.append(f"overlay {overlay_id or index} highlight_id is invalid")
            else:
                overlay_highlight_refs.append((overlay_id or str(index), scoped_highlight))
        design_role = overlay.get("design_role")
        if design_role is not None and design_role not in {"hook", "concept", "rule", "memory", "recap"}:
            errors.append(f"overlay {overlay_id or index} design_role is invalid")
        layout = overlay.get("layout")
        if layout is not None:
            if not isinstance(layout, dict):
                errors.append(f"overlay {overlay_id or index} layout must be an object")
            else:
                for key in ("x", "y", "width", "height"):
                    value = layout.get(key)
                    minimum = 0 if key in {"x", "y"} else 1
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or not minimum <= float(value) <= 100
                    ):
                        errors.append(f"overlay {overlay_id or index} layout {key} is invalid")
        effect_spans = overlay.get("effect_spans", [])
        if not isinstance(effect_spans, list):
            errors.append(f"overlay {overlay_id or index} effect_spans must be an array")
        else:
            if len(effect_spans) > 50:
                errors.append(f"overlay {overlay_id or index} cannot exceed 50 effect spans")
            occupied: list[tuple[int, int]] = []
            for span_index, span in enumerate(effect_spans):
                if not isinstance(span, dict):
                    errors.append(f"overlay {overlay_id or index} effect span {span_index} must be an object")
                    continue
                span_id = str(span.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", span_id):
                    errors.append(f"overlay {overlay_id or index} effect span {span_index} id is invalid")
                start_char = span.get("start_char")
                end_char = span.get("end_char")
                if (
                    isinstance(start_char, bool)
                    or isinstance(end_char, bool)
                    or not isinstance(start_char, int)
                    or not isinstance(end_char, int)
                    or start_char < 0
                    or end_char <= start_char
                    or end_char > caption_engine.utf16_length(overlay_text)
                ):
                    errors.append(f"overlay {overlay_id or index} effect span {span_index} range is invalid")
                    continue
                # Contract offsets are UTF-16 code units (browser selection
                # semantics) — never Python code points.
                try:
                    span_text = caption_engine.slice_utf16(overlay_text, start_char, end_char)
                except ValueError:
                    errors.append(
                        f"overlay {overlay_id or index} effect span {span_index} "
                        "splits a surrogate pair"
                    )
                    continue
                if span_text != str(span.get("text") or ""):
                    errors.append(f"overlay {overlay_id or index} effect span text does not match its range")
                if caption_engine.available() and not caption_engine.span_on_boundaries(
                    overlay_text, start_char, end_char
                ):
                    errors.append(
                        f"overlay {overlay_id or index} effect span {span_index} does not "
                        "sit on grapheme cluster boundaries; snap it via POST /api/captions/snap"
                    )
                if any(start_char < used_end and end_char > used_start for used_start, used_end in occupied):
                    errors.append(f"overlay {overlay_id or index} effect spans cannot overlap")
                occupied.append((start_char, end_char))
                effect_style = span.get("style") if isinstance(span.get("style"), dict) else {}
                if effect_style.get("effect") not in {"pop", "highlight", "underline"}:
                    errors.append(f"overlay {overlay_id or index} effect span effect is invalid")
                effect_color = str(effect_style.get("color") or "")
                if not re.fullmatch(r"#[0-9A-Fa-f]{6}", effect_color):
                    errors.append(f"overlay {overlay_id or index} effect span color is invalid")
                effect_scale = effect_style.get("font_scale")
                if (
                    isinstance(effect_scale, bool)
                    or not isinstance(effect_scale, (int, float))
                    or not math.isfinite(float(effect_scale))
                    or not 0.5 <= float(effect_scale) <= 3.0
                ):
                    errors.append(f"overlay {overlay_id or index} effect span font_scale is invalid")
        z_index = overlay.get("z_index", 0)
        if (
            isinstance(z_index, bool)
            or not isinstance(z_index, (int, float))
            or not math.isfinite(float(z_index))
            or not -1000 <= float(z_index) <= 1000
        ):
            errors.append(f"overlay {overlay_id or index} z_index is invalid")
        style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
        style_bounds = {
            "font_size": (8, 500),
            "stroke_width": (0, 50),
            "x": (-100, 200),
            "y": (-100, 200),
            "max_width": (1, 200),
            "width": (1, 200),
            "opacity": (0, 1),
        }
        for key, (minimum, maximum) in style_bounds.items():
            if key not in style:
                continue
            value = style[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                errors.append(f"overlay {overlay_id or index} style {key} is invalid")
        source = overlay.get("source")
        if source and overlay.get("type") in {"image", "gif", "video"}:
            try:
                scoped_project_path(
                    Path(state.get("project_dir", "/")),
                    str(source),
                    "assets",
                )
            except ValueError:
                errors.append(f"overlay {overlay_id or index} source must be under assets/")
    source_sha256 = state.get("source_sha256")
    if source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(source_sha256)):
        errors.append("source_sha256 must be a lowercase SHA-256 digest")
    plan_revision = state.get("highlight_plan_revision")
    if plan_revision is not None and not re.fullmatch(r"[0-9a-f]{64}", str(plan_revision)):
        errors.append("highlight_plan_revision must be a lowercase SHA-256 digest")
    highlights = state.get("highlights", [])
    highlight_ids: set[str] = set()
    if not isinstance(highlights, list):
        errors.append("highlights must be an array")
    else:
        if len(highlights) > 10:
            errors.append("highlights cannot exceed 10 items")
        for index, highlight in enumerate(highlights):
            if not isinstance(highlight, dict):
                errors.append(f"highlight {index} must be an object")
                continue
            highlight_id = str(highlight.get("id", ""))
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", highlight_id)
                or highlight_id in highlight_ids
            ):
                errors.append(f"highlight {index} has an invalid or duplicate id")
            highlight_ids.add(highlight_id)
            try:
                start = float(highlight.get("start"))
                end = float(highlight.get("end"))
            except (TypeError, ValueError):
                errors.append(f"highlight {highlight_id or index} timing must be numeric")
                continue
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or end > duration_s + 0.05
            ):
                errors.append(f"highlight {highlight_id or index} timing is invalid")
            if highlight.get("review_status") not in {"pending", "approved", "rejected"}:
                errors.append(f"highlight {highlight_id or index} review status is invalid")
            if len(str(highlight.get("title", ""))) > 200:
                errors.append(f"highlight {highlight_id or index} title is too long")
    active_highlight_id = state.get("active_highlight_id")
    if active_highlight_id is not None and str(active_highlight_id) not in highlight_ids:
        errors.append("active_highlight_id must reference a highlight in state")
    for overlay_id, highlight_id in overlay_highlight_refs:
        if highlight_id not in highlight_ids:
            errors.append(f"overlay {overlay_id} highlight_id must reference a highlight in state")
    return errors


def transcript_text(project_dir: Path) -> str:
    transcript = read_json(project_dir / "working/transcript_words.json", {}) or {}
    return str(transcript.get("text", "")).strip()


def copy_draft(platform_id: str, text: str) -> dict[str, Any]:
    clean = re.sub(r"\s+", "", text)
    if not clean:
        clean = "這支影片的重點整理"
    title_limits = {
        "instagram-reels": 36,
        "youtube-shorts": 70,
        "youtube-landscape": 70,
        "tiktok": 32,
        "xiaohongshu-portrait": 20,
        "xiaohongshu-full": 20,
    }
    limit = title_limits.get(platform_id, 36)
    title = clean[:limit] + ("…" if len(clean) > limit else "")
    tag_map = {
        "instagram-reels": ["知識短影音", "重點整理", "Reels"],
        "youtube-shorts": ["Shorts", "知識", "重點整理"],
        "youtube-landscape": ["YouTube", "完整解析", "知識"],
        "tiktok": ["TikTok知識", "你知道嗎", "重點"],
        "xiaohongshu-portrait": ["知識分享", "乾貨", "小紅書影片"],
        "xiaohongshu-full": ["知識分享", "乾貨", "小紅書影片"],
    }
    body = f"{title}\n\n影片重點已整理在畫面中。看完後，你最想延伸哪一點？"
    return {
        "title": title,
        "body": body,
        "hashtags": tag_map.get(platform_id, ["短影音", "重點整理"]),
        "draft_status": "local_draft_requires_review",
        "generator": "deterministic-transcript-draft-v1",
    }


def load_voice_catalog() -> dict[str, Any]:
    command = [sys.executable, str(SKILL_DIR / "scripts/auto_edit.py"), "voices"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        return {
            "defaults": {},
            "voices": [],
            "warning": (result.stderr or result.stdout or "voice catalog unavailable")[-500:],
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"defaults": {}, "voices": [], "warning": "voice catalog returned invalid JSON"}
    payload["cloud_consent_required"] = True
    payload["selection_only"] = True
    return payload


def voice_language_matches(entry_language: str, selected_language: str) -> bool:
    family = "zh" if selected_language.startswith("zh") else "en"
    entry = entry_language.lower()
    if entry == family:
        return True
    return entry == selected_language.lower()


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], project_dir: Path):
        super().__init__(address, EditorHandler)
        self.project_dir = project_dir.resolve()
        # Per-server CSRF token; browsers learn it from GET /api/project and
        # must echo it on every mutation. This blocks cross-origin browser
        # requests only — local processes reading loopback are outside this
        # threat model (contracts/policies/DOWNLOAD_GATE.md).
        self.csrf_token = secrets.token_urlsafe(32)
        self.caption_job_lock = threading.Lock()
        self.caption_job: dict[str, Any] = {
            "state": "idle",
            "caption_revision": None,
            "error": None,
            "sequence": 0,
        }
        self.voice_catalog = load_voice_catalog()
        self.project_lock = threading.RLock()
        self.render_lock = threading.Lock()
        self.render_status: dict[str, Any] = {
            "state": "idle",
            "message": "尚未輸出預覽",
            "output": None,
        }

    def schedule_caption_render(self) -> None:
        """Latest-wins background caption rendering after a state save."""
        import caption_compositor

        if not caption_compositor.compositor_available():
            with self.caption_job_lock:
                self.caption_job.update({"state": "unavailable", "error": None})
            return
        with self.caption_job_lock:
            self.caption_job["sequence"] += 1
            sequence = self.caption_job["sequence"]
            self.caption_job.update({"state": "rendering", "error": None})
        threading.Thread(
            target=self._caption_render_worker, args=(sequence,), daemon=True
        ).start()

    def _caption_render_worker(self, sequence: int) -> None:
        import caption_compositor

        try:
            with self.project_lock:
                state = read_json(self.project_dir / STATE_REL, {}) or {}
            plan = caption_compositor.build_render_plan(self.project_dir, state, 1.0)
            with self.caption_job_lock:
                if self.caption_job["sequence"] != sequence:
                    return  # a newer save superseded this run
                self.caption_job.update(
                    {
                        "state": "ready",
                        "caption_revision": plan.get("caption_revision"),
                        "error": None,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - reported through status
            with self.caption_job_lock:
                if self.caption_job["sequence"] != sequence:
                    return
                self.caption_job.update({"state": "failed", "error": str(exc)[:300]})


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[editor] " + (fmt % args) + "\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self, "_cache_immutable", False):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_host_allowed(self) -> bool:
        """Reject DNS-rebinding style Host headers on a loopback server."""
        return host_header_allowed(
            str(self.server.server_address[0]),
            self.headers.get("Host", ""),
        )

    def mutation_origin_allowed(self) -> bool:
        """Allow CLI calls without Origin and same-origin browser writes only."""
        return mutation_origin_allowed(self.headers)

    def allow_request(self, mutation: bool = False) -> bool:
        if not self.request_host_allowed():
            self.close_connection = True
            self.send_json({"ok": False, "error": "invalid Host for local editor"}, status=403)
            return False
        if mutation and not self.mutation_origin_allowed():
            self.close_connection = True
            self.send_json({"ok": False, "error": "cross-origin writes are not allowed"}, status=403)
            return False
        if mutation and not csrf_token_matches(self.headers, self.server.csrf_token):
            self.close_connection = True
            self.send_json({"ok": False, "error": "missing or invalid CSRF token"}, status=403)
            return False
        return True

    def read_body(self, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > maximum:
            raise ValueError(f"request body exceeds {maximum} bytes")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("request body was truncated")
        return body

    def handle_caption_status(self, project: Path) -> None:
        import caption_compositor

        engine = caption_compositor.engine_descriptor()
        with self.server.caption_job_lock:
            job = dict(self.server.caption_job)
        plan = read_json(project / "working/caption_render_plan.json", None)
        items: list[dict[str, Any]] = []
        ready = False
        if engine["status"] == "present":
            with self.server.project_lock:
                state = read_json(project / STATE_REL, {}) or {}
            expected = caption_compositor.caption_content_revision(
                state, state.get("canvas") or {}, 1.0
            )
            if isinstance(plan, dict) and plan.get("caption_revision") == expected:
                ready = True
                for item in plan.get("items", []):
                    artifact = item.get("artifact", {})
                    items.append(
                        {
                            "id": item.get("caption_item_id"),
                            "artifact_hash": artifact.get("artifact_hash"),
                            "url": (
                                f"/captions/{item.get('caption_item_id')}/"
                                f"{artifact.get('artifact_hash')}.png"
                            ),
                            "width": artifact.get("width"),
                            "height": artifact.get("height"),
                        }
                    )
        self.send_json(
            {"ok": True, "engine": engine, "job": job, "ready": ready, "items": items}
        )

    def handle_font_info(self, project: Path) -> None:
        import caption_compositor

        plan = read_json(project / "working/caption_render_plan.json", None)
        receipt = plan.get("receipt", {}) if isinstance(plan, dict) else {}
        try:
            from render_editor_timeline import font_path

            resolved = str(font_path())
        except ValueError:
            resolved = ""
        self.send_json(
            {
                "ok": True,
                "engine": caption_compositor.engine_descriptor(),
                "project_font": resolved,
                "sanctioned_fallbacks": sorted(
                    caption_compositor.SANCTIONED_FALLBACK_PS_NAMES
                ),
                "receipt_fonts": receipt.get("fonts", {}),
                "disallowed_fallbacks": receipt.get("disallowed_fallbacks", []),
            }
        )

    def handle_caption_png(self, project: Path, item_id: str, artifact_hash: str) -> None:
        plan = read_json(project / "working/caption_render_plan.json", None)
        entry = None
        if isinstance(plan, dict):
            for item in plan.get("items", []):
                if (
                    item.get("caption_item_id") == item_id
                    and item.get("artifact", {}).get("artifact_hash") == artifact_hash
                ):
                    entry = item
                    break
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            artifact = scoped_project_path(
                project, str(entry["artifact"]["rgba_path"]), "working/captions"
            )
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        # Immutable content-addressed URL: safe to cache forever.
        self._cache_immutable = True
        try:
            self.serve_file(artifact)
        finally:
            self._cache_immutable = False

    CAPTION_STYLE_KEYS = {
        "color", "font_size", "stroke_color", "stroke_width", "box", "box_color",
        "max_width", "animation", "font_weight", "font_family", "emphasis_color",
    }

    def handle_caption_apply_style(self) -> None:
        """Server-side style application across a scope (single/selection/track).

        Scope semantics are enforced here, not by front-end batch edits:
        only whitelisted style keys move, timing and unlisted fields never.
        """
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        scope = str(body.get("scope") or "")
        if scope not in {"single", "selection", "track"}:
            self.send_json({"ok": False, "error": "scope must be single/selection/track"}, status=422)
            return
        overlay_id = str(body.get("overlay_id") or "")
        style_patch = {
            key: value
            for key, value in (body.get("style") or {}).items()
            if key in self.CAPTION_STYLE_KEYS
        }
        if not style_patch:
            self.send_json({"ok": False, "error": "no applicable style keys"}, status=422)
            return
        with self.server.project_lock:
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
        plain = [
            overlay
            for overlay in state.get("overlays", [])
            if isinstance(overlay, dict)
            and overlay.get("type") in {"caption", "emphasis"}
            and not overlay.get("design_role")
        ]
        target = next((o for o in plain if str(o.get("id")) == overlay_id), None)
        if scope != "track" and target is None:
            self.send_json({"ok": False, "error": "overlay not found"}, status=404)
            return
        if scope == "single":
            recipients = [target]
        elif scope == "selection":
            highlight = target.get("highlight_id")
            recipients = [o for o in plain if o.get("highlight_id") == highlight]
        else:
            recipients = plain
        for overlay in recipients:
            overlay_style = overlay.get("style")
            if not isinstance(overlay_style, dict):
                overlay_style = {}
                overlay["style"] = overlay_style
            overlay_style.update(style_patch)
        errors, response = self.persist_editor_state(state)
        if errors:
            self.send_json({"ok": False, "errors": errors}, status=422)
            return
        self.server.schedule_caption_render()
        response["state"] = state
        response["applied_to"] = [str(o.get("id")) for o in recipients]
        self.send_json(response)

    def handle_caption_snap(self) -> None:
        """Server-authoritative cluster snapping for browser selections."""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not caption_engine.available():
            self.send_json(
                {"ok": False, "error": "caption engine unavailable on this host"},
                status=503,
            )
            return
        text = str(body.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        try:
            start = int(body.get("start_char"))
            end = int(body.get("end_char"))
        except (TypeError, ValueError):
            self.send_json(
                {"ok": False, "error": "start_char/end_char must be integers"}, status=422
            )
            return
        snapped = caption_engine.snap_span(text, start, end)
        if snapped is None:
            self.send_json({"ok": True, "removed": True})
            return
        snapped_start, snapped_end = snapped
        self.send_json(
            {
                "ok": True,
                "removed": False,
                "start_char": snapped_start,
                "end_char": snapped_end,
                "text": caption_engine.slice_utf16(text, snapped_start, snapped_end),
            }
        )

    def read_json_body(self) -> Any:
        raw = self.read_body(MAX_JSON_BYTES)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc

    def serve_file(self, path: Path, allow_range: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                suffix = int(last)
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        if allow_range:
            self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.allow_request():
            return
        path, _query = self.route()
        project = self.server.project_dir
        if path == "/api/health":
            self.send_json({"ok": True, "project": str(project)})
            return
        if path == "/api/project":
            manifest = read_json(project / "project.json", {}) or {}
            state = read_json(project / STATE_REL)
            if state is None:
                state = default_editor_state(project, manifest)
            else:
                state, _migrated = migrate_editor_state_v1_to_v2(project, manifest, state)
                span_warnings = migrate_caption_spans(state)
                if span_warnings:
                    state["updated_at"] = now_utc()
                    state["revision"] = editor_state_revision(state)
                    atomic_write_json(project / STATE_REL, state)
                upgraded = upgrade_editor_state_layout_effects(project, state)
                upgraded = upgrade_video_template_state(state) or upgraded
                state["asset_digests"] = referenced_asset_digests(project, state)
                revision = editor_state_revision(state)
                if upgraded or state.get("revision") != revision:
                    state["revision"] = revision
                    atomic_write_json(project / STATE_REL, state)
            source_rel = str(manifest.get("source", {}).get("staged_path", ""))
            payload = {
                "csrf_token": self.server.csrf_token,
                "caption_span_migration": locals().get("span_warnings") or [],
                "caption_engine": caption_engine.engine_descriptor(),
                "manifest": manifest,
                "state": state,
                "platform_presets": PLATFORM_PRESETS,
                "director_presets": DIRECTOR_PRESETS,
                "viral_structure_plan": read_json(
                    project / "working/viral_structure_plan.json", None
                ),
                "narrative_plan": read_json(
                    project / "working/narrative_edit_plan.json", None
                ),
                "video_templates": public_template_catalog(),
                "template_capabilities": {
                    "cutout": {
                        key: value
                        for key, value in cutout_capability().items()
                        if key not in {"python", "model_path"}
                    }
                },
                "voice_catalog": self.server.voice_catalog,
                "edit_candidates": read_json(project / "working/edit_candidates.json", {"items": []}),
                "edit_decisions": read_json(project / "working/edit_decisions.json", {"items": []}),
                "transcript_review": read_json(
                    project / "working/transcript_review.json",
                    {
                        "status": "needs_review",
                        "risk_status": "semantic_review_required",
                        "mechanical_issue_count": 0,
                        "semantic_calibration": {"status": "not_configured"},
                    },
                ),
                "transcript_calibration": read_json(
                    project / "working/transcript_calibration.json",
                    {
                        "status": "not_configured",
                        "rule_count": 0,
                        "correction_count": 0,
                        "human_review_required": True,
                    },
                ),
                "transcript_semantic_review": read_json(
                    project / "working/transcript_semantic_review.json",
                    {
                        "status": "not_configured",
                        "coverage_status": "not_started",
                        "reviewed_unit_count": 0,
                        "total_unit_count": 0,
                        "accepted_count": 0,
                        "pending_count": 0,
                        "applied_correction_count": 0,
                        "human_review_required": True,
                    },
                ),
                "highlight_plan": read_json(project / "working/highlight_plan.json", {}),
                "pipeline_status": read_json(
                    project / "working/pipeline_status.json",
                    {
                        "state": "not_started",
                        "phase": "idle",
                        "message": "這個專案尚未啟動本機自動處理。",
                    },
                ),
                "approval_revisions": approval_revisions(project, state),
                "approval_current": {
                    gate: approval_is_current(project, manifest, gate, state)
                    for gate in sorted(GATES)
                },
                "qa": read_json(project / "qa/source-qa.json", {}),
                "delivery_qa": read_json(project / LATEST_DELIVERY_QA_REL, {}),
                "media_url": "/media/source" if source_rel else None,
                "render_status": self.server.render_status,
            }
            self.send_json(payload)
            return
        if path == "/api/render-status":
            self.send_json(self.server.render_status)
            return
        if path == "/api/approval-revisions":
            state = read_json(project / STATE_REL, {}) or {}
            manifest = read_json(project / "project.json", {}) or {}
            self.send_json(
                {
                    "ok": True,
                    "revisions": approval_revisions(project, state),
                    "current": {
                        gate: approval_is_current(project, manifest, gate, state)
                        for gate in sorted(GATES)
                    },
                }
            )
            return
        if path == "/api/pipeline-status":
            pipeline_status = read_json(
                project / "working/pipeline_status.json",
                {
                    "state": "not_started",
                    "phase": "idle",
                    "message": "這個專案尚未啟動本機自動處理。",
                },
            )
            if (
                pipeline_status.get("state") == "running"
                and pipeline_status.get("phase") == "semantic_calibration"
            ):
                semantic_progress = read_json(
                    project / "working/transcript_semantic_review.json",
                    {},
                )
                reviewed_raw = semantic_progress.get("reviewed_unit_count", 0)
                total_raw = semantic_progress.get("total_unit_count", 0)
                reviewed = (
                    int(reviewed_raw)
                    if isinstance(reviewed_raw, (int, float))
                    and not isinstance(reviewed_raw, bool)
                    and reviewed_raw >= 0
                    else 0
                )
                total = (
                    int(total_raw)
                    if isinstance(total_raw, (int, float))
                    and not isinstance(total_raw, bool)
                    and total_raw >= 0
                    else 0
                )
                if total > 0:
                    candidate_raw = semantic_progress.get("candidate_count", 0)
                    error_raw = semantic_progress.get("model_error_count", 0)
                    pipeline_status["semantic_progress"] = {
                        "reviewed_unit_count": reviewed,
                        "total_unit_count": total,
                        "candidate_count": (
                            int(candidate_raw)
                            if isinstance(candidate_raw, (int, float))
                            and not isinstance(candidate_raw, bool)
                            and candidate_raw >= 0
                            else 0
                        ),
                        "model_error_count": (
                            int(error_raw)
                            if isinstance(error_raw, (int, float))
                            and not isinstance(error_raw, bool)
                            and error_raw >= 0
                            else 0
                        ),
                    }
                    pipeline_status["message"] = (
                        f"正在用整份上下文逐句校準字幕… {reviewed}/{total}"
                    )
            self.send_json(pipeline_status)
            return
        if path == "/media/source":
            manifest = read_json(project / "project.json", {}) or {}
            source_rel = str(manifest.get("source", {}).get("staged_path", ""))
            try:
                source = project_entry_path(project, source_rel)
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(source, allow_range=True)
            return
        if path.startswith("/assets/"):
            relative = urllib.parse.unquote(path.removeprefix("/"))
            try:
                asset = scoped_project_path(project, relative, "assets")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(asset, allow_range=asset.suffix.lower() in {".mp4", ".mov"})
            return
        if path == "/api/captions/status":
            self.handle_caption_status(project)
            return
        if path == "/api/fonts":
            self.handle_font_info(project)
            return
        caption_match = re.fullmatch(
            r"/captions/([A-Za-z0-9_-]{1,80})/([0-9a-f]{64})\.png", path
        )
        if caption_match:
            self.handle_caption_png(project, caption_match.group(1), caption_match.group(2))
            return
        if path.startswith("/renders/"):
            relative = urllib.parse.unquote(path.removeprefix("/"))
            try:
                render = scoped_project_path(project, relative, "renders")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            gate_errors = render_download_errors(project, relative)
            if gate_errors:
                self.send_json({"ok": False, "error": gate_errors[0]}, status=403)
                return
            self.serve_file(render, allow_range=render.suffix.lower() == ".mp4")
            return
        if path.startswith("/qa/"):
            relative = urllib.parse.unquote(path.removeprefix("/"))
            try:
                artifact = scoped_project_path(project, relative, "qa")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if artifact.suffix.lower() not in {".json", ".png", ".jpg", ".jpeg", ".webp"}:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            gate_errors = qa_download_errors(project, relative)
            if gate_errors:
                self.send_json({"ok": False, "error": gate_errors[0]}, status=403)
                return
            self.serve_file(artifact)
            return
        static_name = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/")
        if "/" in static_name or static_name.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.serve_file(EDITOR_DIR / static_name)

    def do_PUT(self) -> None:
        if not self.allow_request(mutation=True):
            return
        path, _query = self.route()
        if path == "/api/edit-decisions":
            self.handle_edit_decisions()
            return
        if path == "/api/voice-selection":
            self.handle_voice_selection()
            return
        if path != "/api/editor-state":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            state = self.read_json_body()
            errors, response = self.persist_editor_state(state)
            if errors:
                self.send_json({"ok": False, "errors": errors}, status=422)
                return
        except (ValueError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.server.schedule_caption_render()
        self.send_json(response)

    def persist_editor_state(self, state: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        """Shared validated persistence for PUT and server-side style patches."""
        with self.server.project_lock:
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            duration = float(manifest.get("source", {}).get("duration_s", 0.0))
            plan = read_json(self.server.project_dir / "working/highlight_plan.json", {}) or {}
            state["source_sha256"] = manifest.get("source", {}).get("sha256")
            state["highlight_plan_revision"] = plan.get("plan_revision")
            upgrade_video_template_state(state)
            state["project_dir"] = str(self.server.project_dir)
            errors = validate_editor_state(state, duration)
            state.pop("project_dir", None)
            try:
                state["asset_digests"] = referenced_asset_digests(self.server.project_dir, state)
            except ValueError as exc:
                errors.append(str(exc))
            if errors:
                return errors, {}
            state["updated_at"] = now_utc()
            state["revision"] = editor_state_revision(state)
            atomic_write_json(self.server.project_dir / STATE_REL, state)
            current_revisions = approval_revisions(self.server.project_dir, state)
            invalidated_gates: list[str] = []
            approvals = manifest.setdefault("approvals", {})
            for gate in ("highlight_selection", "timeline", "final"):
                approval = approvals.get(gate)
                if not isinstance(approval, dict) or not approval.get("approved"):
                    continue
                if approval.get("state_revision") == current_revisions[gate]:
                    continue
                approvals[gate] = {
                    "approved": False,
                    "confirmed_by": None,
                    "at": None,
                    "note": f"Invalidated because the {gate} revision changed",
                    "invalidated_at": now_utc(),
                }
                invalidated_gates.append(gate)
            if invalidated_gates:
                stages = manifest.setdefault("stages", {})
                if "highlight_selection" in invalidated_gates:
                    stages["highlight_plan"] = "needs_review"
                if "timeline" in invalidated_gates:
                    stages["timeline_review"] = "needs_review"
                if "final" in invalidated_gates:
                    stages["render"] = "pending"
                    stages["qa"] = "pending"
                manifest["updated_at"] = now_utc()
                atomic_write_json(self.server.project_dir / "project.json", manifest)
        return [], {
            "ok": True,
            "updated_at": state["updated_at"],
            "revision": state["revision"],
            "invalidated_gates": invalidated_gates,
            "approval_revisions": current_revisions,
        }

    def handle_voice_selection(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not isinstance(body, dict):
            self.send_json({"ok": False, "error": "voice selection must be an object"}, status=422)
            return
        enabled = bool(body.get("enabled"))
        manifest_path = self.server.project_dir / "project.json"
        manifest = read_json(manifest_path, {}) or {}
        if not enabled:
            config = {
                "enabled": False,
                "mode": "off",
                "engine": None,
                "provider": None,
                "language": None,
                "gender": None,
                "voice_id": None,
                "speed": 1.0,
                "cloud": False,
                "selection_status": "disabled",
            }
        else:
            language = str(body.get("language", ""))
            gender = str(body.get("gender", ""))
            provider = str(body.get("provider", ""))
            voice_id = str(body.get("voice_id", ""))
            mode = str(body.get("mode", "replace"))
            try:
                speed = float(body.get("speed", 1.0))
            except (TypeError, ValueError):
                speed = 0.0
            if language not in VOICE_LANGUAGES or gender not in VOICE_GENDERS:
                self.send_json({"ok": False, "error": "unsupported voice language or gender"}, status=422)
                return
            if mode not in {"replace", "add"} or not 0.7 <= speed <= 1.3:
                self.send_json({"ok": False, "error": "invalid voice mode or speed"}, status=422)
                return
            entry = next(
                (
                    item
                    for item in self.server.voice_catalog.get("voices", [])
                    if str(item.get("voice_id")) == voice_id
                    and str(item.get("provider")) == provider
                    and str(item.get("gender")) == gender
                    and voice_language_matches(str(item.get("language", "")), language)
                ),
                None,
            )
            if entry is None:
                self.send_json({"ok": False, "error": "voice is not in the allowed shared catalog"}, status=422)
                return
            config = {
                "enabled": True,
                "mode": mode,
                "engine": "rumi-voice-system" if provider == "rumi" else "edge",
                "provider": provider,
                "language": language,
                "gender": gender,
                "voice_id": voice_id,
                "speed": round(speed, 2),
                "cloud": True,
                "cloud_consent_required": True,
                "selection_status": "resolved_not_generated",
            }
        manifest["voiceover"] = config
        manifest["updated_at"] = now_utc()
        atomic_write_json(manifest_path, manifest)
        self.send_json(
            {
                "ok": True,
                "voiceover": config,
                "generated": False,
                "message": "Voice selection saved; no cloud synthesis was called",
            }
        )

    def handle_edit_decisions(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            self.send_json({"ok": False, "error": "items must be an array"}, status=422)
            return
        candidates = read_json(
            self.server.project_dir / "working/edit_candidates.json", {"items": []}
        ) or {"items": []}
        allowed = {str(item.get("id")) for item in candidates.get("items", [])}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                self.send_json({"ok": False, "error": "each decision must be an object"}, status=422)
                return
            candidate_id = str(item.get("candidate_id", ""))
            action = str(item.get("action", ""))
            if candidate_id not in allowed or candidate_id in seen or action not in {"delete", "keep"}:
                self.send_json({"ok": False, "error": "invalid or duplicate edit decision"}, status=422)
                return
            seen.add(candidate_id)
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "action": action,
                    "review_status": "approved" if body.get("approved") else "pending",
                }
            )
        payload = {
            "schema_version": 1,
            "approved_from": now_utc() if body.get("approved") else None,
            "items": normalized,
        }
        with self.server.project_lock:
            atomic_write_json(self.server.project_dir / "working/edit_decisions.json", payload)
            manifest_path = self.server.project_dir / "project.json"
            manifest = read_json(manifest_path, {}) or {}
            invalidated: list[str] = []
            approvals = manifest.setdefault("approvals", {})
            for gate in ("destructive_edit", "highlight_selection", "timeline", "final"):
                approval = approvals.get(gate)
                if not isinstance(approval, dict) or not approval.get("approved"):
                    continue
                approvals[gate] = {
                    "approved": False,
                    "confirmed_by": None,
                    "at": None,
                    "note": "Invalidated because edit decisions changed",
                    "invalidated_at": now_utc(),
                }
                invalidated.append(gate)
            stages = manifest.setdefault("stages", {})
            stages["edit_review"] = "needs_review"
            if invalidated:
                stages["highlight_plan"] = "needs_review"
                stages["timeline_review"] = "needs_review"
                stages["render"] = "pending"
                stages["qa"] = "pending"
            manifest["updated_at"] = now_utc()
            atomic_write_json(manifest_path, manifest)
            revision = gate_revision(self.server.project_dir, "destructive_edit")
        self.send_json(
            {
                "ok": True,
                "items": len(normalized),
                "approval_revision": revision,
                "invalidated_gates": invalidated,
            }
        )

    def do_POST(self) -> None:
        if not self.allow_request(mutation=True):
            return
        path, query = self.route()
        if path == "/api/assets":
            self.handle_asset_upload(query)
            return
        if path == "/api/copy-draft":
            self.handle_copy_draft()
            return
        if path == "/api/plan-highlights":
            self.handle_plan_highlights()
            return
        if path == "/api/captions/snap":
            self.handle_caption_snap()
            return
        if path == "/api/captions/apply-style":
            self.handle_caption_apply_style()
            return
        if path == "/api/approve":
            self.handle_approval()
            return
        if path == "/api/render":
            self.handle_render()
            return
        if path == "/api/render-batch":
            self.handle_render_batch()
            return
        if path == "/api/cover":
            self.handle_cover()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_asset_upload(self, query: dict[str, list[str]]) -> None:
        filename = (query.get("filename") or [""])[0]
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_ASSET_EXTENSIONS:
            self.send_json(
                {"ok": False, "error": "asset type must be PNG, JPG, WEBP, GIF, MP4, or MOV"},
                status=415,
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in ALLOWED_ASSET_MIME_TYPES[suffix]:
            self.close_connection = True
            self.send_json({"ok": False, "error": "asset MIME type does not match its extension"}, status=415)
            return
        try:
            data = self.read_body(MAX_ASSET_BYTES)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=413)
            return
        if not data:
            self.send_json({"ok": False, "error": "asset file is empty"}, status=400)
            return
        if not asset_magic_matches(data[:64], suffix):
            self.send_json({"ok": False, "error": "asset content does not match its extension"}, status=415)
            return
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename).stem).strip("-")[:36] or "asset"
        stored_name = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        output = self.server.project_dir / "assets" / stored_name
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.parent / f".upload-{uuid.uuid4().hex}{suffix}"
        try:
            temporary.write_bytes(data)
            if not ffprobe_has_visual_stream(temporary):
                self.send_json({"ok": False, "error": "asset is not a decodable image or video"}, status=415)
                return
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        provenance_path = self.server.project_dir / "assets/provenance.json"
        provenance = read_json(provenance_path, {"items": []}) or {"items": []}
        provenance.setdefault("items", []).append(
            {
                "file": f"assets/{stored_name}",
                "original_name": Path(filename).name,
                "source": "user-uploaded-through-local-editor",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "uploaded_at": now_utc(),
            }
        )
        atomic_write_json(provenance_path, provenance)
        self.send_json(
            {
                "ok": True,
                "source": f"assets/{stored_name}",
                "url": f"/assets/{urllib.parse.quote(stored_name)}",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    def handle_copy_draft(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        platform_id = str(body.get("platform_id", ""))
        if platform_id not in PLATFORM_PRESETS:
            self.send_json({"ok": False, "error": "unsupported platform"}, status=422)
            return
        draft = copy_draft(platform_id, transcript_text(self.server.project_dir))
        self.send_json({"ok": True, "draft": draft})

    def handle_plan_highlights(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        director = str(body.get("director", ""))
        brief = str(body.get("brief", "")).strip()
        count = body.get("count", 10)
        expected_revision = str(body.get("expected_revision", ""))
        if director not in DIRECTOR_PRESETS:
            self.send_json({"ok": False, "error": "unsupported director profile"}, status=422)
            return
        if len(brief) > 2000 or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            self.send_json({"ok": False, "error": "invalid highlight planning settings"}, status=422)
            return
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            self.send_json({"ok": False, "error": "expected_revision is required"}, status=409)
            return
        with self.server.project_lock:
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            current_revision = editor_state_revision(state)
            if expected_revision != current_revision:
                self.send_json(
                    {
                        "ok": False,
                        "error": "editor state changed before highlight planning",
                        "current_revision": current_revision,
                    },
                    status=409,
                )
                return
            command = [
                sys.executable,
                str(SKILL_DIR / "scripts/auto_edit.py"),
                "plan-highlights",
                "--manifest",
                str(self.server.project_dir / "project.json"),
                "--director",
                director,
                "--count",
                str(count),
                "--brief",
                brief,
            ]
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                self.send_json({"ok": False, "error": "highlight planning timed out"}, status=504)
                return
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "highlight planning failed").strip()[-600:]
                self.send_json(
                    {"ok": False, "error": message},
                    status=422 if result.returncode == 3 else 500,
                )
                return
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            plan = read_json(self.server.project_dir / "working/highlight_plan.json", {}) or {}
            revisions = approval_revisions(self.server.project_dir, state)
        self.send_json(
            {
                "ok": True,
                "manifest": manifest,
                "state": state,
                "highlight_plan": plan,
                "approval_revisions": revisions,
            }
        )

    def handle_approval(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        gate = str(body.get("gate", ""))
        if gate not in GATES:
            self.send_json({"ok": False, "error": "unsupported approval gate"}, status=422)
            return
        expected_revision = str(body.get("expected_revision", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            self.send_json(
                {"ok": False, "error": "expected_revision is required for approval"},
                status=409,
            )
            return
        manifest_path = self.server.project_dir / "project.json"
        with self.server.project_lock:
            manifest = read_json(manifest_path, {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            current_revision = gate_revision(self.server.project_dir, gate, state)
            if expected_revision != current_revision:
                self.send_json(
                    {
                        "ok": False,
                        "error": "approval revision is stale; reload the current project state",
                        "current_revision": current_revision,
                    },
                    status=409,
                )
                return
            errors = approval_prerequisite_errors(
                self.server.project_dir,
                manifest,
                state,
                gate,
            )
            if errors:
                self.send_json({"ok": False, "error": "; ".join(errors)}, status=409)
                return
            approval = {
                "approved": True,
                "confirmed_by": str(body.get("confirmed_by") or "local-editor-user")[:120],
                "at": now_utc(),
                "note": str(body.get("note") or "Approved in local editor")[:500],
                "state_revision": current_revision,
                "revision_kind": gate,
            }
            if gate == "highlight_selection":
                approval["plan_revision"] = state.get("highlight_plan_revision")
            manifest.setdefault("approvals", {})[gate] = approval
            stages = manifest.setdefault("stages", {})
            if gate == "destructive_edit":
                stages["edit_review"] = "complete"
            elif gate == "highlight_selection":
                stages["highlight_plan"] = "complete"
            elif gate == "timeline":
                stages["timeline_review"] = "complete"
            elif gate == "final":
                stages["edit_review"] = "complete"
                stages["cut"] = "skipped"
                stages["retranscribe"] = "skipped"
                if state.get("highlights"):
                    stages["highlight_plan"] = "complete"
                stages["timeline_review"] = "complete"
                overlay_types = {
                    str(item.get("type"))
                    for item in state.get("overlays", [])
                    if isinstance(item, dict) and item.get("visible", True)
                }
                if "caption" in overlay_types:
                    stages["subtitles"] = "complete"
                if "emphasis" in overlay_types:
                    stages["emphasis"] = "complete"
                if overlay_types & {"title", "card", "image", "gif", "video", "animation"}:
                    stages["visual_plan"] = "complete"
                stages["render"] = "complete"
                stages["qa"] = "complete"
            manifest["updated_at"] = now_utc()
            atomic_write_json(manifest_path, manifest)
            revisions = approval_revisions(self.server.project_dir, state)
        self.send_json(
            {
                "ok": True,
                "gate": gate,
                "approval": approval,
                "approval_revisions": revisions,
                "approval_current": {
                    name: approval_is_current(
                        self.server.project_dir,
                        manifest,
                        name,
                        state,
                    )
                    for name in sorted(GATES)
                },
            }
        )

    def handle_render(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        quality = str(body.get("quality", "preview"))
        if quality not in {"preview", "final"}:
            self.send_json({"ok": False, "error": "quality must be preview or final"}, status=422)
            return
        expected_revision = str(body.get("expected_revision", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            self.send_json(
                {"ok": False, "error": "expected_revision is required for render"},
                status=409,
            )
            return
        with self.server.project_lock:
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            current_revision = editor_state_revision(state)
            if expected_revision != current_revision:
                self.send_json(
                    {
                        "ok": False,
                        "error": "render revision is stale; save and reload before rendering",
                        "current_revision": current_revision,
                    },
                    status=409,
                )
                return
            try:
                actual_assets = referenced_asset_digests(self.server.project_dir, state)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=409)
                return
            if actual_assets != (state.get("asset_digests") or {}):
                self.send_json(
                    {"ok": False, "error": "a referenced asset changed after the editor state was saved"},
                    status=409,
                )
                return
            template_state = state.get("video_template")
            if isinstance(template_state, dict):
                readiness = template_readiness_errors(template_state)
                if readiness:
                    self.send_json(
                        {"ok": False, "error": "; ".join(readiness)},
                        status=409,
                    )
                    return
                template_id = str(template_state.get("id") or "")
                if template_id.startswith("cutout-"):
                    capability = cutout_capability()
                    if not capability.get("available"):
                        self.send_json(
                            {
                                "ok": False,
                                "error": str(
                                    capability.get("reason")
                                    or "local subject cutout is unavailable"
                                ),
                            },
                            status=409,
                        )
                        return
            highlights = state.get("highlights", []) if isinstance(state.get("highlights"), list) else []
            clip_id = str(body.get("clip_id") or state.get("active_highlight_id") or "")
            clip = next(
                (
                    item
                    for item in highlights
                    if isinstance(item, dict) and str(item.get("id")) == clip_id
                ),
                None,
            )
            if clip_id and clip is None:
                self.send_json({"ok": False, "error": "selected highlight does not exist"}, status=422)
                return
            if quality == "final":
                if approved_destructive_deletes(self.server.project_dir):
                    self.send_json(
                        {
                            "ok": False,
                            "error": (
                                "reviewed delete decisions are not applied by the page-editor renderer; "
                                "set them to keep or use the destructive cut renderer first"
                            ),
                        },
                        status=409,
                    )
                    return
                if highlights and clip is None:
                    self.send_json(
                        {"ok": False, "error": "select an approved highlight before final render"},
                        status=409,
                    )
                    return
                if clip is not None and clip.get("review_status") != "approved":
                    self.send_json(
                        {"ok": False, "error": "selected highlight must be approved before final render"},
                        status=409,
                    )
                    return
                prerequisite_errors = approval_prerequisite_errors(
                    self.server.project_dir,
                    manifest,
                    state,
                    "timeline",
                )
                if prerequisite_errors or not approval_is_current(
                    self.server.project_dir,
                    manifest,
                    "timeline",
                    state,
                ):
                    self.send_json(
                        {
                            "ok": False,
                            "error": "; ".join(prerequisite_errors)
                            or "current timeline revision must be approved before final render",
                        },
                        status=409,
                    )
                    return
            render_id = f"render_{uuid.uuid4().hex}"
            clip_snapshot = None
            if clip is not None:
                clip_snapshot = {
                    key: clip.get(key)
                    for key in (
                        "id",
                        "plan_item_id",
                        "start",
                        "end",
                        "title",
                        "review_status",
                    )
                }
            snapshot = {
                "schema_version": 1,
                "render_id": render_id,
                "created_at": now_utc(),
                "quality": quality,
                "project_id": manifest.get("project_id"),
                "state_revision": current_revision,
                "approval_revisions": approval_revisions(self.server.project_dir, state),
                "clip": clip_snapshot,
                "visual_quality": visual_quality_report(state, manifest, clip_snapshot),
                "manifest": manifest,
                "state": state,
                "authorization": {
                    gate: manifest.get("approvals", {}).get(gate, {})
                    for gate in GATES
                },
            }
            snapshot_path = (
                self.server.project_dir / "working/render_snapshots" / f"{render_id}.json"
            )
            atomic_write_json(snapshot_path, snapshot)
            safe_clip = re.sub(r"[^A-Za-z0-9_-]", "-", clip_id)[:80] if clip_id else "source-full"
            version = render_id.rsplit("_", 1)[-1][:8]
            output_name = (
                f"{safe_clip}-{version}-preview.mp4"
                if quality == "preview"
                else f"{safe_clip}-{version}-final.mp4"
            )
            with self.server.render_lock:
                if self.server.render_status.get("state") == "running":
                    self.send_json({"ok": False, "error": "a render is already running"}, status=409)
                    return
                self.server.render_status = {
                    "state": "running",
                    "message": "正在輸出預覽…" if quality == "preview" else "正在輸出最終影片…",
                    "quality": quality,
                    "clip_id": clip_id or None,
                    "render_id": render_id,
                    "state_revision": current_revision,
                    "output": None,
                    "started_at": now_utc(),
                }
        threading.Thread(
            target=self.render_worker,
            args=(
                quality,
                snapshot_path,
                output_name,
                render_id,
                clip_id or None,
                current_revision,
            ),
            daemon=True,
        ).start()
        self.send_json({"ok": True, "status": self.server.render_status}, status=202)

    def handle_render_batch(self) -> None:
        """Queue one atomic final-delivery batch for all approved highlights."""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not isinstance(body, dict) or str(body.get("quality", "final")) != "final":
            self.send_json(
                {"ok": False, "error": "batch render supports final quality only"},
                status=422,
            )
            return
        expected_revision = str(body.get("expected_revision", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            self.send_json(
                {"ok": False, "error": "expected_revision is required for batch render"},
                status=409,
            )
            return

        with self.server.project_lock:
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            current_revision = editor_state_revision(state)
            if expected_revision != current_revision:
                self.send_json(
                    {
                        "ok": False,
                        "error": "batch render revision is stale; save and reload before rendering",
                        "current_revision": current_revision,
                    },
                    status=409,
                )
                return
            try:
                actual_assets = referenced_asset_digests(self.server.project_dir, state)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=409)
                return
            if actual_assets != (state.get("asset_digests") or {}):
                self.send_json(
                    {
                        "ok": False,
                        "error": "a referenced asset changed after the editor state was saved",
                    },
                    status=409,
                )
                return

            template_state = state.get("video_template")
            if isinstance(template_state, dict):
                readiness = template_readiness_errors(template_state)
                if readiness:
                    self.send_json(
                        {"ok": False, "error": "; ".join(readiness)},
                        status=409,
                    )
                    return
                template_id = str(template_state.get("id") or "")
                if template_id.startswith("cutout-"):
                    capability = cutout_capability()
                    if not capability.get("available"):
                        self.send_json(
                            {
                                "ok": False,
                                "error": str(
                                    capability.get("reason")
                                    or "local subject cutout is unavailable"
                                ),
                            },
                            status=409,
                        )
                        return

            if approved_destructive_deletes(self.server.project_dir):
                self.send_json(
                    {
                        "ok": False,
                        "error": (
                            "reviewed delete decisions are not applied by the page-editor renderer; "
                            "set them to keep or use the destructive cut renderer first"
                        ),
                    },
                    status=409,
                )
                return
            clips, clip_errors = approved_highlights_in_plan_order(
                self.server.project_dir,
                state,
            )
            prerequisite_errors = approval_prerequisite_errors(
                self.server.project_dir,
                manifest,
                state,
                "timeline",
            )
            if not approval_is_current(
                self.server.project_dir,
                manifest,
                "timeline",
                state,
            ):
                prerequisite_errors.append(
                    "current timeline revision must be approved before batch render"
                )
            prerequisite_errors.extend(clip_errors)
            for clip in clips:
                prerequisite_errors.extend(
                    f"{clip.get('id')}: {error}"
                    for error in visual_quality_errors(state, manifest, clip)
                )
            if prerequisite_errors:
                self.send_json(
                    {"ok": False, "error": "; ".join(dict.fromkeys(prerequisite_errors))},
                    status=409,
                )
                return

            batch_id = f"batch_{uuid.uuid4().hex}"
            jobs: list[dict[str, Any]] = []
            for index, clip in enumerate(clips, start=1):
                render_id = f"render_{uuid.uuid4().hex}"
                clip_snapshot = {
                    key: clip.get(key)
                    for key in (
                        "id",
                        "plan_item_id",
                        "start",
                        "end",
                        "title",
                        "review_status",
                    )
                }
                snapshot = {
                    "schema_version": 1,
                    "render_id": render_id,
                    "batch_id": batch_id,
                    "batch_index": index,
                    "created_at": now_utc(),
                    "quality": "final",
                    "project_id": manifest.get("project_id"),
                    "state_revision": current_revision,
                    "approval_revisions": approval_revisions(
                        self.server.project_dir,
                        state,
                    ),
                    "clip": clip_snapshot,
                    "visual_quality": visual_quality_report(
                        state,
                        manifest,
                        clip_snapshot,
                    ),
                    "manifest": manifest,
                    "state": state,
                    "authorization": {
                        gate: manifest.get("approvals", {}).get(gate, {})
                        for gate in GATES
                    },
                }
                snapshot_path = (
                    self.server.project_dir
                    / "working/render_snapshots"
                    / f"{render_id}.json"
                )
                atomic_write_json(snapshot_path, snapshot)
                clip_id = str(clip.get("id") or "")
                safe_clip = re.sub(r"[^A-Za-z0-9_-]", "-", clip_id)[:80]
                output_name = f"{index:02d}-{safe_clip}-{render_id[-8:]}-final.mp4"
                jobs.append(
                    {
                        "index": index,
                        "clip_id": clip_id,
                        "render_id": render_id,
                        "snapshot_path": snapshot_path,
                        "output_name": output_name,
                    }
                )

            with self.server.render_lock:
                if self.server.render_status.get("state") == "running":
                    self.send_json(
                        {"ok": False, "error": "a render is already running"},
                        status=409,
                    )
                    return
                self.server.render_status = {
                    "state": "running",
                    "mode": "batch",
                    "message": f"正在批次輸出 0/{len(jobs)}…",
                    "quality": "final",
                    "batch_id": batch_id,
                    "state_revision": current_revision,
                    "clip_ids": [str(item.get("id") or "") for item in clips],
                    "current_clip_id": None,
                    "completed_clips": 0,
                    "total_clips": len(jobs),
                    "items": [],
                    "output": None,
                    "started_at": now_utc(),
                }
        threading.Thread(
            target=self.render_batch_worker,
            args=(batch_id, jobs, current_revision),
            daemon=True,
        ).start()
        self.send_json({"ok": True, "status": self.server.render_status}, status=202)

    def render_batch_item(
        self,
        batch_id: str,
        job: dict[str, Any],
        state_revision: str,
    ) -> dict[str, Any]:
        """Render and QA one frozen final snapshot without publishing it as latest."""
        render_id = str(job["render_id"])
        clip_id = str(job["clip_id"])
        snapshot_path = Path(job["snapshot_path"])
        output_name = str(job["output_name"])
        output = self.server.project_dir / "renders" / output_name
        temporary = output.parent / f".{output.stem}.{render_id}.part.mp4"
        qa_report = self.server.project_dir / "qa" / f"{render_id}-qa-report.json"
        qa_contact = self.server.project_dir / "qa" / f"{render_id}-contact.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_payload = read_json(snapshot_path, {}) or {}
            visual_quality = (
                snapshot_payload.get("visual_quality")
                if isinstance(snapshot_payload.get("visual_quality"), dict)
                else {}
            )
            command = [
                sys.executable,
                str(SKILL_DIR / "scripts/render_editor_timeline.py"),
                "--project-dir",
                str(self.server.project_dir),
                "--snapshot",
                str(snapshot_path),
                "--quality",
                "final",
                "--output",
                str(temporary),
            ]
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    timeout=2 * 60 * 60,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("batch clip render timed out") from exc
            if (
                result.returncode != 0
                or not temporary.is_file()
                or not ffprobe_has_visual_stream(temporary)
            ):
                raise RuntimeError(
                    (result.stderr or result.stdout or "batch clip render failed")
                    .strip()[-1200:]
                )

            qa_command = [
                sys.executable,
                str(SKILL_DIR / "scripts/qa_video.py"),
                "--video",
                str(temporary),
                "--report",
                str(qa_report),
                "--contact",
                str(qa_contact),
            ]
            try:
                qa_result = subprocess.run(
                    qa_command,
                    text=True,
                    capture_output=True,
                    timeout=10 * 60,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("delivery QA failed: batch clip QA timed out") from exc
            qa_payload = read_json(qa_report, {}) or {}
            qa_payload["visual_quality"] = visual_quality
            atomic_write_json(qa_report, qa_payload)
            if qa_result.returncode != 0 or qa_payload.get("status") != "pass":
                failure_receipt = {
                    "schema_version": 1,
                    "batch_id": batch_id,
                    "render_id": render_id,
                    "quality": "final",
                    "clip_id": clip_id,
                    "state_revision": state_revision,
                    "status": "fail",
                    "report": str(qa_report.relative_to(self.server.project_dir)),
                    "contact_sheet": str(qa_contact.relative_to(self.server.project_dir))
                    if qa_contact.is_file()
                    else None,
                    "failures": qa_payload.get("failures", []),
                    "warnings": qa_payload.get("warnings", []),
                    "visual_quality": visual_quality,
                    "completed_at": now_utc(),
                }
                atomic_write_json(
                    self.server.project_dir
                    / "working/delivery_qa"
                    / f"{render_id}-failed.json",
                    failure_receipt,
                )
                detail = "; ".join(str(item) for item in qa_payload.get("failures", []))
                raise RuntimeError(
                    "delivery QA failed: "
                    + (
                        detail
                        or (qa_result.stderr or qa_result.stdout or "batch clip QA failed")
                        .strip()[-1100:]
                    )
                )

            output_sha = file_sha256(temporary)
            os.replace(temporary, output)
            render_receipt = {
                "schema_version": 1,
                "batch_id": batch_id,
                "render_id": render_id,
                "quality": "final",
                "clip_id": clip_id,
                "state_revision": state_revision,
                "snapshot": str(snapshot_path.relative_to(self.server.project_dir)),
                "snapshot_sha256": file_sha256(snapshot_path),
                "output": str(output.relative_to(self.server.project_dir)),
                "output_sha256": output_sha,
                "bytes": output.stat().st_size,
                "completed_at": now_utc(),
            }
            render_receipt_path = (
                self.server.project_dir
                / "working/render_receipts"
                / f"{render_id}.json"
            )
            atomic_write_json(render_receipt_path, render_receipt)
            qa_payload["video"] = str(output)
            atomic_write_json(qa_report, qa_payload)
            delivery = {
                "schema_version": 1,
                "batch_id": batch_id,
                "render_id": render_id,
                "quality": "final",
                "clip_id": clip_id,
                "state_revision": state_revision,
                "status": "pass",
                "output": str(output.relative_to(self.server.project_dir)),
                "output_sha256": output_sha,
                "report": str(qa_report.relative_to(self.server.project_dir)),
                "report_sha256": file_sha256(qa_report),
                "contact_sheet": str(qa_contact.relative_to(self.server.project_dir)),
                "contact_sheet_sha256": file_sha256(qa_contact),
                "render_receipt": str(render_receipt_path.relative_to(self.server.project_dir)),
                "render_receipt_sha256": file_sha256(render_receipt_path),
                "warnings": qa_payload.get("warnings", []),
                "failures": qa_payload.get("failures", []),
                "visual_quality": visual_quality,
                "human_review_required": True,
                "completed_at": now_utc(),
            }
            atomic_write_json(
                self.server.project_dir / "working/delivery_qa" / f"{render_id}.json",
                delivery,
            )
            return delivery
        finally:
            temporary.unlink(missing_ok=True)

    def render_batch_worker(
        self,
        batch_id: str,
        jobs: list[dict[str, Any]],
        state_revision: str,
    ) -> None:
        """Render a batch transaction and publish latest_final_qa only on full success."""
        completed: list[dict[str, Any]] = []
        archive_name = f"{batch_id}-final.zip"
        archive = self.server.project_dir / "renders" / archive_name
        archive_temporary = archive.parent / f".{archive_name}.{uuid.uuid4().hex}.part"
        current_clip_id: str | None = None
        try:
            for index, job in enumerate(jobs, start=1):
                current_clip_id = str(job["clip_id"])
                self.server.render_status.update(
                    {
                        "message": f"正在批次輸出 {index}/{len(jobs)}：{current_clip_id}",
                        "current_clip_id": current_clip_id,
                        "current_index": index,
                    }
                )
                item = self.render_batch_item(batch_id, job, state_revision)
                item["archive_name"] = f"{index:02d}-{Path(str(item['output'])).name}"
                completed.append(item)
                self.server.render_status.update(
                    {
                        "completed_clips": len(completed),
                        "items": [
                            {
                                "clip_id": entry["clip_id"],
                                "output": "/" + urllib.parse.quote(
                                    str(entry["output"]),
                                    safe="/",
                                ),
                                "qa_report": "/" + urllib.parse.quote(
                                    str(entry["report"]),
                                    safe="/",
                                ),
                                "qa_contact": "/" + urllib.parse.quote(
                                    str(entry["contact_sheet"]),
                                    safe="/",
                                ),
                            }
                            for entry in completed
                        ],
                    }
                )

            with zipfile.ZipFile(
                archive_temporary,
                "w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as bundle:
                for item in completed:
                    output = scoped_project_path(
                        self.server.project_dir,
                        str(item["output"]),
                        "renders",
                    )
                    if file_sha256(output) != item["output_sha256"]:
                        raise RuntimeError(
                            f"batch output changed before packaging: {item['clip_id']}"
                        )
                    bundle.write(output, arcname=str(item["archive_name"]))
            os.replace(archive_temporary, archive)
            batch_receipt = {
                "schema_version": 2,
                "kind": "batch",
                "delivery_kind": "batch",
                "batch_id": batch_id,
                "quality": "final",
                "state_revision": state_revision,
                "status": "pass",
                "clip_ids": [str(job["clip_id"]) for job in jobs],
                "item_count": len(completed),
                "items": completed,
                "archive": str(archive.relative_to(self.server.project_dir)),
                "archive_sha256": file_sha256(archive),
                "archive_download_name": archive_name,
                "warnings": [
                    {"clip_id": item["clip_id"], "items": item.get("warnings", [])}
                    for item in completed
                    if item.get("warnings")
                ],
                "failures": [],
                "human_review_required": True,
                "completed_at": now_utc(),
            }
            versioned_receipt = (
                self.server.project_dir / "working/delivery_qa" / f"{batch_id}.json"
            )

            with self.server.project_lock:
                current_state = read_json(self.server.project_dir / STATE_REL, {}) or {}
                manifest_path = self.server.project_dir / "project.json"
                manifest = read_json(manifest_path, {}) or {}
                authorization_errors: list[str] = []
                if editor_state_revision(current_state) != state_revision:
                    authorization_errors.append("editor state changed during batch render")
                current_clips, current_clip_errors = approved_highlights_in_plan_order(
                    self.server.project_dir,
                    current_state,
                )
                authorization_errors.extend(current_clip_errors)
                if [str(item.get("id") or "") for item in current_clips] != [
                    str(job["clip_id"]) for job in jobs
                ]:
                    authorization_errors.append(
                        "approved highlight set changed during batch render"
                    )
                for gate in ("destructive_edit", "highlight_selection", "timeline"):
                    if not approval_is_current(
                        self.server.project_dir,
                        manifest,
                        gate,
                        current_state,
                    ):
                        authorization_errors.append(
                            f"{gate} approval changed during batch render"
                        )
                if approved_destructive_deletes(self.server.project_dir):
                    authorization_errors.append(
                        "reviewed delete decisions changed during batch render"
                    )
                try:
                    current_assets = referenced_asset_digests(
                        self.server.project_dir,
                        current_state,
                    )
                except ValueError as exc:
                    authorization_errors.append(str(exc))
                else:
                    if current_assets != (current_state.get("asset_digests") or {}):
                        authorization_errors.append(
                            "a referenced asset changed during batch render"
                        )
                if authorization_errors:
                    raise RuntimeError(
                        "; ".join(dict.fromkeys(authorization_errors))
                        + "; previous delivery was preserved"
                    )
                atomic_write_json(versioned_receipt, batch_receipt)
                final_approval = manifest.setdefault("approvals", {}).get("final")
                if isinstance(final_approval, dict) and final_approval.get("approved"):
                    manifest["approvals"]["final"] = {
                        "approved": False,
                        "confirmed_by": None,
                        "at": None,
                        "note": "Invalidated because a new final batch delivery was rendered",
                        "invalidated_at": now_utc(),
                    }
                stages = manifest.setdefault("stages", {})
                stages["render"] = "complete"
                stages["qa"] = "needs_review"
                manifest["updated_at"] = now_utc()
                atomic_write_json(manifest_path, manifest)
                revisions = approval_revisions(self.server.project_dir, current_state)
                revisions["final"] = canonical_revision(
                    {
                        "editor_state_revision": state_revision,
                        "delivery_qa": batch_receipt,
                    }
                )
                atomic_write_json(
                    self.server.project_dir / LATEST_DELIVERY_QA_REL,
                    batch_receipt,
                )

            self.server.render_status = {
                "state": "complete",
                "mode": "batch",
                "message": f"{len(completed)} 支最終影片與 QA 已完成，請逐支檢查後核可",
                "quality": "final",
                "batch_id": batch_id,
                "state_revision": state_revision,
                "current": True,
                "clip_ids": [str(job["clip_id"]) for job in jobs],
                "current_clip_id": None,
                "completed_clips": len(completed),
                "total_clips": len(jobs),
                "items": self.server.render_status.get("items", []),
                "output": f"/renders/{urllib.parse.quote(archive_name)}",
                "download_name": archive_name,
                "output_sha256": batch_receipt["archive_sha256"],
                "qa": batch_receipt,
                "approval_revisions": revisions,
                "finished_at": now_utc(),
            }
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            diagnostic_items = list(completed)
            if current_clip_id:
                failed_job = next(
                    (
                        job
                        for job in jobs
                        if str(job.get("clip_id") or "") == current_clip_id
                    ),
                    None,
                )
                if isinstance(failed_job, dict):
                    failed_render_id = str(failed_job.get("render_id") or "")
                    failed_item = read_json(
                        self.server.project_dir
                        / "working/delivery_qa"
                        / f"{failed_render_id}-failed.json",
                        None,
                    )
                    if isinstance(failed_item, dict):
                        diagnostic_items.append(failed_item)
            failure = {
                "schema_version": 2,
                "kind": "batch",
                "delivery_kind": "batch",
                "batch_id": batch_id,
                "quality": "final",
                "state_revision": state_revision,
                "status": "fail",
                "clip_ids": [str(job["clip_id"]) for job in jobs],
                "item_count": len(jobs),
                "items": diagnostic_items,
                "failed_clip_id": current_clip_id,
                "completed_clips": len(completed),
                "total_clips": len(jobs),
                "error": str(exc)[-1200:],
                "previous_latest_preserved": True,
                "completed_at": now_utc(),
            }
            try:
                atomic_write_json(
                    self.server.project_dir
                    / "working/delivery_qa"
                    / f"{batch_id}-failed.json",
                    failure,
                )
            except OSError:
                pass
            self.server.render_status = {
                "state": "qa_failed"
                if str(exc).startswith("delivery QA failed")
                else "failed",
                "mode": "batch",
                "message": str(exc)[-1200:],
                "quality": "final",
                "batch_id": batch_id,
                "state_revision": state_revision,
                "clip_ids": [str(job["clip_id"]) for job in jobs],
                "failed_clip_id": current_clip_id,
                "completed_clips": len(completed),
                "total_clips": len(jobs),
                "items": diagnostic_items,
                "qa": failure,
                "output": None,
                "previous_latest_preserved": True,
                "finished_at": now_utc(),
            }
        finally:
            archive_temporary.unlink(missing_ok=True)

    def render_worker(
        self,
        quality: str,
        snapshot_path: Path,
        output_name: str,
        render_id: str,
        clip_id: str | None,
        state_revision: str,
    ) -> None:
        script = SKILL_DIR / "scripts/render_editor_timeline.py"
        output = self.server.project_dir / "renders" / output_name
        temporary = output.parent / f".{output.stem}.{render_id}.part.mp4"
        command = [
            sys.executable,
            str(script),
            "--project-dir",
            str(self.server.project_dir),
            "--snapshot",
            str(snapshot_path),
            "--quality",
            quality,
            "--output",
            str(temporary),
        ]
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_payload = read_json(snapshot_path, {}) or {}
            visual_quality = (
                snapshot_payload.get("visual_quality")
                if isinstance(snapshot_payload.get("visual_quality"), dict)
                else {}
            )
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    timeout=2 * 60 * 60,
                )
            except subprocess.TimeoutExpired:
                result = subprocess.CompletedProcess(command, 124, "", "render timed out")
            if result.returncode != 0 or not temporary.is_file() or not ffprobe_has_visual_stream(temporary):
                raise RuntimeError((result.stderr or result.stdout or "render failed").strip()[-1200:])
            qa_payload: dict[str, Any] | None = None
            qa_report: Path | None = None
            qa_contact: Path | None = None
            if quality == "final":
                qa_report = self.server.project_dir / "qa" / f"{render_id}-qa-report.json"
                qa_contact = self.server.project_dir / "qa" / f"{render_id}-contact.png"
                qa_command = [
                    sys.executable,
                    str(SKILL_DIR / "scripts/qa_video.py"),
                    "--video",
                    str(temporary),
                    "--report",
                    str(qa_report),
                    "--contact",
                    str(qa_contact),
                ]
                try:
                    qa_result = subprocess.run(
                        qa_command,
                        text=True,
                        capture_output=True,
                        timeout=10 * 60,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("delivery QA timed out; previous output was preserved") from exc
                qa_payload = read_json(qa_report, {}) or {}
                qa_payload["visual_quality"] = visual_quality
                atomic_write_json(qa_report, qa_payload)
                if qa_result.returncode != 0 or qa_payload.get("status") != "pass":
                    failure_receipt = {
                        "schema_version": 1,
                        "render_id": render_id,
                        "quality": quality,
                        "clip_id": clip_id,
                        "state_revision": state_revision,
                        "status": "fail",
                        "report": str(qa_report.relative_to(self.server.project_dir)),
                        "contact_sheet": str(qa_contact.relative_to(self.server.project_dir))
                        if qa_contact.is_file()
                        else None,
                        "failures": qa_payload.get("failures", []),
                        "warnings": qa_payload.get("warnings", []),
                        "visual_quality": visual_quality,
                        "completed_at": now_utc(),
                    }
                    atomic_write_json(
                        self.server.project_dir
                        / "working/delivery_qa"
                        / f"{render_id}-failed.json",
                        failure_receipt,
                    )
                    detail = "; ".join(str(item) for item in qa_payload.get("failures", []))
                    self.server.render_status = {
                        "state": "qa_failed",
                        "message": detail
                        or (qa_result.stderr or qa_result.stdout or "delivery QA failed").strip()[-1200:],
                        "quality": quality,
                        "clip_id": clip_id,
                        "render_id": render_id,
                        "output": None,
                        "qa": failure_receipt,
                        "finished_at": now_utc(),
                    }
                    return

            output_sha = file_sha256(temporary)
            os.replace(temporary, output)
            receipt = {
                "schema_version": 1,
                "render_id": render_id,
                "quality": quality,
                "clip_id": clip_id,
                "state_revision": state_revision,
                "snapshot": str(snapshot_path.relative_to(self.server.project_dir)),
                "snapshot_sha256": file_sha256(snapshot_path),
                "output": str(output.relative_to(self.server.project_dir)),
                "output_sha256": output_sha,
                "bytes": output.stat().st_size,
                "completed_at": now_utc(),
            }
            render_receipt_path = (
                self.server.project_dir / "working/render_receipts" / f"{render_id}.json"
            )
            atomic_write_json(render_receipt_path, receipt)
            delivery_qa: dict[str, Any] | None = None
            revisions: dict[str, str] | None = None
            is_current = True
            if quality == "final" and qa_payload is not None and qa_report is not None and qa_contact is not None:
                qa_payload["video"] = str(output)
                atomic_write_json(qa_report, qa_payload)
                delivery_qa = {
                    "schema_version": 1,
                    "render_id": render_id,
                    "quality": "final",
                    "clip_id": clip_id,
                    "state_revision": state_revision,
                    "status": "pass",
                    "output": str(output.relative_to(self.server.project_dir)),
                    "output_sha256": output_sha,
                    "report": str(qa_report.relative_to(self.server.project_dir)),
                    "report_sha256": file_sha256(qa_report),
                    "contact_sheet": str(qa_contact.relative_to(self.server.project_dir)),
                    "contact_sheet_sha256": file_sha256(qa_contact),
                    "render_receipt": str(render_receipt_path.relative_to(self.server.project_dir)),
                    "render_receipt_sha256": file_sha256(render_receipt_path),
                    "warnings": qa_payload.get("warnings", []),
                    "failures": qa_payload.get("failures", []),
                    "visual_quality": visual_quality,
                    "human_review_required": True,
                    "completed_at": now_utc(),
                }
                atomic_write_json(
                    self.server.project_dir / "working/delivery_qa" / f"{render_id}.json",
                    delivery_qa,
                )
                atomic_write_json(
                    self.server.project_dir / LATEST_DELIVERY_QA_REL,
                    delivery_qa,
                )
                with self.server.project_lock:
                    manifest_path = self.server.project_dir / "project.json"
                    manifest = read_json(manifest_path, {}) or {}
                    final_approval = manifest.setdefault("approvals", {}).get("final")
                    if isinstance(final_approval, dict) and final_approval.get("approved"):
                        manifest["approvals"]["final"] = {
                            "approved": False,
                            "confirmed_by": None,
                            "at": None,
                            "note": "Invalidated because a new final delivery was rendered",
                            "invalidated_at": now_utc(),
                        }
                    stages = manifest.setdefault("stages", {})
                    stages["render"] = "complete"
                    stages["qa"] = "needs_review"
                    manifest["updated_at"] = now_utc()
                    atomic_write_json(manifest_path, manifest)
                    current_state = read_json(self.server.project_dir / STATE_REL, {}) or {}
                    is_current = editor_state_revision(current_state) == state_revision
                    revisions = approval_revisions(self.server.project_dir, current_state)
            self.server.render_status = {
                "state": "complete",
                "message": "預覽已完成"
                if quality == "preview"
                else (
                    "最終影片與機械 QA 已完成，請檢查九宮格後核可"
                    if is_current
                    else "輸出完成，但時間軸已變更；請重新輸出後再核可"
                ),
                "quality": quality,
                "clip_id": clip_id,
                "render_id": render_id,
                "state_revision": state_revision,
                "current": is_current,
                "output": f"/renders/{urllib.parse.quote(output_name)}",
                "download_name": output_name,
                "output_sha256": receipt["output_sha256"],
                "qa": delivery_qa,
                "qa_report": f"/{qa_report.relative_to(self.server.project_dir)}"
                if qa_report is not None
                else None,
                "qa_contact": f"/{qa_contact.relative_to(self.server.project_dir)}"
                if qa_contact is not None
                else None,
                "approval_revisions": revisions,
                "finished_at": now_utc(),
            }
        except (OSError, RuntimeError) as exc:
            self.server.render_status = {
                "state": "failed",
                "message": str(exc)[-1200:],
                "quality": quality,
                "clip_id": clip_id,
                "render_id": render_id,
                "output": None,
                "finished_at": now_utc(),
            }
        finally:
            temporary.unlink(missing_ok=True)

    def handle_cover(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        platform_id = str(body.get("platform_id", ""))
        if platform_id not in PLATFORM_PRESETS:
            self.send_json({"ok": False, "error": "unsupported platform"}, status=422)
            return
        try:
            timestamp = max(0.0, float(body.get("time", 0.0)))
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "cover time must be numeric"}, status=422)
            return
        text = str(body.get("text", ""))[:200]
        script = SKILL_DIR / "scripts/render_editor_timeline.py"
        output = self.server.project_dir / "renders/cover.png"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--project-dir",
                str(self.server.project_dir),
                "--cover",
                "--platform",
                platform_id,
                "--cover-time",
                str(timestamp),
                "--cover-text",
                text,
                "--output",
                str(output),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0 or not output.is_file():
            self.send_json(
                {"ok": False, "error": (result.stderr or result.stdout or "cover failed")[-1200:]},
                status=500,
            )
            return
        self.send_json({"ok": True, "output": "/renders/cover.png", "created_at": now_utc()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback bind. Use only on a trusted network.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "project.json").is_file():
        print(f"project.json not found under {project_dir}", file=sys.stderr)
        return 2
    if not is_loopback_host(args.host) and not args.allow_remote:
        print("Refusing non-loopback bind without --allow-remote", file=sys.stderr)
        return 2
    if not EDITOR_DIR.is_dir():
        print(f"editor assets missing: {EDITOR_DIR}", file=sys.stderr)
        return 2
    server = EditorServer((args.host, args.port), project_dir)
    url = f"http://{args.host}:{server.server_port}"
    print(json.dumps({"ok": True, "url": url, "project": str(project_dir)}, ensure_ascii=False))
    if args.open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

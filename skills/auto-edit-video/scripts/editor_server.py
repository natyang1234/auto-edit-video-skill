#!/usr/bin/env python3
"""Local review/editor server for an auto-edit-video project.

The server binds to loopback by default, serves media with HTTP Range support,
and only reads/writes inside the selected project directory.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import stat
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
import asset_registry
import contract_registry
import delivery_envelope
from asset_provider_service import AssetProviderError, AssetProviderService
from local_http_security import (
    csrf_token_matches,
    host_header_allowed,
    is_loopback_host,
    mutation_origin_allowed,
)
from visual_quality import (
    ROLE_LAYOUTS,
    build_highlight_design_overlays,
    rendered_visual_evidence_path,
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


import generated_images
import visual_director
import qa_video
from director_resolver import (
    DirectorResolutionError,
    enforce_runtime_capabilities,
    resolve_director_profile,
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


def platform_safe_area(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The margins this project's platform keeps for its own controls.

    None when the project names no platform, or names one the registry does
    not carry: a missing answer, which callers report as unchecked rather
    than treating as "nothing is in the way".
    """
    canvas = (state or {}).get("canvas")
    if not isinstance(canvas, dict):
        return None
    preset = PLATFORM_PRESETS.get(str(canvas.get("platform_id") or ""))
    safe = (preset or {}).get("safe")
    return safe if isinstance(safe, dict) and safe else None

def _load_director_presets() -> dict[str, dict[str, Any]]:
    """Load UI-compatible presets plus the canonical resolver envelope."""
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
    presets: dict[str, dict[str, Any]] = {}
    for mode in payload["modes"]:
        try:
            resolved = resolve_director_profile(mode["id"])
        except DirectorResolutionError as exc:
            raise RuntimeError(
                f"director mode {mode.get('id', '<unknown>')} failed resolution: {exc.code}"
            ) from exc
        preset = dict(mode["constraints"])
        # Planning consumes density while rendering/UI need the motion
        # envelope.  Dropping the envelope here made every director look
        # "medium" downstream even though the contract carried the setting.
        preset["cut_density"] = mode["envelope"]["cut_density"]
        preset["motion_intensity"] = mode["envelope"]["motion_intensity"]
        preset.update(
            {
                "profile_id": resolved["profile_id"],
                "registry_schema_version": resolved["registry_schema_version"],
                "registry_entry_version": resolved["registry_entry_version"],
                "experience": resolved["experience"],
                "required_capabilities": resolved["required_capabilities"],
                "rules": resolved["rules"],
                "resolved_hash": resolved["resolved_hash"],
                "available": True,
                "missing_capabilities": [],
            }
        )
        try:
            enforce_runtime_capabilities(resolved)
        except DirectorResolutionError as exc:
            if exc.code != "capability_missing":
                raise RuntimeError(
                    f"director mode {mode.get('id', '<unknown>')} capability preflight failed: {exc.code}"
                ) from exc
            preset["available"] = False
            preset["missing_capabilities"] = sorted(exc.missing_capabilities)
        presets[mode["id"]] = preset
    return presets


DIRECTOR_PRESETS: dict[str, dict[str, Any]] = _load_director_presets()

DEFAULT_STYLE_PACK_BY_DIRECTOR = {
    "high-energy": "kinetic-social",
    "kinetic-explainer": "kinetic-social",
    "teacher-punch": "dark-data-presenter",
    "editorial-clean": "editorial-paper",
    "documentary": "editorial-paper",
    "minimal": "editorial-paper",
}

STYLE_PACK_PRESENTATION = {
    "dark-data-presenter": {
        "label": "深色數據",
        "description": "深色面板、冷白文字與紅色數據重點。",
    },
    "kinetic-social": {
        "label": "高動態社群",
        "description": "高對比酸性色與逐字、逐列、數字內容動畫。",
    },
    "editorial-paper": {
        "label": "暖紙編輯",
        "description": "暖紙色、深墨文字與克制淡入，保留畫面呼吸。",
    },
}


def public_style_pack_catalog() -> list[dict[str, Any]]:
    """Validated style packs exposed to the local editor picker."""
    import structured_card_compositor

    catalog = []
    for pack_id in structured_card_compositor.style_pack_ids():
        pack = structured_card_compositor.load_style_pack(pack_id)
        catalog.append(
            {
                "id": pack_id,
                "version": int(pack["version"]),
                **STYLE_PACK_PRESENTATION.get(
                    pack_id, {"label": pack_id, "description": pack_id}
                ),
            }
        )
    return catalog


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_utc_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _editor_render_revision_payload(
    state: dict[str, Any], *, include_audio_event_edits: bool
) -> dict[str, Any]:
    """Return the closed render-authority projection of editor state."""
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
    payload: dict[str, Any] = {
        "schema_version": state.get("schema_version"),
        "project_id": state.get("project_id"),
        "segments": state.get("segments"),
        "style_pack": state.get("style_pack"),
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
        # Required caption publication is authorized as exact adopted bytes,
        # not merely as the visible overlay text. Re-translation or receipt
        # changes must therefore invalidate the timeline approval.
        "caption_delivery": state.get("caption_delivery"),
        "overlays": revision_overlays,
        # Which QA gates apply is part of what was approved: relaxing them
        # must invalidate an approval given under stricter ones.
        "qa_policy": state.get("qa_policy"),
    }
    if include_audio_event_edits and "audio_event_edits" in state:
        payload["audio_event_edits"] = state.get("audio_event_edits")
    return payload


def _hash_editor_render_revision_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def editor_base_state_revision(state: dict[str, Any]) -> str:
    """Render revision excluding Studio audio overrides.

    A finalized base plan binds this value.  Keeping it separate prevents the
    source-plan binding from becoming circular when only an audio edit changes.
    """
    return _hash_editor_render_revision_payload(
        _editor_render_revision_payload(state, include_audio_event_edits=False)
    )


def editor_state_revision(state: dict[str, Any]) -> str:
    """Hash all fields that can change the rendered video."""
    return _hash_editor_render_revision_payload(
        _editor_render_revision_payload(state, include_audio_event_edits=True)
    )


def audio_event_edits_hash(edits: dict[str, Any] | None) -> str | None:
    """Canonical expected override identity supplied independently to QA."""
    if edits is None:
        return None
    return contract_registry.canonical_hash(edits)


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
        layers, visual_plan = load_layer_bundle(project_dir)
        return canonical_revision(
            {
                "editor_state": editor_state_revision(current_state),
                "structured_layers": layers,
                "visual_plan_v2": visual_plan,
            }
        )
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


def verified_receipt_file(
    project_dir: Path,
    relative: str,
    declared_sha256: str,
    scope: str,
    label: str,
) -> tuple[Path | None, str]:
    """Resolve one file a delivery receipt vouches for. Returns (path, error).

    Single, batch and variant deliveries each wrote out this same sequence —
    refuse a symlink, confine to the scope, require the file, require the
    digest — and had already drifted: one caught OSError from resolution and
    the other two did not. Nothing reaches that difference today, but three
    hand-written copies of a delivery gate is how a fourth one ends up
    missing a step that matters.
    """
    if not relative or not re.fullmatch(r"[0-9a-f]{64}", declared_sha256 or ""):
        return None, f"{label} contract is incomplete"
    try:
        if (project_dir / Path(relative)).is_symlink():
            raise ValueError("must not be a symlink")
        path = scoped_project_path(project_dir, relative, scope)
    except (ValueError, OSError):
        return None, f"{label} escapes its project scope"
    if not path.is_file():
        return None, f"{label} is missing"
    if file_sha256(path) != declared_sha256:
        return None, f"{label} changed after verification"
    return path, ""


def _variant_report_errors(
    project_dir: Path,
    receipt: dict[str, Any],
    variant_id: str,
    state: dict[str, Any] | None = None,
) -> list[str]:
    """Re-verify the QA report a variant delivery receipt points at.

    The variant download slot must reject receipts whose QA report is
    missing, tampered, failing, or generated before the enforced QaPolicy —
    otherwise pre-policy (black/silent) variant finals stay downloadable.
    """
    report_path, failure = verified_receipt_file(
        project_dir,
        str(receipt.get("report") or ""),
        str(receipt.get("report_sha256") or ""),
        "qa",
        f"variant {variant_id} QA report",
    )
    if failure:
        # The variant slot answers with one message whatever went wrong, so
        # a caller cannot probe the project by reading the difference.
        return [f"variant {variant_id} QA report is missing or does not match its receipt"]
    report = read_json(report_path, None)
    if not isinstance(report, dict) or report.get("status") != "pass":
        return [f"variant {variant_id} QA report is not passing"]
    if not isinstance(report.get("policy"), dict):
        return [
            f"variant {variant_id} QA report predates the enforced QA policy; "
            "render the variant again"
        ]
    errors = qa_profile_binding_errors(report, state, f"variant {variant_id}")
    errors.extend(
        visual_delivery_binding_errors(report, receipt, f"variant {variant_id}")
    )
    return errors


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
    # Variant outputs resolve to their OWN approval slot (plan v2 B4);
    # legacy single-slot receipts keep the original path below.
    delivery_dir = project_dir / VARIANT_DELIVERY_REL
    if delivery_dir.is_dir():
        for entry in sorted(delivery_dir.glob("*.json")):
            receipt = read_json(entry, None)
            if not isinstance(receipt, dict) or not receipt.get("variant_id"):
                continue
            if receipt.get("output") != relative:
                continue
            variant_id = str(receipt["variant_id"])
            if not variant_approval_is_current(
                project_dir, manifest, "final", state, variant_id
            ):
                return [
                    f"variant {variant_id} needs a current final approval before download"
                ]
            target = project_dir / relative
            if not target.is_file() or file_sha256(target) != receipt.get("output_sha256"):
                return ["variant output does not match its delivery receipt"]
            report_errors = _variant_report_errors(project_dir, receipt, variant_id, state)
            if report_errors:
                return report_errors
            return []
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


LAYERS_REL = Path("working/structured_layers.json")
VISUAL_PLAN_REL = Path("working/visual_plan_v2.json")
LAYER_TXN_JOURNAL_REL = Path("working/.layer-txn-journal.json")


_LAYER_TXN_LOCK = threading.Lock()


def recover_layer_transaction(project_dir: Path) -> None:
    """Roll an interrupted layer transaction FORWARD from its journal.

    The journal stores the full new contents AND target hashes. It is only
    cleared after every file verifiably matches its target hash; any failure
    keeps the journal (the only recovery data) and fails closed.
    """
    journal_path = project_dir / LAYER_TXN_JOURNAL_REL
    if not journal_path.is_file():
        return
    with _LAYER_TXN_LOCK:
        if not journal_path.is_file():
            return
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            files = journal.get("files") or {}
            hashes = journal.get("hashes") or {}
            for rel, content in files.items():
                atomic_write_json(project_dir / rel, content)
            for rel in files:
                written = read_json(project_dir / rel, None)
                expected = hashes.get(rel)
                if expected is not None and canonical_revision(written) != expected:
                    raise OSError(f"layer txn recovery verification failed for {rel}")
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                "layer transaction journal could not be recovered; the journal "
                f"was kept for manual inspection: {exc}"
            ) from exc
        journal_path.unlink(missing_ok=True)


def publish_layer_bundle(
    project_dir: Path,
    layers: dict[str, Any],
    visual_plan: dict[str, Any],
) -> None:
    """Cross-file transactional publish (plan v2 B1): validate everything in
    memory, journal the full target contents, then replace file by file."""
    import contract_registry

    bundle: dict[str, Any] = {
        "structured_layer": layers,
        "visual_plan": visual_plan,
    }
    evidence = read_json(project_dir / "working/evidence_map.json", None)
    if isinstance(evidence, dict):
        bundle["evidence_map"] = evidence
    errors = contract_registry.validate_bundle(bundle)
    if errors:
        raise ValueError("layer bundle rejected: " + "; ".join(errors))
    files = {
        LAYERS_REL.as_posix(): layers,
        VISUAL_PLAN_REL.as_posix(): visual_plan,
    }
    journal = {
        "generation": now_utc(),
        "files": files,
        "hashes": {rel: canonical_revision(content) for rel, content in files.items()},
    }
    with _LAYER_TXN_LOCK:
        atomic_write_json(project_dir / LAYER_TXN_JOURNAL_REL, journal)
        for rel, content in files.items():
            atomic_write_json(project_dir / rel, content)
        for rel, content in files.items():
            if canonical_revision(read_json(project_dir / rel, None)) != journal["hashes"][rel]:
                raise RuntimeError(
                    f"layer bundle publish verification failed for {rel}; "
                    "journal kept for recovery"
                )
        (project_dir / LAYER_TXN_JOURNAL_REL).unlink(missing_ok=True)


def load_layer_bundle(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    recover_layer_transaction(project_dir)
    layers = read_json(project_dir / LAYERS_REL, None) or {
        "schema_version": 1,
        "items": [],
    }
    visual_plan = read_json(project_dir / VISUAL_PLAN_REL, None) or {
        "schema_version": 1,
        "highlight_plan_revision": "0" * 64,
        "items": [],
        "revision": "0" * 64,
    }
    return layers, visual_plan


VARIANT_SNAPSHOTS_REL = Path("working/variant_snapshots")
VARIANT_DELIVERY_REL = Path("working/delivery_qa")


def state_variants(state: dict[str, Any]) -> list[dict[str, Any]]:
    variants = state.get("variants")
    return variants if isinstance(variants, list) else []


def find_variant(state: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
    for variant in state_variants(state):
        if str(variant.get("variant_id")) == variant_id:
            return variant
    return None


def variant_canvas(variant: dict[str, Any]) -> dict[str, Any]:
    """Canvas for a variant: platform preset dims, contain+pad by default.

    ``cover`` is only legal through an explicit frame override — that IS the
    manual reframe confirmation (plan v2: no smart reframe in 1c).
    """
    preset_id = str(variant.get("preset_id") or "")
    preset = PLATFORM_PRESETS.get(preset_id)
    if preset is None:
        raise ValueError(f"variant references an unknown platform preset: {preset_id}")
    fit = "contain"
    for override in variant.get("overrides") or []:
        if override.get("kind") == "frame" and override.get("path") == "canvas.fit":
            value = str(override.get("value"))
            if value not in {"contain", "cover"}:
                raise ValueError("canvas.fit override must be contain or cover")
            fit = value
    return {
        "platform_id": preset_id,
        "width": preset["width"],
        "height": preset["height"],
        "fps": preset["fps"],
        "fit": fit,
        "show_safe_zones": False,
    }


def variant_state_for(state: dict[str, Any], variant_id: str) -> dict[str, Any]:
    """Render-facing state for one variant: swapped canvas + typed overrides."""
    variant = find_variant(state, variant_id)
    if variant is None:
        raise ValueError(f"unknown variant: {variant_id}")
    shaped = json.loads(json.dumps(state))
    shaped["canvas"] = variant_canvas(variant)
    for override in variant.get("overrides") or []:
        kind = override.get("kind")
        path = str(override.get("path") or "")
        value = override.get("value")
        if kind == "caption_layout" and path in {"caption.x", "caption.y"}:
            key = path.split(".", 1)[1]
            for overlay in shaped.get("overlays", []):
                if overlay.get("type") in {"caption", "emphasis"} and not overlay.get("design_role"):
                    overlay.setdefault("style", {})[key] = value
        elif kind == "layout" and path in {"layer.x", "layer.y"}:
            continue  # structured card position overrides land in P4+ UI work
        elif kind == "frame" and path == "canvas.fit":
            continue  # already applied by variant_canvas
        else:
            raise ValueError(f"unsupported variant override: {kind}:{path}")
    return shaped


def compute_variant_snapshot(
    project_dir: Path,
    state: dict[str, Any],
    variant_id: str,
) -> dict[str, Any]:
    """Frozen render identity for a variant (plan v2 B2) — PURE computation.

    Pins every render input identity: editor state, layer bundle, evidence,
    resolved pack, canvas, caption plan content revision and compiler
    versions. Gates hash this; renders re-derive and compare.
    """
    import caption_compositor
    import structured_card_compositor

    shaped = variant_state_for(state, variant_id)
    layers, visual_plan = load_layer_bundle(project_dir)
    evidence = read_json(project_dir / "working/evidence_map.json", None)
    caption_identity = None
    if caption_compositor.compositor_available():
        caption_identity = caption_compositor.caption_content_revision(
            shaped, shaped.get("canvas") or {}, 1.0
        )
    snapshot = {
        "schema_version": 2,
        "variant_id": variant_id,
        "editor_state_revision": editor_state_revision(shaped),
        "structured_layers": canonical_revision(layers),
        "layer_hashes": {
            str(layer.get("id")): canonical_revision(layer)
            for layer in layers.get("items", [])
        },
        "visual_plan_v2": canonical_revision(visual_plan),
        "evidence_map_revision": (evidence or {}).get("revision"),
        "style_pack": shaped.get("style_pack"),
        "caption_content_revision": caption_identity,
        "compilers": {
            "caption": caption_compositor.engine_descriptor(),
            "structured": structured_card_compositor.capability_status(),
        },
        "canvas": shaped.get("canvas"),
    }
    snapshot["snapshot_hash"] = canonical_revision(snapshot)
    return snapshot


def persist_variant_snapshot(
    project_dir: Path, snapshot: dict[str, Any]
) -> dict[str, Any]:
    snapshot_dir = project_dir / VARIANT_SNAPSHOTS_REL
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(snapshot_dir / f"{snapshot['variant_id']}.json", snapshot)
    return snapshot


def build_variant_snapshot(
    project_dir: Path,
    state: dict[str, Any],
    variant_id: str,
) -> dict[str, Any]:
    return persist_variant_snapshot(
        project_dir, compute_variant_snapshot(project_dir, state, variant_id)
    )


def variant_gate_revision(
    project_dir: Path,
    gate: str,
    state: dict[str, Any],
    variant_id: str,
) -> str:
    snapshot = compute_variant_snapshot(project_dir, state, variant_id)
    if gate == "timeline":
        return snapshot["snapshot_hash"]
    if gate == "final":
        receipt = read_json(
            project_dir / VARIANT_DELIVERY_REL / f"{variant_id}.json", None
        )
        return canonical_revision(
            {"snapshot": snapshot["snapshot_hash"], "delivery_qa": receipt}
        )
    raise ValueError(f"gate {gate} has no variant dimension")


def variant_approval_entry(
    manifest: dict[str, Any], gate: str, variant_id: str
) -> dict[str, Any]:
    approvals = manifest.get("approvals", {})
    by_variant = approvals.get(f"{gate}_by_variant")
    if isinstance(by_variant, dict):
        entry = by_variant.get(variant_id)
        if isinstance(entry, dict):
            return entry
    return {}


def variant_approval_is_current(
    project_dir: Path,
    manifest: dict[str, Any],
    gate: str,
    state: dict[str, Any],
    variant_id: str,
) -> bool:
    entry = variant_approval_entry(manifest, gate, variant_id)
    if not entry.get("approved"):
        return False
    try:
        expected = variant_gate_revision(project_dir, gate, state, variant_id)
    except ValueError:
        return False
    return entry.get("state_revision") == expected


RIGHTS_REL = Path("working/rights_assertion.json")


def referenced_render_inputs(
    project_dir: Path, state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Closure of local render inputs for the rights gate (plan v2).

    Collects overlay asset sources, visual-plan selected assets, and any
    project-local font. System fonts (outside the project) and the owned
    source video are recorded but exempt; registry pack assets are exempt by
    their declared license.
    """
    inputs: dict[str, dict[str, Any]] = {}
    registry_problem = False
    try:
        provenance = asset_registry.load_registry(project_dir)
    except asset_registry.AssetRegistryError:
        provenance = {"schema_version": 1, "items": []}
        registry_problem = True
    provenance_by_path: dict[str, list[dict[str, Any]]] = {}
    for provenance_item in provenance["items"]:
        provenance_by_path.setdefault(str(provenance_item.get("path")), []).append(
            provenance_item
        )

    def add(path_text: str, kind: str) -> None:
        if not path_text:
            return
        candidate = (project_dir / path_text).resolve()
        try:
            inside = project_dir.resolve() in candidate.parents
        except OSError:
            inside = False
        if not inside or not candidate.is_file():
            return
        rel = candidate.relative_to(project_dir.resolve()).as_posix()
        requires = rel.startswith("assets/")
        generated_svg_png = (
            rel.startswith("assets/generated/svg/")
            and candidate.suffix.lower() == ".png"
        )
        digest = file_sha256(candidate)
        license_status = "exempt"
        if requires:
            license_status = "manual-assertion-required"
            path_items = provenance_by_path.get(rel, [])
            current = next(
                (item for item in path_items if item.get("sha256") == digest),
                None,
            )
            provider_items = [
                item for item in path_items if item.get("origin") == "provider"
            ]
            if current is not None and current.get("origin") == "provider":
                if asset_registry.provider_consistency_errors(project_dir, current):
                    license_status = "provider-license-invalid"
                else:
                    license_status = "provider-approved"
                    requires = False
            elif provider_items:
                license_status = "provider-provenance-stale"
            elif rel.startswith("assets/providers/"):
                license_status = (
                    "provider-provenance-invalid"
                    if registry_problem
                    else "provider-provenance-missing"
                )
            elif generated_svg_png:
                # Generated SVG PNGs are only publishable as the output of the
                # provider sanitizer/rasterizer pipeline.  Never let an
                # unregistered or non-provider item fall through to a manual
                # rights assertion: that would bypass the transformed-artifact
                # receipt gate.
                license_status = (
                    "provider-provenance-invalid"
                    if registry_problem or current is not None
                    else "provider-provenance-missing"
                )
        if rel not in inputs:
            inputs[rel] = {
                "path": rel,
                "kind": kind,
                "sha256": digest,
                "requires_assertion": requires,
                "license_status": license_status,
            }

    for overlay in state.get("overlays", []):
        if isinstance(overlay, dict) and overlay.get("type") in {"image", "gif", "video"}:
            add(str(overlay.get("source") or ""), "overlay-asset")
    template = state.get("video_template") or {}
    background = template.get("background") or {}
    if isinstance(background, dict) and background.get("asset"):
        add(str(background["asset"]), "template-background")
    layers, visual_plan = load_layer_bundle(project_dir)
    for item in visual_plan.get("items", []):
        if item.get("selected_asset"):
            add(str(item["selected_asset"]), "visual-plan-asset")
    for layer in layers.get("items", []):
        if not isinstance(layer, dict) or layer.get("type") != "mosaic":
            continue
        payload = layer.get("payload")
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if isinstance(asset, dict):
                add(str(asset.get("path") or ""), "structured-mosaic-asset")
    # Project fonts are receipt-bound render inputs, never manual-assertion
    # assets. Resolve each selected id strictly so stale/tampered receipts
    # cannot be smuggled through the rights gate by a matching family name.
    selected_font_ids: set[str] = set()
    defaults = state.get("caption_defaults")
    if isinstance(defaults, dict) and defaults.get("font_asset_id"):
        selected_font_ids.add(str(defaults["font_asset_id"]))
    for overlay in state.get("overlays", []):
        style = overlay.get("style") if isinstance(overlay, dict) and isinstance(overlay.get("style"), dict) else {}
        if style.get("font_asset_id"):
            selected_font_ids.add(str(style["font_asset_id"]))
    for asset_id in sorted(selected_font_ids):
        try:
            binding = asset_registry.resolve_project_font(project_dir, asset_id)
            path = str(binding.get("path") or "")
            sha256 = str(binding.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise asset_registry.AssetRegistryError("font resolver returned no SHA-256")
            inputs[path] = {
                "path": path,
                "kind": "font",
                "asset_id": asset_id,
                "sha256": sha256,
                "requires_assertion": False,
                "license_status": "provider-approved",
            }
        except asset_registry.AssetRegistryError:
            inputs[f"font:{asset_id}"] = {
                "path": f"font:{asset_id}", "kind": "font", "asset_id": asset_id,
                "sha256": "", "requires_assertion": True,
                "license_status": "provider-provenance-invalid",
            }
    return sorted(inputs.values(), key=lambda item: item["path"])


def rights_gate_errors(project_dir: Path, state: dict[str, Any]) -> list[str]:
    """Final gate: every render input that needs a rights assertion has a
    CURRENT one (sha-bound; a changed file voids its assertion)."""
    inputs = referenced_render_inputs(project_dir, state)
    errors: list[str] = []
    provider_current = [
        item for item in inputs if item.get("license_status") == "provider-approved"
    ]
    for item in inputs:
        status = item.get("license_status")
        if status in {
            "provider-license-invalid",
            "provider-provenance-stale",
            "provider-provenance-invalid",
            "provider-provenance-missing",
        }:
            errors.append(
                f"asset {item['path']} provider provenance or license is not current"
            )
    if provider_current:
        try:
            provenance = asset_registry.load_registry(project_dir)
        except asset_registry.AssetRegistryError:
            errors.append("asset provider provenance registry is invalid")
        else:
            errors.extend(asset_registry.attribution_errors(project_dir, provenance))

    required = [
        item
        for item in inputs
        if item["requires_assertion"]
        and item.get("license_status") == "manual-assertion-required"
    ]
    if not required:
        return errors
    assertion = read_json(project_dir / RIGHTS_REL, None)
    by_sha: dict[str, dict[str, Any]] = {}
    if isinstance(assertion, dict):
        for item in assertion.get("items", []):
            if item.get("asserted"):
                by_sha[str(item.get("asset_sha256"))] = item
    for item in required:
        matched = by_sha.get(item["sha256"])
        if matched is None:
            errors.append(
                f"asset {item['path']} needs a rights assertion before final "
                "(POST /api/rights/assert)"
            )
    return errors


def write_output_variant_set(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    variant_entries: list[dict[str, Any]],
) -> None:
    """Persist the output_variant_set contract instance (task 006 item 5)."""
    import contract_registry

    selection = state.get("style_pack") or {}
    pack_id = str(selection.get("project_default") or "")
    per_highlight = selection.get("per_highlight") or {}
    highlight_modes = []
    for highlight in state.get("highlights", []) or []:
        highlight_id = str(highlight.get("id"))
        override_pack_id = str(per_highlight.get(highlight_id) or "")
        highlight_modes.append(
            {
                "highlight_id": highlight_id,
                "mode_id": str(state.get("director_style") or "teacher-punch"),
                "mode_selection": "project-default",
                "style_pack": {
                    "id": override_pack_id or pack_id or "dark-data-presenter",
                    "selection": "user" if override_pack_id else "project-default",
                },
            }
        )
    variants = []
    for entry in variant_entries:
        if entry.get("timeline", {}).get("error"):
            variant_state = "failed"
        elif entry.get("final", {}).get("approved"):
            variant_state = "approved"
        elif entry.get("delivery"):
            variant_state = "final_rendered"
        elif entry.get("timeline", {}).get("approved"):
            variant_state = "preview_rendered"
        else:
            variant_state = "planned"
        source = find_variant(state, str(entry.get("variant_id"))) or {}
        variants.append(
            {
                "variant_id": f"variant-{canonical_revision({'v': entry.get('variant_id')})[:8]}",
                "highlight_id": "highlight-000000000000",
                "preset_id": str(entry.get("preset_id")),
                "state": variant_state,
                "overrides": [
                    {
                        "path": str(override.get("path")),
                        "kind": str(override.get("kind")),
                        "value": override.get("value"),
                    }
                    for override in source.get("overrides") or []
                ],
            }
        )
    preset_ids = {str(entry.get("preset_id")) for entry in variant_entries}
    artifact = {
        "schema_version": 1,
        "master_revision": editor_state_revision(state),
        "highlight_modes": highlight_modes,
        "outputs": [
            {
                "preset_id": preset_id,
                "orientation": (
                    "landscape"
                    if PLATFORM_PRESETS.get(preset_id, {}).get("width", 0)
                    > PLATFORM_PRESETS.get(preset_id, {}).get("height", 1)
                    else "portrait"
                ),
                "enabled": True,
            }
            for preset_id in sorted(preset_ids)
            if preset_id in PLATFORM_PRESETS
        ],
        "variants": variants,
    }
    artifact["revision"] = canonical_revision(
        {k: v for k, v in artifact.items() if k != "revision"}
    )
    errors = contract_registry.validate_artifact("output_variant_set", artifact)
    if errors:
        return  # informational artifact must never break the status route
    atomic_write_json(project_dir / "working/output_variant_set.json", artifact)


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
        errors.extend(rights_gate_errors(project_dir, state))
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


def visual_delivery_binding_errors(
    report: dict[str, Any],
    receipt: dict[str, Any],
    label: str,
) -> list[str]:
    """Schema-3 QA must bind passing renderer evidence into its receipt."""
    schema_version = report.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 3:
        # Schema 1/2 receipts remain readable; every new qa_video report is 3.
        return []
    visual = report.get("visual_delivery")
    if (
        not isinstance(visual, dict)
        or visual.get("schema_version") != 1
        or visual.get("source") != "renderer_evidence"
        or visual.get("status") != "pass"
        or isinstance(visual.get("visual_beat_count"), bool)
        or not isinstance(visual.get("visual_beat_count"), int)
        or isinstance(visual.get("expected_visual_beat_count"), bool)
        or not isinstance(visual.get("expected_visual_beat_count"), int)
        or visual.get("visual_beat_count") < 0
        or visual.get("visual_beat_count")
        != visual.get("expected_visual_beat_count")
    ):
        return [f"{label} visual delivery evidence must be a passing renderer report"]
    if receipt.get("visual_delivery") != visual:
        return [f"{label} visual delivery evidence does not match its QA report"]
    return []


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
        path, failure = verified_receipt_file(
            project_dir, relative, declared, scope, f"delivery QA {path_key}"
        )
        if failure:
            errors.append(failure)
            continue
        resolved[path_key] = path

    report = read_json(resolved.get("report", Path("/nonexistent")), None)
    if not isinstance(report, dict) or report.get("status") != "pass":
        errors.append("delivery QA report is missing a passing status")
    elif not isinstance(report.get("policy"), dict):
        errors.append(
            "delivery QA report predates the enforced QA policy; render the final again"
        )
    else:
        errors.extend(qa_profile_binding_errors(report, state, "delivery"))
        errors.extend(visual_delivery_binding_errors(report, receipt, "delivery QA"))
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
            path, failure = verified_receipt_file(
                project_dir, relative, declared, scope,
                f"batch item {clip_id or index} {path_key}",
            )
            if failure:
                errors.append(failure)
                continue
            resolved[path_key] = path

        report = read_json(resolved.get("report", Path("/nonexistent")), None)
        if not isinstance(report, dict) or report.get("status") != "pass":
            errors.append(f"batch item {clip_id or index} QA report is not passing")
        elif not isinstance(report.get("policy"), dict):
            errors.append(
                f"batch item {clip_id or index} QA report predates the enforced QA policy; render the batch again"
            )
        else:
            errors.extend(
                qa_profile_binding_errors(report, state, f"batch item {clip_id or index}")
            )
            errors.extend(
                visual_delivery_binding_errors(
                    report, item, f"batch item {clip_id or index}"
                )
            )
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


def ffprobe_visual_dimensions(path: Path) -> tuple[int, int] | None:
    """Decode-probe the first visual stream and return positive dimensions."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,width,height",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (AttributeError, json.JSONDecodeError):
        return None
    if not streams or streams[0].get("codec_type") != "video":
        return None
    width, height = streams[0].get("width"), streams[0].get("height")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        return None
    return width, height


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


MAX_CAPTION_EMPHASIS_SPANS = 3


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
    # A model that read the cut named its key terms and every one of them was
    # checked against what is spoken there. Those beat enumerating substrings
    # of a title, which yields fragments like a word cut in half.
    editorial_terms: list[str] = []
    for item in matching:
        editorial = item.get("editorial")
        if isinstance(editorial, dict) and editorial.get("is_editorial_copy"):
            editorial_terms.extend(
                str(term) for term in (editorial.get("keywords") or []) if str(term).strip()
            )
    if editorial_terms:
        return sorted(
            dict.fromkeys(editorial_terms),
            key=lambda term: (-len(term), term.casefold()),
        )
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
    max_spans: int = MAX_CAPTION_EMPHASIS_SPANS,
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
        if len(spans) >= max_spans:
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
        # Every keyword gets the same treatment. Alternating in a backdrop
        # marker meant the second term kept the base text colour, so however
        # many terms a line carried, exactly one of them ever read as
        # emphasised.
        effect = "pop"
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
    for span in spans:
        # Reading order used to decide the effect, so on any line the second
        # term became a backdrop marker that keeps the base text colour: two
        # keywords went in and one came out looking emphasised.
        if span.get("source") != "working/emphasis_plan.json":
            span["style"]["effect"] = "pop"
            span["style"]["font_scale"] = 1.18
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


BURNED_IN_SETTING_VALUES = ("auto", "yes", "no")


def caption_translations(project_dir: Path) -> dict[str, str]:
    """Spoken line -> second-line translation, empty when none was made."""
    try:
        import caption_translator
    except ImportError:
        return {}
    return caption_translator.by_caption_text(project_dir)


CARD_TITLE_MAX_CHARS = 40


def highlight_card_title(
    highlight: dict[str, Any], *, editorial_only: bool = False
) -> str:
    """What this cut's card is called. One answer, for every card path.

    Two paths build title cards, and each used to work out the fallback for
    itself: one ended at a quote from the transcript, the other at the
    highlight's own title, and they truncated at different lengths — so the
    same cut was named two different things depending on which path drew it.
    """
    if not isinstance(highlight, dict):
        return ""
    editorial = highlight.get("editorial")
    if isinstance(editorial, dict) and editorial.get("is_editorial_copy"):
        title = str(editorial.get("title") or "").strip()
        if title:
            return title[:CARD_TITLE_MAX_CHARS]
    if editorial_only:
        # The director's title card needs a written name, not an excerpt: a
        # KTV clip shipped its own mis-heard transcript as a prominent card.
        # The designed-deck path keeps the fallback below — its hook slide
        # needs some text, and a person reviews that deck before it ships.
        return ""
    return str(highlight.get("title") or "").strip()[:CARD_TITLE_MAX_CHARS]


def active_editorial_title(state: dict[str, Any]) -> str:
    """The name of the cut being worked on, by the shared rule."""
    active_id = str(state.get("active_highlight_id") or "")
    for item in state.get("highlights", []):
        if not isinstance(item, dict):
            continue
        if active_id and str(item.get("id") or "") != active_id:
            continue
        return highlight_card_title(item, editorial_only=True)
    return ""


def caption_render_decision(
    project_dir: Path, manifest: dict[str, Any]
) -> tuple[bool, str]:
    """Should this project draw its own captions? Returns (render, reason).

    Footage that already carries burned-in subtitles gets a second, usually
    worse, transcript stacked on top of a correct one. The analysis stage
    reports what it saw; the project setting always wins over it.
    """
    subtitles = manifest.get("subtitles")
    setting = "auto"
    if isinstance(subtitles, dict):
        setting = str(subtitles.get("source_has_burned_in") or "auto").lower()
    if setting not in BURNED_IN_SETTING_VALUES:
        setting = "auto"
    if setting == "yes":
        return False, "project declares the source is already subtitled"
    if setting == "no":
        return True, "project declares the source carries no subtitles"

    analysis_path = project_dir / "working/video_analysis.json"
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, "no analysis yet; assuming the source is unsubtitled"
    detection = analysis.get("burned_in_captions")
    if not isinstance(detection, dict):
        return True, "analysis predates burned-in detection"
    status = str(detection.get("status") or "")
    if status == "detected":
        hits = detection.get("frames_with_band_text")
        sampled = detection.get("frames_sampled")
        return False, (
            f"source already shows subtitles in {hits}/{sampled} sampled frames"
        )
    if status == "not_configured":
        return True, "OCR unavailable; cannot tell, so captions stay on"
    return True, "no burned-in subtitles found in the source"


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
            "editorial": item.get("editorial"),
            "review_status": str(item.get("review_status", "pending")),
            "score": item.get("score"),
            "source": "working/highlight_plan.json",
        }
        for item in highlight_plan.get("items", [])[:10]
        if isinstance(item, dict)
    ]
    keyword_state = {"highlights": highlights, "overlays": []}
    overlays: list[dict[str, Any]] = []
    # Whisper returns as few as one segment for a whole clip, which as a
    # caption is an unreadable wall of text. The transcript carries a reading
    # split alongside it; the sync path already prefers it and this one must
    # agree, or the first caption a project gets depends on which code made it.
    caption_source = transcript.get("caption_segments") or transcript.get("segments", [])
    render_captions, caption_reason = caption_render_decision(project_dir, manifest)
    translations = caption_translations(project_dir)
    if not render_captions:
        caption_source = []
    for index, segment in enumerate(caption_source, start=1):
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
                **({"translation": translations[text]} if text in translations else {}),
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
        "caption_generation": {
            "enabled": render_captions,
            "reason": caption_reason,
        },
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
        "style_pack": {
            "project_default": DEFAULT_STYLE_PACK_BY_DIRECTOR.get(
                director_id, "dark-data-presenter"
            ),
            "per_highlight": {},
        },
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


QA_PROFILES = ("strict", "silent_delivery", "long_pause_delivery")


def qa_policy_errors(declared: Any) -> list[str]:
    """Validate the delivery-kind declaration as a closed set.

    The surrounding state schema tolerates unknown keys; this one must not,
    because everything it can say loosens a gate.
    """
    if declared is None:
        return []
    if not isinstance(declared, dict):
        return ["qa_policy must be an object"]
    unknown = sorted(set(declared) - {"profile", "intent"})
    if unknown:
        return [f"qa_policy has unsupported fields: {', '.join(unknown)}"]
    profile = declared.get("profile")
    if profile not in QA_PROFILES:
        return [f"qa_policy.profile must be one of {', '.join(QA_PROFILES)}"]
    intent = declared.get("intent", "")
    if not isinstance(intent, str):
        return ["qa_policy.intent must be a string"]
    if profile != "strict" and not intent.strip():
        return [f"qa_policy.profile {profile} requires a non-empty intent"]
    return []


def authorized_qa_profile(state: dict[str, Any] | None) -> str:
    """The delivery kind the current state authorizes."""
    declared = (state or {}).get("qa_policy")
    if not isinstance(declared, dict):
        return "strict"
    profile = declared.get("profile")
    return profile if profile in QA_PROFILES else "strict"


def qa_profile_binding_errors(
    report: dict[str, Any], state: dict[str, Any] | None, label: str
) -> list[str]:
    """The report must have run under the thresholds the state authorizes.

    Comparing a report against a receipt only shows that two mutable files
    agree; neither is the authority, and the profile a report names is just
    another field it can carry. What is checked here is the effective policy
    the run actually applied.
    """
    declared = (state or {}).get("qa_policy")
    declared = declared if isinstance(declared, dict) else {}
    authorized = authorized_qa_profile(state)
    policy = report.get("policy")
    if not isinstance(policy, dict):
        return [f"{label} QA report does not record the thresholds it applied"]

    if int(report.get("schema_version") or 1) < 2:
        # Profiles did not exist when these were written, so the only policy
        # they can legitimately carry is the strict one. Reject a report that
        # claims to predate profiles while carrying a relaxation.
        if authorized != "strict":
            return [
                f"{label} QA report predates delivery profiles but this project "
                f"authorizes {authorized!r}; render again"
            ]
        if policy.get("allow_missing_audio") or policy.get("allow_silent_delivery"):
            return [f"{label} QA report claims to predate profiles but relaxes audio checks"]
        return []

    expected = dataclasses.asdict(
        qa_video.QaPolicy.for_profile(authorized, str(declared.get("intent", "")))
    )
    mismatched = sorted(
        name
        for name, value in expected.items()
        if name not in {"profile", "intent"} and policy.get(name) != value
    )
    if mismatched:
        return [
            f"{label} QA report applied thresholds this project does not authorize "
            f"({', '.join(mismatched)}); render again"
        ]
    if report.get("profile") != authorized:
        return [
            f"{label} QA report is labelled {report.get('profile')!r} but ran under "
            f"{authorized!r} thresholds"
        ]
    return []


qa_policy_args = qa_video.qa_policy_args


_AUDIO_EDIT_KEYS = {
    "schema_version",
    "source_render_id",
    "source_plan_sha256",
    "source_timeline_revision",
    "events",
}
_AUDIO_EDIT_EVENT_KEYS = {
    "id",
    "source_event_sha256",
    "event_start_sample",
    "gain_db",
}
_AUDIO_EDIT_RENDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_AUDIO_EDIT_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_AUDIO_EDIT_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_audio_event_edits(value: Any) -> list[str]:
    """Validate the closed, sparse Studio audio override envelope."""
    if value is None:
        return []
    if type(value) is not dict:
        return ["audio_event_edits must be a built-in JSON object"]
    errors: list[str] = []
    unknown = sorted(set(value) - _AUDIO_EDIT_KEYS)
    missing = sorted(_AUDIO_EDIT_KEYS - set(value))
    if unknown:
        errors.append(f"audio_event_edits has unsupported fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"audio_event_edits is missing fields: {', '.join(missing)}")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("audio_event_edits.schema_version must be the integer 1")
    render_id = value.get("source_render_id")
    if type(render_id) is not str or _AUDIO_EDIT_RENDER_ID.fullmatch(render_id) is None:
        errors.append("audio_event_edits.source_render_id is invalid")
    for field in ("source_plan_sha256", "source_timeline_revision"):
        digest = value.get(field)
        if type(digest) is not str or _AUDIO_EDIT_SHA256.fullmatch(digest) is None:
            errors.append(f"audio_event_edits.{field} must be lowercase sha256")
    events = value.get("events")
    if type(events) is not list or not events:
        errors.append("audio_event_edits.events must be a non-empty sparse list")
        return errors
    seen: set[str] = set()
    previous_id = ""
    for index, event in enumerate(events):
        label = f"audio_event_edits.events[{index}]"
        if type(event) is not dict:
            errors.append(f"{label} must be a built-in JSON object")
            continue
        event_unknown = sorted(set(event) - _AUDIO_EDIT_EVENT_KEYS)
        event_missing = sorted(_AUDIO_EDIT_EVENT_KEYS - set(event))
        if event_unknown:
            errors.append(f"{label} has unsupported fields: {', '.join(event_unknown)}")
        if event_missing:
            errors.append(f"{label} is missing fields: {', '.join(event_missing)}")
        event_id = event.get("id")
        if type(event_id) is not str or _AUDIO_EDIT_ID.fullmatch(event_id) is None:
            errors.append(f"{label}.id is invalid")
        elif event_id in seen:
            errors.append(f"{label}.id is duplicated")
        elif previous_id and event_id <= previous_id:
            errors.append("audio_event_edits.events must be ordered by id")
        if type(event_id) is str:
            seen.add(event_id)
            previous_id = event_id
        digest = event.get("source_event_sha256")
        if type(digest) is not str or _AUDIO_EDIT_SHA256.fullmatch(digest) is None:
            errors.append(f"{label}.source_event_sha256 must be lowercase sha256")
        start = event.get("event_start_sample")
        if type(start) is not int or start < 0:
            errors.append(f"{label}.event_start_sample must be a non-negative integer")
        gain = event.get("gain_db")
        if (
            isinstance(gain, bool)
            or not isinstance(gain, (int, float))
            or not math.isfinite(float(gain))
            or not -24.0 <= float(gain) <= -6.0
        ):
            errors.append(f"{label}.gain_db must be finite and between -24 and -6 dB")
    return errors


class AudioEventEditError(ValueError):
    """A Studio audio source or override failed its trust contract."""


class EditorRevisionConflict(ValueError):
    """The editor-state compare-and-swap authority is stale."""


def _finalized_audio_plan(
    project_dir: Path, render_id: str
) -> tuple[dict[str, Any], str]:
    """Load exact plan bytes through their repo-owned finalized envelope."""
    if _AUDIO_EDIT_RENDER_ID.fullmatch(render_id) is None:
        raise AudioEventEditError("audio event source render id is invalid")
    envelope_rel = f"working/delivery_envelopes/{render_id}.json"
    try:
        envelope, envelope_snapshot = delivery_envelope.snapshot_owned_json(
            project_dir, envelope_rel, label="finalized audio delivery envelope"
        )
        if envelope_snapshot.path.name != f"{render_id}.json":
            raise AudioEventEditError("audio event envelope path is aliased")
        if envelope.get("render_id") != render_id:
            raise AudioEventEditError("audio event envelope render id does not match")
        delivery_envelope.validate_envelope(
            project_dir, envelope, expected_state="finalized"
        )
        artifact = (envelope.get("artifacts") or {}).get("audio_event_plan")
        if not isinstance(artifact, dict):
            raise AudioEventEditError("finalized delivery has no audio event plan")
        expected_rel = f"working/audio_event_plans/{render_id}.json"
        if artifact.get("path") != expected_rel:
            raise AudioEventEditError("audio event plan path is not canonical for render id")
        plan, plan_snapshot = delivery_envelope.snapshot_owned_json(
            project_dir, expected_rel, label="finalized audio event plan"
        )
    except (delivery_envelope.DeliveryEnvelopeError, OSError, ValueError) as exc:
        if isinstance(exc, AudioEventEditError):
            raise
        raise AudioEventEditError(f"finalized audio source is invalid: {exc}") from exc
    declared_hash = artifact.get("sha256")
    if plan_snapshot.sha256 != declared_hash:
        raise AudioEventEditError("finalized audio plan hash does not match envelope")
    errors = contract_registry.validate_artifact("audio_event_plan", plan)
    if errors:
        raise AudioEventEditError(f"finalized audio plan is invalid: {errors[0]}")
    if plan.get("schema_version") != 2:
        raise AudioEventEditError("only finalized audio plan schema v2 is editable")
    if plan.get("studio_edits") is not None:
        raise AudioEventEditError("an already edited audio plan cannot become a new edit source")
    try:
        delivery_envelope.revalidate_file_snapshot(plan_snapshot)
        delivery_envelope.revalidate_file_snapshot(envelope_snapshot)
    except delivery_envelope.DeliveryEnvelopeError as exc:
        raise AudioEventEditError("finalized audio source changed while being read") from exc
    return plan, plan_snapshot.sha256


def resolve_audio_event_source(
    project_dir: Path, state: dict[str, Any]
) -> tuple[str, dict[str, Any], str]:
    """Resolve one unambiguous finalized base plan for current visual state."""
    edits = state.get("audio_event_edits")
    base_revision = editor_base_state_revision(state)
    if isinstance(edits, dict):
        render_id = str(edits.get("source_render_id") or "")
        plan, plan_hash = _finalized_audio_plan(project_dir, render_id)
        if plan_hash != edits.get("source_plan_sha256"):
            raise AudioEventEditError("audio event source plan hash changed")
        if plan.get("timeline_revision") != edits.get("source_timeline_revision"):
            raise AudioEventEditError("audio event source timeline binding changed")
        if edits.get("source_timeline_revision") != base_revision:
            raise AudioEventEditError("audio event edits are stale for current visual state")
        return render_id, plan, plan_hash

    envelope_dir = project_dir / "working/delivery_envelopes"
    try:
        metadata = envelope_dir.lstat()
    except FileNotFoundError as exc:
        raise AudioEventEditError("no finalized audio plan exists for current timeline") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AudioEventEditError("finalized delivery envelope directory is unsafe")
    candidates: list[tuple[str, dict[str, Any], str]] = []
    unsafe: list[str] = []
    for entry in sorted(envelope_dir.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or entry.suffix != ".json":
            continue
        render_id = entry.stem
        try:
            plan, plan_hash = _finalized_audio_plan(project_dir, render_id)
        except AudioEventEditError as exc:
            unsafe.append(str(exc))
            continue
        if plan.get("timeline_revision") == base_revision:
            candidates.append((render_id, plan, plan_hash))
    if not candidates:
        reason = unsafe[0] if unsafe else "no finalized audio plan exists for current timeline"
        raise AudioEventEditError(reason)
    hashes = {item[2] for item in candidates}
    if len(hashes) != 1:
        raise AudioEventEditError("multiple distinct finalized audio plans match current timeline")
    return candidates[0]


def resolve_studio_audio_plan(
    source_plan: dict[str, Any], edits: dict[str, Any]
) -> dict[str, Any]:
    """Apply sparse edits and revalidate all final-domain invariants."""
    structural_errors = validate_audio_event_edits(edits)
    if structural_errors:
        raise AudioEventEditError(structural_errors[0])
    plan = json.loads(json.dumps(source_plan, allow_nan=False))
    events = plan.get("events")
    if not isinstance(events, list):
        raise AudioEventEditError("audio event source has no events")
    by_id = {
        event.get("id"): event for event in events if isinstance(event, dict)
    }
    edit_ids = [event["id"] for event in edits["events"]]
    source_order = [event.get("id") for event in events if event.get("id") in set(edit_ids)]
    if edit_ids != source_order:
        raise AudioEventEditError("audio event edits do not follow source event order")
    for edit in edits["events"]:
        event = by_id.get(edit["id"])
        if not isinstance(event, dict):
            raise AudioEventEditError(f"audio event source id is stale: {edit['id']}")
        if contract_registry.canonical_hash(event) != edit["source_event_sha256"]:
            raise AudioEventEditError(f"audio event source hash is stale: {edit['id']}")
        if (
            event.get("event_start_sample") == edit["event_start_sample"]
            and event.get("gain_db") == edit["gain_db"]
        ):
            raise AudioEventEditError(f"audio event edit has no change: {edit['id']}")
        event["event_start_sample"] = edit["event_start_sample"]
        event["gain_db"] = edit["gain_db"]
        event["expected_transient_sample"] = (
            edit["event_start_sample"] + event["asset_transient_anchor_sample"]
        )
    plan["studio_edits"] = json.loads(json.dumps(edits, allow_nan=False))
    plan["studio_edits_sha256"] = audio_event_edits_hash(edits)
    errors = contract_registry.validate_artifact("audio_event_plan", plan)
    if errors:
        raise AudioEventEditError(
            "resolved audio event plan is invalid: " + "; ".join(errors)
        )
    return plan


def audio_event_timeline(project_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Build the read-only Studio audio timeline projection."""
    try:
        render_id, source, plan_hash = resolve_audio_event_source(project_dir, state)
        edits = state.get("audio_event_edits")
        resolved = resolve_studio_audio_plan(source, edits) if isinstance(edits, dict) else source
    except AudioEventEditError as exc:
        return {
            "editable": False,
            "status": "unavailable",
            "reason": str(exc),
            "events": [],
        }
    edit_by_id = {
        item["id"]: item for item in (edits or {}).get("events", [])
    }
    source_by_id = {item["id"]: item for item in source["events"]}
    return {
        "editable": True,
        "status": "edited" if edits else "ready",
        "reason": None,
        "source_render_id": render_id,
        "source_plan_sha256": plan_hash,
        "source_timeline_revision": source["timeline_revision"],
        "studio_edits_sha256": audio_event_edits_hash(edits),
        "events": [
            {
                **event,
                "source_event_sha256": contract_registry.canonical_hash(source_by_id[event["id"]]),
                "edited": event["id"] in edit_by_id,
            }
            for event in resolved["events"]
        ],
    }


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
    errors.extend(qa_policy_errors(state.get("qa_policy")))
    errors.extend(validate_audio_event_edits(state.get("audio_event_edits")))
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
    variants = state.get("variants")
    if not isinstance(variants, list):
        errors.append("variants must be a list")
    else:
        seen_variants: set[str] = set()
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                errors.append(f"variant {index} must be an object")
                continue
            variant_key = str(variant.get("variant_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", variant_key):
                errors.append(f"variant {index} variant_id is invalid")
            elif variant_key in seen_variants:
                errors.append(f"variant id duplicated: {variant_key}")
            seen_variants.add(variant_key)
            if str(variant.get("preset_id") or "") not in PLATFORM_PRESETS:
                errors.append(f"variant {variant_key} references an unknown preset")
            for override in variant.get("overrides") or []:
                if not isinstance(override, dict) or override.get("kind") not in {
                    "layout",
                    "caption_layout",
                    "frame",
                }:
                    errors.append(f"variant {variant_key} has an unsupported override")
    rights = state.get("rights")
    if not isinstance(rights, dict) or not isinstance(rights.get("asserted"), bool):
        errors.append("rights must be an object with a boolean asserted flag")
    style_pack = state.get("style_pack")
    if style_pack is not None:
        import structured_card_compositor

        known_packs = set(structured_card_compositor.style_pack_ids())
        if not isinstance(style_pack, dict):
            errors.append("style_pack must be an object")
        else:
            project_default = style_pack.get("project_default")
            if project_default is not None and project_default not in known_packs:
                errors.append(f"unknown style pack: {project_default}")
            per_highlight = style_pack.get("per_highlight") or {}
            if not isinstance(per_highlight, dict):
                errors.append("style_pack.per_highlight must be an object")
            else:
                for pack_id in per_highlight.values():
                    if pack_id not in known_packs:
                        errors.append(f"unknown style pack: {pack_id}")
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
    caption_defaults = state.get("caption_defaults")
    if caption_defaults is not None and not isinstance(caption_defaults, dict):
        errors.append("caption_defaults must be an object")
    elif isinstance(caption_defaults, dict) and caption_defaults.get("font_asset_id") is not None:
        font_asset_id = caption_defaults["font_asset_id"]
        if not isinstance(font_asset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", font_asset_id):
            errors.append("caption_defaults font_asset_id is invalid")
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
        if style.get("font_asset_id") is not None and (
            not isinstance(style["font_asset_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", style["font_asset_id"])
        ):
            errors.append(f"overlay {overlay_id or index} style font_asset_id is invalid")
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
        resolved_project = project_dir.resolve()
        # Controlled initialization is an explicit local mutation boundary.
        # Complete exact legacy provenance migration before binding the HTTP
        # server so no GET/HEAD request can become a migration trigger.
        asset_registry.migrate_legacy_registry(resolved_project)
        super().__init__(address, EditorHandler)
        self.project_dir = resolved_project
        # Per-server CSRF token; browsers learn it from GET /api/project and
        # must echo it on every mutation. This blocks cross-origin browser
        # requests only — local processes reading loopback are outside this
        # threat model (contracts/policies/DOWNLOAD_GATE.md).
        self.csrf_token = secrets.token_urlsafe(32)
        self.asset_provider_service = AssetProviderService(self.project_dir)
        self.caption_job_lock = threading.Lock()
        self.caption_render_serial = threading.Lock()
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
            # Serialise read→render→publish: two overlapping saves would
            # otherwise let the older render publish last (Codex review M3).
            with self.caption_render_serial:
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
                state, state.get("canvas") or {}, 1.0, project
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
            {
                "ok": True,
                "engine": engine,
                "job": job,
                "ready": ready,
                "items": items,
                "caption_revision": (plan or {}).get("caption_revision")
                if isinstance(plan, dict)
                else None,
            }
        )

    def handle_font_info(self, project: Path) -> None:
        import caption_compositor
        plan = read_json(project / "working/caption_render_plan.json", None)
        receipt = plan.get("receipt", {}) if isinstance(plan, dict) else {}
        with self.server.project_lock:
            state = read_json(project / STATE_REL, {}) or {}
        try:
            fonts = asset_registry.list_project_fonts(project)
        except asset_registry.AssetRegistryError:
            # A broken registry must not turn into a selectable-looking list.
            fonts = []
        selected_id = ""
        defaults = state.get("caption_defaults")
        if isinstance(defaults, dict):
            selected_id = str(defaults.get("font_asset_id") or "")
        public_fonts = [
            {
                key: item.get(key)
                for key in (
                    "asset_id", "family", "style", "weight", "coverage", "scripts",
                    "license", "license_spdx", "provider_id", "sha256", "availability",
                )
            }
            for item in fonts
        ]
        selected = next((item for item in public_fonts if str(item.get("asset_id")) == selected_id), None)
        try:
            from render_editor_timeline import font_path
            resolved = str(font_path())
        except ValueError:
            resolved = ""
        self.send_json(
            {
                "ok": True,
                "engine": caption_compositor.engine_descriptor(),
                # This endpoint deliberately exposes only manifest metadata,
                # never provider download URLs, receipt internals, or paths.
                "fonts": public_fonts,
                "selected": selected,
                "selection_status": (
                    "verified" if selected is not None else ("unavailable" if selected_id else "legacy")
                ),
                "legacy": {
                    "current": Path(resolved).name if resolved else "",
                    "status": "unverified-legacy",
                },
                "sanctioned_fallbacks": sorted(
                    caption_compositor.SANCTIONED_FALLBACK_PS_NAMES
                ),
                "caption_font_asset_ids": sorted(
                    str(asset_id) for asset_id in (receipt.get("fonts", {}) or {})
                ),
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
        try:
            if file_sha256(artifact) != artifact_hash:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # Immutable content-addressed URL: safe to cache forever (hash just
        # re-verified against the bytes on disk).
        self._cache_immutable = True
        try:
            self.serve_file(artifact)
        finally:
            self._cache_immutable = False

    CAPTION_STYLE_KEYS = {
        "color", "font_size", "stroke_color", "stroke_width", "box", "box_color",
        "max_width", "animation", "font_weight", "font_family", "font_asset_id", "emphasis_color",
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
            return self._apply_caption_style_locked(state, scope, overlay_id, style_patch)

    def _apply_caption_style_locked(
        self,
        state: dict[str, Any],
        scope: str,
        overlay_id: str,
        style_patch: dict[str, Any],
    ) -> None:
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
            if not highlight:
                # No highlight scope on the target: an implicit "all unscoped
                # captions" match would be surprising — degrade to single.
                recipients = [target]
            else:
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

    def handle_structured_layers(self) -> None:
        """Transactional CRUD for structured layers + their visual-plan items.

        Timing lives ONLY on the visual_plan item (§7.4.10 SSOT); the server
        creates/updates both sides in one journaled transaction and rebuilds
        the artifact receipts, so a half-written bundle can never be seen.
        """
        import structured_card_compositor

        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        action = str(body.get("action") or "upsert")
        if action not in {"list", "upsert", "delete", "asset_beat"}:
            self.send_json({"ok": False, "error": "unsupported action"}, status=422)
            return
        with self.server.project_lock:
            layers, visual_plan = load_layer_bundle(self.server.project_dir)
            if action == "list":
                self.send_json(
                    {"ok": True, "layers": layers, "visual_plan": visual_plan}
                )
                return
            items = {item["id"]: item for item in layers.get("items", [])}
            plan_items = {
                item.get("structured_layer_id"): item
                for item in visual_plan.get("items", [])
                if item.get("structured_layer_id")
            }
            if action == "asset_beat":
                asset_rel = str((body.get("asset") or {}).get("path") or "")
                timing = body.get("timing") or {}
                try:
                    scoped_project_path(self.server.project_dir, asset_rel, "assets")
                except ValueError:
                    self.send_json({"ok": False, "error": "asset path invalid"}, status=422)
                    return
                if not (self.server.project_dir / asset_rel).is_file():
                    self.send_json({"ok": False, "error": "asset not found"}, status=404)
                    return
                beat_kind = str((body.get("asset") or {}).get("beat") or "image")
                if beat_kind not in {"image", "broll"}:
                    self.send_json({"ok": False, "error": "beat must be image/broll"}, status=422)
                    return
                manifest_now = read_json(self.server.project_dir / "project.json", {}) or {}
                duration_now = float(manifest_now.get("source", {}).get("duration_s") or 0.0)
                try:
                    beat_start = float(timing.get("start"))
                    beat_end = float(timing.get("end"))
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "timing must be numeric"}, status=422)
                    return
                if (
                    not math.isfinite(beat_start)
                    or not math.isfinite(beat_end)
                    or beat_start < 0
                    or beat_end <= beat_start
                    or (duration_now and beat_end > duration_now + 0.05)
                ):
                    self.send_json(
                        {"ok": False, "error": "timing must satisfy 0 <= start < end <= duration"},
                        status=422,
                    )
                    return
                seed = canonical_revision({"asset-beat": asset_rel, "n": len(visual_plan.get("items", []))})
                visual_plan.setdefault("items", []).append(
                    {
                        "id": f"visual-beat-{seed[:12]}",
                        "highlight_id": str(timing.get("highlight_id") or "highlight-000000000000"),
                        "start": beat_start,
                        "end": beat_end,
                        "beat": beat_kind,
                        "structured_layer_id": None,
                        "selected_asset": asset_rel,
                        "conceptual_only": True,
                        "evidence_ids": [],
                        "review_status": "pending",
                    }
                )
            elif action == "delete":
                layer_id = str(body.get("id") or "")
                if layer_id not in items:
                    self.send_json({"ok": False, "error": "layer not found"}, status=404)
                    return
                items.pop(layer_id)
                plan_items.pop(layer_id, None)
                visual_plan["items"] = [
                    item
                    for item in visual_plan.get("items", [])
                    if item.get("structured_layer_id") != layer_id
                ]
            else:
                payload = body.get("layer") or {}
                timing = body.get("timing") or {}
                layer_id = str(body.get("id") or "")
                if not layer_id:
                    seed = canonical_revision({"new-layer": now_utc(), "n": len(items)})
                    layer_id = f"structured-layer-{seed[:12]}"
                beat_id = f"visual-beat-{layer_id.rsplit('-', 1)[-1]}"
                existing = items.get(layer_id)
                layer_type = str(payload.get("type") or (existing or {}).get("type") or "")
                envelope = {
                    "id": layer_id,
                    "visual_plan_item_id": beat_id,
                    "type": layer_type,
                    "revision": int((existing or {}).get("revision", 0)) + 1,
                    "evidence_revision": str(
                        body.get("evidence_revision")
                        or (existing or {}).get("evidence_revision")
                        or "0" * 64
                    ),
                    "payload": payload.get("payload") or {},
                    "review_status": str(payload.get("review_status") or "pending"),
                }
                items[layer_id] = envelope
                evidence_ids: list[str] = []
                if layer_type == "stat":
                    evidence_ids = [str(envelope["payload"].get("evidence_id") or "")]
                elif layer_type == "chart":
                    evidence_ids = [
                        str(datum.get("evidence_id") or "")
                        for datum in envelope["payload"].get("datums", [])
                    ]
                evidence_ids = [e for e in evidence_ids if e]
                plan_item = plan_items.get(layer_id) or {
                    "id": beat_id,
                    "highlight_id": str(timing.get("highlight_id") or "highlight-000000000000"),
                    "beat": layer_type,
                    "structured_layer_id": layer_id,
                    "selected_asset": None,
                    "review_status": "pending",
                }
                plan_item.update(
                    {
                        "start": float(timing.get("start", plan_item.get("start", 0.0))),
                        "end": float(timing.get("end", plan_item.get("end", 0.0))),
                        "beat": layer_type,
                        "conceptual_only": not evidence_ids,
                        "evidence_ids": evidence_ids,
                    }
                )
                if plan_item.get("id") not in {
                    item.get("id") for item in visual_plan.get("items", [])
                }:
                    visual_plan.setdefault("items", []).append(plan_item)
            layers["items"] = list(items.values())
            visual_plan["revision"] = canonical_revision(
                {k: v for k, v in visual_plan.items() if k != "revision"}
            )
            try:
                publish_layer_bundle(self.server.project_dir, layers, visual_plan)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=422)
                return
            artifacts = None
            capability = structured_card_compositor.capability_status()
            if capability["status"] == "static_fallback" and layers["items"]:
                state = read_json(self.server.project_dir / STATE_REL, {}) or {}
                try:
                    artifacts = structured_card_compositor.build_structured_artifacts(
                        self.server.project_dir,
                        state,
                        layers,
                        structured_card_compositor.load_default_pack(),
                        1.0,
                    )
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=422)
                    return
        self.send_json(
            {
                "ok": True,
                "capability": capability,
                "layers": layers,
                "visual_plan": visual_plan,
                "artifacts": artifacts,
                "note": "timeline/final approvals are stale until re-confirmed",
            }
        )

    def handle_variant_approval(
        self, gate: str, variant_id: str, body: dict[str, Any]
    ) -> None:
        with self.server.project_lock:
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            if find_variant(state, variant_id) is None:
                self.send_json({"ok": False, "error": "unknown variant"}, status=404)
                return
            if gate == "final":
                rights_errors = rights_gate_errors(self.server.project_dir, state)
                if rights_errors:
                    self.send_json(
                        {"ok": False, "errors": rights_errors}, status=409
                    )
                    return
            try:
                expected = variant_gate_revision(
                    self.server.project_dir, gate, state, variant_id
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=422)
                return
            if str(body.get("expected_revision") or "") != expected:
                self.send_json(
                    {
                        "ok": False,
                        "error": "approval revision is stale",
                        "current_revision": expected,
                    },
                    status=409,
                )
                return
            approvals = manifest.setdefault("approvals", {})
            slot = approvals.setdefault(f"{gate}_by_variant", {})
            slot[variant_id] = {
                "approved": True,
                "state_revision": expected,
                "confirmed_by": str(body.get("confirmed_by") or "editor"),
                "at": now_utc(),
            }
            manifest["updated_at"] = now_utc()
            atomic_write_json(self.server.project_dir / "project.json", manifest)
        self.send_json(
            {
                "ok": True,
                "gate": gate,
                "variant_id": variant_id,
                "approval": slot[variant_id],
            }
        )

    def handle_rights_status(self, project: Path) -> None:
        with self.server.project_lock:
            state = read_json(project / STATE_REL, {}) or {}
            inputs = referenced_render_inputs(project, state)
            assertion = read_json(project / RIGHTS_REL, None)
        asserted = {
            str(item.get("asset_sha256"))
            for item in (assertion or {}).get("items", [])
            if item.get("asserted")
        }
        for item in inputs:
            item["asserted"] = item["sha256"] in asserted
        self.send_json({"ok": True, "inputs": inputs})

    def handle_rights_assert(self) -> None:
        """Record a hash-bound rights assertion for one render input."""
        import contract_registry

        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        basis = str(body.get("basis") or "")
        if basis not in {"own_work", "licensed", "public_domain", "other"}:
            self.send_json({"ok": False, "error": "invalid basis"}, status=422)
            return
        raw_path = str(body.get("asset_path") or "")
        with self.server.project_lock:
            try:
                asset = scoped_project_path(self.server.project_dir, raw_path, "assets")
            except ValueError:
                self.send_json({"ok": False, "error": "asset path is invalid"}, status=422)
                return
            if not asset.is_file():
                self.send_json({"ok": False, "error": "asset not found"}, status=404)
                return
            digest = file_sha256(asset)
            assertion = read_json(self.server.project_dir / RIGHTS_REL, None) or {
                "schema_version": 1,
                "items": [],
            }
            assertion["items"] = [
                item
                for item in assertion.get("items", [])
                if item.get("asset_sha256") != digest
            ]
            assertion["items"].append(
                {
                    "asset_id": f"asset-{digest[:16]}",
                    "asserted": True,
                    "asserted_by": str(body.get("asserted_by") or "nat"),
                    "asserted_at": now_utc(),
                    "basis": basis,
                    "note": str(body.get("note") or ""),
                    "asset_sha256": digest,
                    "provenance_revision": None,
                    "license_proof": str(body.get("license_proof") or "") or None,
                }
            )
            assertion["revision"] = canonical_revision(
                {k: v for k, v in assertion.items() if k != "revision"}
            )
            errors = contract_registry.validate_artifact("rights_assertion", assertion)
            if errors:
                self.send_json({"ok": False, "errors": errors}, status=422)
                return
            atomic_write_json(self.server.project_dir / RIGHTS_REL, assertion)
        self.send_json({"ok": True, "asset_sha256": digest, "basis": basis})

    def handle_variants_status(self, project: Path) -> None:
        with self.server.project_lock:
            manifest = read_json(project / "project.json", {}) or {}
            state = read_json(project / STATE_REL, {}) or {}
        variants = []
        for variant in state_variants(state):
            variant_id = str(variant.get("variant_id"))
            entry: dict[str, Any] = {
                "variant_id": variant_id,
                "preset_id": variant.get("preset_id"),
            }
            try:
                snapshot = compute_variant_snapshot(project, state, variant_id)
            except ValueError as exc:
                entry["timeline"] = entry["final"] = {"error": str(exc)}
                variants.append(entry)
                continue
            timeline_revision = snapshot["snapshot_hash"]
            receipt = read_json(
                project / VARIANT_DELIVERY_REL / f"{variant_id}.json", None
            )
            final_revision = canonical_revision(
                {"snapshot": timeline_revision, "delivery_qa": receipt}
            )
            for gate, revision in (("timeline", timeline_revision), ("final", final_revision)):
                approval = variant_approval_entry(manifest, gate, variant_id)
                entry[gate] = {
                    "revision": revision,
                    "approved": bool(
                        approval.get("approved")
                        and approval.get("state_revision") == revision
                    ),
                }
            receipt = read_json(
                project / VARIANT_DELIVERY_REL / f"{variant_id}.json", None
            )
            entry["delivery"] = (
                {"output": receipt.get("output")} if isinstance(receipt, dict) else None
            )
            variants.append(entry)
        write_output_variant_set(project, state, manifest, variants)
        self.send_json({"ok": True, "variants": variants})

    _ASSET_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}

    def handle_assets_library(self, project: Path) -> None:
        assets_dir = project / "assets"
        registry_error: str | None = None
        try:
            registry = asset_registry.load_registry(project)
        except asset_registry.AssetRegistryError:
            registry = {"schema_version": 1, "items": []}
            registry_error = "asset provenance registry is invalid"
        registry_by_path: dict[str, list[dict[str, Any]]] = {}
        for registry_item in registry["items"]:
            registry_by_path.setdefault(str(registry_item.get("path")), []).append(
                registry_item
            )
        assertion = read_json(project / RIGHTS_REL, None)
        asserted = {
            str(item.get("asset_sha256"))
            for item in (assertion or {}).get("items", [])
            if item.get("asserted")
        }
        items = []
        if assets_dir.is_dir():
            for path in sorted(assets_dir.rglob("*")):
                if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in {
                    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v",
                }:
                    continue
                stat = path.stat()
                cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
                digest = self._ASSET_DIGEST_CACHE.get(cache_key)
                if digest is None:
                    digest = file_sha256(path)
                    if len(self._ASSET_DIGEST_CACHE) > 2048:
                        self._ASSET_DIGEST_CACHE.clear()
                    self._ASSET_DIGEST_CACHE[cache_key] = digest
                if len(items) >= 500:
                    break
                relative = path.relative_to(project).as_posix()
                current_registry = next(
                    (
                        registry_item
                        for registry_item in registry_by_path.get(relative, [])
                        if registry_item.get("sha256") == digest
                    ),
                    None,
                )
                item_registry_error = registry_error
                if current_registry is None and registry_by_path.get(relative):
                    item_registry_error = "asset provenance hash is stale"
                license_info = (
                    current_registry.get("license", {}) if current_registry else {}
                )
                items.append(
                    {
                        "path": relative,
                        "kind": "video" if path.suffix.lower() in {".mp4", ".mov", ".m4v"} else "image",
                        "sha256": digest,
                        "asserted": digest in asserted,
                        "provider_id": (
                            current_registry.get("provider_id") if current_registry else None
                        ),
                        "license_spdx": license_info.get("spdx"),
                        "review_status": (
                            current_registry.get("review_status")
                            if current_registry
                            else "unregistered"
                        ),
                        "attribution_required": bool(
                            license_info.get("attribution_required")
                        ),
                        "registry_error": item_registry_error,
                    }
                )
        self.send_json(
            {"ok": True, "assets": items, "registry_error": registry_error}
        )

    def _provider_error(self, exc: AssetProviderError) -> None:
        self.send_json(
            {"ok": False, "error": str(exc), "code": exc.code},
            status=exc.status_code,
        )

    def handle_provider_status(self) -> None:
        try:
            payload = self.server.asset_provider_service.status()
        except AssetProviderError as exc:
            self._provider_error(exc)
            return
        self.send_json({"ok": True, **payload})

    def handle_provider_consent(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not isinstance(body, dict) or set(body) != {
            "provider_id",
            "consented",
            "confirmed_by",
        }:
            self.send_json({"ok": False, "error": "consent body is malformed"}, status=400)
            return
        if (
            not isinstance(body.get("provider_id"), str)
            or not isinstance(body.get("consented"), bool)
            or not isinstance(body.get("confirmed_by"), str)
        ):
            self.send_json({"ok": False, "error": "consent body is malformed"}, status=400)
            return
        try:
            consent = self.server.asset_provider_service.set_consent(
                body["provider_id"], body["consented"], body["confirmed_by"]
            )
        except AssetProviderError as exc:
            self._provider_error(exc)
            return
        self.send_json({"ok": True, "consent": consent})

    def handle_provider_search(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not isinstance(body, dict) or not set(body).issubset(
            {"provider_id", "query", "page"}
        ) or not {"provider_id", "query"}.issubset(body):
            self.send_json({"ok": False, "error": "search body is malformed"}, status=400)
            return
        page = body.get("page", 1)
        if (
            not isinstance(body.get("provider_id"), str)
            or not isinstance(body.get("query"), str)
            or isinstance(page, bool)
            or not isinstance(page, int)
        ):
            self.send_json({"ok": False, "error": "search body is malformed"}, status=400)
            return
        try:
            result = self.server.asset_provider_service.search(
                body["provider_id"], body["query"], page
            )
        except AssetProviderError as exc:
            self._provider_error(exc)
            return
        self.send_json({"ok": True, **result})

    def handle_provider_import(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if (
            not isinstance(body, dict)
            or set(body) != {"import_token"}
            or not isinstance(body.get("import_token"), str)
            or not body["import_token"]
        ):
            self.send_json({"ok": False, "error": "import body is malformed"}, status=400)
            return
        try:
            result = self.server.asset_provider_service.import_candidate(
                body["import_token"],
                lambda path: ffprobe_visual_dimensions(path) is not None,
            )
        except AssetProviderError as exc:
            self._provider_error(exc)
            return
        self.send_json({"ok": True, **result})

    def handle_render_variant(self) -> None:
        """Background variant render through the CLI (per-variant gates live
        in the CLI path; the worker only reports status)."""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        quality = str(body.get("quality", "preview"))
        variant_id = str(body.get("variant_id") or "")
        if quality not in {"preview", "final"}:
            self.send_json({"ok": False, "error": "quality must be preview or final"}, status=422)
            return
        with self.server.project_lock:
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
        if find_variant(state, variant_id) is None:
            self.send_json({"ok": False, "error": "unknown variant"}, status=404)
            return
        if not self.server.render_lock.acquire(blocking=False):
            self.send_json({"ok": False, "error": "another render is already running"}, status=409)
            return
        output_rel = f"renders/variant-{variant_id}-{quality}.mp4"
        job_token = secrets.token_hex(8)
        with self.server.project_lock:
            self.server.render_status = {
                "state": "rendering",
                "message": f"變體 {variant_id} 輸出中（{quality}）",
                "output": None,
                "job": job_token,
            }

        def worker() -> None:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("render_editor_timeline.py")),
                        "--project-dir", str(self.server.project_dir),
                        "--output", str(self.server.project_dir / output_rel),
                        "--quality", quality,
                        "--variant", variant_id,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=2 * 60 * 60,
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or result.stdout).strip()[-800:])
                status_payload = {
                    "state": "done",
                    "message": f"變體 {variant_id} 輸出完成",
                    "output": output_rel,
                    "variant_id": variant_id,
                    "quality": quality,
                }
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                status_payload = {
                    "state": "error",
                    "message": str(exc)[:500],
                    "output": None,
                    "variant_id": variant_id,
                }
            finally:
                with self.server.project_lock:
                    # Only this job may write its terminal state, and the
                    # lock is released inside the same critical section so a
                    # successor cannot interleave (Codex review).
                    if self.server.render_status.get("job") == job_token:
                        status_payload["job"] = job_token
                        self.server.render_status = status_payload
                    self.server.render_lock.release()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            with self.server.project_lock:
                self.server.render_lock.release()
                self.server.render_status = {
                    "state": "error",
                    "message": "無法啟動輸出執行緒",
                    "output": None,
                }
            self.send_json({"ok": False, "error": "could not start render worker"}, status=500)
            return
        self.send_json({"ok": True, "output": output_rel, "status_url": "/api/render-status"})

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

    def handle_project_font_bytes(self, project: Path, asset_id: str) -> None:
        """Serve only a freshly verified local font; no range or path input."""
        handle = None
        try:
            binding = asset_registry.resolve_project_font(project, asset_id)
            path = project_entry_path(project, str(binding.get("path") or ""))
            if path.suffix.lower() not in {".ttf", ".otf"}:
                raise asset_registry.AssetRegistryError("font type is invalid")
            expected_sha256 = binding.get("sha256")
            if not isinstance(expected_sha256, str) or re.fullmatch(
                r"[0-9a-f]{64}", expected_sha256
            ) is None:
                raise asset_registry.AssetRegistryError("font hash is invalid")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise OSError("font path is not a regular file")
                handle = os.fdopen(descriptor, "rb")
                descriptor = -1
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise asset_registry.AssetRegistryError("font bytes changed after resolve")
                handle.seek(0)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except (asset_registry.AssetRegistryError, OSError, ValueError):
            if handle is not None:
                handle.close()
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type", "font/ttf" if path.suffix.lower() == ".ttf" else "font/otf"
            )
            self.send_header("Content-Length", str(file_stat.st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile)
        finally:
            handle.close()

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
                "audio_event_timeline": audio_event_timeline(project, state),
                "platform_presets": PLATFORM_PRESETS,
                "director_presets": DIRECTOR_PRESETS,
                "style_packs": public_style_pack_catalog(),
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
        font_match = re.fullmatch(r"/api/fonts/([A-Za-z0-9_-]{1,80})/bytes", path)
        if font_match:
            self.handle_project_font_bytes(project, font_match.group(1))
            return
        if path.startswith("/assets/"):
            encoded_relative = path.removeprefix("/")
            if re.search(r"%(?![0-9A-Fa-f]{2})", encoded_relative):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                relative = urllib.parse.unquote_to_bytes(encoded_relative).decode("utf-8")
            except UnicodeDecodeError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            route_parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in route_parts):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            route_parts_folded = tuple(part.casefold() for part in route_parts)
            route_suffix = Path(relative).suffix.casefold()
            if (
                route_parts_folded[:2] == ("assets", "fonts")
                or route_suffix in {".ttf", ".otf"}
            ):
                # Project fonts are hostile structured binaries.  The only
                # browser exposure path is /api/fonts/<asset_id>/bytes, which
                # resolves receipt-bound bytes and physically revalidates the
                # font on every request.  Generic assets must not create a
                # second, MIME-guess-based path around that boundary.
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                asset = scoped_project_path(project, relative, "assets")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            canonical_parts = asset.relative_to(project.resolve()).parts
            if tuple(part.casefold() for part in canonical_parts[:2]) == ("assets", "fonts"):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if asset.suffix.lower() in {".svg", ".svgz", ".xml"}:
                # SVG/XML bytes are never browser-served.  Provider SVG
                # imports publish only the receipt-bound PNG derivative.
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(asset, allow_range=asset.suffix.lower() in {".mp4", ".mov"})
            return
        if path == "/api/rights":
            self.handle_rights_status(project)
            return
        if path == "/api/variants/status":
            self.handle_variants_status(project)
            return
        if path == "/api/assets/library":
            self.handle_assets_library(project)
            return
        if path == "/api/providers/status":
            self.handle_provider_status()
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
            expected_revision = None
            if isinstance(state, dict):
                expected_revision = state.pop("x_expected_revision", None)
            try:
                errors, response = self.persist_editor_state(
                    state, expected_revision=expected_revision
                )
            except EditorRevisionConflict:
                self.send_json(
                    {
                        "ok": False,
                        "error": "editor state changed on the server; reload before saving",
                        "error_code": "revision_conflict",
                    },
                    status=409,
                )
                return
            if errors:
                self.send_json({"ok": False, "errors": errors}, status=422)
                return
        except (ValueError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.server.schedule_caption_render()
        self.send_json(response)

    def persist_editor_state(
        self,
        state: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Shared validated persistence for PUT and server-side style patches."""
        with self.server.project_lock:
            if expected_revision:
                on_disk = read_json(self.server.project_dir / STATE_REL, {}) or {}
                if on_disk.get("revision") and on_disk["revision"] != expected_revision:
                    raise EditorRevisionConflict("editor state revision changed")
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            duration = float(manifest.get("source", {}).get("duration_s", 0.0))
            plan = read_json(self.server.project_dir / "working/highlight_plan.json", {}) or {}
            state["source_sha256"] = manifest.get("source", {}).get("sha256")
            state["highlight_plan_revision"] = plan.get("plan_revision")
            upgrade_video_template_state(state)
            state["project_dir"] = str(self.server.project_dir)
            errors = validate_editor_state(state, duration)
            state.pop("project_dir", None)
            if not errors and isinstance(state.get("audio_event_edits"), dict):
                try:
                    _render_id, source_plan, _source_hash = resolve_audio_event_source(
                        self.server.project_dir, state
                    )
                    resolve_studio_audio_plan(source_plan, state["audio_event_edits"])
                except AudioEventEditError as exc:
                    errors.append(f"audio_event_edits: {exc}")
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
        if path == "/api/providers/consent":
            self.handle_provider_consent()
            return
        if path == "/api/assets/search":
            self.handle_provider_search()
            return
        if path == "/api/assets/import-provider":
            self.handle_provider_import()
            return
        if path == "/api/auto-visuals":
            self.handle_auto_visuals()
            return
        if path == "/api/generate-image":
            self.handle_generate_image()
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
        if path == "/api/structured-layers":
            self.handle_structured_layers()
            return
        if path == "/api/rights/assert":
            self.handle_rights_assert()
            return
        if path == "/api/render-variant":
            self.handle_render_variant()
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
        relative = f"assets/{stored_name}"
        digest = hashlib.sha256(data).hexdigest()
        item = {
            "asset_id": "asset-upload-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20],
            "path": relative,
            "sha256": digest,
            "origin": "user-upload",
            "provider_id": None,
            "source_url": None,
            "license": {
                "spdx": "UNKNOWN",
                "attribution_required": False,
                "attribution_text": "",
                "verified_at": now_utc_z(),
            },
            "review_status": "pending",
        }
        try:
            asset_registry.upsert_item(self.server.project_dir, item)
        except asset_registry.AssetRegistryError:
            output.unlink(missing_ok=True)
            self.send_json(
                {"ok": False, "error": "asset registry update failed"}, status=409
            )
            return
        self.send_json(
            {
                "ok": True,
                "source": relative,
                "url": f"/assets/{urllib.parse.quote(stored_name)}",
                "sha256": digest,
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
        try:
            resolved = resolve_director_profile(director)
            enforce_runtime_capabilities(resolved)
        except DirectorResolutionError as exc:
            payload: dict[str, Any] = {
                "ok": False,
                "error_code": exc.code,
                "error": (
                    "missing director capabilities: "
                    + ", ".join(exc.missing_capabilities)
                    if exc.code == "capability_missing"
                    else exc.code
                ),
            }
            if exc.missing_capabilities:
                payload["missing_capabilities"] = exc.missing_capabilities
            self.send_json(payload, status=422)
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

    def handle_auto_visuals(self) -> None:
        """Lay out every cut's visuals from what the transcript says."""
        project_dir = self.server.project_dir
        state = read_json(project_dir / STATE_REL, {}) or {}
        evidence = read_json(project_dir / "working/evidence_map.json", None)
        if not isinstance(evidence, dict) or not evidence.get("items"):
            self.send_json(
                {"ok": False, "error": "no evidence index yet; build it from the transcript first"},
                status=422,
            )
            return
        segments = state.get("segments")
        if not isinstance(segments, list) or not segments:
            self.send_json({"ok": False, "error": "the timeline has no segments"}, status=422)
            return
        director = DIRECTOR_PRESETS.get(
            str(state.get("director_style") or ""), DIRECTOR_PRESETS["teacher-punch"]
        )
        planned = visual_director.plan_visuals(
            segments,
            evidence["items"],
            editorial_title=active_editorial_title(state),
            visual_density=str(director.get("visual_density") or "balanced"),
            kinetic_scene_vocabulary=(
                str(state.get("director_style") or "") == "kinetic-explainer"
            ),
            project_assets=(
                asset_registry.load_registry(project_dir)["items"]
                if str(state.get("director_style") or "") == "kinetic-explainer"
                else None
            ),
        )
        errors = visual_director.validate(planned)
        if errors:
            self.send_json({"ok": False, "error": "; ".join(errors[:5])}, status=422)
            return
        try:
            publish_layer_bundle(
                project_dir, planned["structured_layers"], planned["visual_plan"]
            )
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=422)
            return
        beats: dict[str, int] = {}
        for item in planned["visual_plan"]["items"]:
            beats[item["beat"]] = beats.get(item["beat"], 0) + 1
        self.send_json({"ok": True, "beats": beats, "layers": len(planned["structured_layers"]["items"])})

    def handle_generate_image(self) -> None:
        """Make a picture for one beat, or hand back the one it already has."""
        payload = self.read_json_body()
        if payload is None:
            return
        beat_id = str(payload.get("beat_id") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not beat_id or not prompt:
            self.send_json({"ok": False, "error": "beat_id and prompt are required"}, status=422)
            return
        try:
            result = generated_images.generate_image(
                self.server.project_dir, beat_id, prompt
            )
        except Exception as exc:  # the bridge drives a browser; it can fail in many ways
            self.send_json({"ok": False, "error": str(exc)[-400:]}, status=502)
            return
        if not result.get("ok") and not result.get("reused"):
            self.send_json({"ok": False, "error": result.get("reason", "generation failed")}, status=502)
            return
        self.send_json({"ok": True, "image": result})

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
        variant_id = str(body.get("variant_id") or "") or None
        if variant_id:
            if gate not in {"timeline", "final"}:
                self.send_json(
                    {"ok": False, "error": "variant approvals only apply to timeline/final"},
                    status=422,
                )
                return
            self.handle_variant_approval(gate, variant_id, body)
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
                "--visual-evidence",
                str(rendered_visual_evidence_path(self.server.project_dir, render_id)),
                # Every clip shares the declaration frozen into the batch's
                # snapshot, not whatever the project says now. The geometry
                # comes from the same snapshot for the same reason.
                *qa_policy_args(
                    snapshot_payload.get("state"), snapshot_payload.get("manifest")
                ),
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
            visual_delivery = qa_payload.get("visual_delivery")
            if (
                qa_result.returncode != 0
                or qa_payload.get("status") != "pass"
                or not isinstance(visual_delivery, dict)
                or visual_delivery.get("status") != "pass"
            ):
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
                    "visual_delivery": visual_delivery,
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
                "visual_delivery": visual_delivery,
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
                    "--visual-evidence",
                    str(rendered_visual_evidence_path(self.server.project_dir, render_id)),
                    # The declaration frozen into the snapshot this render was
                    # authorized against, not whatever the project says now,
                    # and the geometry it was laid out against.
                    *qa_policy_args(
                        snapshot_payload.get("state"),
                        snapshot_payload.get("manifest"),
                    ),
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
                visual_delivery = qa_payload.get("visual_delivery")
                if (
                    qa_result.returncode != 0
                    or qa_payload.get("status") != "pass"
                    or not isinstance(visual_delivery, dict)
                    or visual_delivery.get("status") != "pass"
                ):
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
                        "visual_delivery": visual_delivery,
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
                    "visual_delivery": qa_payload.get("visual_delivery"),
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

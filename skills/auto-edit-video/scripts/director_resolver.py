#!/usr/bin/env python3
"""Strict, deterministic director profile resolver for the public CLI."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import contract_registry


SCHEMA_VERSION = 1
RESOLVER_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parents[1]
DIRECTOR_REGISTRY_PATH = SKILL_DIR / "contracts/instances/director_mode__registry.json"
# These capabilities have concrete, local P0 implementations.  Keep this
# declaration patchable: public entrypoints use it for their zero-mutation
# preflight and tests deliberately remove a capability to exercise that path.
IMPLEMENTED_CAPABILITIES = frozenset(
    {
        "approved-translation-provider",
        "audio-event-mixer-v1",
        "caption-delivery-v2",
        "kinetic-scene-director-v1",
        "unified-delivery-envelope-v1",
    }
)
SELECTION_SCHEMA_VERSION = 1
SELECTION_REASONS = frozenset(
    {
        "explicit_profile",
        "explicit_kinetic_bundle",
        "reference_style_match",
        "default_unchanged",
    }
)

# Public v1 request names.  Underscore aliases are accepted for callers that
# originate from argparse, then normalized to the public dashed spelling.
_OVERRIDE_ALIASES = {
    "input": "input",
    "folder": "folder",
    "main": "main",
    "out": "out",
    "project-dir": "project-dir",
    "project_dir": "project-dir",
    "clips": "clips",
    "seconds": "seconds",
    "platform": "platform",
    "brief": "brief",
    "glossary": "glossary",
    "fix": "fix",
    "keep-pauses": "keep-pauses",
    "keep_pauses": "keep-pauses",
    "framing": "framing",
    "quality": "quality",
    "model": "model",
    "translate": "translate",
    "cards-from-model": "cards-from-model",
    "cards_from_model": "cards-from-model",
    "no-cards": "no-cards",
    "no_cards": "no-cards",
    "no-editorial": "no-editorial",
    "no_editorial": "no-editorial",
    "burned-in": "burned-in",
    "burned_in": "burned-in",
}
_OVERRIDE_ENUMS = {
    "burned-in": frozenset({"auto", "yes", "no"}),
    "framing": frozenset({"auto", "contain", "cover"}),
    "platform": frozenset(
        {
            "auto",
            "generic-vertical",
            "instagram-reels",
            "youtube-shorts",
            "tiktok",
            "xiaohongshu-portrait",
            "xiaohongshu-full",
            "youtube-landscape",
        }
    ),
    "quality": frozenset({"preview", "final"}),
}


class DirectorResolutionError(ValueError):
    """A stable public selector failure."""

    def __init__(
        self,
        code: str,
        *,
        details: list[str] | None = None,
        conflicts: list[str] | None = None,
        missing_capabilities: list[str] | None = None,
    ) -> None:
        if code not in {
            "unknown_director",
            "registry_invalid",
            "profile_conflict",
            "capability_missing",
        }:
            raise ValueError(f"unsupported director resolution error: {code}")
        self.code = code
        self.details = sorted(details or [])
        self.conflicts = sorted(set(conflicts or []))
        self.missing_capabilities = sorted(set(missing_capabilities or []))
        super().__init__(code)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.code}
        if self.conflicts:
            payload["conflicts"] = self.conflicts
        if self.missing_capabilities:
            payload["missing_capabilities"] = self.missing_capabilities
        if self.details:
            payload["details"] = self.details
        return payload


def canonical_json(value: Any) -> str:
    """Canonical compact JSON used for stdout and hash vectors."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def load_director_registry() -> dict[str, Any]:
    """Load and strictly validate the shipped registry before resolution."""
    try:
        payload = contract_registry.load_artifact_text(
            DIRECTOR_REGISTRY_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DirectorResolutionError("registry_invalid", details=[str(exc)]) from exc
    errors = contract_registry.validate_artifact("director_mode", payload)
    if errors:
        raise DirectorResolutionError("registry_invalid", details=errors)
    return payload


def load_selection_request(path: str | Path) -> dict[str, Any]:
    """Read a strict schema-v1 selection request without mutating the file."""
    request_path = Path(path).expanduser().resolve()
    try:
        request = contract_registry.load_artifact_text(
            request_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DirectorResolutionError("registry_invalid", details=[str(exc)]) from exc
    if not isinstance(request, dict):
        raise DirectorResolutionError(
            "registry_invalid", details=["selection request must be an object"]
        )
    _validate_selection_request_shape(request)
    normalized = dict(request)
    normalized["profile_id"] = request["profile_id"].strip()
    normalized["evidence"] = request["evidence"].strip()
    normalized["overrides"] = normalize_overrides(request["overrides"])
    return normalized


def _validate_selection_request_shape(request: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "profile_id",
        "selection_reason",
        "evidence",
        "overrides",
    }
    missing = sorted(expected - set(request))
    extra = sorted(set(request) - expected)
    details = [f"missing:{key}" for key in missing]
    details.extend(f"unexpected:{key}" for key in extra)
    if details:
        raise DirectorResolutionError("registry_invalid", details=details)
    if (
        isinstance(request["schema_version"], bool)
        or not isinstance(request["schema_version"], int)
        or request["schema_version"] != SELECTION_SCHEMA_VERSION
    ):
        raise DirectorResolutionError(
            "registry_invalid", details=["schema_version must be 1"]
        )
    if not isinstance(request["profile_id"], str) or not request["profile_id"].strip():
        raise DirectorResolutionError(
            "registry_invalid", details=["profile_id must be a non-empty string"]
        )
    if request["profile_id"] != request["profile_id"].strip():
        raise DirectorResolutionError(
            "registry_invalid", details=["profile_id must not have surrounding whitespace"]
        )
    if (
        not isinstance(request["selection_reason"], str)
        or request["selection_reason"] not in SELECTION_REASONS
    ):
        raise DirectorResolutionError(
            "registry_invalid", details=["selection_reason is not supported"]
        )
    if not isinstance(request["evidence"], str):
        raise DirectorResolutionError(
            "registry_invalid", details=["evidence must be a string"]
        )
    if not isinstance(request["overrides"], dict):
        raise DirectorResolutionError(
            "registry_invalid", details=["overrides must be an object"]
        )


def normalize_overrides(
    overrides: dict[str, Any], *, enforce_kinetic_conflicts: bool = False
) -> dict[str, Any]:
    """Validate the v1 compatibility matrix and return canonical key names."""
    normalized: dict[str, Any] = {}
    unknown = sorted(set(overrides) - set(_OVERRIDE_ALIASES))
    if unknown:
        raise DirectorResolutionError(
            "registry_invalid",
            details=[f"unexpected override:{key}" for key in unknown],
        )
    for raw_key, value in overrides.items():
        key = _OVERRIDE_ALIASES[raw_key]
        if value is None or value is False or value == "" or value == []:
            continue
        if key in {"clips"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be an integer"]
                )
        elif key in {"seconds"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be a number"]
                )
            if not math.isfinite(float(value)):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be finite"]
                )
        elif key in {"keep-pauses", "cards-from-model", "no-cards", "no-editorial"}:
            if not isinstance(value, bool):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be boolean"]
                )
        elif key in {"glossary", "fix"}:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be a string array"]
                )
        elif key in {"input", "folder", "main", "out", "project-dir", "platform", "brief",
                     "framing", "quality", "model", "translate", "burned-in"}:
            if not isinstance(value, str):
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must be a string"]
                )
            value = value.strip()
            if key in {"translate", "burned-in"}:
                value = value.lower()
            if not value:
                raise DirectorResolutionError(
                    "registry_invalid", details=[f"override:{raw_key} must not be blank"]
                )
            allowed_values = _OVERRIDE_ENUMS.get(key)
            if allowed_values is not None and value not in allowed_values:
                raise DirectorResolutionError(
                    "registry_invalid",
                    details=[f"override:{raw_key} is not supported"],
                )
        if key in normalized:
            raise DirectorResolutionError(
                "registry_invalid", details=[f"duplicate override:{key}"]
            )
        normalized[key] = value

    conflicts: list[str] = []
    if enforce_kinetic_conflicts:
        if "translate" in normalized and normalized["translate"].lower() != "en":
            conflicts.append("translate")
        if normalized.get("no-cards") is True:
            conflicts.append("no-cards")
        if normalized.get("no-editorial") is True:
            conflicts.append("no-editorial")
        if normalized.get("burned-in", "").lower() == "yes":
            conflicts.append("burned-in")
    if conflicts:
        raise DirectorResolutionError("profile_conflict", conflicts=conflicts)
    return {key: normalized[key] for key in sorted(normalized)}


def _legacy_experience(mode: dict[str, Any]) -> dict[str, Any]:
    """Keep old five profiles resolvable without changing their runtime path."""
    envelope = mode["envelope"]
    constraints = mode["constraints"]
    return {
        "caption_delivery": {"mode": "source", "required": False, "artifact_version": 1},
        "translation": {"required": False, "target_language": None, "provider": "none"},
        "scene_pack": "legacy-cards-v1",
        "style_pack": "legacy",
        "stage_layout": "presenter-full-frame",
        "visual_density": constraints.get("visual_density", "balanced"),
        "motion_intensity": envelope["motion_intensity"],
        "sfx": {"mode": "disabled", "pack": None, "cue_coverage_policy": "not_applicable"},
    }


def resolve_director_profile(
    director: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one registry mode into the hash-bound v1 artifact."""
    registry = load_director_registry()
    mode = next((item for item in registry["modes"] if item.get("id") == director), None)
    if mode is None:
        raise DirectorResolutionError("unknown_director")
    normalized_overrides = normalize_overrides(
        overrides or {}, enforce_kinetic_conflicts=(director == "kinetic-explainer")
    )
    experience = mode.get("experience")
    if not isinstance(experience, dict):
        experience = _legacy_experience(mode)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "profile_id": director,
        "registry_schema_version": registry["schema_version"],
        "registry_entry_version": mode["version"],
        "experience": experience,
        "overrides": normalized_overrides,
        "required_capabilities": mode.get("required_capabilities", {}),
        "rules": sorted(str(rule) for rule in mode.get("auto_select_rules", [])),
    }
    artifact["resolved_hash"] = contract_registry.canonical_hash(artifact)
    return artifact


def resolve_director_selection(
    *,
    director: str | None = None,
    selection_request: dict[str, Any] | None = None,
    extra_overrides: dict[str, Any] | None = None,
    default_director: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a profile and return its normalized request binding."""
    if selection_request is None:
        request_profile = director or default_director
        if not request_profile:
            raise DirectorResolutionError(
                "registry_invalid", details=["director or selection request is required"]
            )
        request = {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "profile_id": request_profile,
            "selection_reason": (
                "explicit_profile" if director else "default_unchanged"
            ),
            "evidence": "",
            "overrides": {},
        }
    else:
        request = selection_request
        request_profile = selection_request["profile_id"]
    if director and selection_request is not None and director != request_profile:
        raise DirectorResolutionError("profile_conflict", conflicts=["director", "profile_id"])
    merged_overrides = dict(request["overrides"])
    if extra_overrides:
        merged_overrides.update(extra_overrides)
    normalized_overrides = normalize_overrides(merged_overrides)
    resolved = resolve_director_profile(
        request_profile,
        overrides=normalized_overrides,
    )
    normalized_request = {
        "schema_version": request["schema_version"],
        "profile_id": request_profile,
        "selection_reason": request["selection_reason"],
        "evidence": request["evidence"].strip(),
        "overrides": normalized_overrides,
        "resolved_profile_hash": resolved["resolved_hash"],
    }
    return resolved, normalized_request


def enforce_runtime_capabilities(resolved: dict[str, Any]) -> None:
    """Fail closed for a cut until every selected downstream module exists."""
    if resolved.get("profile_id") != "kinetic-explainer":
        return
    declared = resolved.get("required_capabilities") or {}
    ids = declared.get("ids", []) if isinstance(declared, dict) else []
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise DirectorResolutionError(
            "registry_invalid", details=["kinetic required_capabilities.ids must be strings"]
        )
    missing = sorted(set(ids) - IMPLEMENTED_CAPABILITIES)
    if missing:
        raise DirectorResolutionError("capability_missing", missing_capabilities=missing)


def persist_director_selection(
    project_dir: str | Path,
    resolved: dict[str, Any],
    selection_request: dict[str, Any],
) -> None:
    """Atomically write canonical profile and normalized request artifacts."""
    if not isinstance(resolved, dict) or not isinstance(selection_request, dict):
        raise DirectorResolutionError(
            "registry_invalid", details=["resolved profile and selection request must be objects"]
        )
    claimed_hash = resolved.get("resolved_hash")
    unsigned_resolved = {
        key: value for key, value in resolved.items() if key != "resolved_hash"
    }
    try:
        computed_hash = contract_registry.canonical_hash(unsigned_resolved)
    except (TypeError, ValueError) as exc:
        raise DirectorResolutionError(
            "registry_invalid", details=[f"resolved profile is not canonical: {exc}"]
        ) from exc
    if claimed_hash != computed_hash:
        raise DirectorResolutionError("profile_conflict", conflicts=["resolved_hash"])

    profile_id = resolved.get("profile_id")
    resolved_overrides = resolved.get("overrides")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise DirectorResolutionError(
            "registry_invalid", details=["resolved profile_id must be non-empty"]
        )
    if not isinstance(resolved_overrides, dict):
        raise DirectorResolutionError(
            "registry_invalid", details=["resolved overrides must be an object"]
        )
    expected_resolved = resolve_director_profile(
        profile_id, overrides=resolved_overrides
    )
    if expected_resolved != resolved:
        raise DirectorResolutionError(
            "profile_conflict", conflicts=["resolved_profile"]
        )

    expected_request_keys = {
        "schema_version",
        "profile_id",
        "selection_reason",
        "evidence",
        "overrides",
        "resolved_profile_hash",
    }
    missing_request_keys = sorted(expected_request_keys - set(selection_request))
    extra_request_keys = sorted(set(selection_request) - expected_request_keys)
    if missing_request_keys or extra_request_keys:
        details = [f"missing:{key}" for key in missing_request_keys]
        details.extend(f"unexpected:{key}" for key in extra_request_keys)
        raise DirectorResolutionError("registry_invalid", details=details)
    request_without_hash = {
        key: selection_request[key]
        for key in expected_request_keys
        if key != "resolved_profile_hash"
    }
    _validate_selection_request_shape(request_without_hash)
    normalized_request_overrides = normalize_overrides(
        request_without_hash["overrides"],
        enforce_kinetic_conflicts=(profile_id == "kinetic-explainer"),
    )
    if normalized_request_overrides != request_without_hash["overrides"]:
        raise DirectorResolutionError(
            "registry_invalid", details=["selection request overrides are not canonical"]
        )
    if selection_request.get("resolved_profile_hash") != claimed_hash:
        raise DirectorResolutionError(
            "profile_conflict", conflicts=["resolved_profile_hash"]
        )
    if selection_request.get("profile_id") != profile_id:
        raise DirectorResolutionError(
            "profile_conflict", conflicts=["profile_id"]
        )
    if selection_request.get("overrides") != resolved_overrides:
        raise DirectorResolutionError("profile_conflict", conflicts=["overrides"])
    working = Path(project_dir).expanduser().resolve() / "working"
    working.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("resolved_director_profile.json", resolved),
        ("director_selection_request.json", selection_request),
    ):
        destination = working / name
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=str(working)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def resolution_error_json(error: DirectorResolutionError) -> str:
    return canonical_json(error.payload())

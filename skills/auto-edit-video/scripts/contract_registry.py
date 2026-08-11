#!/usr/bin/env python3
"""Contract registry: strict schema validation, canonical hashing, fixtures CLI.

Design (PRD Phase 0, 2026-08-04 approved plan):
- Schemas live in ``contracts/schemas/*.schema.json`` and use a *custom strict
  dialect* that borrows JSON Schema syntax. This is NOT a JSON Schema 2020-12
  implementation: any keyword outside ``SUPPORTED_KEYWORDS`` fails validation
  loudly instead of being silently ignored (false-green prevention).
- Cross-artifact rules (evidence references, timing SSOT back-references) are
  enforced by explicit semantic validators, not schema keywords.
- ``canonical_hash`` is the normative artifact hash for every contract:
  sha256 over compact sorted-keys UTF-8 JSON. Artifacts must not contain
  duplicate keys, NaN or Infinity; this Python implementation is normative.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "contracts" / "schemas"
FIXTURE_DIR = SKILL_DIR / "contracts" / "fixtures"
INSTANCE_DIR = SKILL_DIR / "contracts" / "instances"

SUPPORTED_KEYWORDS = {
    "$id", "title", "description",
    "type", "required", "properties", "enum", "const", "pattern",
    "items", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength",
    "additionalProperties",
}
METADATA_PREFIX = "x_"
TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class ContractError(ValueError):
    """Raised for schema-dialect violations and validation failures."""


def _reject_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ContractError(f"duplicate key not allowed: {key!r}")
        seen[key] = value
    return seen


def load_artifact_text(text: str):
    """Parse artifact JSON under contract constraints (no dup keys/NaN/Inf)."""
    data = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda name: (_ for _ in ()).throw(
            ContractError(f"non-finite number not allowed: {name}")
        ),
    )
    _reject_nonfinite(data)
    return data


def _reject_nonfinite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("non-finite number not allowed")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


def canonical_hash(value) -> str:
    """Normative contract hash: sha256 of compact sorted-keys UTF-8 JSON."""
    _reject_nonfinite(value)
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_exact_equal(value, expected) -> bool:
    """Compare JSON values without Python's bool/int/float coercion."""
    if type(value) is not type(expected):
        return False
    if isinstance(value, dict):
        return value.keys() == expected.keys() and all(
            _json_exact_equal(item, expected[key]) for key, item in value.items()
        )
    if isinstance(value, list):
        return len(value) == len(expected) and all(
            _json_exact_equal(item, expected_item)
            for item, expected_item in zip(value, expected)
        )
    return value == expected


def check_schema_dialect(schema, path: str = "$") -> None:
    """Fail closed on any keyword this dialect does not implement."""
    if not isinstance(schema, dict):
        raise ContractError(f"{path}: schema node must be an object")
    for keyword in schema:
        if keyword in SUPPORTED_KEYWORDS or keyword.startswith(METADATA_PREFIX):
            continue
        raise ContractError(f"{path}: unsupported schema keyword {keyword!r}")
    for name, sub in (schema.get("properties") or {}).items():
        check_schema_dialect(sub, f"{path}.properties.{name}")
    if isinstance(schema.get("items"), dict):
        check_schema_dialect(schema["items"], f"{path}.items")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema_dialect(schema["additionalProperties"], f"{path}.additionalProperties")


def validate(value, schema, path: str = "$") -> list[str]:
    """Validate value against the strict dialect; returns error list."""
    errors: list[str] = []
    if "const" in schema:
        if not _json_exact_equal(value, schema["const"]):
            errors.append(f"{path}: expected const {schema['const']!r}")
        return errors
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(TYPE_CHECKS[t](value) for t in types):
            errors.append(f"{path}: expected type {expected}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: not below exclusiveMaximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", False)
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(item, properties[name], f"{path}.{name}"))
            elif isinstance(additional, dict):
                errors.extend(validate(item, additional, f"{path}.{name}"))
            elif additional is False:
                errors.append(f"{path}: unexpected property {name!r}")
    return errors


# ---------------------------------------------------------------------------
# Semantic validators: cross-field / cross-artifact rules the dialect cannot
# express. Each returns a list of error strings.
# ---------------------------------------------------------------------------

STRUCTURED_BEATS = {
    "title", "stat", "chart", "dynamic_list",
    # A quote, a question, a contrast and a definition are drawn from a layer
    # the same way the first four are; the words on them come out of the
    # transcript, so each one still has to name the evidence it was built on.
    "quote", "question", "comparison", "term", "typed_prompt", "grid_progress",
    "asset_mosaic",
}
TITLE_KINDS = {
    "full-screen-hook", "section", "lower-third", "quote", "hero-stat", "prompt"
}


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def semantic_provider_interface(interface) -> list[str]:
    """Validate provider relationships that the schema cannot express.

    Provider IDs are the bundle-wide identity for an integration.  The
    idempotency and preflight lists are compared after trimming so that
    whitespace-only entries and aliases that differ only by surrounding
    whitespace cannot silently bypass uniqueness checks.
    """
    errors = []
    provider_ids = set()
    for index, provider in enumerate(interface.get("providers", [])):
        path = f"$.providers[{index}]"
        provider_id = provider.get("id")
        if provider_id in provider_ids:
            errors.append(f"{path}.id: duplicate provider id {provider_id!r}")
        else:
            provider_ids.add(provider_id)

        key_fields = provider.get("idempotency_key_fields", [])
        seen_fields = set()
        for field_index, field in enumerate(key_fields):
            field_path = f"{path}.idempotency_key_fields[{field_index}]"
            if not _nonempty_str(field):
                errors.append(f"{field_path}: must be non-empty after trim")
                continue
            normalized = field.strip()
            if normalized in seen_fields:
                errors.append(f"{field_path}: duplicate field {normalized!r} after trim")
            else:
                seen_fields.add(normalized)

        preflight = provider.get("preflight") or {}
        checks = preflight.get("checks", [])
        seen_checks = set()
        normalized_checks = set()
        for check_index, check in enumerate(checks):
            check_path = f"{path}.preflight.checks[{check_index}]"
            if not _nonempty_str(check):
                errors.append(f"{check_path}: must be non-empty after trim")
                continue
            normalized = check.strip()
            normalized_checks.add(normalized)
            if normalized in seen_checks:
                errors.append(f"{check_path}: duplicate check {normalized!r} after trim")
            else:
                seen_checks.add(normalized)

        if provider.get("license_class") == "open":
            if preflight.get("required") is not True:
                errors.append(f"{path}.preflight.required: open provider must require preflight")
            if "license-metadata" not in normalized_checks:
                errors.append(
                    f"{path}.preflight.checks: open provider requires 'license-metadata'"
                )
        if provider.get("kind") in {"image", "svg", "font"} and preflight.get(
            "required"
        ) is False:
            errors.append(
                f"{path}.preflight.required: {provider.get('kind')} provider cannot disable preflight"
            )
    return errors


# Final licenses copied from contracts/policies/LICENSE_POLICY.md's allowlist.
LICENSE_FINAL_ALLOWLIST = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "OFL-1.1",
        "Apache-2.0",
        "Ubuntu-font-1.0",
        "MIT",
        "ISC",
        "Unlicense",
        "internal-original",
        "user-owned",
    }
)

_SVG_REPO_LICENSE_EVIDENCE = {
    ("heroicons", "MIT"): "https://github.com/tailwindlabs/heroicons/blob/0435d4ca364a608cc75e2f8683d374e55abbae26/LICENSE",
    ("lucide", "ISC"): "https://github.com/lucide-icons/lucide/blob/f12b0de177fbc2a6795e99be065887e72b237123/LICENSE",
    ("tabler", "MIT"): "https://github.com/tabler/tabler-icons/blob/8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc/LICENSE",
}


def _safe_asset_path(value) -> bool:
    """Return whether *value* is a normalized POSIX path under ``assets/``."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    if "\\" in value:
        return False
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != "assets":
        return False
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return True


def _https_url_without_credentials(value) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` makes malformed numeric ports fail closed too.
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.netloc)
        and hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def _timezone_aware_iso8601(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _canonical_provider_license_evidence(value, spdx) -> bool:
    expected_paths = {
        "CC0-1.0": "/publicdomain/zero/1.0",
        "CC-BY-4.0": "/licenses/by/4.0",
        "CC-BY-SA-4.0": "/licenses/by-sa/4.0",
    }
    expected_path = expected_paths.get(spdx)
    if expected_path is None or not _https_url_without_credentials(value):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.hostname
        and parsed.hostname.casefold() == "creativecommons.org"
        and parsed.port in {None, 443}
        and parsed.path in {expected_path, f"{expected_path}/"}
        and not parsed.query
        and not parsed.fragment
    )


def _canonical_font_license_evidence(provider_id, value, spdx) -> bool:
    if provider_id not in {"google-fonts", "fontsource"} or spdx not in {
        "OFL-1.1", "Apache-2.0", "Ubuntu-font-1.0",
    }:
        return False
    if not _https_url_without_credentials(value):
        return False
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.port is not None:
        return False
    if provider_id == "google-fonts":
        commit = "2796410152d4f9524b68ed46e69c1b60f8e0f7c3"
        match = re.fullmatch(
            rf"/google/fonts/{commit}/(ofl|apache|ufl)/[a-z0-9]{{1,80}}/(OFL\.txt|LICENSE\.txt|UFL\.txt)",
            parsed.path,
        )
        expected = {
            "ofl": ("OFL-1.1", "OFL.txt"),
            "apache": ("Apache-2.0", "LICENSE.txt"),
            "ufl": ("Ubuntu-font-1.0", "UFL.txt"),
        }
        return bool(
            parsed.netloc == "raw.githubusercontent.com"
            and match
            and expected[match.group(1)] == (spdx, match.group(2))
        )
    return bool(
        parsed.netloc == "cdn.jsdelivr.net"
        and re.fullmatch(
            r"/npm/@fontsource/[a-z0-9]+(?:-[a-z0-9]+)*@(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)/LICENSE",
            parsed.path,
        )
    )


def semantic_asset_provenance(provenance) -> list[str]:
    """Validate asset identity, provenance, path, license, and review rules."""
    errors = []
    asset_ids = set()
    paths = set()
    for index, item in enumerate(provenance.get("items", [])):
        path = f"$.items[{index}]"
        asset_id = item.get("asset_id")
        if asset_id in asset_ids:
            errors.append(f"{path}.asset_id: duplicate asset_id {asset_id!r}")
        else:
            asset_ids.add(asset_id)
        asset_path = item.get("path")
        if asset_path in paths:
            errors.append(f"{path}.path: duplicate path {asset_path!r}")
        else:
            paths.add(asset_path)
        if not _safe_asset_path(asset_path):
            errors.append(
                f"{path}.path: must be a normalized POSIX project-relative path under assets/"
            )

        origin = item.get("origin")
        provider_id = item.get("provider_id")
        source_url = item.get("source_url")
        license_info = item.get("license") or {}
        if origin == "provider":
            if not _nonempty_str(provider_id):
                errors.append(f"{path}.provider_id: provider origin requires a non-empty provider_id")
            if not _https_url_without_credentials(source_url):
                errors.append(
                    f"{path}.source_url: provider origin requires an HTTPS URL without credentials"
                )
            if not _https_url_without_credentials(license_info.get("evidence_url")):
                errors.append(
                    f"{path}.license.evidence_url: provider origin requires HTTPS license evidence"
                )
            elif not _canonical_provider_license_evidence(
                license_info.get("evidence_url"), license_info.get("spdx")
            ) and license_info.get("evidence_url") != _SVG_REPO_LICENSE_EVIDENCE.get(
                (provider_id, license_info.get("spdx"))
            ) and not _canonical_font_license_evidence(
                provider_id, license_info.get("evidence_url"), license_info.get("spdx")
            ):
                errors.append(
                    f"{path}.license.evidence_url: must match the canonical SPDX license URL"
                )
        elif origin in {"user-upload", "folder-import"}:
            if provider_id is not None:
                errors.append(f"{path}.provider_id: {origin} origin requires null provider_id")
            if source_url is not None:
                errors.append(f"{path}.source_url: {origin} origin requires null source_url")
            if license_info.get("evidence_url") is not None:
                errors.append(
                    f"{path}.license.evidence_url: {origin} origin requires null evidence"
                )
        elif origin == "generated":
            if not _nonempty_str(provider_id):
                errors.append(f"{path}.provider_id: generated origin requires a non-empty provider_id")

        spdx = license_info.get("spdx")
        if not _timezone_aware_iso8601(license_info.get("verified_at")):
            errors.append(f"{path}.license.verified_at: must be timezone-aware ISO-8601")
        if license_info.get("attribution_required") is True and not _nonempty_str(
            license_info.get("attribution_text")
        ):
            errors.append(
                f"{path}.license.attribution_text: required when attribution_required is true"
            )
        if spdx in {"CC-BY-4.0", "CC-BY-SA-4.0"} and license_info.get(
            "attribution_required"
        ) is not True:
            errors.append(
                f"{path}.license.attribution_required: {spdx} requires attribution_required true"
            )
        if item.get("review_status") == "approved":
            if spdx not in LICENSE_FINAL_ALLOWLIST:
                errors.append(
                    f"{path}.license.spdx: approved asset requires a final-allowlist license"
                )
            if spdx == "UNKNOWN":
                errors.append(f"{path}.license.spdx: UNKNOWN cannot be approved")
        # UNKNOWN is intentionally permitted only while a review is pending or
        # rejected; schema validation handles all other review-status values.
        elif spdx == "UNKNOWN" and item.get("review_status") not in {"pending", "rejected"}:
            errors.append(f"{path}.license.spdx: UNKNOWN is only allowed for pending/rejected")
    return errors


def semantic_structured_layer(layers) -> list[str]:
    """Discriminated payload rules: factual layers bind evidence per datum."""
    errors = []
    for index, item in enumerate(layers.get("items", [])):
        path = f"$.items[{index}].payload"
        payload = item.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"{path}: must be an object")
            continue
        layer_type = item.get("type")
        if layer_type == "title":
            if payload.get("title_kind") not in TITLE_KINDS:
                errors.append(f"{path}.title_kind: must be one of {sorted(TITLE_KINDS)}")
            if not _nonempty_str(payload.get("title")):
                errors.append(f"{path}.title: non-empty title required")
        elif layer_type == "stat":
            if not _nonempty_str(payload.get("label")):
                errors.append(f"{path}.label: non-empty label required")
            value = payload.get("value")
            if not (_nonempty_str(value) or isinstance(value, (int, float))):
                errors.append(f"{path}.value: stat value required")
            if not _nonempty_str(payload.get("evidence_id")):
                errors.append(f"{path}.evidence_id: every stat must bind evidence")
            if not _nonempty_str(payload.get("source_literal")):
                errors.append(f"{path}.source_literal: stat must quote its source")
        elif layer_type == "chart":
            if payload.get("chart_kind") not in {"bar", "line"}:
                errors.append(f"{path}.chart_kind: must be 'bar' or 'line' (Phase 1c)")
            datums = payload.get("datums")
            if not isinstance(datums, list) or not datums:
                errors.append(f"{path}.datums: at least one datum required")
            else:
                for datum_index, datum in enumerate(datums):
                    datum_path = f"{path}.datums[{datum_index}]"
                    if not isinstance(datum, dict):
                        errors.append(f"{datum_path}: must be an object")
                        continue
                    if not _nonempty_str(datum.get("label")):
                        errors.append(f"{datum_path}.label: required")
                    if isinstance(datum.get("value"), bool) or not isinstance(
                        datum.get("value"), (int, float)
                    ):
                        errors.append(f"{datum_path}.value: numeric value required")
                    if not _nonempty_str(datum.get("evidence_id")):
                        errors.append(
                            f"{datum_path}.evidence_id: every chart datum must bind evidence"
                        )
                    if not _nonempty_str(datum.get("source_literal")):
                        errors.append(
                            f"{datum_path}.source_literal: chart datum must quote its source"
                        )
        elif layer_type == "dynamic_list":
            entries = payload.get("items")
            if not isinstance(entries, list) or not entries:
                errors.append(f"{path}.items: at least one list item required")
            else:
                for entry_index, entry in enumerate(entries):
                    entry_path = f"{path}.items[{entry_index}]"
                    if not isinstance(entry, dict) or not _nonempty_str(entry.get("text")):
                        errors.append(f"{entry_path}.text: required")
                        continue
                    if not _nonempty_str(entry.get("evidence_id")) and entry.get(
                        "conceptual"
                    ) is not True:
                        errors.append(
                            f"{entry_path}: needs evidence_id or explicit conceptual:true"
                        )
        elif layer_type == "mosaic":
            evidence_id = payload.get("evidence_id")
            source_literal = payload.get("source_literal")
            if not _nonempty_str(payload.get("title")):
                errors.append(f"{path}.title: non-empty transcript title required")
            if not _nonempty_str(evidence_id) or not _nonempty_str(source_literal):
                errors.append(f"{path}: mosaic must bind transcript evidence")
            assets = payload.get("assets")
            if not isinstance(assets, list) or not 2 <= len(assets) <= 4:
                errors.append(f"{path}.assets: mosaic requires two to four assets")
                continue
            seen_ids: set[str] = set()
            seen_paths: set[str] = set()
            for asset_index, asset in enumerate(assets):
                asset_path = f"{path}.assets[{asset_index}]"
                required = {
                    "asset_id", "path", "sha256", "evidence_id", "source_literal"
                }
                if not isinstance(asset, dict) or set(asset) != required:
                    errors.append(f"{asset_path}: exact frozen asset descriptor required")
                    continue
                asset_id = asset.get("asset_id")
                rel_path = asset.get("path")
                digest = asset.get("sha256")
                if not _nonempty_str(asset_id):
                    errors.append(f"{asset_path}.asset_id: non-empty id required")
                if (
                    not _nonempty_str(rel_path)
                    or str(rel_path).startswith("/")
                    or "\\" in str(rel_path)
                    or any(part in {"", ".", ".."} for part in str(rel_path).split("/"))
                ):
                    errors.append(f"{asset_path}.path: normalized project-relative path required")
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    errors.append(f"{asset_path}.sha256: lowercase SHA-256 required")
                if asset.get("evidence_id") != evidence_id:
                    errors.append(f"{asset_path}.evidence_id: must match mosaic evidence")
                if asset.get("source_literal") != source_literal:
                    errors.append(f"{asset_path}.source_literal: must match mosaic source")
                if asset_id in seen_ids or rel_path in seen_paths:
                    errors.append(f"{asset_path}: duplicate asset identity")
                seen_ids.add(asset_id)
                seen_paths.add(rel_path)
    return errors


def semantic_video_analysis(analysis) -> list[str]:
    """OCR is an optional capability: absent engine ⇒ no ocr fields at all."""
    engines = analysis.get("engines", {})
    ocr = engines.get("ocr", {}) if isinstance(engines, dict) else {}
    if ocr.get("status") == "not_configured" and analysis.get("ocr_spans"):
        return [
            "$.ocr_spans: must be empty while engines.ocr.status is not_configured "
            "(contracts/policies/ANALYSIS_ENGINE.md)"
        ]
    return []


def semantic_visual_plan(visual_plan, structured_layers=None) -> list[str]:
    errors = []
    scene_fields = {
        "eligibility",
        "eligibility_reason",
        "family",
        "role",
        "importance",
        "major_graphic",
        "micro_silent",
        "stage",
        "trigger_role",
    }
    layer_by_id = None
    if structured_layers is not None:
        layer_by_id = {item["id"]: item for item in structured_layers.get("items", [])}
    for index, item in enumerate(visual_plan.get("items", [])):
        path = f"$.items[{index}]"
        if item.get("end", 0) <= item.get("start", 0):
            errors.append(f"{path}: end must be greater than start")
        conceptual = item.get("conceptual_only", False)
        beat = item.get("beat")
        if scene_fields.intersection(item):
            missing = sorted(scene_fields.difference(item))
            if missing:
                errors.append(f"{path}: scene contract missing {missing}")
            elif item.get("eligibility") == "eligible" and item.get(
                "eligibility_reason"
            ) is not None:
                errors.append(
                    f"{path}.eligibility_reason: eligible scene requires null"
                )
            elif item.get("eligibility") == "ineligible" and item.get(
                "eligibility_reason"
            ) is None:
                errors.append(
                    f"{path}.eligibility_reason: ineligible scene requires a reason"
                )
            if item.get("role") == "section_title" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "title_reveal"
                or item.get("importance") != "high"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "scene_transition"
                or beat != "title"
                or conceptual is not False
            ):
                errors.append(f"{path}: section-title scene contract is inconsistent")
            if item.get("role") == "opening_title" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "title_reveal"
                or item.get("importance") != "high"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "full_screen_graphic"
                or item.get("trigger_role") != "title_enter"
                or beat != "title"
                or conceptual is not False
            ):
                errors.append(f"{path}: opening-title scene contract is inconsistent")
            if item.get("role") == "metric_emphasis" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "count_stat"
                or item.get("importance") != "high"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "count_complete"
                or beat != "stat"
                or conceptual is not False
            ):
                errors.append(f"{path}: metric-emphasis scene contract is inconsistent")
            if item.get("role") == "list_explanation" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "staggered_list"
                or item.get("importance") != "medium"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "row_reveal"
                or beat != "dynamic_list"
                or conceptual is not False
            ):
                errors.append(f"{path}: list-explanation scene contract is inconsistent")
            if item.get("role") == "data_explanation" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "analytics_dashboard"
                or item.get("importance") != "high"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "chart_complete"
                or beat != "chart"
                or conceptual is not False
            ):
                errors.append(f"{path}: data-explanation scene contract is inconsistent")
            if item.get("role") == "prompt_command" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "typed_prompt"
                or item.get("importance") != "medium"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "typing"
                or beat != "typed_prompt"
                or conceptual is not False
            ):
                errors.append(f"{path}: prompt-command scene contract is inconsistent")
            if item.get("role") == "workflow_progress" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "grid_progress"
                or item.get("importance") != "medium"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "grid_complete"
                or beat != "grid_progress"
                or conceptual is not False
            ):
                errors.append(f"{path}: workflow-progress scene contract is inconsistent")
            if item.get("role") == "asset_showcase" and (
                item.get("eligibility") != "eligible"
                or item.get("family") != "asset_mosaic"
                or item.get("importance") != "high"
                or item.get("major_graphic") is not True
                or item.get("micro_silent") is not False
                or item.get("stage") != "split_graphic_presenter"
                or item.get("trigger_role") != "scene_transition"
                or beat != "asset_mosaic"
                or conceptual is not False
            ):
                errors.append(f"{path}: asset-showcase scene contract is inconsistent")
        if beat in {"stat", "chart", "dynamic_list", "grid_progress", "asset_mosaic"} and not conceptual and not item.get("evidence_ids"):
            errors.append(f"{path}: factual beat requires evidence_ids unless conceptual_only")
        if beat in STRUCTURED_BEATS:
            if item.get("structured_layer_id") is None:
                errors.append(f"{path}: structured beat requires structured_layer_id")
            if item.get("selected_asset") is not None:
                errors.append(f"{path}: structured beat must keep selected_asset null")
        else:
            if item.get("structured_layer_id") is not None:
                errors.append(f"{path}: non-structured beat must not bind a structured layer")
        layer_id = item.get("structured_layer_id")
        # Cross-artifact back-reference check only runs when the layer bundle
        # is supplied; standalone fixture validation has no bundle context.
        if layer_id is not None and layer_by_id is not None:
            layer = layer_by_id.get(layer_id)
            if layer is None:
                errors.append(f"{path}: structured_layer_id {layer_id!r} does not exist")
            elif layer.get("visual_plan_item_id") != item.get("id"):
                errors.append(
                    f"{path}: structured layer {layer_id!r} does not reference this item "
                    "(timing SSOT back-reference broken)"
                )
    return errors


def semantic_evidence_references(artifact, evidence_map) -> list[str]:
    known = {item["id"] for item in evidence_map.get("items", [])}
    errors = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            if path.endswith(".evidence_ids"):
                for evidence_id in node:
                    if evidence_id not in known:
                        errors.append(f"{path}: unknown evidence id {evidence_id!r}")
            else:
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

    walk(artifact, "$")
    return errors


def semantic_master_timeline(timeline) -> list[str]:
    errors = []
    total = 0.0
    for index, segment in enumerate(timeline.get("segments", [])):
        path = f"$.segments[{index}]"
        if segment.get("source_end", 0) <= segment.get("source_start", 0):
            errors.append(f"{path}: source_end must be greater than source_start")
        else:
            total += segment["source_end"] - segment["source_start"]
    declared = timeline.get("post_cut_duration_s")
    if declared is not None and abs(declared - total) > 1.0 / 24.0:
        errors.append(
            f"$.post_cut_duration_s: {declared} disagrees with segment sum {total:.6f}"
        )
    return errors


def semantic_approval_receipt(receipt) -> list[str]:
    errors = []
    if (
        receipt.get("gate") == "final"
        and receipt.get("approved")
        and not receipt.get("approved_preview_artifact_hash")
    ):
        errors.append(
            "$.approved_preview_artifact_hash: final approval must bind the reviewed "
            "preview artifact hash"
        )
    return errors


def semantic_rights_assertion(assertion) -> list[str]:
    errors = []
    for index, item in enumerate(assertion.get("items", [])):
        if item.get("basis") == "licensed" and not item.get("license_proof"):
            errors.append(
                f"$.items[{index}]: basis 'licensed' requires a license_proof path"
            )
    return errors


CARD_MIN_SECONDS = 0.6


def semantic_card_plan(artifact) -> list[str]:
    """Rules the shape cannot express: real spans, unique ids, one card at a time.

    Two cards live at once only ever means a layout collision on screen, so
    the plan refuses to hold one rather than leaving the renderer to discover
    it. Manual entries are merged before validation, so a clash here is a
    clash the author can still see and fix.
    """
    errors: list[str] = []
    items = artifact.get("items")
    if not isinstance(items, list):
        return ["card plan items must be an array"]
    seen: set[str] = set()
    spans: list[tuple[float, float, str]] = []
    for index, item in enumerate(items):
        identifier = str(item.get("id", index))
        if identifier in seen:
            errors.append(f"card {identifier} is listed twice")
        seen.add(identifier)
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            errors.append(f"card {identifier} timing must be numeric")
            continue
        if end <= start:
            errors.append(f"card {identifier} ends at or before it starts")
            continue
        if end - start < CARD_MIN_SECONDS:
            # Enforced here rather than at the one CLI that happens to add
            # cards today, so every producer meets it — a flash too brief to
            # read is a defect however it got proposed.
            errors.append(
                f"card {identifier} is on screen for {end - start:.2f}s, "
                f"under the {CARD_MIN_SECONDS}s it takes to read one"
            )
        if not item.get("payload"):
            errors.append(f"card {identifier} has an empty payload")
        spans.append((start, end, identifier))
    spans.sort()
    for (start, end, identifier), (next_start, _, next_id) in zip(spans, spans[1:]):
        if next_start < end - 0.001:
            errors.append(
                f"cards {identifier} and {next_id} are on screen at the same time"
            )
    return errors


def semantic_director_mode(artifact) -> list[str]:
    """Validate director-mode invariants beyond the schema dialect."""
    errors: list[str] = []
    mode_ids: set[str] = set()
    for index, mode in enumerate(artifact.get("modes", [])):
        path = f"$.modes[{index}]"
        mode_id = mode.get("id")
        if not _nonempty_str(mode_id):
            errors.append(f"{path}.id: must be a non-empty string")
        else:
            normalized_mode_id = mode_id.strip()
            if mode_id != normalized_mode_id:
                errors.append(f"{path}.id: must not have surrounding whitespace")
            if normalized_mode_id in mode_ids:
                errors.append(
                    f"{path}.id: duplicate mode id {normalized_mode_id!r}"
                )
            else:
                mode_ids.add(normalized_mode_id)

        has_experience = "experience" in mode
        has_capabilities = "required_capabilities" in mode
        if has_experience != has_capabilities:
            errors.append(
                f"{path}: experience and required_capabilities must both be present or absent"
            )
        if has_experience:
            experience = mode["experience"]
            if experience.get("motion_intensity") != mode.get("envelope", {}).get(
                "motion_intensity"
            ):
                errors.append(
                    f"{path}.experience.motion_intensity must match envelope.motion_intensity"
                )
            if experience.get("visual_density") != mode.get("constraints", {}).get(
                "visual_density"
            ):
                errors.append(
                    f"{path}.experience.visual_density must match constraints.visual_density"
                )

        if has_capabilities:
            capability_ids = mode["required_capabilities"].get("ids", [])
            seen_capability_ids: set[str] = set()
            for capability_index, capability_id in enumerate(capability_ids):
                capability_path = f"{path}.required_capabilities.ids[{capability_index}]"
                if not _nonempty_str(capability_id):
                    errors.append(f"{capability_path}: must be non-empty")
                    continue
                normalized_capability_id = capability_id.strip()
                if normalized_capability_id in seen_capability_ids:
                    errors.append(
                        f"{capability_path}: duplicate capability id {normalized_capability_id!r}"
                    )
                else:
                    seen_capability_ids.add(normalized_capability_id)

        seen_rules: set[str] = set()
        for rule_index, rule in enumerate(mode.get("auto_select_rules", [])):
            rule_path = f"{path}.auto_select_rules[{rule_index}]"
            if not _nonempty_str(rule):
                errors.append(f"{rule_path}: must be a non-empty string")
                continue
            normalized_rule = rule.strip()
            if normalized_rule in seen_rules:
                errors.append(f"{rule_path}: duplicate auto_select_rule {normalized_rule!r}")
            else:
                seen_rules.add(normalized_rule)
    return errors


def semantic_delivery_envelope(artifact) -> list[str]:
    """Validate delivery-envelope invariants not expressible in the dialect."""
    errors: list[str] = []
    if type(artifact.get("schema_version")) is not int or artifact.get("schema_version") != 1:
        errors.append("$.schema_version: must be integer 1")
    state = artifact.get("state")
    prepared_hash = artifact.get("prepared_envelope_hash")
    if state == "prepared" and prepared_hash is not None:
        errors.append("$.prepared_envelope_hash: prepared envelope must be null")
    if state == "finalized" and not isinstance(prepared_hash, str):
        errors.append(
            "$.prepared_envelope_hash: finalized envelope must bind a canonical prepared hash"
        )
    artifacts = artifact.get("artifacts")
    if isinstance(artifacts, dict):
        for required_name in (
            "output",
            "qa_report",
            "contact_sheet",
            "visual_evidence",
            "motion_evidence",
        ):
            if artifacts.get(required_name) is None:
                errors.append(f"$.artifacts.{required_name}: required for direct delivery")
        for name, item in artifacts.items():
            if item is None:
                continue
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if name != "output" and isinstance(path, str) and (path.startswith("/") or "\\" in path):
                errors.append(f"$.artifacts.{name}.path: must be a project-relative POSIX path")
            if name != "output" and isinstance(path, str):
                parts = path.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    errors.append(f"$.artifacts.{name}.path: must be normalized")
            digest = item.get("sha256")
            if isinstance(digest, str) and digest != digest.lower():
                errors.append(f"$.artifacts.{name}.sha256: must use lowercase hexadecimal")
        caption_v2 = artifacts.get("caption_v2")
        if isinstance(caption_v2, dict) and caption_v2.get("path") != "working/caption_delivery_v2.json":
            errors.append(
                "$.artifacts.caption_v2.path: must be working/caption_delivery_v2.json"
            )
        sfx_names = ("audio_event_plan", "audio_catalog", "sfx_stem")
        sfx_present = [artifacts.get(name) is not None for name in sfx_names]
        if any(sfx_present) and not all(sfx_present):
            errors.append("$.artifacts: SFX plan, catalog and stem must be all-null or all-present")
        profile = artifact.get("profile")
        if isinstance(profile, dict) and profile.get("id") == "kinetic-explainer" and not all(sfx_present):
            errors.append("$.artifacts: kinetic-explainer direct delivery requires SFX artifacts")
    renderer = artifact.get("renderer_identity")
    if isinstance(renderer, dict):
        if type(renderer.get("contract_version")) is not int or renderer.get("contract_version") != 1:
            errors.append("$.renderer_identity.contract_version: must be integer 1")
    return errors


def semantic_sfx_catalog(artifact) -> list[str]:
    """Validate generated-asset measurements, identity and role mapping."""
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return errors
    if type(artifact.get("schema_version")) is not int or artifact.get("schema_version") != 1:
        errors.append("$.schema_version: must be exact integer 1")
    assets = artifact.get("assets")
    if not isinstance(assets, list):
        return errors
    expected_roles = {
        "soft-ui-tick-v1": "title_enter",
        "short-pop-v1": "row_reveal",
        "short-whoosh-v1": "transition",
        "soft-impact-v1": "grid_fill",
        "short-riser-v1": "count_tick",
        "typing-tick-v1": "typing",
        "completion-chime-v1": "complete",
    }
    expected_generators = {
        "soft-ui-tick-v1": ("soft_ui_tick", "two_sine_exponential_decay"),
        "short-pop-v1": ("short_pop", "sine_pop_exponential_decay"),
        "short-whoosh-v1": ("short_whoosh", "swept_sine_fade"),
        "soft-impact-v1": ("soft_impact", "low_tone_exponential_decay"),
        "short-riser-v1": ("short_riser", "swept_sine_rise"),
        "typing-tick-v1": ("typing_tick", "short_sine_exponential_decay"),
        "completion-chime-v1": ("completion_chime", "two_note_exponential_decay"),
    }
    if len(assets) not in (1, len(expected_roles)):
        errors.append("$.assets: must contain one legacy asset or the complete seven-asset starter pack")
    seen_ids: set[str] = set()
    seen_roles: set[str] = set()
    for index, asset in enumerate(assets):
        path = f"$.assets[{index}]"
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("asset_id")
        role = asset.get("role")
        if asset_id in seen_ids:
            errors.append(f"{path}.asset_id: duplicate asset id")
        seen_ids.add(asset_id)
        if role in seen_roles:
            errors.append(f"{path}.role: duplicate role")
        seen_roles.add(role)
        expected_role = expected_roles.get(asset_id)
        if expected_role is None:
            errors.append(f"{path}.asset_id: unknown starter asset id")
        elif role != expected_role:
            errors.append(f"{path}.role: must map {asset_id} to {expected_role}")
        generator = asset.get("generator")
        expected_generator = expected_generators.get(asset_id)
        if expected_generator is not None and isinstance(generator, dict):
            if type(generator.get("version")) is not int or generator.get("version") != 1:
                errors.append(f"{path}.generator.version: must be exact integer 1")
            if generator.get("name") != expected_generator[0]:
                errors.append(f"{path}.generator.name: does not match asset id")
            if generator.get("recipe") != expected_generator[1]:
                errors.append(f"{path}.generator.recipe: does not match asset id")
        duration = asset.get("duration_samples")
        anchor = asset.get("transient_anchor_sample")
        if type(duration) is int and type(anchor) is int and not 0 <= anchor < duration:
            errors.append(f"{path}.transient_anchor_sample: must be below duration_samples")
        rms = asset.get("rms_dbfs")
        if isinstance(rms, (int, float)) and not isinstance(rms, bool) and rms <= -45:
            errors.append(f"{path}.rms_dbfs: must be above -45 dBFS")
        peak = asset.get("peak_dbfs")
        if isinstance(peak, (int, float)) and not isinstance(peak, bool) and not -12 <= peak <= -1:
            errors.append(f"{path}.peak_dbfs: must be between -12 and -1 dBFS")
        provenance = asset.get("provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            errors.append(f"{path}.provenance: must be non-empty")
        if asset.get("review_state") != "approved_generated":
            errors.append(f"{path}.review_state: generated asset must be approved_generated")
        pack = asset.get("pack")
        if len(assets) == len(expected_roles) and pack != "phase1-local-procedural":
            errors.append(f"{path}.pack: complete starter catalog must use phase1-local-procedural")
        elif len(assets) == 1 and pack != "phase0d-local-procedural":
            errors.append(f"{path}.pack: legacy catalog must use phase0d-local-procedural")
    if len(assets) == len(expected_roles):
        if seen_ids != set(expected_roles):
            errors.append("$.assets: complete starter catalog must contain every expected asset id")
        if seen_roles != set(expected_roles.values()):
            errors.append("$.assets: complete starter catalog must contain every expected role")
    elif len(assets) == 1 and seen_ids != {"soft-ui-tick-v1"}:
        errors.append("$.assets: legacy single-asset catalog must contain soft-ui-tick-v1")
    return errors


def semantic_audio_event_plan(artifact) -> list[str]:
    """Validate final-domain event arithmetic and renderer evidence binding."""
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return errors
    version = artifact.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        errors.append("$.schema_version: must be exact integer 1 or 2")
        return errors
    events = artifact.get("events")
    if not isinstance(events, list):
        return errors
    if version == 1:
        if len(events) != 1:
            errors.append("$.events: schema v1 requires exactly one event")
        if any(field in artifact for field in ("mix_mode", "density", "studio_edits", "studio_edits_sha256")):
            errors.append("$: schema v1 must not contain schema-v2 fields")
    elif "mix_mode" not in artifact or "density" not in artifact:
        errors.append("$: schema v2 requires mix_mode and density evidence")

    role_priority = {
        "title_enter": 0,
        "transition": 0,
        "grid_fill": 1,
        "count_tick": 1,
        "complete": 1,
        "row_reveal": 2,
        "typing": 3,
    }
    trigger_role = {
        ("title", "prompt-card", "word-cascade"): ("typing", "typing-tick-v1"),
        ("title", None, "pop"): ("title_enter", "soft-ui-tick-v1"),
        ("title", None, "pop-in"): ("title_enter", "soft-ui-tick-v1"),
        ("title", None, "slide-up"): ("title_enter", "soft-ui-tick-v1"),
        ("title", None, "slide-in"): ("title_enter", "soft-ui-tick-v1"),
    }

    def role_for_trigger(trigger):
        if not isinstance(trigger, dict):
            return None
        motion = trigger.get("motion")
        if not isinstance(motion, dict):
            return None
        kind = trigger.get("kind")
        component = trigger.get("component_id")
        requested = motion.get("requested")
        if (
            kind == "title"
            and trigger.get("title_kind") == "section"
            and component in {None, "kinetic-title", "title-lockup"}
            and requested in {"pop", "pop-in", "slide-up", "slide-in"}
        ):
            return ("transition", "short-whoosh-v1")
        direct = trigger_role.get((kind, component, requested))
        if direct is not None:
            return direct
        if kind == "title" and component in {None, "kinetic-title", "title-lockup"} and requested in {
            "pop", "pop-in", "slide-up", "slide-in"
        }:
            return ("title_enter", "soft-ui-tick-v1")
        if kind == "dynamic_list" and component in {"dynamic-list", "warning-checklist"} and requested in {
            "staggered-reveal", "check-pop"
        }:
            return ("row_reveal", "short-pop-v1")
        if (
            kind == "stat"
            and component == "progress"
            and trigger.get("family") == "grid_progress"
            and trigger.get("trigger_role") == "grid_complete"
            and requested == "fill"
        ):
            return ("complete", "completion-chime-v1")
        if kind == "stat" and component in {"hero-stat", "progress"} and requested in {"count-up", "fill"}:
            return ("count_tick", "short-riser-v1")
        if kind == "chart" and component == "dashboard" and requested in {"pan", "fill"}:
            return ("grid_fill", "soft-impact-v1")
        if (
            kind == "mosaic"
            and component == "asset-mosaic"
            and trigger.get("family") == "asset_mosaic"
            and trigger.get("trigger_role") == "scene_transition"
            and requested == "pan"
        ):
            return ("transition", "short-whoosh-v1")
        return None

    def validate_event(event, path: str, *, is_v2: bool) -> tuple[dict | None, tuple[int, int, str] | None]:
        if not isinstance(event, dict):
            return None, None
        trigger_id = event.get("trigger_id")
        if is_v2:
            gain = event.get("gain_db")
            if (
                isinstance(gain, bool)
                or not isinstance(gain, (int, float))
                or not math.isfinite(float(gain))
                or not -24 <= float(gain) <= -6
            ):
                errors.append(f"{path}.gain_db: schema v2 requires finite -24..-6 dB")
            elif "studio_edits" not in artifact and float(gain) != -12.0:
                errors.append(f"{path}.gain_db: schema v2 without studio edits requires -12")
            if event.get("duck_group") != "dialogue_priority":
                errors.append(f"{path}.duck_group: schema v2 requires dialogue_priority")
            if event.get("fades") != {"in_samples": 0, "out_samples": 0}:
                errors.append(f"{path}.fades: schema v2 requires zero fades")
        else:
            if event.get("asset_id") != "soft-ui-tick-v1":
                errors.append(f"{path}.asset_id: schema v1 requires soft-ui-tick-v1")
            if event.get("role") != "title_enter":
                errors.append(f"{path}.role: schema v1 requires title_enter")
            if event.get("gain_db") != -12:
                errors.append(f"{path}.gain_db: schema v1 requires -12")
            if event.get("duck_group") != "dialogue_priority":
                errors.append(f"{path}.duck_group: schema v1 requires dialogue_priority")
        start = event.get("event_start_sample")
        anchor = event.get("asset_transient_anchor_sample")
        expected = event.get("expected_transient_sample")
        trigger_onset = event.get("trigger_onset_sample")
        if all(type(value) is int for value in (start, anchor, expected)):
            if expected != start + anchor:
                errors.append(f"{path}.expected_transient_sample: must equal event_start_sample + asset_transient_anchor_sample")
        if all(type(value) is int for value in (expected, trigger_onset)):
            if abs(expected - trigger_onset) > 3840:
                errors.append(f"{path}: expected transient is outside 3840-sample trigger tolerance")
        duration = event.get("duration_samples")
        stem_count = artifact.get("sfx_stem_sample_count")
        if all(type(value) is int for value in (start, duration, stem_count)):
            if start < 0 or duration <= 0 or start + duration > stem_count:
                errors.append(f"{path}: event is outside sfx stem bounds")
        if is_v2 and all(type(value) is int for value in (anchor, duration)) and not 0 <= anchor < duration:
            errors.append(f"{path}.asset_transient_anchor_sample: must be below duration_samples")
        fades = event.get("fades")
        if isinstance(fades, dict) and isinstance(duration, int):
            fade_in = fades.get("in_samples")
            fade_out = fades.get("out_samples")
            if all(type(value) is int for value in (fade_in, fade_out)) and fade_in + fade_out > duration:
                errors.append(f"{path}.fades: fades exceed event duration")
        evidence = event.get("evidence")
        trigger = evidence.get("trigger") if isinstance(evidence, dict) else None
        if not isinstance(trigger, dict):
            return None, None
        if trigger.get("id") != trigger_id:
            errors.append(f"{path}.evidence.trigger.id: does not match trigger_id")
        if trigger.get("onset_sample") != trigger_onset:
            errors.append(f"{path}.evidence.trigger.onset_sample: does not match trigger_onset_sample")
        motion = trigger.get("motion")
        requested = motion.get("requested") if isinstance(motion, dict) else None
        delivered = motion.get("delivered") if isinstance(motion, dict) else None
        status = motion.get("status") if isinstance(motion, dict) else None
        if not is_v2:
            if "component_id" in trigger:
                errors.append(f"{path}.evidence.trigger.component_id: v1 field is not allowed")
            if "title_kind" in trigger:
                errors.append(f"{path}.evidence.trigger.title_kind: v1 field is not allowed")
            if isinstance(evidence, dict) and "renderer_trigger_sha256" in evidence:
                errors.append(f"{path}.evidence.renderer_trigger_sha256: v1 field is not allowed")
            if trigger.get("kind") != "title":
                errors.append(f"{path}.evidence.trigger.kind: must be title")
            if not (requested == "pop" or (isinstance(requested, str) and requested.startswith("slide"))):
                errors.append(f"{path}.evidence.trigger.motion.requested: unsupported trigger motion")
            if not isinstance(delivered, str) or not delivered.strip() or delivered.lower() in {"none", "static"}:
                errors.append(f"{path}.evidence.trigger.motion.delivered: motion was not delivered")
            if not isinstance(motion, dict) or status == "fallback" or motion.get("faithful") is not True:
                errors.append(f"{path}.evidence.trigger.motion: trigger is not faithful")
            return trigger, None
        if not (
            isinstance(requested, str)
            and isinstance(delivered, str)
            and requested
            and delivered == requested
            and delivered.lower() not in {"none", "static"}
        ):
            errors.append(f"{path}.evidence.trigger.motion: motion was not faithfully delivered")
        if status not in {"native", "rendered"} or not isinstance(motion, dict) or motion.get("faithful") is not True:
            errors.append(f"{path}.evidence.trigger.motion: trigger is not faithful")
        expected_trigger_fields = {
            "id", "onset_sample", "kind", "component_id", "motion"
        }
        if trigger.get("title_kind") == "section":
            expected_trigger_fields.add("title_kind")
        has_scene_family = trigger.get("family") in {"grid_progress", "asset_mosaic"}
        has_scene_trigger = trigger.get("trigger_role") in {
            "grid_complete", "scene_transition"
        }
        if has_scene_family or has_scene_trigger:
            expected_trigger_fields.update({"family", "trigger_role"})
            if (trigger.get("family"), trigger.get("trigger_role")) not in {
                ("grid_progress", "grid_complete"),
                ("asset_mosaic", "scene_transition"),
            }:
                errors.append(
                    f"{path}.evidence.trigger: scene family and trigger_role are inconsistent"
                )
        if set(trigger) != expected_trigger_fields:
            errors.append(f"{path}.evidence.trigger: schema v2 normalized trigger fields are required")
        if not isinstance(evidence, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("renderer_trigger_sha256", ""))):
            errors.append(f"{path}.evidence.renderer_trigger_sha256: canonical renderer hash is required")
        expected_role_asset = role_for_trigger(trigger)
        if expected_role_asset is None:
            errors.append(f"{path}.evidence.trigger: no approved role mapping")
        elif (event.get("role"), event.get("asset_id")) != expected_role_asset:
            errors.append(f"{path}: role/asset does not match normalized renderer evidence")
        return trigger, (start, start + duration, trigger_id) if type(start) is int and type(duration) is int else None

    seen_ids: set[str] = set()
    seen_triggers: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for index, event in enumerate(events):
        path = f"$.events[{index}]"
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        trigger_id = event.get("trigger_id")
        if version == 2 and event.get("id") != f"sfx-event-{index + 1:04d}":
            errors.append(f"{path}.id: schema v2 event id must be canonical sequence")
        if event_id in seen_ids:
            errors.append(f"{path}.id: duplicate event id")
        seen_ids.add(event_id)
        if trigger_id in seen_triggers:
            errors.append(f"{path}.trigger_id: duplicate trigger id")
        seen_triggers.add(trigger_id)
        _, interval = validate_event(event, path, is_v2=version == 2)
        if interval is not None:
            intervals.append(interval)

    if version != 2:
        return errors

    studio_edits = artifact.get("studio_edits")
    studio_hash = artifact.get("studio_edits_sha256")
    if (studio_edits is None) != (studio_hash is None):
        errors.append("$: studio_edits and studio_edits_sha256 must be all-or-none")
    elif isinstance(studio_edits, dict):
        if studio_hash != canonical_hash(studio_edits):
            errors.append("$.studio_edits_sha256: does not match canonical studio edits")
        if studio_edits.get("source_timeline_revision") != artifact.get("timeline_revision"):
            errors.append("$.studio_edits.source_timeline_revision: does not match plan timeline")
        edits = studio_edits.get("events")
        edits = edits if isinstance(edits, list) else []
        edit_by_id = {
            item.get("id"): item for item in edits if isinstance(item, dict)
        }
        if len(edit_by_id) != len(edits):
            errors.append("$.studio_edits.events: event ids must be unique")
        if [item.get("id") for item in edits if isinstance(item, dict)] != sorted(edit_by_id):
            errors.append("$.studio_edits.events: event ids must be ordered")
        plan_by_id = {
            item.get("id"): item for item in events if isinstance(item, dict)
        }
        if not edit_by_id or not set(edit_by_id).issubset(plan_by_id):
            errors.append("$.studio_edits.events: must be a non-empty subset of plan events")
        for event_id, edit in edit_by_id.items():
            event = plan_by_id.get(event_id)
            if not isinstance(event, dict):
                continue
            if (
                event.get("event_start_sample") != edit.get("event_start_sample")
                or event.get("gain_db") != edit.get("gain_db")
            ):
                errors.append(f"$.studio_edits.events[{event_id!r}]: does not match resolved event")

    trigger_order = [
        (event.get("trigger_onset_sample"), event.get("trigger_id"))
        for event in events
        if isinstance(event, dict)
    ]
    if trigger_order != sorted(trigger_order):
        errors.append("$.events: trigger order must be sorted by onset and id")
    event_ids = [event.get("id") for event in events if isinstance(event, dict)]
    if event_ids != sorted(event_ids):
        errors.append("$.events: event ids must be sorted")
    expected_samples = [event.get("expected_transient_sample") for event in events if isinstance(event, dict)]
    if all(type(value) is int for value in expected_samples):
        for previous, current in zip(expected_samples, expected_samples[1:]):
            if current - previous < 5760:
                errors.append("$.events: expected transient adjacency is below 120 ms")
                break
    points = []
    for start, end, _ in intervals:
        points.extend(((start, 1), (end, -1)))
    active = 0
    for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
        active += delta
        if active > 2:
            errors.append("$.events: half-open overlap exceeds two events")
            break

    density = artifact.get("density")
    if not isinstance(density, dict):
        return errors
    stem_count = artifact.get("sfx_stem_sample_count")
    if density.get("total_samples") != stem_count:
        errors.append("$.density.total_samples: must match sfx_stem_sample_count")
    for field, expected in (
        ("sample_rate", 48000),
        ("adjacent_onset_samples", 5760),
        ("max_overlap", 2),
        ("max_cues_per_minute", 40),
    ):
        if type(density.get(field)) is not int or density.get(field) != expected:
            errors.append(f"$.density.{field}: does not match deterministic threshold")
    expected_cap = (stem_count * 40) // (60 * 48000) if type(stem_count) is int else None
    if expected_cap is not None and density.get("density_cap") != expected_cap:
        errors.append("$.density.density_cap: does not match deterministic cap")
    if density.get("priority_order") != [
        "title_enter", "transition", "grid_fill", "complete", "count_tick", "row_reveal", "typing"
    ]:
        errors.append("$.density.priority_order: does not match deterministic priority")
    kept_ids = [event.get("trigger_id") for event in events if isinstance(event, dict)]
    dropped = density.get("dropped") if isinstance(density.get("dropped"), list) else []
    dropped_ids = [item.get("trigger_id") for item in dropped if isinstance(item, dict)]
    if density.get("kept_trigger_ids") != kept_ids:
        errors.append("$.density.kept_trigger_ids: does not match events")
    if density.get("dropped_trigger_ids") != dropped_ids:
        errors.append("$.density.dropped_trigger_ids: does not match dropped evidence")
    candidate_ids = density.get("candidate_order")
    if candidate_ids != [event_id for _, _, event_id in sorted(
        [
            (role_priority.get(event.get("role"), 99), event.get("trigger_onset_sample"), event.get("trigger_id"))
            for event in events if isinstance(event, dict)
        ] + [
            (role_priority.get(item.get("role"), 99), item.get("trigger_onset_sample"), item.get("trigger_id"))
            for item in dropped if isinstance(item, dict)
        ]
    )]:
        errors.append("$.density.candidate_order: does not match deterministic candidate order")
    reason_counts: dict[str, int] = {}
    dropped_by_id: dict[str, dict] = {}
    for item in dropped:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if item.get("trigger_id") in dropped_by_id:
            errors.append("$.density.dropped: duplicate trigger id")
        dropped_by_id[item.get("trigger_id")] = item
        _, _ = validate_event({**item, "id": f"dropped-{item.get('trigger_id')}", "gain_db": -12, "fades": {"in_samples": 0, "out_samples": 0}, "duck_group": "dialogue_priority", "review_state": "approved_generated", "reason": reason}, f"$.density.dropped[{dropped.index(item)}]", is_v2=True)
    if density.get("dropped_reasons") != reason_counts:
        errors.append("$.density.dropped_reasons: does not match dropped evidence")
    if set(kept_ids) & set(dropped_ids):
        errors.append("$.density: kept and dropped trigger ids must be disjoint")
    candidate_records = [
        event for event in events if isinstance(event, dict)
    ] + [item for item in dropped if isinstance(item, dict)]
    candidate_by_id = {
        item.get("trigger_id"): item
        for item in candidate_records
        if isinstance(item.get("trigger_id"), str)
    }
    if len(candidate_by_id) != len(candidate_records):
        errors.append("$.density: candidate trigger ids must be unique")
    if set(candidate_by_id) != set(candidate_ids or []):
        errors.append("$.density.candidate_order: must contain every kept and dropped trigger")
    simulated_kept: list[dict] = []
    for candidate_id in candidate_ids or []:
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        expected_reason = None
        candidate_expected = candidate.get("expected_transient_sample")
        if type(candidate_expected) is int and any(
            type(item.get("expected_transient_sample")) is int
            and abs(candidate_expected - item["expected_transient_sample"]) < 5760
            for item in simulated_kept
        ):
            expected_reason = "adjacent_onset"
        else:
            points = []
            for item in (*simulated_kept, candidate):
                start = item.get("event_start_sample")
                duration = item.get("duration_samples")
                if type(start) is int and type(duration) is int:
                    points.extend(((start, 1), (start + duration, -1)))
            active = 0
            for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
                active += delta
                if active > 2:
                    expected_reason = "overlap_limit"
                    break
        if expected_reason is None and len(simulated_kept) >= density.get("density_cap", 0):
            expected_reason = "clip_density_limit"
        if candidate_id in set(kept_ids):
            if expected_reason is not None:
                errors.append(f"$.density: kept candidate {candidate_id!r} should be dropped for {expected_reason}")
            else:
                simulated_kept.append(candidate)
        else:
            actual_reason = candidate.get("reason")
            if expected_reason != actual_reason:
                errors.append(
                    f"$.density.dropped[{candidate_id!r}].reason: expected {expected_reason!r}"
                )
    return errors


def semantic_transcript_source(artifact) -> list[str]:
    errors: list[str] = []
    words = artifact.get("raw_words")
    if isinstance(words, list):
        previous_start = -1
        for index, word in enumerate(words):
            if not isinstance(word, dict):
                continue
            if word.get("source_word_index") != index:
                errors.append(f"$.raw_words[{index}].source_word_index: must equal ordered index")
            start = word.get("start_us")
            end = word.get("end_us")
            if type(start) is int and type(end) is int:
                if end < start:
                    errors.append(f"$.raw_words[{index}]: end_us precedes start_us")
                if start < previous_start:
                    errors.append(f"$.raw_words[{index}]: raw words are unordered")
                previous_start = start
    material = dict(artifact)
    revision = material.pop("revision", None)
    if isinstance(revision, str) and revision != canonical_hash(material):
        errors.append("$.revision: does not match canonical source payload")
    return errors


def semantic_caption_segmentation(artifact) -> list[str]:
    errors: list[str] = []
    spans = artifact.get("spans")
    if isinstance(spans, list):
        for index, span in enumerate(spans):
            if not isinstance(span, dict):
                continue
            first = span.get("first_source_word_index")
            last = span.get("last_source_word_index")
            start = span.get("fallback_start_us")
            end = span.get("fallback_end_us")
            indexed = type(first) is int and type(last) is int and start is None and end is None
            fallback = first is None and last is None and type(start) is int and type(end) is int
            if not (indexed or fallback):
                errors.append(f"$.spans[{index}]: must use exactly one span identity mode")
            elif indexed and last < first:
                errors.append(f"$.spans[{index}]: word span is reversed")
            elif fallback and end <= start:
                errors.append(f"$.spans[{index}]: fallback span is empty or reversed")
    material = dict(artifact)
    revision = material.pop("segmentation_revision", None)
    if isinstance(revision, str) and revision != canonical_hash(material):
        errors.append("$.segmentation_revision: does not match canonical segmentation payload")
    return errors


def semantic_caption_delivery(artifact) -> list[str]:
    errors: list[str] = []
    root_fields = (
        "source_revision",
        "segmentation_revision",
        "source_list_hash",
        "cut_map_sha256",
        "timeline_revision",
        "target_language",
        "provider_receipt",
    )
    seen_instances: set[str] = set()
    previous_start = -1
    for index, item in enumerate(artifact.get("items", [])):
        if not isinstance(item, dict):
            continue
        for name in root_fields:
            if item.get(name) != artifact.get(name):
                errors.append(f"$.items[{index}].{name}: does not match root binding")
        instance_id = item.get("caption_instance_id")
        if instance_id in seen_instances:
            errors.append(f"$.items[{index}].caption_instance_id: duplicate")
        seen_instances.add(instance_id)
        source_start = item.get("source_start_us")
        source_end = item.get("source_end_us")
        final_start = item.get("final_start_us")
        final_end = item.get("final_end_us")
        if all(type(value) is int for value in (source_start, source_end, final_start, final_end)):
            if source_end <= source_start or final_end <= final_start:
                errors.append(f"$.items[{index}]: timing range is empty or reversed")
            if final_start < previous_start:
                errors.append(f"$.items[{index}]: final timing order is not canonical")
            previous_start = final_start
        corrected = item.get("corrected_source")
        corrected_hash = item.get("corrected_source_sha256")
        if isinstance(corrected, str) and isinstance(corrected_hash, str):
            actual = hashlib.sha256(corrected.encode("utf-8")).hexdigest()
            if corrected_hash != actual:
                errors.append(f"$.items[{index}].corrected_source_sha256: mismatch")
        identity = item.get("identity_preserved")
        reason = item.get("identity_reason")
        status = item.get("translation_status")
        if identity is True and (reason is None or status != "identity_preserved"):
            errors.append(f"$.items[{index}]: identity exception is incomplete")
        if identity is False and (reason is not None or status != "translated"):
            errors.append(f"$.items[{index}]: translated item has identity exception fields")
    return errors


SEMANTIC_VALIDATORS = {
    "card_plan": semantic_card_plan,
    "visual_plan": lambda artifact: semantic_visual_plan(artifact),
    "master_timeline": semantic_master_timeline,
    "approval_receipt": semantic_approval_receipt,
    "asset_provenance": semantic_asset_provenance,
    "provider_interface": semantic_provider_interface,
    "structured_layer": semantic_structured_layer,
    "video_analysis": semantic_video_analysis,
    "rights_assertion": semantic_rights_assertion,
    "director_mode": semantic_director_mode,
    "delivery_envelope": semantic_delivery_envelope,
    "sfx_catalog": semantic_sfx_catalog,
    "audio_event_plan": semantic_audio_event_plan,
    "transcript_source": semantic_transcript_source,
    "caption_segmentation": semantic_caption_segmentation,
    "caption_delivery": semantic_caption_delivery,
}


def validate_bundle(artifacts: dict[str, dict]) -> list[str]:
    """Cross-artifact validation over a bundle of already-schema-valid artifacts.

    Rules: evidence references must resolve against the bundle's evidence_map;
    visual_plan ↔ structured_layer back-references must agree (timing SSOT).
    """
    errors: list[str] = []
    for name, artifact in artifacts.items():
        schema_errors = validate_artifact(name, artifact)
        errors.extend(f"{name}: {error}" for error in schema_errors)
    if errors:
        return errors
    evidence_map = artifacts.get("evidence_map")
    if evidence_map is not None:
        for name in ("content_analysis", "visual_plan", "structured_layer"):
            artifact = artifacts.get(name)
            if artifact is not None:
                errors.extend(
                    f"{name}: {error}"
                    for error in semantic_evidence_references(artifact, evidence_map)
                )
    visual_plan = artifacts.get("visual_plan")
    if visual_plan is not None and "structured_layer" in artifacts:
        errors.extend(
            f"visual_plan: {error}"
            for error in semantic_visual_plan(visual_plan, artifacts["structured_layer"])
        )
    return errors


# ---------------------------------------------------------------------------
# Registry loading and fixture CLI
# ---------------------------------------------------------------------------

def load_manifest() -> list[str]:
    manifest = load_artifact_text((SCHEMA_DIR / "manifest.json").read_text("utf-8"))
    return manifest["required_schemas"]


def load_schemas() -> dict[str, dict]:
    required = load_manifest()
    on_disk = {p.name[: -len(".schema.json")] for p in SCHEMA_DIR.glob("*.schema.json")}
    if on_disk != set(required):
        missing = sorted(set(required) - on_disk)
        extra = sorted(on_disk - set(required))
        raise ContractError(f"manifest mismatch: missing={missing} extra={extra}")
    schemas = {}
    for name in required:
        schema = load_artifact_text((SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))
        check_schema_dialect(schema, name)
        schemas[name] = schema
    return schemas


def validate_artifact(name: str, artifact) -> list[str]:
    schemas = load_schemas()
    if name not in schemas:
        raise ContractError(f"unknown contract {name!r}")
    errors = validate(artifact, schemas[name])
    semantic = SEMANTIC_VALIDATORS.get(name)
    if not errors and semantic:
        errors = semantic(artifact)
    return errors


def run_fixture_suite() -> tuple[int, int, list[str]]:
    """Validate every fixture; returns (valid_count, invalid_count, failures)."""
    schemas = load_schemas()
    failures: list[str] = []
    valid_count = invalid_count = 0
    for name in schemas:
        fixture_dir = FIXTURE_DIR / name
        if not fixture_dir.is_dir():
            failures.append(f"{name}: no fixtures directory")
            continue
        valid_fixtures = sorted(fixture_dir.glob("valid*.json"))
        invalid_fixtures = sorted(fixture_dir.glob("invalid*.json"))
        if not valid_fixtures:
            failures.append(f"{name}: no valid fixture")
        if not invalid_fixtures:
            failures.append(f"{name}: no invalid fixture")
        for fixture in valid_fixtures:
            try:
                artifact = load_artifact_text(fixture.read_text("utf-8"))
                errors = validate_artifact(name, artifact)
            except ContractError as exc:
                errors = [str(exc)]
            if errors:
                failures.append(f"{fixture.relative_to(SKILL_DIR)}: {errors[0]}")
            else:
                valid_count += 1
        for fixture in invalid_fixtures:
            try:
                artifact = load_artifact_text(fixture.read_text("utf-8"))
                errors = validate_artifact(name, artifact)
            except ContractError:
                errors = ["contract error"]
            if errors:
                invalid_count += 1
            else:
                failures.append(
                    f"{fixture.relative_to(SKILL_DIR)}: expected rejection but validated clean"
                )
    return valid_count, invalid_count, failures


BUNDLE_DIR = FIXTURE_DIR / "bundles"


def run_bundle_suite() -> tuple[int, int, list[str]]:
    """Validate cross-artifact bundle fixtures.

    A bundle directory holds one JSON per schema name plus ``expect.json``
    with {"valid": bool, "error_contains": str}.
    """
    failures: list[str] = []
    valid_count = invalid_count = 0
    if not BUNDLE_DIR.is_dir():
        return 0, 0, ["no bundle fixtures directory"]
    bundle_dirs = sorted(p for p in BUNDLE_DIR.iterdir() if p.is_dir())
    if not bundle_dirs:
        failures.append("no bundle fixtures found")
    for bundle_dir in bundle_dirs:
        expect_path = bundle_dir / "expect.json"
        try:
            expect = load_artifact_text(expect_path.read_text("utf-8"))
            artifacts = {
                entry.stem: load_artifact_text(entry.read_text("utf-8"))
                for entry in sorted(bundle_dir.glob("*.json"))
                if entry.name != "expect.json"
            }
            errors = validate_bundle(artifacts)
        except (OSError, ValueError) as exc:
            failures.append(f"{bundle_dir.name}: {exc}")
            continue
        if expect.get("valid"):
            if errors:
                failures.append(f"{bundle_dir.name}: expected valid, got {errors[0]}")
            else:
                valid_count += 1
        else:
            needle = str(expect.get("error_contains") or "")
            if not errors:
                failures.append(f"{bundle_dir.name}: expected rejection but bundle is clean")
            elif needle and not any(needle in error for error in errors):
                failures.append(
                    f"{bundle_dir.name}: no error mentions {needle!r} (got {errors[0]})"
                )
            else:
                invalid_count += 1
    return valid_count, invalid_count, failures


def run_instance_suite() -> list[str]:
    failures = []
    for instance in sorted(INSTANCE_DIR.glob("*.json")):
        name = instance.stem.split("__")[0]
        try:
            artifact = load_artifact_text(instance.read_text("utf-8"))
            errors = validate_artifact(name, artifact)
        except ContractError as exc:
            errors = [str(exc)]
        for error in errors:
            failures.append(f"{instance.relative_to(SKILL_DIR)}: {error}")
    return failures


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] != "validate":
        print("usage: contract_registry.py validate", file=sys.stderr)
        return 2
    try:
        schemas = load_schemas()
    except ContractError as exc:
        print(f"FAIL schema load: {exc}", file=sys.stderr)
        return 1
    valid_count, invalid_count, failures = run_fixture_suite()
    bundle_valid, bundle_invalid, bundle_failures = run_bundle_suite()
    failures.extend(bundle_failures)
    failures.extend(run_instance_suite())
    print(
        f"schemas={len(schemas)} valid_fixtures_passed={valid_count} "
        f"invalid_fixtures_rejected={invalid_count} "
        f"bundles_valid={bundle_valid} bundles_rejected={bundle_invalid}"
    )
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

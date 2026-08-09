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
        if value != schema["const"]:
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
    "quote", "question", "comparison", "term",
}
TITLE_KINDS = {"full-screen-hook", "section", "lower-third", "quote", "hero-stat"}


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
    layer_by_id = None
    if structured_layers is not None:
        layer_by_id = {item["id"]: item for item in structured_layers.get("items", [])}
    for index, item in enumerate(visual_plan.get("items", [])):
        path = f"$.items[{index}]"
        if item.get("end", 0) <= item.get("start", 0):
            errors.append(f"{path}: end must be greater than start")
        conceptual = item.get("conceptual_only", False)
        beat = item.get("beat")
        if beat in {"stat", "chart", "dynamic_list"} and not conceptual and not item.get("evidence_ids"):
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

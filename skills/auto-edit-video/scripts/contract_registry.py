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
from pathlib import Path

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

STRUCTURED_BEATS = {"title", "stat", "chart", "dynamic_list"}
TITLE_KINDS = {"full-screen-hook", "section", "lower-third", "quote", "hero-stat"}


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


SEMANTIC_VALIDATORS = {
    "visual_plan": lambda artifact: semantic_visual_plan(artifact),
    "master_timeline": semantic_master_timeline,
    "approval_receipt": semantic_approval_receipt,
    "structured_layer": semantic_structured_layer,
    "video_analysis": semantic_video_analysis,
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

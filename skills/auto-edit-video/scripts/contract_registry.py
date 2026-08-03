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
}


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
    failures.extend(run_instance_suite())
    print(
        f"schemas={len(schemas)} valid_fixtures_passed={valid_count} "
        f"invalid_fixtures_rejected={invalid_count}"
    )
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

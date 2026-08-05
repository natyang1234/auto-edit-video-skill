#!/usr/bin/env python3
"""Evidence index, frozen content analysis, formula router and narrative plan.

Trust model (Codex-reviewed plan v2, B3): the *tool* is the evidence
authority. ``build_evidence_index`` derives every citable quote/number
deterministically from the frozen word-timed transcript; the agent may only
*reference* those IDs in its content analysis. ``freeze_content_analysis``
re-derives the index and rejects any tampered evidence map, so a fabricated
literal cannot enter the pipeline.

The formula router is deterministic over (frozen content analysis, evidence
map, versioned policy artifact): same inputs + same policy hash ⇒ same plan
hash (PRD §9.2, §7.4.1.1).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import contract_registry

TRANSCRIPT_REL = Path("working/transcript_words.json")
EVIDENCE_REL = Path("working/evidence_map.json")
CONTENT_REL = Path("working/content_analysis.json")
PLAN_REL = Path("working/viral_structure_plan.json")
NARRATIVE_REL = Path("working/narrative_edit_plan.json")
POLICY_PATH = (
    Path(__file__).resolve().parent.parent
    / "contracts/instances/viral_formula_policy.json"
)

SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]")
NUMBER_TOKEN = re.compile(r"\d|[百千萬億%％]|percent", re.IGNORECASE)


class NarrativeError(ValueError):
    """Raised for contract violations in the semantic pipeline."""


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise NarrativeError(f"required artifact missing: {path}")
    return contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scratch.replace(path)


def normalized_literal(text: str) -> str:
    """Normalisation used for literal comparison: strip spaces/case-fold."""
    return re.sub(r"\s+", "", text).casefold()


# ---------------------------------------------------------------------------
# Evidence index (tool-side authority)
# ---------------------------------------------------------------------------

def derive_evidence_items(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically derive citable evidence from word timings.

    - one ``quote`` per punctuation-delimited sentence;
    - one ``number`` per token containing digits/percent markers.
    Entries are verbatim transcript content, hence ``approved``.
    """
    items: list[dict[str, Any]] = []

    def add(kind: str, literal: str, start: float, end: float) -> None:
        literal = literal.strip()
        if not literal or end <= start:
            return
        identifier = "evidence-" + contract_registry.canonical_hash(
            {"kind": kind, "literal": literal, "start_ms": int(round(start * 1000))}
        )[:12]
        items.append(
            {
                "id": identifier,
                "kind": kind,
                "literal": literal,
                "start": round(start, 3),
                "end": round(end, 3),
                "confidence": 1.0,
                "review_status": "approved",
            }
        )

    sentence_words: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal sentence_words
        if not sentence_words:
            return
        add(
            "quote",
            "".join(str(w.get("text", "")) for w in sentence_words),
            float(sentence_words[0]["start"]),
            float(sentence_words[-1]["end"]),
        )
        sentence_words = []

    for word in words:
        text = str(word.get("text", ""))
        if not text.strip():
            continue
        # The recogniser's own segmentation is a sentence boundary too.
        # Recognisers that emit no punctuation — Breeze among them — would
        # otherwise yield one quote holding the entire transcript, and every
        # card built from it reads as a run-on cut off mid-word.
        segment_id = word.get("segment_id")
        if (
            sentence_words
            and segment_id is not None
            and segment_id != sentence_words[-1].get("segment_id")
        ):
            flush()
        sentence_words.append(word)
        if NUMBER_TOKEN.search(text):
            add("number", text, float(word["start"]), float(word["end"]))
        if SENTENCE_BREAK.search(text):
            flush()
    if sentence_words:
        add(
            "quote",
            "".join(str(w.get("text", "")) for w in sentence_words),
            float(sentence_words[0]["start"]),
            float(sentence_words[-1]["end"]),
        )
    return items


def build_evidence_index(project_dir: Path) -> dict[str, Any]:
    manifest = _read_json(project_dir / "project.json")
    transcript = _read_json(project_dir / TRANSCRIPT_REL)
    words = transcript.get("words") or []
    if not words:
        raise NarrativeError("transcript has no word timings; run transcription first")
    evidence_map = {
        "schema_version": 1,
        "source_sha256": str(manifest.get("source", {}).get("sha256") or ""),
        "transcript_revision": contract_registry.canonical_hash(transcript),
        "items": derive_evidence_items(words),
    }
    evidence_map["revision"] = contract_registry.canonical_hash(evidence_map)
    errors = contract_registry.validate_artifact("evidence_map", evidence_map)
    if errors:
        raise NarrativeError("evidence map failed contract validation: " + "; ".join(errors))
    _write_json(project_dir / EVIDENCE_REL, evidence_map)
    return evidence_map


def verify_evidence_map(project_dir: Path) -> dict[str, Any]:
    """Re-derive the index and reject any on-disk tampering (agent or human)."""
    stored = _read_json(project_dir / EVIDENCE_REL)
    manifest = _read_json(project_dir / "project.json")
    transcript = _read_json(project_dir / TRANSCRIPT_REL)
    expected = {
        "schema_version": 1,
        "source_sha256": str(manifest.get("source", {}).get("sha256") or ""),
        "transcript_revision": contract_registry.canonical_hash(transcript),
        "items": derive_evidence_items(transcript.get("words") or []),
    }
    expected["revision"] = contract_registry.canonical_hash(expected)
    if stored != expected:
        raise NarrativeError(
            "evidence_map.json does not match the transcript-derived index; "
            "it may have been edited by hand — re-run build-evidence-index"
        )
    return stored


# ---------------------------------------------------------------------------
# Frozen content analysis (agent-authored, tool-validated)
# ---------------------------------------------------------------------------

def freeze_content_analysis(
    project_dir: Path,
    draft: dict[str, Any],
    engine_id: str,
    prompt_policy_version: str,
    generated_at: str,
) -> dict[str, Any]:
    manifest = _read_json(project_dir / "project.json")
    evidence_map = verify_evidence_map(project_dir)
    analysis = dict(draft)
    analysis["schema_version"] = 1
    analysis["source_sha256"] = str(manifest.get("source", {}).get("sha256") or "")
    analysis["evidence_map_revision"] = evidence_map["revision"]
    analysis["transcript_revision"] = evidence_map["transcript_revision"]
    analysis.setdefault(
        "engine",
        {"id": engine_id, "kind": "agent", "model": engine_id, "version": "1"},
    )
    analysis["engine"] = {
        **analysis["engine"],
        "prompt_policy_version": prompt_policy_version,
    }
    analysis["generated_at"] = generated_at
    analysis["frozen"] = True
    analysis.pop("revision", None)
    analysis["revision"] = contract_registry.canonical_hash(analysis)
    errors = contract_registry.validate_bundle(
        {"evidence_map": evidence_map, "content_analysis": analysis}
    )
    if errors:
        raise NarrativeError("content analysis rejected: " + "; ".join(errors))
    duration = float(manifest.get("source", {}).get("duration_s") or 0.0)
    for index, candidate in enumerate(analysis.get("idea_candidates", [])):
        for range_index, source_range in enumerate(candidate.get("source_ranges", [])):
            if float(source_range["end"]) > duration + 0.05:
                raise NarrativeError(
                    f"idea_candidates[{index}].source_ranges[{range_index}] "
                    "extends past the source duration"
                )
    _write_json(project_dir / CONTENT_REL, analysis)
    return analysis


def load_frozen_analysis(project_dir: Path, evidence_map: dict[str, Any]) -> dict[str, Any]:
    """Load content_analysis.json and reject any post-freeze tampering.

    Verifies (a) the stored canonical revision matches the recomputed hash,
    and (b) the frozen artifact is bound to the CURRENT evidence map and
    transcript revisions — editing thesis/ranges/truth map after freeze, or
    swapping the transcript underneath, both fail closed.
    """
    analysis = _read_json(project_dir / CONTENT_REL)
    if analysis.get("frozen") is not True:
        raise NarrativeError("content analysis is not frozen; run freeze-content-analysis")
    stored_revision = analysis.get("revision")
    recomputed = contract_registry.canonical_hash(
        {key: value for key, value in analysis.items() if key != "revision"}
    )
    if stored_revision != recomputed:
        raise NarrativeError(
            "content_analysis.json was modified after freezing; re-run "
            "freeze-content-analysis on an honest draft"
        )
    if analysis.get("evidence_map_revision") != evidence_map.get("revision"):
        raise NarrativeError(
            "frozen analysis is bound to a different evidence map revision; "
            "re-run build-evidence-index and freeze again"
        )
    if analysis.get("transcript_revision") != evidence_map.get("transcript_revision"):
        raise NarrativeError(
            "frozen analysis is bound to a different transcript revision; "
            "re-freeze against the current transcript"
        )
    return analysis


# ---------------------------------------------------------------------------
# Formula router (deterministic over frozen artifacts + policy)
# ---------------------------------------------------------------------------

def load_policy() -> dict[str, Any]:
    policy = contract_registry.load_artifact_text(POLICY_PATH.read_text(encoding="utf-8"))
    errors = contract_registry.validate_artifact("viral_formula_policy", policy)
    if errors:
        raise NarrativeError("formula policy invalid: " + "; ".join(errors))
    return policy


def _candidate_features(
    candidate: dict[str, Any],
    analysis: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, float]:
    duration = sum(
        float(r["end"]) - float(r["start"]) for r in candidate.get("source_ranges", [])
    )
    evidence = [
        evidence_by_id[eid] for eid in candidate.get("evidence_ids", []) if eid in evidence_by_id
    ]
    numbers = sum(1 for item in evidence if item["kind"] == "number")
    quotes = sum(1 for item in evidence if item["kind"] == "quote")
    thesis = str(candidate.get("thesis", ""))
    contrarian_markers = policy.get("contrarian_markers", [])
    truth_map = analysis.get("truth_map", {})
    return {
        "source_completeness": 1.0 if duration > 0 and candidate.get("payoff") else 0.0,
        "hook_clarity": min(1.0, (1.0 if len(thesis) <= 40 else 0.5) + (0.5 if numbers else 0.0)),
        "audience_specificity": 1.0 if len(str(truth_map.get("target_audience", {}).get("text", ""))) >= 4 else 0.3,
        "promise_payoff": 1.0 if candidate.get("payoff") and candidate.get("evidence_ids") else 0.0,
        "proof_strength": min(1.0, (numbers * 0.4 + quotes * 0.2)),
        "info_density": min(1.0, (len(evidence) / duration * 10.0) if duration else 0.0),
        "emotion_novelty": 1.0 if any(marker in thesis for marker in contrarian_markers) else 0.4,
        "platform_length_fit": 1.0 if policy["target_duration_s"][0] <= duration <= policy["target_duration_s"][1] else 0.5,
        "cta_fit": 1.0 if truth_map.get("cta") else 0.5,
        "reorder_penalty": 0.0,
    }


def _formula_eligible(formula: dict[str, Any], features: dict[str, float]) -> bool:
    for feature, minimum in (formula.get("eligibility") or {}).items():
        if features.get(feature, 0.0) < float(minimum):
            return False
    return True


def route_formulas(project_dir: Path) -> dict[str, Any]:
    evidence_map = verify_evidence_map(project_dir)
    analysis = load_frozen_analysis(project_dir, evidence_map)
    policy = load_policy()
    policy_hash = contract_registry.canonical_hash(policy)
    evidence_by_id = {item["id"]: item for item in evidence_map["items"]}
    weights = policy["weights"]
    candidates_out: list[dict[str, Any]] = []
    scored: list[tuple[float, str, str]] = []
    for candidate in analysis.get("idea_candidates", []):
        features = _candidate_features(candidate, analysis, evidence_by_id, policy)
        integrity = "pass"
        if not candidate.get("evidence_ids"):
            integrity = "fail"
        else:
            for eid in candidate["evidence_ids"]:
                item = evidence_by_id.get(eid)
                if item is None or item.get("review_status") != "approved":
                    integrity = "fail"
                    break
        best_formula = None
        best_score = -1.0
        for formula in policy["formulas"]:
            if not _formula_eligible(formula, features):
                continue
            score = sum(
                weights[dimension] * features.get(dimension, 0.0) for dimension in weights
            )
            if score > best_score:
                best_score = score
                best_formula = formula["id"]
        warnings: list[str] = []
        if best_formula is None:
            best_formula = policy["fallback_formula"]
            warnings.append("no formula eligible; clean_complete_idea fallback")
            best_score = 0.0
        ranges = sorted(
            candidate.get("source_ranges", []), key=lambda r: float(r["start"])
        )
        segment_order = [
            "segment-" + contract_registry.canonical_hash(
                {"candidate": candidate["id"], "index": index}
            )[:12]
            for index in range(len(ranges))
        ]
        candidates_out.append(
            {
                "idea_id": candidate["id"],
                "formula": best_formula,
                "scores": {k: round(v, 4) for k, v in features.items()},
                "integrity_gate": integrity,
                "segment_order": segment_order,
                "warnings": warnings,
            }
        )
        if integrity == "pass":
            scored.append((round(best_score, 6), candidate["id"], best_formula))
    if not scored:
        raise NarrativeError(
            "no candidate passed the factual-integrity gate; review the content analysis"
        )
    scored.sort(key=lambda item: (-item[0], item[1]))
    _best_score, selected_id, selected_formula = scored[0]
    plan = {
        "schema_version": 1,
        "content_analysis_revision": analysis["revision"],
        "evidence_map_revision": evidence_map["revision"],
        "policy_version": policy["policy_version"],
        "policy_hash": policy_hash,
        "candidates": candidates_out,
        "selected": {"idea_id": selected_id, "formula": selected_formula},
    }
    plan["plan_hash"] = contract_registry.canonical_hash(plan)
    errors = contract_registry.validate_artifact("viral_structure_plan", plan)
    if errors:
        raise NarrativeError("structure plan failed contract validation: " + "; ".join(errors))
    _write_json(project_dir / PLAN_REL, plan)
    return plan


# ---------------------------------------------------------------------------
# Narrative planner (low-risk: source order preserved)
# ---------------------------------------------------------------------------

PURPOSES = ("hook", "context", "method", "proof", "payoff", "cta")


def build_narrative_plan(project_dir: Path) -> dict[str, Any]:
    structure = _read_json(project_dir / PLAN_REL)
    manifest = _read_json(project_dir / "project.json")
    transcript = _read_json(project_dir / TRANSCRIPT_REL)
    evidence_map = verify_evidence_map(project_dir)
    analysis = load_frozen_analysis(project_dir, evidence_map)
    selected_id = structure["selected"]["idea_id"]
    candidate = next(
        (c for c in analysis.get("idea_candidates", []) if c["id"] == selected_id),
        None,
    )
    if candidate is None:
        raise NarrativeError(f"selected candidate missing from analysis: {selected_id}")
    ranges = sorted(
        candidate.get("source_ranges", []), key=lambda r: float(r["start"])
    )
    if not ranges:
        raise NarrativeError("selected candidate has no source ranges")
    merged: list[list[float]] = []
    for source_range in ranges:
        start, end = float(source_range["start"]), float(source_range["end"])
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    segments = []
    for index, (start, end) in enumerate(merged):
        purpose = PURPOSES[min(index, len(PURPOSES) - 1)] if len(merged) > 1 else "hook"
        segments.append(
            {
                "id": "segment-" + contract_registry.canonical_hash(
                    {"candidate": selected_id, "start": start, "end": end}
                )[:12],
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "purpose": purpose,
            }
        )
    warnings = []
    if structure["selected"]["formula"] in {"result_first", "contrarian"}:
        warnings.append(
            "formula suggests a cold-open reorder; not applied automatically "
            "(order changes are high-risk and need explicit confirmation)"
        )
    plan = {
        "schema_version": 1,
        "source_sha256": str(manifest.get("source", {}).get("sha256") or ""),
        "segments": segments,
        "reorder": False,
        "risk": "low",
        "reanchor": {
            "status": "stale",
            "transcript_revision": contract_registry.canonical_hash(transcript),
        },
        "warnings": warnings,
    }
    plan["plan_hash"] = contract_registry.canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )
    errors = contract_registry.validate_artifact("narrative_edit_plan", plan)
    if errors:
        raise NarrativeError("narrative plan failed contract validation: " + "; ".join(errors))
    _write_json(project_dir / NARRATIVE_REL, plan)
    return plan


# ---------------------------------------------------------------------------
# Evidence re-anchoring against a rough-cut transcript
# ---------------------------------------------------------------------------

def reanchor(
    project_dir: Path,
    rough_cut_transcript: dict[str, Any],
) -> dict[str, Any]:
    """Verify referenced evidence literals still exist in the re-transcription.

    ``anchored``: every evidence literal referenced by the selected candidate
    is present (normalised containment) in the rough-cut transcript.
    ``stale``: some literals were not found — evidence stays bound to the
    original timeline and the UI must show it as unverified.
    ``failed``: the rough-cut transcript is empty/unusable.
    """
    plan = _read_json(project_dir / NARRATIVE_REL)
    structure = _read_json(project_dir / PLAN_REL)
    evidence_map = verify_evidence_map(project_dir)
    analysis = load_frozen_analysis(project_dir, evidence_map)
    words = rough_cut_transcript.get("words") or []
    tokens = [
        normalized_literal(str(word.get("text", "")))
        for word in words
        if normalized_literal(str(word.get("text", "")))
    ]
    if not tokens:
        status = "failed"
    else:
        joined = "".join(tokens)
        # Token-boundary map: a literal only anchors when its normalized text
        # starts AND ends on token boundaries — substring hits that stitch
        # across unrelated tokens do not count (Codex review, containment
        # false positives).
        boundaries = set()
        cursor = 0
        for token in tokens:
            boundaries.add(cursor)
            cursor += len(token)
        boundaries.add(cursor)

        def literal_anchored(literal: str) -> bool:
            needle = normalized_literal(literal)
            if not needle:
                return False
            start = joined.find(needle)
            while start != -1:
                if start in boundaries and (start + len(needle)) in boundaries:
                    return True
                start = joined.find(needle, start + 1)
            return False

        selected_id = structure["selected"]["idea_id"]
        candidate = next(
            (c for c in analysis.get("idea_candidates", []) if c["id"] == selected_id),
            {},
        )
        evidence_by_id = {item["id"]: item for item in evidence_map["items"]}
        missing = [
            eid
            for eid in candidate.get("evidence_ids", [])
            if not literal_anchored(evidence_by_id.get(eid, {}).get("literal", ""))
        ]
        status = "anchored" if not missing else "stale"
    plan["reanchor"] = {
        "status": status,
        "transcript_revision": contract_registry.canonical_hash(rough_cut_transcript),
    }
    plan["plan_hash"] = contract_registry.canonical_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )
    errors = contract_registry.validate_artifact("narrative_edit_plan", plan)
    if errors:
        raise NarrativeError("re-anchored plan failed validation: " + "; ".join(errors))
    _write_json(project_dir / NARRATIVE_REL, plan)
    return plan

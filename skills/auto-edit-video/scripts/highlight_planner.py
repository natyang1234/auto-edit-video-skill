#!/usr/bin/env python3
"""Deterministic, transcript-grounded highlight proposal planner."""

from __future__ import annotations

import hashlib
import json
import math
import re
import text_joining
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1

DIRECTOR_PROFILES: dict[str, dict[str, Any]] = {
    "teacher-punch": {
        "label": "專業教學",
        "duration_quantile": 0.65,
        "max_pause_s": 1.3,
        "target_chars_per_s": 4.0,
        "dedupe_lambda": 0.25,
        "weights": {
            "hook": 0.08,
            "payoff": 0.16,
            "teaching": 0.22,
            "conflict": 0.05,
            "evidence_context": 0.15,
            "viewpoint": 0.02,
            "informativeness": 0.10,
            "brief_relevance": 0.08,
            "completeness": 0.12,
            "tempo_fit": 0.02,
        },
    },
    "high-energy": {
        "label": "爆款短影音",
        "duration_quantile": 0.25,
        "max_pause_s": 0.8,
        "target_chars_per_s": 5.2,
        "dedupe_lambda": 0.35,
        "weights": {
            "hook": 0.22,
            "payoff": 0.18,
            "teaching": 0.05,
            "conflict": 0.16,
            "evidence_context": 0.07,
            "viewpoint": 0.05,
            "informativeness": 0.08,
            "brief_relevance": 0.07,
            "completeness": 0.06,
            "tempo_fit": 0.06,
        },
    },
    "documentary": {
        "label": "八卦時事",
        "duration_quantile": 0.80,
        "max_pause_s": 1.8,
        "target_chars_per_s": 3.8,
        "dedupe_lambda": 0.22,
        "weights": {
            "hook": 0.10,
            "payoff": 0.10,
            "teaching": 0.02,
            "conflict": 0.23,
            "evidence_context": 0.22,
            "viewpoint": 0.04,
            "informativeness": 0.09,
            "brief_relevance": 0.07,
            "completeness": 0.11,
            "tempo_fit": 0.02,
        },
    },
    "minimal": {
        "label": "POV 藏鏡人",
        "duration_quantile": 0.90,
        "max_pause_s": 2.4,
        "target_chars_per_s": 3.2,
        "dedupe_lambda": 0.18,
        "weights": {
            "hook": 0.08,
            "payoff": 0.08,
            "teaching": 0.01,
            "conflict": 0.08,
            "evidence_context": 0.07,
            "viewpoint": 0.30,
            "informativeness": 0.08,
            "brief_relevance": 0.07,
            "completeness": 0.13,
            "tempo_fit": 0.10,
        },
    },
    "editorial-clean": {
        "label": "編輯精簡",
        "duration_quantile": 0.50,
        "max_pause_s": 1.2,
        "target_chars_per_s": 4.3,
        "dedupe_lambda": 0.30,
        "weights": {
            "hook": 0.05,
            "payoff": 0.13,
            "teaching": 0.08,
            "conflict": 0.08,
            "evidence_context": 0.13,
            "viewpoint": 0.04,
            "informativeness": 0.18,
            "brief_relevance": 0.10,
            "completeness": 0.18,
            "tempo_fit": 0.03,
        },
    },
}

HOOK_RE = re.compile(r"你以為|你知道|為什麼|怎麼|竟然|真相|關鍵|最大的|先講結論|[？?]")
PAYOFF_RE = re.compile(r"所以|因此|結果|答案|關鍵|最後|才發現|增加|降低|成功|失敗")
TEACHING_RE = re.compile(r"第一|第二|第三|步驟|方法|做法|先|接著|然後|定義|原因|案例")
CONFLICT_RE = re.compile(r"但是|可是|卻|反而|不是.+而是|爭議|反駁|衝突|離開|不同")
EVIDENCE_RE = re.compile(r"\d|一倍|兩倍|三倍|四倍|百分之|％|%|根據|數據|測試|指出|表示")
VIEWPOINT_RE = re.compile(r"我|我們|現場|眼前|走進|看到|聽到|按下|親手|當時|第一眼")
CONNECTOR_START_RE = re.compile(r"^(但是|可是|所以|因為|然後|而且|接著|以及|或者)")
SENSITIVE_RE = re.compile(r"爆料|指控|犯罪|詐騙|政治|政府|選舉|死亡|受傷|內幕")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).strip()


def join_transcript_parts(parts: list[str]) -> str:
    """Transcript text for these parts, by the shared spacing rule."""
    return text_joining.join_tokens(parts)


# How much room a title may take, measured in Latin letters. A card drawn
# from thirty-six Chinese characters is seventy-two wide, and the compositor
# shrank it to one unreadable line across the whole frame rather than refuse
# it. Forty-eight is what the titles that read well actually measure.
MAX_TITLE_WIDTH = 48.0


def transcript_title_excerpt(text: str, max_width: float = MAX_TITLE_WIDTH) -> str:
    """The opening of a line, cut to something a card can hold.

    Cut by how much room it takes, not by how many characters it has: the
    two differ by a factor of two between scripts, which is how a Chinese
    title half again too long passed a limit written for Latin text.
    """
    if text_joining.display_width(text) <= max_width:
        return text
    excerpt = text_joining.trim_to_width(text, max_width)
    remainder = text[len(excerpt):]
    # Only back up to a boundary when the cut landed inside a Latin word;
    # Chinese has no spaces, and a recogniser that emits no punctuation
    # offers nothing to back up to.
    if (
        excerpt
        and remainder
        and excerpt[-1].isascii()
        and excerpt[-1].isalnum()
        and remainder[0].isascii()
        and remainder[0].isalnum()
    ):
        boundaries = [excerpt.rfind(character) for character in " ，。！？、,.!?：:；;—-"]
        boundary = max(boundaries)
        if text_joining.display_width(excerpt[:boundary]) >= max_width / 2:
            excerpt = excerpt[:boundary]
    return excerpt.rstrip()


def char_ngrams(value: str, width: int = 3) -> set[str]:
    text = clean_text(value).lower()
    if not text:
        return set()
    if len(text) <= width:
        return {text}
    return {text[index : index + width] for index in range(len(text) - width + 1)}


def text_similarity(left: str, right: str) -> float:
    a = char_ngrams(left)
    b = char_ngrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def time_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(0.0, min(float(left["end"]), float(right["end"])) - max(float(left["start"]), float(right["start"])))
    union = max(float(left["end"]), float(right["end"])) - min(float(left["start"]), float(right["start"]))
    return intersection / union if union > 0 else 0.0


def _word_clause(segment: dict[str, Any]) -> list[dict[str, Any]]:
    raw_words = [word for word in segment.get("words", []) if clean_text(word.get("text", ""))]
    if not raw_words:
        text = str(segment.get("text", "")).strip()
        if not text:
            return []
        return [
            {
                "start": float(segment.get("start", 0.0)),
                "end": float(segment.get("end", 0.0)),
                "text": text,
                "segment_ids": [str(segment.get("id", ""))],
                "word_ids": [],
                "confidence": [],
                "word_timed": False,
            }
        ]

    clauses: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for word in raw_words:
        current.append(word)
        if re.search(r"[。！？!?；;]$", str(word.get("text", "")).strip()):
            clauses.append(_clause_from_words(current, segment))
            current = []
    if current:
        clauses.append(_clause_from_words(current, segment))
    return clauses


def _clause_from_words(words: list[dict[str, Any]], segment: dict[str, Any]) -> dict[str, Any]:
    confidences = [
        float(word["confidence"])
        for word in words
        if isinstance(word.get("confidence"), (int, float))
        and math.isfinite(float(word["confidence"]))
    ]
    return {
        "start": float(words[0].get("start", segment.get("start", 0.0))),
        "end": float(words[-1].get("end", segment.get("end", 0.0))),
        "text": join_transcript_parts(
            [str(word.get("text", "")) for word in words]
        ),
        "segment_ids": [str(segment.get("id", ""))],
        "word_ids": [str(word.get("id", "")) for word in words if word.get("id")],
        "confidence": confidences,
        "word_timed": True,
    }


def transcript_clauses(transcript: dict[str, Any], duration_s: float) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for segment in transcript.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for clause in _word_clause(segment):
            try:
                start = float(clause["start"])
                end = float(clause["end"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end):
                continue
            if start < 0 or end <= start or start >= duration_s:
                continue
            clause["start"] = round(start, 3)
            clause["end"] = round(min(duration_s, end), 3)
            clause["text"] = str(clause["text"]).strip()
            if clause["text"]:
                clauses.append(clause)
    clauses.sort(key=lambda item: (float(item["start"]), float(item["end"]), item["text"]))
    return clauses


def duration_configuration(
    manifest: dict[str, Any],
    duration_s: float,
    profile: dict[str, Any],
) -> tuple[float, float, float, float, list[str]]:
    warnings: list[str] = []
    target = manifest.get("output_target") if isinstance(manifest.get("output_target"), dict) else {}
    values = [target.get("min_seconds"), target.get("target_seconds"), target.get("max_seconds")]
    if all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
        minimum, desired, maximum = (float(value) for value in values)
    else:
        maximum = min(duration_s, 60.0)
        minimum = min(maximum, 10.0 if duration_s >= 10.0 else max(1.0, duration_s * 0.45))
        desired = min(maximum, max(minimum, 30.0 if duration_s >= 30.0 else duration_s))
        warnings.append("output target had no numeric duration bounds; used local editorial defaults")
    maximum = min(maximum, duration_s)
    minimum = min(minimum, maximum)
    desired = min(maximum, max(minimum, desired))
    quantile_target = minimum + float(profile["duration_quantile"]) * (maximum - minimum)
    preferred = min(maximum, max(minimum, 0.6 * desired + 0.4 * quantile_target))
    return minimum, desired, maximum, preferred, warnings


def bounded_signal(regex: re.Pattern[str], text: str, scale: float = 2.0) -> float:
    matches = len(regex.findall(text))
    return min(1.0, matches / scale)


def brief_relevance(text: str, brief: str) -> float:
    if not clean_text(brief):
        return 0.5
    brief_terms = char_ngrams(brief, 2)
    text_terms = char_ngrams(text, 2)
    return len(brief_terms & text_terms) / len(brief_terms) if brief_terms else 0.5


def candidate_signals(
    text: str,
    duration_s: float,
    preferred_duration_s: float,
    brief: str,
    profile: dict[str, Any],
) -> dict[str, float]:
    clean = clean_text(text)
    unique_ratio = len(set(clean)) / max(1, len(clean))
    length_fit = min(1.0, len(clean) / 50.0)
    completeness = 1.0
    if CONNECTOR_START_RE.search(clean):
        completeness -= 0.35
    if clean and clean[-1] not in "。！？!?；;":
        completeness -= 0.2
    duration_fit = max(0.0, 1.0 - abs(duration_s - preferred_duration_s) / max(preferred_duration_s, 1.0))
    chars_per_s = len(clean) / max(duration_s, 0.1)
    speech_fit = max(
        0.0,
        1.0 - abs(chars_per_s - float(profile["target_chars_per_s"])) / max(float(profile["target_chars_per_s"]), 1.0),
    )
    return {
        "hook": bounded_signal(HOOK_RE, clean),
        "payoff": bounded_signal(PAYOFF_RE, clean),
        "teaching": bounded_signal(TEACHING_RE, clean, 3.0),
        "conflict": bounded_signal(CONFLICT_RE, clean),
        "evidence_context": bounded_signal(EVIDENCE_RE, clean),
        "viewpoint": bounded_signal(VIEWPOINT_RE, clean, 3.0),
        "informativeness": min(1.0, 0.55 * unique_ratio + 0.45 * length_fit),
        "brief_relevance": brief_relevance(clean, brief),
        "completeness": max(0.0, completeness),
        "tempo_fit": 0.6 * duration_fit + 0.4 * speech_fit,
    }


def frame_bounds(start: float, end: float, fps: float, duration_s: float) -> tuple[float, float]:
    if not math.isfinite(fps) or fps <= 0:
        return round(start, 3), round(min(duration_s, end), 3)
    aligned_start = math.floor(start * fps + 1e-9) / fps
    aligned_end = math.ceil(end * fps - 1e-9) / fps
    return round(max(0.0, aligned_start), 3), round(min(duration_s, aligned_end), 3)


def candidate_windows(
    clauses: list[dict[str, Any]],
    *,
    minimum: float,
    maximum: float,
    preferred: float,
    duration_s: float,
    fps: float,
    brief: str,
    profile: dict[str, Any],
    stable_seed: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for start_index, first in enumerate(clauses):
        group: list[dict[str, Any]] = []
        for end_index in range(start_index, len(clauses)):
            clause = clauses[end_index]
            if group:
                pause = float(clause["start"]) - float(group[-1]["end"])
                if pause > float(profile["max_pause_s"]):
                    break
            group.append(clause)
            raw_start = float(group[0]["start"])
            raw_end = float(group[-1]["end"])
            clip_duration = raw_end - raw_start
            if clip_duration > maximum + 0.05:
                break
            if clip_duration + 0.05 < minimum:
                continue
            evidence = join_transcript_parts([str(item["text"]) for item in group])
            if len(clean_text(evidence)) < 8:
                continue
            start, end = frame_bounds(raw_start, raw_end, fps, duration_s)
            if end <= start:
                continue
            signals = candidate_signals(evidence, end - start, preferred, brief, profile)
            score = sum(float(profile["weights"][key]) * value for key, value in signals.items())
            segment_ids = list(dict.fromkeys(identifier for item in group for identifier in item["segment_ids"] if identifier))
            word_ids = list(dict.fromkeys(identifier for item in group for identifier in item["word_ids"] if identifier))
            known_confidence = [value for item in group for value in item["confidence"]]
            risk_flags: list[str] = []
            if EVIDENCE_RE.search(evidence):
                risk_flags.append("numeric_or_evidence_claim_requires_fact_check")
            if SENSITIVE_RE.search(evidence):
                risk_flags.append("sensitive_claim_requires_fact_check")
            if known_confidence and sum(known_confidence) / len(known_confidence) < 0.55:
                risk_flags.append("low_asr_confidence")
            title = transcript_title_excerpt(evidence)
            item_id = "highlight-" + canonical_hash(
                {
                    **stable_seed,
                    "start": start,
                    "end": end,
                    "text": evidence,
                }
            )[:12]
            candidates.append(
                {
                    "id": item_id,
                    "start": start,
                    "end": end,
                    "duration_s": round(end - start, 3),
                    "title": title,
                    "title_source": "transcript_extract",
                    "evidence": {
                        "text": evidence,
                        "segment_ids": segment_ids,
                        "word_ids": word_ids,
                        "exact_transcript_extract": True,
                    },
                    "boundary": {
                        "raw_start": round(raw_start, 3),
                        "raw_end": round(raw_end, 3),
                        "frame_aligned": bool(fps > 0),
                        "starts_on_unit": True,
                        "ends_on_unit": True,
                    },
                    "score": round(score, 6),
                    "score_components": {key: round(value, 6) for key, value in signals.items()},
                    "selection_tags": [key.replace("_", "-") for key, value in signals.items() if value >= 0.65],
                    "risk_flags": risk_flags,
                    "review_status": "pending",
                    "review": {"confirmed_by": None, "confirmed_at": None, "user_title": None},
                    "export_status": "not_rendered",
                }
            )
            if len(candidates) >= 5000:
                return candidates
    return candidates


def select_mmr(
    candidates: list[dict[str, Any]],
    count: int,
    dedupe_lambda: float,
) -> list[dict[str, Any]]:
    remaining = sorted(candidates, key=lambda item: (-float(item["score"]), float(item["start"]), float(item["end"]), item["id"]))[:500]
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < count:
        scored: list[tuple[float, float, float, float, str, dict[str, Any]]] = []
        for candidate in remaining:
            max_text = max((text_similarity(candidate["evidence"]["text"], item["evidence"]["text"]) for item in selected), default=0.0)
            max_time = max((time_iou(candidate, item) for item in selected), default=0.0)
            if max_time > 0.60:
                continue
            adjusted = float(candidate["score"]) - dedupe_lambda * max_text - (dedupe_lambda + 0.25) * max_time
            scored.append((adjusted, float(candidate["score"]), -float(candidate["start"]), -float(candidate["end"]), candidate["id"], candidate))
        if not scored:
            break
        chosen = max(scored)[-1]
        selected.append(chosen)
        remaining = [item for item in remaining if item["id"] != chosen["id"]]
    selected.sort(key=lambda item: (-float(item["score"]), float(item["start"]), float(item["end"]), item["id"]))
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return selected


def build_highlight_plan(
    transcript: dict[str, Any],
    manifest: dict[str, Any],
    *,
    director_profile: str,
    requested_count: int,
    editing_brief: str,
) -> dict[str, Any]:
    if director_profile not in DIRECTOR_PROFILES:
        raise ValueError(f"unsupported director profile: {director_profile}")
    if isinstance(requested_count, bool) or not isinstance(requested_count, int) or not 1 <= requested_count <= 10:
        raise ValueError("requested_count must be an integer between 1 and 10")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    try:
        duration_s = float(source.get("duration_s", transcript.get("duration_s", 0.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("source duration is invalid") from exc
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("source duration must be positive and finite")
    try:
        fps = float(source.get("fps") or 0.0)
    except (TypeError, ValueError):
        fps = 0.0
    profile = DIRECTOR_PROFILES[director_profile]
    transcript_hash = canonical_hash(
        {
            "schema_version": transcript.get("schema_version"),
            "language": transcript.get("language"),
            "segments": transcript.get("segments", []),
            "words": transcript.get("words", []),
        }
    )
    brief = str(editing_brief or "").strip()[:2000]
    brief_hash = canonical_hash(brief)
    minimum, desired, maximum, preferred, warnings = duration_configuration(manifest, duration_s, profile)
    configuration = {
        "director_profile": director_profile,
        "director_label": profile["label"],
        "requested_count": requested_count,
        "platform": (manifest.get("output_target") or {}).get("platform", "auto"),
        "duration_profile": (manifest.get("output_target") or {}).get("duration_profile", "auto"),
        "duration_bounds_s": {
            "min": round(minimum, 3),
            "target": round(desired, 3),
            "max": round(maximum, 3),
        },
        "preferred_duration_s": round(preferred, 3),
        "editing_brief": brief,
        "brief_sha256": brief_hash,
        "planner_version": 1,
    }
    source_contract = {
        "artifact": "working/transcript_words.json",
        "sha256": transcript_hash,
        "source_sha256": str(source.get("sha256") or ""),
        "timebase": "source_media",
        "media": str(source.get("staged_path") or ""),
        "duration_s": round(duration_s, 3),
    }
    clauses = transcript_clauses(transcript, duration_s)
    if not clauses:
        warnings.append("transcript has no timed spoken segments; semantic highlight planning did not run")
        plan = {
            "schema_version": SCHEMA_VERSION,
            "generator": "deterministic-local-highlight-planner-v1",
            "status": "needs_transcript",
            "source": source_contract,
            "configuration": configuration,
            "generated_at": now_utc(),
            "warnings": warnings,
            "items": [],
        }
        plan["plan_revision"] = canonical_hash({key: value for key, value in plan.items() if key not in {"generated_at", "plan_revision"}})
        return plan

    stable_seed = {
        "transcript_sha256": transcript_hash,
        "source_sha256": source_contract["source_sha256"],
        "director_profile": director_profile,
        "brief_sha256": brief_hash,
    }
    candidates = candidate_windows(
        clauses,
        minimum=minimum,
        maximum=maximum,
        preferred=preferred,
        duration_s=duration_s,
        fps=fps,
        brief=brief,
        profile=profile,
        stable_seed=stable_seed,
    )
    if not candidates and clauses:
        first = clauses[0]
        last = clauses[-1]
        if float(last["end"]) - float(first["start"]) <= maximum + 0.05:
            candidates = candidate_windows(
                clauses,
                minimum=max(0.1, float(last["end"]) - float(first["start"]) - 0.05),
                maximum=maximum,
                preferred=min(preferred, float(last["end"]) - float(first["start"])),
                duration_s=duration_s,
                fps=fps,
                brief=brief,
                profile=profile,
                stable_seed=stable_seed,
            )
            warnings.append("source was shorter than the requested minimum; proposed actual spoken length without padding")
    selected = select_mmr(candidates, requested_count, float(profile["dedupe_lambda"]))
    if len(selected) < requested_count:
        warnings.append(f"only {len(selected)} distinct transcript-grounded highlights were available; no filler clips were added")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "generator": "deterministic-local-highlight-planner-v1",
        "status": "needs_review" if selected else "needs_transcript",
        "source": source_contract,
        "configuration": configuration,
        "generated_at": now_utc(),
        "warnings": warnings,
        "items": selected,
    }
    plan["plan_revision"] = canonical_hash({key: value for key, value in plan.items() if key not in {"generated_at", "plan_revision"}})
    return plan


def validate_highlight_plan(plan: Any, duration_s: float) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION:
        return ["highlight plan schema_version must be 1"]
    if plan.get("status") not in {"needs_transcript", "needs_review", "reviewed"}:
        errors.append("highlight plan status is invalid")
    revision = str(plan.get("plan_revision", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", revision):
        errors.append("highlight plan revision must be a SHA-256 digest")
    items = plan.get("items")
    if not isinstance(items, list):
        return errors + ["highlight plan items must be an array"]
    if len(items) > 10:
        errors.append("highlight plan cannot exceed 10 items")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"highlight {index} must be an object")
            continue
        item_id = str(item.get("id", ""))
        if not re.fullmatch(r"highlight-[0-9a-f]{12}", item_id) or item_id in seen:
            errors.append(f"highlight {index} has an invalid or duplicate id")
        seen.add(item_id)
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            errors.append(f"highlight {item_id or index} timing must be numeric")
            continue
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration_s + 0.05:
            errors.append(f"highlight {item_id or index} timing is out of bounds")
        if item.get("review_status") not in {"pending", "approved", "rejected"}:
            errors.append(f"highlight {item_id or index} review status is invalid")
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        evidence_text = str(evidence.get("text", ""))
        title = str(item.get("title", ""))
        if not title or title not in evidence_text or not evidence.get("exact_transcript_extract"):
            errors.append(f"highlight {item_id or index} title must be an exact transcript extract")
        score = item.get("score")
        if not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            errors.append(f"highlight {item_id or index} score must be finite and between 0 and 1")
    return errors

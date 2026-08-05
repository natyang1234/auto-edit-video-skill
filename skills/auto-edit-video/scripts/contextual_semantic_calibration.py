#!/usr/bin/env python3
"""Whole-transcript, context-aware semantic calibration helpers.

The model is only a proposal engine.  This module owns deterministic coverage,
minimal-patch validation, timing projection, and the auditable accepted / pending
/ rejected split used by the Auto Edit Video pipeline.
"""

from __future__ import annotations

import json
import math
import os
import re
import text_joining
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ALLOWED_CATEGORIES = {
    "homophone",
    "idiom",
    "domain_term",
    "grammar_term",
    "name",
    "transliteration",
    "typo",
    "word_choice",
}
PENDING_DECISIONS = {"uncertain", "review"}
HAN_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _finite_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0 <= number <= 1:
        return None
    return round(number, 4)


def _punctuation_signature(value: str) -> list[str]:
    return [
        character
        for character in value
        if not character.isspace()
        and not character.isalnum()
        and not HAN_RE.fullmatch(character)
    ]


def _latin_only_phrase(value: str) -> bool:
    return bool(LATIN_RE.search(value)) and HAN_RE.search(value) is None


def _is_script_conversion(source: str, replacement: str) -> bool:
    """Whether a proposal only rewrites the source in the other script.

    Models trained on mainland text routinely report Traditional spellings as
    typos and offer the Simplified form with high confidence. The project has
    already declared which script it is written in, so a change that survives
    converting back is an orthography edit wearing a correction's clothes.
    """
    if source == replacement:
        return False
    try:
        from traditional_chinese import to_taiwan_traditional
    except ImportError:
        return False
    try:
        return to_taiwan_traditional(replacement) == source
    except Exception:
        return False


def _patch_has_unchanged_edges(source: str, replacement: str) -> bool:
    """Detect a model wrapping a small change in unchanged sentence context."""

    if _latin_only_phrase(source) and _latin_only_phrase(replacement):
        return False
    prefix = 0
    while (
        prefix < len(source)
        and prefix < len(replacement)
        and source[prefix] == replacement[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(source) - prefix
        and suffix < len(replacement) - prefix
        and source[-(suffix + 1)] == replacement[-(suffix + 1)]
    ):
        suffix += 1
    return prefix > 0 or suffix > 0


def _creates_cross_caption_duplicate(
    unit: dict[str, Any],
    source: str,
    replacement: str,
) -> bool:
    """Reject a patch that invents a repeated character at a caption split."""

    text = str(unit.get("text", ""))
    source_start = text.find(source)
    if source_start < 0 or not source or not replacement:
        return False
    source_end = source_start + len(source)
    previous = unit.get("previous", [])
    following = unit.get("next", [])
    if source_start == 0 and previous:
        previous_text = str(previous[-1].get("text", "")).rstrip()
        if (
            previous_text
            and replacement[0] == previous_text[-1]
            and source[0] != replacement[0]
        ):
            return True
    if source_end == len(text) and following:
        next_text = str(following[0].get("text", "")).lstrip()
        if (
            next_text
            and replacement[-1] == next_text[0]
            and source[-1] != replacement[-1]
        ):
            return True
    return False


def _public_unit(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id", "")),
        "start": round(float(raw.get("start", 0.0)), 3),
        "end": round(float(raw.get("end", 0.0)), 3),
        "text": str(raw.get("text", "")),
        "word_ids": [str(item) for item in raw.get("word_ids", [])],
    }


def build_context_units(
    transcript: dict[str, Any],
    *,
    context_radius: int = 2,
) -> list[dict[str, Any]]:
    """Return every readable caption with bounded previous/next context."""

    if not isinstance(context_radius, int) or not 1 <= context_radius <= 4:
        raise ValueError("context_radius must be an integer from 1 to 4")
    captions = [
        _public_unit(item)
        for item in transcript.get("caption_segments", [])
        if isinstance(item, dict)
        and str(item.get("id", ""))
        and str(item.get("text", "")).strip()
    ]
    result: list[dict[str, Any]] = []
    for index, target in enumerate(captions):
        previous = captions[max(0, index - context_radius) : index]
        following = captions[index + 1 : index + context_radius + 1]
        result.append(
            {
                "id": target["id"],
                "start": target["start"],
                "end": target["end"],
                "text": target["text"],
                "word_ids": list(target["word_ids"]),
                "previous": [dict(item) for item in previous],
                "next": [dict(item) for item in following],
            }
        )
    return result


def _document_context(units: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"unit_id": str(item["id"]), "text": str(item["text"])}
        for item in units
    ]


def _proposal_prompt(
    batch: list[dict[str, Any]],
    glossary: list[str],
    document: list[dict[str, str]],
) -> str:
    targets = [
        {
            "unit_id": item["id"],
            "previous": [context["text"] for context in item["previous"]],
            "target": item["text"],
            "next": [context["text"] for context in item["next"]],
        }
        for item in batch
    ]
    return (
        "你是台灣繁體中文 ASR 字幕正字員。請逐一檢查每個 target，並同時利用整份 "
        "document_transcript、previous 與 next 判斷"
        "同音字、成語、領域術語、文法術語、人名或音譯錯誤。只修正高度確定的語音辨識錯字。\n"
        "硬性規則：\n"
        "0. 只檢查 units 陣列中的 target；unit_id 必須逐字複製該 target 的 unit_id，"
        "不可引用 document_transcript 裡其他段落的 id。重複出現的正確專業術語可作為正字證據。\n"
        "1. source 必須逐字複製自該 target，而且只能包含最短錯誤片段。\n"
        "2. replacement 只放對應正字；禁止整句改寫、順句、刪口語、改標點。\n"
        "3. 禁止改動數字；英文只有在 glossary 明列正確拼法時才能更正。\n"
        "4. 沒有明顯錯字的 target 不要產生 item。\n"
        "5. category 只能是 homophone、idiom、domain_term、grammar_term、name、"
        "transliteration、typo、word_choice。\n"
        "輸出純 JSON：{\"items\":[{\"unit_id\":\"...\",\"source\":\"最短原字串\","
        "\"replacement\":\"最短正字\",\"category\":\"idiom\",\"reason\":\"上下文依據\","
        "\"confidence\":0.0}]}。\n"
        "例如 target 是『不要頭重小琴』時，只能提案實際錯掉的最小部分 source『小琴』、"
        "replacement『腳輕』，不可把相同的『頭重』包進 patch。\n"
        f"glossary={json.dumps(glossary[:80], ensure_ascii=False)}\n"
        f"document_transcript={json.dumps(document, ensure_ascii=False)}\n"
        f"units={json.dumps(targets, ensure_ascii=False)}"
    )


def _verification_prompt(
    batch: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    glossary: list[str],
    document: list[dict[str, str]],
) -> str:
    contexts = {
        item["id"]: {
            "previous": [context["text"] for context in item["previous"]],
            "target": item["text"],
            "next": [context["text"] for context in item["next"]],
        }
        for item in batch
    }
    return (
        "你是第二位台灣繁體字幕審核員。逐筆核對 proposed patches 是否真的是由前後文支持的 ASR 錯字。"
        "只接受最短局部正字；任何風格潤色、同義改寫、沒有充分證據的補字、數字變更或英文猜測都必須"
        " reject；兩種說法都可能時用 uncertain。\n"
        "輸出純 JSON：{\"items\":[{\"unit_id\":\"...\",\"source\":\"...\","
        "\"replacement\":\"...\",\"decision\":\"accept|uncertain|reject\","
        "\"confidence\":0.0,\"reason\":\"核對理由\"}]}。\n"
        f"glossary={json.dumps(glossary[:80], ensure_ascii=False)}\n"
        f"document_transcript={json.dumps(document, ensure_ascii=False)}\n"
        f"contexts={json.dumps(contexts, ensure_ascii=False)}\n"
        f"proposals={json.dumps(proposals, ensure_ascii=False)}"
    )


def propose_contextual_corrections(
    transcript: dict[str, Any],
    *,
    glossary: list[str],
    model_call: Any,
    batch_size: int = 10,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Review every unit with context, then run a separate verification pass."""

    if not isinstance(batch_size, int) or not 1 <= batch_size <= 20:
        raise ValueError("batch_size must be an integer from 1 to 20")
    units = build_context_units(transcript)
    document = _document_context(units)
    reviewed_unit_ids: list[str] = []
    output_items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def report_progress() -> None:
        if not callable(progress_callback):
            return
        progress_callback(
            {
                "reviewed_unit_count": len(reviewed_unit_ids),
                "total_unit_count": len(units),
                "candidate_count": len(output_items),
                "model_error_count": len(errors),
            }
        )

    for offset in range(0, len(units), batch_size):
        batch = units[offset : offset + batch_size]
        try:
            proposal_payload = model_call(
                _proposal_prompt(batch, glossary, document),
                "propose",
            )
        except Exception as exc:  # model/provider failures remain an auditable partial pass
            errors.append(
                {
                    "stage": "propose",
                    "unit_ids": [item["id"] for item in batch],
                    "error": str(exc)[:500],
                }
            )
            report_progress()
            continue
        if not isinstance(proposal_payload, dict) or not isinstance(
            proposal_payload.get("items", []), list
        ):
            errors.append(
                {
                    "stage": "propose",
                    "unit_ids": [item["id"] for item in batch],
                    "error": "model response did not contain an items array",
                }
            )
            report_progress()
            continue
        target_ids = {item["id"] for item in batch}
        proposals = [
            {
                "unit_id": str(item.get("unit_id", "")),
                "source": str(item.get("source", "")).strip(),
                "replacement": str(item.get("replacement", "")).strip(),
                "category": str(item.get("category", "")).strip().lower(),
                "reason": str(item.get("reason", "")).strip()[:500],
                "confidence": item.get("confidence"),
            }
            for item in proposal_payload.get("items", [])[:200]
            if isinstance(item, dict) and str(item.get("unit_id", "")) in target_ids
        ]
        if not proposals:
            reviewed_unit_ids.extend(item["id"] for item in batch)
            report_progress()
            continue
        verification_succeeded = False
        try:
            verification_payload = model_call(
                _verification_prompt(batch, proposals, glossary, document),
                "verify",
            )
        except Exception as exc:
            errors.append(
                {
                    "stage": "verify",
                    "unit_ids": [item["id"] for item in batch],
                    "error": str(exc)[:500],
                }
            )
            verification_payload = {"items": []}
        else:
            verification_succeeded = (
                isinstance(verification_payload, dict)
                and isinstance(verification_payload.get("items", []), list)
            )
            if not verification_succeeded:
                errors.append(
                    {
                        "stage": "verify",
                        "unit_ids": [item["id"] for item in batch],
                        "error": "model response did not contain an items array",
                    }
                )
                verification_payload = {"items": []}
        if verification_succeeded:
            reviewed_unit_ids.extend(item["id"] for item in batch)
        verified_items = (
            verification_payload.get("items", [])
            if isinstance(verification_payload, dict)
            and isinstance(verification_payload.get("items", []), list)
            else []
        )
        verification_index = {
            (
                str(item.get("unit_id", "")),
                str(item.get("source", "")).strip(),
                str(item.get("replacement", "")).strip(),
            ): item
            for item in verified_items
            if isinstance(item, dict)
        }
        for proposal in proposals:
            key = (
                proposal["unit_id"],
                proposal["source"],
                proposal["replacement"],
            )
            verification = verification_index.get(key, {})
            output_items.append(
                {
                    **proposal,
                    "verifier_decision": str(
                        verification.get("decision", "uncertain")
                    ).lower(),
                    "verifier_confidence": verification.get("confidence", 0.0),
                    "verifier_reason": str(verification.get("reason", ""))[:500],
                }
            )
        report_progress()
    return {
        "reviewed_unit_ids": reviewed_unit_ids,
        "items": output_items,
        "errors": errors,
    }


def ollama_json_model_call(
    prompt: str,
    stage: str,
    *,
    model: str,
    timeout: int = 300,
) -> dict[str, Any]:
    """Call a loopback-only Ollama model with deterministic JSON output."""

    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model):
        raise ValueError("Ollama model name is invalid")
    raw_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    if "://" not in raw_host:
        raw_host = f"http://{raw_host}"
    parsed = urllib.parse.urlsplit(raw_host)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("contextual semantic calibration only permits loopback Ollama")
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/api/chat", "", "")
    )
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only the requested JSON. Preserve Taiwan Traditional Chinese, "
                        "source wording, numbers, punctuation, and English unless explicitly corrected."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": 16384,
                "num_predict": 1536,
            },
            "keep_alive": "10m",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama {stage} request failed: {exc}") from exc
    content = response_payload.get("message", {}).get("content", "")
    if not isinstance(content, str):
        raise RuntimeError(f"Ollama {stage} response did not contain text")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama {stage} response was not valid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Ollama {stage} response must be a JSON object")
    return result


def _ascii_change_allowed(source: str, replacement: str, glossary: list[str]) -> bool:
    source_tokens = [item.casefold() for item in LATIN_RE.findall(source)]
    replacement_tokens = [item.casefold() for item in LATIN_RE.findall(replacement)]
    if source_tokens == replacement_tokens:
        return True
    glossary_terms = {re.sub(r"\s+", " ", item).strip().casefold() for item in glossary}
    glossary_words = {
        token.casefold()
        for item in glossary
        for token in LATIN_RE.findall(str(item))
    }
    normalized_replacement = re.sub(r"\s+", " ", replacement).strip().casefold()
    return normalized_replacement in glossary_terms or (
        bool(replacement_tokens)
        and all(token in glossary_words for token in replacement_tokens)
    )


def _timed_source_span(
    unit: dict[str, Any],
    source: str,
    words_by_id: dict[str, dict[str, Any]],
) -> tuple[float, float, list[str]] | None:
    words = [
        words_by_id[word_id]
        for word_id in unit.get("word_ids", [])
        if word_id in words_by_id
    ]
    if not words:
        return None
    # This searches for text the caption and highlight paths produced, so it
    # has to space tokens exactly as they did — hence the shared rule rather
    # than a third copy of the punctuation sets.
    chunks: list[str] = []
    combined = ""
    for word in words:
        token = str(word.get("text", "")).strip()
        if not token:
            chunks.append("")
            continue
        prefix = (
            " " if combined and text_joining.needs_space(combined[-1], token[0]) else ""
        )
        chunk = prefix + token
        chunks.append(chunk)
        combined += chunk
    if combined.count(source) != 1:
        return None
    source_start = combined.index(source)
    source_end = source_start + len(source)
    cursor = 0
    matched: list[dict[str, Any]] = []
    for word, chunk in zip(words, chunks):
        chunk_start = cursor
        chunk_end = cursor + len(chunk)
        cursor = chunk_end
        if chunk_start < source_end and chunk_end > source_start:
            matched.append(word)
    if not matched:
        return None
    start = round(float(matched[0].get("start", unit.get("start", 0.0))), 3)
    end = round(float(matched[-1].get("end", unit.get("end", start))), 3)
    if end <= start:
        unit_start = float(unit.get("start", start))
        unit_end = float(unit.get("end", end))
        start = round(max(unit_start, start - 0.001), 3)
        end = round(min(unit_end, end + 0.001), 3)
        if end <= start:
            return None
    return start, end, [str(item.get("id", "")) for item in matched]


def validate_contextual_proposals(
    transcript: dict[str, Any],
    payload: dict[str, Any],
    *,
    glossary: list[str],
    minimum_confidence: float = 0.92,
) -> dict[str, Any]:
    """Validate model proposals and produce time-scoped calibration rules.

    This function never trusts a model's claim that a pass was complete.  Unit
    coverage is derived from the current transcript, and only exact, minimal,
    verified patches with safe timing are eligible for automatic application.
    """

    if not 0.5 <= float(minimum_confidence) <= 1:
        raise ValueError("minimum_confidence must be between 0.5 and 1")
    units = build_context_units(transcript)
    unit_map = {item["id"]: item for item in units}
    all_unit_ids = list(unit_map)
    reviewed_ids = []
    for raw_id in payload.get("reviewed_unit_ids", []):
        unit_id = str(raw_id)
        if unit_id in unit_map and unit_id not in reviewed_ids:
            reviewed_ids.append(unit_id)
    coverage_status = "complete" if set(reviewed_ids) == set(all_unit_ids) else "partial"

    words_by_id = {
        str(item.get("id", "")): item
        for item in transcript.get("words", [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    accepted: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_ranges: dict[str, list[tuple[int, int]]] = {}

    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []
    for index, raw in enumerate(raw_items[:2048]):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reject_reason": "proposal_not_object"})
            continue
        unit_id = str(raw.get("unit_id", ""))
        source = str(raw.get("source", "")).strip()
        replacement = str(raw.get("replacement", "")).strip()
        category = str(raw.get("category", "")).strip().lower()
        decision = str(raw.get("verifier_decision", "")).strip().lower()
        confidence = _finite_confidence(raw.get("confidence"))
        verifier_confidence = _finite_confidence(raw.get("verifier_confidence"))
        record = {
            "unit_id": unit_id,
            "source": source,
            "replacement": replacement,
            "category": category,
            "reason": str(raw.get("reason", "")).strip()[:500],
            "confidence": confidence,
            "verifier_decision": decision,
            "verifier_confidence": verifier_confidence,
        }

        reject_reason: str | None = None
        unit = unit_map.get(unit_id)
        if unit is None:
            reject_reason = "unknown_unit"
        elif unit_id not in reviewed_ids:
            reject_reason = "unit_not_reviewed"
        elif category not in ALLOWED_CATEGORIES:
            reject_reason = "unsupported_category"
        elif not source or not replacement or source == replacement:
            reject_reason = "empty_or_identity_patch"
        elif len(source) > 40 or len(replacement) > 48 or "\n" in source + replacement:
            reject_reason = "patch_too_large"
        elif (
            _punctuation_signature(source) != _punctuation_signature(replacement)
            and not (_latin_only_phrase(source) or _latin_only_phrase(replacement))
        ):
            reject_reason = "punctuation_changed"
        elif not (HAN_RE.search(source + replacement) or LATIN_RE.search(source + replacement)):
            reject_reason = "patch_has_no_words"
        elif unit["text"].count(source) != 1:
            reject_reason = "source_not_unique_in_unit"
        elif _patch_has_unchanged_edges(source, replacement):
            reject_reason = "patch_contains_unchanged_context"
        elif _creates_cross_caption_duplicate(unit, source, replacement):
            reject_reason = "cross_caption_duplicate_created"
        elif len(unit["text"]) > 20 and len(source) / max(1, len(unit["text"])) > 0.6:
            reject_reason = "patch_is_sentence_rewrite"
        elif NUMBER_RE.findall(source) != NUMBER_RE.findall(replacement):
            reject_reason = "numbers_changed"
        elif _is_script_conversion(source, replacement):
            reject_reason = "script_converted_not_corrected"
        elif not _ascii_change_allowed(source, replacement, glossary):
            reject_reason = "latin_terms_changed_without_glossary"
        elif confidence is None or verifier_confidence is None:
            reject_reason = "invalid_confidence"
        elif decision not in {"accept", "reject", *PENDING_DECISIONS}:
            reject_reason = "invalid_verifier_decision"

        if reject_reason is not None:
            rejected.append({**record, "reject_reason": reject_reason})
            continue
        if decision == "reject":
            rejected.append({**record, "reject_reason": "verifier_rejected"})
            continue
        if category == "word_choice":
            pending.append({**record, "pending_reason": "word_choice_requires_human"})
            continue
        if decision in PENDING_DECISIONS or min(confidence, verifier_confidence) < minimum_confidence:
            pending.append({**record, "pending_reason": "needs_human_confirmation"})
            continue

        timing = _timed_source_span(unit, source, words_by_id)
        if timing is None:
            rejected.append({**record, "reject_reason": "source_timing_unresolved"})
            continue
        source_start = unit["text"].index(source)
        source_end = source_start + len(source)
        prior_ranges = accepted_ranges.setdefault(unit_id, [])
        if any(start < source_end and end > source_start for start, end in prior_ranges):
            rejected.append({**record, "reject_reason": "overlapping_patch"})
            continue
        prior_ranges.append((source_start, source_end))
        start, end, source_word_ids = timing
        accepted.append(
            {
                **record,
                "start": start,
                "end": end,
                "source_word_ids": source_word_ids,
            }
        )

    rules = [
        {
            "canonical": item["replacement"],
            "aliases": [item["source"]],
            "start": item["start"],
            "end": item["end"],
            "unit_id": item["unit_id"],
            "provenance": "contextual_semantic_calibration",
        }
        for item in accepted
    ]
    return {
        "coverage_status": coverage_status,
        "reviewed_unit_ids": reviewed_ids,
        "reviewed_unit_count": len(reviewed_ids),
        "total_unit_count": len(all_unit_ids),
        "minimum_confidence": round(float(minimum_confidence), 4),
        "accepted": accepted,
        "pending": pending,
        "rejected": rejected,
        "applied_count": len(accepted),
        "pending_count": len(pending),
        "rejected_count": len(rejected),
        "rules": rules,
    }

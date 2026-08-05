#!/usr/bin/env python3
"""Choose what to put on screen for each cut, from what was actually said.

Every card is built out of evidence entries, which are verbatim transcript
extracts. A number can only reach the screen if someone said it, so there is
no path here that invents one — the decision is which of the true things to
show, and when showing nothing is better.
"""

from __future__ import annotations

import re
from typing import Any

import contract_registry

# A segment carrying nothing worth a card keeps the picture it already has.
PLAIN_BEAT = "keep_aroll"
# Cards compete with the speaker for attention, so they stay sparse: at most
# this share of segments gets one, and never two in a row.
MAX_DECORATED_SHARE = 0.5
# Numbers are only a chart when there are enough of them to compare.
CHART_MIN_DATUMS = 3
LIST_MIN_ITEMS = 3
# How long each kind of card stays on screen, regardless of how long the
# segment it belongs to runs. A hook lands fast; a chart needs reading time.
CARD_DWELL_SECONDS = {
    "title": 3.0,
    "stat": 4.0,
    "chart": 5.5,
    "dynamic_list": 5.5,
}
# Enumeration in speech, in the languages this tool is used in.
LIST_MARKERS = re.compile(
    r"(第[一二三四五六七八九十]+|首先|其次|再來|最後|然後|另外"
    r"|\bfirst\b|\bsecond\b|\bthird\b|\bnext\b|\bfinally\b|\balso\b)",
    re.IGNORECASE,
)
NUMBER_VALUE = re.compile(r"-?\d+(?:\.\d+)?")
# Speech is full of numbers that are not measurements: exit 4, the second
# floor, half past six, "whether you play with 2000 dogs or one". A figure
# earns a card when it says how much of something there is — a percentage, a
# decimal, or a unit. Size alone does not qualify it: a big round number is
# just as often hyperbole, and a stat card asserts it was a measurement.
MEASUREMENT = re.compile(
    r"(%|％|\d+\.\d|\d+\s*(?:倍|萬|億|千|百分|分鐘|小時|天|年|人|次|元|美元|kg|km|MB|GB))"
)


def is_measurement(literal: str) -> bool:
    return bool(MEASUREMENT.search(str(literal)))


def _identifier(prefix: str, *parts: Any) -> str:
    return f"{prefix}-" + contract_registry.canonical_hash(list(parts))[:12]


def _numeric_value(literal: str) -> float | None:
    match = NUMBER_VALUE.search(literal.replace(",", ""))
    return float(match.group(0)) if match else None


def _within(evidence: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return [
        item
        for item in evidence
        if float(item.get("start", 0.0)) >= start - 0.001
        and float(item.get("end", 0.0)) <= end + 0.001
    ]


def _label_for(literal: str) -> str:
    """A short label for a number, taken from the words around it."""
    cleaned = literal.strip().strip("，,。.、！!？?")
    return cleaned[:18] if cleaned else "—"


def _title_payload(
    layer_id: str, quotes: list[dict[str, Any]], editorial_title: str = ""
) -> dict[str, Any]:
    # An editorial title says what the cut is about; the first quote only says
    # how it happens to open. Same preference as the highlight design cards.
    headline = editorial_title.strip()[:40] or _label_for(quotes[0]["literal"])[:40]
    return {
        "id": layer_id,
        "type": "title",
        "payload": {
            "title": headline,
            # The opening card names what the piece is about, which is the
            # hook rather than a section marker or a pulled quote.
            "title_kind": "full-screen-hook",
        },
    }


def _stat_payload(
    layer_id: str, number: dict[str, Any], quotes: list[dict[str, Any]]
) -> dict[str, Any]:
    # The figure carries the emphasis; the sentence it came from says what it
    # counts. Without a sentence the figure has to label itself.
    context = _label_for(str(quotes[0]["literal"])) if quotes else ""
    return {
        "id": layer_id,
        "type": "stat",
        "payload": {
            "value": number["literal"].strip(),
            "label": context or number["literal"].strip(),
            "evidence_id": number["id"],
            "source_literal": number["literal"].strip(),
        },
    }


def _chart_payload(layer_id: str, numbers: list[dict[str, Any]]) -> dict[str, Any] | None:
    datums = []
    for number in numbers:
        value = _numeric_value(str(number.get("literal", "")))
        if value is None:
            continue
        datums.append(
            {
                "label": _label_for(str(number["literal"])),
                "value": value,
                "evidence_id": number["id"],
                "source_literal": str(number["literal"]).strip(),
            }
        )
    if len(datums) < CHART_MIN_DATUMS:
        return None
    return {
        "id": layer_id,
        "type": "chart",
        "payload": {"chart_kind": "bar", "datums": datums},
    }


def _list_payload(layer_id: str, quotes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": layer_id,
        "type": "dynamic_list",
        "payload": {
            "items": [
                {"text": _label_for(str(quote["literal"]))[:30], "evidence_id": quote["id"]}
                for quote in quotes
            ]
        },
    }


def _classify(found: list[dict[str, Any]], is_opening: bool) -> str:
    """What this segment is carrying, or PLAIN_BEAT when it is carrying prose."""
    numbers = [
        item
        for item in found
        if item.get("kind") == "number" and is_measurement(item.get("literal", ""))
    ]
    quotes = [item for item in found if item.get("kind") == "quote"]
    if len(numbers) >= CHART_MIN_DATUMS:
        return "chart"
    if numbers:
        return "stat"
    enumerated = [quote for quote in quotes if LIST_MARKERS.search(str(quote.get("literal", "")))]
    if len(enumerated) >= LIST_MIN_ITEMS:
        return "dynamic_list"
    if is_opening and quotes:
        return "title"
    return PLAIN_BEAT


def plan_visuals(
    segments: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    max_decorated_share: float = MAX_DECORATED_SHARE,
    editorial_title: str = "",
) -> dict[str, Any]:
    """Decide a beat for every segment, and build the cards those beats need.

    Returns the visual plan and the structured layers it refers to. A segment
    the rules cannot read confidently keeps its picture rather than getting a
    card built from a guess.
    """
    plan_items: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    # At least one card is allowed, or a timeline that is still one long
    # take could never carry anything.
    budget = max(1, int(len(segments) * max_decorated_share)) if segments else 0
    decorated = 0
    previous_decorated = False

    for index, segment in enumerate(segments):
        # Timeline segments carry source_start/source_end; callers holding a
        # plain window use start/end.
        start = float(segment.get("source_start", segment.get("start", 0.0)))
        end = float(segment.get("source_end", segment.get("end", 0.0)))
        if end <= start:
            continue
        highlight_id = str(segment.get("id") or "")
        if not re.fullmatch(r"highlight-[0-9a-f]{8,}", highlight_id):
            highlight_id = _identifier("highlight", segment.get("id"), start, end)
        item_id = _identifier("visual-beat", highlight_id, index)
        found = _within(evidence, start, end)
        beat = _classify(found, is_opening=index == 0)

        # Two cards in a row read as a slideshow, and a decorated cut costs the
        # viewer more attention than a plain one.
        if beat != PLAIN_BEAT and (previous_decorated or decorated >= budget):
            beat = PLAIN_BEAT

        layer_id = None
        evidence_ids: list[str] = []
        if beat != PLAIN_BEAT:
            layer_id = _identifier("structured-layer", item_id, beat)
            numbers = [
                item
                for item in found
                if item.get("kind") == "number" and is_measurement(item.get("literal", ""))
            ]
            quotes = [item for item in found if item.get("kind") == "quote"]
            if beat == "chart":
                layer = _chart_payload(layer_id, numbers)
                if layer is None:
                    beat, layer_id = PLAIN_BEAT, None
                else:
                    evidence_ids = [datum["evidence_id"] for datum in layer["payload"]["datums"]]
            elif beat == "stat":
                layer = _stat_payload(layer_id, numbers[0], quotes)
                evidence_ids = [numbers[0]["id"]]
            elif beat == "dynamic_list":
                enumerated = [
                    quote for quote in quotes if LIST_MARKERS.search(str(quote.get("literal", "")))
                ]
                layer = _list_payload(layer_id, enumerated)
                evidence_ids = [entry["evidence_id"] for entry in layer["payload"]["items"]]
            else:
                layer = _title_payload(layer_id, quotes, editorial_title)
                evidence_ids = []
            if layer_id is not None:
                layer["visual_plan_item_id"] = item_id
                layer["revision"] = 1
                layer["evidence_revision"] = contract_registry.canonical_hash(
                    sorted(evidence_ids)
                )
                layer["review_status"] = "pending"
                layers.append(layer)

        # A card summarises its whole segment but must not sit there for the
        # whole segment: on a single-take clip that means one box parked over
        # the speaker's face for the entire video. Evidence still comes from
        # the full window above; only the time on screen is bounded.
        display_end = end
        dwell = CARD_DWELL_SECONDS.get(beat)
        if dwell is not None:
            display_end = min(end, start + dwell)

        plan_items.append(
            {
                "id": item_id,
                "highlight_id": highlight_id,
                "start": round(start, 3),
                "end": round(display_end, 3),
                "beat": beat,
                "structured_layer_id": layer_id,
                "selected_asset": None,
                # A title states what the segment is about rather than citing
                # a figure, so it carries no evidence and says so.
                "conceptual_only": beat == "title",
                "evidence_ids": evidence_ids,
                "review_status": "pending",
            }
        )
        if beat != PLAIN_BEAT:
            decorated += 1
            previous_decorated = True
        else:
            previous_decorated = False

    plan = {
        "schema_version": 1,
        "revision": contract_registry.canonical_hash(plan_items),
        "highlight_plan_revision": contract_registry.canonical_hash(segments),
        "items": plan_items,
    }
    return {
        "visual_plan": plan,
        "structured_layers": {"schema_version": 1, "items": layers},
    }


def validate(result: dict[str, Any]) -> list[str]:
    """Both artifacts must satisfy the contracts the renderer relies on."""
    return contract_registry.validate_artifact(
        "visual_plan", result["visual_plan"]
    ) + contract_registry.validate_artifact(
        "structured_layer", result["structured_layers"]
    )

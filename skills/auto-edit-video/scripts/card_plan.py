#!/usr/bin/env python3
"""One list of the cards a video shows, whoever asked for them.

Cards reached the screen down three unrelated paths — evidence-driven
structured layers, five-role highlight decks, and a legacy overlay
fallback — each with its own idea of what a card is and when it appears.
Adding a fourth for hand-authored cards would have made the drift worse, so
this is a single list instead, and it feeds the one path that already has a
plan-to-render contract.

Every card says where it came from. ``manual`` outranks everything: a card
the author placed by hand is never moved or dropped to make room for one a
model proposed, because the author can see the frame and the model cannot.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import contract_registry

CARD_PLAN_REL = Path("working/card_plan.json")
# Whose card survives when two want the same moment. Manual first: it was
# placed by someone looking at the picture.
ORIGIN_RANK = {"manual": 0, "model": 1, "director": 2}
# Kinds the compositor can actually draw today. The contract allows more so
# that a plan written for a newer renderer still validates, but rendering one
# fails loudly rather than dropping it silently.
DRAWABLE_KINDS = ("title", "stat", "chart", "dynamic_list")
MIN_CARD_SECONDS = 0.6


def card_id(*parts: Any) -> str:
    return "card-" + contract_registry.canonical_hash(list(parts))[:12]


def empty_plan(source_sha256: str) -> dict[str, Any]:
    return finalise({"schema_version": 1, "source_sha256": source_sha256, "items": []})


def finalise(plan: dict[str, Any]) -> dict[str, Any]:
    plan["items"] = sorted(plan.get("items", []), key=lambda item: float(item["start"]))
    plan["revision"] = contract_registry.canonical_hash(
        {key: value for key, value in plan.items() if key != "revision"}
    )
    return plan


def load(project_dir: Path, source_sha256: str) -> dict[str, Any]:
    path = project_dir / CARD_PLAN_REL
    if not path.is_file():
        return empty_plan(source_sha256)
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_plan(source_sha256)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        return empty_plan(source_sha256)
    return plan


def save(project_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    plan = finalise(plan)
    errors = contract_registry.validate_artifact("card_plan", plan)
    if errors:
        raise ValueError("card plan failed contract validation: " + "; ".join(errors))
    path = project_dir / CARD_PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scratch.replace(path)
    return plan


_ID_SHAPE = re.compile(r"^card-[0-9a-z]{6,16}$")


def normalised(card: dict[str, Any], origin: str) -> dict[str, Any]:
    """Fill in what a producer may reasonably have left out.

    A producer hands over what it decided — a moment and some content. It
    should not have to know the id format, and it certainly should not learn
    it from a schema error raised three calls later, at save.
    """
    card = dict(card)
    card.setdefault("origin", origin)
    if not _ID_SHAPE.fullmatch(str(card.get("id", ""))):
        card["id"] = card_id(card.get("start"), card.get("end"), card.get("payload"))
    return card


def merge(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Combine card lists, keeping the higher-ranked card when they collide.

    Returns (cards, notes). Every drop is reported: a card that vanishes
    without a word reads as a card the author never added.
    """
    notes: list[str] = []
    ordered = sorted(
        [*existing, *incoming],
        key=lambda item: (
            ORIGIN_RANK.get(str(item.get("origin")), 9),
            float(item.get("start", 0.0)),
        ),
    )
    kept: list[dict[str, Any]] = []
    for card in ordered:
        start = float(card.get("start", 0.0))
        end = float(card.get("end", 0.0))
        clash = next(
            (
                other
                for other in kept
                if start < float(other["end"]) - 0.001
                and end > float(other["start"]) + 0.001
            ),
            None,
        )
        if clash is not None:
            notes.append(
                f"dropped {card.get('origin')} card at {start:.2f}s: "
                f"{clash.get('origin')} card already holds that moment"
            )
            continue
        kept.append(card)
    kept.sort(key=lambda item: float(item["start"]))
    return kept, notes


def add(
    project_dir: Path,
    source_sha256: str,
    *,
    start: float,
    end: float,
    kind: str,
    payload: dict[str, Any],
    origin: str = "manual",
    editorial: bool = True,
    note: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Place one card, or say why it could not be placed."""
    if end - start < MIN_CARD_SECONDS:
        raise ValueError(
            f"a card needs at least {MIN_CARD_SECONDS}s on screen to be read"
        )
    plan = load(project_dir, source_sha256)
    card = {
        "id": card_id(start, end, kind, payload),
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "kind": kind,
        "payload": payload,
        "origin": origin,
        "editorial": bool(editorial),
    }
    if note:
        card["note"] = note[:400]
    merged, notes = merge(plan.get("items", []), [card])
    if not any(item["id"] == card["id"] for item in merged):
        raise ValueError("; ".join(notes) or "the card collided with an existing one")
    plan["items"] = merged
    plan["source_sha256"] = source_sha256
    return save(project_dir, plan), notes


def replace_origin(
    project_dir: Path,
    source_sha256: str,
    origin: str,
    cards: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Swap out every card from one producer, leaving the others alone.

    Re-running the director must not wipe a hand-placed card, and must not
    accumulate its own old proposals either.
    """
    plan = load(project_dir, source_sha256)
    others = [item for item in plan.get("items", []) if item.get("origin") != origin]
    merged, notes = merge(others, [normalised(card, origin) for card in cards])
    plan["items"] = merged
    plan["source_sha256"] = source_sha256
    return save(project_dir, plan), notes


def to_layer_bundle(
    plan: dict[str, Any], highlight_id: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render-ready structured layers + visual plan for the cards in this plan.

    The renderer already knows how to draw a visual_plan_v2 bundle, so the
    card list is expressed in that vocabulary rather than given a second
    renderer of its own.
    """
    layers: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for index, card in enumerate(plan.get("items", [])):
        kind = str(card.get("kind"))
        if kind not in DRAWABLE_KINDS:
            raise ValueError(
                f"card {card.get('id')} is a {kind!r} card, which this renderer "
                "cannot draw yet"
            )
        item_id = "visual-beat-" + contract_registry.canonical_hash(
            [card["id"], index]
        )[:12]
        layer_id = "structured-layer-" + contract_registry.canonical_hash(
            [card["id"], kind]
        )[:12]
        payload = dict(card["payload"])
        if kind == "title":
            # A title layer must declare how it is meant to read. Cards
            # placed by hand or by a model are hooks unless they say so.
            payload.setdefault("title_kind", "full-screen-hook")
        layers.append(
            {
                "id": layer_id,
                "type": kind,
                "payload": payload,
                "visual_plan_item_id": item_id,
                "revision": 1,
                "evidence_revision": contract_registry.canonical_hash(
                    card.get("evidence_ids") or []
                ),
                "review_status": "pending",
            }
        )
        items.append(
            {
                "id": item_id,
                "highlight_id": highlight_id or (
                    "highlight-" + contract_registry.canonical_hash([card["id"]])[:12]
                ),
                "start": float(card["start"]),
                "end": float(card["end"]),
                "beat": kind,
                "structured_layer_id": layer_id,
                "selected_asset": None,
                "conceptual_only": kind == "title",
                "evidence_ids": list(card.get("evidence_ids") or []),
                "review_status": "pending",
            }
        )
    visual_plan = {
        "schema_version": 1,
        "revision": contract_registry.canonical_hash(items),
        "highlight_plan_revision": plan.get("revision", ""),
        "items": items,
    }
    return {"schema_version": 1, "items": layers}, visual_plan

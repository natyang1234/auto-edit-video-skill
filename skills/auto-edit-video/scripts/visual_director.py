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
    # A quote and a question are read once; a definition and a contrast are
    # read twice, because both halves have to land.
    "quote": 4.0,
    "question": 3.5,
    "comparison": 5.0,
    "term": 4.5,
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


# Four more kinds of card, all built the same way the first four were: the
# words on them are lifted out of what was said, never composed. A card that
# paraphrases is a card asserting the speaker said something they did not.
#
# For a quote or a question that is easy — the card is the line. For a
# comparison or a definition it means the two halves have to be *found*, so
# each pattern below requires the connective that separates them. Without one
# the segment keeps its picture: the tool does not guess where "A" ends and
# "B" begins.

# A line worth pulling out and setting as a statement. These are the marks of
# a speaker landing a point.
#
# Deliberately narrow. 其實, 說真的, 老實說 and 你會發現 open a third of the
# sentences in ordinary Taiwanese speech and signal nothing; including 其實
# turned "這件事其實沒那麼複雜" — the fixture this repo uses as its example of
# plain prose — into a pull quote. A marker that fires on ordinary narration
# does not select, it just decorates.
INSIGHT_MARKERS = re.compile(
    r"(重點|關鍵在|關鍵是|記住|最重要的|千萬別|千萬不要|問題就在|說穿了"
    r"|\bthe point is\b|\bthe key is\b|\bhere'?s the thing\b)",
    re.IGNORECASE,
)
# An asked question, not a verbal tic. 對不對 and 好不好 end half the sentences
# in spoken teaching and ask nothing.
QUESTION_WORDS = re.compile(
    r"(為什麼|為何|怎麼辦|怎麼樣|該怎麼|什麼是|是什麼|哪一個|哪些|多少|如何"
    r"|\bwhy\b|\bhow do\b|\bwhat is\b|\bwhich\b)",
    re.IGNORECASE,
)
QUESTION_TAIL = re.compile(r"(嗎|呢|\?|？)\s*$")
QUESTION_TICS = re.compile(r"(對不對|好不好|是不是啊|你知道嗎)\s*$")
# Two things held against each other. The connective is what makes the split
# real; both sides are copied out around it.
CONTRASTS = (
    re.compile(r"不是(?P<a>[^，,。.！!？?]{2,20}?)而是(?P<b>[^，,。.！!？?]{2,20})"),
    re.compile(r"(?P<a>[^，,。.！!？?]{2,16})跟(?P<b>[^，,。.！!？?]{2,16}?)的?差別"),
    re.compile(r"(?P<a>[^，,。.！!？?]{2,16})和(?P<b>[^，,。.！!？?]{2,16}?)的?差別"),
    re.compile(r"比起(?P<a>[^，,。.！!？?]{2,16})[，,]?\s*(?P<b>[^，,。.！!？?]{2,20})更"),
)
# A term and what it means, again split on a connective that is actually said.
DEFINITIONS = (
    re.compile(r"所謂的?(?P<term>[^，,。.！!？?]{2,16})就是(?P<meaning>[^，,。.！!？?]{2,40})"),
    re.compile(r"(?P<term>[^，,。.！!？?]{2,16})指的是(?P<meaning>[^，,。.！!？?]{2,40})"),
    re.compile(r"(?P<term>[^，,。.！!？?]{2,16})的意思是(?P<meaning>[^，,。.！!？?]{2,40})"),
)
# "這個東西叫做虛主詞" names the thing second, so this one is read backwards.
#
# Anchored at the end, because unpunctuated speech gives no other signal for
# where the name stops. "所以呢雪茄叫做cigar它是大隻的" carries on past the
# name, and taking everything after 叫做 put "cigar它是大隻的" on a card as
# though that were the term. When the sentence continues there is no way to
# tell, so no card — a wrong term is worse than none.
#
# Six characters, because a name is a name and not a clause: every term
# this is for fits (虛主詞, 不定詞片語, 真正的主詞), while the run-ons that
# reach the end of a sentence — 「cigar它是大隻的」, 「香菸來補充給你」 —
# do not.
NAMED_AS = re.compile(
    r"(?P<meaning>[^，,。.！!？?]{2,40})叫做(?P<term>[^，,。.！!？?]{2,6})$"
)
# A pulled quote that fills the frame stops being a quote and starts being a
# wall of text.
MAX_QUOTE_CHARS = 34
MAX_QUESTION_CHARS = 28


# An enumeration that starts unannounced: 「...更多的錢。第二個願望...第三個
# 願望...」 marks its second and third items but never says 第一. The line
# spoken immediately before the first marked item is that first item.
_LATER_ORDINAL = re.compile(r"第[二三四五六七八九十]|其次|再來|最後|\bsecond\b|\bthird\b", re.IGNORECASE)


_ORDINAL_VALUE = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL = re.compile(r"第([一二三四五六七八九十])")


def _ordinal_of(literal: str) -> int | None:
    found = _ORDINAL.search(literal)
    return _ORDINAL_VALUE[found.group(1)] if found else None


def enumerated_quotes(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The spoken items of a list, in order — one rule for finding them.

    Written twice before (classify and build), which is the drift this repo
    keeps paying for; both callers now ask here.
    """
    ordered = sorted(quotes, key=lambda item: float(item.get("start", 0.0)))
    marked = [
        index for index, quote in enumerate(ordered)
        if LIST_MARKERS.search(str(quote.get("literal", "")))
    ]
    # 第五顆蛋糕 carries an ordinal and is not a list item: 第二, 第三, …
    # 第五 skips a number, and an enumeration does not. Among the marked
    # quotes whose markers are numbered, keep the longest consecutive run;
    # markers without numbers (首先, 然後, 最後) are kept as they are.
    numbered = [
        (index, _ordinal_of(str(ordered[index].get("literal", ""))))
        for index in marked
    ]
    if sum(1 for _i, value in numbered if value is not None) >= 2:
        runs: list[list[int]] = []
        for index, value in numbered:
            if value is None:
                continue
            if runs and value == runs[-1][-1][1] + 1:
                runs[-1].append((index, value))
            else:
                runs.append([(index, value)])
        best = max(runs, key=len)
        kept = {index for index, _value in best}
        marked = [
            index for index, value in numbered
            if value is None or index in kept
        ]
    items = [ordered[index] for index in marked]
    if (
        len(items) == LIST_MIN_ITEMS - 1
        and marked
        and marked[0] > 0
        and _LATER_ORDINAL.search(str(ordered[marked[0]].get("literal", "")))
    ):
        # The words on the card are still all spoken; only the grouping is
        # inferred, and only when the first marker says it is not the first.
        items = [ordered[marked[0] - 1], *items]
    return items


def _first_match(patterns, text: str):
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found
    return None


def _is_question(literal: str) -> bool:
    text = literal.strip()
    if not text or len(text) > MAX_QUESTION_CHARS:
        return False
    if QUESTION_TICS.search(text):
        return False
    return bool(QUESTION_WORDS.search(text) or QUESTION_TAIL.search(text))


def _quote_payload(layer_id: str, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": layer_id,
        "type": "quote",
        "payload": {
            "quote": str(quote["literal"]).strip(),
            "evidence_id": quote["id"],
            "source_literal": str(quote["literal"]).strip(),
        },
    }


def _question_payload(layer_id: str, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": layer_id,
        "type": "question",
        "payload": {
            "question": str(quote["literal"]).strip(),
            "evidence_id": quote["id"],
            "source_literal": str(quote["literal"]).strip(),
        },
    }


def _comparison_payload(
    layer_id: str, quotes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for quote in quotes:
        text = str(quote.get("literal", ""))
        found = _first_match(CONTRASTS, text)
        if not found:
            continue
        left, right = found.group("a").strip(), found.group("b").strip()
        if not left or not right or left == right:
            continue
        return {
            "id": layer_id,
            "type": "comparison",
            "payload": {
                "left": left,
                "right": right,
                "evidence_id": quote["id"],
                "source_literal": text.strip(),
            },
        }
    return None


def _term_payload(layer_id: str, quotes: list[dict[str, Any]]) -> dict[str, Any] | None:
    for quote in quotes:
        text = str(quote.get("literal", ""))
        found = _first_match(DEFINITIONS, text) or NAMED_AS.search(text)
        if not found:
            continue
        term, meaning = found.group("term").strip(), found.group("meaning").strip()
        if not term or not meaning:
            continue
        return {
            "id": layer_id,
            "type": "term",
            "payload": {
                "term": term,
                "meaning": meaning,
                "evidence_id": quote["id"],
                "source_literal": text.strip(),
            },
        }
    return None


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


def _classify(
    found: list[dict[str, Any]], is_opening: bool, has_editorial: bool = False
) -> str:
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
    # The opening window is the clip's nameplate — when there is a name. A
    # title card requires editorial copy: with the model unavailable the
    # fallback was a transcript sentence, and a KTV clip shipped its own
    # (mis-heard) transcript as a prominently displayed card. The words are
    # already on screen as captions; a card repeating them adds nothing and
    # amplifies whatever the recogniser got wrong. Without a name the
    # opening window is read like any other.
    if is_opening and quotes and has_editorial:
        return "title"
    if len(enumerated_quotes(quotes)) >= LIST_MIN_ITEMS:
        return "dynamic_list"
    # Most specific first: a definition and a contrast are recognised by a
    # connective that is actually spoken, so when one is there it is the more
    # certain reading. A question is next, and a pulled quote — which asks
    # only for a marker of emphasis — last.
    literals = [str(quote.get("literal", "")) for quote in quotes]
    if any(_first_match(DEFINITIONS, text) or NAMED_AS.search(text) for text in literals):
        return "term"
    if any(_first_match(CONTRASTS, text) for text in literals):
        return "comparison"
    if any(_is_question(text) for text in literals):
        return "question"
    if any(
        INSIGHT_MARKERS.search(text) and len(text.strip()) <= MAX_QUOTE_CHARS
        for text in literals
    ):
        return "quote"
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
    #
    # Rounded, not floored. Three windows at a half share is one and a half,
    # and flooring made it one — which the opening title always took, so a
    # definition or a question later in the same clip could never be drawn no
    # matter what was said. Two of three is still the share this is set to,
    # and the never-two-in-a-row rule below is what actually keeps them apart.
    budget = max(1, round(len(segments) * max_decorated_share)) if segments else 0
    decorated = 0
    previous_decorated = False

    # An enumeration is a clip-level structure. 「...更多的錢。第二個願望...
    # 第三個願望...」 spreads its items across ten seconds, so no single
    # planning window ever holds three of them and counting per window found
    # nothing to draw. Found once over the whole clip, drawn in the window
    # where its middle falls — while the items are still being spoken.
    clip_quotes = [item for item in evidence if item.get("kind") == "quote"]
    clip_list = enumerated_quotes(clip_quotes)
    clip_list_mid: float | None = None
    if len(clip_list) >= LIST_MIN_ITEMS:
        clip_list_mid = (
            float(clip_list[0].get("start", 0.0))
            + float(clip_list[-1].get("end", 0.0))
        ) / 2.0

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
        beat = _classify(
            found, is_opening=index == 0,
            has_editorial=bool(editorial_title.strip()),
        )
        # The clip-wide list lands in the first plain window at or after its
        # middle. The middle itself often falls in the opening window, which
        # the title already holds — the next window is still inside the
        # enumeration being spoken.
        uses_clip_list = False
        if (
            clip_list_mid is not None
            and beat == PLAIN_BEAT
            and end > clip_list_mid
        ):
            beat = "dynamic_list"
            uses_clip_list = True
            clip_list_mid = None

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
                layer = _list_payload(
                    layer_id,
                    clip_list if uses_clip_list else enumerated_quotes(quotes),
                )
                evidence_ids = [entry["evidence_id"] for entry in layer["payload"]["items"]]
            elif beat in {"term", "comparison", "quote", "question"}:
                builder = {
                    "term": _term_payload,
                    "comparison": _comparison_payload,
                }.get(beat)
                if builder is not None:
                    layer = builder(layer_id, quotes)
                else:
                    picked = next(
                        (
                            quote for quote in quotes
                            if (
                                _is_question(str(quote.get("literal", "")))
                                if beat == "question"
                                else INSIGHT_MARKERS.search(str(quote.get("literal", "")))
                            )
                        ),
                        None,
                    )
                    layer = (
                        None if picked is None
                        else (_question_payload if beat == "question" else _quote_payload)(
                            layer_id, picked
                        )
                    )
                # The classifier found a pattern; the builder re-finds it to
                # copy the words out. If the second look comes up empty the
                # segment keeps its picture rather than getting an empty card.
                if layer is None:
                    beat, layer_id = PLAIN_BEAT, None
                else:
                    evidence_ids = [layer["payload"]["evidence_id"]]
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
            # The opening title is the clip's nameplate, not a mid-roll
            # decoration: it holds the screen for three seconds and leaves.
            # Counting it against the budget meant a short clip — two or
            # three planning windows — spent its whole allowance on the
            # title, and a list or a question later in the same clip could
            # never be drawn no matter what was said.
            if not (index == 0 and beat == "title"):
                decorated += 1
                previous_decorated = True
            else:
                previous_decorated = False
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

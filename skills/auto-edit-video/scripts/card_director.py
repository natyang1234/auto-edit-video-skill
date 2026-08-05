#!/usr/bin/env python3
"""Ask a model where a card belongs, then check it against what was said.

The rule-based director recognises four situations — enough numbers, a
number, an enumeration, an opening — and stays silent everywhere else,
which on ordinary speech is everywhere. A reel that puts a widget on screen
when the speaker mentions recording themselves is not matching a pattern;
it understood the sentence.

So the model proposes, and the transcript adjudicates, exactly as it does
for cut selection: a card must quote something actually spoken inside the
window it claims. Wording on the card may be condensed — that is what a
card is for — but the moment it points at has to be real.
"""
from __future__ import annotations

import json
from typing import Any

import card_plan
import editorial_planner
from editorial_planner import EditorialUnavailable, normalise

GENERATOR = "card-director-v1"
# Cards compete with the speaker. One every few seconds is a busy reel; one
# every few seconds for a whole minute is a slideshow with a person behind it.
MIN_GAP_SECONDS = 4.0
SECONDS_PER_CARD = 8.0
MAX_CARDS = 12
DEFAULT_SECONDS = 3.0
MIN_QUOTE_CHARS = 4

KIND_GUIDE = """- note：講到某個東西／工具／紀錄時，用小卡把它具象化。
  payload：{"icon":"emoji","title":"那是什麼","meta":"右上小字，可省","waveform":true 代表錄音}
- chip：一句很短的流程或關係，像「A → B → C」。payload：{"text":"..."}
- statement：這段在數第幾件事，或一句要記住的話。payload：{"lead":"03 可省","text":"..."}
- title：整段的主題標題。payload：{"title":"..."}
- stat：講到一個帶單位的量值時。payload：{"value":"30 分鐘","label":"在說什麼"}"""


def build_prompt(view: str, *, duration_s: float, budget: int) -> str:
    return f"""你是短影音美術。這是一支影片的逐字稿，附時間碼：

{view}

請挑出最多 {budget} 個「值得在畫面上配一張卡」的時刻。
卡是用來把講到的東西具象化，不是把字幕再寫一次。寧可少給也不要每句都配。

可用的卡種類：
{KIND_GUIDE}

每一筆要有：
- at：卡出現的秒數（0–{duration_s:.3f}）
- seconds：停留幾秒（2–5）
- kind：上面其中一種
- payload：對應那個種類的欄位
- quote：這個時刻**逐字稿裡的原句**，必須一字不差出現在 at 到 at+seconds 之間
- reason：為什麼這裡值得配卡

卡上的字可以精簡改寫，但 quote 必須是原話。兩張卡之間至少隔 {MIN_GAP_SECONDS:.0f} 秒。

只回傳 JSON array，不要 markdown：
[{{"at":8.0,"seconds":3,"kind":"note","payload":{{"icon":"🎙","title":"採訪自己"}},\
"quote":"我就把自己錄起來","reason":"把抽象動作變成看得見的東西"}}]"""


def ground_cards(
    proposals: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    *,
    duration_s: float,
    budget: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep the cards the video actually supports; say why the rest went."""
    notes: list[str] = []
    accepted: list[dict[str, Any]] = []

    for index, proposal in enumerate(proposals, start=1):
        label = f"{proposal.get('kind', '?')} card {index}"
        try:
            at = float(proposal.get("at"))
            seconds = float(proposal.get("seconds") or DEFAULT_SECONDS)
        except (TypeError, ValueError):
            notes.append(f"dropped {label}: timing is not numeric")
            continue
        seconds = max(card_plan.MIN_CARD_SECONDS, min(6.0, seconds))
        end = at + seconds
        if at < 0 or end > duration_s + 0.05:
            notes.append(f"dropped {label}: {at:.2f}s is outside the video")
            continue

        kind = str(proposal.get("kind") or "")
        if kind not in card_plan.DRAWABLE_KINDS:
            notes.append(f"dropped {label}: {kind!r} is not a card this can draw")
            continue
        payload = proposal.get("payload")
        if not isinstance(payload, dict) or not payload:
            notes.append(f"dropped {label}: no payload")
            continue

        spoken = "".join(
            str(clause.get("text", ""))
            for clause in clauses
            if float(clause["end"]) > at - 1.0 and float(clause["start"]) < end + 1.0
        )
        quote = str(proposal.get("quote") or "").strip()
        if len(normalise(quote)) < MIN_QUOTE_CHARS:
            notes.append(f"dropped {label}: quote too short to verify")
            continue
        if normalise(quote) not in normalise(spoken):
            # The model placed a card at a moment it invented.
            notes.append(f"dropped {label}: nothing like that is said at {at:.2f}s")
            continue

        accepted.append(
            {
                "start": round(at, 3),
                "end": round(end, 3),
                "kind": kind,
                "payload": payload,
                "origin": "model",
                "editorial": True,
                "note": str(proposal.get("reason") or "")[:400],
            }
        )

    accepted.sort(key=lambda card: card["start"])
    spaced: list[dict[str, Any]] = []
    for card in accepted:
        if spaced and card["start"] - spaced[-1]["start"] < MIN_GAP_SECONDS:
            notes.append(
                f"dropped {card['kind']} card at {card['start']:.2f}s: "
                f"within {MIN_GAP_SECONDS:.0f}s of the one before it"
            )
            continue
        spaced.append(card)
    if len(spaced) > budget:
        notes.append(f"kept {budget} of {len(spaced)} cards to leave the speaker room")
        spaced = spaced[:budget]
    return spaced, notes


def card_budget(duration_s: float) -> int:
    return max(1, min(MAX_CARDS, int(duration_s // SECONDS_PER_CARD)))


def propose_cards(
    transcript: dict[str, Any],
    *,
    duration_s: float,
    provider: tuple[str, ...] = editorial_planner.DEFAULT_PROVIDER,
    timeout_s: int = editorial_planner.DEFAULT_TIMEOUT_S,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cards a model proposed and the transcript confirmed."""
    clauses = editorial_planner.highlight_planner.transcript_clauses(
        transcript, duration_s
    )
    if not clauses:
        raise EditorialUnavailable("transcript has no timed spoken clauses")
    budget = card_budget(duration_s)
    prompt = build_prompt(
        editorial_planner.transcript_view(transcript),
        duration_s=duration_s,
        budget=budget,
    )
    proposals = editorial_planner.parse_json_payload(
        editorial_planner.call_provider(prompt, provider=provider, timeout_s=timeout_s)
    )
    if not proposals:
        raise EditorialUnavailable("the model proposed no cards")
    cards, notes = ground_cards(
        proposals, clauses, duration_s=duration_s, budget=budget
    )
    if not cards:
        raise EditorialUnavailable(
            "no proposed card survived grounding: " + "; ".join(notes[:4])
        )
    return cards, notes


def main() -> int:
    import argparse
    import shlex
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--provider", default="")
    parser.add_argument("--timeout", type=int, default=editorial_planner.DEFAULT_TIMEOUT_S)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).expanduser().resolve()
    manifest = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    transcript = json.loads(
        (project_dir / "working/transcript_words.json").read_text(encoding="utf-8")
    )
    source = manifest.get("source") or {}
    try:
        cards, notes = propose_cards(
            transcript,
            duration_s=float(source.get("duration_s") or 0.0),
            provider=tuple(shlex.split(args.provider))
            if args.provider
            else editorial_planner.DEFAULT_PROVIDER,
            timeout_s=args.timeout,
        )
    except (EditorialUnavailable, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    plan, merge_notes = card_plan.replace_origin(
        project_dir, str(source.get("sha256") or ""), "model", cards
    )
    print(
        json.dumps(
            {
                "ok": True,
                "cards": len(plan["items"]),
                "proposed": len(cards),
                "notes": notes + merge_notes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

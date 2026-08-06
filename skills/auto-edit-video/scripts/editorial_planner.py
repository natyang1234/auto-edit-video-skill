#!/usr/bin/env python3
"""Editorial highlight selection: a model reads the transcript and names the cuts.

The deterministic planner scores clause windows on regex signals. It is
reproducible and auditable, and it cannot tell that "your head would be too
heavy and you'd tip over" is the memorable part of a grammar lesson. A model
that has read the whole transcript can, and it can give the cut a name a
person would click on.

What the model is trusted with and what it is not:

- trusted: which spans are worth cutting, in what order they matter, and what
  to call them. That is editorial judgement.
- not trusted: the words themselves. Every proposal is snapped to real clause
  boundaries, and its hook must appear verbatim inside the window it claims.
  A hook the transcript does not contain is a fabrication and the proposal is
  dropped, not repaired.

Editorial titles are condensed, never verbatim, so they are carried in a
separate ``editorial`` block marked ``is_editorial_copy``. The item's own
``title`` stays an exact transcript extract, which keeps the existing
highlight-plan contract unchanged (PRD §"Hook 預設取自原片真實句子；標題與圖卡
可濃縮，但標記為 editorial copy，不能用引號偽裝成逐字原話").
"""
from __future__ import annotations

import json
import re
import text_joining
import shutil
import subprocess
import uuid
from typing import Any

import highlight_planner

GENERATOR = "editorial-model-highlight-planner-v1"
DEFAULT_PROVIDER = ("openclaw", "agent", "--agent", "agent-7")
DEFAULT_TIMEOUT_S = 600
MAX_TRANSCRIPT_CHARS = 60000
MAX_TITLE_CHARS = 24
MIN_HOOK_CHARS = 4
MIN_KEYWORDS = 3
MAX_KEYWORDS = 6
# A keyword is a term, not a clause. Long enough to mean something, short
# enough that emphasising it does not just recolour the whole line.
MIN_KEYWORD_CHARS = 2
MAX_KEYWORD_CHARS = 8
MAX_LATIN_KEYWORD_CHARS = 20
# A hook is quoted speech; allow for punctuation and spacing drift between the
# model's echo and the transcript, but nothing more.
_NOISE = re.compile(r"[\s，,。.、！!？?：:；;「」『』\"'（）()\-—…]+")


class EditorialUnavailable(RuntimeError):
    """The provider could not be reached or produced nothing usable."""


def normalise(text: str) -> str:
    return _NOISE.sub("", str(text or "")).lower()


def transcript_view(transcript: dict[str, Any], limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Timecoded lines — the only form in which the model sees the source."""
    lines: list[str] = []
    segments = transcript.get("caption_segments") or transcript.get("segments") or []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        lines.append(
            f"[{float(segment.get('start', 0.0)):08.2f}-"
            f"{float(segment.get('end', 0.0)):08.2f}] {text}"
        )
    view = "\n".join(lines)
    if len(view) > limit:
        raise EditorialUnavailable(
            f"transcript view is {len(view)} chars, over the {limit} limit for one pass"
        )
    return view


def build_prompt(
    view: str,
    *,
    duration_s: float,
    count: int,
    min_duration: float,
    max_duration: float,
    brief: str = "",
) -> str:
    brief_line = f"\n剪輯指示（優先遵守）：{brief}\n" if brief.strip() else ""
    return f"""你是短影音精華剪輯師。完整讀取這份有時間碼的逐字稿：

{view}
{brief_line}
從這支長影片選出最多 {count} 個互不重疊的短影音候選。每段必須：
- 長度介於 {min_duration:.0f}–{max_duration:.0f} 秒
- 開頭能獨立形成 Hook，內容有完整語意與結尾
- 優先保留具體觀點、反差、故事、教學結論或情緒高點
- 不因湊數重複同一論點；寧可少給也不要硬湊
- start/end 必須使用逐字稿可見的實際秒數，範圍在 0–{duration_s:.3f}

欄位規則：
- title：你自己下的短標題，{MAX_TITLE_CHARS} 字以內，講這段在講什麼，不要照抄原句
- hook：這段開頭的**逐字稿原句**，必須一字不差出現在你選的時間範圍內
- reason：為什麼這段值得單獨發
- keywords：這段字幕裡該highlight的關鍵詞 {MIN_KEYWORDS}–{MAX_KEYWORDS} 個。
  每個都必須**一字不差出現在這段的逐字稿裡**，而且要是完整的詞
  （例如「虛主詞」「不定詞」「單數」「cigar」），不要切一半、不要整句、不要虛詞

只回傳 JSON array，不要 markdown、不要說明文字：
[{{"title":"短標題","start":0.0,"end":30.0,"hook":"開頭原句","reason":"入選理由",\
"keywords":["關鍵詞1","關鍵詞2","關鍵詞3"]}}]"""


def parse_json_payload(text: str) -> list[dict[str, Any]]:
    """Pull the JSON array out of whatever wrapper the provider returned."""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fenced:
        payload = fenced.group(1)
    else:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise EditorialUnavailable("provider response contained no JSON array")
        payload = text[start : end + 1]
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise EditorialUnavailable(f"provider response was not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise EditorialUnavailable("provider response must be a JSON array")
    return [item for item in data if isinstance(item, dict)]


def call_provider(
    prompt: str,
    *,
    provider: tuple[str, ...] = DEFAULT_PROVIDER,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    if not provider:
        raise EditorialUnavailable("no editorial provider configured")
    if shutil.which(provider[0]) is None:
        raise EditorialUnavailable(f"editorial provider {provider[0]!r} is not installed")
    command = [
        *provider,
        "--session-id", f"auto-edit-editorial-{uuid.uuid4().hex[:12]}",
        "--message", prompt,
        "--json",
    ]
    try:
        result = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise EditorialUnavailable(f"editorial provider timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        raise EditorialUnavailable(
            f"editorial provider exited {result.returncode}: {(result.stderr or '')[-400:]}"
        )
    return extract_reply(result.stdout or "")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def extract_reply(raw: str) -> str:
    """Pull the assistant's text out of whatever the provider printed.

    Gateways print startup notices, migration warnings and colour codes
    before the envelope, so the envelope is decoded from the first brace
    rather than assuming stdout is JSON from byte zero. A provider that just
    prints the array is equally fine.
    """
    text = _ANSI.sub("", raw)
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        try:
            envelope, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        reply = _reply_from_envelope(envelope)
        if reply is not None:
            return reply
        break
    return text


def _reply_from_envelope(envelope: Any) -> str | None:
    if isinstance(envelope, list):
        if envelope and isinstance(envelope[0], dict) and "text" in envelope[0]:
            return "\n".join(str(part.get("text", "")) for part in envelope)
        # A bare array of proposals is already the answer.
        return json.dumps(envelope, ensure_ascii=False)
    if not isinstance(envelope, dict):
        return None
    payloads = (envelope.get("result") or {}).get("payloads")
    if isinstance(payloads, list) and payloads:
        joined = "\n".join(
            str(part.get("text", "")) for part in payloads if isinstance(part, dict)
        )
        if joined.strip():
            return joined
    for key in ("text", "message", "content", "reply"):
        if isinstance(envelope.get(key), str):
            return envelope[key]
    return None


def keyword_length_ok(term: str) -> bool:
    """Is this a term rather than a clause?

    Eight CJK characters is a sentence fragment; nine Latin letters is still
    one word ("cigarette"). Measuring both on one ruler either lets Chinese
    clauses through or throws English words away.

    The two limits stay separate — a term and a clause are not the same
    question as how much room text takes — but what counts as Chinese comes
    from the shared rule, because that part had already been written twice.
    """
    if len(term) < MIN_KEYWORD_CHARS:
        return False
    if text_joining.has_wide(term):
        return len(term) <= MAX_KEYWORD_CHARS
    return len(term) <= MAX_LATIN_KEYWORD_CHARS and len(term.split()) <= 2


def ground_keywords(raw: Any, spoken: str) -> tuple[list[str], list[str]]:
    """Keep the keywords the cut actually says; report the ones it does not.

    Emphasis is the one place a wrong word is worse than no word: it points
    the eye at something and asserts it matters. A term the speaker never
    said would be highlighted in someone else's sentence.
    """
    if not isinstance(raw, list):
        return [], []
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for value in raw:
        term = str(value or "").strip()
        if not term or not keyword_length_ok(term):
            if term:
                dropped.append(term)
            continue
        if term.casefold() in seen:
            continue
        if term not in spoken:
            dropped.append(term)
            continue
        seen.add(term.casefold())
        kept.append(term)
        if len(kept) >= MAX_KEYWORDS:
            break
    return kept, dropped


def snap_to_clauses(
    clauses: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """The clauses whose spoken time falls inside the proposed window."""
    return [
        clause
        for clause in clauses
        if float(clause["start"]) >= start - 0.35 and float(clause["end"]) <= end + 0.35
    ]


def ground_proposals(
    proposals: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    *,
    duration_s: float,
    min_duration: float,
    max_duration: float,
    count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn model proposals into transcript-grounded highlight items.

    Every rejection is reported. A silently dropped proposal would read as
    "the model only found three" when it found five and two were fabricated.
    """
    grounded: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, proposal in enumerate(proposals, start=1):
        label = str(proposal.get("title") or f"proposal {index}").strip()[:40]
        missing = {"title", "start", "end", "hook", "reason"} - set(proposal)
        if missing:
            warnings.append(f"dropped {label!r}: missing {sorted(missing)}")
            continue
        try:
            start = float(proposal["start"])
            end = float(proposal["end"])
        except (TypeError, ValueError):
            warnings.append(f"dropped {label!r}: timing is not numeric")
            continue
        if not (0 <= start < end <= duration_s + 0.05):
            warnings.append(f"dropped {label!r}: {start:.2f}-{end:.2f} is outside the source")
            continue
        if not min_duration - 0.05 <= end - start <= max_duration + 0.05:
            warnings.append(
                f"dropped {label!r}: {end - start:.1f}s is outside "
                f"{min_duration:.0f}-{max_duration:.0f}s"
            )
            continue

        window = snap_to_clauses(clauses, start, end)
        if not window:
            warnings.append(f"dropped {label!r}: no spoken clause falls inside it")
            continue

        spoken = "".join(str(clause.get("text", "")) for clause in window)
        hook = str(proposal["hook"]).strip()
        if len(normalise(hook)) < MIN_HOOK_CHARS:
            warnings.append(f"dropped {label!r}: hook is too short to verify")
            continue
        if normalise(hook) not in normalise(spoken):
            # The model quoted something the transcript does not say here.
            warnings.append(f"dropped {label!r}: hook is not spoken inside the window")
            continue

        title = str(proposal["title"]).strip()[:MAX_TITLE_CHARS]
        if not title:
            warnings.append(f"dropped {label!r}: title is empty")
            continue

        keywords, dropped = ground_keywords(proposal.get("keywords"), spoken)
        if dropped:
            warnings.append(
                f"{label!r}: ignored keywords not spoken in the cut: {dropped[:5]}"
            )

        snapped_start = round(float(window[0]["start"]), 3)
        snapped_end = round(float(window[-1]["end"]), 3)
        grounded.append(
            {
                "start": snapped_start,
                "end": snapped_end,
                "spoken": spoken,
                "window": window,
                "editorial": {
                    "title": title,
                    "hook": hook,
                    "reason": str(proposal["reason"]).strip()[:400],
                    "keywords": keywords,
                    "is_editorial_copy": True,
                    "generator": GENERATOR,
                },
            }
        )

    grounded.sort(key=lambda item: item["start"])
    kept: list[dict[str, Any]] = []
    for item in grounded:
        if kept and item["start"] < kept[-1]["end"] - 0.05:
            warnings.append(
                f"dropped {item['editorial']['title']!r}: overlaps the cut before it"
            )
            continue
        kept.append(item)

    if len(kept) > count:
        warnings.append(f"provider returned {len(kept)} usable cuts; kept the first {count}")
        kept = kept[:count]
    return kept, warnings


def to_highlight_items(grounded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit the plan-item shape the rest of the pipeline already consumes."""
    items: list[dict[str, Any]] = []
    for rank, item in enumerate(grounded, start=1):
        spoken = item["spoken"]
        # The item's own title stays an exact extract so the existing plan
        # contract holds; the editorial wording rides alongside it, labelled.
        extract = spoken.strip()[:36] or spoken.strip()
        identifier = "highlight-" + highlight_planner.canonical_hash(
            {"start": item["start"], "end": item["end"], "text": spoken}
        )[:12]
        items.append(
            {
                "id": identifier,
                "rank": rank,
                "start": item["start"],
                "end": item["end"],
                "title": extract,
                "title_source": "transcript_extract",
                "editorial": item["editorial"],
                # Ranked by the model's ordering, not by a signal score. The
                # field is kept in range so the plan contract still validates.
                "score": round(max(0.0, 1.0 - (rank - 1) * 0.05), 6),
                "review_status": "pending",
                "evidence": {
                    "text": spoken,
                    "exact_transcript_extract": True,
                    "start": item["start"],
                    "end": item["end"],
                },
            }
        )
    return items


def plan_editorial_highlights(
    transcript: dict[str, Any],
    *,
    duration_s: float,
    count: int,
    min_duration: float,
    max_duration: float,
    brief: str = "",
    provider: tuple[str, ...] = DEFAULT_PROVIDER,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ask a model to cut the video, then verify every cut against the source."""
    clauses = highlight_planner.transcript_clauses(transcript, duration_s)
    if not clauses:
        raise EditorialUnavailable("transcript has no timed spoken clauses")
    prompt = build_prompt(
        transcript_view(transcript),
        duration_s=duration_s,
        count=count,
        min_duration=min_duration,
        max_duration=max_duration,
        brief=brief,
    )
    proposals = parse_json_payload(
        call_provider(prompt, provider=provider, timeout_s=timeout_s)
    )
    if not proposals:
        raise EditorialUnavailable("provider proposed no cuts")
    grounded, warnings = ground_proposals(
        proposals,
        clauses,
        duration_s=duration_s,
        min_duration=min_duration,
        max_duration=max_duration,
        count=count,
    )
    if not grounded:
        raise EditorialUnavailable(
            "no proposal survived grounding: " + "; ".join(warnings[:4])
        )
    return to_highlight_items(grounded), warnings

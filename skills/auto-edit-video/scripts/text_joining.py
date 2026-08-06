#!/usr/bin/env python3
"""How transcript tokens become a readable line, and how wide that line is.

Whether a space goes between two tokens is one editorial rule, and it was
written out three times: once for caption text, once for highlight text, and
once inside the routine that maps a calibrated sentence back onto word ids.
Those three have to agree exactly — the last one searches for text the first
two produced, so a punctuation mark added to one set and not the others
means the search quietly finds nothing and the grounding is lost.

Measuring length has the same problem. Anything that caps text by counting
characters is really asking how much room it takes, and a Chinese character
takes about twice the room of a Latin letter. Counting both on one ruler cost
an English word that fit ("cigarette", thrown out by an eight-character
limit) and passed a Chinese title that did not (thirty-six characters, drawn
as one unreadable line across the whole frame).
"""
from __future__ import annotations

import re

# Punctuation that closes: never preceded by a space.
NO_SPACE_BEFORE = frozenset("，。！？、,.!?：:；;％%）)]}〉》」』…")
# Punctuation that opens: never followed by one.
NO_SPACE_AFTER = frozenset("（([{〈《「『")


def needs_space(previous: str, current: str) -> bool:
    """Does a space belong between these two adjacent characters?"""
    if not previous or not current:
        return False
    if current in NO_SPACE_BEFORE or previous in NO_SPACE_AFTER:
        return False
    # Latin needs the space that Chinese does not.
    return previous.isascii() or current.isascii()


def join_tokens(tokens) -> str:
    """Join transcript tokens into the line a viewer reads."""
    result = ""
    for raw in tokens:
        token = str(raw).strip()
        if not token:
            continue
        if not result:
            result = token
            continue
        result += (" " if needs_space(result[-1], token[0]) else "") + token
    return result.strip()


NO_LINE_BREAK_BEFORE_OPENERS = "（(「『【《〈“‘\"'"

# Han, plus the kana and full-width punctuation that sit at the same width.
# One definition: a second copy in another module is a second answer to
# "is this Chinese", and this repo has watched two such copies drift apart.
WIDE = re.compile(r"[⺀-〿぀-ヿ㐀-䶿一-鿿"
                  r"豈-﫿＀-｠￠-￦]")


def has_wide(text: str) -> bool:
    """Does this text contain characters drawn at full width?"""
    return bool(WIDE.search(str(text)))


def display_width(text: str) -> float:
    """Roughly how much room the text takes, in Latin letters.

    Not a typesetting measurement — the compositor does that properly, with
    the real font. This is for the places that cap text before anyone has a
    font in hand, and only needs to be right about the two-to-one difference
    that a character count gets wrong.
    """
    return sum(2.0 if WIDE.match(character) else 1.0 for character in str(text))


def trim_to_width(text: str, limit: float) -> str:
    """The longest leading run of the text that fits, without splitting wide.

    Trailing whitespace and an orphaned opening bracket go with it: a title
    ending on an unclosed quotation mark reads as though the render failed.
    """
    if display_width(text) <= limit:
        return str(text)
    kept: list[str] = []
    used = 0.0
    for character in str(text):
        step = 2.0 if WIDE.match(character) else 1.0
        if used + step > limit:
            break
        kept.append(character)
        used += step
    return "".join(kept).rstrip().rstrip(NO_LINE_BREAK_BEFORE_OPENERS)

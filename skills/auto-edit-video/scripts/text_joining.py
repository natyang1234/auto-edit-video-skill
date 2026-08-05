#!/usr/bin/env python3
"""How transcript tokens become a readable line.

Whether a space goes between two tokens is one editorial rule, and it was
written out three times: once for caption text, once for highlight text, and
once inside the routine that maps a calibrated sentence back onto word ids.
Those three have to agree exactly — the last one searches for text the first
two produced, so a punctuation mark added to one set and not the others
means the search quietly finds nothing and the grounding is lost.
"""
from __future__ import annotations

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

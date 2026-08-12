#!/usr/bin/env python3
"""CoreText caption compositor — the single caption raster truth (Phase 1b).

Renders each caption/emphasis overlay into a tight-bbox RGBA PNG with
CTFramesetter shaping (real line breaking, per-run font fallback) and writes
the ``caption_render_plan`` contract artifact. macOS is the single supported
runtime; the engine version is part of every cache key and receipt.

Font policy (plan v2 B2): text is laid out with the project's resolved font
file plus a sanctioned emoji cascade (Apple Color Emoji). Any OTHER system
fallback CoreText picks is recorded and flagged — final rendering fails
closed on unsanctioned fallbacks.
"""
from __future__ import annotations

import hashlib
import math
import os
import platform
import re
import struct
from pathlib import Path
from typing import Any

import caption_engine
import contract_registry

CAPTIONS_REL = Path("working/captions")
RENDER_PLAN_REL = Path("working/caption_render_plan.json")
SANCTIONED_FALLBACK_PS_NAMES = {"AppleColorEmoji"}
# Compatibility name for pre-project-font plans/tests. New glyph runs use the
# actual selected asset id and never emit this placeholder.
PROJECT_FONT_ASSET_ID = "font-project-default"
EMOJI_FONT_ASSET_ID = "font-system-emoji"
# A translation is support, not a second headline.
TRANSLATION_SCALE = 0.62
# ...and it carries a lighter outline in proportion.
TRANSLATION_STROKE = 0.55

_CORETEXT = None


def _load_coretext():
    global _CORETEXT
    if _CORETEXT is not None:
        return _CORETEXT
    if os.environ.get("AUTO_EDIT_DISABLE_CORETEXT") == "1":
        _CORETEXT = False
        return _CORETEXT
    try:
        import CoreText  # type: ignore
        import Foundation  # type: ignore
        import Quartz  # type: ignore
    except Exception:  # pragma: no cover - host-dependent
        _CORETEXT = False
        return _CORETEXT
    _CORETEXT = (CoreText, Foundation, Quartz)
    return _CORETEXT


def compositor_available() -> bool:
    return bool(_load_coretext()) and caption_engine.available()


def engine_descriptor() -> dict[str, str]:
    ok = compositor_available()
    return {
        "name": "macos-coretext",
        "version": f"macos-{platform.mac_ver()[0] or 'unknown'}" if ok else "",
        "status": "present" if ok else "not_configured",
    }


def _font_binding(
    project_dir: Path,
    state: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the exact selected bytes used by this caption raster."""
    from render_editor_timeline import project_font_binding  # lazy: import cycle

    style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
    binding = project_font_binding(
        project_dir, state, str(style.get("font_asset_id") or "") or None,
        str(overlay.get("text") or ""),
    )
    if binding is not None:
        return binding
    from render_editor_timeline import font_path

    path = font_path()
    return {
        "asset_id": "font-legacy-system",
        "path": path,
        "sha256": font_digest(path),
        "legacy": True,
        "verified": False,
    }


def _font_file(project_dir: Path, state: dict[str, Any] | None = None, overlay: dict[str, Any] | None = None) -> Path:
    """Compatibility helper; project-aware calls retain exact binding truth."""
    if state is not None and overlay is not None:
        return Path(_font_binding(project_dir, state, overlay)["path"])
    from render_editor_timeline import font_path
    return font_path()


_FONT_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def font_digest(path: Path) -> str:
    """SHA-256 of a (possibly large) font file, cached by (path,mtime,size)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _FONT_DIGEST_CACHE.get(key)
    if cached is None:
        cached = _file_sha256(path)
        _FONT_DIGEST_CACHE.clear()
        _FONT_DIGEST_CACHE[key] = cached
    return cached


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_base_font(ct, size: float, font_file: Path):
    descriptors = ct.CTFontManagerCreateFontDescriptorsFromURL(
        __import__("Foundation").NSURL.fileURLWithPath_(str(font_file))
    )
    if not descriptors:
        raise ValueError(f"font file yields no descriptors: {font_file}")
    emoji_descriptor = ct.CTFontDescriptorCreateWithNameAndSize("Apple Color Emoji", size)
    base_descriptor = ct.CTFontDescriptorCreateCopyWithAttributes(
        descriptors[0], {ct.kCTFontCascadeListAttribute: [emoji_descriptor]}
    )
    return ct.CTFontCreateWithFontDescriptor(base_descriptor, size, None)


def _cg_color(quartz, hex_color: str, alpha: float = 1.0):
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return quartz.CGColorCreateGenericRGB(red, green, blue, alpha)


_LATIN = re.compile(r"[0-9A-Za-z@#$%&'’\-.]")
# Punctuation that must not start a line, and punctuation that must not end one.
_NO_LINE_START = "，,。.、！!？?：:；;）)」』】》〉…”’%％"
_NO_LINE_END = "（(「『【《〈“‘"
# A line holding only a couple of characters reads as a mistake, not a line.
MIN_TAIL_CHARS = 3
# Shrinking a little beats wrapping badly, but only a little. This bounds
# that "little" — the shrink a caption takes to stay off a second line —
# and is deliberately narrow; the absolute floors in docs/SPEC-phase3-
# bilingual-typography-v1.md §1 bound the different, later shrink a caption
# takes when even wrapping leaves it over its line budget.
MIN_AUTOFIT_SCALE = 0.82
AUTOFIT_STEP = 0.04

# Phase 3 SPEC v1 (docs/SPEC-phase3-bilingual-typography-v1.md) §1: explicit
# mobile type tokens. Values are for the 1080x1920 canvas baseline; callers
# scale by render_scale the same way font_size already does.
CAPTION_PRIMARY_FLOOR = 40.0
CAPTION_SECONDARY_FLOOR = 32.0
CAPTION_SECONDARY_MIN_SCALE = 0.55

# SPEC §2: explicit line-height multiples, replacing CoreText's native
# ascent+descent+leading sum. The same numbers drive both the actual raster
# (render_caption_png, below) and the caption's reported height, which is
# the only number render_editor_timeline.clamp_captions_into_safe_area ever
# reads — one set of numbers, not two.
LINE_HEIGHT_PRIMARY_SCALE = 1.25
LINE_HEIGHT_SECONDARY_SCALE = 1.20
BLOCK_GAP_SCALE = 0.35
# SPEC §4: never three lines. A caption that needs one is a caption that
# has to fail closed, not one that quietly grows.
MAX_CAPTION_LINES = 2
# SPEC §3 step 3: the character budget handed back to the translator is two
# lines of measured capacity, minus a margin. The margin is there because a
# budget is a count of characters and a line is a count of pixels: an answer
# of exactly the measured capacity is one wide glyph away from not fitting,
# and the whole point of asking again is not to have to ask a third time.
CHARACTER_BUDGET_SAFETY = 0.95


class CaptionOverflowError(ValueError):
    """A caption cannot be safely drawn at any allowed size or line count.

    Raised instead of drawing past the type tokens: a third line on either
    tier always raises. Subclasses ValueError so it is caught wherever the
    pipeline already treats a bad caption as a controlled, fail-closed
    error (see auto_edit.py's existing `except ValueError` handling around
    caption rendering) rather than an unhandled crash.
    """


def line_height_px(font_size: float, secondary: bool = False) -> float:
    """Explicit line-to-line distance for one caption tier (SPEC §2)."""
    scale = LINE_HEIGHT_SECONDARY_SCALE if secondary else LINE_HEIGHT_PRIMARY_SCALE
    return font_size * scale


SPAN_SCALE_MIN = 0.5
SPAN_SCALE_MAX = 3.0


def clamp_span_scale(raw: Any) -> float:
    """One reading of an effect span's ``font_scale``, for every caller.

    The drawn run and the line height it has to fit inside were reading the
    same field through two clamps; one number, read once.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(SPAN_SCALE_MIN, min(SPAN_SCALE_MAX, value))


def emphasis_line_scale(spans: list[dict[str, Any]] | None) -> float:
    """How much taller than the base size the spoken block is drawn (SPEC §2).

    The tallest emphasised run in the block, never below 1.0: a line height
    that follows emphasis up but is not dragged down by a span asking to be
    drawn smaller than the line it sits in. Applied to the block rather than
    per line on purpose — a caption whose two lines had different pitches
    because one of them happened to hold the keyword reads as a layout
    mistake, and the pitch is what the eye tracks between lines.
    """
    scale = 1.0
    for span in spans or []:
        if int(span.get("end_char", 0)) <= int(span.get("start_char", 0)):
            continue
        scale = max(scale, clamp_span_scale((span.get("style") or {}).get("font_scale", 1.0)))
    return scale


def block_gap_px(secondary_font_size: float) -> float:
    """Gap between the primary block and the translation below it (SPEC §2)."""
    return secondary_font_size * BLOCK_GAP_SCALE


def secondary_font_size(
    primary_font_size: float, render_scale: float = 1.0
) -> tuple[float, bool]:
    """Translation size: primary*0.62, floored at 32px (SPEC §1).

    Returns ``(size, needs_shortening)``. ``needs_shortening`` is True
    exactly when the floor overrode the 0.62 ratio: the spoken line shrank
    and the translation could not follow it down, so the two are closer in
    size than the design asks. That is SPEC §3's entry condition, which §3
    step 1 still passes when the translation wraps inside two lines — the
    caption it cannot pass fails closed in ``render_caption_png`` instead.

    ``primary_font_size`` arrives already multiplied by ``render_scale``
    (that is what the compositor works in), so the floor has to be scaled
    the same way to be the same floor. Held at an absolute 32px it stopped
    being a floor at all in a half-scale preview: the primary itself is
    26px there, so every translation came out larger than the line it
    translates and every caption claimed it needed shortening.
    """
    floor = CAPTION_SECONDARY_FLOOR * max(render_scale, 0.0)
    scaled = primary_font_size * TRANSLATION_SCALE
    if scaled < floor:
        return floor, True
    return scaled, False


# SPEC Phase 3 v1's numbers are part of what a rendered caption *is*, so a
# plan drawn under one set of them is not a plan under another. The cache
# key carries this alongside the caption text; without it, changing a line
# height left every already-rendered project serving its old rasters.
TYPOGRAPHY_REVISION = "phase3-v1"


def typography_tokens() -> dict[str, Any]:
    """The live type-token values, for the render plan's cache key."""
    return {
        "revision": TYPOGRAPHY_REVISION,
        "primary_floor": CAPTION_PRIMARY_FLOOR,
        "secondary_floor": CAPTION_SECONDARY_FLOOR,
        "secondary_min_scale": CAPTION_SECONDARY_MIN_SCALE,
        "secondary_scale": TRANSLATION_SCALE,
        "line_height_primary": LINE_HEIGHT_PRIMARY_SCALE,
        "line_height_secondary": LINE_HEIGHT_SECONDARY_SCALE,
        "block_gap": BLOCK_GAP_SCALE,
        "max_lines": MAX_CAPTION_LINES,
        "min_autofit_scale": MIN_AUTOFIT_SCALE,
        "autofit_step": AUTOFIT_STEP,
    }


def _breakable(text: str, index: int) -> bool:
    """Can a line end just before ``index``?"""
    if index <= 0 or index >= len(text):
        return False
    before, after = text[index - 1], text[index]
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return False
    if before == " ":
        return True
    # Never split a Latin word or a number down the middle; Chinese may break
    # between any two characters, which is why the framesetter alone leaves
    # words like 小門就是 cut in half.
    if _LATIN.match(before) and _LATIN.match(after):
        return False
    return True


def wrap_lines(
    text: str,
    measure,
    max_width: float,
    _seen: dict[tuple[str, float], list[str] | None] | None = None,
) -> list[str] | None:
    """Break one line into balanced lines, or None if it cannot be done well.

    Greedy filling packs the first line and leaves the remainder stranded —
    that is where the single dangling character comes from. This balances
    the lines instead, and refuses a break that would strand one.

    Every candidate break re-wraps the tail behind it, and the tails of
    different breaks overlap almost entirely, so the same tail was re-solved
    once per break above it: a caption needing three lines took seconds and
    one needing four took minutes. ``_seen`` remembers a tail's answer for
    the duration of one top-level call, where ``measure`` is fixed, so the
    text and width identify it.
    """
    seen = {} if _seen is None else _seen
    memo_key = (text, max_width)
    if memo_key in seen:
        return seen[memo_key]
    if measure(text) <= max_width:
        seen[memo_key] = [text]
        return seen[memo_key]
    positions = [index for index in range(1, len(text)) if _breakable(text, index)]
    if not positions:
        seen[memo_key] = None
        return None

    best: tuple[float, list[str]] | None = None
    for split in positions:
        head, tail = text[:split].rstrip(), text[split:].lstrip()
        if not head or not tail:
            continue
        if len(head) < MIN_TAIL_CHARS or len(tail) < MIN_TAIL_CHARS:
            continue
        head_width, tail_width = measure(head), measure(tail)
        if head_width > max_width:
            continue
        if tail_width > max_width:
            rest = wrap_lines(tail, measure, max_width, seen)
            if rest is None:
                continue
            score = abs(head_width - max_width) + 1000 * len(rest)
            candidate = [head, *rest]
        else:
            score = abs(head_width - tail_width)
            candidate = [head, tail]
        if best is None or score < best[0]:
            best = (score, candidate)
    seen[memo_key] = best[1] if best else None
    return seen[memo_key]


def fit_caption_text(
    text: str,
    measure_at,
    font_size: float,
    max_width: float,
    floor_px: float | None = None,
    max_lines: int | None = None,
):
    """(lines, font size), in SPEC Phase 3 v1 §3's order.

    Step 1 — shrink a little before accepting a second line, then wrap at
    the caption's own size. A caption that drops one point to stay on one
    line reads better than one that keeps its size and wraps with two
    characters left over; a caption that would have to drop a fifth of its
    size does not, it wraps, which is what §3 step 1 says to do.

    Step 2 — only a caption that still needs more than ``max_lines`` after
    wrapping shrinks past that: it autofits toward ``floor_px``, re-wrapping
    at each size, and stops at the first size that fits in the line budget.
    ``floor_px`` is an absolute lower bound in the same units as
    ``font_size`` (SPEC §1's 40px primary token, scaled by the caller like
    ``font_size`` already is) and, per §1, is where a caption fails closed
    rather than shrinking again — so it bounds this step rather than
    widening the shrink-instead-of-wrapping band above, which shrank an
    ordinary two-line caption by 18% to keep it on one line.

    Both bounds are needed together: given neither, this is the plain
    legacy autofit.
    """
    paragraphs = text.split("\n")

    def wrapped_at(size: float) -> list[str]:
        measure = measure_at(size)
        lines: list[str] = []
        for part in paragraphs:
            broken = wrap_lines(part, measure, max_width)
            # Nothing splits well: leave it to the framesetter rather than
            # forcing a break somewhere worse.
            lines.extend(broken if broken is not None else [part])
        return lines

    size = font_size
    while size >= font_size * MIN_AUTOFIT_SCALE:
        measure = measure_at(size)
        if all(measure(part) <= max_width for part in paragraphs):
            return paragraphs, size
        size *= 1.0 - AUTOFIT_STEP

    wrapped = wrapped_at(font_size)
    if max_lines is None or floor_px is None or len(wrapped) <= max_lines:
        return wrapped, font_size

    size = font_size
    while size * (1.0 - AUTOFIT_STEP) >= floor_px:
        size *= 1.0 - AUTOFIT_STEP
        candidate = wrapped_at(size)
        if len(candidate) <= max_lines:
            return candidate, size
    # Out of room: hand back the base-size wrap so the caller can fail
    # closed against the line budget it set.
    return wrapped, font_size


def measured_line_capacity(text: str, measure, max_width: float) -> int:
    """How many characters of ``text`` one line actually holds.

    Measured, not counted. A budget derived from a rule of thumb ("roughly
    forty Latin characters") is a different ruler from the one the frame is
    cut with, and a translation cut to the wrong ruler comes back still too
    long — so the capacity is the longest prefix of this very text that this
    very ``measure`` says fits, at the size the raster will use.

    Prefix width grows with prefix length, so the answer is bisected rather
    than walked: a 4,000-character translation is 12 measurements, not 4,000.
    """
    if not text:
        return 0
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle]) <= max_width:
            low = middle
        else:
            high = middle - 1
    return low


def character_budget(
    text: str,
    measure,
    max_width: float,
    *,
    max_lines: int = MAX_CAPTION_LINES,
    safety: float = CHARACTER_BUDGET_SAFETY,
) -> int:
    """SPEC §3 step 3: max_lines x measured line capacity x the safety margin."""
    capacity = measured_line_capacity(text, measure, max_width)
    return max(1, int(max_lines * capacity * safety))


def _character_map(original: str, wrapped: str) -> list[int]:
    """Where each character of `original` ended up in `wrapped`, or -1.

    Wrapping inserts newlines and can drop the space it broke on, so the two
    strings are walked together rather than assuming a fixed shift.
    """
    mapping: list[int] = []
    cursor = 0
    for character in original:
        while (
            cursor < len(wrapped)
            and wrapped[cursor] == "\n"
            and character != "\n"
        ):
            cursor += 1
        if cursor < len(wrapped) and wrapped[cursor] == character:
            mapping.append(cursor)
            cursor += 1
        else:
            # Dropped at a break: the space a line was split on.
            mapping.append(-1)
    return mapping


def rewrapped_effect_spans(
    spans: list[dict[str, Any]] | None, original: str, wrapped: str
) -> list[dict[str, Any]]:
    """The same emphasis, addressed to the text as it is actually laid out.

    Breaks are decided here, after the spans were measured against the
    unbroken line, so every character after a break sits one place further
    along than the span says. Drawn as given, a keyword highlight slides left
    by one character per break — seen on a real caption as 「叫做真正的主詞」
    lighting up 「做真正的主」.
    """
    if not spans or original == wrapped:
        return list(spans or [])
    mapping = _character_map(original, wrapped)
    if len(mapping) != len(original):
        return list(spans)
    moved: list[dict[str, Any]] = []
    for span in spans:
        try:
            start = caption_engine.codepoint_index_for_utf16(
                original, int(span.get("start_char", 0))
            )
            end = caption_engine.codepoint_index_for_utf16(
                original, int(span.get("end_char", 0))
            )
        except ValueError:
            continue
        kept = [mapping[index] for index in range(start, min(end, len(mapping)))
                if 0 <= index < len(mapping) and mapping[index] >= 0]
        if not kept:
            # Every character of it was dropped at a break. Emphasis pointing
            # at nothing is dropped too, rather than pointed somewhere else.
            continue
        moved.append(
            dict(
                span,
                start_char=caption_engine.utf16_length(wrapped[: kept[0]]),
                end_char=caption_engine.utf16_length(wrapped[: kept[-1] + 1]),
            )
        )
    return moved


def _measure_context(
    project_dir: Path,
    overlay: dict[str, Any],
    canvas: dict[str, Any],
    render_scale: float,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ruler for one caption, built once and shared.

    SPEC Phase 3 v1 §2 is explicit that the numbers deciding a break must be
    the numbers that draw it. Two callers need these measurements — the
    raster below, and the character budget in `translation_fit` — so they
    are derived here rather than twice, because a budget measured with a
    slightly different font, width or scale than the raster is a budget that
    silently permits a translation the frame then rejects.
    """
    modules = _load_coretext()
    if not modules:
        raise RuntimeError("caption compositor is not available")
    ct, foundation, quartz = modules
    pack, pack_source = selected_pack(state or {}, overlay)
    style, style_sources = resolve_caption_style(overlay, pack, pack_source)
    text = str(overlay.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("caption text is empty")
    # A translation rides under the spoken line, in the same frame so the
    # two share one set of line-breaking rules. Effect spans index the
    # spoken text, so it has to stay at the front and keep its offsets.
    translation = str(overlay.get("translation") or "").strip()
    canvas_width = int(canvas.get("width", 1080))
    font_size = max(14.0, float(style.get("font_size", 52)) * render_scale)
    max_width = max(20.0, min(96.0, float(style.get("max_width", 84))))
    frame_width = canvas_width * render_scale * (max_width / 100.0)
    font_binding = _font_binding(project_dir, state, overlay)
    font_file = Path(font_binding["path"])

    def measure_at(size: float):
        probe_font = _make_base_font(ct, size, font_file)

        def measure(value: str) -> float:
            if not value:
                return 0.0
            probe = foundation.NSMutableAttributedString.alloc().initWithString_(value)
            probe.addAttributes_range_(
                {ct.kCTFontAttributeName: probe_font},
                foundation.NSMakeRange(0, probe.length()),
            )
            line = ct.CTLineCreateWithAttributedString(probe)
            width, _a, _d, _l = ct.CTLineGetTypographicBounds(line, None, None, None)
            return float(width)

        return measure

    # Emphasised words are drawn larger than the rest, so measuring at the
    # base size reports a line narrower than it will be and the framesetter
    # breaks it again — putting back the stranded character these breaks
    # exist to avoid. Budget for the widest span in the caption.
    emphasis_scale = 1.0
    for span in overlay.get("effect_spans") or []:
        try:
            emphasis_scale = max(
                emphasis_scale,
                float((span.get("style") or {}).get("font_scale", 1.0)),
            )
        except (TypeError, ValueError):
            continue
    return {
        "modules": modules,
        "ct": ct,
        "foundation": foundation,
        "quartz": quartz,
        "pack": pack,
        "pack_source": pack_source,
        "style": style,
        "style_sources": style_sources,
        "text": text,
        "translation": translation,
        "canvas_width": canvas_width,
        "font_size": font_size,
        "max_width": max_width,
        "frame_width": frame_width,
        "font_binding": font_binding,
        "font_file": font_file,
        "measure_at": measure_at,
        "wrap_width": frame_width / max(1.0, emphasis_scale),
    }


def _wrap_translation(
    translation: str,
    measure_at,
    font_size: float,
    wrap_width: float,
    render_scale: float,
) -> tuple[list[str], float, bool]:
    """(lines, secondary size, floor-overrode-ratio) for the second line."""
    secondary_size, needs_shortening = secondary_font_size(font_size, render_scale)
    secondary_measure = measure_at(secondary_size)
    if secondary_measure(translation) <= wrap_width:
        lines = [translation]
    else:
        lines = wrap_lines(translation, secondary_measure, wrap_width) or [translation]
    return lines, secondary_size, needs_shortening


def translation_fit(
    project_dir: Path,
    overlay: dict[str, Any],
    canvas: dict[str, Any],
    render_scale: float = 1.0,
    state: dict[str, Any] | None = None,
    *,
    translation: str | None = None,
) -> dict[str, Any]:
    """Ask, before anything is drawn, whether this translation will fit.

    SPEC Phase 3 v1 §3 step 3. `render_caption_png` answers the same question
    by raising, which is the right answer at render time and a useless one at
    translation time — by then the provider has been paid and the artifact
    hashed. This answers it as data instead, and when the answer is no it
    carries the measured character budget to ask the provider again with.

    `reason` is `"primary"` when it is the spoken line that overflows: no
    amount of shortening the translation fixes that, so there is no budget
    and no retry, only the fail-closed at render.
    """
    context = _measure_context(project_dir, overlay, canvas, render_scale, state)
    value = context["translation"] if translation is None else translation.strip()
    empty = {
        "fits": True,
        "reason": None,
        "character_budget": None,
        "line_count": 0,
        "secondary_font_size": None,
    }
    if not value:
        return empty
    wrap_width = context["wrap_width"]
    spoken_lines, font_size = fit_caption_text(
        context["text"], context["measure_at"], context["font_size"], wrap_width,
        floor_px=CAPTION_PRIMARY_FLOOR * render_scale, max_lines=MAX_CAPTION_LINES,
    )
    if len(spoken_lines) > MAX_CAPTION_LINES:
        return {**empty, "fits": False, "reason": "primary"}
    lines, secondary_size, _needs_shortening = _wrap_translation(
        value, context["measure_at"], font_size, wrap_width, render_scale
    )
    secondary_measure = context["measure_at"](secondary_size)
    # Counting lines is not enough. A run with nothing to break on comes back
    # as one line — one line wider than the frame, which no wrap and no shrink
    # to the floor can save, and which the safe-area check discards a whole
    # render later. Two lines "holding" a translation is a fact about pixels.
    fits_the_frame = all(
        secondary_measure(line) <= wrap_width for line in lines
    )
    if len(lines) <= MAX_CAPTION_LINES and fits_the_frame:
        return {
            "fits": True,
            "reason": None,
            "character_budget": None,
            "line_count": len(lines),
            "secondary_font_size": secondary_size,
        }
    # A budget of two lines' capacity is only honest if the text can be made
    # to occupy two lines. A run with no break point cannot: telling a
    # translator it has ninety-five characters when only forty-seven of them
    # will ever be on screen buys back the same answer and burns the one
    # retry §3 allows.
    usable_lines = (
        MAX_CAPTION_LINES
        if wrap_lines(value, secondary_measure, wrap_width) is not None
        else 1
    )
    return {
        "fits": False,
        "reason": "secondary",
        "character_budget": character_budget(
            value, secondary_measure, wrap_width, max_lines=usable_lines
        ),
        "line_count": len(lines),
        "secondary_font_size": secondary_size,
    }


def render_caption_png(
    project_dir: Path,
    overlay: dict[str, Any],
    canvas: dict[str, Any],
    render_scale: float,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape + rasterise one caption overlay; returns the plan item dict."""
    context = _measure_context(project_dir, overlay, canvas, render_scale, state)
    ct, foundation, quartz = context["modules"]
    style, style_sources = context["style"], context["style_sources"]
    text = context["text"]
    translation = context["translation"]
    font_size = context["font_size"]
    frame_width = context["frame_width"]
    font_binding = context["font_binding"]
    font_file = context["font_file"]
    measure_at = context["measure_at"]
    wrap_width = context["wrap_width"]

    # Decide the breaks here rather than letting the framesetter fill greedily:
    # it packs the first line and strands whatever is left, which is how a
    # caption ends up with one character on a line of its own. A caption
    # that needs more than two lines at its own size autofits toward the
    # absolute 40px floor (SPEC Phase 3 v1 §1, §3 step 2) and never renders
    # smaller than that: a third line at the floor fails closed rather than
    # the caption growing one.
    primary_floor_px = CAPTION_PRIMARY_FLOOR * render_scale
    unwrapped = text
    spoken_lines, font_size = fit_caption_text(
        text, measure_at, font_size, wrap_width,
        floor_px=primary_floor_px, max_lines=MAX_CAPTION_LINES,
    )
    if len(spoken_lines) > MAX_CAPTION_LINES:
        raise CaptionOverflowError(
            f"caption {overlay.get('id')!r} needs {len(spoken_lines)} lines "
            f"even at its {primary_floor_px:.1f}px floor — SPEC Phase 3 v1 "
            "§4 caps a caption at two lines; this one must fail closed "
            "rather than render a third"
        )
    text = "\n".join(spoken_lines)
    spoken_text = text
    # The breaks moved the text, so the emphasis has to move with it.
    drawn_spans = rewrapped_effect_spans(
        overlay.get("effect_spans"), unwrapped, text
    )

    # The translation's own size is derived from the primary's, not shared
    # with it, so it is wrapped and floored separately (SPEC §1): secondary
    # = primary*0.62, floored at 32px, and the floor always wins the
    # conflict rather than being shrunk past.
    translation_range: tuple[int, int] | None = None
    secondary_size = font_size
    needs_shortening = False
    primary_line_count = len(spoken_lines)
    secondary_line_count = 0
    if translation:
        translation_lines, secondary_size, needs_shortening = _wrap_translation(
            translation, measure_at, font_size, wrap_width, render_scale
        )
        secondary_line_count = len(translation_lines)
        if secondary_line_count > MAX_CAPTION_LINES:
            raise CaptionOverflowError(
                f"translation for caption {overlay.get('id')!r} must be "
                f"shortened: it needs {secondary_line_count} lines even at "
                f"its {secondary_size:.1f}px floor size, and SPEC Phase 3 v1 "
                "§4 caps a caption at two — this is where §3's flow runs "
                "out of steps, so it fails closed rather than rendering a "
                "third line or quietly dropping words"
            )
        translation_text = "\n".join(translation_lines)
        translation_range = (len(text) + 1, len(text) + 1 + len(translation_text))
        text = f"{text}\n{translation_text}"
    base_font = _make_base_font(ct, font_size, font_file)

    stroke_width = float(style.get("stroke_width", 3)) * render_scale
    stroke_color = _cg_color(quartz, str(style.get("stroke_color") or "#17130F"))
    fill_color = _cg_color(quartz, str(style.get("color") or "#F7F2E8"))

    max_span_scale = 1.0

    # SPEC Phase 3 v1 §2: explicit line-height multiples, not CoreText's
    # native ascent+descent+leading.
    #
    # Pinning minimum and maximum line height alone is not enough, and looks
    # like it is: it clamps ascent+descent to the asked-for number, but the
    # font's own leading is still added on top of that when the framesetter
    # advances to the next baseline. Hiragino's leading is 26px, so a
    # caption asked for 1.25x52=65px was drawn at 91px pitch while every
    # number reported said 65. Clamping line *spacing* to zero as well is
    # what actually removes it, and then baseline-to-baseline is exactly
    # font_size * multiple — measured back out of the drawn frame below.
    def _fixed_line_height_style(px: float, spacing_before: float = 0.0):
        settings = {
            "MinimumLineHeight": px,
            "MaximumLineHeight": px,
            "MinimumLineSpacing": 0.0,
            "MaximumLineSpacing": 0.0,
            "LineSpacingAdjustment": 0.0,
        }
        if spacing_before:
            settings["ParagraphSpacingBefore"] = spacing_before
        packed = [
            (
                getattr(ct, "kCTParagraphStyleSpecifier" + name),
                8,
                struct.pack("d", float(value)),
            )
            for name, value in settings.items()
        ]
        return ct.CTParagraphStyleCreate(packed, len(packed))

    # ...and a clamped line height is a ceiling as much as a floor. Pinned at
    # 1.25x the *base* size while an emphasised run is drawn larger than the
    # base, CoreText answers by compressing the line box down to the pin: the
    # pipeline's own default emphasis (font_scale 1.18) drew a 61px pitch
    # where the tokens said 65, and at 1.8 the pitch fell to 49px — under the
    # caption's own 52px type size, i.e. two rows of CJK faces touching. So
    # the pin follows the tallest run the line actually contains, and the
    # base size is its floor: emphasis opens a line up, never squashes it.
    primary_scale = emphasis_line_scale(drawn_spans)
    target_primary_pitch = line_height_px(font_size * primary_scale)
    primary_paragraph_style = _fixed_line_height_style(target_primary_pitch)
    # The tiers' own numbers, kept out here because the baselines below are
    # laid out by hand and need them whether or not a translation exists.
    secondary_line_height = line_height_px(secondary_size, secondary=True)
    translation_block_gap = block_gap_px(secondary_size)
    translation_style = None
    if translation_range is not None:
        # One clamped height for every translation line; the gap between the
        # blocks is a baseline decision, made once, below — not paragraph
        # spacing that a translation wrapping onto its own second line would
        # then collect a second time.
        translation_style = _fixed_line_height_style(secondary_line_height)

    def build_attributed(pass_kind: str):
        """One attributed string per drawing pass.

        CoreText centres a stroke on the glyph outline, so a stroke wide
        enough to read against video eats inward until it swallows the fill
        of thin CJK strokes entirely.  Draw the outline first at double
        width, then lay the fill over its inner half: what survives is a
        true outside-only outline.
        """
        built = foundation.NSMutableAttributedString.alloc().initWithString_(text)
        built_range = foundation.NSMakeRange(0, built.length())
        attrs = {ct.kCTFontAttributeName: base_font}
        if pass_kind == "stroke":
            # Positive stroke width = stroke only, no fill.
            attrs[ct.kCTStrokeWidthAttributeName] = (
                stroke_width * 2.0 / font_size * 100.0
            )
            attrs[ct.kCTStrokeColorAttributeName] = stroke_color
            attrs[ct.kCTForegroundColorAttributeName] = stroke_color
        else:
            attrs[ct.kCTForegroundColorAttributeName] = fill_color
        built.addAttributes_range_(attrs, built_range)
        # Explicit line height for the spoken tier (SPEC §2), applied last
        # among the base attributes so it is not overwritten by them.
        built.addAttributes_range_(
            {ct.kCTParagraphStyleAttributeName: primary_paragraph_style}, built_range
        )

        if translation_range is not None:
            # Smaller and quieter: it supports the spoken line rather than
            # competing with it. It keeps the same outline, because it sits
            # on the same moving picture and needs the same legibility.
            # Its size (and floor) were decided once, above, alongside its
            # own line wrapping — not re-derived here.
            start, end = translation_range
            translation_font = ct.CTFontCreateCopyWithAttributes(
                base_font, secondary_size, None, None
            )
            sub = {ct.kCTFontAttributeName: translation_font}
            if pass_kind == "fill":
                sub[ct.kCTForegroundColorAttributeName] = _cg_color(
                    quartz, str(style.get("translation_color") or "#DCD6CA")
                )
            else:
                # Stroke width is a share of each run's own size, so the same
                # share on a smaller line reads as equally bold — two
                # headlines instead of a line and its support.
                sub[ct.kCTStrokeWidthAttributeName] = (
                    stroke_width * 2.0 * TRANSLATION_STROKE
                    / secondary_size * 100.0
                )
            built.addAttributes_range_(sub, foundation.NSMakeRange(start, end - start))
            if translation_style is not None:
                built.addAttributes_range_(
                    {ct.kCTParagraphStyleAttributeName: translation_style},
                    foundation.NSMakeRange(start, end - start),
                )

        nonlocal max_span_scale
        for span in drawn_spans:
            span_style = span.get("style") or {}
            start = int(span.get("start_char", 0))
            end = int(span.get("end_char", 0))
            if end <= start or end > built.length():
                continue
            span_range = foundation.NSMakeRange(start, end - start)
            scale = clamp_span_scale(span_style.get("font_scale", 1.0))
            max_span_scale = max(max_span_scale, scale)
            effect_kind = str(span_style.get("effect") or "pop")
            span_attrs: dict[Any, Any] = {}
            if pass_kind == "fill" and effect_kind != "highlight":
                # Highlight = backdrop marker: keep the base text colour and
                # paint a translucent pill behind the span (drawn below).
                span_attrs[ct.kCTForegroundColorAttributeName] = _cg_color(
                    quartz, str(span_style.get("color") or "#FF5533")
                )
            if scale != 1.0:
                span_attrs[ct.kCTFontAttributeName] = ct.CTFontCreateCopyWithAttributes(
                    base_font, font_size * scale, None, None
                )
            if pass_kind == "fill" and span_style.get("effect") == "underline":
                span_attrs[ct.kCTUnderlineStyleAttributeName] = ct.kCTUnderlineStyleSingle
            if span_attrs:
                built.addAttributes_range_(span_attrs, span_range)
        return built

    # Layout is identical across passes: stroke width does not change glyph
    # advances, so the fill lands exactly on top of its own outline.
    attributed = build_attributed("fill")
    framesetter = ct.CTFramesetterCreateWithAttributedString(attributed)
    constraint = quartz.CGSizeMake(frame_width, 100000.0)
    fitted, _ = ct.CTFramesetterSuggestFrameSizeWithConstraints(
        framesetter, foundation.NSMakeRange(0, 0), None, constraint, None
    )
    padding = int(max(8.0, stroke_width * 2.0, font_size * (max_span_scale - 1.0) + 4.0))
    path = quartz.CGPathCreateMutable()
    quartz.CGPathAddRect(
        path, None,
        quartz.CGRectMake(padding, padding, fitted.width, fitted.height),
    )
    frame = ct.CTFramesetterCreateFrame(framesetter, foundation.NSMakeRange(0, 0), path, None)

    # The framesetter decides where the lines *break*; where they sit is
    # decided here, line by line.
    #
    # Left to the framesetter, baseline-to-baseline is one line's descent
    # plus the next line's ascent, and a clamped line height splits itself
    # between the two in whatever proportion each line's own metrics ask
    # for. That proportion changes the moment one line holds an emphasised
    # run and the other does not — so the drawn pitch came out short when
    # the keyword was on line 1 and long when it was on line 2 (1.8x
    # emphasis: 101px and 132px against the same 117px pin). Topping the
    # shortfall up with paragraph spacing could only ever push lines apart,
    # so the long case had no way back and the pitch quietly depended on
    # which line the emphasis happened to land on — the exact thing
    # emphasis_line_scale() pins the whole block to avoid.
    #
    # So the baselines are placed, not read: the first sits one ascent below
    # the top of the ink, and each one after it a fixed step below its
    # predecessor — the primary pitch inside the spoken block, the secondary
    # pitch inside the translation, and descent + gap + ascent across the
    # boundary between them (which is exactly how the gap is measured back
    # out further down). Nothing about the step depends on which line the
    # emphasis is on.
    lines = ct.CTFrameGetLines(frame)
    frame_origins = ct.CTFrameGetLineOrigins(
        frame, foundation.NSMakeRange(0, len(lines)), None
    )
    line_metrics: list[dict[str, Any]] = []
    for line, origin in zip(lines, frame_origins):
        _w, line_ascent, line_descent, _leading = ct.CTLineGetTypographicBounds(
            line, None, None, None
        )
        location = float(ct.CTLineGetStringRange(line).location)
        line_metrics.append({
            "x": float(origin.x),
            "ascent": float(line_ascent),
            "descent": float(line_descent),
            "location": location,
            "translation": (
                translation_range is not None and location >= translation_range[0]
            ),
        })

    drops: list[float] = []
    drop = 0.0
    for index, current in enumerate(line_metrics):
        if index:
            previous = line_metrics[index - 1]
            if current["translation"] and not previous["translation"]:
                drop += previous["descent"] + translation_block_gap + current["ascent"]
            elif current["translation"]:
                drop += secondary_line_height
            else:
                drop += target_primary_pitch
        drops.append(drop)
    if line_metrics:
        first_ascent = line_metrics[0]["ascent"]
        ink_height = first_ascent + drops[-1] + line_metrics[-1]["descent"]
    else:
        first_ascent = 0.0
        ink_height = float(fitted.height)

    width = int(fitted.width) + padding * 2
    height = int(math.ceil(ink_height)) + padding * 2
    width += width % 2
    height += height % 2
    # Context coordinates: y counts up from the bottom of the raster, and the
    # top of the ink is one padding down from the top.
    baselines = [height - padding - first_ascent - value for value in drops]

    color_space = quartz.CGColorSpaceCreateDeviceRGB()
    context = quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, color_space,
        quartz.kCGImageAlphaPremultipliedLast,
    )
    if context is None:
        raise RuntimeError("could not create bitmap context")
    if bool(style.get("box")):
        quartz.CGContextSetFillColorWithColor(
            context, _cg_color(quartz, str(style.get("box_color") or "#201B17"), 0.82)
        )
        quartz.CGContextFillRect(context, quartz.CGRectMake(0, 0, width, height))

    highlight_spans = [
        (int(span.get("start_char", 0)), int(span.get("end_char", 0)), span.get("style") or {})
        for span in drawn_spans
        if (span.get("style") or {}).get("effect") == "highlight"
        and int(span.get("end_char", 0)) > int(span.get("start_char", 0))
    ]
    if highlight_spans:
        for line, metrics, baseline in zip(lines, line_metrics, baselines):
            line_range = ct.CTLineGetStringRange(line)
            ascent, descent = metrics["ascent"], metrics["descent"]
            for span_start, span_end, span_style in highlight_spans:
                clipped_start = max(span_start, line_range.location)
                clipped_end = min(span_end, line_range.location + line_range.length)
                if clipped_end <= clipped_start:
                    continue

                def line_offset(index: int) -> float:
                    result = ct.CTLineGetOffsetForStringIndex(line, index, None)
                    return float(result[0] if isinstance(result, tuple) else result)

                x_start = line_offset(clipped_start)
                x_end = line_offset(clipped_end)
                quartz.CGContextSetFillColorWithColor(
                    context,
                    _cg_color(quartz, str(span_style.get("color") or "#F5A623"), 0.45),
                )
                quartz.CGContextFillRect(
                    context,
                    quartz.CGRectMake(
                        padding + metrics["x"] + min(x_start, x_end),
                        baseline - descent - 2,
                        abs(x_end - x_start),
                        ascent + descent + 4,
                    ),
                )

    def draw_at_baselines(drawn_frame) -> None:
        """Draw one pass's lines at the baselines decided above.

        Not CTFrameDraw: that would put the lines back where the framesetter
        wanted them, which is the whole point of placing them by hand. The
        stroke pass breaks identically — stroke width does not change glyph
        advances — so it takes the same baselines.
        """
        for line, metrics, baseline in zip(
            ct.CTFrameGetLines(drawn_frame), line_metrics, baselines
        ):
            quartz.CGContextSetTextPosition(context, padding + metrics["x"], baseline)
            ct.CTLineDraw(line, context)

    if stroke_width > 0:
        stroke_frame = ct.CTFramesetterCreateFrame(
            ct.CTFramesetterCreateWithAttributedString(build_attributed("stroke")),
            foundation.NSMakeRange(0, 0),
            path,
            None,
        )
        draw_at_baselines(stroke_frame)
    draw_at_baselines(frame)

    # Glyph-run font accounting (plan v2 B2): map every run back to a
    # declared asset or flag it.
    project_ps_names = set()
    ps_name = ct.CTFontCopyPostScriptName(base_font)
    if ps_name:
        project_ps_names.add(str(ps_name))
    clusters = caption_engine.boundary_map(text)

    def cluster_index_for(utf16_offset: int) -> int:
        for index, (cluster_start, cluster_end) in enumerate(clusters):
            if cluster_start <= utf16_offset < cluster_end:
                return index
        return max(0, len(clusters) - 1)

    glyph_runs: list[dict[str, Any]] = []
    disallowed_fallbacks: list[str] = []
    # Measured, not declared. The type tokens above say what the layout was
    # asked for; these numbers are the positions the lines were actually
    # drawn at, read back off the same baselines the drawing used. A formula
    # standing in for the measurement is a second answer, and when it
    # drifted (37-52px, from leading the framesetter added and the formula
    # did not know about) the renderer's y-shift moved the spoken line off
    # its declared position.
    laid_out: list[dict[str, float]] = [
        {
            "location": metrics["location"],
            "baseline": baseline,
            "ascent": metrics["ascent"],
            "descent": metrics["descent"],
        }
        for metrics, baseline in zip(line_metrics, baselines)
    ]
    spoken_laid_out = [
        line for line in laid_out
        if translation_range is None or line["location"] < translation_range[0]
    ]
    translation_laid_out = [
        line for line in laid_out
        if translation_range is not None and line["location"] >= translation_range[0]
    ]

    def _pitch(block: list[dict[str, float]]) -> float | None:
        """Baseline-to-baseline distance inside one block, as drawn."""
        if len(block) < 2:
            return None
        steps = [block[i]["baseline"] - block[i + 1]["baseline"] for i in range(len(block) - 1)]
        return round(sum(steps) / len(steps), 3)

    measured_block_gap: float | None = None
    if spoken_laid_out and translation_laid_out:
        # The framesetter advances by descent + spacing-before + ascent
        # across the block boundary, so the gap is what is left after the
        # two lines' own metrics are taken out.
        last_spoken = spoken_laid_out[-1]
        first_translation = translation_laid_out[0]
        measured_block_gap = round(
            (last_spoken["baseline"] - first_translation["baseline"])
            - last_spoken["descent"]
            - first_translation["ascent"],
            3,
        )
    # How much of the raster the spoken line occupies. The overlay is placed
    # by the spoken line: a translation makes the block taller, and centring
    # the whole block pushed the spoken line up into the picture. So this is
    # the whole height the translation added — its own lines *and* the gap
    # above them — measured from the bottom of the spoken block down.
    translation_height = 0.0
    if spoken_laid_out and translation_laid_out:
        translation_height = round(
            (spoken_laid_out[-1]["baseline"] - spoken_laid_out[-1]["descent"])
            - (translation_laid_out[-1]["baseline"] - translation_laid_out[-1]["descent"]),
            3,
        )
    spoken_height = max(1, int(round(height - translation_height)))
    for line in lines:
        for run in ct.CTLineGetGlyphRuns(line):
            attributes = ct.CTRunGetAttributes(run)
            run_font = attributes.get(ct.kCTFontAttributeName)
            run_ps = str(ct.CTFontCopyPostScriptName(run_font)) if run_font else "unknown"
            run_range = ct.CTRunGetStringRange(run)
            if run_ps in project_ps_names:
                asset_id = str(font_binding["asset_id"])
            elif run_ps in SANCTIONED_FALLBACK_PS_NAMES:
                asset_id = EMOJI_FONT_ASSET_ID
            else:
                asset_id = f"font-unsanctioned-{run_ps}"
                disallowed_fallbacks.append(run_ps)
            glyph_runs.append(
                {
                    "font_asset_id": asset_id,
                    "cluster_start": cluster_index_for(run_range.location),
                    "cluster_end": cluster_index_for(
                        max(run_range.location, run_range.location + run_range.length - 1)
                    )
                    + 1,
                }
            )

    image = quartz.CGBitmapContextCreateImage(context)
    captions_dir = project_dir / CAPTIONS_REL
    captions_dir.mkdir(parents=True, exist_ok=True)
    scratch = captions_dir / f".rendering-{overlay.get('id')}.png"
    url = foundation.NSURL.fileURLWithPath_(str(scratch))
    destination = quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if destination is None:
        raise RuntimeError("could not create PNG destination")
    quartz.CGImageDestinationAddImage(destination, image, None)
    if not quartz.CGImageDestinationFinalize(destination):
        raise RuntimeError("PNG finalize failed")
    artifact_hash = _file_sha256(scratch)
    final_path = captions_dir / f"{overlay.get('id')}-{artifact_hash[:16]}.png"
    scratch.replace(final_path)

    return {
        "caption_item_id": str(overlay.get("id")),
        "style_sources": style_sources,
        "clusters": clusters,
        "glyph_runs": glyph_runs,
        "artifact": {
            "rgba_path": final_path.relative_to(project_dir).as_posix(),
            "artifact_hash": artifact_hash,
            "width": width,
            "height": height,
            # The ink is narrower than the raster by this much on each side.
            # Without it, a check on where the caption sits measures the
            # transparent margin and reports text that is comfortably inside
            # a boundary as crossing it.
            "padding": padding,
            # The spoken line's share of the height; the rest is translation.
            # The renderer anchors the spoken line and lets the translation
            # hang below, instead of centring the block over the picture.
            "spoken_height": spoken_height,
        },
        # SPEC Phase 3 v1 §1: the actual sizes and line counts this item was
        # drawn at, so a caller can tell a caption that used the plain 0.62
        # ratio from one the 32px floor overrode without re-deriving it.
        "typography": {
            "primary_font_size": round(font_size, 3),
            "primary_line_count": primary_line_count,
            # No translation means no secondary tier. Reporting the primary's
            # own size here read as a full-size translation that is not
            # there; null is the honest answer to a question with no subject.
            "secondary_font_size": (
                round(secondary_size, 3) if translation_range is not None else None
            ),
            "secondary_line_count": secondary_line_count,
            "needs_shortening": needs_shortening,
            # The relationship actually drawn. `needs_shortening` says the
            # floor won; this says by how much, which is what tells a
            # caption that shrank a point from a translation grown out of
            # proportion to the line it supports.
            "secondary_ratio": (
                round(secondary_size / font_size, 4)
                if translation_range is not None and font_size > 0
                else None
            ),
            # What the frame actually did, read back off it — the evidence
            # that the tokens above reached the pixels.
            "measured": {
                "primary_line_pitch": _pitch(spoken_laid_out),
                "secondary_line_pitch": _pitch(translation_laid_out),
                "block_gap": measured_block_gap,
                "translation_height": translation_height,
            },
        },
        "x_padding": padding,
        "x_disallowed_fallbacks": sorted(set(disallowed_fallbacks)),
        "x_font_binding": font_binding,
    }


def caption_overlays(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        overlay
        for overlay in state.get("overlays", [])
        if isinstance(overlay, dict)
        and overlay.get("type") in {"caption", "emphasis"}
        and overlay.get("visible", True)
        and not overlay.get("design_role")
        and str(overlay.get("text") or "").strip()
    ]


def caption_content_revision(
    state: dict[str, Any], canvas: dict[str, Any], render_scale: float, project_dir: Path | None = None,
) -> str:
    font_identity: dict[str, Any] = {}
    if project_dir is not None:
        for overlay in caption_overlays(state):
            try:
                binding = _font_binding(project_dir, state, overlay)
                font_identity[str(overlay.get("id"))] = {
                    "asset_id": binding.get("asset_id"), "sha256": binding.get("sha256"),
                }
            except (OSError, ValueError):
                font_identity[str(overlay.get("id"))] = "unresolved"
    else:
        try:
            font_identity["legacy"] = font_digest(_font_file(None))
        except (OSError, ValueError):
            font_identity["legacy"] = "unresolved"
    pack_identity: dict[str, Any] = {}
    for overlay in caption_overlays(state):
        try:
            pack, pack_source = selected_pack(state, overlay)
        except ValueError:
            pack, pack_source = None, "invalid"
        pack_identity[str(overlay.get("id"))] = {
            "source": pack_source,
            "defaults": pack_caption_defaults(pack) if pack else {},
        }
    payload = {
        "engine": engine_descriptor(),
        # The type tokens are part of the raster, so they are part of the
        # key. Keyed on content alone, a project rendered under one set of
        # line heights kept serving those rasters after the numbers changed
        # — and kept serving plans written before the typography block
        # existed at all.
        "typography": typography_tokens(),
        "font_sha256": font_identity,
        "style_packs": pack_identity,
        "canvas": {"width": canvas.get("width"), "height": canvas.get("height")},
        "render_scale": round(float(render_scale), 4),
        "items": [
            {
                "id": overlay.get("id"),
                "text": overlay.get("text"),
                "style": overlay.get("style"),
                "effect_spans": overlay.get("effect_spans"),
                # Part of the raster, so part of the key. Left out, adding a
                # translation changed nothing in the hash and the cached
                # single-line rasters were served as if nothing had happened.
                "translation": overlay.get("translation"),
            }
            for overlay in caption_overlays(state)
        ],
    }
    return contract_registry.canonical_hash(payload)


def build_render_plan(
    project_dir: Path,
    state: dict[str, Any],
    render_scale: float = 1.0,
) -> dict[str, Any]:
    """Render every caption overlay (cache-aware) and write the plan artifact."""
    if not compositor_available():
        raise RuntimeError("caption compositor is not available on this host")
    canvas = state.get("canvas") or {}
    content_revision = caption_content_revision(state, canvas, render_scale, project_dir)
    plan_path = project_dir / RENDER_PLAN_REL
    if plan_path.is_file():
        try:
            existing = contract_registry.load_artifact_text(plan_path.read_text("utf-8"))
            if (
                existing.get("caption_revision") == content_revision
                and all(
                    (project_dir / item["artifact"]["rgba_path"]).is_file()
                    and _file_sha256(project_dir / item["artifact"]["rgba_path"])
                    == item["artifact"]["artifact_hash"]
                    for item in existing.get("items", [])
                )
                # A cached plan is handed to callers exactly like a fresh
                # one, so it has to clear the same contract. It did not:
                # plans written before a schema change were returned
                # unvalidated, and the only artifact nobody ever checked was
                # the one served most often.
                and not contract_registry.validate_artifact(
                    "caption_render_plan", existing
                )
            ):
                return existing
        except (ValueError, OSError, KeyError):
            pass
    items = [
        render_caption_png(project_dir, overlay, canvas, render_scale, state)
        for overlay in caption_overlays(state)
    ]
    disallowed = sorted({name for item in items for name in item.pop("x_disallowed_fallbacks")})
    font_receipts: dict[str, dict[str, Any]] = {}
    for item in items:
        item.pop("x_padding", None)
        binding = item.pop("x_font_binding", {})
        asset_id = str(binding.get("asset_id") or "font-legacy-system")
        path = binding.get("path")
        font_receipts[asset_id] = {
            "path": str(path), "sha256": str(binding.get("sha256") or ""),
        }
    plan = {
        "schema_version": 1,
        "caption_revision": content_revision,
        "grapheme_contract": "grapheme_cluster_v1",
        "items": items,
        "receipt": {
            "shaping_engine": "macos-coretext",
            "shaping_engine_version": engine_descriptor()["version"],
            "fonts": {
                **font_receipts,
                EMOJI_FONT_ASSET_ID: {"path": "system:Apple Color Emoji", "sha256": ""},
            },
            "disallowed_fallbacks": disallowed,
        },
    }
    plan["revision"] = contract_registry.canonical_hash(plan)
    errors = contract_registry.validate_artifact("caption_render_plan", plan)
    if errors:
        raise ValueError("caption render plan failed contract validation: " + "; ".join(errors))
    scratch = plan_path.with_name(plan_path.name + ".tmp")
    scratch.write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    scratch.replace(plan_path)
    return plan

SYSTEM_CAPTION_DEFAULTS = {"color": "#F7F2E8", "stroke_color": "#17130F"}
PACK_CAPTION_KEYS = ("color", "stroke_color")


def pack_caption_defaults(pack: dict[str, Any]) -> dict[str, str]:
    palette = pack.get("tokens", {}).get("palette", {})
    defaults: dict[str, str] = {}
    if palette.get("ink"):
        defaults["color"] = str(palette["ink"])
    if palette.get("bg"):
        defaults["stroke_color"] = str(palette["bg"])
    return defaults


def resolve_caption_style(
    overlay: dict[str, Any],
    pack: dict[str, Any] | None,
    pack_source: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Precedence (plan v2): manual key > pack default > system default.

    Variant overrides slot in above manual at render time (P3). Returns the
    resolved style plus a per-key source map for the render receipt.
    """
    manual = overlay.get("style") or {}
    resolved = dict(manual)
    sources = {key: "manual" for key in manual}
    pack_defaults = pack_caption_defaults(pack) if pack else {}
    for key in PACK_CAPTION_KEYS:
        if key in manual and manual.get(key):
            continue
        if key in pack_defaults:
            resolved[key] = pack_defaults[key]
            sources[key] = pack_source
        elif key in SYSTEM_CAPTION_DEFAULTS:
            resolved[key] = SYSTEM_CAPTION_DEFAULTS[key]
            sources[key] = "system"
    return resolved, sources


def selected_pack(state: dict[str, Any], overlay: dict[str, Any]):
    """Per-highlight pack beats the project default; None when unset."""
    import structured_card_compositor

    selection = state.get("style_pack") or {}
    highlight = str(overlay.get("highlight_id") or "")
    pack_id = (
        (selection.get("per_highlight") or {}).get(highlight)
        or selection.get("project_default")
    )
    if not pack_id:
        return None, "system"
    pack = structured_card_compositor.load_style_pack(pack_id)
    scope = "pack-highlight" if (selection.get("per_highlight") or {}).get(highlight) else "pack-project"
    return pack, f"{scope}:{pack_id}"

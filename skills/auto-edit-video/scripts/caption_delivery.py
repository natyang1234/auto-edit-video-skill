#!/usr/bin/env python3
"""Immutable transcript identity and instance-bound caption delivery v2.

Only a loopback Ollama provider is supported.  Caption artifacts bind the raw
ASR revision, deterministic segmentation, ordered editor segments, and every
post-cut caption occurrence.  Required delivery is validated before a direct
renderer creates staging or output bytes.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import contract_registry


CAPTION_REL = Path("working/caption_delivery_v2.json")
SOURCE_CURRENT_REL = Path("working/transcript_source_current.json")
SOURCE_VERSIONS_REL = Path("working/transcript_sources")
SEGMENTATION_REL = Path("working/caption_segmentation.json")
SCHEMA_VERSION = 2
SOURCE_SCHEMA_VERSION = 1
SEGMENTATION_SCHEMA_VERSION = 1
PCM_FORMAT = {
    "sample_rate_hz": 48000,
    "channels": 2,
    "sample_format": "s16le",
}
CHUNKER = {
    "name": "readable_caption_segments",
    "version": 1,
    "config": {
        "gap_us": 700000,
        "max_duration_us": 5500000,
        "max_display_units": 56,
        "sentence_flush_min_us": 800000,
        "clause_flush_min_us": 2400000,
    },
}
IDENTITY_REASONS = {"brand", "proper_name", "code", "number_unit"}
# SPEC Phase 3 v1 §3 step 3: a translation that does not fit in two lines at
# its floor size is asked for again with a measured character budget — once.
# Once, because a second retry buys a shorter answer at the price of an
# unbounded provider loop, and because the caption still fails closed at
# render (§4) if the one retry was not enough.
SHORTENING_MAX_ROUNDS = 1
# The rounds and budgets are a record of what happened during one delivery,
# not part of the provider's identity. They are stripped before the receipt
# is compared with the one derived from the manifest, which knows the
# provider but cannot know how long the answers came back.
SHORTENING_RECEIPT_KEYS = ("shortening_rounds", "shortening_character_budgets")
# The preservation contract is a judgement about one *answer*, and the
# provider is a 7B local model sampling a new one every time. Failing the
# whole delivery on the first violation makes a cut a dice roll: six
# consecutive real cuts died on four different codes, each of which a
# resample would have cleared. So a violation is fed back and asked again —
# twice, never more. Two, because the failure this is for is sampling noise
# and a third round buys almost nothing against a model that genuinely
# cannot comply, while every round is wall-clock time on the user's own
# machine. After the ceiling the original verdict stands and the delivery
# fails closed exactly as before.
VALIDATION_MAX_ROUNDS = 2
# The codes that describe an answer, as opposed to a broken pipe. A shape
# the schema rejects (`translation_invalid`, `translation_incomplete`) or a
# provider that never answered is not something re-asking can fix, so those
# keep failing on the first attempt.
CONTRACT_VIOLATION_CODES = frozenset(
    {
        "translation_unchanged",
        "translation_order_mismatch",
        "translation_duplicate",
        "translation_identity_invalid",
        "translation_token_missing",
        "translation_number_invented",
        "translation_number_order",
        "translation_wrong_language",
    }
)
# Which rejection of a *shortened* answer is worth one more sample. Not the
# preservation codes: dropping the brand or converting the unit to make the
# budget is the trade-off §4 deliberately fails closed on, and re-asking
# mostly re-rolls the same trade-off — the shortening round stays "once,
# never twice" for those. Writing the answer in the source language is not
# that trade-off at all. It is the model losing the target entirely, on a
# caption whose first-round answer was correct English, which is what a real
# cut died of the moment the fit measurement started using the render's own
# frame. That one is sampling noise, and sampling noise is what a resample
# is for; the resampled answer faces the identical validator and the last
# verdict still fails the delivery closed.
SHORTENING_REASK_CODES = frozenset({"translation_wrong_language"})
# Whether an answer had to be re-asked caption by caption is, like the round
# counts, a record of how this one delivery went rather than part of who the
# provider is — a delivery that needed it must not read as a swapped
# provider when the receipt is re-derived from the manifest.
VALIDATION_RECEIPT_KEYS = (
    "validation_retry_rounds",
    "individual_reask",
)
MAX_CAPTION_SOURCES = 20_000
MAX_CAPTION_TEXT_CHARS = 4_000
MAX_CAPTION_ARTIFACT_BYTES = 64 * 1024 * 1024
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+/-]*|\d+(?:\.\d+)?(?:%|[A-Za-z]+)?")
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
TRANSLATION_NORMALISE_RE = re.compile(r"[\W_]+", re.UNICODE)
# SPEC Phase 3 v1 \u00a74 (v1.5): which script the answer is written in.
#
# Only the targets whose own writing system is Latin are judged this way; a
# zh or ja delivery is none of this rule's business and is left alone. The
# language subtag is what counts, so `en`, `en-US` and `pt-BR` all land here.
LATIN_SCRIPT_TARGETS = frozenset(
    {"en", "es", "fr", "de", "it", "pt", "nl", "pl", "tr", "id", "ms", "vi"}
)
LATIN_SCRIPT_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u1e00-\u1eff]")
# Han, kana and hangul. Not "everything that is not Latin": a rule that
# cannot name what it saw has no business failing a delivery closed.
CJK_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# Half of the letters, not all of them. A correct English caption keeps a
# name in its own script often enough ("We are meeting in \u81fa\u5357 tonight") that
# demanding zero CJK would refuse good answers, and the failure being
# stopped here is not subtle \u2014 it is an entire sentence in the wrong
# language, where the ratio is 0.
MIN_TARGET_SCRIPT_RATIO = 0.5
# The delivery path always states the target it asked for. This default is
# for the validator's other callers, and it is a target the check is *on*
# for rather than off, so that a caller who forgets to pass one loses a
# caption to a retry instead of losing the rule.
DEFAULT_VALIDATION_TARGET = "en"
# How a number is *written* is the target language's business; which number
# it is, is the speaker's. Fullwidth digits are what a Chinese IME and
# whisper both produce, and en groups thousands where zh often does not, so
# both are flattened on both sides before anything is compared. Nothing else
# about the token is touched.
FULLWIDTH_NUMERIC_MAP = {
    **{0xFF10 + offset: 0x30 + offset for offset in range(10)},
    0xFF05: ord("%"),  # \uff05
    0xFF0E: ord("."),  # \uff0e
}
# Only a real grouping is a grouping: three digits after every comma, and
# nothing after the last group. "12,00" is not 1200, it is a typo or a
# different number, and it stays visible to the comparison.
THOUSANDS_GROUP_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)")
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
CHINESE_DIGIT_VALUES = {
    "\u96f6": 0, "\u3007": 0, "\u4e00": 1, "\u4e8c": 2, "\u5169": 2, "\u4e24": 2, "\u4e09": 3, "\u56db": 4,
    "\u4e94": 5, "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9,
}
CHINESE_UNIT_VALUES = {"\u5341": 10, "\u767e": 100, "\u5343": 1000}
CHINESE_MYRIAD = {"\u842c": 10000, "\u4e07": 10000}
CHINESE_NUMERAL_CHARS = frozenset(CHINESE_DIGIT_VALUES) | frozenset(
    CHINESE_UNIT_VALUES
) | frozenset(CHINESE_MYRIAD)
CHINESE_PERCENT_PREFIXES = ("\u767e\u5206\u4e4b", "\u767e\u5206\u9ede")
# A single numeral character is not read as a number at all, and neither is
# a compound that comes out under ten. \u4e00 is the most common character in
# Chinese subtitles (\u4e00\u8d77, \u4e00\u76f4, \u4e00\u5b9a, \u7b2c\u4e00\u6b21) and \u5341\u5206 means "very"; \u842c\u4e00
# means "in case" and parses to 1. Reading those as quantities would demand
# a digit in the translation of ordinary speech and refuse every correct
# answer, which is how a preservation rule gets switched off. Ten is where
# the idiom rate falls below the quantity rate.
CHINESE_NUMBER_MIN_VALUE = 10


def normalise_numerals(text: str) -> str:
    """Fullwidth digits to ASCII, thousands separators removed."""
    return THOUSANDS_GROUP_RE.sub(
        lambda match: match.group(0).replace(",", ""),
        text.translate(FULLWIDTH_NUMERIC_MAP),
    )


def _parse_chinese_numerals(run: str) -> int | None:
    """Value of a pure numeral run, or None if it is not one."""
    if not run:
        return None
    total = 0
    section = 0
    digit: int | None = None
    for char in run:
        if char in CHINESE_DIGIT_VALUES:
            digit = CHINESE_DIGIT_VALUES[char]
        elif char in CHINESE_UNIT_VALUES:
            # \u5341\u516b is eighteen: the leading one is implied, so a unit with
            # nothing in front of it multiplies one, not zero.
            section += (1 if digit is None else digit) * CHINESE_UNIT_VALUES[char]
            digit = None
        elif char in CHINESE_MYRIAD:
            total += (section + (digit or 0)) * CHINESE_MYRIAD[char]
            section = 0
            digit = None
        else:
            return None
    return total + section + (digit or 0)


def chinese_number_value(text: str) -> str | None:
    """The number a Chinese numeral run states, if it states one.

    Deliberately conservative \u2014 see CHINESE_NUMBER_MIN_VALUE. A None here
    means "not enough signal to enforce", not "zero".
    """
    if len(text) < 2 or any(char not in CHINESE_NUMERAL_CHARS for char in text):
        return None
    value = _parse_chinese_numerals(text)
    if value is None or value < CHINESE_NUMBER_MIN_VALUE:
        return None
    return str(value)


def chinese_percent_value(text: str) -> str | None:
    """"\u767e\u5206\u4e4b\u516b\u5341\u4e03" -> "87%". The sign is part of what has to survive."""
    for prefix in CHINESE_PERCENT_PREFIXES:
        if text.startswith(prefix):
            value = _parse_chinese_numerals(text[len(prefix):])
            # No minimum here: \u767e\u5206\u4e4b\u4e94 is unambiguously five percent, the
            # prefix is the signal the bare characters lacked.
            return None if value is None else f"{value}%"
    return None


def _chinese_number_spans(text: str) -> list[tuple[int, str]]:
    """(offset, value) for every Chinese-written number, in source order.

    Percentages carry their sign; everything else is the bare value, which
    is what the translation has to contain somewhere in its own digits.
    """
    found: list[tuple[int, str]] = []
    index = 0
    while index < len(text):
        prefix = next(
            (
                candidate
                for candidate in CHINESE_PERCENT_PREFIXES
                if text.startswith(candidate, index)
            ),
            None,
        )
        start = index + len(prefix) if prefix else index
        end = start
        while end < len(text) and text[end] in CHINESE_NUMERAL_CHARS:
            end += 1
        if end == start:
            index += 1
            continue
        run = text[start:end]
        value = (
            chinese_percent_value(f"{prefix}{run}") if prefix
            else chinese_number_value(run)
        )
        if value is not None:
            found.append((index, value))
        index = end
    return found


def _number_key(literal: str) -> str:
    """One number, one spelling: 08, 8 and 8.0 are the same quantity."""
    text = literal.rstrip("%")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = text.lstrip("0")
    return text or "0"


def _ascii_number_spans(text: str) -> list[tuple[int, str]]:
    return [
        (match.start(), match.group(0))
        for match in NUMERIC_TOKEN_RE.finditer(text)
        # A number inside a code or a brand (NEO2026, H264) is that token's
        # business, not a quantity of its own.
        if match.start() == 0 or not (
            text[match.start() - 1].isalpha() or text[match.start() - 1] == "-"
        )
    ]


def source_number_sequence(text: str) -> list[str]:
    """Every number the source states, as bare values, in source order.

    Both the digits it wrote and the ones it spelled in Chinese: a caption
    that says \u4e09\u5341 states thirty just as much as one that says 30.

    Advisory only. Nothing blocking reads this any more (v1.4, 2026-08-13):
    the Chinese half of it turned out to refuse correct English far more
    often than it caught a changed fact, because ordinary English does not
    put a digit where Chinese put a numeral (\u5341\u5e74\u524d -> "ten years ago",
    \u5343\u842c\u5225 -> "do not"). It stays for metadata and for a human reading
    a report; the enforced half is `source_arabic_number_sequence`.
    """
    normalised = normalise_numerals(text)
    spans = _ascii_number_spans(normalised) + _chinese_number_spans(normalised)
    return [_number_key(value) for _offset, value in sorted(spans)]


def source_arabic_number_sequence(text: str) -> list[str]:
    """Only the numbers the source wrote in digits, in source order.

    The enforceable half of the source's numbers. A digit in the source is
    a fact the translation has to carry through unchanged; a numeral the
    source spelled in Chinese is a fact this program cannot read reliably
    enough to convict an answer over.
    """
    normalised = normalise_numerals(text)
    return [_number_key(value) for _offset, value in _ascii_number_spans(normalised)]


def states_chinese_numerals(text: str) -> bool:
    """Whether the source spells anything in Chinese numeral characters.

    Deliberately the loosest possible test \u2014 one character is enough,
    including the ones the converter refuses to read as quantities
    (\u4e00\u8d77, \u5341\u5206, \u842c\u4e00). The claim being made downstream is not "this
    source states a number", it is "this program cannot say what numbers
    this source states", and that claim is true for \u5341\u5206 as much as for
    \u4e09\u5341. Being wrong in this direction costs a missed mutation in one
    caption; being wrong in the other direction refuses correct captions
    by the dozen, which is how the rule gets switched off entirely.
    """
    return any(char in CHINESE_NUMERAL_CHARS for char in text)


def translation_number_sequence(text: str) -> list[str]:
    """Every number the translation puts on screen, in the order drawn."""
    normalised = normalise_numerals(text)
    return [_number_key(value) for _offset, value in _ascii_number_spans(normalised)]


def chinese_number_advisories(source: str, translated: str) -> list[str]:
    """Numbers the source seems to spell in Chinese and the answer omits.

    Advisory, never blocking (v1.4, 2026-08-13). Every entry here is a
    guess a human should be allowed to overrule, which is why it returns
    strings for a report instead of raising: 三分之一 -> "1/3" lands here
    and is a perfectly good caption.
    """
    delivered = set(translation_number_sequence(translated))
    stated = [
        _number_key(value)
        for _offset, value in _chinese_number_spans(normalise_numerals(source))
    ]
    return [value for value in stated if value not in delivered]


def _token_key(token: str) -> str:
    """Casefolded, except where case is what the token means.

    A brand shouted at the start of a sentence is still the brand, so
    words fold. A unit does not: 5mW is a phone charger and 5MW is a power
    station, and folding them together let that mutation through.
    """
    if token[:1].isdigit() and any(char.isalpha() for char in token):
        return token
    return token.casefold()


def _letter_is_fused_into_cjk(text: str, start: int, end: int) -> bool:
    """Is this lone Latin letter a part of the Chinese word next to it?

    「三根K棒」 is one noun — K棒 is a candlestick — and every natural
    English translation of it says "candlestick". Requiring the letter K to
    reappear made that caption an unsatisfiable contract: no correct answer
    exists, so the delivery could only fail closed or be switched off, and
    a rule that cries wolf on ordinary speech protects nothing.

    Kept as narrow as the defect: exactly one letter, no digits, and no
    Latin word touching it. `5mW` is two characters and stays required,
    `RSI` is three and stays required, and the B of "plan B" has a Latin
    word on its left so it stays required even with Chinese after it.
    Spaces are looked through, because 「這是 A 級的」 is the same word
    whether or not the speaker's transcript spaced it out.
    """
    token = text[start:end]
    if len(token) != 1 or not token.isascii() or not token.isalpha():
        return False
    before = text[:start].rstrip()[-1:]
    after = text[end:].lstrip()[:1]
    if LATIN_SCRIPT_RE.match(before) or LATIN_SCRIPT_RE.match(after):
        return False
    return bool(CJK_SCRIPT_RE.match(before) or CJK_SCRIPT_RE.match(after))


def _sequence_difference(left: list[str], right: list[str]) -> list[str]:
    """Members of `left` that `right` does not have as many of."""
    remaining = list(right)
    missing: list[str] = []
    for value in left:
        if value in remaining:
            remaining.remove(value)
        else:
            missing.append(value)
    return missing


class CaptionDeliveryError(ValueError):
    """Stable, user-visible fail-closed caption error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class _ProjectTrust:
    root: Path
    working: Path
    root_fd: int
    working_fd: int
    root_identity: tuple[int, int]
    working_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _TranscriptSourceTrust:
    project: _ProjectTrust
    versions_fd: int
    versions_identity: tuple[int, int]


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _verify_project_trust(trust: _ProjectTrust) -> None:
    """Require the pathname identities to still name the opened directories."""
    try:
        root_now = os.stat(trust.root, follow_symlinks=False)
        working_now = os.stat("working", dir_fd=trust.root_fd, follow_symlinks=False)
        opened_root = os.fstat(trust.root_fd)
        opened_working = os.fstat(trust.working_fd)
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    if (
        not stat.S_ISDIR(root_now.st_mode)
        or not stat.S_ISDIR(working_now.st_mode)
        or _identity(root_now) != trust.root_identity
        or _identity(opened_root) != trust.root_identity
        or _identity(working_now) != trust.working_identity
        or _identity(opened_working) != trust.working_identity
    ):
        raise CaptionDeliveryError(
            "caption_project_changed", "project root or working directory identity changed"
        )


@contextlib.contextmanager
def _project_trust(project_dir: Path) -> Iterator[_ProjectTrust]:
    root, working = _trusted_working_root(project_dir)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = working_fd = -1
    try:
        root_fd = os.open(root, flags)
        working_fd = os.open("working", flags, dir_fd=root_fd)
        trust = _ProjectTrust(
            root=root,
            working=working,
            root_fd=root_fd,
            working_fd=working_fd,
            root_identity=_identity(os.fstat(root_fd)),
            working_identity=_identity(os.fstat(working_fd)),
        )
        _verify_project_trust(trust)
        yield trust
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    finally:
        if working_fd >= 0:
            os.close(working_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _verify_transcript_source_trust(trust: _TranscriptSourceTrust) -> None:
    _verify_project_trust(trust.project)
    try:
        versions_now = os.stat(
            SOURCE_VERSIONS_REL.name,
            dir_fd=trust.project.working_fd,
            follow_symlinks=False,
        )
        opened_versions = os.fstat(trust.versions_fd)
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    if (
        not stat.S_ISDIR(versions_now.st_mode)
        or _identity(versions_now) != trust.versions_identity
        or _identity(opened_versions) != trust.versions_identity
    ):
        raise CaptionDeliveryError(
            "caption_project_changed", "transcript source directory identity changed"
        )


@contextlib.contextmanager
def _transcript_source_trust(project_dir: Path) -> Iterator[_TranscriptSourceTrust]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    versions_fd = -1
    with _project_trust(project_dir) as project_trust:
        try:
            try:
                os.mkdir(
                    SOURCE_VERSIONS_REL.name,
                    mode=0o700,
                    dir_fd=project_trust.working_fd,
                )
            except FileExistsError:
                pass
            _verify_project_trust(project_trust)
            versions_fd = os.open(
                SOURCE_VERSIONS_REL.name,
                flags,
                dir_fd=project_trust.working_fd,
            )
            trust = _TranscriptSourceTrust(
                project=project_trust,
                versions_fd=versions_fd,
                versions_identity=_identity(os.fstat(versions_fd)),
            )
            _verify_transcript_source_trust(trust)
            yield trust
        except CaptionDeliveryError:
            raise
        except OSError as exc:
            raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
        finally:
            if versions_fd >= 0:
                os.close(versions_fd)


def _atomic_write_to_fd(
    *,
    directory_fd: int,
    name: str,
    payload: Any,
    verify: Callable[[], None],
) -> None:
    """Atomically adopt canonical bytes beneath an already trusted directory fd."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise CaptionDeliveryError("caption_project_changed", "unsafe adoption filename")
    verify()
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        raw = canonical_bytes(payload)
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        verify()
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        verify()
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _atomic_write_at(
    trust: _ProjectTrust,
    *,
    working: bool,
    name: str,
    payload: Any,
) -> None:
    """Atomic no-follow write relative to a directory held open across provider wait."""
    directory_fd = trust.working_fd if working else trust.root_fd
    _atomic_write_to_fd(
        directory_fd=directory_fd,
        name=name,
        payload=payload,
        verify=lambda: _verify_project_trust(trust),
    )


def _trusted_working_root(project_dir: Path) -> tuple[Path, Path]:
    root = project_dir.resolve()
    if not root.is_dir():
        raise CaptionDeliveryError("caption_project_invalid", "project directory is missing")
    working = root / "working"
    if working.is_symlink() or not working.is_dir() or working.resolve() != working:
        raise CaptionDeliveryError("caption_project_invalid", "working directory is not owned")
    return root, working


def _owned_artifact(project_dir: Path, relative: Path) -> Path:
    root, _working = _trusted_working_root(project_dir)
    path = root / relative
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise CaptionDeliveryError("caption_artifact_invalid", f"unsafe artifact: {relative}")
    # Every existing parent below the project must be a real directory.  This
    # prevents a symlinked working subdirectory from redirecting reads/writes.
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise CaptionDeliveryError("caption_artifact_invalid", f"unsafe parent: {relative}")
    return path


def _ensure_owned_subdir(project_dir: Path, relative: Path) -> Path:
    root, _working = _trusted_working_root(project_dir)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CaptionDeliveryError("caption_project_invalid", f"unsafe directory: {relative}")
        if current.exists():
            if not current.is_dir():
                raise CaptionDeliveryError("caption_project_invalid", f"not a directory: {relative}")
        else:
            current.mkdir(mode=0o700)
        if current.resolve() != current:
            raise CaptionDeliveryError("caption_project_invalid", f"aliased directory: {relative}")
    return current


def canonical_bytes(payload: Any) -> bytes:
    contract_registry._reject_nonfinite(payload)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(canonical_bytes(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> Any:
    try:
        return contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError("caption_artifact_invalid", str(exc)) from exc


def _read_regular_at(
    directory_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> bytes | None:
    """Read a bounded regular file by trusted-directory-relative name."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise CaptionDeliveryError("transcript_source_invalid", "unsafe source filename")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CAPTION_ARTIFACT_BYTES:
            raise CaptionDeliveryError(
                "transcript_source_invalid", f"unsafe source artifact: {name}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_CAPTION_ARTIFACT_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CAPTION_ARTIFACT_BYTES:
                raise CaptionDeliveryError(
                    "transcript_source_invalid", f"source artifact too large: {name}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CaptionDeliveryError("transcript_source_invalid", f"missing source artifact: {name}")
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("transcript_source_invalid", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_json_bytes(raw: bytes, *, code: str) -> Any:
    try:
        return contract_registry.load_artifact_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError(code, str(exc)) from exc


def _load_owned_json_bytes(project_dir: Path, relative: Path) -> tuple[Any, bytes]:
    """Read one owned regular artifact exactly once through O_NOFOLLOW."""
    path = _owned_artifact(project_dir, relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CAPTION_ARTIFACT_BYTES:
            raise CaptionDeliveryError(
                "caption_artifact_invalid", f"artifact size/type is unsafe: {relative}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CAPTION_ARTIFACT_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CAPTION_ARTIFACT_BYTES:
                raise CaptionDeliveryError(
                    "caption_artifact_invalid", f"artifact is too large: {relative}"
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
        payload = contract_registry.load_artifact_text(raw.decode("utf-8"))
        return payload, raw
    except CaptionDeliveryError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError("caption_artifact_invalid", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owned_source(project_dir: Path, manifest: dict[str, Any]) -> Path:
    root = project_dir.resolve()
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise CaptionDeliveryError("transcript_source_invalid", "manifest source is missing")
    relative = str(source.get("staged_path") or "")
    entry = root / relative
    if entry.is_symlink():
        raise CaptionDeliveryError("transcript_source_invalid", "source media is a symlink")
    resolved = entry.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise CaptionDeliveryError("transcript_source_invalid", "source media is not project-owned")
    declared = str(source.get("sha256") or "")
    if not SHA_RE.fullmatch(declared) or _file_sha256(resolved) != declared:
        raise CaptionDeliveryError("transcript_source_invalid", "source media hash mismatch")
    return resolved


def decoded_pcm_sha256(project_dir: Path, manifest: dict[str, Any]) -> str:
    """Hash ffmpeg-decoded audio under the fixed 48k/stereo/s16le contract."""
    source = _owned_source(project_dir, manifest)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CaptionDeliveryError("pcm_decode_unavailable", "ffmpeg is unavailable")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    digest = hashlib.sha256()
    decoded_bytes = 0
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log)
        assert process.stdout is not None
        try:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                decoded_bytes += len(chunk)
                digest.update(chunk)
            try:
                return_code = process.wait(timeout=3600)
            except subprocess.TimeoutExpired as exc:
                raise CaptionDeliveryError(
                    "pcm_decode_failed", "ffmpeg decode timed out"
                ) from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        error_log.seek(0)
        stderr = error_log.read()
    if return_code != 0 or decoded_bytes == 0:
        detail = stderr.decode("utf-8", "replace")[-500:]
        raise CaptionDeliveryError("pcm_decode_failed", detail or "decoded PCM was empty")
    return digest.hexdigest()


def _seconds_us(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionDeliveryError("caption_contract_invalid", f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise CaptionDeliveryError("caption_contract_invalid", f"{label} must be finite and nonnegative")
    return int(round(numeric * 1_000_000))


def _raw_words(data: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise CaptionDeliveryError("transcript_source_invalid", "segments must be an array")
    for segment in segments:
        if not isinstance(segment, dict):
            raise CaptionDeliveryError("transcript_source_invalid", "segment must be an object")
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for raw in words:
            if not isinstance(raw, dict):
                raise CaptionDeliveryError("transcript_source_invalid", "word must be an object")
            text = str(raw.get("word", ""))
            if not text.strip():
                continue
            start_us = _seconds_us(raw.get("start", segment.get("start", 0)), "raw word start")
            end_us = _seconds_us(raw.get("end", segment.get("end", 0)), "raw word end")
            if end_us < start_us:
                raise CaptionDeliveryError("transcript_source_invalid", "raw word end precedes start")
            speaker = raw.get("speaker", segment.get("speaker"))
            if speaker is not None and not isinstance(speaker, str):
                raise CaptionDeliveryError("transcript_source_invalid", "speaker must be string or null")
            output.append(
                {
                    "source_word_index": len(output),
                    "start_us": start_us,
                    "end_us": end_us,
                    "text": text,
                    "speaker": speaker,
                }
            )
    if not output:
        raise CaptionDeliveryError("transcript_source_invalid", "raw transcript has no timed words")
    return output


def capture_transcript_source(
    project_dir: Path,
    manifest: dict[str, Any],
    data: dict[str, Any],
    *,
    model: str,
    force_retranscription: bool = False,
) -> dict[str, Any]:
    """Persist immutable raw ASR identity before any text correction occurs."""
    if not MODEL_RE.fullmatch(model):
        raise CaptionDeliveryError("transcript_source_invalid", "model name is invalid")
    params = data.get("decoding_params", {})
    if not isinstance(params, dict):
        raise CaptionDeliveryError("transcript_source_invalid", "decoding_params must be an object")
    contract_registry._reject_nonfinite(params)
    raw_words = _raw_words(data)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    with _transcript_source_trust(project_dir) as trust:
        current_raw = _read_regular_at(
            trust.project.working_fd,
            SOURCE_CURRENT_REL.name,
            missing_ok=True,
        )
        previous: dict[str, Any] | None = None
        if current_raw is not None:
            pointer = _decode_json_bytes(current_raw, code="transcript_source_invalid")
            revision = str(pointer.get("revision") or "") if isinstance(pointer, dict) else ""
            if SHA_RE.fullmatch(revision):
                previous_raw = _read_regular_at(
                    trust.versions_fd,
                    f"{revision}.json",
                    missing_ok=True,
                )
                if previous_raw is not None:
                    decoded = _decode_json_bytes(
                        previous_raw, code="transcript_source_invalid"
                    )
                    if isinstance(decoded, dict):
                        previous = decoded
        generation = 0
        if previous is not None:
            value = previous.get("source_generation")
            if type(value) is not int or value < 0:
                raise CaptionDeliveryError(
                    "transcript_source_invalid", "source_generation is invalid"
                )
            generation = value + (1 if force_retranscription else 0)

        pcm_sha256 = decoded_pcm_sha256(trust.project.root, manifest)
        _verify_transcript_source_trust(trust)
        payload: dict[str, Any] = {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "revision": "",
            "source_media_sha256": str(source.get("sha256") or ""),
            "audio_stream_index": 0,
            "decoded_pcm": {**PCM_FORMAT, "sha256": pcm_sha256},
            "engine": str(data.get("engine") or "openai-whisper"),
            "engine_version": str(
                data.get("engine_version") or data.get("version") or "unknown"
            ),
            "model": model,
            "language": str(data.get("language") or "unknown"),
            "decoding_params": params,
            "source_generation": generation,
            "raw_words": raw_words,
        }
        revision_payload = dict(payload)
        revision_payload.pop("revision")
        revision = contract_registry.canonical_hash(revision_payload)
        payload["revision"] = revision
        errors = contract_registry.validate_artifact("transcript_source", payload)
        if errors:
            raise CaptionDeliveryError("transcript_source_invalid", "; ".join(errors))
        expected = canonical_bytes(payload)
        version_name = f"{revision}.json"
        existing = _read_regular_at(
            trust.versions_fd,
            version_name,
            missing_ok=True,
        )
        if existing is not None:
            if existing != expected:
                raise CaptionDeliveryError(
                    "transcript_source_immutable", "revision bytes differ"
                )
        else:
            _atomic_write_to_fd(
                directory_fd=trust.versions_fd,
                name=version_name,
                payload=payload,
                verify=lambda: _verify_transcript_source_trust(trust),
            )
        _atomic_write_to_fd(
            directory_fd=trust.project.working_fd,
            name=SOURCE_CURRENT_REL.name,
            payload={
                "schema_version": 1,
                "revision": revision,
                "path": (SOURCE_VERSIONS_REL / version_name).as_posix(),
                "artifact_sha256": hashlib.sha256(expected).hexdigest(),
            },
            verify=lambda: _verify_transcript_source_trust(trust),
        )
        return payload


def load_current_source(project_dir: Path) -> dict[str, Any]:
    root = project_dir.resolve()
    pointer = _load_json(_owned_artifact(root, SOURCE_CURRENT_REL))
    if not isinstance(pointer, dict) or not SHA_RE.fullmatch(str(pointer.get("revision") or "")):
        raise CaptionDeliveryError("transcript_source_missing")
    relative = str(pointer.get("path") or "")
    expected = (SOURCE_VERSIONS_REL / f"{pointer['revision']}.json").as_posix()
    if relative != expected:
        raise CaptionDeliveryError("transcript_source_invalid", "current pointer path mismatch")
    path = root / relative
    if path.is_symlink() or not path.is_file() or path.resolve().parent != (root / SOURCE_VERSIONS_REL).resolve():
        raise CaptionDeliveryError("transcript_source_invalid", "current source path is unsafe")
    payload = _load_json(path)
    if payload.get("revision") != pointer["revision"]:
        raise CaptionDeliveryError("transcript_source_invalid", "current pointer revision mismatch")
    if hashlib.sha256(path.read_bytes()).hexdigest() != pointer.get("artifact_sha256"):
        raise CaptionDeliveryError("transcript_source_invalid", "current source bytes changed")
    errors = contract_registry.validate_artifact("transcript_source", payload)
    if errors:
        raise CaptionDeliveryError("transcript_source_invalid", "; ".join(errors))
    return payload


def build_segmentation(project_dir: Path, transcript: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = load_current_source(project_dir)
    words = transcript.get("words")
    captions = transcript.get("caption_segments") or transcript.get("segments")
    if not isinstance(words, list) or not isinstance(captions, list):
        raise CaptionDeliveryError("caption_segmentation_invalid", "transcript words/captions missing")
    if len(words) > 100_000 or len(captions) > MAX_CAPTION_SOURCES:
        raise CaptionDeliveryError("caption_segmentation_invalid", "transcript exceeds caption limits")
    word_indexes: dict[str, int] = {}
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            raise CaptionDeliveryError("caption_segmentation_invalid", "word must be object")
        word_id = str(word.get("id") or "")
        if word_id:
            if word_id in word_indexes:
                raise CaptionDeliveryError("caption_segmentation_invalid", "duplicate word id")
            word_indexes[word_id] = index
    spans: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    ordinal_by_span: dict[tuple[Any, ...], int] = {}
    for segment in captions:
        if not isinstance(segment, dict):
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption must be object")
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MAX_CAPTION_TEXT_CHARS:
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption text is too long")
        start_us = _seconds_us(segment.get("start", 0), "caption start")
        end_us = _seconds_us(segment.get("end", 0), "caption end")
        if end_us <= start_us:
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption end must exceed start")
        word_ids = segment.get("word_ids")
        indices: list[int] = []
        if isinstance(word_ids, list) and word_ids:
            try:
                indices = [word_indexes[str(item)] for item in word_ids]
            except KeyError as exc:
                raise CaptionDeliveryError("caption_segmentation_invalid", "caption references unknown word") from exc
            if indices != list(range(indices[0], indices[-1] + 1)):
                raise CaptionDeliveryError("caption_segmentation_invalid", "caption word span is not contiguous")
        if indices:
            if indices[-1] >= len(source.get("raw_words", [])):
                raise CaptionDeliveryError(
                    "caption_segmentation_invalid", "caption span exceeds raw source words"
                )
            span = {
                "first_source_word_index": indices[0],
                "last_source_word_index": indices[-1],
                "fallback_start_us": None,
                "fallback_end_us": None,
            }
            key: tuple[Any, ...] = (indices[0], indices[-1])
        else:
            span = {
                "first_source_word_index": None,
                "last_source_word_index": None,
                "fallback_start_us": start_us,
                "fallback_end_us": end_us,
            }
            key = ("fallback", start_us, end_us)
        within = ordinal_by_span.get(key, 0)
        ordinal_by_span[key] = within + 1
        spans.append({**span, "within_span_ordinal": within})
        source_records.append(
            {
                "text": text,
                "source_start_us": start_us,
                "source_end_us": end_us,
                "span": span,
                "within_span_ordinal": within,
            }
        )
    if not spans:
        raise CaptionDeliveryError("caption_segmentation_invalid", "no caption segments")
    base = {
        "schema_version": SEGMENTATION_SCHEMA_VERSION,
        "source_revision": source["revision"],
        "segmentation_revision": "",
        "chunker": copy.deepcopy(CHUNKER),
        "spans": spans,
    }
    revision_payload = dict(base)
    revision_payload.pop("segmentation_revision")
    base["segmentation_revision"] = contract_registry.canonical_hash(revision_payload)
    errors = contract_registry.validate_artifact("caption_segmentation", base)
    if errors:
        raise CaptionDeliveryError("caption_segmentation_invalid", "; ".join(errors))
    for item in source_records:
        material = {
            "source_revision": source["revision"],
            "segmentation_revision": base["segmentation_revision"],
            "span": item["span"],
            "within_span_ordinal": item["within_span_ordinal"],
        }
        item["caption_source_id"] = "caption-" + contract_registry.canonical_hash(material)[:16]
        item["corrected_source_sha256"] = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        item["source_revision"] = source["revision"]
        item["segmentation_revision"] = base["segmentation_revision"]
    return base, source_records


def _state_segments(state: dict[str, Any]) -> list[dict[str, int]]:
    raw = state.get("segments")
    if not isinstance(raw, list) or not raw:
        raise CaptionDeliveryError("caption_timeline_invalid", "editor segments are missing")
    output: list[dict[str, int]] = []
    offset = 0
    for item in raw:
        if not isinstance(item, dict):
            raise CaptionDeliveryError("caption_timeline_invalid", "editor segment must be object")
        start = _seconds_us(item.get("source_start"), "segment source_start")
        end = _seconds_us(item.get("source_end"), "segment source_end")
        if end <= start:
            raise CaptionDeliveryError("caption_timeline_invalid", "editor segment end must exceed start")
        output.append({"source_start_us": start, "source_end_us": end, "final_start_us": offset})
        offset += end - start
    return output


def _cut_map_hash(project_dir: Path, segments: list[dict[str, int]]) -> str:
    path = project_dir / "working/cut_map.json"
    if path.is_symlink():
        raise CaptionDeliveryError("caption_timeline_invalid", "cut map is a symlink")
    if path.is_file():
        return _file_sha256(path)
    return contract_registry.canonical_hash({"segments": segments})


def expected_instances(
    project_dir: Path,
    transcript: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    segmentation, sources = build_segmentation(project_dir, transcript)
    segments = _state_segments(state)
    source_list_hash = contract_registry.canonical_hash(
        [item["caption_source_id"] for item in sources]
    )
    cut_map_hash = _cut_map_hash(project_dir, segments)
    timeline_revision = contract_registry.canonical_hash(
        {
            "segments": segments,
            "caption_sources": [
                {
                    "caption_source_id": item["caption_source_id"],
                    "source_start_us": item["source_start_us"],
                    "source_end_us": item["source_end_us"],
                }
                for item in sources
            ],
        }
    )
    pending: list[dict[str, Any]] = []
    for timeline_index, segment in enumerate(segments):
        for source_index, source in enumerate(sources):
            hit_start = max(segment["source_start_us"], source["source_start_us"])
            hit_end = min(segment["source_end_us"], source["source_end_us"])
            if hit_end <= hit_start:
                continue
            final_start = segment["final_start_us"] + hit_start - segment["source_start_us"]
            final_end = segment["final_start_us"] + hit_end - segment["source_start_us"]
            pending.append(
                {
                    "timeline_index": timeline_index,
                    "source_index": source_index,
                    "caption_source_id": source["caption_source_id"],
                    "corrected_source": source["text"],
                    "corrected_source_sha256": source["corrected_source_sha256"],
                    "source_start_us": hit_start,
                    "source_end_us": hit_end,
                    "final_start_us": final_start,
                    "final_end_us": final_end,
                    "source_revision": source["source_revision"],
                    "segmentation_revision": source["segmentation_revision"],
                }
            )
    pending.sort(key=lambda item: (item["final_start_us"], item["timeline_index"], item["source_index"]))
    occurrence: dict[str, int] = {}
    for item in pending:
        source_id = item["caption_source_id"]
        ordinal = occurrence.get(source_id, 0)
        occurrence[source_id] = ordinal + 1
        item["caption_instance_id"] = "caption-instance-" + contract_registry.canonical_hash(
            {"caption_source_id": source_id, "occurrence_ordinal": ordinal}
        )[:16]
        item["occurrence_ordinal"] = ordinal
        item.pop("timeline_index")
        item.pop("source_index")
    return {
        "segmentation": segmentation,
        "sources": sources,
        "instances": pending,
        "source_list_hash": source_list_hash,
        "cut_map_sha256": cut_map_hash,
        "timeline_revision": timeline_revision,
    }


def _provider_config(manifest: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    subtitles = manifest.get("subtitles")
    if not isinstance(subtitles, dict):
        raise CaptionDeliveryError("translation_provider_unsupported", "subtitle config missing")
    translation = subtitles.get("translation")
    if translation is None:
        translation = {}
    if not isinstance(translation, dict):
        raise CaptionDeliveryError("translation_provider_unsupported", "translation config invalid")
    provider = str(translation.get("provider") or "ollama")
    semantic = subtitles.get("contextual_semantic_calibration")
    semantic = semantic if isinstance(semantic, dict) else {}
    model = str(translation.get("model") or semantic.get("model") or "qwen2.5:7b")
    if provider != "ollama" or not MODEL_RE.fullmatch(model):
        raise CaptionDeliveryError("translation_provider_unsupported", provider)
    raw_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    if "://" not in raw_host:
        raw_host = f"http://{raw_host}"
    parsed = urllib.parse.urlsplit(raw_host)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CaptionDeliveryError(
            "translation_provider_unsupported", "only loopback Ollama is permitted"
        )
    try:
        _port = parsed.port
    except ValueError as exc:
        raise CaptionDeliveryError("translation_provider_unsupported", "invalid Ollama port") from exc
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    config = {
        "provider": provider,
        "mode": "local_loopback",
        "model": model,
        "endpoint_origin": origin,
    }
    return provider, model, config


def provider_receipt(manifest: dict[str, Any]) -> dict[str, Any]:
    provider, model, config = _provider_config(manifest)
    return {
        "provider_id": provider,
        "mode": "local_loopback",
        "model": model,
        "config_sha256": contract_registry.canonical_hash(config),
        "consent_mode": "not_required_local",
        "consent_sha256": None,
    }


def _translation_prompt(instances: list[dict[str, Any]], target: str) -> str:
    request = [
        {
            "caption_instance_id": item["caption_instance_id"],
            "source": item["corrected_source"],
        }
        for item in instances
    ]
    return (
        f"Translate each caption to {target}. Return only JSON object {{\"items\":[...]}}. "
        "Keep the exact input order and caption_instance_id; one output per input, no extras. "
        "Each item needs translated_text. A deliberately unchanged brand, proper name, code, "
        "or number/unit also needs identity_preserved=true and identity_reason equal to "
        "brand, proper_name, code, or number_unit. Preserve all source Latin, number, and unit tokens.\n"
        + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    )


def _shortening_prompt(
    instances: list[dict[str, Any]],
    target: str,
    budgets: dict[str, int],
) -> str:
    """Ask again for the same lines, shorter, without licence to drop facts.

    SPEC Phase 3 v1 §4: a shorter line is worth having; a shorter line that
    lost the number, the unit or the brand is not, it is a wrong subtitle
    that happens to fit. So the budget is stated per caption and the things
    that may not be spent to meet it are stated with it, and the answer is
    validated exactly like the first one — this prompt is a request, not a
    permission slip.
    """
    request = [
        {
            "caption_instance_id": item["caption_instance_id"],
            "source": item["corrected_source"],
            "character_budget": budgets[item["caption_instance_id"]],
        }
        for item in instances
    ]
    return (
        f"These {target} captions are too long for two lines on screen. "
        "Translate each source again, shorter, so that translated_text is at "
        "most character_budget characters long. Say the same thing in fewer "
        "words; do not summarise away meaning. Numbers, units, brands and "
        "proper names must not be dropped, abbreviated or converted — keep "
        "every one of them exactly as it appears in the source, and spend the "
        "budget on the words around them. "
        'Return only JSON object {"items":[...]}. '
        "Keep the exact input order and caption_instance_id; one output per "
        "input, no extras. Each item needs translated_text. A deliberately "
        "unchanged brand, proper name, code, or number/unit also needs "
        "identity_preserved=true and identity_reason equal to brand, "
        "proper_name, code, or number_unit.\n"
        + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    )


def _revalidation_prompt(
    instances: list[dict[str, Any]],
    target: str,
    errors: list[CaptionDeliveryError],
) -> str:
    """Ask again, saying what was wrong with the last answer.

    A retry that repeats the original prompt is a second roll of the same
    dice. This one names the rule that was broken and which caption broke
    it, so the second sample is drawn with the information the first one
    was missing — and it restates the rules rather than relaxing any of
    them, because the answer it produces faces the identical validator.
    """
    # Built as a preface to the original prompt rather than as a
    # replacement for it. A retry prompt written from scratch is a
    # different instruction, and a small model answers it differently in
    # ways that have nothing to do with the rejection: the first version of
    # this leaned on "keep every name and number exactly as it appears" and
    # the model started returning the Chinese source untranslated. The task
    # has not changed, so the wording of the task does not change either —
    # only what went wrong is added, in front.
    return _revalidation_preface(errors, target) + _translation_prompt(instances, target)


def _revalidation_preface(errors: list[CaptionDeliveryError], target: str) -> str:
    """What went wrong, in front of whichever question is being repeated.

    Both questions this delivery asks — translate, then translate shorter —
    face the same validator and are answered by the same 7B model, so both
    are re-asked the same way: the rejection named, the rules restated, the
    question itself unchanged behind it.
    """
    rejected = "; ".join(
        f"{error.code} ({error.detail})" if error.detail else error.code
        for error in errors
    )
    return (
        f"Your previous answer was rejected by an automatic check: "
        f"{rejected}. Fix exactly that and change nothing "
        "else. Reminders, in the order they are most often broken: "
        f"translated_text must be written in {target}, never in the source "
        "language; identity_preserved belongs only on a line deliberately "
        "left in the source language, and its identity_reason must be "
        "exactly one of brand, proper_name, code, number_unit — no other "
        "word; leave identity_reason out entirely when identity_preserved "
        "is false; every number, unit, percentage and name in the source "
        "must appear in translated_text in the same order, and no number "
        "may appear that the source does not contain; repeat each "
        "caption_instance_id back exactly as given, including when only one "
        "caption is listed.\n\n"
    )


def _receipt_identity(receipt: Any) -> Any:
    """The receipt without this delivery's own bookkeeping.

    How many rounds it took is a record of what happened while producing
    this artifact, not part of who the provider is. The receipt is compared
    against one derived from the manifest to catch a swapped provider, and
    a retried delivery must not read as a different provider.
    """
    if not isinstance(receipt, dict):
        return receipt
    bookkeeping = set(SHORTENING_RECEIPT_KEYS) | set(VALIDATION_RECEIPT_KEYS)
    return {key: value for key, value in receipt.items() if key not in bookkeeping}


def _rendered_frame(
    state: dict[str, Any], overlays: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float]:
    """The captions and the scale the render will actually measure with.

    A caption is authored at the director's own `max_width` and cut at the
    narrower of that and what the platform leaves clear, and the narrowing
    happens on the render path — after this delivery has been paid for and
    hashed. Measuring the fit at the authored width therefore answers a
    question about a frame nobody draws: the twelve-caption Tainan cut
    reported every translation as fitting at 86 and then died at render with
    a third line "even at the 36px floor", because Reels leaves 84. The two
    measurements are taken of one column here, with one scale, or the
    shortening round §3 exists to spend is never spent.

    A host without the render module cannot narrow anything, so it measures
    what it has: the same verdict as before this existed, never a wider one.
    """
    try:
        from editor_server import platform_safe_area
        from render_editor_timeline import constrain_caption_wrap_to_safe_area, even
    except ImportError:  # pragma: no cover - host-dependent
        return overlays, 1.0
    canvas = state.get("canvas") if isinstance(state.get("canvas"), dict) else {}
    try:
        width = int(canvas.get("width", 1080))
    except (TypeError, ValueError):
        width = 1080
    # The deliverable is the final render, whose width is the canvas rounded
    # up to an even number of pixels — the one scale a shipped caption is
    # ever drawn at. A preview is cut smaller and is not what is being
    # guarded.
    scale = even(width) / width if width > 0 else 1.0
    return (
        constrain_caption_wrap_to_safe_area(overlays, platform_safe_area(state)),
        scale,
    )


def _overflowing_budgets(
    project_dir: Path,
    state: dict[str, Any],
    instances: list[dict[str, Any]],
    texts: list[str],
) -> dict[str, int]:
    """Instance id -> measured character budget, for the ones that overflow.

    Measured through the compositor, which is the only thing that knows what
    a line holds. A host without it cannot measure, and a guess here would be
    worse than not asking: it would spend a provider round on a budget no
    frame agrees with. So an unavailable compositor means no retry, and the
    render-time fail-closed stays exactly where it was.
    """
    try:
        import caption_compositor
    except ImportError:  # pragma: no cover - host-dependent
        return {}
    if not caption_compositor.compositor_available():
        return {}
    rendered, render_scale = _rendered_frame(state, _caption_overlays(state))
    overlays = {
        str(overlay.get("caption_source_id") or ""): overlay for overlay in rendered
    }
    canvas = state.get("canvas") if isinstance(state.get("canvas"), dict) else {}
    budgets: dict[str, int] = {}
    for instance, text in zip(instances, texts, strict=True):
        overlay = overlays.get(str(instance.get("caption_source_id") or ""))
        if overlay is None:
            continue
        try:
            fit = caption_compositor.translation_fit(
                project_dir, overlay, canvas, render_scale, state, translation=text
            )
        except (OSError, ValueError, RuntimeError, KeyError):
            # Measuring failed, so nothing was learned about this caption.
            # Leaving it alone keeps the existing behaviour rather than
            # inventing a budget from a measurement that did not happen.
            continue
        if not fit["fits"] and fit["reason"] == "secondary":
            budgets[instance["caption_instance_id"]] = fit["character_budget"]
    return budgets


def _latin_script_ratio(text: str) -> float | None:
    """Share of the letters that are Latin, or None when there are none.

    Digits, punctuation, spaces and emoji are counted on neither side:
    they say nothing about which language a line is written in, and a
    caption that is only "90" or only "🔥🔥🔥" is the same caption in every
    target. Those come back as None — no opinion — rather than as 0.
    """
    latin = len(LATIN_SCRIPT_RE.findall(text))
    cjk = len(CJK_SCRIPT_RE.findall(text))
    if latin + cjk == 0:
        return None
    return latin / (latin + cjk)


def _writes_in_target_script(source: str, translated: str, identity: bool) -> bool:
    """Whether this answer is written in a Latin-script target's script.

    SPEC Phase 3 v1 §4 (v1.5). The check the twelve-caption Tainan delivery
    walked straight through: qwen2.5:7b answered every `en` caption in
    Chinese, converted traditional to simplified — so the answers differed
    from their sources and `translation_unchanged` never fired — and
    stamped eight of them `identity_preserved=true, proper_name`, which
    excused the rest. The bilingual final shipped Chinese under Chinese.

    So the script is decided here, deterministically, instead of being
    taken on the model's word. The identity exemption survives for what it
    was written for and no further (v1.5 narrows v1.3): a line the model
    was right to leave alone is one whose *source* is already mostly Latin
    — a brand, a code, a Latin proper name, `S outh bound` — and echoing
    such a source verbatim is that same test passing. A source that is
    itself a Chinese sentence cannot be a name left alone, whatever the
    stamp says.
    """
    ratio = _latin_script_ratio(translated)
    if ratio is None or ratio >= MIN_TARGET_SCRIPT_RATIO:
        return True
    if not identity:
        return False
    source_ratio = _latin_script_ratio(source)
    return source_ratio is not None and source_ratio >= MIN_TARGET_SCRIPT_RATIO


def _judge_translation(
    expected: dict[str, Any],
    proposed: dict[str, Any],
    translated: str,
    instance_id: str,
    glossary_tokens: set[str],
    target: str,
) -> dict[str, Any]:
    """The verdict on one answer: the adopted item, or why not.

    Split out from the loop so a caller can hold a verdict per caption
    instead of only the first one, without any of the rules moving.
    """
    source = expected["corrected_source"].strip()
    identity = proposed.get("identity_preserved") is True
    reason = proposed.get("identity_reason")
    # An empty reason field is an unfilled field, not a claim. Local 7B
    # models routinely answer `"identity_reason": ""` alongside
    # `identity_preserved: false`, which says exactly what `null` says
    # and used to fail the delivery. Nothing is let through by reading
    # it as absent: the exemption hangs on identity_preserved, and a
    # reason that actually names something still contradicts a false
    # claim and is still rejected below.
    if isinstance(reason, str) and not reason.strip():
        reason = None
    if identity:
        if reason not in IDENTITY_REASONS:
            raise CaptionDeliveryError("translation_identity_invalid", instance_id)
    elif reason is not None:
        raise CaptionDeliveryError("translation_identity_invalid", instance_id)
    source_normalised = TRANSLATION_NORMALISE_RE.sub("", source).casefold()
    translated_normalised = TRANSLATION_NORMALISE_RE.sub("", translated).casefold()
    if (
        CHINESE_RE.search(source)
        and translated_normalised == source_normalised
        and not identity
    ):
        raise CaptionDeliveryError("translation_unchanged", instance_id)
    if target.split("-")[0].casefold() in LATIN_SCRIPT_TARGETS and not (
        _writes_in_target_script(source, translated, identity)
    ):
        raise CaptionDeliveryError("translation_wrong_language", instance_id)
    # Compared on the numbers, not on how they were typed: fullwidth
    # digits and thousands separators are flattened on both sides first
    # (a real cut died on `2000` vs `2,000`, a correct answer refused).
    normalised_source = normalise_numerals(source)
    source_matches = list(ASCII_TOKEN_RE.finditer(normalised_source))
    source_tokens = [match.group(0) for match in source_matches]
    delivered_tokens = ASCII_TOKEN_RE.findall(normalise_numerals(translated))
    # A single letter welded to a Chinese word is a stroke of that word,
    # not a term the translation owes back (see `_letter_is_fused_into_cjk`).
    # A glossary entry still overrules the exemption: naming the term is
    # the caller saying they want it carried.
    required = {
        _token_key(match.group(0))
        for match in source_matches
        if not _letter_is_fused_into_cjk(
            normalised_source, match.start(), match.end()
        )
    } | glossary_tokens.intersection(token.casefold() for token in source_tokens)
    delivered = {_token_key(token) for token in delivered_tokens}
    missing = sorted(required - delivered)
    if missing:
        raise CaptionDeliveryError("translation_token_missing", f"{instance_id}: {', '.join(missing)}")
    # Sets lose three things a caption cannot afford to lose: how many
    # times a value was said, in what order, and whether the answer
    # added one nobody said. So the numbers are compared as a sequence.
    #
    # Digits only, on the source side (v1.4, 2026-08-13). Reading the
    # numbers a caption spelled in Chinese is best-effort and best-effort
    # is not a basis for failing a delivery closed: correct English
    # answers this rule refused include 十年前 -> "10 years ago",
    # 前十名 -> "Top 10", 三分之一 -> "1/3", 十點三十分 -> "10:30" and
    # 千萬別忘記 -> "Do not forget". nat reviews the cut; a rule that
    # cries wolf on ordinary sentences gets turned off wholesale and then
    # protects nothing, including the digits it could actually read.
    source_values = source_arabic_number_sequence(source)
    delivered_values = translation_number_sequence(translated)
    missing_values = _sequence_difference(source_values, delivered_values)
    if missing_values:
        raise CaptionDeliveryError(
            "translation_token_missing",
            f"{instance_id}: {', '.join(missing_values)}",
        )
    # Symmetric to the above, and the part that is easy to get wrong: if
    # the source's numbers cannot be read, then neither "the answer
    # invented one" nor "the answer reordered them" can be claimed about
    # this caption — both accusations are statements about the source's
    # number sequence, which is exactly what is unavailable. Only captions
    # whose numbers are wholly ASCII stay convictable on these two, and
    # those keep both rules untouched.
    if not states_chinese_numerals(source):
        invented = _sequence_difference(delivered_values, source_values)
        if invented:
            # The direction nothing checked: a number on screen that the
            # speaker never said is not a translation error the viewer can
            # see, it is a fabricated fact in the speaker's mouth.
            raise CaptionDeliveryError(
                "translation_number_invented", f"{instance_id}: {', '.join(invented)}"
            )
        if source_values != delivered_values:
            # Same numbers, rearranged: "12 hours for 24 dollars" and
            # "24 hours for 12 dollars" carry identical tokens and opposite
            # meanings.
            raise CaptionDeliveryError(
                "translation_number_order",
                f"{instance_id}: {' '.join(source_values)} -> {' '.join(delivered_values)}",
            )
    return {
        "translated_text": translated,
        "translation_status": "identity_preserved" if identity else "translated",
        "identity_preserved": identity,
        "identity_reason": reason if identity else None,
    }


def _validate_translations(
    instances: list[dict[str, Any]],
    response: dict[str, Any],
    glossary: list[str],
    *,
    target: str = DEFAULT_VALIDATION_TARGET,
) -> list[dict[str, Any]]:
    """Every answer, or the first reason one of them is unacceptable."""
    results, failures = _validate_translations_with_failures(
        instances, response, glossary, target=target
    )
    if failures:
        raise failures[0][1]
    return [result for result in results if result is not None]


def _validate_translations_with_failures(
    instances: list[dict[str, Any]],
    response: dict[str, Any],
    glossary: list[str],
    *,
    target: str = DEFAULT_VALIDATION_TARGET,
) -> tuple[list[dict[str, Any] | None], list[tuple[int, CaptionDeliveryError]]]:
    """Judge every item, and say which ones failed rather than only the first.

    Same rules, same codes, same order — the difference is that a caller
    who intends to ask again learns *which* captions to ask about. Asking
    again about all of them turned out to be actively harmful: on a real
    delivery qwen2.5:7b answered nine captions well and two badly, and
    re-asking the whole set brought back eleven untranslated Chinese lines.

    Anything that is not attributable to one item — a response of the wrong
    shape, wrong length, or misaligned ids — is still raised, because there
    is no per-item verdict to give. An item-level failure already found
    outranks it, so that this reports exactly what the first-failure
    version reported.
    """
    raw = response.get("items")
    if set(response) != {"items"}:
        raise CaptionDeliveryError("translation_invalid", "provider response has unexpected fields")
    if not isinstance(raw, list) or len(raw) != len(instances):
        raise CaptionDeliveryError("translation_incomplete", "provider item count mismatch")
    output: list[dict[str, Any] | None] = []
    failures: list[tuple[int, CaptionDeliveryError]] = []
    seen: set[str] = set()

    def _structural(error: CaptionDeliveryError) -> CaptionDeliveryError:
        """The first item-level verdict wins over a later structural one."""
        return failures[0][1] if failures else error

    glossary_tokens = {token.casefold() for term in glossary for token in ASCII_TOKEN_RE.findall(str(term))}
    for index, (expected, proposed) in enumerate(zip(instances, raw, strict=True)):
        if not isinstance(proposed, dict):
            raise _structural(CaptionDeliveryError("translation_invalid", f"item {index} is not an object"))
        if not set(proposed).issubset(
            {"caption_instance_id", "translated_text", "identity_preserved", "identity_reason"}
        ):
            raise _structural(CaptionDeliveryError("translation_invalid", f"item {index} has unexpected fields"))
        instance_id = str(proposed.get("caption_instance_id") or "")
        if not instance_id and len(instances) == 1:
            # An id left out of a one-item exchange cannot be ambiguous:
            # one caption was asked about, one answer came back, and there
            # is no other caption it could belong to. The id exists to stop
            # answers being matched to the wrong caption, and with a single
            # item that mapping is forced.
            #
            # This is not theoretical tidying — it is what a per-caption
            # retry runs into. qwen2.5:7b echoes the id happily in a list
            # of six and drops it when the list has one, so the retried
            # answer was correct and thrown away for a missing field, three
            # rounds in a row, and the cut died. An id that is *present and
            # different* is still a mismatch: that one says the provider
            # answered about something else.
            instance_id = str(expected["caption_instance_id"])
        if instance_id != expected["caption_instance_id"]:
            raise _structural(CaptionDeliveryError("translation_order_mismatch", f"item {index}"))
        if instance_id in seen:
            raise _structural(CaptionDeliveryError("translation_duplicate", instance_id))
        seen.add(instance_id)
        translated = str(proposed.get("translated_text") or "").strip()
        if not translated:
            raise _structural(CaptionDeliveryError("translation_incomplete", instance_id))
        if len(translated) > MAX_CAPTION_TEXT_CHARS:
            raise _structural(CaptionDeliveryError("translation_invalid", f"{instance_id} text is too long"))
        try:
            output.append(
                _judge_translation(
                    expected, proposed, translated, instance_id, glossary_tokens, target
                )
            )
        except CaptionDeliveryError as exc:
            output.append(None)
            failures.append((index, exc))
    return output, failures


def _answers_carry_no_ids(
    instances: list[dict[str, Any]],
    response: dict[str, Any],
) -> bool:
    """Whether the answer is a full-length batch reply carrying no ids at all.

    The shape that has to be re-asked one caption at a time: as many answers
    as captions, every entry an object, and **not one** of them naming the
    caption it belongs to. There is nothing here to match an answer to a
    caption on, so nothing places them — see
    `_reask_each_caption_individually`.

    A *partly* identified answer is not this shape and is left strictly
    alone. One id present means the provider was tracking ids for at least
    that item, so the absent ones are missing information rather than a
    uniform omission, and the strict id match stands.
    """
    raw = response.get("items")
    if not isinstance(raw, list) or len(raw) != len(instances) or not raw:
        return False
    for item in raw:
        if not isinstance(item, dict) or str(item.get("caption_instance_id") or ""):
            return False
    return True


def _reask_each_caption_individually(
    instances: list[dict[str, Any]],
    *,
    prompt_for: Callable[[dict[str, Any]], str],
    model_call: Callable[..., dict[str, Any]],
    model: str,
    timeout: int,
    trust: _ProjectTrust,
    attempt: int,
) -> dict[str, Any]:
    """Ask about each caption on its own, and merge the answers back.

    The fallback when a batch cannot be read by position: an answer to a
    question that named exactly one caption cannot be misattributed, because
    there is no other caption it could belong to. The one-item carve-out for
    a missing id is safe for the same reason, and this reuses it rather than
    widening it.

    It costs one provider call per caption, which is why it is not the
    default. Nothing here judges a translation — the merged answer goes
    through exactly the same `_validate_translations_with_failures` as a
    batch reply, keeps its per-caption verdicts, and whatever still fails
    fails closed exactly as before.
    """
    items: list[dict[str, Any]] = []
    for instance in instances:
        try:
            response = model_call(
                prompt_for(instance),
                "caption_translation",
                model=model,
                timeout=timeout,
                attempt=attempt,
            )
        except CaptionDeliveryError:
            raise
        except Exception as exc:
            raise CaptionDeliveryError("translation_provider_failed", str(exc)[:500]) from exc
        # Same reason as the batch path: the blocking wait is where another
        # process can swap project paths, so trust is re-checked before any
        # returned data is kept.
        _verify_project_trust(trust)
        if not isinstance(response, dict) or set(response) != {"items"}:
            raise CaptionDeliveryError("translation_invalid", "provider response must be an object")
        raw = response["items"]
        if not isinstance(raw, list) or len(raw) != 1:
            raise CaptionDeliveryError("translation_incomplete", "provider item count mismatch")
        item = raw[0]
        if not isinstance(item, dict):
            raise CaptionDeliveryError("translation_invalid", "item 0 is not an object")
        given = str(item.get("caption_instance_id") or "")
        if given and given != instance["caption_instance_id"]:
            # An id that names a different caption is the provider answering
            # about something else, single question or not.
            raise CaptionDeliveryError(
                "translation_order_mismatch", str(instance["caption_instance_id"])
            )
        items.append({**item, "caption_instance_id": instance["caption_instance_id"]})
    return {"items": items}


def _caption_overlays(state: dict[str, Any]) -> list[dict[str, Any]]:
    overlays = state.get("overlays")
    if not isinstance(overlays, list):
        return []
    return [
        item
        for item in overlays
        if isinstance(item, dict)
        and item.get("type") == "caption"
        and item.get("visible", True)
        and item.get("source") == "working/transcript_words.json"
    ]


def _attribute_partial_response(
    pending: list[tuple[int, dict[str, Any]]],
    response: dict[str, Any],
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any], list[tuple[int, dict[str, Any]]]]:
    """Split a wrong-length answer into the captions it answered and the rest.

    Returns `(answered, response_for_answered, unanswered)`. An item that
    names a caption this round asked about is that caption's answer,
    whatever else shares the list with it. Everything else in the list
    names no caption that is in question and is dropped where it stands:
    an item with no id at all, an id from outside this round, and a repeat
    of an id already answered.

    Dropping rather than voiding is the whole point. A real cut re-asked
    two captions, got three items back — both captions named by id and
    correctly translated, plus one stray with no id — and threw the list
    away because of the stray, then spent the rest of `VALIDATION_MAX_ROUNDS`
    re-asking captions it already had and failed closed. One junk item is
    junk; it is not a verdict on the items that did name their caption.

    Nothing is ever placed by list position: position is not a claim about
    which caption an answer belongs to, so an unidentified item is never
    some caption's answer by proximity. When no item names a caption in
    question, nothing is split off and the caller's validator gives the
    same verdict it always gave — including the id-less full-length shape
    that is re-asked one caption at a time, which is left untouched here.
    A response that is not a list of objects is a malformed shape rather
    than a stray answer, and is likewise left to the validator.
    """
    unchanged = (pending, response, [])
    if set(response) != {"items"}:
        return unchanged
    raw = response.get("items")
    if not isinstance(raw, list) or len(raw) == len(pending):
        return unchanged
    wanted = {str(instance["caption_instance_id"]) for _index, instance in pending}
    by_id: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict):
            return unchanged
        instance_id = str(item.get("caption_instance_id") or "")
        # The first answer for a caption stands; a second one is the
        # provider contradicting itself, and letting the later item win
        # would let a stray repeat overwrite an answer already given.
        if instance_id in wanted and instance_id not in by_id:
            by_id[instance_id] = item
    answered = [
        entry for entry in pending if str(entry[1]["caption_instance_id"]) in by_id
    ]
    if not answered:
        return unchanged
    unanswered = [
        entry for entry in pending if str(entry[1]["caption_instance_id"]) not in by_id
    ]
    aligned = {
        "items": [by_id[str(instance["caption_instance_id"])] for _index, instance in answered]
    }
    return answered, aligned, unanswered


def caption_generation_decision(state: dict[str, Any]) -> tuple[bool, str]:
    """Whether this project draws captions at all, and why.

    `editor_server.caption_render_decision` writes this when the project is
    set up: a source that already carries burned-in subtitles gets no
    second set drawn over it. The transcript keeps its caption segments
    either way, which is why delivery has to read the decision rather than
    infer it from an empty overlay list.
    """
    decision = state.get("caption_generation")
    if not isinstance(decision, dict) or decision.get("enabled") is not False:
        return True, str(decision.get("reason") or "") if isinstance(decision, dict) else ""
    return False, str(decision.get("reason") or "caption generation is disabled")


def require_caption_overlays(state: dict[str, Any]) -> None:
    """Refuse a delivery for a project that draws no captions, by name.

    Comparing transcript sources against overlays that were never meant to
    exist reported `caption overlay count mismatch`: true about the numbers
    and wrong about the cause, and it cost two real cuts before anyone read
    the state. The count check below still owns every other disagreement —
    this only speaks when the state says there are no captions *and* there
    are none.
    """
    enabled, reason = caption_generation_decision(state)
    if not enabled and not _caption_overlays(state):
        raise CaptionDeliveryError("caption_generation_disabled", reason)


def _bind_sources_to_state(state: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    overlays = _caption_overlays(updated)
    if len(overlays) != len(sources):
        raise CaptionDeliveryError("caption_binding_missing", "caption overlay count mismatch")
    for index, (overlay, source) in enumerate(zip(overlays, sources, strict=True)):
        try:
            start_us = _seconds_us(overlay.get("start"), "overlay start")
            end_us = _seconds_us(overlay.get("end"), "overlay end")
        except CaptionDeliveryError as exc:
            raise CaptionDeliveryError("caption_binding_missing", str(exc)) from exc
        if (
            str(overlay.get("text") or "").strip() != source["text"]
            or start_us != source["source_start_us"]
            or end_us != source["source_end_us"]
        ):
            raise CaptionDeliveryError("caption_binding_missing", f"overlay {index} source mismatch")
        existing = overlay.get("caption_source_id")
        if existing not in {None, "", source["caption_source_id"]}:
            raise CaptionDeliveryError("caption_binding_missing", f"overlay {index} has stale source id")
        overlay["caption_source_id"] = source["caption_source_id"]
    return updated


def _translate_and_adopt(
    trust: _ProjectTrust,
    *,
    manifest: dict[str, Any],
    bound_state: dict[str, Any],
    expected: dict[str, Any],
    target: str,
    required: bool,
    timeout: int,
    model: str,
    receipt: dict[str, Any],
    model_call: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    subtitles = manifest.get("subtitles") if isinstance(manifest.get("subtitles"), dict) else {}
    glossary = subtitles.get("glossary") if isinstance(subtitles.get("glossary"), list) else []
    # A rejected answer is asked again with the rejection in the prompt, up
    # to VALIDATION_MAX_ROUNDS times, and then the rejection stands. The
    # provider is a sampling model: the first answer being wrong says much
    # less about whether it can meet the contract than three do, and one
    # bad sample used to cost the whole cut. Nothing here loosens what
    # counts as wrong — the retried answer goes through exactly the same
    # `_validate_translations` that turned the last one down.
    #
    # Only the captions that were turned down are asked about again. The
    # first attempt at this re-asked the whole set and made deliveries
    # worse, not better: qwen2.5:7b answered nine of eleven captions well,
    # and a re-ask that included the nine came back with eleven
    # untranslated Chinese lines. A caption that already passed is kept and
    # never shown to the provider again.
    adopted: list[dict[str, Any] | None] = [None] * len(expected["instances"])
    pending = list(enumerate(expected["instances"]))
    prompt = _translation_prompt([instance for _index, instance in pending], target)
    validation_rounds = 0
    individual_reask = False
    while True:
        try:
            response = model_call(
                prompt,
                "caption_translation",
                model=model,
                timeout=timeout,
                # Which try this is, so a provider pinned to a fixed seed
                # can draw a different sample instead of returning the
                # rejected answer verbatim.
                attempt=validation_rounds,
            )
        except CaptionDeliveryError:
            raise
        except Exception as exc:
            raise CaptionDeliveryError("translation_provider_failed", str(exc)[:500]) from exc
        # The blocking provider wait is the point where another process can
        # swap project paths.  Reject before validating or adopting any
        # returned data — including before deciding whether to ask again,
        # so a retry is never issued against a project that moved.
        _verify_project_trust(trust)
        if not isinstance(response, dict):
            raise CaptionDeliveryError("translation_invalid", "provider response must be an object")
        asked = [instance for _index, instance in pending]
        # A list of the wrong length is not a verdict on any one caption,
        # and it used to end the delivery where it stood: a real cut died
        # with `provider item count mismatch` after one ask, with the
        # caption the provider *did* answer answered correctly. Split it
        # instead — the answers that name a caption asked about are judged
        # by exactly the same validator, and the captions left out become
        # the question for the next round, under the same ceiling.
        answered, aligned, unanswered = _attribute_partial_response(pending, response)
        # An answer with no ids anywhere in it is neither adopted nor
        # refused. Order is not a statement about which answer belongs to
        # which caption — a provider that reverses two lines and drops the
        # ids produces exactly the same bytes as one that answered in order
        # — so it is asked again, one caption per question, where a single
        # answer has only one caption it could be about. Whatever comes back
        # is validated exactly as a batch reply is, and still fails closed
        # if it fails.
        answered_instances = [instance for _index, instance in answered]
        if _answers_carry_no_ids(answered_instances, aligned):
            aligned = _reask_each_caption_individually(
                answered_instances,
                prompt_for=lambda instance: _translation_prompt([instance], target),
                model_call=model_call,
                model=model,
                timeout=timeout,
                trust=trust,
                attempt=validation_rounds,
            )
            individual_reask = True
        try:
            results, failures = _validate_translations_with_failures(
                [instance for _index, instance in answered],
                aligned,
                glossary,
                target=target,
            )
        except CaptionDeliveryError as exc:
            # Not a verdict on any one caption — a response of the wrong
            # shape or with the ids out of order. Worth one more ask for
            # the same reason a violation is, and about the same captions.
            if (
                exc.code not in CONTRACT_VIOLATION_CODES
                or validation_rounds >= VALIDATION_MAX_ROUNDS
            ):
                raise
            validation_rounds += 1
            prompt = _revalidation_prompt(asked, target, [exc])
            continue
        for (index, _instance), result in zip(answered, results, strict=True):
            if result is not None:
                adopted[index] = result
        reasons = [exc for _position, exc in failures]
        if unanswered:
            reasons.append(
                CaptionDeliveryError("translation_incomplete", "provider item count mismatch")
            )
        pending = [answered[position] for position, _exc in failures] + unanswered
        if not pending:
            break
        if validation_rounds >= VALIDATION_MAX_ROUNDS:
            # The ceiling: the verdict on the first caption still failing
            # stands, and the delivery fails closed exactly as it did
            # before any of this existed. With nothing but silence to go
            # on, the verdict is that silence.
            raise failures[0][1] if failures else reasons[-1]
        validation_rounds += 1
        prompt = _revalidation_prompt(
            [instance for _index, instance in pending],
            target,
            reasons,
        )
    translations = [item for item in adopted if item is not None]

    # SPEC Phase 3 v1 §3 step 3, between wrapping and failing closed: the
    # captions whose translation still needs a third line at its floor size
    # are asked for again, once, with the budget the compositor measured for
    # them. Everything else is left untouched — a translation that fits does
    # not cost a second provider round.
    budgets = _overflowing_budgets(
        trust.root,
        bound_state,
        expected["instances"],
        [item["translated_text"] for item in translations],
    )
    rounds = 0
    if budgets:
        rounds = SHORTENING_MAX_ROUNDS
        retry_instances = [
            instance
            for instance in expected["instances"]
            if instance["caption_instance_id"] in budgets
        ]
        shortening_prompt = _shortening_prompt(retry_instances, target, budgets)
        shortening_rounds = 0
        while True:
            try:
                retry_response = model_call(
                    shortening_prompt,
                    "caption_translation",
                    model=model,
                    timeout=timeout,
                )
            except CaptionDeliveryError:
                raise
            except Exception as exc:
                raise CaptionDeliveryError("translation_provider_failed", str(exc)[:500]) from exc
            _verify_project_trust(trust)
            if not isinstance(retry_response, dict):
                raise CaptionDeliveryError(
                    "translation_invalid", "provider response must be an object"
                )
            # The shorter answer earns no exemption: same validation, same
            # identity rules. A retry is where a provider is most tempted to
            # drop the unit to make the length.
            # The shortening round is a second question to the same provider,
            # which drops ids the same way, so it gets the same treatment for
            # the same reason: an id-less answer is asked again one caption at a
            # time rather than placed on the order it came back in.
            if _answers_carry_no_ids(retry_instances, retry_response):
                retry_response = _reask_each_caption_individually(
                    retry_instances,
                    prompt_for=lambda instance: _shortening_prompt(
                        [instance],
                        target,
                        {
                            instance["caption_instance_id"]: budgets[
                                instance["caption_instance_id"]
                            ]
                        },
                    ),
                    model_call=model_call,
                    model=model,
                    timeout=timeout,
                    trust=trust,
                    attempt=0,
                )
                individual_reask = True
            try:
                shortened = _validate_translations(
                    retry_instances, retry_response, glossary, target=target
                )
                break
            except CaptionDeliveryError as exc:
                # The same second chance the first round gets, for the same
                # reason and under the same ceiling: this is one sample from a
                # 7B model, and the first delivery to reach this branch on real
                # material came back in simplified Chinese — for a caption
                # whose first-round answer had been correct English. Nothing is
                # relaxed; the resampled answer faces this identical validator,
                # and the last verdict still fails the delivery closed.
                if (
                    exc.code not in SHORTENING_REASK_CODES
                    or shortening_rounds >= VALIDATION_MAX_ROUNDS
                ):
                    raise
                shortening_rounds += 1
                shortening_prompt = _revalidation_preface(
                    [exc], target
                ) + _shortening_prompt(retry_instances, target, budgets)
        validation_rounds += shortening_rounds
        by_instance = {
            instance["caption_instance_id"]: value
            for instance, value in zip(retry_instances, shortened, strict=True)
        }
        translations = [
            by_instance.get(instance["caption_instance_id"], value)
            for instance, value in zip(expected["instances"], translations, strict=True)
        ]
    # The receipt is built after the answers are final, so the artifact is
    # hashed once, over what will actually be drawn. A retry never rewrites
    # already-hashed bytes; there are none yet.
    receipt = {
        **receipt,
        "shortening_rounds": rounds,
        "shortening_character_budgets": dict(sorted(budgets.items())),
        "validation_retry_rounds": validation_rounds,
        "individual_reask": individual_reask,
    }
    items: list[dict[str, Any]] = []
    for instance, translated in zip(expected["instances"], translations, strict=True):
        item = dict(instance)
        item.update(translated)
        item["source_list_hash"] = expected["source_list_hash"]
        item["cut_map_sha256"] = expected["cut_map_sha256"]
        item["timeline_revision"] = expected["timeline_revision"]
        item["target_language"] = target
        item["provider_receipt"] = receipt
        items.append(item)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": expected["segmentation"]["source_revision"],
        "segmentation_revision": expected["segmentation"]["segmentation_revision"],
        "source_list_hash": expected["source_list_hash"],
        "cut_map_sha256": expected["cut_map_sha256"],
        "timeline_revision": expected["timeline_revision"],
        "target_language": target,
        "required": required,
        "provider_receipt": receipt,
        "items": items,
    }
    errors = contract_registry.validate_artifact("caption_delivery", artifact)
    if errors:
        raise CaptionDeliveryError("caption_contract_invalid", "; ".join(errors))

    artifact_sha256 = hashlib.sha256(canonical_bytes(artifact)).hexdigest()
    translated_by_source: dict[str, str] = {}
    for item in items:
        translated_by_source.setdefault(item["caption_source_id"], item["translated_text"])
    for overlay in _caption_overlays(bound_state):
        source_id = str(overlay.get("caption_source_id") or "")
        if source_id in translated_by_source:
            overlay["translation"] = translated_by_source[source_id]
        else:
            # A caption the cut does not use. The transcript covers the
            # whole recording and the timeline covers what was kept, so an
            # editor state normally holds overlays no segment touches —
            # `expected_instances` never asks the provider about them and
            # there is no translation to bind. This used to index straight
            # into the map and take the run down with a KeyError.
            #
            # Removed rather than left as it was: an overlay carrying a
            # translation from an older delivery would be a line nobody
            # delivered for this timeline, and the state is about to be
            # stamped with this delivery's hash.
            overlay.pop("translation", None)
        overlay["caption_delivery_artifact_sha256"] = artifact_sha256
    bound_state["caption_delivery"] = {
        "schema_version": 2,
        "artifact": CAPTION_REL.as_posix(),
        "artifact_sha256": artifact_sha256,
        "timeline_revision": expected["timeline_revision"],
    }
    try:
        from editor_server import editor_state_revision

        bound_state["revision"] = editor_state_revision(bound_state)
    except ImportError:
        pass
    translation_config = subtitles.setdefault("translation", {})
    translation_config.update(
        {
            "required": required,
            "target_language": target,
            "provider": "ollama",
            "model": model,
            "artifact": CAPTION_REL.as_posix(),
            "provider_receipt": receipt,
        }
    )
    approvals = manifest.setdefault("approvals", {})
    for gate in ("timeline", "final"):
        approvals[gate] = {
            "approved": False,
            "confirmed_by": None,
            "at": None,
            "note": "Invalidated because caption delivery changed",
        }

    # Every replace is relative to a directory descriptor opened before the
    # provider wait.  A path swap therefore cannot redirect writes outside;
    # the identity check before each replace turns a swap into a hard failure.
    _atomic_write_at(
        trust,
        working=True,
        name=SEGMENTATION_REL.name,
        payload=expected["segmentation"],
    )
    _atomic_write_at(trust, working=True, name=CAPTION_REL.name, payload=artifact)
    _atomic_write_at(
        trust,
        working=True,
        name="editor_state.json",
        payload=bound_state,
    )
    _atomic_write_at(trust, working=False, name="project.json", payload=manifest)
    return artifact


def create_delivery(
    project_dir: Path,
    target: str,
    *,
    required: bool,
    timeout: int = 300,
    model_call: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate and atomically adopt caption v2 after every check passes."""
    if type(required) is not bool:
        raise CaptionDeliveryError("caption_contract_invalid", "required must be boolean")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise CaptionDeliveryError("translation_provider_unsupported", "timeout must be 1..3600")
    if not TARGET_RE.fullmatch(target):
        raise CaptionDeliveryError("translation_target_invalid", target)
    root = project_dir.resolve()
    _trusted_working_root(root)
    manifest = _load_json(_owned_artifact(root, Path("project.json")))
    transcript = _load_json(_owned_artifact(root, Path("working/transcript_words.json")))
    if not isinstance(manifest, dict) or not isinstance(transcript, dict):
        raise CaptionDeliveryError("caption_artifact_invalid", "project transcript missing")
    _provider, model, _config = _provider_config(manifest)  # consent/provider preflight before mutation
    receipt = provider_receipt(manifest)
    state = _load_json(_owned_artifact(root, Path("working/editor_state.json")))
    if not isinstance(state, dict):
        raise CaptionDeliveryError("caption_timeline_invalid", "editor state missing")
    require_caption_overlays(state)
    expected = expected_instances(root, transcript, state)
    bound_state = _bind_sources_to_state(state, expected["sources"])
    if model_call is None:
        from contextual_semantic_calibration import ollama_json_model_call

        model_call = ollama_json_model_call
    with _project_trust(root) as trust:
        return _translate_and_adopt(
            trust,
            manifest=manifest,
            bound_state=bound_state,
            expected=expected,
            target=target,
            required=required,
            timeout=timeout,
            model=model,
            receipt=receipt,
            model_call=model_call,
        )


def caption_v2_required(manifest: dict[str, Any]) -> bool:
    subtitles = manifest.get("subtitles")
    translation = subtitles.get("translation") if isinstance(subtitles, dict) else None
    return bool(isinstance(translation, dict) and translation.get("required") is True)


def validate_for_render(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Reload and bind v2 before direct staging. Legacy/non-required is unchanged."""
    if not caption_v2_required(manifest):
        return None, state
    root = project_dir.resolve()
    path = root / CAPTION_REL
    if path.is_symlink() or not path.is_file():
        raise CaptionDeliveryError("caption_binding_missing", "caption v2 artifact missing")
    artifact, artifact_bytes = _load_owned_json_bytes(root, CAPTION_REL)
    live_artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    state_binding = state.get("caption_delivery")
    adopted_sha256 = (
        str(state_binding.get("artifact_sha256") or "")
        if isinstance(state_binding, dict)
        else ""
    )
    if not SHA_RE.fullmatch(adopted_sha256) or live_artifact_sha256 != adopted_sha256:
        raise CaptionDeliveryError(
            "caption_binding_missing", "live caption artifact hash differs from adopted state"
        )
    for overlay in _caption_overlays(state):
        if overlay.get("caption_delivery_artifact_sha256") != adopted_sha256:
            raise CaptionDeliveryError(
                "caption_binding_missing", "caption overlay artifact hash is stale"
            )
    errors = contract_registry.validate_artifact("caption_delivery", artifact)
    if errors:
        raise CaptionDeliveryError("caption_binding_missing", "; ".join(errors))
    subtitles = manifest.get("subtitles") if isinstance(manifest.get("subtitles"), dict) else {}
    translation = subtitles.get("translation") if isinstance(subtitles.get("translation"), dict) else {}
    if artifact.get("required") is not True:
        raise CaptionDeliveryError("caption_binding_missing", "required artifact is not marked required")
    if artifact.get("target_language") != translation.get("target_language"):
        raise CaptionDeliveryError("caption_binding_missing", "target language is stale")
    provider = str(translation.get("provider") or "")
    model = str(translation.get("model") or "")
    stored_receipt = translation.get("provider_receipt")
    if provider != "ollama" or model != artifact.get("provider_receipt", {}).get("model"):
        raise CaptionDeliveryError("translation_provider_unsupported", provider)
    current_receipt = provider_receipt(manifest)
    if (
        _receipt_identity(artifact.get("provider_receipt")) != current_receipt
        or _receipt_identity(stored_receipt) != current_receipt
    ):
        raise CaptionDeliveryError("caption_binding_missing", "provider receipt is stale")
    transcript = _load_json(_owned_artifact(root, Path("working/transcript_words.json")))
    expected = expected_instances(root, transcript, state)
    segmentation = _load_json(_owned_artifact(root, SEGMENTATION_REL))
    segmentation_errors = contract_registry.validate_artifact(
        "caption_segmentation", segmentation
    )
    if segmentation_errors or segmentation != expected["segmentation"]:
        detail = "; ".join(segmentation_errors) if segmentation_errors else "payload mismatch"
        raise CaptionDeliveryError("caption_binding_missing", f"segmentation artifact: {detail}")
    bound_state = _bind_sources_to_state(state, expected["sources"])
    root_fields = {
        "source_revision": expected["segmentation"]["source_revision"],
        "segmentation_revision": expected["segmentation"]["segmentation_revision"],
        "source_list_hash": expected["source_list_hash"],
        "cut_map_sha256": expected["cut_map_sha256"],
        "timeline_revision": expected["timeline_revision"],
    }
    for key, value in root_fields.items():
        if artifact.get(key) != value:
            raise CaptionDeliveryError("caption_binding_missing", f"stale {key}")
    actual_items = artifact.get("items")
    if not isinstance(actual_items, list) or len(actual_items) != len(expected["instances"]):
        raise CaptionDeliveryError("caption_binding_missing", "caption instance count mismatch")
    for index, (actual, wanted) in enumerate(zip(actual_items, expected["instances"], strict=True)):
        if not isinstance(actual, dict):
            raise CaptionDeliveryError("caption_binding_missing", f"item {index} invalid")
        for key in (
            "caption_source_id",
            "caption_instance_id",
            "occurrence_ordinal",
            "corrected_source",
            "corrected_source_sha256",
            "source_start_us",
            "source_end_us",
            "final_start_us",
            "final_end_us",
            "source_revision",
            "segmentation_revision",
        ):
            if actual.get(key) != wanted.get(key):
                raise CaptionDeliveryError("caption_binding_missing", f"item {index} {key} mismatch")
        for key, value in root_fields.items():
            item_key = "source_list_hash" if key == "source_list_hash" else key
            if actual.get(item_key) != value:
                raise CaptionDeliveryError("caption_binding_missing", f"item {index} stale {item_key}")
    bound_state["_caption_delivery_v2"] = {
        "artifact_sha256": live_artifact_sha256,
        "items": copy.deepcopy(actual_items),
    }
    return artifact, bound_state


def render_item_map(state: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    binding = state.get("_caption_delivery_v2")
    items = binding.get("items") if isinstance(binding, dict) else None
    if not isinstance(items, list):
        return {}
    return {
        (
            str(item.get("caption_source_id") or ""),
            int(item.get("final_start_us")),
            int(item.get("final_end_us")),
        ): item
        for item in items
        if isinstance(item, dict)
    }

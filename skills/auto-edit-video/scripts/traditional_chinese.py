#!/usr/bin/env python3
"""Deterministic Taiwan Traditional Chinese orthography normalization."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from vendor.opencc import OpenCC


ORTHOGRAPHY_VARIANT = "zh-TW"
OPENCC_CONFIGURATION = "s2twp"
OPENCC_BACKEND = "vendored-opencc-python-reimplemented-0.1.7"


@lru_cache(maxsize=1)
def _converter() -> OpenCC:
    return OpenCC(OPENCC_CONFIGURATION)


@lru_cache(maxsize=1)
def _mixed_phrase_alias_data() -> tuple[dict[str, str], int, int]:
    """Build safe aliases for half-converted phrases such as ``聯系``.

    Whisper and prior correction passes can produce a phrase where one Han
    character is already Traditional while another ambiguous character still
    has its Simplified form. OpenCC's normal phrase matcher expects the whole
    Simplified phrase, so derive the character-converted source spelling for
    each official phrase and map only aliases the normal converter misses.
    """

    dictionary_dir = Path(__file__).resolve().parent / "vendor/opencc/dictionary"

    def dictionary(path: Path, *, first_variant: bool) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            source, target = line.split("\t", 1)
            values[source] = target.split()[0] if first_variant else target
        return values

    characters = dictionary(
        dictionary_dir / "STCharacters.txt",
        first_variant=True,
    )
    phrases = dictionary(
        dictionary_dir / "STPhrases.txt",
        first_variant=False,
    )
    aliases: dict[str, str | None] = {}
    converter = _converter()
    for source in phrases:
        alias = "".join(characters.get(character, character) for character in source)
        if alias == source:
            continue
        final = converter.convert(source)
        if converter.convert(alias) == final:
            continue
        existing = aliases.get(alias)
        if existing is not None and existing != final:
            aliases[alias] = None
        elif alias not in aliases:
            aliases[alias] = final
    resolved = {
        alias: target
        for alias, target in aliases.items()
        if isinstance(target, str) and target
    }
    lengths = [len(alias) for alias in resolved]
    return resolved, min(lengths, default=1), max(lengths, default=1)


def _repair_mixed_script_phrases(text: str) -> str:
    aliases, minimum, maximum = _mixed_phrase_alias_data()
    if not text or not aliases:
        return text
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        upper = min(maximum, len(text) - cursor)
        replacement: str | None = None
        matched_length = 0
        for length in range(upper, minimum - 1, -1):
            replacement = aliases.get(text[cursor : cursor + length])
            if replacement is not None:
                matched_length = length
                break
        if replacement is None:
            output.append(text[cursor])
            cursor += 1
            continue
        output.append(replacement)
        cursor += matched_length
    return "".join(output)


def to_taiwan_traditional(text: str) -> str:
    """Convert Simplified/standard Chinese to Taiwan Traditional Chinese.

    OpenCC only rewrites matching Chinese words and characters, so Latin text,
    punctuation, numbers, and timestamp data pass through unchanged.
    """

    value = _repair_mixed_script_phrases(str(text))
    return _converter().convert(value)


def _redistribute_converted_text(
    original_chunks: list[str],
    converted_text: str,
) -> list[str]:
    """Project phrase-aware conversion back onto Whisper word boundaries.

    Whisper often splits a phrase into one-character words. Converting each
    word independently loses phrase context (``联`` + ``系`` used to become
    ``聯`` + ``系``). Convert the complete segment word stream, then map the
    result back to the original timed word boundaries. Most OpenCC mappings
    preserve length; the alignment branch also handles localized expansions
    or contractions without changing timestamps.
    """

    original_text = "".join(original_chunks)
    if len(original_text) == len(converted_text):
        result: list[str] = []
        cursor = 0
        for chunk in original_chunks:
            end = cursor + len(chunk)
            result.append(converted_text[cursor:end])
            cursor = end
        return result

    boundaries = [0]
    for chunk in original_chunks:
        boundaries.append(boundaries[-1] + len(chunk))

    opcodes = SequenceMatcher(
        None,
        original_text,
        converted_text,
        autojunk=False,
    ).get_opcodes()

    def project_boundary(boundary: int) -> int:
        for tag, source_start, source_end, target_start, target_end in opcodes:
            if source_start <= boundary <= source_end:
                if tag == "equal":
                    return target_start + boundary - source_start
                source_width = source_end - source_start
                if source_width <= 0:
                    return target_end
                offset = boundary - source_start
                target_width = target_end - target_start
                return target_start + round(offset * target_width / source_width)
        return len(converted_text)

    projected = [project_boundary(boundary) for boundary in boundaries]
    projected[0] = 0
    projected[-1] = len(converted_text)
    for index in range(1, len(projected)):
        projected[index] = max(projected[index - 1], projected[index])
    return [
        converted_text[projected[index] : projected[index + 1]]
        for index in range(len(original_chunks))
    ]


def should_normalize_taiwan_traditional(
    manifest: dict[str, Any],
    detected_language: str | None,
) -> bool:
    """Resolve whether the source transcript should use Taiwan orthography."""

    subtitles = manifest.get("subtitles")
    if not isinstance(subtitles, dict):
        subtitles = {}
    source_language = str(subtitles.get("source_language") or "auto")
    detected = str(detected_language or "").lower()

    # An explicitly Mainland-Chinese source is the one supported opt-out.
    if source_language == "zh-CN":
        return False
    if source_language in {"zh-TW", "zh-en"}:
        return True
    if source_language == "auto" and detected.startswith("zh"):
        return True

    target = str(subtitles.get("target_language") or "").lower()
    translation_variant = str(subtitles.get("translation_variant") or "").lower()
    source_contains_chinese = source_language.startswith("zh") or detected.startswith("zh")
    return source_contains_chinese and (
        target in {"zh-tw", "zh-hant"} or translation_variant == "zh-hant"
    )


def normalize_whisper_orthography(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize all Whisper transcript truth fields in place and return stats."""

    changed_strings = 0
    changed_characters = 0

    def convert_field(container: dict[str, Any], key: str) -> None:
        nonlocal changed_strings, changed_characters
        value = container.get(key)
        if not isinstance(value, str):
            return
        converted = to_taiwan_traditional(value)
        if converted == value:
            return
        container[key] = converted
        changed_strings += 1
        common = min(len(value), len(converted))
        changed_characters += sum(
            1 for index in range(common) if value[index] != converted[index]
        ) + abs(len(value) - len(converted))

    convert_field(data, "text")
    for segment in data.get("segments", []):
        if not isinstance(segment, dict):
            continue
        convert_field(segment, "text")
        word_fields: list[tuple[dict[str, Any], str, str]] = []
        for word in segment.get("words", []):
            if not isinstance(word, dict):
                continue
            key = "word" if "word" in word else "text" if "text" in word else None
            if key is None or not isinstance(word.get(key), str):
                continue
            word_fields.append((word, key, str(word[key])))
        if not word_fields:
            continue
        source_chunks = [value for _, _, value in word_fields]
        converted_chunks = _redistribute_converted_text(
            source_chunks,
            to_taiwan_traditional("".join(source_chunks)),
        )
        for (word, key, source), converted in zip(word_fields, converted_chunks):
            if converted == source:
                continue
            word[key] = converted
            changed_strings += 1
            common = min(len(source), len(converted))
            changed_characters += sum(
                1 for index in range(common) if source[index] != converted[index]
            ) + abs(len(source) - len(converted))

    return {
        "variant": ORTHOGRAPHY_VARIANT,
        "configuration": OPENCC_CONFIGURATION,
        "backend": OPENCC_BACKEND,
        "changed_strings": changed_strings,
        "changed_characters": changed_characters,
    }

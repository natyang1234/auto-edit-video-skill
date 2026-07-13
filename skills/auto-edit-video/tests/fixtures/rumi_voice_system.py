"""Deterministic, offline Rumi voice-catalog fixture for tests."""

from __future__ import annotations


DEFAULT_ZH = "rumi"
DEFAULT_EN = "en-US-JennyNeural"

_VOICES = {
    "rumi": ("zh", "female", "offline test fixture: Chinese female"),
    "溫暖磁性男聲旁白": ("zh", "male", "offline test fixture: Chinese male"),
    "en-US-JennyNeural": ("en-US", "female", "offline test fixture: English female"),
    "en-US-AndrewNeural": ("en-US", "male", "offline test fixture: English male"),
}


def catalog() -> dict[str, tuple[str, str, str]]:
    return dict(_VOICES)


def is_allowed(voice_id: str) -> bool:
    return voice_id in _VOICES


def _engine_of(voice_id: str) -> str:
    if voice_id not in _VOICES:
        raise ValueError(f"unknown fixture voice: {voice_id}")
    return "fish" if voice_id in {"rumi", "溫暖磁性男聲旁白"} else "edge"

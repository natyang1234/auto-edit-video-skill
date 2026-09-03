#!/usr/bin/env python3
"""Single source of truth for SFX role/category/asset identity.

Pure constants and pure functions only.  No filesystem or network I/O, and
no import of ``contract_registry`` or ``sfx_delivery`` (both of those import
this module instead, so an import cycle here would be a bug).
"""
from __future__ import annotations

SFX_CATEGORIES = (
    "cash_coin",
    "click_pop",
    "whoosh",
    "impact_hit",
    "success_ding",
    "error_glitch",
)

ROLE_TO_CATEGORY = {
    "title_enter": "click_pop",
    "row_reveal": "click_pop",
    "count_tick": "click_pop",
    "typing": "click_pop",
    "transition": "whoosh",
    "grid_fill": "impact_hit",
    "complete": "success_ding",
}

STARTER_PACK_ID = "phase1-local-procedural"

STARTER_ASSET_IDS = (
    "soft-ui-tick-v1",
    "short-pop-v1",
    "short-whoosh-v1",
    "soft-impact-v1",
    "short-riser-v1",
    "typing-tick-v1",
    "completion-chime-v1",
)

STARTER_ASSET_ROLES = {
    "soft-ui-tick-v1": "title_enter",
    "short-pop-v1": "row_reveal",
    "short-whoosh-v1": "transition",
    "soft-impact-v1": "grid_fill",
    "short-riser-v1": "count_tick",
    "typing-tick-v1": "typing",
    "completion-chime-v1": "complete",
}

STARTER_ASSET_FILENAMES = {
    "soft-ui-tick-v1": "generated-soft-ui-tick.wav",
    "short-pop-v1": "generated-short-pop-v1.wav",
    "short-whoosh-v1": "generated-short-whoosh-v1.wav",
    "soft-impact-v1": "generated-soft-impact-v1.wav",
    "short-riser-v1": "generated-short-riser-v1.wav",
    "typing-tick-v1": "generated-typing-tick-v1.wav",
    "completion-chime-v1": "generated-completion-chime-v1.wav",
}

# asset_id -> (generator.name, generator.recipe), version is always 1.
STARTER_ASSET_GENERATORS = {
    "soft-ui-tick-v1": ("soft_ui_tick", "two_sine_exponential_decay"),
    "short-pop-v1": ("short_pop", "sine_pop_exponential_decay"),
    "short-whoosh-v1": ("short_whoosh", "swept_sine_fade"),
    "soft-impact-v1": ("soft_impact", "low_tone_exponential_decay"),
    "short-riser-v1": ("short_riser", "swept_sine_rise"),
    "typing-tick-v1": ("typing_tick", "short_sine_exponential_decay"),
    "completion-chime-v1": ("completion_chime", "two_note_exponential_decay"),
}

STARTER_ASSET_CATEGORIES = {
    asset_id: ROLE_TO_CATEGORY[role] for asset_id, role in STARTER_ASSET_ROLES.items()
}

# v2-only procedural assets that fill categories the seven-asset starter pack
# does not cover.  These must never be added to STARTER_ASSET_IDS: the v1
# "exactly seven" catalog closure is a public contract and stays frozen.
V2_EXTRA_PROCEDURAL_ASSET_IDS = (
    "coin-blip-v1",
    "glitch-buzz-v1",
)

V2_EXTRA_ASSET_CATEGORIES = {
    "coin-blip-v1": "cash_coin",
    "glitch-buzz-v1": "error_glitch",
}

V2_EXTRA_ASSET_GENERATORS = {
    "coin-blip-v1": ("coin_blip", "double_sine_ping"),
    "glitch-buzz-v1": ("glitch_buzz", "square_burst_exponential_decay"),
}

V2_PROCEDURAL_ASSET_IDS = STARTER_ASSET_IDS + V2_EXTRA_PROCEDURAL_ASSET_IDS

V2_PROCEDURAL_ASSET_GENERATORS = {
    **STARTER_ASSET_GENERATORS,
    **V2_EXTRA_ASSET_GENERATORS,
}

V2_PROCEDURAL_ASSET_CATEGORIES = {
    **STARTER_ASSET_CATEGORIES,
    **V2_EXTRA_ASSET_CATEGORIES,
}

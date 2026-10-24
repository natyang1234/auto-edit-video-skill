#!/usr/bin/env python3
"""Generate checked-in style packs from palette-catalog colour themes.

Each theme from ``palette-catalog`` (``pal.py theme <id>``) becomes two packs:

* ``theme-<id>-paper`` – light paper cards, ink text, dark captions with a
  paper outline.
* ``theme-<id>-navy`` – dark cards, light text, light captions with a dark
  outline.

Titles and stat values use the serif display face.  The generated files and
``contracts/theme_packs.manifest.json`` are written next to the three original
packs; ``structured_card_compositor`` registers exactly the ids listed in
that manifest, so a pack only becomes selectable after it is generated here.

Usage::

    python3 theme_packs.py build              # the palette-catalog presets
    python3 theme_packs.py build 075 021      # specific themes
    python3 theme_packs.py list
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
INSTANCES = SKILL_ROOT / "contracts" / "instances"
MANIFEST = SKILL_ROOT / "contracts" / "theme_packs.manifest.json"
TEMPLATE = INSTANCES / "style_pack__editorial_paper.json"
PALETTE_CATALOG = Path.home() / ".claude" / "skills" / "palette-catalog"
THEME_ID_RE = re.compile(r"^theme-[a-z0-9]+-(paper|navy)$")
FILE_RE = re.compile(r"^style_pack__theme_[a-z0-9]+_(paper|navy)\.json$")
VARIANT_LABELS = {"paper": "紙底", "navy": "深底"}


def _fx_theme():
    if str(PALETTE_CATALOG) not in sys.path:
        sys.path.insert(0, str(PALETTE_CATALOG))
    import fx_theme  # noqa: PLC0415 – optional sibling skill

    return fx_theme


def _palettes() -> list[dict[str, Any]]:
    return json.loads((PALETTE_CATALOG / "palettes.json").read_text("utf-8"))


def _theme_source(theme_no: str) -> dict[str, Any]:
    fx = _fx_theme()
    if theme_no == "kwons":
        return fx.KWONS
    no = theme_no.zfill(3)
    for entry in _palettes():
        if entry["no"] == no:
            return entry
    raise SystemExit(f"unknown palette-catalog theme: {theme_no}")


def _readable_mix(fx, text: str, ground: str) -> str:
    """A softened text colour that still reads at 4.5:1 on its ground."""
    for share in (0.45, 0.35, 0.25, 0.15, 0.0):
        colour = fx.mix(text, ground, share)
        if fx.contrast(colour, ground) >= 4.5:
            return colour
    return text


def pack_for(theme_no: str, variant: str) -> dict[str, Any]:
    """Build one style pack dict for a palette-catalog theme and variant."""
    if variant not in VARIANT_LABELS:
        raise ValueError(f"unknown variant: {variant}")
    fx = _fx_theme()
    source = _theme_source(theme_no)
    roles, _notes = fx.derive(source)
    template = json.loads(TEMPLATE.read_text("utf-8"))
    paper, ink = roles["paper"], roles["ink"]
    if variant == "paper":
        palette = {
            "bg": paper,
            "panel": paper,
            "ink": ink,
            "accent": roles["accent"],
            "muted": _readable_mix(fx, ink, paper),
            "caption_emphasis": roles["accent"],
        }
        panel_alpha = 0.96
    else:
        palette = {
            "bg": roles["navy"],
            "panel": roles["navy"],
            "ink": roles["on-navy"],
            "accent": roles["accent-2"],
            "muted": _readable_mix(fx, roles["on-navy"], roles["navy"]),
            "caption_emphasis": roles["accent-2"],
        }
        panel_alpha = 0.92
    palette.update(
        {
            "amber": roles["gilt"],
            "surface": paper,
            "surface_ink": ink,
            "surface_muted": _readable_mix(fx, ink, paper),
            "surface_accent": roles["accent"],
            "border": roles["gilt"],
        }
    )
    slug = str(source["no"]).lower()
    return {
        "schema_version": 1,
        "id": f"theme-{slug}-{variant}",
        "version": 1,
        "tokens": {
            "palette": palette,
            "typography": {
                "title": "font-noto-serif-tc-bold",
                "body": "font-noto-sans-tc",
                "mono": "font-jetbrains-mono",
            },
            "spacing": {
                "unit": 8,
                "safe_margin": 24,
                "card_radius": 12,
                "border_width": 2,
                "panel_alpha": panel_alpha,
            },
        },
        "components": template["components"],
        "license": "internal-original",
    }


def _label(theme_no: str, variant: str) -> dict[str, str]:
    source = _theme_source(theme_no)
    name = source["zh"]
    return {
        "label": f"主題 {source['no']} {name}｜{VARIANT_LABELS[variant]}",
        "description": (
            f"palette-catalog「{name}」配色；宋體標題、"
            + ("米白紙卡與墨色字。" if variant == "paper" else "深色卡片與亮色字。")
        ),
    }


def build(theme_nos: list[str], replace: bool = False) -> list[dict[str, str]]:
    """Generate packs; merge into the manifest unless ``replace`` is set."""
    import contract_registry  # noqa: PLC0415

    entries = []
    for theme_no in theme_nos:
        for variant in ("paper", "navy"):
            pack = pack_for(theme_no, variant)
            errors = contract_registry.validate_artifact("style_pack", pack)
            if errors:
                raise SystemExit(f"{pack['id']} failed validation: {'; '.join(errors)}")
            filename = pack["id"].replace("theme-", "style_pack__theme_").replace("-", "_") + ".json"
            (INSTANCES / filename).write_text(
                json.dumps(pack, ensure_ascii=False, indent=2) + "\n", "utf-8"
            )
            entries.append({"id": pack["id"], "file": filename, **_label(theme_no, variant)})
    merged = entries
    if not replace:
        fresh = {entry["id"] for entry in entries}
        merged = [e for e in load_manifest() if e["id"] not in fresh] + entries
    MANIFEST.write_text(
        json.dumps({"schema": "theme-pack-manifest/1", "packs": merged}, ensure_ascii=False, indent=2)
        + "\n",
        "utf-8",
    )
    return entries


def load_manifest(path: Path = MANIFEST) -> list[dict[str, str]]:
    """Read and strictly validate the theme-pack manifest (empty if absent)."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unreadable theme-pack manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema") != "theme-pack-manifest/1":
        raise RuntimeError(f"invalid theme-pack manifest: {path}")
    packs = data.get("packs")
    if not isinstance(packs, list):
        raise RuntimeError(f"invalid theme-pack manifest: {path}")
    seen: set[str] = set()
    for entry in packs:
        pack_id = entry.get("id") if isinstance(entry, dict) else None
        filename = entry.get("file") if isinstance(entry, dict) else None
        if not (isinstance(pack_id, str) and THEME_ID_RE.fullmatch(pack_id)):
            raise RuntimeError(f"invalid theme pack id in manifest: {pack_id!r}")
        if not (isinstance(filename, str) and FILE_RE.fullmatch(filename)):
            raise RuntimeError(f"invalid theme pack file in manifest: {filename!r}")
        for key in ("label", "description"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise RuntimeError(f"theme pack {pack_id} is missing {key}")
        if pack_id in seen:
            raise RuntimeError(f"duplicate theme pack id in manifest: {pack_id}")
        seen.add(pack_id)
    return packs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate packs and the manifest")
    b.add_argument("themes", nargs="*", help="palette-catalog numbers or 'kwons'")
    b.add_argument("--replace", action="store_true", help="rewrite the manifest with only these themes")
    sub.add_parser("list", help="show packs in the manifest")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        themes = args.themes or list(_fx_theme().PRESETS)
        for entry in build(themes, replace=args.replace):
            print(f"{entry['id']:<22} {entry['label']}")
        return 0
    for entry in load_manifest():
        print(f"{entry['id']:<22} {entry['label']}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())

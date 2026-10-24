#!/usr/bin/env python3
"""Effect overlays (type ``fx``): the 115 HyperFrames effect blocks from the
``edit-style-profiles`` skill (風格菜單) as one more timed layer in the Studio.

An ``fx`` overlay stores only *what* to draw — effect code, text variables,
placement, card scheme — on the source time axis like every other overlay:

    {"id": "fx-…", "type": "fx", "start": 2.8, "end": 5.8, "code": "T3",
     "values": {"title": "…"}, "placement": "top", "scheme": "",
     "style": {"x": 50, "y": 50, "width": 100, "animation": "none"}, ...}

At render time ``render_for_timeline`` turns it into a transparent ProRes
4444 .mov (``working/fx/<code>-<key>.mov``) through ``compose.py clip`` of
that skill — the same shape ``card_animator`` uses for animated cards — and
the timeline overlays it like any ``video`` layer. Colours follow the
project's style pack (``theme-<no>-paper`` → that palette theme, paper cards;
``theme-<no>-navy`` → night cards); anything else falls back to the kwons
palette. Blocks that need a transparent person (ⓜ) get one from
``matte_video.py`` (RobustVideoMatting, local), cached per window.

A failure never blocks a delivery: the effect is skipped and the reason is
printed to stderr as ``{"fx_fallback": …}``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

FX_VERSION = "fx-overlay-v1"
FX_REL = Path("working/fx")
MATTE_TIMEOUT_S = 30 * 60


def _skill_roots() -> list[Path]:
    roots = [Path.home() / ".claude/skills"]
    for extra in os.environ.get("CLAUDE_SKILL_ROOTS", "").split(":"):
        if extra.strip():
            roots.append(Path(extra).expanduser())
    return roots


def skill_dir() -> Path:
    override = os.environ.get("EDIT_STYLE_PROFILES_SKILL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    for root in _skill_roots():
        candidate = root / "edit-style-profiles"
        if (candidate / "SKILL.md").is_file():
            return candidate
    return _skill_roots()[0] / "edit-style-profiles"


def available() -> bool:
    if os.environ.get("AUTO_EDIT_DISABLE_FX") == "1":
        return False
    d = skill_dir()
    return (d / "tools/compose.py").is_file() and (d / "menu/effects.json").is_file()


def _compose():
    d = skill_dir()
    tools = str(d / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import compose  # noqa: E402  (edit-style-profiles/tools/compose.py)

    return compose


_CATALOG: dict[str, Any] | None = None


def catalog(refresh: bool = False) -> dict[str, Any]:
    """Public effect catalog for the editor: codes, names, categories, thumbs, variables."""
    global _CATALOG
    if _CATALOG is not None and not refresh:
        return _CATALOG
    if not available():
        _CATALOG = {"available": False, "reason": "edit-style-profiles skill (compose.py / menu) not found",
                    "effects": [], "categories": {}, "schemes": {}, "themes": []}
        return _CATALOG
    compose = _compose()
    import card_schemes as cs  # from the same tools dir

    menu = skill_dir() / "menu"
    eff = json.loads((menu / "effects.json").read_text(encoding="utf-8"))
    tdir = menu / "themes"
    theme_nos = sorted(p.stem for p in tdir.glob("*.json")) if tdir.is_dir() else []
    blocks = compose.all_blocks()
    effects = []
    for e in sorted(eff["effects"], key=lambda x: (x["category"], x["num"])):
        schema = blocks.get(e.get("block") or "")
        own = [no for no in theme_nos if (menu / "thumbs" / no / f"{e['num']:02d}.jpg").is_file()]
        ref = e.get("thumb") if (menu / str(e.get("thumb", ""))).is_file() else ""
        effects.append({
            "code": e["code"], "num": e["num"], "name": e["name"], "category": e["category"],
            "spec": e.get("spec", ""), "matte": bool(e.get("matte")), "block": e.get("block"),
            "available": schema is not None,
            "thumb": (f"thumbs/{own[0]}/{e['num']:02d}.jpg" if own else ref),
            "thumbs_by_theme": {no: f"thumbs/{no}/{e['num']:02d}.jpg" for no in own},
            "vars": [v for v in (schema or {}).get("vars", []) if v["kind"] in {"text", "placement"}],
            "needs_video": bool((schema or {}).get("needs_video")),
            "needs_person": bool((schema or {}).get("needs_person")),
            "media_max": (schema or {}).get("media_max"),
        })
    _CATALOG = {
        "available": True, "version": FX_VERSION,
        "effects": effects, "categories": eff.get("categories", {}),
        "schemes": {k: v["label"] for k, v in cs.SCHEMES.items()},
        "placements": ["", "top", "bottom", "left", "right"],
        "themes": theme_nos, "default_theme": "kwons",
        "thumb_base": "/fx-thumbs/",
        "font_roles": list(FONT_ROLES),
        "fonts": [{k: v for k, v in e.items() if k not in {"file_digest", "file"}} for e in fonts_catalog()["fonts"]],
        "fonts_version": fonts_catalog()["version"],
    }
    return _CATALOG


FONT_ROLES = ("display", "body", "mono", "hand")
_FONTS: dict[str, Any] | None = None
_FILE_DIGESTS: dict[str, tuple[tuple[int, int, int], str]] = {}


def _file_digest(path: Path) -> str:
    """sha256 of a font file, cached by (inode, size, mtime_ns) so a swapped file is re-hashed."""
    import hashlib
    try:
        st = path.stat()
    except OSError:
        return ""
    if path.is_symlink() or not path.is_file():
        return ""
    key = (st.st_ino, st.st_size, st.st_mtime_ns)
    hit = _FILE_DIGESTS.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    _FILE_DIGESTS[str(path)] = (key, h.hexdigest())
    return h.hexdigest()


def fonts_catalog(refresh: bool = False) -> dict[str, Any]:
    """Font catalog from edit-style-profiles/menu/fonts.json.

    Every call re-stats fonts.json and every referenced font file (hashes are cached by
    inode/size/mtime), so ``version`` changes as soon as the catalog or a font file on
    disk changes — approvals and render caches are bound to the exact glyphs (2026-10-22
    Codex review: no process-lifetime freeze).  An entry is ``verified`` only when its
    pinned sha256 matches the file actually installed; Studio fx layers may use only
    verified entries (fail closed)."""
    global _FONTS
    import hashlib
    path = skill_dir() / "menu" / "fonts.json"
    try:
        st = path.stat()
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = None
    entries: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        digest.update(raw)
        fonts_dir = Path.home() / "Library" / "Fonts"
        for f in data.get("fonts", []):
            fid = str(f.get("id") or "")
            fam = str(f.get("family") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", fid) or not re.fullmatch(r"[A-Za-z0-9\u3400-\u9FFF\u3040-\u30FF ._()-]{1,80}", fam):
                continue
            fname = str(f.get("file") or "")
            pinned = str(f.get("sha256") or "")
            file_digest = ""
            file_path = None
            if fname and "/" not in fname and ".." not in fname and not fname.startswith("."):
                file_path = fonts_dir / fname
                file_digest = _file_digest(file_path)
            is_ttc = fname.lower().endswith(".ttc")
            # TTC entries are pinned on the extracted face, so the container hash is bound
            # to the digest but cannot be compared to the pin; they stay family-resolved.
            verified = bool(file_digest) and (is_ttc or (bool(pinned) and file_digest == pinned))
            digest.update(f"{fid}:{file_digest}:{int(verified)}\n".encode())
            entries.append({"id": fid, "family": fam, "label": str(f.get("label") or fam), "role": f.get("role") or "display",
                            "lang": f.get("lang") or "sys", "cat": f.get("cat") or "", "note": f.get("note") or "",
                            "sample": f.get("sample") or "Aa", "google": f.get("google") or "", "github": bool(f.get("github")), "verified": verified,
                            "src": "family" if (is_ttc or not verified) else "file",
                            "file": str(file_path) if (verified and not is_ttc and file_path) else "", "file_digest": file_digest})
    except (OSError, ValueError):
        entries = []
    _FONTS = {"fonts": entries, "by_id": {e["id"]: e for e in entries}, "version": digest.hexdigest()[:16], "stamp": stamp}
    return _FONTS


def validate_font_ids(font_ids: Any) -> list[str]:
    """``{"display": "<id>", "body": "<id>"}`` → errors (empty when fine). Only catalog ids pass."""
    if font_ids is None:
        return []
    if not isinstance(font_ids, dict) or len(font_ids) > len(FONT_ROLES):
        return ["font_ids must be an object of role → font id"]
    by_id = fonts_catalog()["by_id"]
    errors = []
    for role, fid in font_ids.items():
        if role not in FONT_ROLES:
            errors.append(f"font role {role!r} is not one of {', '.join(FONT_ROLES)}")
        elif fid and (not isinstance(fid, str) or fid not in by_id):
            errors.append(f"font id {fid!r} is not in the font catalog")
        elif fid and not by_id[fid]["verified"]:
            errors.append(f"font id {fid!r} is not verified (file missing or sha256 differs from the catalog pin)")
    return errors


def families_for(font_ids: Any) -> dict[str, str]:
    by_id = fonts_catalog()["by_id"]
    return {r: by_id[i]["family"] for r, i in (font_ids or {}).items()
            if r in FONT_ROLES and isinstance(i, str) and i in by_id and by_id[i]["verified"]}


def font_files_for(font_ids: Any) -> dict[str, str]:
    """family → verified file path, for the renderer to declare @font-face against the exact bytes."""
    by_id = fonts_catalog()["by_id"]
    return {by_id[i]["family"]: by_id[i]["file"] for r, i in (font_ids or {}).items()
            if r in FONT_ROLES and isinstance(i, str) and i in by_id and by_id[i]["verified"] and by_id[i]["file"]}


def fonts_digest_for(state: dict[str, Any]) -> str:
    """Digest of the font catalog version plus the files behind every font id an fx layer uses."""
    import hashlib
    cat = fonts_catalog()
    h = hashlib.sha256(cat["version"].encode())
    for o in state.get("overlays", []) if isinstance(state.get("overlays"), list) else []:
        if isinstance(o, dict) and o.get("type") == "fx" and isinstance(o.get("font_ids"), dict):
            for r, i in sorted(o["font_ids"].items()):
                e = cat["by_id"].get(i)
                h.update(f"{o.get('id')}:{r}:{i}:{e['file_digest'] if e else '-'}\n".encode())
    return h.hexdigest()[:16]


def codes() -> set[str]:
    return {e["code"] for e in catalog().get("effects", [])}


PORTRAIT = (1080, 1920)


def canvas_ok(canvas: tuple[int, int] | None) -> bool:
    """Effect blocks are authored at 1080×1920; any 9:16 canvas scales cleanly, nothing else does."""
    if not canvas:
        return True
    w, h = canvas
    return w > 0 and h > 0 and abs(w / h - 9 / 16) < 0.01


def validate(overlay: dict[str, Any], state: dict[str, Any] | None = None) -> list[str]:
    """Field checks for one ``fx`` overlay (called from the editor-state validator).

    Everything the UI constrains is re-checked here so a hand-written PUT
    cannot persist an effect the renderer would then drop: the code must be
    in the catalog (no catalog, no fx layers), values only name that block's
    text variables, placement only where the block has one, and a block that
    transforms the clip cannot run longer than its inner video.
    """
    errors: list[str] = []
    oid = overlay.get("id")
    if not available():
        errors.append(f"fx overlay {oid}: effect catalog unavailable (edit-style-profiles skill not found)")
        return errors
    code = str(overlay.get("code") or "")
    effect = next((e for e in catalog()["effects"] if e["code"] == code), None)
    if effect is None or effect.get("available") is False:
        errors.append(f"fx overlay {oid} has unknown effect code {code!r}")
        return errors
    text_vars = {v["id"] for v in effect["vars"] if v["kind"] == "text"}
    values = overlay.get("values")
    if values is None:
        values = {}
    if not isinstance(values, dict) or len(values) > 64:
        errors.append(f"fx overlay {oid} values must be an object")
    else:
        for k, v in values.items():
            if not isinstance(k, str) or k not in text_vars:
                errors.append(f"fx overlay {oid} values has a field {k!r} that effect {code} does not take")
            elif not isinstance(v, (str, int, float)) or isinstance(v, bool) or len(str(v)) > 500:
                errors.append(f"fx overlay {oid} value {k!r} must be a short string")
    placement = overlay.get("placement") or ""
    has_placement = any(v["kind"] == "placement" for v in effect["vars"])
    if placement and (not has_placement or placement not in {"top", "bottom", "left", "right"}):
        errors.append(f"fx overlay {oid} placement {placement!r} is not valid for effect {code}")
    scheme = overlay.get("scheme") or ""
    if scheme and scheme not in catalog()["schemes"]:
        errors.append(f"fx overlay {oid} scheme is invalid")
    for err in validate_font_ids(overlay.get("font_ids")):
        errors.append(f"fx overlay {oid}: {err}")
    try:
        span = float(overlay.get("end", 0)) - float(overlay.get("start", 0))
    except (TypeError, ValueError):
        span = 0.0
    if False and effect.get("needs_video") and effect.get("media_max") and span > float(effect["media_max"]) + 1e-6:
        errors.append(f"fx overlay {oid}: effect {code} can run at most {effect['media_max']} s (it transforms the clip)")
    if span > 30:
        errors.append(f"fx overlay {oid}: an effect longer than 30 s is not an effect; split it")
    canvas = (state or {}).get("canvas") or {}
    try:
        cw, ch = int(canvas.get("width") or 0), int(canvas.get("height") or 0)
    except (TypeError, ValueError):
        cw = ch = 0
    if cw and ch and not canvas_ok((cw, ch)):
        errors.append(f"fx overlay {oid}: effects need a 9:16 canvas (this project is {cw}×{ch})")
    return errors


def theme_for_pack(pack_id: str | None) -> tuple[str, str]:
    """Style pack id → (palette theme no, main card scheme)."""
    m = re.fullmatch(r"theme-([A-Za-z0-9]+)-(paper|navy)", str(pack_id or ""))
    if m:
        return m.group(1), ("night" if m.group(2) == "navy" else "paper")
    return "kwons", "paper"


def _log(payload: dict[str, Any]) -> None:
    print(json.dumps({"fx_fallback": payload}, ensure_ascii=False), file=sys.stderr)


def _person_clip(project_dir: Path, source: Path, start: float, dur: float) -> Path | None:
    """Transparent person webm for [start, start+dur] of the source (cached)."""
    compose = _compose()
    key = hashlib.sha256(f"{source}|{source.stat().st_mtime}|{start:.3f}|{dur:.3f}|matte-v1".encode()).hexdigest()[:16]
    out = project_dir / FX_REL / f"person-{key}.webm"
    if out.is_file() and out.stat().st_size > 0:
        return out
    matte = skill_dir() / "tools/matte_video.py"
    if not matte.is_file():
        return None
    workdir = project_dir / FX_REL / f"matte-{key}"
    workdir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(matte), str(source), "--start", f"{start:.3f}", "--duration", f"{dur:.3f}",
                        "--alpha-mov", "-o", str(workdir)], capture_output=True, text=True, timeout=MATTE_TIMEOUT_S)
    mov = workdir / "person_alpha.mov"
    if r.returncode or not mov.is_file():
        _log({"stage": "matte", "error": (r.stderr or r.stdout).strip()[-400:]})
        return None
    vf = f"scale={compose.W}:{compose.H}:force_original_aspect_ratio=increase,crop={compose.W}:{compose.H},setsar=1"
    q = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mov), "-vf", vf, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                        "-b:v", "0", "-crf", "24", "-row-mt", "1", "-an", str(out)], capture_output=True, text=True)
    if q.returncode:
        _log({"stage": "matte-webm", "error": q.stderr.strip()[-400:]})
        return None
    try:
        mov.unlink()  # ProRes 4444 is huge; the webm is what the block eats
    except OSError:
        pass
    return out


def render_for_timeline(project_dir: Path, overlay: dict[str, Any], source: Path, pack_id: str | None,
                        quality: str = "draft", canvas: tuple[int, int] | None = None) -> dict[str, Any] | None:
    """Render one fx overlay to a transparent .mov under working/fx/; None on failure."""
    if not available():
        _log({"layer": overlay.get("id"), "reason": "edit-style-profiles skill unavailable"})
        return None
    if not canvas_ok(canvas):
        _log({"layer": overlay.get("id"), "reason": f"effects need a 9:16 canvas, got {canvas[0]}×{canvas[1]}"})
        return None
    compose = _compose()
    code = str(overlay.get("code") or "")
    start = max(0.0, float(overlay.get("start", 0.0)))
    end = float(overlay.get("end", start))
    dur = round(end - start, 3)
    if dur <= 0.05:
        return None
    theme, main = theme_for_pack(pack_id)
    scheme = str(overlay.get("scheme") or "")
    values = {k: str(v) for k, v in (overlay.get("values") or {}).items()}
    placement = str(overlay.get("placement") or "")
    fonts = families_for(overlay.get("font_ids"))
    workdir = project_dir / FX_REL
    if fonts:  # cache keyed by the exact font files, not just the family names
        workdir = workdir / f"fonts-{fonts_digest_for({'overlays': [overlay]})[:10]}"
    workdir.mkdir(parents=True, exist_ok=True)
    person = None
    try:
        eff = next(e for e in catalog()["effects"] if e["code"] == code)
    except StopIteration:
        _log({"layer": overlay.get("id"), "reason": f"unknown code {code}"})
        return None
    if eff["needs_person"]:
        person = _person_clip(project_dir, source, start, dur)
        if person is None:
            _log({"layer": overlay.get("id"), "code": code, "reason": "person matte unavailable; using placeholder person"})
    try:
        rep = compose.render_clip(str(source), start, dur, code, theme, main, scheme, values, placement, None,
                                  str(person) if person else None, "draft" if quality != "final" else "final",
                                  str(workdir), "mov", log=lambda m: None, fonts=fonts or None,
                                  font_files=font_files_for(overlay.get("font_ids")) or None)
    except SystemExit as exc:
        _log({"layer": overlay.get("id"), "code": code, "error": str(exc)})
        return None
    if not rep.get("ok"):
        _log({"layer": overlay.get("id"), "code": code, "error": rep.get("log", "")[-400:]})
        return None
    out = Path(rep["out"])
    return {"source": str(out.relative_to(project_dir)), "cached": rep.get("cached"), "seconds": rep.get("seconds"),
            "theme": theme, "main": main, "scheme": scheme or main, "code": code, "block": eff["block"],
            "fonts": fonts, "person": str(person.relative_to(project_dir)) if person else None}


def plan_from_menu(state: dict[str, Any], duration: float, codes: list[str], theme: str = "",
                   main: str = "", alt: str = "", font_ids: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Turn a 風格菜單 order (codes + theme + card schemes) into fx overlays on this project.

    Placement follows storyboard.py's suggestion rule: the hook (H…) on the first
    caption, the ending (E…) on the last, the rest spread over the captions in
    between — or evenly over the clip when there are no captions yet. Returns
    (overlays, style_pack_id or None, warnings)."""
    cat = catalog()
    by_code = {e["code"]: e for e in cat.get("effects", [])}
    warnings: list[str] = []
    codes = [c for c in codes if c in by_code] or []
    bad = [c for c in codes if c not in by_code]
    if bad:
        warnings.append(f"略過沒有的代碼：{'、'.join(bad)}")
    captions = sorted((o for o in state.get("overlays", []) if o.get("type") == "caption" and o.get("visible", True)),
                      key=lambda o: float(o.get("start", 0)))
    anchors = [(float(c["start"]), float(c["end"])) for c in captions]
    if not anchors:
        n = max(len(codes), 1)
        step = max(duration / n, 0.5)
        anchors = [(i * step, min(duration, i * step + step)) for i in range(n)]
    hooks = [c for c in codes if c.startswith("H")]
    ends = [c for c in codes if c.startswith("E")]
    body = [c for c in codes if c[0] not in "HE"]
    slots: list[tuple[str, float, float]] = []
    if hooks:
        slots.append((hooks[0], anchors[0][0], anchors[0][1]))
    if ends and len(anchors) > 1:
        slots.append((ends[0], anchors[-1][0], anchors[-1][1]))
    middle = anchors[1:-1] if len(anchors) > 2 else anchors
    if body and middle:
        stride = max(1, len(middle) // len(body))
        for i, code in enumerate(body):
            a = middle[min(len(middle) - 1, i * stride)]
            slots.append((code, a[0], a[1]))
    alt_key = alt if alt in cat.get("schemes", {}) else ""
    clean_fonts = {r: i for r, i in (font_ids or {}).items() if i} if not validate_font_ids(font_ids) else {}
    if font_ids and not clean_fonts and any(font_ids.values()):
        warnings.append("字型 id 不在目錄內，已略過")
    overlays = []
    stamp = int(__import__("time").time() * 1000)
    for i, (code, s0, s1) in enumerate(sorted(slots, key=lambda x: x[1])):
        e = by_code[code]
        dur = 3.0
        if e.get("media_max"):
            dur = min(dur, float(e["media_max"]))
        dur = min(dur, max(0.5, duration - s0))
        values = {v["id"]: v["default"] for v in e["vars"] if v["kind"] == "text"}
        placement = next((v["default"] for v in e["vars"] if v["kind"] == "placement"), "")
        overlays.append({
            "id": f"fx-menu-{stamp}-{i + 1:02d}", "type": "fx", "start": round(s0, 3), "end": round(s0 + dur, 3),
            "text": e["name"], "emphasis": [], "visible": True, "locked": False, "z_index": 25,
            "style": {"x": 50, "y": 50, "width": 100, "animation": "none", "opacity": 1}, "source": None,
            "provenance": "風格菜單 apply (fx_overlay.plan_from_menu)", "highlight_id": None,
            "code": code, "values": values, "placement": placement,
            "scheme": alt_key if (alt_key and code.startswith("T") and i % 3 == 2) else "",
            **({"font_ids": clean_fonts} if clean_fonts else {}),
        })
    pack = None
    if theme:
        pack = f"theme-{theme}-{'navy' if main == 'night' else 'paper'}"
    return overlays, pack, warnings


def as_video_overlay(overlay: dict[str, Any], rendered: dict[str, Any]) -> dict[str, Any]:
    """The fx overlay rewritten as the full-frame video layer the timeline knows how to draw."""
    video = dict(overlay)
    video["type"] = "video"
    video["source"] = rendered["source"]
    video["fx"] = {k: rendered[k] for k in ("code", "block", "theme", "main", "scheme", "person")}
    video["style"] = {"x": 50, "y": 50, "width": 100, "animation": "none",
                      "opacity": float((overlay.get("style") or {}).get("opacity", 1) or 1)}
    video["provenance"] = f"edit-style-profiles effect {rendered['code']} ({rendered['block']}) via compose.py clip"
    return video


if __name__ == "__main__":  # quick manual check: fx_overlay.py <project_dir> <code> <start> <end> [pack]
    pd = Path(sys.argv[1]).resolve()
    manifest = json.loads((pd / "project.json").read_text())
    src = pd / manifest["source"]["staged_path"]
    ov = {"id": "fx-test", "type": "fx", "code": sys.argv[2], "start": float(sys.argv[3]), "end": float(sys.argv[4])}
    print(json.dumps(render_for_timeline(pd, ov, src, sys.argv[5] if len(sys.argv) > 5 else None), ensure_ascii=False))

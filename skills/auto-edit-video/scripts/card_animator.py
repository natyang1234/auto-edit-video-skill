#!/usr/bin/env python3
"""Content animation for structured cards, rendered by HyperFrames.

Four pack presets animate a card's *contents* — digits counting up, words
arriving one by one, list items revealing in sequence, a bar filling. A
finished PNG cannot do any of that, so those presets were approximated with
entrance animations and flagged unfaithful in the receipt. This is the real
adapter: the card is rebuilt as a small GSAP composition and rendered to a
transparent .mov, which the timeline overlays exactly where the static PNG
would have gone.

What stays shared, so the two renderers cannot drift apart:
- geometry: the composition takes the static card's measured width and
  height, so placement, collision checks and safe-area maths are unchanged;
- styling: colours come from the same style-pack tokens the CoreText
  compositor reads, and the type sizes mirror its constants.

Every render is cache-keyed on everything that shaped it; a failure falls
back to the static card with its entrance approximation — a delivery is
never blocked on a browser.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ANIMATOR_VERSION = "hyperframes-card-animator-v1"
ANIMATIONS_REL = Path("working/structured_cards")
# The presets whose motion lives inside the card. `flip` stays approximated:
# a page turn needs a second page to turn to, which these payloads carry no
# content for.
CONTENT_PRESETS = {"count-up", "word-cascade", "staggered-reveal", "fill"}
# The animation finishes early and the last frame holds; the tail keeps the
# looped input from wrapping back to frame zero inside the overlay window.
TAIL_SECONDS = 0.5
RENDER_TIMEOUT_S = 15 * 60


def hyperframes_command() -> list[str] | None:
    """The CLI, resolved the same way the designed-package path resolves it."""
    import graphic_package

    try:
        return graphic_package._hyperframes_command()
    except ValueError:
        return None


def available() -> bool:
    if os.environ.get("AUTO_EDIT_DISABLE_CARD_ANIMATION") == "1":
        return False
    return hyperframes_command() is not None


def _token(pack: dict[str, Any], group: str, key: str, fallback: str) -> str:
    value = (pack.get("tokens", {}).get(group, {}) or {}).get(key)
    return str(value) if value else fallback


def _chunks(text: str) -> list[str]:
    """The units a line arrives in: CJK by character, Latin by word."""
    return [
        piece
        for piece in re.findall(r"[㐀-鿿]|[^㐀-鿿\s]+", str(text))
        if piece.strip()
    ]


def _spans(text: str) -> str:
    return "".join(
        f'<span class="u">{html.escape(chunk)}</span>' for chunk in _chunks(text)
    )


def _body_html(layer: dict[str, Any], preset: str) -> str:
    payload = layer.get("payload") or {}
    if preset == "word-cascade":
        return f'<div class="title">{_spans(payload.get("title") or "")}</div>'
    if preset == "staggered-reveal":
        rows = "".join(
            f'<div class="row"><span class="index">{index + 1}.</span> '
            f"{html.escape(str(entry.get('text', '')))}</div>"
            for index, entry in enumerate(payload.get("items") or [])
        )
        return f'<div class="list">{rows}</div>'
    if preset == "count-up":
        value = str(payload.get("value") or "").strip()
        label = html.escape(str(payload.get("label") or ""))
        return (
            f'<div class="value" data-value="{html.escape(value)}">'
            f"{html.escape(value)}</div>"
            f'<div class="label">{label}</div>'
        )
    if preset == "fill":
        value = str(payload.get("value") or "").strip()
        ratio = payload.get("ratio")
        share = max(0.0, min(1.0, float(ratio))) if isinstance(ratio, (int, float)) else 1.0
        return (
            f'<div class="value">{html.escape(value)}</div>'
            f'<div class="track"><div class="bar" data-share="{share:.4f}"></div></div>'
            f'<div class="label">{html.escape(str(payload.get("label") or ""))}</div>'
        )
    raise ValueError(f"no content animation for preset {preset!r}")


_TIMELINES = {
    # Each block builds onto `tl`, a paused GSAP timeline HyperFrames scrubs.
    "word-cascade": """
      tl.fromTo('.u',{opacity:0,y:14},{opacity:1,y:0,duration:.3,
        ease:'power2.out',stagger:.05},0.05);
    """,
    "staggered-reveal": """
      tl.fromTo('.row',{opacity:0,y:16},{opacity:1,y:0,duration:.4,
        ease:'power2.out',stagger:.28},0.1);
    """,
    "count-up": """
      var el=document.querySelector('.value');
      var raw=el.getAttribute('data-value');
      var m=raw.match(/-?[0-9][0-9,]*(?:\\.[0-9]+)?/);
      if(m){
        var num=parseFloat(m[0].replace(/,/g,''));
        var decimals=(m[0].split('.')[1]||'').length;
        var state={n:0};
        tl.to(state,{n:num,duration:.9,ease:'power1.out',onUpdate:function(){
          el.textContent=raw.replace(m[0],state.n.toFixed(decimals));
        }},0.1);
      }else{
        tl.fromTo(el,{opacity:0},{opacity:1,duration:.3},0.1);
      }
    """,
    "fill": """
      var bar=document.querySelector('.bar');
      tl.fromTo(bar,{scaleX:0},{scaleX:Number(bar.getAttribute('data-share')),
        duration:.8,ease:'power2.out'},0.1);
    """,
}


def build_card_html(
    layer: dict[str, Any],
    pack: dict[str, Any],
    preset: str,
    width: int,
    height: int,
    duration_s: float,
    fps: int,
) -> str:
    ink = _token(pack, "palette", "ink", "#E6EDF3")
    accent = _token(pack, "palette", "accent", "#E5484D")
    panel = _token(pack, "palette", "panel", "#161B22")
    # Type sizes mirror the static compositor's constants; the two draw the
    # same card or the swap is visible.
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:transparent}}
#stage{{position:relative;width:{width}px;height:{height}px;
  font-family:'PingFang TC','Noto Sans TC',sans-serif}}
.card{{position:absolute;inset:0;background:{panel}EB;border-radius:14px;
  padding:28px;box-sizing:border-box;color:{ink};overflow:hidden}}
.title{{font-size:58px;font-weight:700;text-align:center}}
.u{{display:inline-block;white-space:pre}}
.list{{font-size:32px;line-height:48px}}
.index{{color:{accent}}}
.value{{font-size:64px;font-weight:700;color:{accent}}}
.label{{font-size:20px;margin-top:6px}}
.track{{height:6px;background:#9AA4AF55;margin-top:10px}}
.bar{{height:100%;background:{accent};transform-origin:left center}}
</style></head><body>
<div id="stage" data-composition-id="card-animation" data-start="0"
  data-duration="{duration_s:.4f}" data-fps="{fps}"
  data-width="{width}" data-height="{height}">
  <div class="card">{_body_html(layer, preset)}</div>
</div>
<script src="vendor/gsap.min.js"></script>
<script>(function(){{
  var tl=window.gsap.timeline({{paused:true}});
  {_TIMELINES[preset]}
  window.__timelines=window.__timelines||{{}};
  window.__timelines["card-animation"]=tl;
}})();</script>
</body></html>"""


def _gsap_vendor() -> Path:
    import graphic_package

    return graphic_package._talking_head_skill_dir() / "assets/vendor/gsap.min.js"


def render_card_animation(
    project_dir: Path,
    layer: dict[str, Any],
    pack: dict[str, Any],
    preset: str,
    width: int,
    height: int,
    duration_s: float,
    fps: int,
) -> Path:
    """The card's animation as a transparent .mov, cached by what shaped it."""
    if preset not in CONTENT_PRESETS:
        raise ValueError(f"preset {preset!r} has no content animation")
    command = hyperframes_command()
    if command is None:
        raise ValueError("HyperFrames CLI is not installed")
    seconds = round(float(duration_s) + TAIL_SECONDS, 3)
    page = build_card_html(layer, pack, preset, width, height, seconds, fps)
    key = hashlib.sha256(
        json.dumps(
            [ANIMATOR_VERSION, layer.get("type"), layer.get("payload"),
             pack.get("id"), pack.get("version"), preset,
             width, height, seconds, fps],
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    target_dir = project_dir / ANIMATIONS_REL
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"anim-{layer.get('id')}-{key}.mov"
    if target.is_file():
        return target

    import tempfile

    with tempfile.TemporaryDirectory(prefix="auto-edit-card-anim-") as scratch:
        public = Path(scratch) / "public"
        (public / "vendor").mkdir(parents=True)
        shutil.copy2(_gsap_vendor(), public / "vendor/gsap.min.js")
        (public / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [*command, "render", "public", "--format", "mov",
             "-o", "card.mov", "--fps", str(fps), "--quiet"],
            cwd=scratch, text=True, capture_output=True,
            timeout=RENDER_TIMEOUT_S,
            env={**os.environ, "PRODUCER_BROWSER_GPU_MODE": "hardware"},
        )
        built = Path(scratch) / "card.mov"
        if result.returncode != 0 or not built.is_file():
            raise ValueError(
                (result.stderr or result.stdout or "card animation render failed")[-800:]
            )
        shutil.copy2(built, target)
    return target

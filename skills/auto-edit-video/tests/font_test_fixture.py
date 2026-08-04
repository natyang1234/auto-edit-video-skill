"""Deterministic tiny sfnt fixture shared by provider/registry tests."""

from __future__ import annotations

from io import BytesIO

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def build_ttf(
    *,
    cmap: dict[int, str] | None = None,
    family_name: str = "Phase Two Test",
) -> bytes:
    cmap = cmap or {
        0x0020: "space",
        0x0041: "A",
        0x00C1: "Aacute",
        0x0301: "acutecomb",
        0x4E2D: "uni4E2D",
        0x7E41: "uni7E41",
    }
    glyph_order = [".notdef", *dict.fromkeys(cmap.values())]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in glyph_order:
        pen = TTGlyphPen(None)
        if name not in {".notdef", "space"}:
            pen.moveTo((50, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": family_name,
            "styleName": "Regular",
            "uniqueFontIdentifier": f"{family_name} Regular 1.0",
            "fullName": f"{family_name} Regular",
            "psName": "PhaseTwoTest-Regular",
            "version": "Version 1.0",
        }
    )
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
        usWeightClass=400,
        fsType=0,
    )
    builder.setupPost()
    builder.setupMaxp()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


__all__ = ["build_ttf"]

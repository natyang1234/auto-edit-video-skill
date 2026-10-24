import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import card_animator  # noqa: E402
import contract_registry  # noqa: E402
import editor_server  # noqa: E402
import structured_card_compositor as scc  # noqa: E402
import theme_packs  # noqa: E402


class ThemePackTests(unittest.TestCase):
    def test_manifest_packs_validate_and_match_ids(self) -> None:
        entries = theme_packs.load_manifest()
        self.assertTrue(entries)
        for entry in entries:
            pack = scc.load_style_pack(entry["id"])
            self.assertEqual(pack["id"], entry["id"])
            self.assertEqual(contract_registry.validate_artifact("style_pack", pack), [])
            palette = pack["tokens"]["palette"]
            for key in ("bg", "panel", "ink", "accent", "border", "surface",
                        "surface_ink", "caption_emphasis", "muted"):
                self.assertRegex(palette[key], r"^#[0-9A-F]{6}$", key)

    def test_generated_pack_is_reproducible(self) -> None:
        regenerated = theme_packs.pack_for("075", "navy")
        on_disk = scc.load_style_pack("theme-075-navy")
        self.assertEqual(regenerated["tokens"], on_disk["tokens"])

    def test_manifest_rejects_bad_entries(self) -> None:
        bad = [
            {"id": "../evil", "file": "style_pack__theme_x_paper.json"},
            {"id": "theme-x-paper", "file": "../../etc/passwd"},
        ]
        for entry in bad:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.json"
                path.write_text(json.dumps({"schema": "theme-pack-manifest/1", "packs": [entry]}))
                with self.assertRaises(RuntimeError):
                    theme_packs.load_manifest(path)

    def test_manifest_rejects_malformed_documents(self) -> None:
        documents = [
            [],
            {"schema": "theme-pack-manifest/1", "packs": [
                {"id": "theme-x-paper\n", "file": "style_pack__theme_x_paper.json",
                 "label": "a", "description": "b"}]},
            {"schema": "theme-pack-manifest/1", "packs": [
                {"id": "theme-x-paper", "file": "style_pack__theme_x_paper.json"}]},
        ]
        for document in documents:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.json"
                path.write_text(json.dumps(document))
                with self.assertRaises(RuntimeError):
                    theme_packs.load_manifest(path)

    def test_muted_text_stays_readable(self) -> None:
        sys.path.insert(0, str(theme_packs.PALETTE_CATALOG))
        import fx_theme

        for entry in theme_packs.load_manifest():
            palette = scc.load_style_pack(entry["id"])["tokens"]["palette"]
            self.assertGreaterEqual(fx_theme.contrast(palette["muted"], palette["panel"]), 4.5, entry["id"])
            self.assertGreaterEqual(
                fx_theme.contrast(palette["surface_muted"], palette["surface"]), 4.5, entry["id"]
            )

    def test_unknown_pack_still_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            scc.load_style_pack("theme-999-paper")

    def test_serif_title_font_only_for_theme_packs(self) -> None:
        self.assertIsNotNone(scc.title_font_spec(scc.load_style_pack("theme-kwons-paper")))
        self.assertIsNone(scc.title_font_spec(scc.load_style_pack("editorial-paper")))

    def test_animated_card_uses_serif_title_and_theme_colors(self) -> None:
        pack = scc.load_style_pack("theme-kwons-navy")
        layer = {"type": "kinetic_title", "payload": {"title": "放大"}}
        html = card_animator.build_card_html(layer, pack, "word-cascade", 900, 400, 1.0, 30)
        self.assertIn("Noto Serif TC", html)
        self.assertIn(pack["tokens"]["palette"]["panel"], html)
        plain = card_animator.build_card_html(
            layer, scc.load_style_pack("editorial-paper"), "word-cascade", 900, 400, 1.0, 30
        )
        self.assertNotIn("Noto Serif TC", plain)

    def test_pinned_pack_overrides_director_default_and_emphasis(self) -> None:
        manifest = {"style_pack": "theme-075-paper"}
        self.assertEqual(
            editor_server.default_style_pack_for(manifest, "teacher-punch"), "theme-075-paper"
        )
        styled = editor_server.theme_caption_style(manifest, {"emphasis_color": "#ffd447"})
        self.assertEqual(
            styled["emphasis_color"],
            scc.load_style_pack("theme-075-paper")["tokens"]["palette"]["caption_emphasis"],
        )
        self.assertEqual(
            editor_server.default_style_pack_for({}, "teacher-punch"),
            editor_server.DEFAULT_STYLE_PACK_BY_DIRECTOR["teacher-punch"],
        )
        self.assertEqual(editor_server.theme_caption_style({}, {"a": 1}), {"a": 1})
        with self.assertRaises(ValueError):
            editor_server.default_style_pack_for({"style_pack": "nope"}, "teacher-punch")


if __name__ == "__main__":
    unittest.main()

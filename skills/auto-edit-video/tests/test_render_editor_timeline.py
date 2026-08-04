from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import asset_registry  # noqa: E402
from render_editor_timeline import font_path, project_font_binding  # noqa: E402


class ProjectFontResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="renderer-font-")
        self.project = Path(self.temp.name)
        (self.project / "assets/fonts").mkdir(parents=True)
        for suffix, payload in (("a", b"font-a"), ("b", b"font-b")):
            (self.project / f"assets/fonts/{suffix}.ttf").write_bytes(payload)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_asset_id_selects_exact_same_family_file(self) -> None:
        ids = {
            "font-google-fonts-0123456789abcdef-0123456789abcdef": "a",
            "font-google-fonts-fedcba9876543210-fedcba9876543210": "b",
        }

        def resolve(_project: Path, asset_id: str, required_text: str = "") -> dict:
            suffix = ids[asset_id]
            payload = (self.project / f"assets/fonts/{suffix}.ttf").read_bytes()
            return {
                "asset_id": asset_id,
                "path": f"assets/fonts/{suffix}.ttf",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "family": "Same Family",
            }

        with patch.object(asset_registry, "resolve_project_font", side_effect=resolve):
            first = project_font_binding(self.project, font_asset_id=next(iter(ids)), required_text="甲")
            second = project_font_binding(self.project, font_asset_id=list(ids)[1], required_text="乙")
        self.assertEqual(first["path"].name, "a.ttf")
        self.assertEqual(second["path"].name, "b.ttf")
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_selected_missing_font_never_uses_legacy_fallback(self) -> None:
        asset_id = "font-google-fonts-0123456789abcdef-0123456789abcdef"
        with patch.object(
            asset_registry,
            "resolve_project_font",
            side_effect=asset_registry.AssetRegistryError("receipt missing"),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                font_path(self.project, {"caption_defaults": {"font_asset_id": asset_id}})

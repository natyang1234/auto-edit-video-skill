from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from configure_resvg import (  # noqa: E402
    ConfigureError,
    _canonical_executable,
    _canonical_manifest_destination,
    _discover_version,
    write_manifest_atomic,
)


class ConfigureResvgTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_executable_must_be_canonical_regular_and_not_group_writable(self) -> None:
        executable = self.root / "resvg"
        executable.write_bytes(b"binary")
        executable.chmod(0o755)
        canonical = executable.resolve()
        self.assertEqual(_canonical_executable(str(canonical)), canonical)

        link = self.root / "link"
        link.symlink_to(executable)
        with self.assertRaises(ConfigureError):
            _canonical_executable(str(link))
        executable.chmod(0o775)
        with self.assertRaises(ConfigureError):
            _canonical_executable(str(canonical))

    def test_version_discovery_is_sandboxed_and_strict(self) -> None:
        executable = self.root / "resvg"
        executable.write_bytes(b"binary")
        executable.chmod(0o755)
        with patch(
            "configure_resvg._run_bounded_process",
            return_value={"returncode": 0, "stdout": b"0.48.1\n", "stderr": b""},
        ) as runner:
            self.assertEqual(_discover_version(executable), "0.48.1")
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
        self.assertEqual(argv[-2:], [str(executable), "--version"])

        with patch(
            "configure_resvg._run_bounded_process",
            return_value={"returncode": 0, "stdout": b"0.48.1\n", "stderr": b"warning"},
        ):
            with self.assertRaises(ConfigureError):
                _discover_version(executable)

    def test_atomic_writer_creates_owner_only_regular_manifest(self) -> None:
        destination = self.root.resolve() / "config" / "resvg-manifest.json"
        manifest = {"schema_version": 1, "marker": "verified"}
        write_manifest_atomic(manifest, destination)
        info = destination.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), manifest)

        outside = self.root.resolve() / "outside"
        outside.write_text("keep", encoding="utf-8")
        destination.unlink()
        destination.symlink_to(outside)
        write_manifest_atomic(manifest, destination)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        self.assertTrue(stat.S_ISREG(destination.lstat().st_mode))

    def test_manifest_destination_canonicalizes_parent_not_final_symlink(self) -> None:
        lexical = self.root / "config" / "resvg-manifest.json"
        canonical = _canonical_manifest_destination(lexical)
        self.assertEqual(canonical, lexical.parent.resolve(strict=True) / lexical.name)
        self.assertTrue(canonical.is_absolute())

        outside = self.root.resolve() / "outside-manifest"
        outside.write_text("keep", encoding="utf-8")
        canonical.symlink_to(outside)
        self.assertEqual(_canonical_manifest_destination(lexical), canonical)
        self.assertTrue(canonical.is_symlink())

        # macOS TemporaryDirectory commonly exposes /var as a /private/var
        # alias. The configured manifest must pin the canonical sibling path.
        if str(lexical).startswith("/var/"):
            self.assertTrue(str(canonical).startswith("/private/var/"))

    def test_atomic_writer_rejects_relative_destination(self) -> None:
        with self.assertRaises(ConfigureError):
            write_manifest_atomic({}, Path("relative.json"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import binascii
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from svg_security import (  # noqa: E402
    BUNDLED_SANDBOX_PROFILE,
    BUNDLED_SANDBOX_PROFILE_BYTES,
    BUNDLED_SANDBOX_PROFILE_SHA256,
    DEFAULT_RESVG_MANIFEST_PATH,
    RESVG_MANIFEST_ENV,
    POLICY_VERSION,
    SANITIZER_VERSION,
    PNGValidationResult,
    ResvgRasterizer,
    SvgSecurityError,
    load_resvg_manifest,
    sanitize_and_rasterize,
    sanitize_svg_bytes,
    validate_png_bytes,
)


CORPUS = SKILL_DIR / "contracts" / "fixtures" / "svg_threat_corpus.json"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(kind)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_bytes(width: int = 2, height: int = 2, *, rgba: bool = True) -> bytes:
    color_type = 6 if rgba else 2
    channels = 4 if rgba else 3
    rows = b"".join(b"\x00" + bytes([17]) * (channels * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]

    def test_every_corpus_case_has_declared_verdict(self) -> None:
        self.assertGreaterEqual(len(self.cases), 30)
        for case in self.cases:
            with self.subTest(case=case["id"]):
                if case["expect"] == "accept":
                    result = sanitize_svg_bytes(
                        case["svg"].encode("utf-8"),
                        requested_width=64,
                        requested_height=64,
                    )
                    self.assertTrue(result.canonical_svg.startswith(b"<svg "))
                else:
                    with self.assertRaises(SvgSecurityError) as rejected:
                        sanitize_svg_bytes(
                            case["svg"].encode("utf-8"),
                            requested_width=64,
                            requested_height=64,
                        )
                    self.assertEqual(rejected.exception.code, case["code"])

    def test_canonical_equivalence_and_versioned_identity(self) -> None:
        first = sanitize_svg_bytes(
            b'<svg viewBox="0 0 24 24"><path fill="#fff" d="M 0 0 L 24 0 L24 24 Z"/></svg>',
            requested_width=256,
            requested_height=256,
        )
        second = sanitize_svg_bytes(
            b"<svg viewBox='0.0,0,24.000,24'><path d='M0,0L24,0 L 24,24Z' fill='white'></path></svg>",
            requested_width=256,
            requested_height=256,
        )
        self.assertEqual(first.canonical_svg, second.canonical_svg)
        self.assertEqual(first.sanitized_sha256, second.sanitized_sha256)
        self.assertEqual(first.metadata["policy_version"], POLICY_VERSION)
        self.assertEqual(first.metadata["sanitizer_version"], SANITIZER_VERSION)
        self.assertRegex(first.metadata["limits_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(first.metadata["sanitize_cache_key_sha256"], r"^[0-9a-f]{64}$")


class ParserLimitTests(unittest.TestCase):
    def reject(self, payload: bytes, code: str) -> None:
        with self.assertRaises(SvgSecurityError) as rejected:
            sanitize_svg_bytes(payload, requested_width=64, requested_height=64)
        self.assertEqual(rejected.exception.code, code)
        self.assertNotIn("evil", str(rejected.exception))

    def test_size_depth_node_and_attribute_limits(self) -> None:
        self.reject(b"<svg>" + b" " * (2 * 1024 * 1024) + b"</svg>", "SVG_RAW_TOO_LARGE")
        self.reject(
            ("<svg>" + "<g>" * 32 + "</g>" * 32 + "</svg>").encode(),
            "SVG_DEPTH_LIMIT",
        )
        self.reject(
            ("<svg>" + "<rect/>" * 5000 + "</svg>").encode(),
            "SVG_ELEMENT_LIMIT",
        )
        repeated = '<rect x="0" y="0" width="1" height="1" opacity="1"/>' * 4_001
        self.reject(f"<svg>{repeated}</svg>".encode(), "SVG_ATTRIBUTE_LIMIT")

    def test_path_is_fully_parsed_and_bounded(self) -> None:
        for path, code in (
            ("M0 0A1 1 0 2 0 3 3", "SVG_PATH_INVALID"),
            ("M0 0L1 1 garbage", "SVG_PATH_INVALID"),
            ("M0 0L1e999 1", "SVG_GEOMETRY_LIMIT"),
            ("M0 0R1 1", "SVG_PATH_INVALID"),
        ):
            with self.subTest(path=path):
                self.reject(f'<svg><path d="{path}"/></svg>'.encode(), code)
        command_block = "M0 0" + "L0 0" * 200
        paths = "".join(f'<path d="{command_block}"/>' for _ in range(100))
        self.reject(f"<svg>{paths}</svg>".encode(), "SVG_PATH_COMMAND_LIMIT")

    def test_reference_chain_limit_and_cycles(self) -> None:
        chain = "".join(
            f'<clipPath id="c{i}" clip-path="url(#c{i + 1})"><rect width="1" height="1"/></clipPath>'
            for i in range(1, 10)
        ) + '<clipPath id="c10"><rect width="1" height="1"/></clipPath>'
        self.reject(f"<svg><defs>{chain}</defs><rect clip-path=\"url(#c1)\"/></svg>".encode(), "SVG_REFERENCE_DEPTH")
        cycle = (
            b'<svg><defs><clipPath id="a" clip-path="url(#b)"/>'
            b'<clipPath id="b" clip-path="url(#a)"/></defs></svg>'
        )
        self.reject(cycle, "SVG_REFERENCE_CYCLE")

        # A suffix declared first must not poison depth memoization and let a
        # later, longer prefix bypass the eight-edge limit.
        reverse_order = "".join(
            f'<clipPath id="r{i}" clip-path="url(#r{i + 1})"/>'
            for i in range(9, 0, -1)
        ) + '<clipPath id="r10"/>'
        self.reject(
            f"<svg><defs>{reverse_order}</defs></svg>".encode(),
            "SVG_REFERENCE_DEPTH",
        )

    def test_cumulative_transform_overflow_rejected(self) -> None:
        transforms = " ".join("scale(1000000)" for _ in range(2))
        self.reject(
            f'<svg><g transform="{transforms}"/></svg>'.encode(),
            "SVG_GEOMETRY_LIMIT",
        )


class StrictPngTests(unittest.TestCase):
    def test_accepts_exact_bounded_rgb_and_rgba(self) -> None:
        for rgba in (False, True):
            result = validate_png_bytes(png_bytes(3, 2, rgba=rgba), expected_width=3, expected_height=2)
            self.assertIsInstance(result, PNGValidationResult)
            self.assertEqual((result.width, result.height), (3, 2))

    def test_rejects_signature_crc_dimensions_chunks_and_trailing(self) -> None:
        good = png_bytes()
        cases = []
        cases.append((b"bad" + good[3:], "PNG_SIGNATURE"))
        bad_crc = bytearray(good)
        bad_crc[29] ^= 1
        cases.append((bytes(bad_crc), "PNG_CRC"))
        cases.append((good, "PNG_DIMENSIONS"))
        cases.append((good + b"x", "PNG_TRAILING_DATA"))
        unknown = good[:33] + _chunk(b"ABCD", b"") + good[33:]
        cases.append((unknown, "PNG_CRITICAL_CHUNK"))
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SvgSecurityError) as rejected:
                    validate_png_bytes(
                        payload,
                        expected_width=9 if code == "PNG_DIMENSIONS" else 2,
                        expected_height=2,
                    )
                self.assertEqual(rejected.exception.code, code)

    def test_rejects_invalid_filter_truncated_and_inflate_overrun(self) -> None:
        def custom_rows(rows: bytes) -> bytes:
            ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
            return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")

        for payload, code in (
            (custom_rows(b"\x05" + b"\0" * 4), "PNG_FILTER"),
            (custom_rows(b"\x00" + b"\0" * 3), "PNG_INFLATE_SIZE"),
            (custom_rows(b"\x00" + b"\0" * 5), "PNG_INFLATE_SIZE"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(SvgSecurityError) as rejected:
                    validate_png_bytes(payload, expected_width=1, expected_height=1)
                self.assertEqual(rejected.exception.code, code)


class RasterizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.executable = self.root / "resvg"
        self.executable.write_bytes(b"fake pinned resvg")
        self.executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.sandbox = self.root / "sandbox-exec"
        self.sandbox.write_bytes(b"fake sandbox")
        self.sandbox.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.profile = self.root / "svg.sb"
        self.profile.write_text("(version 1)\n(deny default)\n", encoding="utf-8")
        self.reviewed_profile = self.root / "resvg-sandbox.sb"
        # Use the same immutable import-time reviewed bytes as the loader. This
        # keeps the fixture coherent even if a developer edits the source
        # profile concurrently with a long discovery run; production still
        # fails closed against any machine-profile drift.
        self.reviewed_profile.write_bytes(BUNDLED_SANDBOX_PROFILE_BYTES)
        self.reviewed_profile.chmod(0o600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manifest(self, **overrides: object) -> dict[str, object]:
        manifest: dict[str, object] = {
            "schema_version": 1,
            "executable_path": str(self.executable),
            "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
            "version": "resvg 0.test",
            "sandbox_executable_path": str(self.sandbox),
            "sandbox_executable_sha256": hashlib.sha256(self.sandbox.read_bytes()).hexdigest(),
            "sandbox_profile_path": str(self.profile),
            "sandbox_profile_sha256": hashlib.sha256(self.profile.read_bytes()).hexdigest(),
        }
        manifest.update(overrides)
        return manifest

    def probe(self, _manifest: object) -> tuple[str, bytes]:
        return "resvg 0.test", png_bytes(1, 1)

    def test_missing_symlink_hash_version_and_sandbox_fail_closed(self) -> None:
        self.assertFalse(ResvgRasterizer(None).preflight().available)

        symlink = self.root / "link"
        symlink.symlink_to(self.executable)
        self.assertEqual(
            ResvgRasterizer(self.manifest(executable_path=str(symlink)), probe=self.probe).preflight().code,
            "RASTERIZER_EXECUTABLE_UNSAFE",
        )
        self.assertEqual(
            ResvgRasterizer(self.manifest(executable_sha256="0" * 64), probe=self.probe).preflight().code,
            "RASTERIZER_HASH_MISMATCH",
        )
        self.assertEqual(
            ResvgRasterizer(self.manifest(), probe=lambda _: ("wrong", png_bytes(1, 1))).preflight().code,
            "RASTERIZER_VERSION_MISMATCH",
        )
        self.assertEqual(
            ResvgRasterizer(self.manifest(), probe=lambda _: (_ for _ in ()).throw(RuntimeError())).preflight().code,
            "RASTERIZER_SANDBOX_FAILED",
        )

    def test_injected_backend_never_reports_production_available(self) -> None:
        checked = ResvgRasterizer(self.manifest(), probe=self.probe).preflight()
        self.assertTrue(checked.checks_ok)
        self.assertFalse(checked.available)
        self.assertEqual(checked.code, "RASTERIZER_TEST_BACKEND")

    def test_machine_manifest_loader_is_strict_and_permission_gated(self) -> None:
        path = self.root / "manifest.json"
        manifest = self.manifest(
            executable_path=str(self.executable.resolve()),
            sandbox_executable_path="/usr/bin/sandbox-exec",
            sandbox_executable_sha256=hashlib.sha256(
                Path("/usr/bin/sandbox-exec").read_bytes()
            ).hexdigest(),
            sandbox_profile_path=str(self.reviewed_profile.resolve()),
            sandbox_profile_sha256=BUNDLED_SANDBOX_PROFILE_SHA256,
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        loaded, code = load_resvg_manifest(path)
        self.assertEqual(code, "OK")
        self.assertEqual(loaded, manifest)

        self.reviewed_profile.chmod(0o644)
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_UNSAFE")
        self.reviewed_profile.chmod(0o600)

        path.chmod(0o644)
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_UNSAFE")
        path.chmod(0o600)
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_INVALID")
        path.write_text(json.dumps({**manifest, "extra": True}), encoding="utf-8")
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_INVALID")

        link = self.root / "manifest-link.json"
        link.symlink_to(path)
        self.assertEqual(load_resvg_manifest(link)[1], "RASTERIZER_MANIFEST_UNSAFE")

    def test_manifest_schema_version_requires_exact_plain_integer_one(self) -> None:
        path = self.root / "manifest.json"
        manifest = self.manifest(
            executable_path=str(self.executable.resolve()),
            sandbox_executable_path="/usr/bin/sandbox-exec",
            sandbox_executable_sha256=hashlib.sha256(
                Path("/usr/bin/sandbox-exec").read_bytes()
            ).hexdigest(),
            sandbox_profile_path=str(self.reviewed_profile.resolve()),
            sandbox_profile_sha256=BUNDLED_SANDBOX_PROFILE_SHA256,
        )
        for hostile_version in (True, 1.0):
            with self.subTest(schema_version=hostile_version):
                hostile = {**manifest, "schema_version": hostile_version}
                path.write_text(json.dumps(hostile), encoding="utf-8")
                path.chmod(0o600)
                self.assertEqual(
                    load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_INVALID"
                )
                self.assertEqual(
                    ResvgRasterizer(hostile, probe=self.probe).preflight().code,
                    "RASTERIZER_MANIFEST_INVALID",
                )

    def test_manifest_environment_override_must_be_explicit_absolute_path(self) -> None:
        self.assertTrue(DEFAULT_RESVG_MANIFEST_PATH.is_absolute())
        with patch.dict(os.environ, {RESVG_MANIFEST_ENV: "relative.json"}, clear=False):
            self.assertEqual(
                ResvgRasterizer.from_machine_manifest().preflight().code,
                "RASTERIZER_MANIFEST_INVALID",
            )

    def test_reviewed_profile_cannot_be_replaced_by_self_signed_profile(self) -> None:
        path = self.root / "manifest.json"
        manifest = self.manifest()
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_INVALID")

        reviewed_manifest = self.manifest(
            executable_path=str(self.executable.resolve()),
            sandbox_executable_path="/usr/bin/sandbox-exec",
            sandbox_executable_sha256=hashlib.sha256(
                Path("/usr/bin/sandbox-exec").read_bytes()
            ).hexdigest(),
            sandbox_profile_path=str(self.reviewed_profile.resolve()),
            sandbox_profile_sha256=BUNDLED_SANDBOX_PROFILE_SHA256,
        )
        path.write_text(json.dumps(reviewed_manifest), encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(load_resvg_manifest(path)[1], "OK")
        self.reviewed_profile.write_bytes(BUNDLED_SANDBOX_PROFILE_BYTES + b"\n; drift")
        self.assertEqual(load_resvg_manifest(path)[1], "RASTERIZER_MANIFEST_UNSAFE")

    def test_manifest_is_independent_of_runtime_bundle_copy_path(self) -> None:
        path = self.root / "manifest.json"
        manifest = self.manifest(
            executable_path=str(self.executable.resolve()),
            sandbox_executable_path="/usr/bin/sandbox-exec",
            sandbox_executable_sha256=hashlib.sha256(
                Path("/usr/bin/sandbox-exec").read_bytes()
            ).hexdigest(),
            sandbox_profile_path=str(self.reviewed_profile.resolve()),
            sandbox_profile_sha256=BUNDLED_SANDBOX_PROFILE_SHA256,
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        other_copy = self.root / "other-copy.sb"
        other_copy.write_bytes(BUNDLED_SANDBOX_PROFILE_BYTES + b"\n; concurrent edit")
        with patch("svg_security.BUNDLED_SANDBOX_PROFILE", other_copy):
            self.assertEqual(load_resvg_manifest(path)[1], "OK")

    def test_reviewed_profile_denies_network_and_has_no_home_read_scope(self) -> None:
        profile = BUNDLED_SANDBOX_PROFILE.read_text(encoding="utf-8")
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn('(literal (param "RESVG_EXECUTABLE"))', profile)
        self.assertNotIn("/Users", profile)
        self.assertNotIn("(allow network", profile)
        self.assertNotIn("mach-", profile)

    def test_production_runner_contract_has_no_shell_and_no_memory_cap_claim(self) -> None:
        manifest = self.manifest(
            executable_path=str(self.executable.resolve()),
            sandbox_executable_path="/usr/bin/sandbox-exec",
            sandbox_executable_sha256=hashlib.sha256(
                Path("/usr/bin/sandbox-exec").read_bytes()
            ).hexdigest(),
            sandbox_profile_path=str(self.reviewed_profile.resolve()),
            sandbox_profile_sha256=BUNDLED_SANDBOX_PROFILE_SHA256,
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_process(argv: list[str], **kwargs: object) -> dict[str, object]:
            calls.append((argv, kwargs))
            if argv[-1] == "--version":
                return {"returncode": 0, "stdout": b"resvg 0.test\n", "stderr": b""}
            Path(argv[-1]).write_bytes(png_bytes(1, 1))
            return {"returncode": 0, "stdout": b"", "stderr": b""}

        rasterizer = ResvgRasterizer._production_for_configure(manifest)
        with patch("svg_security._run_bounded_process", side_effect=fake_process):
            checked = rasterizer.preflight()
        self.assertTrue(checked.available)
        raster_argv, raster_kwargs = calls[-1]
        self.assertIn("--quiet", raster_argv)
        self.assertIn("--skip-system-fonts", raster_argv)
        self.assertIn("--resources-dir", raster_argv)
        self.assertNotIn("shell", raster_kwargs)
        self.assertNotIn("memory_bytes", raster_kwargs)

    def test_hostile_svg_never_calls_rasterizer(self) -> None:
        calls = []

        def runner(*_args: object, **_kwargs: object) -> object:
            calls.append(True)
            raise AssertionError("must not run")

        rasterizer = ResvgRasterizer(self.manifest(), probe=self.probe, runner=runner)
        with self.assertRaises(SvgSecurityError) as rejected:
            sanitize_and_rasterize(
                b"<svg><script>evil</script></svg>",
                requested_width=2,
                requested_height=2,
                rasterizer=rasterizer,
            )
        self.assertEqual(rejected.exception.code, "SVG_ELEMENT_FORBIDDEN")
        self.assertEqual(calls, [])

    def test_injected_runner_exercises_orchestrator_and_png_gate(self) -> None:
        calls = []

        def runner(argv: list[str], **_kwargs: object) -> object:
            calls.append(argv)
            Path(argv[-1]).write_bytes(png_bytes(2, 2))
            return {"returncode": 0, "stdout": b"", "stderr": b""}

        rasterizer = ResvgRasterizer(self.manifest(), probe=self.probe, runner=runner)
        result, png = sanitize_and_rasterize(
            b'<svg viewBox="0 0 2 2"><rect width="2" height="2"/></svg>',
            requested_width=2,
            requested_height=2,
            rasterizer=rasterizer,
        )
        self.assertEqual(result.metadata["requested_width"], 2)
        self.assertEqual(png.png_sha256, hashlib.sha256(png_bytes(2, 2)).hexdigest())
        self.assertEqual(len(calls), 1)

        def timeout_runner(*_args: object, **_kwargs: object) -> object:
            raise TimeoutError

        timed = ResvgRasterizer(self.manifest(), probe=self.probe, runner=timeout_runner)
        sanitized = sanitize_svg_bytes(b"<svg/>", requested_width=2, requested_height=2)
        with self.assertRaises(SvgSecurityError) as rejected:
            timed.rasterize(sanitized)
        self.assertEqual(rejected.exception.code, "RASTERIZER_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

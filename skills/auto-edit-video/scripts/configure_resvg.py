#!/usr/bin/env python3
"""Explicitly pin and verify the local production resvg rasterizer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from svg_security import (
    BUNDLED_SANDBOX_PROFILE,
    BUNDLED_SANDBOX_PROFILE_BYTES,
    BUNDLED_SANDBOX_PROFILE_SHA256,
    DEFAULT_RESVG_MANIFEST_PATH,
    RASTER_TIMEOUT_SECONDS,
    RESVG_MANIFEST_ENV,
    SANDBOX_EXECUTABLE,
    ResvgRasterizer,
    _run_bounded_process,
)


class ConfigureError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigureError("file is not regular")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _canonical_executable(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ConfigureError("resvg path must be absolute")
    try:
        info = candidate.lstat()
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigureError("resvg executable is missing") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(candidate, os.X_OK)
        or str(canonical) != str(candidate)
    ):
        raise ConfigureError("resvg executable path is unsafe or non-canonical")
    return candidate


def _discover_version(executable: Path) -> str:
    provisional = {
        "sandbox_executable_path": str(SANDBOX_EXECUTABLE),
        "sandbox_profile_path": str(BUNDLED_SANDBOX_PROFILE),
        "executable_path": str(executable),
    }
    with tempfile.TemporaryDirectory(prefix="auto-edit-svg-configure-") as directory:
        root = Path(directory).resolve(strict=True)
        os.chmod(root, 0o700)
        result = _run_bounded_process(
            ResvgRasterizer._sandbox_argv(provisional, root) + ["--version"],
            cwd=str(root),
            env=ResvgRasterizer._fixed_env(root),
            timeout=RASTER_TIMEOUT_SECONDS,
        )
    if result.get("returncode") != 0 or result.get("stderr"):
        raise ConfigureError("sandboxed resvg version probe failed")
    stdout = result.get("stdout")
    try:
        version = stdout.decode("ascii").strip() if isinstance(stdout, bytes) else ""
    except UnicodeDecodeError as exc:
        raise ConfigureError("resvg version is not ASCII") from exc
    if (
        not version
        or len(version) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in version)
    ):
        raise ConfigureError("resvg version is invalid")
    return version


def build_verified_manifest(
    executable_value: str | os.PathLike[str], profile_path: Path
) -> dict[str, Any]:
    executable = _canonical_executable(executable_value)
    if not SANDBOX_EXECUTABLE.exists():
        raise ConfigureError("/usr/bin/sandbox-exec is unavailable")
    version = _discover_version(executable)
    manifest = {
        "schema_version": 1,
        "executable_path": str(executable),
        "executable_sha256": _sha256(executable),
        "version": version,
        "sandbox_executable_path": str(SANDBOX_EXECUTABLE),
        "sandbox_executable_sha256": _sha256(SANDBOX_EXECUTABLE),
        "sandbox_profile_path": str(profile_path),
        "sandbox_profile_sha256": BUNDLED_SANDBOX_PROFILE_SHA256,
    }
    checked = ResvgRasterizer._production_for_configure(manifest).preflight()
    if not checked.available or not checked.checks_ok or checked.code != "OK":
        raise ConfigureError(f"production verification failed: {checked.code}")
    return manifest


def _write_bytes_atomic(payload: bytes, destination: Path) -> None:
    if not destination.is_absolute():
        raise ConfigureError("manifest path must be absolute")
    parent = destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def write_manifest_atomic(manifest: dict[str, Any], destination: Path) -> None:
    payload = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    _write_bytes_atomic(payload, destination)


def _canonical_manifest_destination(value: str | os.PathLike[str]) -> Path:
    """Canonicalize only the parent, never an existing final symlink target."""
    destination = Path(value)
    if not destination.is_absolute() or not destination.name:
        raise ConfigureError("manifest path must be an absolute file path")
    parent = destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        canonical_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ConfigureError("manifest parent cannot be canonicalized") from exc
    return canonical_parent / destination.name


def _default_destination() -> Path:
    override = os.environ.get(RESVG_MANIFEST_ENV)
    if override is None:
        return DEFAULT_RESVG_MANIFEST_PATH
    path = Path(override)
    if not path.is_absolute():
        raise ConfigureError(f"{RESVG_MANIFEST_ENV} must be an absolute path")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pin and sandbox-verify a production resvg executable."
    )
    parser.add_argument("--resvg", required=True, help="canonical absolute resvg path")
    parser.add_argument("--manifest", help="absolute output path (default: machine config)")
    arguments = parser.parse_args()
    try:
        requested_destination = (
            Path(arguments.manifest) if arguments.manifest else _default_destination()
        )
        destination = _canonical_manifest_destination(requested_destination)
        profile_path = destination.with_name("resvg-sandbox.sb")
        _write_bytes_atomic(BUNDLED_SANDBOX_PROFILE_BYTES, profile_path)
        manifest = build_verified_manifest(arguments.resvg, profile_path)
        write_manifest_atomic(manifest, destination)
    except (ConfigureError, OSError) as exc:
        parser.error(str(exc))
    print(f"configured verified resvg {manifest['version']} at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

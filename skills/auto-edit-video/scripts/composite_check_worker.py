#!/usr/bin/env python3
"""Bounded child-process bridge for optional full-composite diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024


def _read_request(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size <= 0
        or info.st_size > MAX_REQUEST_BYTES
    ):
        raise ValueError("unsafe composite-check request")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("composite-check request must be an object")
    return payload


def _atomic_result(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("composite-check result is too large")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    request_path = Path(args.request).absolute()
    result_path = Path(args.result).absolute()
    result_is_owned = False
    try:
        request = _read_request(request_path)
        scratch = Path(request.get("scratch", "")).resolve(strict=True)
        if (
            request_path.parent.resolve(strict=True) != scratch
            or result_path.parent.resolve(strict=True) != scratch
            or result_path.name != "result.json"
        ):
            raise ValueError("composite-check IPC escaped owned scratch")
        result_is_owned = True
        import composite_check

        receipt = composite_check.analyze(request)
        if not isinstance(receipt, dict):
            raise ValueError("composite-check analyzer returned no receipt")
        _atomic_result(result_path, {"ok": True, "composition_check": receipt})
        return 0
    except Exception:
        if result_is_owned:
            try:
                _atomic_result(
                    result_path,
                    {"ok": False, "reason": "analysis_unavailable"},
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

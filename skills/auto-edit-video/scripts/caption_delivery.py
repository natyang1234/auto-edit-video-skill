#!/usr/bin/env python3
"""Immutable transcript identity and instance-bound caption delivery v2.

Only a loopback Ollama provider is supported.  Caption artifacts bind the raw
ASR revision, deterministic segmentation, ordered editor segments, and every
post-cut caption occurrence.  Required delivery is validated before a direct
renderer creates staging or output bytes.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import contract_registry


CAPTION_REL = Path("working/caption_delivery_v2.json")
SOURCE_CURRENT_REL = Path("working/transcript_source_current.json")
SOURCE_VERSIONS_REL = Path("working/transcript_sources")
SEGMENTATION_REL = Path("working/caption_segmentation.json")
SCHEMA_VERSION = 2
SOURCE_SCHEMA_VERSION = 1
SEGMENTATION_SCHEMA_VERSION = 1
PCM_FORMAT = {
    "sample_rate_hz": 48000,
    "channels": 2,
    "sample_format": "s16le",
}
CHUNKER = {
    "name": "readable_caption_segments",
    "version": 1,
    "config": {
        "gap_us": 700000,
        "max_duration_us": 5500000,
        "max_display_units": 56,
        "sentence_flush_min_us": 800000,
        "clause_flush_min_us": 2400000,
    },
}
IDENTITY_REASONS = {"brand", "proper_name", "code", "number_unit"}
MAX_CAPTION_SOURCES = 20_000
MAX_CAPTION_TEXT_CHARS = 4_000
MAX_CAPTION_ARTIFACT_BYTES = 64 * 1024 * 1024
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+/-]*|\d+(?:\.\d+)?(?:%|[A-Za-z]+)?")
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
TRANSLATION_NORMALISE_RE = re.compile(r"[\W_]+", re.UNICODE)


class CaptionDeliveryError(ValueError):
    """Stable, user-visible fail-closed caption error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class _ProjectTrust:
    root: Path
    working: Path
    root_fd: int
    working_fd: int
    root_identity: tuple[int, int]
    working_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _TranscriptSourceTrust:
    project: _ProjectTrust
    versions_fd: int
    versions_identity: tuple[int, int]


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _verify_project_trust(trust: _ProjectTrust) -> None:
    """Require the pathname identities to still name the opened directories."""
    try:
        root_now = os.stat(trust.root, follow_symlinks=False)
        working_now = os.stat("working", dir_fd=trust.root_fd, follow_symlinks=False)
        opened_root = os.fstat(trust.root_fd)
        opened_working = os.fstat(trust.working_fd)
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    if (
        not stat.S_ISDIR(root_now.st_mode)
        or not stat.S_ISDIR(working_now.st_mode)
        or _identity(root_now) != trust.root_identity
        or _identity(opened_root) != trust.root_identity
        or _identity(working_now) != trust.working_identity
        or _identity(opened_working) != trust.working_identity
    ):
        raise CaptionDeliveryError(
            "caption_project_changed", "project root or working directory identity changed"
        )


@contextlib.contextmanager
def _project_trust(project_dir: Path) -> Iterator[_ProjectTrust]:
    root, working = _trusted_working_root(project_dir)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = working_fd = -1
    try:
        root_fd = os.open(root, flags)
        working_fd = os.open("working", flags, dir_fd=root_fd)
        trust = _ProjectTrust(
            root=root,
            working=working,
            root_fd=root_fd,
            working_fd=working_fd,
            root_identity=_identity(os.fstat(root_fd)),
            working_identity=_identity(os.fstat(working_fd)),
        )
        _verify_project_trust(trust)
        yield trust
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    finally:
        if working_fd >= 0:
            os.close(working_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _verify_transcript_source_trust(trust: _TranscriptSourceTrust) -> None:
    _verify_project_trust(trust.project)
    try:
        versions_now = os.stat(
            SOURCE_VERSIONS_REL.name,
            dir_fd=trust.project.working_fd,
            follow_symlinks=False,
        )
        opened_versions = os.fstat(trust.versions_fd)
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    if (
        not stat.S_ISDIR(versions_now.st_mode)
        or _identity(versions_now) != trust.versions_identity
        or _identity(opened_versions) != trust.versions_identity
    ):
        raise CaptionDeliveryError(
            "caption_project_changed", "transcript source directory identity changed"
        )


@contextlib.contextmanager
def _transcript_source_trust(project_dir: Path) -> Iterator[_TranscriptSourceTrust]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    versions_fd = -1
    with _project_trust(project_dir) as project_trust:
        try:
            try:
                os.mkdir(
                    SOURCE_VERSIONS_REL.name,
                    mode=0o700,
                    dir_fd=project_trust.working_fd,
                )
            except FileExistsError:
                pass
            _verify_project_trust(project_trust)
            versions_fd = os.open(
                SOURCE_VERSIONS_REL.name,
                flags,
                dir_fd=project_trust.working_fd,
            )
            trust = _TranscriptSourceTrust(
                project=project_trust,
                versions_fd=versions_fd,
                versions_identity=_identity(os.fstat(versions_fd)),
            )
            _verify_transcript_source_trust(trust)
            yield trust
        except CaptionDeliveryError:
            raise
        except OSError as exc:
            raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
        finally:
            if versions_fd >= 0:
                os.close(versions_fd)


def _atomic_write_to_fd(
    *,
    directory_fd: int,
    name: str,
    payload: Any,
    verify: Callable[[], None],
) -> None:
    """Atomically adopt canonical bytes beneath an already trusted directory fd."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise CaptionDeliveryError("caption_project_changed", "unsafe adoption filename")
    verify()
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        raw = canonical_bytes(payload)
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        verify()
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        verify()
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("caption_project_changed", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _atomic_write_at(
    trust: _ProjectTrust,
    *,
    working: bool,
    name: str,
    payload: Any,
) -> None:
    """Atomic no-follow write relative to a directory held open across provider wait."""
    directory_fd = trust.working_fd if working else trust.root_fd
    _atomic_write_to_fd(
        directory_fd=directory_fd,
        name=name,
        payload=payload,
        verify=lambda: _verify_project_trust(trust),
    )


def _trusted_working_root(project_dir: Path) -> tuple[Path, Path]:
    root = project_dir.resolve()
    if not root.is_dir():
        raise CaptionDeliveryError("caption_project_invalid", "project directory is missing")
    working = root / "working"
    if working.is_symlink() or not working.is_dir() or working.resolve() != working:
        raise CaptionDeliveryError("caption_project_invalid", "working directory is not owned")
    return root, working


def _owned_artifact(project_dir: Path, relative: Path) -> Path:
    root, _working = _trusted_working_root(project_dir)
    path = root / relative
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise CaptionDeliveryError("caption_artifact_invalid", f"unsafe artifact: {relative}")
    # Every existing parent below the project must be a real directory.  This
    # prevents a symlinked working subdirectory from redirecting reads/writes.
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise CaptionDeliveryError("caption_artifact_invalid", f"unsafe parent: {relative}")
    return path


def _ensure_owned_subdir(project_dir: Path, relative: Path) -> Path:
    root, _working = _trusted_working_root(project_dir)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CaptionDeliveryError("caption_project_invalid", f"unsafe directory: {relative}")
        if current.exists():
            if not current.is_dir():
                raise CaptionDeliveryError("caption_project_invalid", f"not a directory: {relative}")
        else:
            current.mkdir(mode=0o700)
        if current.resolve() != current:
            raise CaptionDeliveryError("caption_project_invalid", f"aliased directory: {relative}")
    return current


def canonical_bytes(payload: Any) -> bytes:
    contract_registry._reject_nonfinite(payload)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(canonical_bytes(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> Any:
    try:
        return contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError("caption_artifact_invalid", str(exc)) from exc


def _read_regular_at(
    directory_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> bytes | None:
    """Read a bounded regular file by trusted-directory-relative name."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise CaptionDeliveryError("transcript_source_invalid", "unsafe source filename")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CAPTION_ARTIFACT_BYTES:
            raise CaptionDeliveryError(
                "transcript_source_invalid", f"unsafe source artifact: {name}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_CAPTION_ARTIFACT_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CAPTION_ARTIFACT_BYTES:
                raise CaptionDeliveryError(
                    "transcript_source_invalid", f"source artifact too large: {name}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CaptionDeliveryError("transcript_source_invalid", f"missing source artifact: {name}")
    except CaptionDeliveryError:
        raise
    except OSError as exc:
        raise CaptionDeliveryError("transcript_source_invalid", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_json_bytes(raw: bytes, *, code: str) -> Any:
    try:
        return contract_registry.load_artifact_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError(code, str(exc)) from exc


def _load_owned_json_bytes(project_dir: Path, relative: Path) -> tuple[Any, bytes]:
    """Read one owned regular artifact exactly once through O_NOFOLLOW."""
    path = _owned_artifact(project_dir, relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CAPTION_ARTIFACT_BYTES:
            raise CaptionDeliveryError(
                "caption_artifact_invalid", f"artifact size/type is unsafe: {relative}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CAPTION_ARTIFACT_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CAPTION_ARTIFACT_BYTES:
                raise CaptionDeliveryError(
                    "caption_artifact_invalid", f"artifact is too large: {relative}"
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
        payload = contract_registry.load_artifact_text(raw.decode("utf-8"))
        return payload, raw
    except CaptionDeliveryError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, contract_registry.ContractError) as exc:
        raise CaptionDeliveryError("caption_artifact_invalid", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owned_source(project_dir: Path, manifest: dict[str, Any]) -> Path:
    root = project_dir.resolve()
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise CaptionDeliveryError("transcript_source_invalid", "manifest source is missing")
    relative = str(source.get("staged_path") or "")
    entry = root / relative
    if entry.is_symlink():
        raise CaptionDeliveryError("transcript_source_invalid", "source media is a symlink")
    resolved = entry.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise CaptionDeliveryError("transcript_source_invalid", "source media is not project-owned")
    declared = str(source.get("sha256") or "")
    if not SHA_RE.fullmatch(declared) or _file_sha256(resolved) != declared:
        raise CaptionDeliveryError("transcript_source_invalid", "source media hash mismatch")
    return resolved


def decoded_pcm_sha256(project_dir: Path, manifest: dict[str, Any]) -> str:
    """Hash ffmpeg-decoded audio under the fixed 48k/stereo/s16le contract."""
    source = _owned_source(project_dir, manifest)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CaptionDeliveryError("pcm_decode_unavailable", "ffmpeg is unavailable")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    digest = hashlib.sha256()
    decoded_bytes = 0
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log)
        assert process.stdout is not None
        try:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                decoded_bytes += len(chunk)
                digest.update(chunk)
            try:
                return_code = process.wait(timeout=3600)
            except subprocess.TimeoutExpired as exc:
                raise CaptionDeliveryError(
                    "pcm_decode_failed", "ffmpeg decode timed out"
                ) from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        error_log.seek(0)
        stderr = error_log.read()
    if return_code != 0 or decoded_bytes == 0:
        detail = stderr.decode("utf-8", "replace")[-500:]
        raise CaptionDeliveryError("pcm_decode_failed", detail or "decoded PCM was empty")
    return digest.hexdigest()


def _seconds_us(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionDeliveryError("caption_contract_invalid", f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise CaptionDeliveryError("caption_contract_invalid", f"{label} must be finite and nonnegative")
    return int(round(numeric * 1_000_000))


def _raw_words(data: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise CaptionDeliveryError("transcript_source_invalid", "segments must be an array")
    for segment in segments:
        if not isinstance(segment, dict):
            raise CaptionDeliveryError("transcript_source_invalid", "segment must be an object")
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for raw in words:
            if not isinstance(raw, dict):
                raise CaptionDeliveryError("transcript_source_invalid", "word must be an object")
            text = str(raw.get("word", ""))
            if not text.strip():
                continue
            start_us = _seconds_us(raw.get("start", segment.get("start", 0)), "raw word start")
            end_us = _seconds_us(raw.get("end", segment.get("end", 0)), "raw word end")
            if end_us < start_us:
                raise CaptionDeliveryError("transcript_source_invalid", "raw word end precedes start")
            speaker = raw.get("speaker", segment.get("speaker"))
            if speaker is not None and not isinstance(speaker, str):
                raise CaptionDeliveryError("transcript_source_invalid", "speaker must be string or null")
            output.append(
                {
                    "source_word_index": len(output),
                    "start_us": start_us,
                    "end_us": end_us,
                    "text": text,
                    "speaker": speaker,
                }
            )
    if not output:
        raise CaptionDeliveryError("transcript_source_invalid", "raw transcript has no timed words")
    return output


def capture_transcript_source(
    project_dir: Path,
    manifest: dict[str, Any],
    data: dict[str, Any],
    *,
    model: str,
    force_retranscription: bool = False,
) -> dict[str, Any]:
    """Persist immutable raw ASR identity before any text correction occurs."""
    if not MODEL_RE.fullmatch(model):
        raise CaptionDeliveryError("transcript_source_invalid", "model name is invalid")
    params = data.get("decoding_params", {})
    if not isinstance(params, dict):
        raise CaptionDeliveryError("transcript_source_invalid", "decoding_params must be an object")
    contract_registry._reject_nonfinite(params)
    raw_words = _raw_words(data)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    with _transcript_source_trust(project_dir) as trust:
        current_raw = _read_regular_at(
            trust.project.working_fd,
            SOURCE_CURRENT_REL.name,
            missing_ok=True,
        )
        previous: dict[str, Any] | None = None
        if current_raw is not None:
            pointer = _decode_json_bytes(current_raw, code="transcript_source_invalid")
            revision = str(pointer.get("revision") or "") if isinstance(pointer, dict) else ""
            if SHA_RE.fullmatch(revision):
                previous_raw = _read_regular_at(
                    trust.versions_fd,
                    f"{revision}.json",
                    missing_ok=True,
                )
                if previous_raw is not None:
                    decoded = _decode_json_bytes(
                        previous_raw, code="transcript_source_invalid"
                    )
                    if isinstance(decoded, dict):
                        previous = decoded
        generation = 0
        if previous is not None:
            value = previous.get("source_generation")
            if type(value) is not int or value < 0:
                raise CaptionDeliveryError(
                    "transcript_source_invalid", "source_generation is invalid"
                )
            generation = value + (1 if force_retranscription else 0)

        pcm_sha256 = decoded_pcm_sha256(trust.project.root, manifest)
        _verify_transcript_source_trust(trust)
        payload: dict[str, Any] = {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "revision": "",
            "source_media_sha256": str(source.get("sha256") or ""),
            "audio_stream_index": 0,
            "decoded_pcm": {**PCM_FORMAT, "sha256": pcm_sha256},
            "engine": str(data.get("engine") or "openai-whisper"),
            "engine_version": str(
                data.get("engine_version") or data.get("version") or "unknown"
            ),
            "model": model,
            "language": str(data.get("language") or "unknown"),
            "decoding_params": params,
            "source_generation": generation,
            "raw_words": raw_words,
        }
        revision_payload = dict(payload)
        revision_payload.pop("revision")
        revision = contract_registry.canonical_hash(revision_payload)
        payload["revision"] = revision
        errors = contract_registry.validate_artifact("transcript_source", payload)
        if errors:
            raise CaptionDeliveryError("transcript_source_invalid", "; ".join(errors))
        expected = canonical_bytes(payload)
        version_name = f"{revision}.json"
        existing = _read_regular_at(
            trust.versions_fd,
            version_name,
            missing_ok=True,
        )
        if existing is not None:
            if existing != expected:
                raise CaptionDeliveryError(
                    "transcript_source_immutable", "revision bytes differ"
                )
        else:
            _atomic_write_to_fd(
                directory_fd=trust.versions_fd,
                name=version_name,
                payload=payload,
                verify=lambda: _verify_transcript_source_trust(trust),
            )
        _atomic_write_to_fd(
            directory_fd=trust.project.working_fd,
            name=SOURCE_CURRENT_REL.name,
            payload={
                "schema_version": 1,
                "revision": revision,
                "path": (SOURCE_VERSIONS_REL / version_name).as_posix(),
                "artifact_sha256": hashlib.sha256(expected).hexdigest(),
            },
            verify=lambda: _verify_transcript_source_trust(trust),
        )
        return payload


def load_current_source(project_dir: Path) -> dict[str, Any]:
    root = project_dir.resolve()
    pointer = _load_json(_owned_artifact(root, SOURCE_CURRENT_REL))
    if not isinstance(pointer, dict) or not SHA_RE.fullmatch(str(pointer.get("revision") or "")):
        raise CaptionDeliveryError("transcript_source_missing")
    relative = str(pointer.get("path") or "")
    expected = (SOURCE_VERSIONS_REL / f"{pointer['revision']}.json").as_posix()
    if relative != expected:
        raise CaptionDeliveryError("transcript_source_invalid", "current pointer path mismatch")
    path = root / relative
    if path.is_symlink() or not path.is_file() or path.resolve().parent != (root / SOURCE_VERSIONS_REL).resolve():
        raise CaptionDeliveryError("transcript_source_invalid", "current source path is unsafe")
    payload = _load_json(path)
    if payload.get("revision") != pointer["revision"]:
        raise CaptionDeliveryError("transcript_source_invalid", "current pointer revision mismatch")
    if hashlib.sha256(path.read_bytes()).hexdigest() != pointer.get("artifact_sha256"):
        raise CaptionDeliveryError("transcript_source_invalid", "current source bytes changed")
    errors = contract_registry.validate_artifact("transcript_source", payload)
    if errors:
        raise CaptionDeliveryError("transcript_source_invalid", "; ".join(errors))
    return payload


def build_segmentation(project_dir: Path, transcript: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = load_current_source(project_dir)
    words = transcript.get("words")
    captions = transcript.get("caption_segments") or transcript.get("segments")
    if not isinstance(words, list) or not isinstance(captions, list):
        raise CaptionDeliveryError("caption_segmentation_invalid", "transcript words/captions missing")
    if len(words) > 100_000 or len(captions) > MAX_CAPTION_SOURCES:
        raise CaptionDeliveryError("caption_segmentation_invalid", "transcript exceeds caption limits")
    word_indexes: dict[str, int] = {}
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            raise CaptionDeliveryError("caption_segmentation_invalid", "word must be object")
        word_id = str(word.get("id") or "")
        if word_id:
            if word_id in word_indexes:
                raise CaptionDeliveryError("caption_segmentation_invalid", "duplicate word id")
            word_indexes[word_id] = index
    spans: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    ordinal_by_span: dict[tuple[Any, ...], int] = {}
    for segment in captions:
        if not isinstance(segment, dict):
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption must be object")
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MAX_CAPTION_TEXT_CHARS:
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption text is too long")
        start_us = _seconds_us(segment.get("start", 0), "caption start")
        end_us = _seconds_us(segment.get("end", 0), "caption end")
        if end_us <= start_us:
            raise CaptionDeliveryError("caption_segmentation_invalid", "caption end must exceed start")
        word_ids = segment.get("word_ids")
        indices: list[int] = []
        if isinstance(word_ids, list) and word_ids:
            try:
                indices = [word_indexes[str(item)] for item in word_ids]
            except KeyError as exc:
                raise CaptionDeliveryError("caption_segmentation_invalid", "caption references unknown word") from exc
            if indices != list(range(indices[0], indices[-1] + 1)):
                raise CaptionDeliveryError("caption_segmentation_invalid", "caption word span is not contiguous")
        if indices:
            if indices[-1] >= len(source.get("raw_words", [])):
                raise CaptionDeliveryError(
                    "caption_segmentation_invalid", "caption span exceeds raw source words"
                )
            span = {
                "first_source_word_index": indices[0],
                "last_source_word_index": indices[-1],
                "fallback_start_us": None,
                "fallback_end_us": None,
            }
            key: tuple[Any, ...] = (indices[0], indices[-1])
        else:
            span = {
                "first_source_word_index": None,
                "last_source_word_index": None,
                "fallback_start_us": start_us,
                "fallback_end_us": end_us,
            }
            key = ("fallback", start_us, end_us)
        within = ordinal_by_span.get(key, 0)
        ordinal_by_span[key] = within + 1
        spans.append({**span, "within_span_ordinal": within})
        source_records.append(
            {
                "text": text,
                "source_start_us": start_us,
                "source_end_us": end_us,
                "span": span,
                "within_span_ordinal": within,
            }
        )
    if not spans:
        raise CaptionDeliveryError("caption_segmentation_invalid", "no caption segments")
    base = {
        "schema_version": SEGMENTATION_SCHEMA_VERSION,
        "source_revision": source["revision"],
        "segmentation_revision": "",
        "chunker": copy.deepcopy(CHUNKER),
        "spans": spans,
    }
    revision_payload = dict(base)
    revision_payload.pop("segmentation_revision")
    base["segmentation_revision"] = contract_registry.canonical_hash(revision_payload)
    errors = contract_registry.validate_artifact("caption_segmentation", base)
    if errors:
        raise CaptionDeliveryError("caption_segmentation_invalid", "; ".join(errors))
    for item in source_records:
        material = {
            "source_revision": source["revision"],
            "segmentation_revision": base["segmentation_revision"],
            "span": item["span"],
            "within_span_ordinal": item["within_span_ordinal"],
        }
        item["caption_source_id"] = "caption-" + contract_registry.canonical_hash(material)[:16]
        item["corrected_source_sha256"] = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        item["source_revision"] = source["revision"]
        item["segmentation_revision"] = base["segmentation_revision"]
    return base, source_records


def _state_segments(state: dict[str, Any]) -> list[dict[str, int]]:
    raw = state.get("segments")
    if not isinstance(raw, list) or not raw:
        raise CaptionDeliveryError("caption_timeline_invalid", "editor segments are missing")
    output: list[dict[str, int]] = []
    offset = 0
    for item in raw:
        if not isinstance(item, dict):
            raise CaptionDeliveryError("caption_timeline_invalid", "editor segment must be object")
        start = _seconds_us(item.get("source_start"), "segment source_start")
        end = _seconds_us(item.get("source_end"), "segment source_end")
        if end <= start:
            raise CaptionDeliveryError("caption_timeline_invalid", "editor segment end must exceed start")
        output.append({"source_start_us": start, "source_end_us": end, "final_start_us": offset})
        offset += end - start
    return output


def _cut_map_hash(project_dir: Path, segments: list[dict[str, int]]) -> str:
    path = project_dir / "working/cut_map.json"
    if path.is_symlink():
        raise CaptionDeliveryError("caption_timeline_invalid", "cut map is a symlink")
    if path.is_file():
        return _file_sha256(path)
    return contract_registry.canonical_hash({"segments": segments})


def expected_instances(
    project_dir: Path,
    transcript: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    segmentation, sources = build_segmentation(project_dir, transcript)
    segments = _state_segments(state)
    source_list_hash = contract_registry.canonical_hash(
        [item["caption_source_id"] for item in sources]
    )
    cut_map_hash = _cut_map_hash(project_dir, segments)
    timeline_revision = contract_registry.canonical_hash(
        {
            "segments": segments,
            "caption_sources": [
                {
                    "caption_source_id": item["caption_source_id"],
                    "source_start_us": item["source_start_us"],
                    "source_end_us": item["source_end_us"],
                }
                for item in sources
            ],
        }
    )
    pending: list[dict[str, Any]] = []
    for timeline_index, segment in enumerate(segments):
        for source_index, source in enumerate(sources):
            hit_start = max(segment["source_start_us"], source["source_start_us"])
            hit_end = min(segment["source_end_us"], source["source_end_us"])
            if hit_end <= hit_start:
                continue
            final_start = segment["final_start_us"] + hit_start - segment["source_start_us"]
            final_end = segment["final_start_us"] + hit_end - segment["source_start_us"]
            pending.append(
                {
                    "timeline_index": timeline_index,
                    "source_index": source_index,
                    "caption_source_id": source["caption_source_id"],
                    "corrected_source": source["text"],
                    "corrected_source_sha256": source["corrected_source_sha256"],
                    "source_start_us": hit_start,
                    "source_end_us": hit_end,
                    "final_start_us": final_start,
                    "final_end_us": final_end,
                    "source_revision": source["source_revision"],
                    "segmentation_revision": source["segmentation_revision"],
                }
            )
    pending.sort(key=lambda item: (item["final_start_us"], item["timeline_index"], item["source_index"]))
    occurrence: dict[str, int] = {}
    for item in pending:
        source_id = item["caption_source_id"]
        ordinal = occurrence.get(source_id, 0)
        occurrence[source_id] = ordinal + 1
        item["caption_instance_id"] = "caption-instance-" + contract_registry.canonical_hash(
            {"caption_source_id": source_id, "occurrence_ordinal": ordinal}
        )[:16]
        item["occurrence_ordinal"] = ordinal
        item.pop("timeline_index")
        item.pop("source_index")
    return {
        "segmentation": segmentation,
        "sources": sources,
        "instances": pending,
        "source_list_hash": source_list_hash,
        "cut_map_sha256": cut_map_hash,
        "timeline_revision": timeline_revision,
    }


def _provider_config(manifest: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    subtitles = manifest.get("subtitles")
    if not isinstance(subtitles, dict):
        raise CaptionDeliveryError("translation_provider_unsupported", "subtitle config missing")
    translation = subtitles.get("translation")
    if translation is None:
        translation = {}
    if not isinstance(translation, dict):
        raise CaptionDeliveryError("translation_provider_unsupported", "translation config invalid")
    provider = str(translation.get("provider") or "ollama")
    semantic = subtitles.get("contextual_semantic_calibration")
    semantic = semantic if isinstance(semantic, dict) else {}
    model = str(translation.get("model") or semantic.get("model") or "qwen2.5:7b")
    if provider != "ollama" or not MODEL_RE.fullmatch(model):
        raise CaptionDeliveryError("translation_provider_unsupported", provider)
    raw_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    if "://" not in raw_host:
        raw_host = f"http://{raw_host}"
    parsed = urllib.parse.urlsplit(raw_host)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CaptionDeliveryError(
            "translation_provider_unsupported", "only loopback Ollama is permitted"
        )
    try:
        _port = parsed.port
    except ValueError as exc:
        raise CaptionDeliveryError("translation_provider_unsupported", "invalid Ollama port") from exc
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    config = {
        "provider": provider,
        "mode": "local_loopback",
        "model": model,
        "endpoint_origin": origin,
    }
    return provider, model, config


def provider_receipt(manifest: dict[str, Any]) -> dict[str, Any]:
    provider, model, config = _provider_config(manifest)
    return {
        "provider_id": provider,
        "mode": "local_loopback",
        "model": model,
        "config_sha256": contract_registry.canonical_hash(config),
        "consent_mode": "not_required_local",
        "consent_sha256": None,
    }


def _translation_prompt(instances: list[dict[str, Any]], target: str) -> str:
    request = [
        {
            "caption_instance_id": item["caption_instance_id"],
            "source": item["corrected_source"],
        }
        for item in instances
    ]
    return (
        f"Translate each caption to {target}. Return only JSON object {{\"items\":[...]}}. "
        "Keep the exact input order and caption_instance_id; one output per input, no extras. "
        "Each item needs translated_text. A deliberately unchanged brand, proper name, code, "
        "or number/unit also needs identity_preserved=true and identity_reason equal to "
        "brand, proper_name, code, or number_unit. Preserve all source Latin, number, and unit tokens.\n"
        + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_translations(
    instances: list[dict[str, Any]],
    response: dict[str, Any],
    glossary: list[str],
) -> list[dict[str, Any]]:
    raw = response.get("items")
    if set(response) != {"items"}:
        raise CaptionDeliveryError("translation_invalid", "provider response has unexpected fields")
    if not isinstance(raw, list) or len(raw) != len(instances):
        raise CaptionDeliveryError("translation_incomplete", "provider item count mismatch")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    glossary_tokens = {token.casefold() for term in glossary for token in ASCII_TOKEN_RE.findall(str(term))}
    for index, (expected, proposed) in enumerate(zip(instances, raw, strict=True)):
        if not isinstance(proposed, dict):
            raise CaptionDeliveryError("translation_invalid", f"item {index} is not an object")
        if not set(proposed).issubset(
            {"caption_instance_id", "translated_text", "identity_preserved", "identity_reason"}
        ):
            raise CaptionDeliveryError("translation_invalid", f"item {index} has unexpected fields")
        instance_id = str(proposed.get("caption_instance_id") or "")
        if instance_id != expected["caption_instance_id"]:
            raise CaptionDeliveryError("translation_order_mismatch", f"item {index}")
        if instance_id in seen:
            raise CaptionDeliveryError("translation_duplicate", instance_id)
        seen.add(instance_id)
        translated = str(proposed.get("translated_text") or "").strip()
        if not translated:
            raise CaptionDeliveryError("translation_incomplete", instance_id)
        if len(translated) > MAX_CAPTION_TEXT_CHARS:
            raise CaptionDeliveryError("translation_invalid", f"{instance_id} text is too long")
        source = expected["corrected_source"].strip()
        identity = proposed.get("identity_preserved") is True
        reason = proposed.get("identity_reason")
        if identity:
            if reason not in IDENTITY_REASONS:
                raise CaptionDeliveryError("translation_identity_invalid", instance_id)
        elif reason is not None:
            raise CaptionDeliveryError("translation_identity_invalid", instance_id)
        source_normalised = TRANSLATION_NORMALISE_RE.sub("", source).casefold()
        translated_normalised = TRANSLATION_NORMALISE_RE.sub("", translated).casefold()
        if (
            CHINESE_RE.search(source)
            and translated_normalised == source_normalised
            and not identity
        ):
            raise CaptionDeliveryError("translation_unchanged", instance_id)
        required = {token.casefold() for token in ASCII_TOKEN_RE.findall(source)} | glossary_tokens.intersection(
            token.casefold() for token in ASCII_TOKEN_RE.findall(source)
        )
        delivered = {token.casefold() for token in ASCII_TOKEN_RE.findall(translated)}
        missing = sorted(required - delivered)
        if missing:
            raise CaptionDeliveryError("translation_token_missing", f"{instance_id}: {', '.join(missing)}")
        output.append(
            {
                "translated_text": translated,
                "translation_status": "identity_preserved" if identity else "translated",
                "identity_preserved": identity,
                "identity_reason": reason if identity else None,
            }
        )
    return output


def _caption_overlays(state: dict[str, Any]) -> list[dict[str, Any]]:
    overlays = state.get("overlays")
    if not isinstance(overlays, list):
        return []
    return [
        item
        for item in overlays
        if isinstance(item, dict)
        and item.get("type") == "caption"
        and item.get("visible", True)
        and item.get("source") == "working/transcript_words.json"
    ]


def _bind_sources_to_state(state: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    overlays = _caption_overlays(updated)
    if len(overlays) != len(sources):
        raise CaptionDeliveryError("caption_binding_missing", "caption overlay count mismatch")
    for index, (overlay, source) in enumerate(zip(overlays, sources, strict=True)):
        try:
            start_us = _seconds_us(overlay.get("start"), "overlay start")
            end_us = _seconds_us(overlay.get("end"), "overlay end")
        except CaptionDeliveryError as exc:
            raise CaptionDeliveryError("caption_binding_missing", str(exc)) from exc
        if (
            str(overlay.get("text") or "").strip() != source["text"]
            or start_us != source["source_start_us"]
            or end_us != source["source_end_us"]
        ):
            raise CaptionDeliveryError("caption_binding_missing", f"overlay {index} source mismatch")
        existing = overlay.get("caption_source_id")
        if existing not in {None, "", source["caption_source_id"]}:
            raise CaptionDeliveryError("caption_binding_missing", f"overlay {index} has stale source id")
        overlay["caption_source_id"] = source["caption_source_id"]
    return updated


def _translate_and_adopt(
    trust: _ProjectTrust,
    *,
    manifest: dict[str, Any],
    bound_state: dict[str, Any],
    expected: dict[str, Any],
    target: str,
    required: bool,
    timeout: int,
    model: str,
    receipt: dict[str, Any],
    model_call: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    try:
        response = model_call(
            _translation_prompt(expected["instances"], target),
            "caption_translation",
            model=model,
            timeout=timeout,
        )
    except CaptionDeliveryError:
        raise
    except Exception as exc:
        raise CaptionDeliveryError("translation_provider_failed", str(exc)[:500]) from exc
    # The blocking provider wait is the point where another process can swap
    # project paths.  Reject before validating or adopting any returned data.
    _verify_project_trust(trust)
    if not isinstance(response, dict):
        raise CaptionDeliveryError("translation_invalid", "provider response must be an object")
    subtitles = manifest.get("subtitles") if isinstance(manifest.get("subtitles"), dict) else {}
    glossary = subtitles.get("glossary") if isinstance(subtitles.get("glossary"), list) else []
    translations = _validate_translations(expected["instances"], response, glossary)
    items: list[dict[str, Any]] = []
    for instance, translated in zip(expected["instances"], translations, strict=True):
        item = dict(instance)
        item.update(translated)
        item["source_list_hash"] = expected["source_list_hash"]
        item["cut_map_sha256"] = expected["cut_map_sha256"]
        item["timeline_revision"] = expected["timeline_revision"]
        item["target_language"] = target
        item["provider_receipt"] = receipt
        items.append(item)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": expected["segmentation"]["source_revision"],
        "segmentation_revision": expected["segmentation"]["segmentation_revision"],
        "source_list_hash": expected["source_list_hash"],
        "cut_map_sha256": expected["cut_map_sha256"],
        "timeline_revision": expected["timeline_revision"],
        "target_language": target,
        "required": required,
        "provider_receipt": receipt,
        "items": items,
    }
    errors = contract_registry.validate_artifact("caption_delivery", artifact)
    if errors:
        raise CaptionDeliveryError("caption_contract_invalid", "; ".join(errors))

    artifact_sha256 = hashlib.sha256(canonical_bytes(artifact)).hexdigest()
    translated_by_source: dict[str, str] = {}
    for item in items:
        translated_by_source.setdefault(item["caption_source_id"], item["translated_text"])
    for overlay in _caption_overlays(bound_state):
        source_id = str(overlay.get("caption_source_id") or "")
        overlay["translation"] = translated_by_source[source_id]
        overlay["caption_delivery_artifact_sha256"] = artifact_sha256
    bound_state["caption_delivery"] = {
        "schema_version": 2,
        "artifact": CAPTION_REL.as_posix(),
        "artifact_sha256": artifact_sha256,
        "timeline_revision": expected["timeline_revision"],
    }
    try:
        from editor_server import editor_state_revision

        bound_state["revision"] = editor_state_revision(bound_state)
    except ImportError:
        pass
    translation_config = subtitles.setdefault("translation", {})
    translation_config.update(
        {
            "required": required,
            "target_language": target,
            "provider": "ollama",
            "model": model,
            "artifact": CAPTION_REL.as_posix(),
            "provider_receipt": receipt,
        }
    )
    approvals = manifest.setdefault("approvals", {})
    for gate in ("timeline", "final"):
        approvals[gate] = {
            "approved": False,
            "confirmed_by": None,
            "at": None,
            "note": "Invalidated because caption delivery changed",
        }

    # Every replace is relative to a directory descriptor opened before the
    # provider wait.  A path swap therefore cannot redirect writes outside;
    # the identity check before each replace turns a swap into a hard failure.
    _atomic_write_at(
        trust,
        working=True,
        name=SEGMENTATION_REL.name,
        payload=expected["segmentation"],
    )
    _atomic_write_at(trust, working=True, name=CAPTION_REL.name, payload=artifact)
    _atomic_write_at(
        trust,
        working=True,
        name="editor_state.json",
        payload=bound_state,
    )
    _atomic_write_at(trust, working=False, name="project.json", payload=manifest)
    return artifact


def create_delivery(
    project_dir: Path,
    target: str,
    *,
    required: bool,
    timeout: int = 300,
    model_call: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate and atomically adopt caption v2 after every check passes."""
    if type(required) is not bool:
        raise CaptionDeliveryError("caption_contract_invalid", "required must be boolean")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise CaptionDeliveryError("translation_provider_unsupported", "timeout must be 1..3600")
    if not TARGET_RE.fullmatch(target):
        raise CaptionDeliveryError("translation_target_invalid", target)
    root = project_dir.resolve()
    _trusted_working_root(root)
    manifest = _load_json(_owned_artifact(root, Path("project.json")))
    transcript = _load_json(_owned_artifact(root, Path("working/transcript_words.json")))
    if not isinstance(manifest, dict) or not isinstance(transcript, dict):
        raise CaptionDeliveryError("caption_artifact_invalid", "project transcript missing")
    _provider, model, _config = _provider_config(manifest)  # consent/provider preflight before mutation
    receipt = provider_receipt(manifest)
    state = _load_json(_owned_artifact(root, Path("working/editor_state.json")))
    if not isinstance(state, dict):
        raise CaptionDeliveryError("caption_timeline_invalid", "editor state missing")
    expected = expected_instances(root, transcript, state)
    bound_state = _bind_sources_to_state(state, expected["sources"])
    if model_call is None:
        from contextual_semantic_calibration import ollama_json_model_call

        model_call = ollama_json_model_call
    with _project_trust(root) as trust:
        return _translate_and_adopt(
            trust,
            manifest=manifest,
            bound_state=bound_state,
            expected=expected,
            target=target,
            required=required,
            timeout=timeout,
            model=model,
            receipt=receipt,
            model_call=model_call,
        )


def caption_v2_required(manifest: dict[str, Any]) -> bool:
    subtitles = manifest.get("subtitles")
    translation = subtitles.get("translation") if isinstance(subtitles, dict) else None
    return bool(isinstance(translation, dict) and translation.get("required") is True)


def validate_for_render(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Reload and bind v2 before direct staging. Legacy/non-required is unchanged."""
    if not caption_v2_required(manifest):
        return None, state
    root = project_dir.resolve()
    path = root / CAPTION_REL
    if path.is_symlink() or not path.is_file():
        raise CaptionDeliveryError("caption_binding_missing", "caption v2 artifact missing")
    artifact, artifact_bytes = _load_owned_json_bytes(root, CAPTION_REL)
    live_artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    state_binding = state.get("caption_delivery")
    adopted_sha256 = (
        str(state_binding.get("artifact_sha256") or "")
        if isinstance(state_binding, dict)
        else ""
    )
    if not SHA_RE.fullmatch(adopted_sha256) or live_artifact_sha256 != adopted_sha256:
        raise CaptionDeliveryError(
            "caption_binding_missing", "live caption artifact hash differs from adopted state"
        )
    for overlay in _caption_overlays(state):
        if overlay.get("caption_delivery_artifact_sha256") != adopted_sha256:
            raise CaptionDeliveryError(
                "caption_binding_missing", "caption overlay artifact hash is stale"
            )
    errors = contract_registry.validate_artifact("caption_delivery", artifact)
    if errors:
        raise CaptionDeliveryError("caption_binding_missing", "; ".join(errors))
    subtitles = manifest.get("subtitles") if isinstance(manifest.get("subtitles"), dict) else {}
    translation = subtitles.get("translation") if isinstance(subtitles.get("translation"), dict) else {}
    if artifact.get("required") is not True:
        raise CaptionDeliveryError("caption_binding_missing", "required artifact is not marked required")
    if artifact.get("target_language") != translation.get("target_language"):
        raise CaptionDeliveryError("caption_binding_missing", "target language is stale")
    provider = str(translation.get("provider") or "")
    model = str(translation.get("model") or "")
    stored_receipt = translation.get("provider_receipt")
    if provider != "ollama" or model != artifact.get("provider_receipt", {}).get("model"):
        raise CaptionDeliveryError("translation_provider_unsupported", provider)
    current_receipt = provider_receipt(manifest)
    if (
        artifact.get("provider_receipt") != current_receipt
        or stored_receipt != current_receipt
    ):
        raise CaptionDeliveryError("caption_binding_missing", "provider receipt is stale")
    transcript = _load_json(_owned_artifact(root, Path("working/transcript_words.json")))
    expected = expected_instances(root, transcript, state)
    segmentation = _load_json(_owned_artifact(root, SEGMENTATION_REL))
    segmentation_errors = contract_registry.validate_artifact(
        "caption_segmentation", segmentation
    )
    if segmentation_errors or segmentation != expected["segmentation"]:
        detail = "; ".join(segmentation_errors) if segmentation_errors else "payload mismatch"
        raise CaptionDeliveryError("caption_binding_missing", f"segmentation artifact: {detail}")
    bound_state = _bind_sources_to_state(state, expected["sources"])
    root_fields = {
        "source_revision": expected["segmentation"]["source_revision"],
        "segmentation_revision": expected["segmentation"]["segmentation_revision"],
        "source_list_hash": expected["source_list_hash"],
        "cut_map_sha256": expected["cut_map_sha256"],
        "timeline_revision": expected["timeline_revision"],
    }
    for key, value in root_fields.items():
        if artifact.get(key) != value:
            raise CaptionDeliveryError("caption_binding_missing", f"stale {key}")
    actual_items = artifact.get("items")
    if not isinstance(actual_items, list) or len(actual_items) != len(expected["instances"]):
        raise CaptionDeliveryError("caption_binding_missing", "caption instance count mismatch")
    for index, (actual, wanted) in enumerate(zip(actual_items, expected["instances"], strict=True)):
        if not isinstance(actual, dict):
            raise CaptionDeliveryError("caption_binding_missing", f"item {index} invalid")
        for key in (
            "caption_source_id",
            "caption_instance_id",
            "occurrence_ordinal",
            "corrected_source",
            "corrected_source_sha256",
            "source_start_us",
            "source_end_us",
            "final_start_us",
            "final_end_us",
            "source_revision",
            "segmentation_revision",
        ):
            if actual.get(key) != wanted.get(key):
                raise CaptionDeliveryError("caption_binding_missing", f"item {index} {key} mismatch")
        for key, value in root_fields.items():
            item_key = "source_list_hash" if key == "source_list_hash" else key
            if actual.get(item_key) != value:
                raise CaptionDeliveryError("caption_binding_missing", f"item {index} stale {item_key}")
    bound_state["_caption_delivery_v2"] = {
        "artifact_sha256": live_artifact_sha256,
        "items": copy.deepcopy(actual_items),
    }
    return artifact, bound_state


def render_item_map(state: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    binding = state.get("_caption_delivery_v2")
    items = binding.get("items") if isinstance(binding, dict) else None
    if not isinstance(items, list):
        return {}
    return {
        (
            str(item.get("caption_source_id") or ""),
            int(item.get("final_start_us")),
            int(item.get("final_end_us")),
        ): item
        for item in items
        if isinstance(item, dict)
    }

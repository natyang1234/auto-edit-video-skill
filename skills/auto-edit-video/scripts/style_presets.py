#!/usr/bin/env python3
"""Strict cross-project style preset store for Auto Edit Studio."""
from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fx_overlay
import fx_preview_contract


SCHEMA_VERSION = 1
MAX_PRESETS = 256
MAX_STORE_BYTES = 1024 * 1024
PRESET_ID_RE = re.compile(r"preset_[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")
CAPTION_STYLE_BOUNDS = {
    "font_size": (8.0, 500.0),
    "stroke_width": (0.0, 50.0),
    "x": (-100.0, 200.0),
    "y": (-100.0, 200.0),
    "max_width": (1.0, 200.0),
}
CAPTION_COLOR_KEYS = {"color", "emphasis_color", "stroke_color"}
CAPTION_ANIMATIONS = {"none", "fade", "pop", "slide-up", "slide-in", "pan"}
CAPTION_STYLE_KEYS = set(CAPTION_STYLE_BOUNDS) | CAPTION_COLOR_KEYS | {"animation"}
FX_KEYS = {"code", "values", "placement", "scheme", "font_ids", "opacity", "text_fit"}


class StylePresetError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


UNSAFE_PATH_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EISDIR})


def _classified_store_os_error(
    exc: OSError,
    *,
    unsafe_message: str,
    writing: bool,
) -> StylePresetError:
    """Keep no-follow/type failures distinct from operational store I/O."""
    if exc.errno in UNSAFE_PATH_ERRNOS:
        return StylePresetError(409, "store_unsafe", unsafe_message)
    if writing:
        return StylePresetError(
            503,
            "store_io_error",
            "預設寫入結果未能完整確認；請重新載入清單確認後再試，並檢查磁碟空間與權限",
        )
    return StylePresetError(
        503,
        "store_io_error",
        "預設資料暫時無法讀取；原檔未變更，請檢查磁碟或權限後重試",
    )


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise StylePresetError(422, "preset_invalid", "預設內容不是有效 JSON") from exc


def revision_for_document(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "presets": []}


def normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        raise StylePresetError(422, "name_invalid", "預設名稱必須是文字")
    name = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(name) <= 80 or any(unicodedata.category(ch) == "Cc" for ch in name):
        raise StylePresetError(422, "name_invalid", "預設名稱必須是 1 到 80 個字")
    return name


def _finite_number(value: Any, minimum: float, maximum: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StylePresetError(422, "settings_invalid", f"{field} 必須是有限數字")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise StylePresetError(422, "settings_invalid", f"{field} 必須是有限數字") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise StylePresetError(422, "settings_invalid", f"{field} 超出允許範圍")
    return 0.0 if number == 0.0 else number


def normalize_font_descriptor(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"sha256", "family", "style", "weight"}:
        raise StylePresetError(422, "font_invalid", "字型描述格式無效")
    sha256 = value.get("sha256")
    family = value.get("family")
    style = value.get("style")
    weight = value.get("weight")
    if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
        raise StylePresetError(422, "font_invalid", "字型雜湊無效")
    if not isinstance(family, str) or not 1 <= len(family) <= 200:
        raise StylePresetError(422, "font_invalid", "字型 family 無效")
    if not isinstance(style, str) or len(style) > 200:
        raise StylePresetError(422, "font_invalid", "字型 style 無效")
    if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 1000:
        raise StylePresetError(422, "font_invalid", "字型 weight 無效")
    return {
        "sha256": sha256,
        "family": unicodedata.normalize("NFC", family),
        "style": unicodedata.normalize("NFC", style),
        "weight": weight,
    }


def normalize_caption_settings(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or "style" not in value
        or set(value) - {"style", "font"}
    ):
        raise StylePresetError(422, "settings_invalid", "字幕預設欄位不符合規格")
    raw_style = value.get("style")
    if not isinstance(raw_style, dict) or not raw_style or set(raw_style) - CAPTION_STYLE_KEYS:
        raise StylePresetError(422, "settings_invalid", "字幕樣式欄位不符合規格")
    style: dict[str, Any] = {}
    for key, item in raw_style.items():
        if key in CAPTION_STYLE_BOUNDS:
            style[key] = _finite_number(item, *CAPTION_STYLE_BOUNDS[key], key)
        elif key in CAPTION_COLOR_KEYS:
            if not isinstance(item, str) or COLOR_RE.fullmatch(item) is None:
                raise StylePresetError(422, "settings_invalid", f"{key} 必須是 #RRGGBB")
            style[key] = item.upper()
        elif key == "animation":
            if not isinstance(item, str) or item not in CAPTION_ANIMATIONS:
                raise StylePresetError(422, "settings_invalid", "字幕動畫不受支援")
            style[key] = item
    return {"style": style, "font": normalize_font_descriptor(value.get("font"))}


def _normalize_fx_settings_static(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FX_KEYS:
        raise StylePresetError(422, "settings_invalid", "FX 預設欄位不符合規格")
    code = value.get("code")
    if not isinstance(code, str) or re.fullmatch(r"[A-Z][0-9]{1,3}", code) is None:
        raise StylePresetError(422, "settings_invalid", "FX code 無效")
    values = value.get("values")
    if not isinstance(values, dict) or len(values) > 32:
        raise StylePresetError(422, "settings_invalid", "FX 文字參數格式無效")
    normalized_values: dict[str, str | int | float] = {}
    total = 0
    for key, item in values.items():
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 80
            or any(unicodedata.category(ch) == "Cc" for ch in key)
        ):
            raise StylePresetError(422, "settings_invalid", "FX 文字參數欄位無效")
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise StylePresetError(422, "settings_invalid", "FX 文字參數格式無效")
        if isinstance(item, float) and not math.isfinite(item):
            raise StylePresetError(422, "settings_invalid", "FX 文字參數必須是有限數字")
        if len(str(item)) > 500:
            raise StylePresetError(422, "settings_invalid", "FX 文字參數過長")
        total += len(str(item))
        normalized_values[key] = item
    if total > 8192:
        raise StylePresetError(422, "settings_invalid", "FX 文字參數總長度過長")
    placement = value.get("placement")
    if not isinstance(placement, str) or placement not in {"", "top", "bottom", "left", "right"}:
        raise StylePresetError(422, "settings_invalid", "FX placement 無效")
    scheme = value.get("scheme")
    if (
        not isinstance(scheme, str)
        or len(scheme) > 80
        or any(unicodedata.category(ch) == "Cc" for ch in scheme)
    ):
        raise StylePresetError(422, "settings_invalid", "FX scheme 無效")
    font_ids = value.get("font_ids")
    if not isinstance(font_ids, dict) or len(font_ids) > len(fx_overlay.FONT_ROLES):
        raise StylePresetError(422, "settings_invalid", "FX font_ids 格式無效")
    normalized_fonts: dict[str, str] = {}
    for role, font_id in font_ids.items():
        if role not in fx_overlay.FONT_ROLES:
            raise StylePresetError(422, "settings_invalid", "FX font role 無效")
        if (
            not isinstance(font_id, str)
            or len(font_id) > 120
            or any(unicodedata.category(ch) == "Cc" for ch in font_id)
        ):
            raise StylePresetError(422, "settings_invalid", "FX font id 無效")
        normalized_fonts[role] = font_id
    try:
        opacity = fx_preview_contract.normalize_fx_opacity(value.get("opacity"))
    except fx_preview_contract.FxPreviewContractError as exc:
        raise StylePresetError(422, exc.code, exc.message) from exc
    if not isinstance(value.get("text_fit"), bool):
        raise StylePresetError(422, "settings_invalid", "FX text_fit 必須是布林值")
    return {
        "code": code,
        "values": dict(sorted(normalized_values.items())),
        "placement": placement,
        "scheme": scheme,
        "font_ids": dict(sorted(normalized_fonts.items())),
        "opacity": opacity,
        "text_fit": value["text_fit"],
    }


def normalize_fx_settings(
    value: Any, *, catalog: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate an FX preset write against current runtime capabilities."""
    normalized = _normalize_fx_settings_static(value)
    try:
        current_catalog = catalog if catalog is not None else fx_overlay.catalog(refresh=True)
    except Exception as exc:
        raise StylePresetError(422, "catalog_changed", "目前無法載入 FX 目錄") from exc
    if (
        not isinstance(current_catalog, dict)
        or not isinstance(current_catalog.get("effects"), list)
        or not isinstance(current_catalog.get("schemes"), dict)
    ):
        raise StylePresetError(422, "catalog_changed", "FX 目錄格式目前無效")
    effect = next(
        (
            item for item in current_catalog.get("effects", [])
            if isinstance(item, dict) and item.get("code") == normalized["code"]
        ),
        None,
    )
    if effect is None or effect.get("available") is False:
        raise StylePresetError(422, "effect_unavailable", "FX 效果目前不可用")
    allowed = {
        str(item.get("id")) for item in effect.get("vars", [])
        if isinstance(item, dict) and item.get("kind") == "text"
    }
    if set(normalized["values"]) - allowed:
        raise StylePresetError(422, "catalog_changed", "FX 文字欄位已不受目前版本支援")
    has_placement = any(
        isinstance(item, dict) and item.get("kind") == "placement"
        for item in effect.get("vars", [])
    )
    if normalized["placement"] and not has_placement:
        raise StylePresetError(422, "catalog_changed", "FX placement 已不受目前版本支援")
    if normalized["scheme"] and normalized["scheme"] not in current_catalog.get("schemes", {}):
        raise StylePresetError(422, "catalog_changed", "FX 配色已不受目前版本支援")
    if fx_overlay.validate_font_ids(normalized["font_ids"]):
        raise StylePresetError(422, "font_unavailable", "FX 全域字型目前不可用")
    return normalized


def normalize_settings(kind: Any, value: Any) -> dict[str, Any]:
    if kind == "caption":
        return normalize_caption_settings(value)
    if kind == "fx":
        return normalize_fx_settings(value)
    raise StylePresetError(422, "kind_invalid", "預設 kind 必須是 caption 或 fx")


def _normalize_record(value: Any) -> dict[str, Any]:
    required = {"id", "name", "kind", "settings", "created_at", "updated_at"}
    if not isinstance(value, dict) or set(value) != required:
        raise StylePresetError(409, "store_corrupt", "預設資料欄位毀損")
    preset_id = value.get("id")
    if not isinstance(preset_id, str) or PRESET_ID_RE.fullmatch(preset_id) is None:
        raise StylePresetError(409, "store_corrupt", "預設識別碼毀損")
    name = normalize_name(value.get("name"))
    kind = value.get("kind")
    if kind == "caption":
        settings = normalize_caption_settings(value.get("settings"))
    elif kind == "fx":
        settings = _normalize_fx_settings_static(value.get("settings"))
    else:
        raise StylePresetError(422, "kind_invalid", "預設 kind 必須是 caption 或 fx")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    for timestamp in (created_at, updated_at):
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise StylePresetError(409, "store_corrupt", "預設時間欄位毀損")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StylePresetError(409, "store_corrupt", "預設時間欄位毀損") from exc
    return {
        "id": preset_id,
        "name": name,
        "kind": kind,
        "settings": settings,
        "created_at": created_at,
        "updated_at": updated_at,
    }


class StylePresetStore:
    def __init__(self, directory: Path | None = None):
        configured = os.environ.get("AUTO_EDIT_PRESETS_DIR") if directory is None else None
        chosen = (
            Path(configured).expanduser()
            if configured
            else Path(directory) if directory is not None
            else Path.home() / "AutoEditStudio" / "presets"
        )
        # Keep the configured lexical path: resolve() would follow a symlink
        # before the store has had a chance to reject it.
        self.directory = Path(os.path.abspath(os.fspath(chosen.expanduser())))
        self.path = self.directory / "presets.json"
        self.lock_path = self.directory / ".presets.lock"

    def _open_directory(self, *, create: bool) -> int | None:
        """Open every path component without following symlinks and pin the final fd."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.sep, flags)
        except OSError as exc:
            raise _classified_store_os_error(
                exc,
                unsafe_message="預設資料夾含不安全的路徑元件",
                writing=create,
            ) from exc
        try:
            for component in self.directory.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        os.close(descriptor)
                        descriptor = -1
                        return None
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        # Another valid creator may have won the mkdir race;
                        # the no-follow open below still revalidates its type.
                        pass
                    except OSError as exc:
                        raise _classified_store_os_error(
                            exc,
                            unsafe_message="無法安全建立預設資料夾",
                            writing=True,
                        ) from exc
                    try:
                        child = os.open(component, flags, dir_fd=descriptor)
                    except OSError as exc:
                        raise _classified_store_os_error(
                            exc,
                            unsafe_message="無法安全建立預設資料夾",
                            writing=True,
                        ) from exc
                except OSError as exc:
                    raise _classified_store_os_error(
                        exc,
                        unsafe_message="預設資料夾含不安全的路徑元件",
                        writing=create,
                    ) from exc
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    def _load_unlocked(self, directory_fd: int | None = None) -> dict[str, Any]:
        owned_fd = directory_fd is None
        if owned_fd:
            directory_fd = self._open_directory(create=False)
        if directory_fd is None:
            return empty_document()
        try:
            descriptor = os.open(
                "presets.json",
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if owned_fd:
                os.close(directory_fd)
            return empty_document()
        except OSError as exc:
            if owned_fd:
                os.close(directory_fd)
            raise _classified_store_os_error(
                exc,
                unsafe_message="無法安全讀取預設資料",
                writing=False,
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORE_BYTES:
                raise StylePresetError(409, "store_corrupt", "預設資料格式或大小無效")
            raw = b""
            while len(raw) <= MAX_STORE_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_STORE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > MAX_STORE_BYTES:
                raise StylePresetError(409, "store_corrupt", "預設資料過大")
        finally:
            os.close(descriptor)
            if owned_fd:
                os.close(directory_fd)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StylePresetError(409, "store_corrupt", "預設資料已毀損，原檔未變更") from exc
        if not isinstance(document, dict) or set(document) != {"schema_version", "presets"}:
            raise StylePresetError(409, "store_corrupt", "預設資料 schema 無效")
        if document.get("schema_version") != SCHEMA_VERSION or not isinstance(document.get("presets"), list):
            raise StylePresetError(409, "store_corrupt", "預設資料 schema 無效")
        if len(document["presets"]) > MAX_PRESETS:
            raise StylePresetError(409, "store_corrupt", "預設資料筆數超限")
        try:
            presets = [_normalize_record(item) for item in document["presets"]]
        except StylePresetError as exc:
            raise StylePresetError(409, "store_corrupt", "預設資料已毀損，原檔未變更") from exc
        if len({item["id"] for item in presets}) != len(presets):
            raise StylePresetError(409, "store_corrupt", "預設識別碼重複")
        if len({item["name"].casefold() for item in presets}) != len(presets):
            raise StylePresetError(409, "store_corrupt", "預設名稱重複")
        return {"schema_version": SCHEMA_VERSION, "presets": presets}

    def collection(self) -> dict[str, Any]:
        try:
            document = self._load_unlocked()
        except OSError as exc:
            raise StylePresetError(
                503,
                "store_io_error",
                "預設資料暫時無法讀取；原檔未變更，請檢查磁碟或權限後重試",
            ) from exc
        return self._public_collection(document)

    @staticmethod
    def _availability(
        record: dict[str, Any],
        catalog: dict[str, Any] | None,
        fonts: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if record["kind"] == "caption":
            return {"status": "available", "reason": None, "message": None}
        settings = record["settings"]
        if catalog is None:
            return {
                "status": "unavailable",
                "reason": "catalog_changed",
                "message": "目前無法載入 FX 目錄，預設仍可刪除或稍後重試",
            }
        effect = next(
            (
                item for item in catalog.get("effects", [])
                if isinstance(item, dict) and item.get("code") == settings["code"]
            ),
            None,
        )
        if effect is None or effect.get("available") is False:
            return {
                "status": "unavailable",
                "reason": "effect_unavailable",
                "message": f"FX {settings['code']} 在目前電腦不可用",
            }
        allowed = {
            str(item.get("id")) for item in effect.get("vars", [])
            if isinstance(item, dict) and item.get("kind") == "text"
        }
        has_placement = any(
            isinstance(item, dict) and item.get("kind") == "placement"
            for item in effect.get("vars", [])
        )
        if (
            set(settings["values"]) - allowed
            or (settings["placement"] and not has_placement)
            or (settings["scheme"] and settings["scheme"] not in catalog.get("schemes", {}))
        ):
            return {
                "status": "unavailable",
                "reason": "catalog_changed",
                "message": "FX 預設欄位已不受目前目錄版本支援",
            }
        by_id = fonts.get("by_id", {}) if isinstance(fonts, dict) else {}
        if any(
            font_id
            and (
                not isinstance(by_id.get(font_id), dict)
                or by_id[font_id].get("verified") is not True
            )
            for font_id in settings["font_ids"].values()
        ):
            return {
                "status": "unavailable",
                "reason": "font_unavailable",
                "message": "FX 預設使用的全域字型在目前電腦缺少或驗證失敗",
            }
        return {"status": "available", "reason": None, "message": None}

    def _public_collection(self, document: dict[str, Any]) -> dict[str, Any]:
        catalog: dict[str, Any] | None = None
        if any(record["kind"] == "fx" for record in document["presets"]):
            try:
                catalog = fx_overlay.catalog(refresh=True)
            except Exception:
                catalog = None
            if (
                not isinstance(catalog, dict)
                or not isinstance(catalog.get("effects"), list)
                or not isinstance(catalog.get("schemes"), dict)
                or not isinstance(catalog.get("fonts"), list)
            ):
                catalog = None
        fonts = (
            {
                "by_id": {
                    item["id"]: item
                    for item in catalog.get("fonts", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            }
            if isinstance(catalog, dict)
            else None
        )
        presets = copy.deepcopy(document["presets"])
        for record in presets:
            record["availability"] = self._availability(record, catalog, fonts)
        return {"revision": revision_for_document(document), "presets": presets}

    @contextmanager
    def locked_collection(self):
        """Hold the cross-process reader lock across a revision-bound apply."""
        directory_fd = self._open_directory(create=False)
        if directory_fd is None:
            yield {"revision": revision_for_document(empty_document()), "presets": []}
            return
        lock_fd = -1
        last_error: OSError | None = None
        for _attempt in range(3):
            try:
                lock_fd = os.open(
                    ".presets.lock",
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileNotFoundError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
                break
        if lock_fd < 0:
            os.close(directory_fd)
            assert last_error is not None
            raise _classified_store_os_error(
                last_error,
                unsafe_message="無法安全讀取預設鎖",
                writing=False,
            ) from last_error
        try:
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise StylePresetError(409, "store_unsafe", "預設鎖檔不安全")
                fcntl.flock(lock_fd, fcntl.LOCK_SH)
                document = self._load_unlocked(directory_fd)
                collection = self._public_collection(document)
            except OSError as exc:
                raise StylePresetError(
                    503,
                    "store_io_error",
                    "預設資料暫時無法讀取；原檔未變更，請檢查磁碟或權限後重試",
                ) from exc
            yield collection
        finally:
            os.close(lock_fd)
            os.close(directory_fd)

    def _write_unlocked(self, document: dict[str, Any], directory_fd: int) -> None:
        raw = _canonical_bytes(document) + b"\n"
        if len(raw) > MAX_STORE_BYTES:
            raise StylePresetError(422, "store_too_large", "預設資料超過大小上限")
        try:
            target_info = os.stat("presets.json", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_info = None
        except OSError as exc:
            raise _classified_store_os_error(
                exc,
                unsafe_message="預設資料檔不安全",
                writing=True,
            ) from exc
        if target_info is not None and not stat.S_ISREG(target_info.st_mode):
            raise StylePresetError(409, "store_unsafe", "預設資料檔不安全")
        temporary = f".presets-{os.getpid()}-{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise _classified_store_os_error(
                    exc,
                    unsafe_message="預設暫存檔不安全",
                    writing=True,
                ) from exc
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary,
                "presets.json",
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _mutate(self, expected_revision: Any, operation) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or SHA256_RE.fullmatch(expected_revision) is None:
            raise StylePresetError(409, "library_revision_required", "必須提供目前的預設版本")
        try:
            return self._mutate_io(expected_revision, operation)
        except OSError as exc:
            raise StylePresetError(
                503,
                "store_io_error",
                "預設寫入結果未能完整確認；請重新載入清單確認後再試，並檢查磁碟空間與權限",
            ) from exc

    def _mutate_io(self, expected_revision: str, operation) -> dict[str, Any]:
        directory_fd = self._open_directory(create=True)
        assert directory_fd is not None
        lock_fd = -1
        last_error: OSError | None = None
        # Darwin may transiently return ENOENT when two O_CREAT calls race on
        # the same new dir_fd-relative name. Repeating against the same pinned
        # directory fd cannot redirect the operation or follow a symlink.
        for _attempt in range(3):
            try:
                lock_fd = os.open(
                    ".presets.lock",
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileNotFoundError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
                break
        if lock_fd < 0:
            os.close(directory_fd)
            assert last_error is not None
            raise _classified_store_os_error(
                last_error,
                unsafe_message="無法鎖定預設資料",
                writing=True,
            ) from last_error
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise StylePresetError(409, "store_unsafe", "預設鎖檔不安全")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            document = self._load_unlocked(directory_fd)
            if revision_for_document(document) != expected_revision:
                raise StylePresetError(409, "library_revision_conflict", "預設清單已變更，請重新載入")
            operation(document)
            self._write_unlocked(document, directory_fd)
            return self._public_collection(document)
        finally:
            os.close(lock_fd)
            os.close(directory_fd)

    @staticmethod
    def _reject_duplicate_name(document: dict[str, Any], name: str, exclude_id: str | None = None) -> None:
        folded = name.casefold()
        if any(item["id"] != exclude_id and item["name"].casefold() == folded for item in document["presets"]):
            raise StylePresetError(409, "name_conflict", "已有同名預設")

    def create(self, expected_revision: Any, name: Any, kind: Any, settings: Any) -> dict[str, Any]:
        normalized_name = normalize_name(name)
        normalized_settings = normalize_settings(kind, settings)
        created: dict[str, Any] = {}

        def operation(document: dict[str, Any]) -> None:
            if len(document["presets"]) >= MAX_PRESETS:
                raise StylePresetError(422, "preset_limit", "預設最多 256 筆")
            self._reject_duplicate_name(document, normalized_name)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            record = {
                "id": f"preset_{secrets.token_hex(16)}",
                "name": normalized_name,
                "kind": kind,
                "settings": normalized_settings,
                "created_at": now,
                "updated_at": now,
            }
            document["presets"].append(record)
            created.update(copy.deepcopy(record))

        result = self._mutate(expected_revision, operation)
        result["preset"] = next(
            item for item in result["presets"] if item["id"] == created["id"]
        )
        return result

    def update(self, preset_id: str, expected_revision: Any, name: Any, kind: Any, settings: Any) -> dict[str, Any]:
        if PRESET_ID_RE.fullmatch(str(preset_id)) is None:
            raise StylePresetError(404, "preset_not_found", "找不到預設")
        normalized_name = normalize_name(name)
        normalized_settings = normalize_settings(kind, settings)
        updated: dict[str, Any] = {}

        def operation(document: dict[str, Any]) -> None:
            matches = [item for item in document["presets"] if item["id"] == preset_id]
            if len(matches) != 1:
                raise StylePresetError(404, "preset_not_found", "找不到預設")
            record = matches[0]
            if record["kind"] != kind:
                raise StylePresetError(422, "kind_immutable", "不能變更預設種類")
            self._reject_duplicate_name(document, normalized_name, preset_id)
            record["name"] = normalized_name
            record["settings"] = normalized_settings
            record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            updated.update(copy.deepcopy(record))

        result = self._mutate(expected_revision, operation)
        result["preset"] = next(
            item for item in result["presets"] if item["id"] == updated["id"]
        )
        return result

    def delete(self, preset_id: str, expected_revision: Any) -> dict[str, Any]:
        if PRESET_ID_RE.fullmatch(str(preset_id)) is None:
            raise StylePresetError(404, "preset_not_found", "找不到預設")

        def operation(document: dict[str, Any]) -> None:
            before = len(document["presets"])
            document["presets"] = [item for item in document["presets"] if item["id"] != preset_id]
            if len(document["presets"]) == before:
                raise StylePresetError(404, "preset_not_found", "找不到預設")

        result = self._mutate(expected_revision, operation)
        result["deleted_id"] = preset_id
        return result

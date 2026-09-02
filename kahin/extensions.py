"""Safe WebExtension staging for the native Camoufox addon loader."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_EXTENSION_FILES = 1_000
MAX_EXTENSION_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_EXTENSION_NAME = 96

_UNSUPPORTED_MANIFEST_KEYS = (
    "declarative_net_request",
    "declarative_net_request_with_host_access",
    "offscreen",
    "side_panel",
    "externally_connectable",
    "protocol_handlers",
)


def _safe_source(source: str | Path) -> Path:
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise ValueError("extension source must be an absolute path")
    if not path.exists() or path.is_symlink():
        raise ValueError("extension source does not exist or is a symlink")
    if not path.is_dir() and path.suffix.lower() != ".zip":
        raise ValueError("extension source must be a directory or .zip archive")
    return path


def _manifest_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest.json exceeds the 1 MiB limit")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json is not valid JSON: {exc.msg}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must contain an object")
    version = manifest.get("manifest_version")
    if version not in (2, 3):
        raise ValueError("manifest_version must be 2 or 3")
    for field in ("name", "version"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"manifest.{field} must be a non-empty string")
    return manifest


def _unsupported(manifest: dict[str, Any]) -> list[str]:
    unsupported: list[str] = []
    background = manifest.get("background")
    if isinstance(background, dict) and isinstance(background.get("service_worker"), str):
        unsupported.append("background.service_worker")
    if isinstance(manifest.get("background"), str):
        unsupported.append("background")
    for key in _UNSUPPORTED_MANIFEST_KEYS:
        if key in manifest:
            unsupported.append(key)
    if "native_messaging" in manifest.get("permissions", []):
        unsupported.append("permissions.native_messaging")
    return sorted(set(unsupported))


def _manifest_summary(manifest: dict[str, Any], source_type: str, unsupported: list[str]) -> dict[str, Any]:
    scripts = manifest.get("content_scripts")
    script_count = len(scripts) if isinstance(scripts, list) else 0
    permissions = manifest.get("permissions")
    permission_count = len(permissions) if isinstance(permissions, list) else 0
    return {
        "name": str(manifest["name"])[:MAX_EXTENSION_NAME],
        "version": str(manifest["version"])[:MAX_EXTENSION_NAME],
        "manifestVersion": manifest["manifest_version"],
        "contentScriptCount": script_count,
        "permissionCount": permission_count,
        "hasBackground": isinstance(manifest.get("background"), dict),
        "sourceType": source_type,
        "unsupported": unsupported,
    }


def _safe_relative(raw: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("extension archive contains a path traversal entry")
    return path


def _copy_directory(source: Path, destination: Path) -> tuple[bytes, int]:
    total = 0
    files = 0
    manifest_raw: bytes | None = None
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if item.is_symlink():
            raise ValueError(f"extension contains a symlink: {relative}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file():
            raise ValueError(f"extension contains a non-regular file: {relative}")
        size = item.stat().st_size
        total += size
        files += 1
        if total > MAX_EXTENSION_BYTES or files > MAX_EXTENSION_FILES:
            raise ValueError("extension exceeds the file or size limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        if relative.as_posix() == "manifest.json":
            manifest_raw = item.read_bytes()
    if manifest_raw is None:
        raise ValueError("extension is missing a root manifest.json")
    return manifest_raw, files


def _zip_members(bundle: zipfile.ZipFile) -> tuple[list[tuple[zipfile.ZipInfo, PurePosixPath]], str]:
    files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    total = 0
    manifest_paths: list[PurePosixPath] = []
    for info in bundle.infolist():
        path = _safe_relative(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError("extension archive contains a symlink")
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_EXTENSION_BYTES or len(files) >= MAX_EXTENSION_FILES:
            raise ValueError("extension exceeds the file or size limit")
        files.append((info, path))
        if path.name == "manifest.json":
            manifest_paths.append(path)
    if not manifest_paths:
        raise ValueError("extension archive is missing manifest.json")
    if len(manifest_paths) > 1:
        raise ValueError("extension archive contains multiple manifest.json files")
    manifest_path = manifest_paths[0]
    prefix = manifest_path.parent
    normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for info, path in files:
        try:
            relative = path.relative_to(prefix)
        except ValueError as exc:
            raise ValueError("extension archive has files outside its manifest root") from exc
        if not relative.parts:
            continue
        normalized.append((info, relative))
    return normalized, "zip"


def _copy_zip(source: Path, destination: Path) -> tuple[bytes, int]:
    with zipfile.ZipFile(source) as bundle:
        members, _ = _zip_members(bundle)
        manifest_raw: bytes | None = None
        for info, relative in members:
            target = destination / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=64 * 1024)
            if relative.as_posix() == "manifest.json":
                manifest_raw = target.read_bytes()
        if manifest_raw is None:
            raise ValueError("extension archive is missing a root manifest.json")
        return manifest_raw, len(members)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-_")
    return (slug or "extension")[:MAX_EXTENSION_NAME]


def stage_extension(source: str | Path, destination_root: Path) -> dict[str, Any]:
    """Stage a real directory/zip addon and return its compatibility report."""
    source_path = _safe_source(source)
    if not destination_root.is_absolute():
        raise ValueError("extension destination must be an absolute path")
    destination_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:12]
    temporary = Path(tempfile.mkdtemp(prefix=".kahin-extension-", dir=destination_root))
    try:
        if source_path.is_dir():
            manifest_raw, file_count = _copy_directory(source_path, temporary)
            source_type = "directory"
        else:
            manifest_raw, file_count = _copy_zip(source_path, temporary)
            source_type = "zip"
        manifest = _manifest_bytes(manifest_raw)
        unsupported = _unsupported(manifest)
        final = destination_root / f"{_slug(str(manifest['name']))}-{digest}"
        if final.exists():
            shutil.rmtree(final)
        temporary.replace(final)
        summary = _manifest_summary(manifest, source_type, unsupported)
        return {
            "path": str(final),
            "compatible": not unsupported,
            "files": file_count,
            "manifest": summary,
            "unsupported": unsupported,
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def inspect_extension(path: str | Path) -> dict[str, Any]:
    """Inspect an unpacked addon path before passing it to Camoufox."""
    source = _safe_source(path)
    if not source.is_dir():
        raise ValueError("browser addons must be staged directories, not zip archives")
    manifest_path = source / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"extension manifest.json is unreadable: {exc}") from exc
    manifest = _manifest_bytes(raw)
    unsupported = _unsupported(manifest)
    return {
        "path": str(source),
        "compatible": not unsupported,
        "manifest": _manifest_summary(manifest, "directory", unsupported),
        "unsupported": unsupported,
    }

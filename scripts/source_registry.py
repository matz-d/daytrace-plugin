#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

VALID_SCOPE_MODES = {"all-day", "workspace"}
REQUIRED_SOURCE_FIELDS = {
    "name",
    "command",
    "required",
    "timeout_sec",
    "platforms",
    "supports_date_range",
    "supports_all_sessions",
    "scope_mode",
}
MANIFEST_KIND = "daytrace-source-manifest/v1"
SOURCE_IDENTITY_VERSION = "daytrace-source-identity/v1"
DEFAULT_USER_SOURCES_DIR = Path("~/.config/daytrace/sources.d")
BUILT_IN_REGISTRY_SCOPE = "built-in"
USER_REGISTRY_SCOPE = "user"


class RegistryValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]], message: str | None = None) -> None:
        self.issues = issues
        if message is None:
            if len(issues) == 1:
                message = str(issues[0]["message"])
            else:
                message = f"Source registry validation failed with {len(issues)} issue(s)"
        super().__init__(message)


def registry_error(
    *,
    kind: str,
    path: Path,
    registry_scope: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    issue = {
        "kind": kind,
        "path": str(path),
        "registry_scope": registry_scope,
        "message": message,
    }
    issue.update(extra)
    return issue


def normalize_confidence_categories(source: dict[str, Any]) -> list[str]:
    raw_value = source.get("confidence_category")
    source_name = str(source.get("name", "<unknown-source>"))
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        if not raw_value:
            raise ValueError(f"confidence_category must not be empty for {source_name}")
        return [raw_value]
    if isinstance(raw_value, list):
        if not all(isinstance(item, str) and item for item in raw_value):
            raise ValueError(f"confidence_category must be a string or list of non-empty strings for {source_name}")
        return list(raw_value)
    raise ValueError(f"confidence_category must be a string or list of non-empty strings for {source_name}")


def build_source_identity(source: dict[str, Any]) -> dict[str, str]:
    return {
        "source_id": str(source["name"]),
        "scope_mode": str(source["scope_mode"]),
        "identity_version": str(source.get("identity_version", SOURCE_IDENTITY_VERSION)),
    }


def manifest_fingerprint_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_kind": str(source.get("manifest_kind", MANIFEST_KIND)),
        "name": str(source["name"]),
        "command": str(source["command"]),
        "scope_mode": str(source["scope_mode"]),
        "supports_date_range": bool(source["supports_date_range"]),
        "supports_all_sessions": bool(source["supports_all_sessions"]),
        "confidence_categories": normalize_confidence_categories(source),
        "prerequisites": source.get("prerequisites", []),
    }


def compute_manifest_fingerprint(source: dict[str, Any]) -> str:
    payload = manifest_fingerprint_payload(source)
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_bool(value: Any, field_name: str, source_name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean for {source_name}")


def _validate_prerequisites(prerequisites: Any, source_name: str) -> None:
    if prerequisites is None:
        return
    if not isinstance(prerequisites, list):
        raise ValueError(f"prerequisites must be a list for {source_name}")
    for prerequisite in prerequisites:
        if not isinstance(prerequisite, dict):
            raise ValueError(f"each prerequisite must be an object for {source_name}")
        prereq_type = prerequisite.get("type")
        if not isinstance(prereq_type, str) or not prereq_type:
            raise ValueError(f"each prerequisite.type must be a non-empty string for {source_name}")


def validate_source_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("Each source entry must be an object")

    missing = REQUIRED_SOURCE_FIELDS - set(entry.keys())
    if missing:
        raise ValueError(f"Source entry is missing fields: {sorted(missing)}")

    source_name = entry["name"]
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("name must be a non-empty string")
    if not isinstance(entry["command"], str) or not entry["command"]:
        raise ValueError(f"command must be a non-empty string for {source_name}")

    _validate_bool(entry["required"], "required", source_name)
    timeout_sec = entry["timeout_sec"]
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or timeout_sec <= 0:
        raise ValueError(f"timeout_sec must be a positive number for {source_name}")

    platforms = entry["platforms"]
    if not isinstance(platforms, list) or not platforms or not all(isinstance(item, str) and item for item in platforms):
        raise ValueError(f"platforms must be a non-empty list of strings for {source_name}")

    _validate_bool(entry["supports_date_range"], "supports_date_range", source_name)
    _validate_bool(entry["supports_all_sessions"], "supports_all_sessions", source_name)

    if entry["scope_mode"] not in VALID_SCOPE_MODES:
        raise ValueError(f"scope_mode must be one of {sorted(VALID_SCOPE_MODES)} for {source_name}")

    normalize_confidence_categories(entry)
    _validate_prerequisites(entry.get("prerequisites", []), source_name)

    normalized = dict(entry)
    normalized.setdefault("prerequisites", [])
    normalized["manifest_kind"] = str(entry.get("manifest_kind", MANIFEST_KIND))
    normalized["identity_version"] = str(entry.get("identity_version", SOURCE_IDENTITY_VERSION))
    normalized["source_identity"] = build_source_identity(normalized)
    normalized["source_id"] = normalized["source_identity"]["source_id"]
    normalized["manifest_fingerprint"] = compute_manifest_fingerprint(normalized)
    return normalized


def _load_manifest_data(
    path: Path,
    *,
    registry_scope: str,
    allow_array: bool,
) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RegistryValidationError(
            [
                registry_error(
                    kind="invalid_json",
                    path=path,
                    registry_scope=registry_scope,
                    message=f"Invalid JSON in source manifest: {exc.msg}",
                    line=exc.lineno,
                    column=exc.colno,
                )
            ]
        ) from exc

    if isinstance(data, dict):
        data = [data]
    elif not allow_array or not isinstance(data, list):
        expected_shape = "JSON object or array" if allow_array else "single JSON object"
        raise RegistryValidationError(
            [
                registry_error(
                    kind="invalid_shape",
                    path=path,
                    registry_scope=registry_scope,
                    message=f"Source manifest file must contain a {expected_shape}",
                )
            ]
        )

    sources = []
    seen_source_ids: set[str] = set()
    for index, entry in enumerate(data):
        try:
            normalized = validate_source_entry(entry)
        except ValueError as exc:
            raise RegistryValidationError(
                [
                    registry_error(
                        kind="invalid_manifest",
                        path=path,
                        registry_scope=registry_scope,
                        message=str(exc),
                        entry_index=index,
                    )
                ]
            ) from exc
        source_id = normalized["source_id"]
        if source_id in seen_source_ids:
            raise RegistryValidationError(
                [
                    registry_error(
                        kind="duplicate_source",
                        path=path,
                        registry_scope=registry_scope,
                        message=f"Duplicate source name in registry: {source_id}",
                        source_name=source_id,
                        entry_index=index,
                    )
                ]
            )
        seen_source_ids.add(source_id)
        normalized["registry_scope"] = registry_scope
        normalized["manifest_path"] = str(path)
        sources.append(normalized)
    return sources


def load_sources(path: Path) -> list[dict[str, Any]]:
    return _load_manifest_data(path, registry_scope="explicit", allow_array=True)


def load_built_in_sources(path: Path) -> list[dict[str, Any]]:
    return _load_manifest_data(path, registry_scope=BUILT_IN_REGISTRY_SCOPE, allow_array=True)


def discover_user_sources(user_sources_dir: Path | None = None) -> list[Path]:
    resolved_dir = (user_sources_dir or DEFAULT_USER_SOURCES_DIR).expanduser()
    if not resolved_dir.exists():
        return []
    if not resolved_dir.is_dir():
        raise RegistryValidationError(
            [
                registry_error(
                    kind="invalid_registry_path",
                    path=resolved_dir,
                    registry_scope=USER_REGISTRY_SCOPE,
                    message="User sources path must be a directory",
                )
            ]
        )
    return sorted(path for path in resolved_dir.iterdir() if path.is_file() and path.suffix == ".json")


def load_user_sources(user_sources_dir: Path | None = None) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for manifest_path in discover_user_sources(user_sources_dir):
        try:
            sources.extend(_load_manifest_data(manifest_path, registry_scope=USER_REGISTRY_SCOPE, allow_array=False))
        except RegistryValidationError as exc:
            issues.extend(exc.issues)
    if issues:
        raise RegistryValidationError(issues)
    return sources


def load_registry(
    built_in_sources_file: Path,
    *,
    user_sources_dir: Path | None = None,
    include_user_sources: bool = True,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_source_paths: dict[str, str] = {}

    try:
        sources.extend(load_built_in_sources(built_in_sources_file))
    except RegistryValidationError as exc:
        issues.extend(exc.issues)

    if include_user_sources:
        try:
            sources.extend(load_user_sources(user_sources_dir))
        except RegistryValidationError as exc:
            issues.extend(exc.issues)

    for source in sources:
        source_id = str(source["source_id"])
        manifest_path = str(source["manifest_path"])
        if source_id in seen_source_paths:
            issues.append(
                registry_error(
                    kind="duplicate_source",
                    path=Path(manifest_path),
                    registry_scope=str(source["registry_scope"]),
                    message=f"Duplicate source name in registry: {source_id}",
                    source_name=source_id,
                    conflicting_path=seen_source_paths[source_id],
                )
            )
            continue
        seen_source_paths[source_id] = manifest_path

    if issues:
        raise RegistryValidationError(issues)
    return sorted(sources, key=lambda item: str(item["name"]))

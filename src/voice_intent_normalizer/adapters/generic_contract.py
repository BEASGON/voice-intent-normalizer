"""Pure, dependency-free V1 contracts for the generic adapter capsule."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sized
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

CAPSULE_PROTOCOL = 1
GENERATION_FORMAT = 1
STATUS_FORMAT = 5
LAYOUT_NAME = "versioned-v1"

_CAPSULE_IDENTIFIER = "voice-intent-normalizer"
_MAX_FILES = 4096
_MAX_PATH_LENGTH = 512
_MAX_PATH_DEPTH = 32
_MAX_VERSION_LENGTH = 256
_MAX_JSON_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_GENERATION_ID_RE = re.compile(r"\Ag-[0-9a-f]{64}-[0-9a-f]{32}\Z")
_TRANSACTION_ID_RE = re.compile(r"\At-[0-9a-f]{32}\Z")
_TRANSACTION_PHASES = frozenset(
    {
        "generation-published",
        "capsule-published",
        "activation-pending",
        "rollback-pending",
        "deactivation-pending",
        "capsule-retired",
        "cleanup-pending",
    }
)


@dataclass(frozen=True, slots=True)
class GenerationRef:
    """One immutable runtime generation selected by protected status."""

    generation_id: str
    manifest_digest: str
    package_hash: str
    package_version: str


@dataclass(frozen=True, slots=True)
class CapsuleRef:
    """The immutable host-visible capsule selected by protected status."""

    protocol: int
    manifest_digest: str
    package_hash: str


@dataclass(frozen=True, slots=True)
class StatusV5:
    """Validated status and paths derived solely from trusted roots and IDs."""

    capability: str
    capsule: CapsuleRef
    active: GenerationRef
    previous: GenerationRef | None
    transaction_id: str | None
    transaction_phase: str | None
    capsule_root: Path
    active_root: Path
    previous_root: Path | None


def canonical_json_bytes(value: object) -> bytes:
    """Encode a JSON value in the sole canonical representation used by V1."""
    try:
        return json.dumps(
            _json_ready(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def build_manifest(
    kind: str,
    identifier: str,
    package_version: str,
    files: Mapping[str, bytes],
) -> Mapping[str, object]:
    """Build a canonical immutable manifest from exact file bytes."""
    if not isinstance(files, Mapping):
        raise ValueError("manifest files must be a mapping")
    entries = _bounded_mapping_keys(files, _MAX_FILES, "manifest file list")
    entries = tuple(sorted(_validate_files(entries)))
    hashes: dict[str, str] = {}
    for path in entries:
        data = files[path]
        if not isinstance(data, bytes):
            raise ValueError("manifest file data must be bytes")
        hashes[path] = hashlib.sha256(data).hexdigest()
    return _manifest_mapping(kind, identifier, package_version, entries, hashes)


def validate_manifest(payload: object) -> Mapping[str, object]:
    """Validate and detach a V1 manifest from untrusted JSON or mappings."""
    value = _json_value(payload, "manifest")
    value = _mapping_with_fields(value, {
        "format",
        "kind",
        "identifier",
        "package_version",
        "package_hash",
        "files",
        "file_hashes",
    }, "invalid generation manifest fields")
    if value["format"] != GENERATION_FORMAT or type(value["format"]) is not int:
        raise ValueError("unsupported generation manifest format")
    kind = value["kind"]
    identifier = value["identifier"]
    package_version = value["package_version"]
    package_hash = value["package_hash"]
    if not isinstance(kind, str) or kind not in {"generation", "capsule"}:
        raise ValueError("invalid generation manifest kind")
    _validate_identifier(kind, identifier)
    _validate_version(package_version)
    _validate_sha256(package_hash, "package hash")
    if not isinstance(value["files"], (list, tuple)):
        raise ValueError("manifest files must be a sequence")
    files = _validate_files(value["files"])
    if files != tuple(sorted(files)):
        raise ValueError("manifest files must be sorted and unique")
    hashes = _validate_hashes(value["file_hashes"], files)
    manifest = _manifest_mapping(kind, identifier, package_version, files, hashes)
    if manifest["package_hash"] != package_hash:
        raise ValueError("manifest aggregate hash does not match")
    return manifest


def manifest_digest(payload: object) -> str:
    """Return the canonical manifest identity, including its generation ID."""
    return hashlib.sha256(canonical_json_bytes(validate_manifest(payload))).hexdigest()


def validate_status_v5(
    payload: object, *, skill_root: Path, generations_root: Path
) -> StatusV5:
    """Validate V5 status and derive paths without accepting JSON path strings."""
    value = _json_value(payload, "adapter status")
    value = _mapping_with_fields(value, {
        "format",
        "layout",
        "capability",
        "capsule",
        "active",
        "previous",
        "transaction",
    }, "invalid adapter status fields")
    if value["format"] != STATUS_FORMAT or type(value["format"]) is not int:
        raise ValueError("unsupported adapter status format")
    if value["layout"] != LAYOUT_NAME:
        raise ValueError("unsupported adapter status layout")
    if value["capability"] not in {"manual", "implicit"}:
        raise ValueError("invalid generic adapter capability")
    if not isinstance(skill_root, Path) or not isinstance(generations_root, Path):
        raise ValueError("trusted roots must be paths")
    capsule = _validate_capsule(value["capsule"])
    active = _validate_generation_ref(value["active"], "active generation")
    previous_value = value["previous"]
    previous = (
        None
        if previous_value is None
        else _validate_generation_ref(previous_value, "previous generation")
    )
    if previous is not None and previous.generation_id == active.generation_id:
        raise ValueError("previous generation must not be active")
    transaction_id, transaction_phase = _validate_transaction(value["transaction"])
    capsule_root = skill_root / _CAPSULE_IDENTIFIER
    active_root = generations_root / active.generation_id
    previous_root = (
        None if previous is None else generations_root / previous.generation_id
    )
    return StatusV5(
        capability=value["capability"],
        capsule=capsule,
        active=active,
        previous=previous,
        transaction_id=transaction_id,
        transaction_phase=transaction_phase,
        capsule_root=capsule_root,
        active_root=active_root,
        previous_root=previous_root,
    )


def _manifest_mapping(
    kind: str,
    identifier: str,
    package_version: str,
    files: tuple[str, ...],
    hashes: Mapping[str, str],
) -> Mapping[str, object]:
    _validate_identifier(kind, identifier)
    _validate_version(package_version)
    package_hash = _aggregate_hash(kind, package_version, files, hashes)
    return MappingProxyType(
        {
            "format": GENERATION_FORMAT,
            "kind": kind,
            "identifier": identifier,
            "package_version": package_version,
            "package_hash": package_hash,
            "files": files,
            "file_hashes": MappingProxyType(dict(hashes)),
        }
    )


def _aggregate_hash(
    kind: str,
    package_version: str,
    files: tuple[str, ...],
    hashes: Mapping[str, str],
) -> str:
    aggregate = {
        "kind": kind,
        "package_version": package_version,
        "files": files,
        "file_hashes": dict(hashes),
    }
    return hashlib.sha256(canonical_json_bytes(aggregate)).hexdigest()


def _json_value(payload: object, label: str) -> object:
    if isinstance(payload, (bytes, str)):
        _validate_raw_json_size(payload, label)
        try:
            return json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid {label} JSON") from exc
    if isinstance(payload, Mapping):
        return payload
    raise ValueError(f"invalid {label}")


def _validate_raw_json_size(payload: bytes | str, label: str) -> None:
    if isinstance(payload, bytes):
        if len(payload) > _MAX_JSON_BYTES:
            raise ValueError(f"{label} exceeds size limit")
        return
    if len(payload) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds size limit")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds size limit")


def _json_ready(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_files(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_FILES:
        raise ValueError("invalid manifest file list")
    files: list[str] = []
    seen: set[str] = set()
    iterator = iter(values)
    for _ in range(_MAX_FILES + 1):
        try:
            value = next(iterator)
        except StopIteration:
            return tuple(files)
        if not isinstance(value, str):
            raise ValueError("manifest file path must be a string")
        _validate_relative_path(value)
        if value in seen:
            raise ValueError("manifest file paths must be unique")
        seen.add(value)
        files.append(value)
    raise ValueError("invalid manifest file list")


def _bounded_mapping_keys(
    value: Mapping[str, object], maximum: int, label: str
) -> tuple[str, ...]:
    if isinstance(value, Sized) and len(value) > maximum:
        raise ValueError(f"invalid {label}")
    keys: list[str] = []
    iterator = iter(value)
    for _ in range(maximum + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return tuple(keys)
        if not isinstance(key, str):
            raise ValueError(f"invalid {label}")
        keys.append(key)
    raise ValueError(f"invalid {label}")


def _mapping_with_fields(
    value: object, fields: set[str], label: str
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(label)
    keys = _bounded_mapping_keys(value, len(fields), label)
    if len(keys) != len(fields) or set(keys) != fields:
        raise ValueError(label)
    return {key: value[key] for key in fields}


def _validate_hashes(value: object, files: tuple[str, ...]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("manifest file hashes do not match files")
    keys = _bounded_mapping_keys(value, len(files), "manifest file hashes")
    if len(keys) != len(files) or set(keys) != set(files):
        raise ValueError("manifest file hashes do not match files")
    hashes: dict[str, str] = {}
    for path in files:
        digest = value[path]
        _validate_sha256(digest, "manifest file hash")
        hashes[path] = digest
    return MappingProxyType(hashes)


def _validate_identifier(kind: object, identifier: object) -> None:
    if not isinstance(identifier, str):
        raise ValueError("manifest identifier must be a string")
    if kind == "generation":
        if not _GENERATION_ID_RE.fullmatch(identifier):
            raise ValueError("invalid generation identifier")
        return
    if kind == "capsule" and identifier == _CAPSULE_IDENTIFIER:
        return
    raise ValueError("invalid manifest identifier")


def _validate_version(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_VERSION_LENGTH
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("invalid package version")


def _validate_relative_path(value: str) -> None:
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise ValueError("invalid manifest file path")
    try:
        path_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("invalid manifest file path") from exc
    if path_length > _MAX_PATH_LENGTH:
        raise ValueError("invalid manifest file path")
    parts = value.split("/")
    if (
        len(parts) > _MAX_PATH_DEPTH
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.endswith((".", " ")) for part in parts)
        or any(_is_windows_device_name(part) for part in parts)
        or any(any(ord(character) < 0x20 for character in part) for part in parts)
    ):
        raise ValueError("invalid manifest file path")


def _is_windows_device_name(component: str) -> bool:
    base = component.split(".", 1)[0].upper()
    return base in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }


def _validate_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _validate_capsule(value: object) -> CapsuleRef:
    value = _mapping_with_fields(value, {
        "protocol",
        "manifest_digest",
        "package_hash",
    }, "invalid capsule reference")
    if value["protocol"] != CAPSULE_PROTOCOL or type(value["protocol"]) is not int:
        raise ValueError("unsupported capsule protocol")
    _validate_sha256(value["manifest_digest"], "capsule manifest digest")
    _validate_sha256(value["package_hash"], "capsule package hash")
    return CapsuleRef(
        protocol=value["protocol"],
        manifest_digest=value["manifest_digest"],
        package_hash=value["package_hash"],
    )


def _validate_generation_ref(value: object, label: str) -> GenerationRef:
    value = _mapping_with_fields(value, {
        "generation_id",
        "manifest_digest",
        "package_hash",
        "package_version",
    }, f"invalid {label}")
    generation_id = value["generation_id"]
    _validate_identifier("generation", generation_id)
    _validate_sha256(value["manifest_digest"], f"{label} manifest digest")
    _validate_sha256(value["package_hash"], f"{label} package hash")
    _validate_version(value["package_version"])
    if generation_id[2:66] != value["package_hash"]:
        raise ValueError(f"{label} package hash is not anchored by its identifier")
    return GenerationRef(
        generation_id=generation_id,
        manifest_digest=value["manifest_digest"],
        package_hash=value["package_hash"],
        package_version=value["package_version"],
    )


def _validate_transaction(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    value = _mapping_with_fields(value, {"id", "phase"}, "invalid adapter transaction")
    transaction_id = value["id"]
    phase = value["phase"]
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
        or phase not in _TRANSACTION_PHASES
    ):
        raise ValueError("invalid adapter transaction")
    return transaction_id, phase

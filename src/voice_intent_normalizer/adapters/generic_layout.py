"""Private state layout for the versioned generic adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from voice_intent_normalizer.adapters.generic_contract import (
    CapsuleRef,
    GenerationRef,
    StatusV5,
    build_manifest,
    canonical_json_bytes,
    manifest_digest,
    validate_manifest,
    validate_status_v5,
)
from voice_intent_normalizer.paths import StatePaths

_WINDOWS_REPARSE_POINT = 0x400
_MAX_SOURCE_FILES = 4096
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_DEPTH = 32

_CAPSULE_SOURCES = {
    "SKILL.md": "SKILL.md",
    "agents/openai.yaml": "agents/openai.yaml",
    "references/correction-policy.md": "references/correction-policy.md",
    "references/domain-packs.md": "references/domain-packs.md",
    "references/lexicon-schema.md": "references/lexicon-schema.md",
    "scripts/_voice_intent_contract.py": (
        "src/voice_intent_normalizer/adapters/generic_contract.py"
    ),
    "scripts/voice_intent.py": "scripts/voice_intent.py",
}

_GENERATION_FILES = (
    "SKILL.md",
    "LICENSE",
    "pyproject.toml",
    "agents/openai.yaml",
    "assets/lexicons/base-zh.jsonl",
    "assets/lexicons/hotwords-snapshot.jsonl",
    "assets/lexicons/domains/ai.jsonl",
    "assets/lexicons/domains/product-design.jsonl",
    "assets/lexicons/domains/software-development.jsonl",
    "references/correction-policy.md",
    "references/domain-packs.md",
    "references/lexicon-schema.md",
    "scripts/voice_intent.py",
    "src/voice_intent_normalizer/__init__.py",
    "src/voice_intent_normalizer/cli.py",
    "src/voice_intent_normalizer/hook.py",
    "src/voice_intent_normalizer/installer.py",
    "src/voice_intent_normalizer/learning.py",
    "src/voice_intent_normalizer/lexicon.py",
    "src/voice_intent_normalizer/matching.py",
    "src/voice_intent_normalizer/models.py",
    "src/voice_intent_normalizer/paths.py",
    "src/voice_intent_normalizer/policy.py",
    "src/voice_intent_normalizer/project_scan.py",
    "src/voice_intent_normalizer/service.py",
    "src/voice_intent_normalizer/updater.py",
    "src/voice_intent_normalizer/adapters/__init__.py",
    "src/voice_intent_normalizer/adapters/base.py",
    "src/voice_intent_normalizer/adapters/generic.py",
    "src/voice_intent_normalizer/adapters/generic_contract.py",
    "src/voice_intent_normalizer/adapters/generic_layout.py",
)
_VERSION_PATTERN = re.compile(br'(?m)^version = "([^"\r\n]+)"$')
_TRANSACTION_ID_PATTERN = re.compile(r"t-[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
CAPSULE_PROTOCOL_VERSION = "1"
TRANSACTION_PHASES = (
    "generation-published",
    "capsule-published",
    "activation-pending",
    "rollback-pending",
    "deactivation-pending",
    "capsule-retired",
    "cleanup-pending",
)


@dataclass(frozen=True, slots=True)
class GenericLayoutPaths:
    """Resolved generic-adapter paths without filesystem mutation."""

    adapter_root: Path
    status: Path
    transaction: Path
    generations: Path
    staging: Path
    retired: Path


@dataclass(frozen=True, slots=True)
class VersionedArtifact:
    """One complete immutable directory prepared for private publication."""

    kind: str
    identifier: str
    package_version: str
    package_hash: str
    manifest_digest: str
    manifest_name: str
    files: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class VersionedArtifacts:
    """The stable capsule and one identifier-independent runtime generation."""

    capsule: VersionedArtifact
    generation: VersionedArtifact


def generic_layout_paths(state_paths: StatePaths) -> GenericLayoutPaths:
    """Return the immutable V1 generic-adapter layout under shared state."""
    root = state_paths.generic_adapter_root()
    return GenericLayoutPaths(
        adapter_root=root,
        status=root / "status.json",
        transaction=root / "transaction.json",
        generations=root / "generations",
        staging=root / "staging",
        retired=root / "retired",
    )


def capsule_source_files(repository: str | Path) -> dict[str, bytes]:
    """Build the exact immutable V1 agent-visible capsule byte set."""
    root = _direct_repository(repository)
    files = {
        destination: _read_direct_source(root, source)
        for destination, source in _CAPSULE_SOURCES.items()
    }
    _enforce_source_limits(files)
    manifest = build_manifest(
        "capsule",
        "voice-intent-normalizer",
        CAPSULE_PROTOCOL_VERSION,
        files,
    )
    result = {**files, "capsule.json": canonical_json_bytes(manifest)}
    return dict(sorted(result.items()))


def generation_source_files(repository: str | Path) -> dict[str, bytes]:
    """Collect the exact self-contained runtime repository for one generation."""
    root = _direct_repository(repository)
    files = {
        relative: _read_direct_source(root, relative)
        for relative in _GENERATION_FILES
    }
    _enforce_source_limits(files)
    return dict(sorted(files.items()))


def prepare_versioned_artifacts(
    repository: str | Path, generation_nonce: str
) -> VersionedArtifacts:
    """Build the complete immutable byte sets for one first activation."""
    if re.fullmatch(r"[0-9a-f]{32}", generation_nonce) is None:
        raise ValueError("invalid generation nonce")

    capsule_files = capsule_source_files(repository)
    capsule_manifest_bytes = capsule_files["capsule.json"]
    capsule_manifest = validate_manifest(capsule_manifest_bytes)
    if (
        capsule_manifest["kind"] != "capsule"
        or capsule_manifest["identifier"] != "voice-intent-normalizer"
        or canonical_json_bytes(capsule_manifest) != capsule_manifest_bytes
    ):
        raise ValueError("invalid prepared capsule")
    capsule = VersionedArtifact(
        kind="capsule",
        identifier="voice-intent-normalizer",
        package_version=str(capsule_manifest["package_version"]),
        package_hash=str(capsule_manifest["package_hash"]),
        manifest_digest=manifest_digest(capsule_manifest),
        manifest_name="capsule.json",
        files=MappingProxyType(dict(capsule_files)),
    )

    generation_runtime = generation_source_files(repository)
    package_version = _package_version(generation_runtime["pyproject.toml"])
    provisional = build_manifest(
        "generation",
        f"g-{'0' * 64}-{generation_nonce}",
        package_version,
        generation_runtime,
    )
    generation_id = f"g-{provisional['package_hash']}-{generation_nonce}"
    generation_manifest = build_manifest(
        "generation",
        generation_id,
        package_version,
        generation_runtime,
    )
    generation_files = {
        **generation_runtime,
        "generation.json": canonical_json_bytes(generation_manifest),
    }
    generation = VersionedArtifact(
        kind="generation",
        identifier=generation_id,
        package_version=package_version,
        package_hash=str(generation_manifest["package_hash"]),
        manifest_digest=manifest_digest(generation_manifest),
        manifest_name="generation.json",
        files=MappingProxyType(dict(sorted(generation_files.items()))),
    )
    return VersionedArtifacts(capsule=capsule, generation=generation)


def generation_ref_payload(artifact: VersionedArtifact) -> dict[str, object]:
    """Return the canonical status reference for one prepared generation."""
    if artifact.kind != "generation":
        raise ValueError("generation reference requires a generation artifact")
    return {
        "generation_id": artifact.identifier,
        "manifest_digest": artifact.manifest_digest,
        "package_hash": artifact.package_hash,
        "package_version": artifact.package_version,
    }


def status_v5_payload(
    *,
    capability: str,
    capsule: CapsuleRef | VersionedArtifact,
    active: GenerationRef | VersionedArtifact,
    previous: GenerationRef | VersionedArtifact | None,
    transaction_id: str | None = None,
    transaction_phase: str | None = None,
) -> dict[str, object]:
    """Build one canonical V5 payload from already validated references."""
    if isinstance(capsule, VersionedArtifact):
        if capsule.kind != "capsule":
            raise ValueError("invalid capsule reference")
        capsule_payload = {
            "protocol": 1,
            "manifest_digest": capsule.manifest_digest,
            "package_hash": capsule.package_hash,
        }
    else:
        capsule_payload = {
            "protocol": capsule.protocol,
            "manifest_digest": capsule.manifest_digest,
            "package_hash": capsule.package_hash,
        }

    def generation_payload(
        value: GenerationRef | VersionedArtifact,
    ) -> dict[str, object]:
        if isinstance(value, VersionedArtifact):
            return generation_ref_payload(value)
        return {
            "generation_id": value.generation_id,
            "manifest_digest": value.manifest_digest,
            "package_hash": value.package_hash,
            "package_version": value.package_version,
        }

    if (transaction_id is None) != (transaction_phase is None):
        raise ValueError("incomplete adapter transaction")
    transaction = None
    if transaction_id is not None:
        if (
            _TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
            or transaction_phase not in TRANSACTION_PHASES
        ):
            raise ValueError("invalid adapter transaction")
        transaction = {"id": transaction_id, "phase": transaction_phase}
    return {
        "format": 5,
        "layout": "versioned-v1",
        "capability": capability,
        "capsule": capsule_payload,
        "active": generation_payload(active),
        "previous": None if previous is None else generation_payload(previous),
        "transaction": transaction,
    }


def canonical_status_v5(
    payload: object, *, skill_root: Path, generations_root: Path
) -> tuple[StatusV5, bytes]:
    """Validate V5 status and require its sole canonical byte representation."""
    status = validate_status_v5(
        payload, skill_root=skill_root, generations_root=generations_root
    )
    rebuilt = status_v5_payload(
        capability=status.capability,
        capsule=status.capsule,
        active=status.active,
        previous=status.previous,
        transaction_id=status.transaction_id,
        transaction_phase=status.transaction_phase,
    )
    canonical = canonical_json_bytes(rebuilt)
    if isinstance(payload, bytes) and payload != canonical:
        raise ValueError("adapter status is not canonical")
    if isinstance(payload, str) and payload.encode("utf-8") != canonical:
        raise ValueError("adapter status is not canonical")
    return status, canonical


def validate_anchored_manifest(
    payload: object,
    *,
    kind: str,
    identifier: str,
    expected_manifest_digest: str,
    expected_package_hash: str,
    expected_package_version: str | None = None,
) -> Mapping[str, object]:
    """Validate a manifest against a complete protected status reference."""
    manifest = validate_manifest(payload)
    canonical = canonical_json_bytes(manifest)
    if isinstance(payload, bytes) and payload != canonical:
        raise ValueError("artifact manifest is not canonical")
    if (
        manifest["kind"] != kind
        or manifest["identifier"] != identifier
        or manifest["package_hash"] != expected_package_hash
        or manifest_digest(manifest) != expected_manifest_digest
        or (
            expected_package_version is not None
            and manifest["package_version"] != expected_package_version
        )
    ):
        raise ValueError("artifact manifest is not anchored by adapter status")
    return manifest


def recovery_marker_bytes(transaction_id: str, status_bytes: bytes) -> bytes:
    """Return the minimal independent marker for one status transaction."""
    if _TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None:
        raise ValueError("invalid recovery transaction id")
    return canonical_json_bytes(
        {
            "status_digest": hashlib.sha256(status_bytes).hexdigest(),
            "transaction_id": transaction_id,
        }
    )


def validate_recovery_marker(
    payload: bytes, *, transaction_id: str, status_bytes: bytes
) -> None:
    """Require a canonical marker bound to the exact authoritative status."""
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid adapter recovery marker") from exc
    expected = recovery_marker_bytes(transaction_id, status_bytes)
    if (
        not isinstance(value, dict)
        or set(value) != {"status_digest", "transaction_id"}
        or value.get("transaction_id") != transaction_id
        or not isinstance(value.get("status_digest"), str)
        or _SHA256_PATTERN.fullmatch(str(value["status_digest"])) is None
        or payload != expected
    ):
        raise ValueError("adapter recovery marker is not status anchored")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _direct_repository(repository: str | Path) -> Path:
    root = Path(repository).absolute()
    try:
        info = root.lstat()
    except OSError as exc:
        raise ValueError("runtime repository is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_alias(info):
        raise ValueError("runtime repository must be a direct directory")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("runtime repository is unavailable") from exc
    if os.path.normcase(os.fspath(root)) != os.path.normcase(os.fspath(resolved)):
        raise ValueError("runtime repository contains an alias")
    return resolved


def _read_direct_source(root: Path, relative: str) -> bytes:
    parts = relative.split("/")
    if (
        not parts
        or len(parts) > _MAX_SOURCE_DEPTH
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("invalid runtime source path")
    current = root
    for component in parts[:-1]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError("runtime source is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or _is_alias(info):
            raise ValueError("runtime source contains an alias")
    source = current / parts[-1]
    try:
        before = source.lstat()
    except OSError as exc:
        raise ValueError("runtime source is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or _is_alias(before):
        raise ValueError("runtime source must be a direct regular file")
    if before.st_size > _MAX_SOURCE_FILE_BYTES:
        raise ValueError("runtime source file exceeds size limit")
    try:
        data = source.read_bytes()
        after = source.lstat()
    except OSError as exc:
        raise ValueError("runtime source is unavailable") from exc
    if (
        len(data) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or _is_alias(after)
    ):
        raise ValueError("runtime source changed while it was read")
    return data


def _enforce_source_limits(files: dict[str, bytes]) -> None:
    if len(files) > _MAX_SOURCE_FILES:
        raise ValueError("runtime source exceeds file limit")
    total = 0
    for relative, data in files.items():
        if len(relative.split("/")) > _MAX_SOURCE_DEPTH:
            raise ValueError("runtime source exceeds depth limit")
        if len(data) > _MAX_SOURCE_FILE_BYTES:
            raise ValueError("runtime source file exceeds size limit")
        total += len(data)
        if total > _MAX_SOURCE_BYTES:
            raise ValueError("runtime source exceeds byte limit")


def _package_version(pyproject: bytes) -> str:
    match = _VERSION_PATTERN.search(pyproject)
    if match is None:
        raise ValueError("package version is unavailable")
    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("package version is unavailable") from exc


def _is_alias(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )

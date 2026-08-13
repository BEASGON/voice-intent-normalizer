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
    _validate_capsule,
    _validate_generation_ref,
    build_manifest,
    canonical_json_bytes,
    manifest_digest,
    status_skill_root,
    validate_manifest,
    validate_status_v5,
)
from voice_intent_normalizer.paths import StatePaths, validate_state_root

_WINDOWS_REPARSE_POINT = 0x400
_MAX_SOURCE_FILES = 4096
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_DEPTH = 32
_MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_OWNERSHIP_JOURNAL_FIELDS = {
    "baseline_status_digest",
    "candidate",
    "capsule",
    "format",
    "operation",
    "selected_skill_root",
    "status_transition",
    "transaction_id",
}

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
    "references/platform-compatibility.md",
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
    "src/voice_intent_normalizer/adapters/codex.py",
    "src/voice_intent_normalizer/adapters/openclaw.py",
    "src/voice_intent_normalizer/adapters/workbuddy.py",
    "src/voice_intent_normalizer/adapters/generic.py",
    "src/voice_intent_normalizer/adapters/generic_contract.py",
    "src/voice_intent_normalizer/adapters/generic_layout.py",
)
_VERSION_PATTERN = re.compile(br'(?m)^version = "([^"\r\n]+)"\r?$')
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


@dataclass(frozen=True, slots=True)
class JournalTransition:
    """The exact protected-status digest transition owned by one journal."""

    before_digest: str | None
    after_digest: str


@dataclass(frozen=True, slots=True)
class OwnershipJournal:
    """The one clean-V1 prepublication ownership record."""

    operation: str
    transaction_id: str
    selected_skill_root: Path
    baseline_status_digest: str | None
    transition: JournalTransition
    capsule: CapsuleRef
    candidate: GenerationRef
    staging_root: Path
    generation_root: Path


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
    skill_root: Path,
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
        "selected_skill_root": os.fspath(skill_root),
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
        skill_root=status.selected_skill_root,
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


def ownership_journal_bytes(
    *,
    operation: str,
    transaction_id: str,
    skill_root: Path,
    baseline_status_bytes: bytes | None,
    before_status_bytes: bytes | None,
    after_status_bytes: bytes,
    capsule: CapsuleRef | VersionedArtifact,
    candidate: GenerationRef | VersionedArtifact,
) -> bytes:
    """Build the sole canonical clean-V1 ownership journal."""
    selected_skill_root = _validated_journal_skill_root(skill_root)
    baseline_digest = _status_digest(baseline_status_bytes, "baseline status")
    before_digest = _status_digest(before_status_bytes, "before status")
    after_digest = _required_status_digest(after_status_bytes, "after status")
    journal_capsule = _journal_capsule_ref(capsule)
    journal_candidate = _journal_generation_ref(candidate)
    _validate_journal_transition(
        operation=operation,
        transaction_id=transaction_id,
        baseline_digest=baseline_digest,
        before_digest=before_digest,
        after_digest=after_digest,
    )
    return canonical_json_bytes(
        _ownership_journal_payload(
            operation=operation,
            transaction_id=transaction_id,
            selected_skill_root=selected_skill_root,
            baseline_digest=baseline_digest,
            before_digest=before_digest,
            after_digest=after_digest,
            capsule=journal_capsule,
            candidate=journal_candidate,
        )
    )


def ownership_journal_transition_bytes(
    journal: OwnershipJournal,
    *,
    before_status_bytes: bytes | None,
    after_status_bytes: bytes,
) -> bytes:
    """Rewrite one validated journal with an exact next status transition."""
    before_digest = _status_digest(before_status_bytes, "before status")
    after_digest = _required_status_digest(after_status_bytes, "after status")
    _validate_journal_transition(
        operation=journal.operation,
        transaction_id=journal.transaction_id,
        baseline_digest=journal.baseline_status_digest,
        before_digest=before_digest,
        after_digest=after_digest,
    )
    return canonical_json_bytes(
        _ownership_journal_payload(
            operation=journal.operation,
            transaction_id=journal.transaction_id,
            selected_skill_root=journal.selected_skill_root,
            baseline_digest=journal.baseline_status_digest,
            before_digest=before_digest,
            after_digest=after_digest,
            capsule=journal.capsule,
            candidate=journal.candidate,
        )
    )


def validate_ownership_journal(
    payload: bytes,
    *,
    skill_root: Path,
    generations_root: Path,
) -> OwnershipJournal:
    """Validate exact journal bytes and derive its only owned paths."""
    value = _parse_ownership_journal(payload)
    if not isinstance(generations_root, Path):
        raise ValueError("invalid generation root")
    trusted_generations_root = _validated_journal_generation_root(generations_root)
    selected_skill_root = _selected_journal_skill_root(value["selected_skill_root"])
    trusted_skill_root = _validated_journal_skill_root(skill_root)
    if not _same_journal_root(selected_skill_root, trusted_skill_root):
        raise ValueError("selected skill root does not match trusted root")
    operation = value["operation"]
    transaction_id = value["transaction_id"]
    baseline_digest = _journal_digest(
        value["baseline_status_digest"], "baseline status digest", nullable=True
    )
    transition = _journal_transition(value["status_transition"])
    capsule = _validate_capsule(value["capsule"])
    candidate = _validate_generation_ref(value["candidate"], "candidate generation")
    _validate_journal_transition(
        operation=operation,
        transaction_id=transaction_id,
        baseline_digest=baseline_digest,
        before_digest=transition.before_digest,
        after_digest=transition.after_digest,
    )
    expected = canonical_json_bytes(
        _ownership_journal_payload(
            operation=operation,
            transaction_id=transaction_id,
            selected_skill_root=selected_skill_root,
            baseline_digest=baseline_digest,
            before_digest=transition.before_digest,
            after_digest=transition.after_digest,
            capsule=capsule,
            candidate=candidate,
        )
    )
    if payload != expected:
        raise ValueError("adapter ownership journal is not canonical")
    return OwnershipJournal(
        operation=operation,
        transaction_id=transaction_id,
        selected_skill_root=selected_skill_root,
        baseline_status_digest=baseline_digest,
        transition=transition,
        capsule=capsule,
        candidate=candidate,
        staging_root=trusted_generations_root.parent
        / "staging"
        / candidate.generation_id,
        generation_root=trusted_generations_root / candidate.generation_id,
    )


def ownership_journal_skill_root(payload: bytes) -> Path:
    """Select only the direct canonical root from strict journal bytes."""
    value = _parse_ownership_journal(payload)
    return _selected_journal_skill_root(value["selected_skill_root"])


def _parse_ownership_journal(payload: bytes) -> Mapping[str, object]:
    """Parse the sole bounded, duplicate-free, canonical journal object."""
    if not isinstance(payload, bytes) or len(payload) > _MAX_JOURNAL_BYTES:
        raise ValueError("invalid adapter ownership journal")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid adapter ownership journal") from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != _OWNERSHIP_JOURNAL_FIELDS
        or value.get("format") != 1
    ):
        raise ValueError("invalid adapter ownership journal fields")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid adapter ownership journal") from exc
    if payload != canonical:
        raise ValueError("adapter ownership journal is not canonical")
    return value


def _ownership_journal_payload(
    *,
    operation: str,
    transaction_id: str,
    selected_skill_root: Path,
    baseline_digest: str | None,
    before_digest: str | None,
    after_digest: str,
    capsule: CapsuleRef,
    candidate: GenerationRef,
) -> dict[str, object]:
    return {
        "format": 1,
        "operation": operation,
        "transaction_id": transaction_id,
        "selected_skill_root": os.fspath(selected_skill_root),
        "baseline_status_digest": baseline_digest,
        "status_transition": {
            "before_digest": before_digest,
            "after_digest": after_digest,
        },
        "capsule": {
            "protocol": capsule.protocol,
            "manifest_digest": capsule.manifest_digest,
            "package_hash": capsule.package_hash,
        },
        "candidate": {
            "generation_id": candidate.generation_id,
            "manifest_digest": candidate.manifest_digest,
            "package_hash": candidate.package_hash,
            "package_version": candidate.package_version,
        },
    }


def _journal_capsule_ref(value: CapsuleRef | VersionedArtifact) -> CapsuleRef:
    if isinstance(value, VersionedArtifact):
        if value.kind != "capsule" or value.identifier != "voice-intent-normalizer":
            raise ValueError("invalid capsule reference")
        value = CapsuleRef(
            protocol=1,
            manifest_digest=value.manifest_digest,
            package_hash=value.package_hash,
        )
    return _validate_capsule(
        {
            "protocol": value.protocol,
            "manifest_digest": value.manifest_digest,
            "package_hash": value.package_hash,
        }
    )


def _journal_generation_ref(value: GenerationRef | VersionedArtifact) -> GenerationRef:
    if isinstance(value, VersionedArtifact):
        if value.kind != "generation":
            raise ValueError("invalid candidate generation")
        value = GenerationRef(
            generation_id=value.identifier,
            manifest_digest=value.manifest_digest,
            package_hash=value.package_hash,
            package_version=value.package_version,
        )
    return _validate_generation_ref(
        {
            "generation_id": value.generation_id,
            "manifest_digest": value.manifest_digest,
            "package_hash": value.package_hash,
            "package_version": value.package_version,
        },
        "candidate generation",
    )


def _status_digest(value: bytes | None, label: str) -> str | None:
    if value is None:
        return None
    return _required_status_digest(value, label)


def _required_status_digest(value: bytes, label: str) -> str:
    if not isinstance(value, bytes):
        raise ValueError(f"invalid {label}")
    return hashlib.sha256(value).hexdigest()


def _journal_digest(value: object, label: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _journal_transition(value: object) -> JournalTransition:
    if not isinstance(value, Mapping) or set(value) != {
        "before_digest",
        "after_digest",
    }:
        raise ValueError("invalid journal status transition")
    return JournalTransition(
        before_digest=_journal_digest(
            value["before_digest"], "before status digest", nullable=True
        ),
        after_digest=_journal_digest(
            value["after_digest"], "after status digest", nullable=False
        ) or "",
    )


def _validate_journal_transition(
    *,
    operation: object,
    transaction_id: object,
    baseline_digest: str | None,
    before_digest: str | None,
    after_digest: str,
) -> None:
    if (
        not isinstance(operation, str)
        or operation not in {"first-install", "upgrade"}
        or not isinstance(transaction_id, str)
        or _TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        or before_digest == after_digest
    ):
        raise ValueError("invalid ownership journal transition")
    if operation == "first-install":
        if baseline_digest is not None:
            raise ValueError("first-install journal must not retain a baseline")
    elif baseline_digest is None or before_digest is None:
        raise ValueError("upgrade journal requires a baseline transition")


def _selected_journal_skill_root(value: object) -> Path:
    try:
        selected = status_skill_root(
            {
                "format": 5,
                "layout": "versioned-v1",
                "selected_skill_root": value,
                "capability": None,
                "capsule": None,
                "active": None,
                "previous": None,
                "transaction": None,
            }
        )
    except ValueError as exc:
        raise ValueError("invalid selected skill root") from exc
    return _validated_journal_skill_root(selected)


def _validated_journal_skill_root(value: object) -> Path:
    if not isinstance(value, Path):
        raise ValueError("invalid selected skill root")
    try:
        root = validate_state_root(value)
        _reject_journal_windows_ads(root)
        info = root.lstat()
    except (OSError, ValueError) as exc:
        raise ValueError("selected skill root must be a direct directory") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_alias(info):
        raise ValueError("selected skill root must be a direct directory")
    return root


def _validated_journal_generation_root(value: Path) -> Path:
    try:
        root = validate_state_root(value)
        _reject_journal_windows_ads(root)
        return root
    except ValueError as exc:
        raise ValueError("invalid generation root") from exc


def _reject_journal_windows_ads(root: Path) -> None:
    if os.name != "nt":
        return
    import ntpath

    _, tail = ntpath.splitdrive(os.fspath(root).replace("/", "\\"))
    if ":" in tail:
        raise ValueError("journal root must not contain an ADS")


def _same_journal_root(left: Path, right: Path) -> bool:
    if os.name == "nt":
        return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))
    return os.fspath(left) == os.fspath(right)


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

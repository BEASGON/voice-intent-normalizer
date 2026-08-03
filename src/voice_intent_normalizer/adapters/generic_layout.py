"""Private state layout for the versioned generic adapter."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from voice_intent_normalizer.adapters.generic_contract import (
    build_manifest,
    canonical_json_bytes,
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


@dataclass(frozen=True, slots=True)
class GenericLayoutPaths:
    """Resolved generic-adapter paths without filesystem mutation."""

    adapter_root: Path
    status: Path
    transaction: Path
    generations: Path
    staging: Path
    retired: Path


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
    package_version = _package_version(_read_direct_source(root, "pyproject.toml"))
    manifest = build_manifest(
        "capsule", "voice-intent-normalizer", package_version, files
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

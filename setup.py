"""Build the exact runtime skill allowlist into the Python package."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_WINDOWS_REPARSE_POINT = 0x400
_BUNDLE_FILES = (
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


def _validated_bundle_sources(repository: str | Path) -> dict[str, bytes]:
    """Read the complete explicit source inventory without following aliases."""
    root = Path(repository).absolute()
    _require_direct_directory(root, "runtime repository")
    keys: set[str] = set()
    sources: dict[str, bytes] = {}
    for relative in _BUNDLE_FILES:
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise RuntimeError("invalid runtime bundle source path")
        key = os.path.normcase(relative)
        if key in keys:
            raise RuntimeError("duplicate runtime bundle source")
        keys.add(key)
        sources[relative] = _read_direct_source(root, relative)

    expected_python = {
        relative
        for relative in _BUNDLE_FILES
        if relative.startswith("src/voice_intent_normalizer/")
        and relative.endswith(".py")
    }
    actual_python = set(_direct_python_sources(root))
    missing = expected_python - actual_python
    if missing:
        raise RuntimeError(f"missing runtime Python source: {min(missing)}")
    extra = actual_python - expected_python
    if extra:
        raise RuntimeError(f"extra runtime Python source: {min(extra)}")
    for prefix in ("agents", "assets/lexicons", "references", "scripts"):
        expected_data = {
            relative
            for relative in _BUNDLE_FILES
            if relative.startswith(f"{prefix}/")
        }
        actual_data = set(_direct_data_sources(root, prefix))
        missing_data = expected_data - actual_data
        if missing_data:
            raise RuntimeError(f"missing runtime data source: {min(missing_data)}")
        extra_data = actual_data - expected_data
        if extra_data:
            raise RuntimeError(f"extra runtime data source: {min(extra_data)}")
    return sources


def _read_direct_source(root: Path, relative: str) -> bytes:
    current = root
    parts = relative.split("/")
    for component in parts[:-1]:
        current /= component
        _require_direct_directory(current, "runtime bundle source")
    source = current / parts[-1]
    try:
        before = source.lstat()
    except OSError as exc:
        raise RuntimeError(f"missing runtime bundle source: {relative}") from exc
    if not stat.S_ISREG(before.st_mode) or _is_alias(before):
        raise RuntimeError(f"runtime bundle source contains an alias: {relative}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeError(f"runtime bundle source is unavailable: {relative}") from exc
    try:
        handle_before = os.fstat(descriptor)
        before_snapshot = _source_snapshot(handle_before, relative)
        if _source_identity(before) != _source_identity(handle_before):
            raise RuntimeError(
                f"runtime bundle source changed while read: {relative}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
        handle_after = os.fstat(descriptor)
        after_snapshot = _source_snapshot(handle_after, relative)
        after = source.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"runtime bundle source is unavailable: {relative}"
        ) from exc
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(handle_before.st_mode) or not stat.S_ISREG(
        handle_after.st_mode
    ):
        raise RuntimeError(f"runtime bundle source contains an alias: {relative}")
    if _is_alias(after) or not stat.S_ISREG(after.st_mode):
        raise RuntimeError(f"runtime bundle source contains an alias: {relative}")
    if (
        before_snapshot != after_snapshot
        or len(data) != handle_before.st_size
        or _source_identity(after) != _source_identity(handle_after)
    ):
        raise RuntimeError(f"runtime bundle source changed while read: {relative}")
    return data


def _source_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _source_snapshot(info: os.stat_result, relative: str) -> tuple[int, ...]:
    names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    values = tuple(getattr(info, name, None) for name in names)
    if (
        any(not isinstance(value, int) or value < 0 for value in values)
        or values[1] == 0
    ):
        raise RuntimeError(
            f"runtime bundle source has no stable change indicator: {relative}"
        )
    return values


def _direct_python_sources(repository: Path) -> tuple[str, ...]:
    package = repository / "src" / "voice_intent_normalizer"
    _require_direct_directory(package, "runtime Python package")
    pending = [package]
    result: list[str] = []
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("runtime Python package is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError("runtime Python source is unavailable") from exc
            if _is_alias(info):
                raise RuntimeError("runtime Python package contains an alias")
            if stat.S_ISDIR(info.st_mode):
                if path.name != "__pycache__":
                    pending.append(path)
                continue
            if path.suffix == ".py":
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("runtime Python package contains an alias")
                result.append(path.relative_to(repository).as_posix())
    return tuple(sorted(result))


def _direct_data_sources(repository: Path, relative_root: str) -> tuple[str, ...]:
    root = repository / Path(relative_root)
    _require_direct_directory(root, "runtime data directory")
    pending = [root]
    result: list[str] = []
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("runtime data directory is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError("runtime data source is unavailable") from exc
            if _is_alias(info):
                raise RuntimeError("runtime data directory contains an alias")
            if stat.S_ISDIR(info.st_mode):
                if path.name != "__pycache__":
                    pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("runtime data directory contains an alias")
            result.append(path.relative_to(repository).as_posix())
    return tuple(sorted(result))


def _clear_generated_bundle(package_root: Path) -> Path:
    bundle = package_root / "_skill_bundle"
    if bundle.parent != package_root or bundle.name != "_skill_bundle":
        raise RuntimeError("refusing to clear an unsafe generated bundle path")
    try:
        info = bundle.lstat()
    except FileNotFoundError:
        return bundle
    except OSError as exc:
        raise RuntimeError("generated runtime bundle is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_alias(info):
        raise RuntimeError("generated runtime bundle contains an alias")
    try:
        if bundle.resolve(strict=True).parent != package_root.resolve(strict=True):
            raise RuntimeError("refusing to clear an unsafe generated bundle path")
    except OSError as exc:
        raise RuntimeError("generated runtime bundle is unavailable") from exc
    shutil.rmtree(bundle)
    return bundle


def _validate_generated_bundle(bundle: Path, expected: dict[str, bytes]) -> None:
    actual: dict[str, bytes] = {}
    pending = [bundle]
    while pending:
        directory = pending.pop()
        _require_direct_directory(directory, "generated runtime bundle")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("generated runtime bundle is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError("generated runtime bundle is unavailable") from exc
            if _is_alias(info):
                raise RuntimeError("generated runtime bundle contains an alias")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("generated runtime bundle contains an alias")
            actual[path.relative_to(bundle).as_posix()] = path.read_bytes()
    if actual.keys() != expected.keys():
        missing = expected.keys() - actual.keys()
        extra = actual.keys() - expected.keys()
        label = min(missing or extra)
        kind = "missing" if missing else "extra"
        raise RuntimeError(f"{kind} generated runtime bundle file: {label}")
    if actual != expected:
        raise RuntimeError("generated runtime bundle content does not match source")


def _require_direct_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_alias(info):
        raise RuntimeError(f"{label} contains an alias")


def _is_alias(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


class _BuildPyWithSkillBundle(_build_py):
    def run(self) -> None:
        repository = Path(__file__).resolve().parent
        sources = _validated_bundle_sources(repository)
        super().run()
        package_root = (Path(self.build_lib) / "voice_intent_normalizer").resolve()
        bundle = _clear_generated_bundle(package_root)
        for relative, data in sources.items():
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        _validate_generated_bundle(bundle, sources)


setup(cmdclass={"build_py": _BuildPyWithSkillBundle})

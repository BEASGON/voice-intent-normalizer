"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import importlib.machinery
import ntpath
import os
import posixpath
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal


def main() -> int:
    repository = Path(__file__).resolve(strict=True).parents[1]
    source = _trusted_source(repository / "src")
    _prioritize_source(source)
    expected_cli = _preflight_package(source)
    with _bytecode_isolation():
        _clear_preloaded_package()
        from voice_intent_normalizer import cli

        if (
            cli.__name__ != "voice_intent_normalizer.cli"
            or not _same_native_file(cli.__file__, expected_cli)
            or not _same_native_file(
                None if cli.__spec__ is None else cli.__spec__.origin,
                expected_cli,
            )
        ):
            raise RuntimeError("trusted repository CLI could not be imported")

        return cli.main()


def _prioritize_source(source: Path) -> None:
    trusted = _canonical_path(source)
    sys.path[:] = [
        entry for entry in sys.path if _canonical_path(entry) != trusted
    ]
    sys.path.insert(0, str(source.resolve()))


def _trusted_source(source: Path) -> Path:
    _require_direct_directory(source)
    resolved = source.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError("trusted repository source is unavailable")
    return resolved


def _preflight_package(source: Path) -> Path:
    package = source / "voice_intent_normalizer"
    _require_direct_directory(package)
    _require_package_marker(package, "__init__.py")
    expected_cli = _require_package_marker(package, "cli.py")
    pending = [(package, 0)]
    directories = 0
    python_files = 0
    while pending:
        directory, depth = pending.pop()
        directories += 1
        if directories > 32 or depth > 8:
            raise RuntimeError("trusted repository package is too large")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("trusted repository package is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError("trusted repository package is unavailable") from exc
            if _is_reparse_point(info) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("trusted repository package contains an alias")
            if stat.S_ISDIR(info.st_mode):
                if path.name != "__pycache__":
                    pending.append((path, depth + 1))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("trusted repository package contains an alias")
            if path.suffix in {".pyc", ".pyo"} or any(
                path.name.endswith(suffix)
                for suffix in importlib.machinery.EXTENSION_SUFFIXES
            ):
                raise RuntimeError("trusted repository package contains an alias")
            if path.suffix != ".py":
                continue
            _require_direct_regular_file(path, info)
            if not _native_is_below(path, package):
                raise RuntimeError("trusted repository package contains an alias")
            python_files += 1
            if python_files > 64:
                raise RuntimeError("trusted repository package is too large")
    return expected_cli


def _require_package_marker(package: Path, name: str) -> Path:
    marker = package / name
    try:
        info = marker.lstat()
    except OSError as exc:
        raise RuntimeError("trusted repository package is unavailable") from exc
    _require_direct_regular_file(marker, info)
    try:
        resolved = marker.resolve(strict=True)
        trusted_package = package.resolve(strict=True)
        resolved.relative_to(trusted_package)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("trusted repository package contains an alias") from exc
    return resolved


def _require_direct_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("trusted repository source is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise RuntimeError("trusted repository source must be a direct directory")


def _require_direct_regular_file(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
        raise RuntimeError("trusted repository package contains an alias")


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _canonical_path(value: str | Path) -> str:
    resolved = str(Path(value).expanduser().resolve())
    return resolved.casefold() if sys.platform == "win32" else resolved


def _clear_preloaded_package() -> None:
    for name in tuple(sys.modules):
        if name == "voice_intent_normalizer" or name.startswith(
            "voice_intent_normalizer."
        ):
            del sys.modules[name]


@contextmanager
def _bytecode_isolation():
    original_prefix = sys.pycache_prefix
    original_dont_write_bytecode = sys.dont_write_bytecode
    try:
        cache = Path(tempfile.mkdtemp(prefix="voice-intent-bootstrap-"))
        os.rmdir(cache)
        if cache.exists():
            raise OSError("private cache path still exists")
    except Exception:
        raise RuntimeError("trusted repository import isolation unavailable") from None

    try:
        sys.pycache_prefix = str(cache)
        sys.dont_write_bytecode = True
        yield cache
    finally:
        sys.pycache_prefix = original_prefix
        sys.dont_write_bytecode = original_dont_write_bytecode


def _is_below(
    value: str | None,
    root: str | Path,
    *,
    flavor: Literal["posix", "windows"] | None = None,
) -> bool:
    if value is None:
        return False
    selected = flavor or ("windows" if sys.platform == "win32" else "posix")
    if selected == "windows":
        candidate = _windows_path(value)
        trusted = _windows_path(root)
    else:
        candidate = PurePosixPath(posixpath.normpath(str(value)))
        trusted = PurePosixPath(posixpath.normpath(str(root)))
    try:
        candidate.relative_to(trusted)
    except ValueError:
        return False
    return candidate.is_absolute() and trusted.is_absolute()


def _native_is_below(value: str | None, root: Path) -> bool:
    if value is None:
        return False
    try:
        candidate = Path(value).resolve(strict=True)
        trusted = root.resolve(strict=True)
        candidate.relative_to(trusted)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _same_native_file(value: str | None, expected: Path) -> bool:
    if value is None:
        return False
    try:
        candidate = Path(value).resolve(strict=True)
        trusted = expected.resolve(strict=True)
        return os.path.samefile(candidate, trusted)
    except (OSError, RuntimeError, ValueError):
        return False


def _windows_path(value: str | Path) -> PureWindowsPath:
    raw = str(value).replace("/", "\\")
    folded = raw.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        raw = "\\\\" + raw[8:]
    elif (
        folded.startswith("\\\\?\\")
        and len(raw) >= 6
        and raw[4].isalpha()
        and raw[5] == ":"
    ):
        raw = raw[4:]
    normalized = ntpath.normpath(raw).casefold()
    return PureWindowsPath(normalized)


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import ntpath
import os
import posixpath
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_TREE_BYTES = 64 * 1024 * 1024
_MAX_TREE_FILES = 4096
_MAX_TREE_DIRECTORIES = 4096
_MAX_TREE_DEPTH = 32
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CAPSULE_MANIFEST_FILES = frozenset(
    {
        "SKILL.md",
        "agents/openai.yaml",
        "references/correction-policy.md",
        "references/domain-packs.md",
        "references/lexicon-schema.md",
        "scripts/_voice_intent_contract.py",
        "scripts/voice_intent.py",
    }
)


def main() -> int:
    entry_script = Path(__file__)
    source = _runtime_source(entry_script)
    capsule_or_checkout = entry_script.resolve(strict=True).parents[1]
    checkout_source = capsule_or_checkout / "src"
    if _same_resolved_path(source, checkout_source):
        _prioritize_source(source)
    else:
        _isolate_installed_source(source)
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


def _runtime_source(entry_script: Path) -> Path:
    capsule_or_checkout = entry_script.resolve(strict=True).parents[1]
    checkout_src = capsule_or_checkout / "src"
    if _is_complete_direct_checkout(checkout_src):
        return _trusted_source(checkout_src)
    return _installed_generation_source(capsule_or_checkout)


def _is_complete_direct_checkout(source: Path) -> bool:
    try:
        trusted = _trusted_source(source)
        _preflight_package(trusted)
    except RuntimeError:
        return False
    return True


def _installed_generation_source(capsule: Path) -> Path:
    _require_direct_tree_path(capsule)
    state_root = _canonical_state_root()
    status_path = state_root / "adapters" / "generic" / "status.json"
    _require_direct_tree_path(status_path.parent, trusted_root=state_root)
    status_bytes = _read_direct_file(status_path, "adapter status")
    status_payload = _strict_json(status_bytes, "adapter status")
    protected_capsule_digest = _protected_capsule_digest(status_payload)

    capsule_manifest_path = capsule / "capsule.json"
    capsule_bytes = _read_direct_file(capsule_manifest_path, "capsule manifest")
    if hashlib.sha256(capsule_bytes).hexdigest() != protected_capsule_digest:
        raise RuntimeError("installed capsule is not anchored by adapter status")
    capsule_payload = _strict_json(capsule_bytes, "capsule manifest")
    helper_digest = _protected_helper_digest(capsule_payload)
    _validate_manifest_tree(
        capsule,
        capsule_payload,
        manifest_name="capsule.json",
        label="installed capsule",
    )
    helper_path = capsule / "scripts" / "_voice_intent_contract.py"
    helper_bytes = _read_direct_file(helper_path, "capsule contract")
    if hashlib.sha256(helper_bytes).hexdigest() != helper_digest:
        raise RuntimeError("installed capsule contract is not anchored")
    contract = _load_contract(helper_path, protected_capsule_digest)

    try:
        capsule_manifest = contract.validate_manifest(capsule_bytes)
        if contract.canonical_json_bytes(capsule_manifest) != capsule_bytes:
            raise ValueError("capsule manifest is not canonical")
        status = contract.validate_status_v5(
            status_bytes,
            skill_root=capsule.parent,
            generations_root=(
                state_root / "adapters" / "generic" / "generations"
            ),
        )
        capsule_digest = contract.manifest_digest(capsule_manifest)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("installed capsule metadata is invalid") from exc
    if (
        capsule_manifest["kind"] != "capsule"
        or capsule_digest != status.capsule.manifest_digest
        or capsule_manifest["package_hash"] != status.capsule.package_hash
        or not _same_native_file(capsule, status.capsule_root)
    ):
        raise RuntimeError("installed capsule metadata is not anchored")

    generation = status.active_root
    _require_direct_tree_path(generation, trusted_root=state_root)
    generation_manifest_path = generation / "generation.json"
    generation_bytes = _read_direct_file(
        generation_manifest_path, "generation manifest"
    )
    try:
        generation_manifest = contract.validate_manifest(generation_bytes)
        if contract.canonical_json_bytes(generation_manifest) != generation_bytes:
            raise ValueError("generation manifest is not canonical")
        generation_digest = contract.manifest_digest(generation_manifest)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("installed generation metadata is invalid") from exc
    if (
        generation_manifest["kind"] != "generation"
        or generation_manifest["identifier"] != status.active.generation_id
        or generation_manifest["package_hash"] != status.active.package_hash
        or generation_manifest["package_version"]
        != status.active.package_version
        or generation_digest != status.active.manifest_digest
    ):
        raise RuntimeError("installed generation metadata is not anchored")
    _validate_manifest_tree(
        generation,
        generation_manifest,
        manifest_name="generation.json",
        label="installed generation",
    )
    return _trusted_source(generation / "src")


def _canonical_state_root() -> Path:
    configured = os.environ.get("VOICE_INTENT_HOME", "")
    supplied = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".voice-intent-normalizer"
    )
    raw = os.fspath(supplied)
    if not supplied.is_absolute() or "\x00" in raw:
        raise RuntimeError("state root must be a canonical local path")
    if os.name == "nt":
        folded = raw.casefold()
        drive, tail = ntpath.splitdrive(raw)
        if (
            not drive
            or not tail.startswith("\\")
            or folded.startswith(("\\\\", "\\\\?\\", "\\??\\"))
            or ":" in tail
            or ntpath.normcase(ntpath.normpath(raw))
            != ntpath.normcase(raw)
            or _windows_drive_type(f"{drive}\\") != 3
        ):
            raise RuntimeError("state root must be a canonical local path")
    elif posixpath.normpath(raw) != raw or not raw.startswith("/"):
        raise RuntimeError("state root must be a canonical local path")
    root = Path(raw)
    _require_direct_existing_components(root)
    try:
        resolved = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("state root must be a canonical local path") from exc
    if _canonical_path(resolved) != _canonical_path(root):
        raise RuntimeError("state root must be a canonical local path")
    return root


def _windows_drive_type(root: str) -> int:
    if os.name != "nt":
        return 3
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        return int(kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError):
        return 0


def _require_direct_existing_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError("trusted path is unavailable") from exc
        if _is_reparse_point(info) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("trusted path contains an alias")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("trusted path is unavailable")


def _require_direct_tree_path(
    path: Path, *, trusted_root: Path | None = None
) -> None:
    candidate = Path(path)
    if trusted_root is None:
        start = Path(candidate.anchor)
        parts = candidate.parts[1:]
    else:
        try:
            parts = candidate.relative_to(trusted_root).parts
        except ValueError as exc:
            raise RuntimeError("trusted path escaped its root") from exc
        start = trusted_root
        _require_direct_directory(start)
    current = start
    for component in parts:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimeError("trusted directory is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
        ):
            raise RuntimeError("trusted directory contains an alias")


def _read_direct_file(path: Path, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or _is_reparse_point(before)
        or before.st_size > _MAX_METADATA_BYTES
    ):
        raise RuntimeError(f"{label} must be a bounded direct file")
    try:
        data = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if (
        len(data) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or _is_reparse_point(after)
    ):
        raise RuntimeError(f"{label} changed while it was read")
    return data


def _strict_json(payload: bytes, label: str):
    if len(payload) > _MAX_METADATA_BYTES:
        raise RuntimeError(f"{label} exceeds size limit")
    try:
        return json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _protected_capsule_digest(payload) -> str:
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "layout",
        "selected_skill_root",
        "capability",
        "capsule",
        "active",
        "previous",
        "transaction",
    }:
        raise RuntimeError("adapter status is invalid")
    if (
        type(payload.get("format")) is not int
        or payload.get("format") != 5
        or payload.get("layout") != "versioned-v1"
    ):
        raise RuntimeError("adapter status selector is unsupported")
    capsule = payload.get("capsule")
    if not isinstance(capsule, dict) or set(capsule) != {
        "protocol",
        "manifest_digest",
        "package_hash",
    }:
        raise RuntimeError("adapter status capsule reference is invalid")
    if (
        type(capsule.get("protocol")) is not int
        or capsule.get("protocol") != 1
    ):
        raise RuntimeError("adapter status capsule protocol is unsupported")
    digest = capsule.get("manifest_digest")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("adapter status capsule reference is invalid")
    return digest


def _protected_helper_digest(payload) -> str:
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "kind",
        "identifier",
        "package_version",
        "package_hash",
        "files",
        "file_hashes",
    }:
        raise RuntimeError("capsule manifest is invalid")
    files = payload.get("files")
    hashes = payload.get("file_hashes")
    if (
        payload.get("format") != 1
        or type(payload.get("format")) is not int
        or payload.get("kind") != "capsule"
        or payload.get("identifier") != "voice-intent-normalizer"
        or not isinstance(files, list)
        or frozenset(files) != _CAPSULE_MANIFEST_FILES
        or len(files) != len(_CAPSULE_MANIFEST_FILES)
        or not isinstance(hashes, dict)
        or set(hashes) != _CAPSULE_MANIFEST_FILES
    ):
        raise RuntimeError("capsule manifest is invalid")
    digest = hashes.get("scripts/_voice_intent_contract.py")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("capsule contract digest is invalid")
    return digest


def _validate_manifest_tree(
    root: Path, manifest, *, manifest_name: str, label: str
) -> None:
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    hashes = manifest.get("file_hashes") if isinstance(manifest, Mapping) else None
    if (
        not isinstance(files, (list, tuple))
        or len(files) > _MAX_TREE_FILES
        or not isinstance(hashes, Mapping)
        or set(files) != set(hashes)
        or len(files) != len(set(files))
    ):
        raise RuntimeError(f"{label} manifest file table is invalid")
    expected = set(files)
    expected_with_manifest = expected | {manifest_name}
    actual: set[str] = set()
    pending = [(root, 0)]
    directories = 0
    while pending:
        directory, depth = pending.pop()
        directories += 1
        if directories > _MAX_TREE_DIRECTORIES or depth > _MAX_TREE_DEPTH:
            raise RuntimeError(f"{label} tree exceeds limits")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"{label} tree is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError(f"{label} tree is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise RuntimeError(f"{label} tree contains an alias")
            if stat.S_ISDIR(info.st_mode):
                if path.name == "__pycache__":
                    raise RuntimeError(f"{label} tree contains bytecode")
                pending.append((path, depth + 1))
                continue
            if not stat.S_ISREG(info.st_mode) or _is_loader_artifact(path.name):
                raise RuntimeError(f"{label} tree contains a loader artifact")
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            if len(actual) > _MAX_TREE_FILES + 1:
                raise RuntimeError(f"{label} tree exceeds limits")
    if actual != expected_with_manifest:
        raise RuntimeError(f"{label} tree does not match its manifest")
    total = 0
    for relative in files:
        if not isinstance(relative, str):
            raise RuntimeError(f"{label} manifest path is invalid")
        data = _read_direct_file(root / relative, f"{label} file")
        total += len(data)
        if total > _MAX_TREE_BYTES:
            raise RuntimeError(f"{label} tree exceeds limits")
        digest = hashes.get(relative)
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise RuntimeError(f"{label} file hash does not match")


def _load_contract(path: Path, capsule_digest: str):
    module_name = f"_voice_intent_contract_{capsule_digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("capsule contract cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    prior = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _bytecode_isolation():
            spec.loader.exec_module(module)
    finally:
        if prior is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = prior
    if (
        not _same_native_file(getattr(module, "__file__", None), path)
        or not _same_native_file(
            None if module.__spec__ is None else module.__spec__.origin, path
        )
    ):
        raise RuntimeError("capsule contract origin is invalid")
    return module


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return _canonical_path(left) == _canonical_path(right)
    except (OSError, RuntimeError):
        return False


def _isolate_installed_source(source: Path) -> None:
    ambient_roots = {
        _canonical_path(entry)
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry
    }
    cwd = _canonical_path(Path.cwd())
    retained: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        canonical = _canonical_path(entry)
        if canonical == cwd or canonical in ambient_roots:
            continue
        retained.append(entry)
    trusted = _canonical_path(source)
    sys.path[:] = [entry for entry in retained if _canonical_path(entry) != trusted]
    sys.path.insert(0, str(source.resolve(strict=True)))
    _clear_ambient_modules(ambient_roots)


def _clear_ambient_modules(ambient_roots: set[str]) -> None:
    if not ambient_roots:
        return
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        if any(_canonical_is_below(origin, root) for root in ambient_roots):
            del sys.modules[name]


def _canonical_is_below(value: str | Path, root: str | Path) -> bool:
    try:
        candidate = Path(value).resolve(strict=False)
        trusted = Path(root).resolve(strict=False)
        candidate.relative_to(trusted)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


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
            if _is_loader_artifact(path.name):
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


def _is_loader_artifact(
    name: str,
    *,
    flavor: Literal["posix", "windows"] | None = None,
) -> bool:
    selected = flavor or ("windows" if sys.platform == "win32" else "posix")
    normalizer = ntpath.normcase if selected == "windows" else posixpath.normcase
    candidate = normalizer(name)
    suffixes = (".pyc", ".pyo", *importlib.machinery.EXTENSION_SUFFIXES)
    return any(candidate.endswith(normalizer(suffix)) for suffix in suffixes)


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

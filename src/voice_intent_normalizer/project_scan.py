"""Bounded, local-only extraction of project vocabulary candidates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from .lexicon import _entry_data, load_jsonl_bytes
from .models import EntryStatus, LexiconEntry, Scope
from .paths import (
    ProjectRootAuthority,
    ProjectRootValidationError,
    StatePaths,
    StateRootLease,
    duplicate_project_root_descriptor,
    guard_project_root,
    guard_state_root,
)

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "dist",
        "build",
        "coverage",
        "__pycache__",
    }
)
_CREDENTIAL_NAME_PARTS = frozenset(
    {
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "id_rsa",
    }
)
_SSH_PRIVATE_KEY_NAMES = frozenset(
    {
        "id_dsa",
        "id_ecdsa",
        "id_ecdsa_sk",
        "id_ed25519",
        "id_ed25519_sk",
        "id_rsa",
        "id_xmss",
    }
)
_TEXT_EXTENSIONS = frozenset(
    {
        "",
        ".bat",
        ".c",
        ".cc",
        ".cfg",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".dart",
        ".ex",
        ".exs",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".mjs",
        ".php",
        ".properties",
        ".ps1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_CAMEL_CASE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)+\b"
)
_SNAKE_CASE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_QUOTED_CHINESE = re.compile(r"[“\"「『]([\u4e00-\u9fff]{2,20})[”\"」』]")
_NAMED_CHINESE = re.compile(
    r"(?:产品名是|产品名称是|名称是|名为|叫做)[：:\s]*([\u4e00-\u9fff]{2,20})(?=[，。；;、\s]|$)"
)
_SCAN_STATE_VERSION = 4
_MAX_SCAN_STATE_BYTES = 64 * 1024
_MAX_SCAN_CACHE_BYTES = 10 * 1024 * 1024
_MAX_SCAN_FILES = 5_000
_MAX_SCAN_TEXT_BYTES = 2_000_000
_MAX_DIRECTORY_ENTRIES = 4_096
_MAX_SCAN_ENTRIES = 50_000
_SCAN_ATTEMPTS = 2
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\Z", re.ASCII)
_WINDOWS_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class ScanResult:
    """The bounded scan output and the candidate-file work consumed by it."""

    entries: tuple[LexiconEntry, ...]
    truncated: bool
    files_scanned: int
    text_bytes_scanned: int


@dataclass(frozen=True, slots=True)
class _ProjectMetadata:
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _ProjectSource:
    source: str
    name: str
    is_directory: bool
    metadata: _ProjectMetadata
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class _ProjectSnapshot:
    sources: tuple[_ProjectSource, ...]
    truncated: bool
    files_scanned: int
    text_bytes_scanned: int


@dataclass(slots=True)
class _RetainedProjectHandle:
    path: Path
    relative: Path
    descriptor: int
    is_directory: bool
    metadata: _ProjectMetadata
    identity: tuple[int, int]
    root_key: str | None = None


def _is_private_name(name: str) -> bool:
    """Return whether *name* is hidden or conventionally stores credentials."""
    lowered = name.casefold()
    return (
        lowered.startswith(".")
        or lowered in _SSH_PRIVATE_KEY_NAMES
        or any(part in lowered for part in _CREDENTIAL_NAME_PARTS)
    )


def _is_allowed_text_file(path: Path) -> bool:
    """Limit content checks to known text-like filenames."""
    return (
        not _is_private_name(path.name) and path.suffix.casefold() in _TEXT_EXTENSIONS
    )


def _add_stem(
    observations: dict[str, Counter[tuple[str, str]]], stem: str, source: str, kind: str
) -> None:
    normalized = stem.strip()
    if len(normalized) >= 2 and not _is_private_name(normalized):
        observations[normalized][(source, kind)] += 1


def _extract_text_terms(
    content: str,
    source: str,
    observations: dict[str, Counter[tuple[str, str]]],
) -> None:
    for line in content.splitlines():
        if heading := _MARKDOWN_HEADING.match(line):
            candidate = heading.group(1).strip()
            if 2 <= len(candidate) <= 20:
                _add_stem(observations, candidate, source, "markdown-heading")
    for pattern, kind in (
        (_CAMEL_CASE, "camel-case"),
        (_SNAKE_CASE, "snake-case"),
        (_QUOTED_CHINESE, "quoted-chinese"),
        (_NAMED_CHINESE, "named-chinese"),
    ):
        for match in pattern.finditer(content):
            term = match.group(1) if match.lastindex else match.group()
            _add_stem(observations, term, source, kind)


def _entry_weight(frequency: int) -> float:
    """Map repeated local observations to a bounded candidate confidence."""
    return min(1.0, 0.5 + (frequency - 1) * 0.1)


def _entries_from_observations(
    observations: dict[str, Counter[tuple[str, str]]], project_id: str
) -> tuple[LexiconEntry, ...]:
    entries: list[LexiconEntry] = []
    for canonical in sorted(observations, key=lambda value: (value.casefold(), value)):
        locations = observations[canonical]
        source, kind = min(
            locations,
            key=lambda item: (-locations[item], item[0], item[1]),
        )
        frequency = sum(locations.values())
        entries.append(
            LexiconEntry(
                canonical=canonical,
                scope=Scope.PROJECT,
                aliases=(canonical,),
                domains=(),
                weight=_entry_weight(frequency),
                status=(
                    EntryStatus.REPEATED if frequency > 1 else EntryStatus.CANDIDATE
                ),
                project_id=project_id,
                source=source,
                use_count=frequency,
                notes=kind,
            )
        )
    return tuple(entries)


def scan_project(
    root: str | Path | ProjectRootAuthority,
    state_paths: StatePaths,
    max_files: int = 5_000,
    max_text_bytes: int = 2_000_000,
) -> ScanResult:
    """Create a project-local candidate lexicon without retaining source text.

    The scanner deliberately reads only known text extensions, never follows
    symlinks, and treats unreadable files as absent.  Reaching either supplied
    limit produces the partial cache and marks the result as truncated.
    """
    _validate_scan_limits(max_files, max_text_bytes)

    try:
        authority_context, project_authority, root_authority = (
            _acquire_project_scan_root(root)
        )
    except (OSError, ValueError):
        return ScanResult((), False, 0, 0)
    try:
        try:
            project_paths = state_paths.for_project(project_authority)
        except (OSError, ValueError):
            return ScanResult((), False, 0, 0)
        # A source can change while it is read. Only publish a cache after a
        # matching bounded metadata snapshot, otherwise retry and leave old
        # state stale. Every snapshot is duplicated from the same retained root
        # authority; the lexical project path is never reacquired.
        for _attempt in range(_SCAN_ATTEMPTS):
            before = _project_fingerprint_retained(
                root_authority, max_files=max_files
            )
            result = _scan_once_retained(
                root_authority,
                project_paths.project_id,
                max_files,
                max_text_bytes,
            )
            after = _project_fingerprint_retained(
                root_authority, max_files=max_files
            )
            if before != after:
                continue
            cache, cached_entries, output_truncated = _serialize_entries(
                result.entries
            )
            result = replace(
                result,
                entries=cached_entries,
                truncated=result.truncated or output_truncated,
            )
            relative_dir = Path("projects") / project_paths.project_id
            with _project_publication_lock(
                state_paths, project_paths.project_id
            ) as lease:
                cache_relative = relative_dir / "project-scan.jsonl"
                state_relative = relative_dir / "scan-state.json"
                if not lease.exists(cache_relative):
                    _remove_pre_release_scan_entries(
                        lease, relative_dir, project_paths.project_id
                    )
                lease.write_bytes_atomic(cache_relative, cache)
                # Re-read only after the cross-process lease is held. Every
                # successful publication therefore consumes a unique generation.
                generation = _next_generation(lease, state_relative)
                lease.write_bytes_atomic(
                    state_relative,
                    _scan_state_bytes(
                        after,
                        cache,
                        max_files,
                        max_text_bytes,
                        generation,
                        result.truncated,
                        project_paths.project_id,
                    ),
                )
            return result
        raise RuntimeError("project changed during bounded scan")
    finally:
        _close_retained_project_handle(root_authority)
        authority_context.__exit__(None, None, None)


def project_cache_is_stale(
    root: str | Path | ProjectRootAuthority,
    state_paths: StatePaths,
    max_files: int = 5_000,
    max_text_bytes: int = 2_000_000,
    *,
    diagnostics: list[str] | None = None,
) -> bool:
    """Return whether the bounded scanner cache needs a safe refresh."""
    _validate_scan_limits(max_files, max_text_bytes)
    try:
        authority_context, project_authority, root_authority = (
            _acquire_project_scan_root(root)
        )
    except (OSError, ValueError):
        _append_project_root_diagnostic(diagnostics)
        return False
    try:
        project_paths = state_paths.for_project(project_authority)
        relative_dir = Path("projects") / project_paths.project_id
        with guard_state_root(state_paths.root, retained_dirs=(relative_dir,)) as lease:
            cache_relative = relative_dir / "project-scan.jsonl"
            state_relative = relative_dir / "scan-state.json"
            if not lease.root_exists:
                return True
            cache_exists = lease.exists(cache_relative)
            state_exists = lease.exists(state_relative)
            if not cache_exists and not state_exists:
                return True
            if cache_exists != state_exists:
                _append_scan_diagnostic(diagnostics)
                return True
            cache = lease.read_bytes(
                cache_relative, _MAX_SCAN_CACHE_BYTES, "scan cache"
            )
            raw = json.loads(
                lease.read_bytes(state_relative, _MAX_SCAN_STATE_BYTES, "scan state")
            )
        if not _valid_scan_state(raw, project_paths.project_id):
            _append_scan_diagnostic(diagnostics)
            return True
        if (
            raw["max_files"] != max_files
            or raw["max_text_bytes"] != max_text_bytes
        ):
            _append_scan_diagnostic(diagnostics)
            return True
        if hashlib.sha256(cache).hexdigest() != raw["cache_sha256"]:
            _append_scan_diagnostic(diagnostics)
            return True
        return raw["fingerprint"] != _project_fingerprint_retained(
            root_authority, max_files=max_files
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _append_scan_diagnostic(diagnostics)
        return True
    finally:
        _close_retained_project_handle(root_authority)
        authority_context.__exit__(None, None, None)


def load_project_scan_entries(
    lease: StateRootLease,
    project_authority: ProjectRootAuthority,
    project_id: str,
) -> tuple[tuple[LexiconEntry, ...], str | None]:
    """Resolve one hash-coherent scanner cache through the caller's lease."""
    relative_dir = Path("projects") / project_id
    cache_relative = relative_dir / "project-scan.jsonl"
    state_relative = relative_dir / "scan-state.json"
    try:
        if not lease.available(relative_dir):
            return (), None
        cache_exists = lease.exists(cache_relative)
        state_exists = lease.exists(state_relative)
        if not cache_exists and not state_exists:
            return (), None
        if cache_exists != state_exists:
            return (), "project_scan_invalid"
        cache = lease.read_bytes(cache_relative, _MAX_SCAN_CACHE_BYTES, "scan cache")
        raw = json.loads(
            lease.read_bytes(state_relative, _MAX_SCAN_STATE_BYTES, "scan state")
        )
        if not _valid_scan_state(raw, project_id):
            return (), "project_scan_invalid"
        if hashlib.sha256(cache).hexdigest() != raw["cache_sha256"]:
            return (), "project_scan_invalid"
        entries = load_jsonl_bytes(
            cache, cache_relative, expected_scope=Scope.PROJECT
        )
        if any(entry.project_id != project_id for entry in entries):
            return (), "project_scan_invalid"
        if project_authority.project_id != project_id:
            return (), "project_scan_invalid"
        try:
            root_authority = _retained_project_root(project_authority)
        except (OSError, ValueError):
            return (), "project_scan_invalid"
        try:
            stale = raw["fingerprint"] != _project_fingerprint_retained(
                root_authority, max_files=raw["max_files"]
            )
        finally:
            _close_retained_project_handle(root_authority)
        return entries, "project_scan_stale" if stale else None
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return (), "project_scan_invalid"


def _valid_scan_state(raw: object, project_id: str | None = None) -> bool:
    """Validate the complete cache transaction contract before using bytes."""
    return (
        isinstance(raw, dict)
        and set(raw)
        == {
            "fingerprint",
            "cache_sha256",
            "generation",
            "max_files",
            "max_text_bytes",
            "schema_version",
            "truncated",
            "project_id",
        }
        and raw["schema_version"] == _SCAN_STATE_VERSION
        and type(raw["schema_version"]) is int
        and isinstance(raw["fingerprint"], str)
        and _SHA256.fullmatch(raw["fingerprint"]) is not None
        and isinstance(raw["cache_sha256"], str)
        and _SHA256.fullmatch(raw["cache_sha256"]) is not None
        and type(raw["generation"]) is int
        and raw["generation"] >= 0
        and type(raw["max_files"]) is int
        and 0 < raw["max_files"] <= _MAX_SCAN_FILES
        and type(raw["max_text_bytes"]) is int
        and 0 < raw["max_text_bytes"] <= _MAX_SCAN_TEXT_BYTES
        and type(raw["truncated"]) is bool
        and isinstance(raw["project_id"], str)
        and _PROJECT_ID.fullmatch(raw["project_id"]) is not None
        and (project_id is None or raw["project_id"] == project_id)
    )


def _validate_scan_limits(max_files: int, max_text_bytes: int) -> None:
    """Reject caller or metadata limits outside immutable scanner hard caps."""
    if (
        type(max_files) is not int
        or not 0 < max_files <= _MAX_SCAN_FILES
        or type(max_text_bytes) is not int
        or not 0 < max_text_bytes <= _MAX_SCAN_TEXT_BYTES
    ):
        raise ValueError("scan limits must be positive integers within hard caps")


def _append_scan_diagnostic(diagnostics: list[str] | None) -> None:
    if diagnostics is not None and "project_scan_invalid" not in diagnostics:
        diagnostics.append("project_scan_invalid")


def _append_project_root_diagnostic(diagnostics: list[str] | None) -> None:
    if diagnostics is not None and "project_root_invalid" not in diagnostics:
        diagnostics.append("project_root_invalid")


def _project_fingerprint(
    root: str | Path | ProjectRootAuthority,
    *,
    max_files: int,
) -> str:
    """Hash bounded metadata only; source content never enters state."""
    try:
        authority_context, _project_authority, root_authority = (
            _acquire_project_scan_root(root)
        )
    except (OSError, ValueError):
        return _fingerprint_snapshot(_ProjectSnapshot((), False, 0, 0))
    try:
        return _project_fingerprint_retained(
            root_authority, max_files=max_files
        )
    finally:
        _close_retained_project_handle(root_authority)
        authority_context.__exit__(None, None, None)


def _project_fingerprint_retained(
    root_authority: _RetainedProjectHandle,
    *,
    max_files: int,
) -> str:
    snapshot = _secure_project_snapshot(root_authority, max_files, None)
    return _fingerprint_snapshot(snapshot)


def _fingerprint_snapshot(snapshot: _ProjectSnapshot) -> str:
    records = [
        (
            source.source + ("/" if source.is_directory else ""),
            source.metadata.size,
            source.metadata.mtime_ns,
        )
        for source in snapshot.sources
    ]
    payload = json.dumps(
        {"records": records, "truncated": snapshot.truncated},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scan_once(
    root: str | Path | ProjectRootAuthority,
    project_id: str,
    max_files: int,
    max_text_bytes: int,
) -> ScanResult:
    try:
        authority_context, _project_authority, root_authority = (
            _acquire_project_scan_root(root)
        )
    except (OSError, ValueError):
        return ScanResult((), False, 0, 0)
    try:
        return _scan_once_retained(
            root_authority, project_id, max_files, max_text_bytes
        )
    finally:
        _close_retained_project_handle(root_authority)
        authority_context.__exit__(None, None, None)


def _scan_once_retained(
    root_authority: _RetainedProjectHandle,
    project_id: str,
    max_files: int,
    max_text_bytes: int,
) -> ScanResult:
    observations: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    snapshot = _secure_project_snapshot(
        root_authority, max_files, max_text_bytes
    )
    for source in snapshot.sources:
        if source.is_directory:
            _add_stem(
                observations,
                source.name,
                source.source,
                "directory-stem",
            )
            continue
        content_bytes = source.content
        if content_bytes is None or b"\0" in content_bytes:
            continue
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        _add_stem(
            observations,
            Path(source.name).stem,
            source.source,
            "file-stem",
        )
        _extract_text_terms(content, source.source, observations)
    return ScanResult(
        _entries_from_observations(observations, project_id),
        snapshot.truncated,
        snapshot.files_scanned,
        snapshot.text_bytes_scanned,
    )


def _secure_project_snapshot(
    root_authority: _RetainedProjectHandle,
    max_files: int,
    max_text_bytes: int | None,
) -> _ProjectSnapshot:
    """Walk a bounded project through retained no-follow directory authority."""
    try:
        root_handle = _duplicate_retained_project_handle(root_authority)
    except (OSError, ValueError):
        return _ProjectSnapshot((), False, 0, 0)
    pending = [root_handle]
    sources: list[_ProjectSource] = []
    files_selected = files_scanned = text_bytes_scanned = entries_seen = 0
    max_entries = _tree_entry_budget(max_files)
    truncated = False
    try:
        while pending and not truncated:
            directory = pending.pop()
            new_directories: list[_RetainedProjectHandle] = []
            try:
                try:
                    names, overflow = _bounded_retained_names(directory)
                except (OSError, ValueError):
                    continue
                if overflow:
                    truncated = True
                    continue
                for name in names:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        truncated = True
                        break
                    if _is_private_name(name):
                        continue
                    path = directory.path / name
                    # Preserve the public scanner's filename selection boundary
                    # before opening the entry. The retained parent remains the
                    # authority if the name changes immediately afterward.
                    allowed_text = _is_allowed_text_file(path)
                    try:
                        child = _open_retained_project_child(directory, name)
                    except (OSError, ValueError):
                        continue
                    if child is None:
                        continue
                    if child.is_directory:
                        if name.casefold() in _EXCLUDED_DIRECTORY_NAMES:
                            _close_retained_project_handle(child)
                            continue
                        new_directories.append(child)
                        sources.append(
                            _ProjectSource(
                                child.relative.as_posix(),
                                name,
                                True,
                                child.metadata,
                            )
                        )
                        continue
                    try:
                        if not allowed_text:
                            continue
                        if files_selected >= max_files:
                            truncated = True
                            break
                        content: bytes | None = None
                        if max_text_bytes is None:
                            files_selected += 1
                        else:
                            remaining = max_text_bytes - text_bytes_scanned
                            if remaining <= 0:
                                truncated = True
                                break
                            try:
                                content = _read_descriptor_limited(
                                    child.descriptor, remaining + 1
                                )
                            except (OSError, ValueError):
                                continue
                            if len(content) > remaining:
                                truncated = True
                                break
                            files_selected += 1
                            files_scanned += 1
                            text_bytes_scanned += len(content)
                        sources.append(
                            _ProjectSource(
                                child.relative.as_posix(),
                                name,
                                False,
                                child.metadata,
                                content,
                            )
                        )
                    finally:
                        _close_retained_project_handle(child)
            except BaseException:
                _close_retained_project_handles(new_directories)
                raise
            finally:
                _close_retained_project_handle(directory)
            if truncated:
                _close_retained_project_handles(new_directories)
                break
            pending.extend(reversed(new_directories))
    finally:
        _close_retained_project_handles(pending)
    return _ProjectSnapshot(
        tuple(sources),
        truncated,
        files_scanned,
        text_bytes_scanned,
    )


def _acquire_project_scan_root(
    root: str | Path | ProjectRootAuthority,
):
    """Enter shared authority and duplicate it without leaking partial opens."""
    authority_context = guard_project_root(root)
    project_authority: ProjectRootAuthority | None = None
    try:
        project_authority = authority_context.__enter__()
        retained = _retained_project_root(project_authority)
    except BaseException:
        if project_authority is not None:
            authority_context.__exit__(None, None, None)
        raise
    return authority_context, project_authority, retained


def _retained_project_root(
    authority: ProjectRootAuthority,
) -> _RetainedProjectHandle:
    descriptor = duplicate_project_root_descriptor(authority)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectRootValidationError(
                "project root authority must remain a direct directory"
            )
        return _retained_from_stat(
            authority.canonical_root,
            Path(),
            descriptor,
            True,
            info,
            root_key=authority.root_key,
            identity=authority.identity,
        )
    except Exception:
        os.close(descriptor)
        raise


def _duplicate_retained_project_handle(
    handle: _RetainedProjectHandle,
) -> _RetainedProjectHandle:
    if handle.descriptor < 0:
        raise ValueError("project root authority is closed")
    descriptor = os.dup(handle.descriptor)
    try:
        info = os.fstat(descriptor)
        is_directory = stat.S_ISDIR(info.st_mode)
        if is_directory != handle.is_directory:
            raise ValueError("duplicated project authority changed type")
        return _retained_from_stat(
            handle.path,
            handle.relative,
            descriptor,
            is_directory,
            info,
            root_key=handle.root_key,
            identity=handle.identity,
        )
    except Exception:
        os.close(descriptor)
        raise


def _open_retained_project_child(
    parent: _RetainedProjectHandle, name: str
) -> _RetainedProjectHandle | None:
    if not name or name in {".", ".."} or Path(name).name != name:
        return None
    if os.name == "nt":
        if parent.root_key is None:
            raise ValueError("Windows project root key is missing")
        return _open_windows_project_path(
            parent.path / name,
            parent.relative / name,
            parent.root_key,
        )
    return _open_posix_project_child(parent, name)


def _open_posix_project_child(
    parent: _RetainedProjectHandle, name: str
) -> _RetainedProjectHandle | None:
    try:
        initial = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError:
        return None
    if stat.S_ISLNK(initial.st_mode):
        return None
    if stat.S_ISDIR(initial.st_mode):
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        expected_directory = True
    elif stat.S_ISREG(initial.st_mode):
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
        )
        expected_directory = False
    else:
        return None
    descriptor = os.open(name, flags, dir_fd=parent.descriptor)
    try:
        info = os.fstat(descriptor)
        if expected_directory != stat.S_ISDIR(info.st_mode):
            raise ValueError("project entry type changed during retained open")
        if not expected_directory and not stat.S_ISREG(info.st_mode):
            raise ValueError("project source must be a regular file")
        return _retained_from_stat(
            parent.path / name,
            parent.relative / name,
            descriptor,
            expected_directory,
            info,
        )
    except Exception:
        os.close(descriptor)
        raise


def _retained_from_stat(
    path: Path,
    relative: Path,
    descriptor: int,
    is_directory: bool,
    info: os.stat_result,
    *,
    root_key: str | None = None,
    identity: tuple[int, int] | None = None,
) -> _RetainedProjectHandle:
    return _RetainedProjectHandle(
        path=path,
        relative=relative,
        descriptor=descriptor,
        is_directory=is_directory,
        metadata=_ProjectMetadata(info.st_size, info.st_mtime_ns),
        identity=(info.st_dev, info.st_ino) if identity is None else identity,
        root_key=root_key,
    )


def _bounded_retained_names(
    directory: _RetainedProjectHandle,
) -> tuple[list[str], bool]:
    if not directory.is_directory:
        raise ValueError("only retained directories can be enumerated")
    if os.name == "nt":
        _verify_windows_directory_binding(directory)
        scan_target: str | Path | int = directory.path
    else:
        if os.scandir not in os.supports_fd:
            raise OSError("secure POSIX traversal requires scandir(fd)")
        info = os.fstat(directory.descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != directory.identity
        ):
            raise ValueError("retained project directory identity changed")
        scan_target = directory.descriptor
    names: list[str] = []
    with os.scandir(scan_target) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > _MAX_DIRECTORY_ENTRIES:
                return [], True
    if os.name == "nt":
        _verify_windows_directory_binding(directory)
    return sorted(names, key=lambda name: (name.casefold(), name)), False


def _close_retained_project_handle(handle: _RetainedProjectHandle) -> None:
    descriptor = handle.descriptor
    if descriptor < 0:
        return
    handle.descriptor = -1
    try:
        os.close(descriptor)
    except OSError:
        return


def _close_retained_project_handles(
    handles: Iterable[_RetainedProjectHandle],
) -> None:
    for handle in handles:
        _close_retained_project_handle(handle)


def _open_windows_project_path(
    path: Path,
    relative: Path,
    root_key: str | None,
) -> _RetainedProjectHandle:
    import ctypes
    import msvcrt
    import ntpath

    from .paths import _extended_windows_path

    kernel32, attributes_type, information_type = _windows_project_api()
    ctypes.set_last_error(0)
    native_handle = kernel32.CreateFileW(
        _extended_windows_path(path),
        0x81,  # FILE_LIST_DIRECTORY/FILE_READ_DATA | FILE_READ_ATTRIBUTES
        0x3,  # share read/write but deliberately deny delete/rename sharing
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if native_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor: int | None = None
    try:
        attributes = attributes_type()
        if not kernel32.GetFileInformationByHandleEx(
            native_handle,
            9,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.file_attributes & _WINDOWS_REPARSE_POINT:
            raise ValueError("project entry must not be a Windows reparse point")
        is_directory = bool(attributes.file_attributes & 0x10)
        information = information_type()
        if not kernel32.GetFileInformationByHandle(
            native_handle, ctypes.byref(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = (
            int(information.volume_serial_number),
            int(
                (information.file_index_high << 32)
                | information.file_index_low
            ),
        )
        final_path = _windows_handle_final_path(kernel32, native_handle)
        final_key = ntpath.normcase(ntpath.normpath(final_path))
        expected_key = ntpath.normcase(ntpath.normpath(os.fspath(path)))
        if final_key != expected_key or (
            root_key is not None
            and ntpath.commonpath((root_key, final_key)) != root_key
        ):
            raise ValueError("retained project entry escaped its lexical path")
        descriptor = msvcrt.open_osfhandle(
            native_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        native_handle = None
        info = os.fstat(descriptor)
        if is_directory != stat.S_ISDIR(info.st_mode):
            raise ValueError("Windows project entry type changed during open")
        if not is_directory and not stat.S_ISREG(info.st_mode):
            raise ValueError("Windows project source must be a regular file")
        return _retained_from_stat(
            path,
            relative,
            descriptor,
            is_directory,
            info,
            root_key=root_key,
            identity=identity,
        )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        if native_handle is not None:
            kernel32.CloseHandle(native_handle)


def _verify_windows_directory_binding(
    directory: _RetainedProjectHandle,
) -> None:
    import ctypes
    import msvcrt
    import ntpath

    if directory.root_key is None:
        raise ValueError("Windows retained directory has no root key")
    kernel32, attributes_type, information_type = _windows_project_api()
    native_handle = msvcrt.get_osfhandle(directory.descriptor)
    attributes = attributes_type()
    if not kernel32.GetFileInformationByHandleEx(
        native_handle,
        9,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if (
        not attributes.file_attributes & 0x10
        or attributes.file_attributes & _WINDOWS_REPARSE_POINT
    ):
        raise ValueError("retained project directory became invalid")
    information = information_type()
    if not kernel32.GetFileInformationByHandle(
        native_handle, ctypes.byref(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    identity = (
        int(information.volume_serial_number),
        int((information.file_index_high << 32) | information.file_index_low),
    )
    final_key = ntpath.normcase(
        ntpath.normpath(_windows_handle_final_path(kernel32, native_handle))
    )
    expected_key = ntpath.normcase(ntpath.normpath(os.fspath(directory.path)))
    if identity != directory.identity or final_key != expected_key:
        raise ValueError("retained Windows directory binding changed")
    probe = _open_windows_project_path(
        directory.path,
        directory.relative,
        directory.root_key,
    )
    try:
        if not probe.is_directory or probe.identity != directory.identity:
            raise ValueError("Windows directory path no longer names its handle")
    finally:
        _close_retained_project_handle(probe)


@lru_cache(maxsize=1)
def _windows_project_api():
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, _FileAttributeTagInfo, _ByHandleFileInformation


def _windows_handle_final_path(kernel32, native_handle: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetFinalPathNameByHandleW(
        native_handle, buffer, len(buffer), 0
    )
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error() or 206)
    return _plain_windows_path(buffer.value)


def _plain_windows_path(value: str) -> str:
    folded = value.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if folded.startswith("\\\\?\\"):
        return value[4:]
    return value


def _read_descriptor_limited(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        remaining = limit - total
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        chunks.append(chunk)
    return b"".join(chunks)


def _tree_entry_budget(max_files: int) -> int:
    return max(_MAX_DIRECTORY_ENTRIES, max_files * 4 + 1)


def _serialize_entries(
    entries: Iterable[LexiconEntry],
) -> tuple[bytes, tuple[LexiconEntry, ...], bool]:
    """Encode a deterministic, bounded cache without allocating unbounded JSON."""
    serialized = bytearray()
    kept: list[LexiconEntry] = []
    for entry in entries:
        line = (
            json.dumps(
                _entry_data(entry),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if (
            len(kept) >= _MAX_SCAN_ENTRIES
            or len(serialized) + len(line) > _MAX_SCAN_CACHE_BYTES
        ):
            return bytes(serialized), tuple(kept), True
        serialized.extend(line)
        kept.append(entry)
    return bytes(serialized), tuple(kept), False


def _scan_state_bytes(
    fingerprint: str,
    cache: bytes,
    max_files: int,
    max_text_bytes: int,
    generation: int,
    truncated: bool,
    project_id: str,
) -> bytes:
    return json.dumps(
        {
            "cache_sha256": hashlib.sha256(cache).hexdigest(),
            "fingerprint": fingerprint,
            "generation": generation,
            "max_files": max_files,
            "max_text_bytes": max_text_bytes,
            "project_id": project_id,
            "schema_version": _SCAN_STATE_VERSION,
            "truncated": truncated,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _next_generation(lease: StateRootLease, relative: Path) -> int:
    try:
        raw = json.loads(
            lease.read_bytes(relative, _MAX_SCAN_STATE_BYTES, "scan state")
        )
        generation = raw.get("generation", 0) if isinstance(raw, dict) else 0
        return (
            generation + 1
            if type(generation) is int and generation >= 0
            else 1
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return 1


def _project_publication_lock(state_paths: StatePaths, project_id: str):
    """Serialize one project through Task 7's alias-stable OS coordination."""
    from .updater import state_serialization_lock

    relative_dir = Path("projects") / project_id
    return state_serialization_lock(
        state_paths,
        f"project-scan:{project_id}",
        retained_dirs=(relative_dir,),
    )


def _remove_pre_release_scan_entries(
    lease: StateRootLease, relative_dir: Path, project_id: str
) -> None:
    relative = relative_dir / "project.jsonl"
    if not lease.exists(relative):
        return
    try:
        entries = load_jsonl_bytes(
            lease.read_bytes(relative, _MAX_SCAN_CACHE_BYTES, "project lexicon"),
            relative,
            expected_scope=Scope.PROJECT,
        )
    except (OSError, ValueError):
        return
    kept = tuple(
        entry for entry in entries if not _is_pre_release_scan_entry(entry, project_id)
    )
    if len(kept) != len(entries):
        serialized, _cached, truncated = _serialize_entries(kept)
        if not truncated:
            lease.write_bytes_atomic(relative, serialized)


def _is_pre_release_scan_entry(entry: LexiconEntry, project_id: str) -> bool:
    if entry.use_count is None or entry.use_count < 1:
        return False
    return (
        entry.project_id == project_id
        and entry.scope is Scope.PROJECT
        and entry.status in {EntryStatus.CANDIDATE, EntryStatus.REPEATED}
        and entry.status
        is (
            EntryStatus.REPEATED
            if entry.use_count > 1
            else EntryStatus.CANDIDATE
        )
        and entry.weight == _entry_weight(entry.use_count)
        and entry.source is not None
        and entry.notes
        in {
            "directory-stem",
            "file-stem",
            "markdown-heading",
            "camel-case",
            "snake-case",
            "quoted-chinese",
            "named-chinese",
        }
        and entry.aliases == (entry.canonical,)
        and entry.domains == ()
        and entry.phonetics == ()
        and entry.negative_aliases == ()
    )

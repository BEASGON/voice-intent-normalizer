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
from pathlib import Path

from .lexicon import _entry_data, load_jsonl_bytes
from .models import EntryStatus, LexiconEntry, Scope
from .paths import StatePaths, StateRootLease, guard_state_root

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


def _relative_source(root: Path, path: Path) -> str:
    """Return a portable, project-relative source location."""
    return path.relative_to(root).as_posix()


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
    root: str | Path,
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

    supplied_root = Path(root).expanduser()
    if supplied_root.is_symlink():
        return ScanResult((), False, 0, 0)
    project_root = supplied_root.resolve()
    project_paths = state_paths.for_project(project_root)
    # A source can change while it is read.  Only publish a cache after a
    # matching bounded metadata snapshot, otherwise retry and leave old state stale.
    for attempt in range(_SCAN_ATTEMPTS):
        before = _project_fingerprint(project_root, max_files=max_files)
        result = _scan_once(
            project_root, project_paths.project_id, max_files, max_text_bytes
        )
        after = _project_fingerprint(project_root, max_files=max_files)
        if before != after:
            continue
        cache, cached_entries, output_truncated = _serialize_entries(result.entries)
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


def project_cache_is_stale(
    root: str | Path,
    state_paths: StatePaths,
    max_files: int = 5_000,
    max_text_bytes: int = 2_000_000,
    *,
    diagnostics: list[str] | None = None,
) -> bool:
    """Return whether the bounded scanner cache needs a safe refresh."""
    _validate_scan_limits(max_files, max_text_bytes)
    supplied_root = Path(root).expanduser()
    if supplied_root.is_symlink():
        return False
    project_root = supplied_root.resolve()
    project_paths = state_paths.for_project(project_root)
    relative_dir = Path("projects") / project_paths.project_id
    try:
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
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _append_scan_diagnostic(diagnostics)
        return True
    return raw["fingerprint"] != _project_fingerprint(project_root, max_files=max_files)


def load_project_scan_entries(
    lease: StateRootLease,
    project_root: Path,
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
        stale = raw["fingerprint"] != _project_fingerprint(
            project_root, max_files=raw["max_files"]
        )
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


def _project_fingerprint(root: Path, *, max_files: int) -> str:
    """Hash bounded metadata only; source content never enters state."""
    records: list[tuple[str, int, int]] = []
    pending = [root]
    files_seen = entries_seen = 0
    max_entries = _tree_entry_budget(max_files)
    truncated = False
    while pending and not truncated:
        directory = pending.pop()
        try:
            children, overflow = _bounded_children(directory)
        except OSError:
            continue
        if overflow:
            truncated = True
            break
        directories: list[Path] = []
        for path in children:
            entries_seen += 1
            if entries_seen > max_entries:
                truncated = True
                break
            if path.is_symlink():
                continue
            if path.is_dir():
                if (
                    path.name.casefold() not in _EXCLUDED_DIRECTORY_NAMES
                    and not _is_private_name(path.name)
                ):
                    # Directory names are scanner input too: they can produce
                    # project candidates even when they contain no files.
                    info = path.stat()
                    records.append(
                        (
                            _relative_source(root, path) + "/",
                            info.st_size,
                            info.st_mtime_ns,
                        )
                    )
                    directories.append(path)
                continue
            if not path.is_file() or not _is_allowed_text_file(path):
                continue
            if files_seen >= max_files:
                truncated = True
                break
            try:
                info = path.stat()
            except OSError:
                continue
            files_seen += 1
            records.append(
                (_relative_source(root, path), info.st_size, info.st_mtime_ns)
            )
        pending.extend(reversed(directories))
    payload = json.dumps(
        {"records": records, "truncated": truncated},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scan_once(
    root: Path, project_id: str, max_files: int, max_text_bytes: int
) -> ScanResult:
    observations: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    pending = [root]
    files_scanned = text_bytes_scanned = entries_seen = 0
    truncated = False
    max_entries = _tree_entry_budget(max_files)
    while pending and not truncated:
        directory = pending.pop()
        try:
            children, overflow = _bounded_children(directory)
        except OSError:
            continue
        if overflow:
            truncated = True
            break
        directories: list[Path] = []
        for path in children:
            entries_seen += 1
            if entries_seen > max_entries:
                truncated = True
                break
            if path.is_symlink():
                continue
            if path.is_dir():
                if (
                    path.name.casefold() not in _EXCLUDED_DIRECTORY_NAMES
                    and not _is_private_name(path.name)
                ):
                    source = _relative_source(root, path)
                    _add_stem(observations, path.name, source, "directory-stem")
                    directories.append(path)
                continue
            if not path.is_file() or not _is_allowed_text_file(path):
                continue
            if files_scanned >= max_files or max_text_bytes - text_bytes_scanned <= 0:
                truncated = True
                break
            remaining = max_text_bytes - text_bytes_scanned
            try:
                content_bytes = _read_project_file(
                    root, path, remaining + 1
                )
            except (OSError, ValueError):
                continue
            if len(content_bytes) > remaining:
                truncated = True
                break
            files_scanned += 1
            text_bytes_scanned += len(content_bytes)
            if b"\0" in content_bytes:
                continue
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            source = _relative_source(root, path)
            _add_stem(observations, path.stem, source, "file-stem")
            _extract_text_terms(content, source, observations)
        pending.extend(reversed(directories))
    return ScanResult(
        _entries_from_observations(observations, project_id),
        truncated,
        files_scanned,
        text_bytes_scanned,
    )


def _read_project_file(root: Path, path: Path, limit: int) -> bytes:
    """Read a bounded direct regular file without escaping the project root."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("project read limit must be a positive integer")
    relative = path.relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("project source must be strictly beneath its root")
    root_info = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ValueError("project root must remain a direct directory")
    root_identity = (root_info.st_dev, root_info.st_ino)
    if os.name == "nt":
        descriptor = _open_windows_project_file(root, path, root_identity)
        try:
            return _read_descriptor_limited(descriptor, limit)
        finally:
            os.close(descriptor)
    return _read_posix_project_file(root, relative, root_identity, limit)


def _read_posix_project_file(
    root: Path,
    relative: Path,
    root_identity: tuple[int, int],
    limit: int,
) -> bytes:
    """Walk every source component relative to one no-follow root descriptor."""
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise OSError("secure project reads require POSIX no-follow flags")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        info = os.fstat(current)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != root_identity
        ):
            raise ValueError("project root identity changed during source open")
        for component in relative.parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("project source parent must be a directory")
        file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        source = os.open(relative.parts[-1], file_flags, dir_fd=current)
        descriptors.append(source)
        if not stat.S_ISREG(os.fstat(source).st_mode):
            raise ValueError("project source must be a regular file")
        return _read_descriptor_limited(source, limit)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_windows_project_file(
    root: Path,
    path: Path,
    root_identity: tuple[int, int],
) -> int:
    """Open a no-follow Windows file and verify its final handle containment."""
    import ctypes
    import msvcrt
    import ntpath
    from ctypes import wintypes

    from .paths import _extended_windows_path

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
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
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        _extended_windows_path(path),
        0x80000000,  # GENERIC_READ
        0x7,  # share read/write/delete; the exact handle remains authoritative
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _FileAttributeTagInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.file_attributes & (0x10 | _WINDOWS_REPARSE_POINT):
            raise ValueError("project source must be a direct regular file")
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if length == 0 or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error() or 206)
        final_path = _plain_windows_path(buffer.value)
        root_path = ntpath.normcase(ntpath.normpath(os.fspath(root)))
        final_key = ntpath.normcase(ntpath.normpath(final_path))
        if ntpath.commonpath((root_path, final_key)) != root_path:
            raise ValueError("project source escaped its root")
        root_after = os.stat(root, follow_symlinks=False)
        if (root_after.st_dev, root_after.st_ino) != root_identity:
            raise ValueError("project root identity changed during source open")
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        handle = None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("project source must be a regular file")
        return descriptor
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


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


def _bounded_children(directory: Path) -> tuple[list[Path], bool]:
    iterator = directory.iterdir()
    children: list[Path] = []
    for child in iterator:
        children.append(child)
        if len(children) > _MAX_DIRECTORY_ENTRIES:
            return [], True
    return sorted(children, key=lambda path: (path.name.casefold(), path.name)), False


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

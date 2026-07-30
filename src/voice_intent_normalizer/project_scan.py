"""Bounded, local-only extraction of project vocabulary candidates."""

from __future__ import annotations

import hashlib
import json
import re
import threading
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
_SCAN_STATE_VERSION = 3
_MAX_SCAN_STATE_BYTES = 64 * 1024
_MAX_SCAN_CACHE_BYTES = 10 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 4_096
_MAX_SCAN_ENTRIES = 50_000
_SCAN_ATTEMPTS = 2
_PUBLICATION_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PUBLICATION_LOCKS_GUARD = threading.Lock()


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
    if max_files < 0 or max_text_bytes < 0:
        raise ValueError("scan limits must be non-negative")

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
        with _project_publication_lock(state_paths, project_paths.project_id):
            with guard_state_root(
                state_paths.root,
                create=True,
                retained_dirs=(relative_dir,),
                create_retained=True,
            ) as lease:
                cache_relative = relative_dir / "project-scan.jsonl"
                state_relative = relative_dir / "scan-state.json"
                if not lease.exists(cache_relative):
                    _remove_pre_release_scan_entries(
                        lease, relative_dir, project_paths.project_id
                    )
                lease.write_bytes_atomic(cache_relative, cache)
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
                    ),
                )
        return result
    raise RuntimeError("project changed during bounded scan")


def project_cache_is_stale(
    root: str | Path,
    state_paths: StatePaths,
    max_files: int = 5_000,
    max_text_bytes: int = 2_000_000,
) -> bool:
    """Return whether the bounded scanner cache needs a safe refresh."""
    if max_files < 0 or max_text_bytes < 0:
        raise ValueError("scan limits must be non-negative")
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
            if not lease.root_exists or not lease.exists(cache_relative):
                return True
            cache = lease.read_bytes(
                cache_relative, _MAX_SCAN_CACHE_BYTES, "scan cache"
            )
            raw = json.loads(
                lease.read_bytes(state_relative, _MAX_SCAN_STATE_BYTES, "scan state")
            )
        if not isinstance(raw, dict) or set(raw) != {
            "fingerprint",
            "cache_sha256",
            "generation",
            "max_files",
            "max_text_bytes",
            "schema_version",
            "truncated",
        }:
            return True
        if (
            raw["schema_version"] != _SCAN_STATE_VERSION
            or raw["max_files"] != max_files
            or raw["max_text_bytes"] != max_text_bytes
            or not isinstance(raw["fingerprint"], str)
            or not isinstance(raw["cache_sha256"], str)
            or not isinstance(raw["truncated"], bool)
            or isinstance(raw["generation"], bool)
            or not isinstance(raw["generation"], int)
        ):
            return True
        if hashlib.sha256(cache).hexdigest() != raw["cache_sha256"]:
            return True
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
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
        if not lease.available(relative_dir) or not lease.exists(cache_relative):
            return (), None
        cache = lease.read_bytes(cache_relative, _MAX_SCAN_CACHE_BYTES, "scan cache")
        raw = json.loads(
            lease.read_bytes(state_relative, _MAX_SCAN_STATE_BYTES, "scan state")
        )
        if not _valid_scan_state(raw):
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


def _valid_scan_state(raw: object) -> bool:
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
        }
        and raw["schema_version"] == _SCAN_STATE_VERSION
        and isinstance(raw["fingerprint"], str)
        and isinstance(raw["cache_sha256"], str)
        and isinstance(raw["generation"], int)
        and not isinstance(raw["generation"], bool)
        and isinstance(raw["max_files"], int)
        and raw["max_files"] >= 0
        and isinstance(raw["max_text_bytes"], int)
        and raw["max_text_bytes"] >= 0
        and isinstance(raw["truncated"], bool)
    )


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
                with path.open("rb") as text_file:
                    content_bytes = text_file.read(remaining + 1)
            except OSError:
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
) -> bytes:
    return json.dumps(
        {
            "cache_sha256": hashlib.sha256(cache).hexdigest(),
            "fingerprint": fingerprint,
            "generation": generation,
            "max_files": max_files,
            "max_text_bytes": max_text_bytes,
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
            if isinstance(generation, int) and not isinstance(generation, bool)
            else 1
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return 1


def _project_publication_lock(state_paths: StatePaths, project_id: str):
    """Serialize same-process scanner publications by direct state-root identity."""
    key = (str(state_paths.root), project_id)
    with _PUBLICATION_LOCKS_GUARD:
        lock = _PUBLICATION_LOCKS.setdefault(key, threading.Lock())
    return lock


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

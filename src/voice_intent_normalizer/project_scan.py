"""Bounded, local-only extraction of project vocabulary candidates."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .lexicon import write_jsonl_atomic
from .models import EntryStatus, LexiconEntry, Scope
from .paths import StatePaths

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
    return lowered.startswith(".") or lowered in _SSH_PRIVATE_KEY_NAMES or any(
        part in lowered for part in _CREDENTIAL_NAME_PARTS
    )


def _is_allowed_text_file(path: Path) -> bool:
    """Limit content checks to known text-like filenames."""
    return (
        not _is_private_name(path.name)
        and path.suffix.casefold() in _TEXT_EXTENSIONS
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
    observations: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    pending = [project_root]
    files_scanned = 0
    text_bytes_scanned = 0
    truncated = False

    while pending and not truncated:
        directory = pending.pop()
        try:
            children = sorted(
                directory.iterdir(), key=lambda path: path.name.casefold()
            )
        except OSError:
            continue
        for path in children:
            if path.is_symlink():
                continue
            if path.is_dir():
                if (
                    path.name.casefold() not in _EXCLUDED_DIRECTORY_NAMES
                    and not _is_private_name(path.name)
                ):
                    source = _relative_source(project_root, path)
                    _add_stem(observations, path.name, source, "directory-stem")
                    pending.append(path)
                continue
            if not path.is_file() or not _is_allowed_text_file(path):
                continue
            if files_scanned >= max_files:
                truncated = True
                break
            remaining_bytes = max_text_bytes - text_bytes_scanned
            if remaining_bytes <= 0:
                truncated = True
                break
            try:
                with path.open("rb") as text_file:
                    content_bytes = text_file.read(remaining_bytes + 1)
            except OSError:
                continue
            if len(content_bytes) > remaining_bytes:
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

            source = _relative_source(project_root, path)
            _add_stem(observations, path.stem, source, "file-stem")
            _extract_text_terms(content, source, observations)

    entries = _entries_from_observations(observations, project_paths.project_id)
    project_paths.root.mkdir(parents=True, exist_ok=True)
    write_jsonl_atomic(project_paths.lexicon_file, entries)
    return ScanResult(
        entries=entries,
        truncated=truncated,
        files_scanned=files_scanned,
        text_bytes_scanned=text_bytes_scanned,
    )

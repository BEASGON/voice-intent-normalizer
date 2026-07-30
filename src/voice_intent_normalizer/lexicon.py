"""Strict JSONL parsing and persistence for lexicon entries."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import EntryStatus, LexiconEntry, Scope
from .paths import (
    StatePaths,
    StateRootLease,
    StateRootValidationError,
    guard_state_root,
    state_root_identity,
    validate_state_root,
)
from .updater import resolve_hotword_file

_REQUIRED_FIELDS = frozenset(
    {"canonical", "scope", "aliases", "domains", "weight", "status"}
)
_OPTIONAL_FIELDS = frozenset(
    {
        "phonetics",
        "project_id",
        "source",
        "negative_aliases",
        "use_count",
        "notes",
    }
)
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_MAX_STATE_LEXICON_BYTES = 10 * 1024 * 1024


def _normalized_alias(value: str) -> str:
    """Normalize alias lookup keys while preserving entry display values."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _deduplicate(entries: Sequence[LexiconEntry]) -> tuple[LexiconEntry, ...]:
    """Keep only the final record for each exact identity within one file."""
    unique: dict[tuple[str, Scope, str | None], LexiconEntry] = {}
    for entry in entries:
        key = (entry.canonical, entry.scope, entry.project_id)
        unique.pop(key, None)
        unique[key] = entry
    return tuple(unique.values())


def _load_if_present(path: Path, scope: Scope) -> tuple[LexiconEntry, ...]:
    """Load one lexicon when present; a present invalid file remains an error."""
    if not path.is_file():
        return ()
    return _deduplicate(load_jsonl(path, expected_scope=scope))


def _load_lease_if_present(
    lease: StateRootLease,
    relative: str | Path,
    scope: Scope,
) -> tuple[LexiconEntry, ...]:
    """Load one optional state lexicon through its retained directory identity."""
    if not lease.available(relative) or not lease.exists(relative):
        return ()
    if not stat.S_ISREG(lease.stat(relative).st_mode):
        return ()
    data = lease.read_bytes(relative, _MAX_STATE_LEXICON_BYTES, "lexicon")
    return _deduplicate(
        load_jsonl_bytes(data, relative, expected_scope=scope)
    )


def _first_existing(paths: Sequence[Path]) -> Path | None:
    """Return the first conventional built-in file that exists."""
    return next((path for path in paths if path.is_file()), None)


@dataclass(frozen=True, slots=True)
class LexiconSet:
    """Layered lexicon entries and a normalized alias index."""

    entries: tuple[LexiconEntry, ...]
    _aliases: Mapping[str, tuple[LexiconEntry, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        aliases: dict[str, list[LexiconEntry]] = {}
        for entry in entries:
            for alias in entry.aliases:
                aliases.setdefault(_normalized_alias(alias), []).append(entry)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "_aliases",
            {alias: tuple(matches) for alias, matches in aliases.items()},
        )

    @classmethod
    def load(
        cls,
        state_paths: StatePaths,
        builtins_root: str | Path,
        project_root: str | Path | None = None,
        domains: Sequence[str] = (),
    ) -> LexiconSet:
        """Load layers in personal, project, industry, hot, then base precedence."""
        validate_state_root(state_paths.root)
        initial_root_identity = state_root_identity(state_paths.root)
        builtin_paths = Path(builtins_root)
        project_paths = (
            None
            if project_root is None
            else state_paths.for_project(project_root)
        )
        retained_dirs: list[Path] = [
            Path("hotwords"),
            Path("hotwords/payloads"),
        ]
        if project_paths is not None:
            retained_dirs.append(
                Path("projects") / project_paths.project_id
            )
        layers: list[LexiconEntry] = []
        hotword_data = resolve_hotword_file(state_paths)
        if initial_root_identity is None:
            initial_root_identity = state_root_identity(state_paths.root)

        with guard_state_root(
            state_paths.root, retained_dirs=retained_dirs
        ) as lease:
            if (
                initial_root_identity is not None
                and state_root_identity(state_paths.root)
                != initial_root_identity
            ):
                raise StateRootValidationError(
                    "state root rejected: direct canonical local path required"
                )
            if lease.root_exists:
                layers.extend(
                    _load_lease_if_present(
                        lease, "personal.jsonl", Scope.PERSONAL
                    )
                )

            if project_paths is not None and lease.root_exists:
                project_relative = (
                    Path("projects")
                    / project_paths.project_id
                    / "project.jsonl"
                )
                project_entries = (
                    _load_lease_if_present(
                        lease,
                        project_relative,
                        Scope.PROJECT,
                    )
                    if lease.available(project_relative)
                    else ()
                )
                layers.extend(
                    entry
                    for entry in project_entries
                    if entry.project_id == project_paths.project_id
                )

            seen_domains: set[str] = set()
            for domain in domains:
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                industry_path = _first_existing(
                    (
                        builtin_paths / "domains" / f"{domain}.jsonl",
                        builtin_paths / "industry" / f"{domain}.jsonl",
                    )
                )
                if industry_path is not None:
                    layers.extend(
                        _load_if_present(industry_path, Scope.INDUSTRY)
                    )

            hotword_path = None
            if hotword_data is None:
                hotword_path = _first_existing(
                    (
                        builtin_paths / "hotwords-snapshot.jsonl",
                        builtin_paths / "hot.jsonl",
                    )
                )
            if hotword_data is not None:
                layers.extend(
                    _deduplicate(
                        load_jsonl_bytes(
                            hotword_data,
                            "hotwords/authoritative",
                            expected_scope=Scope.HOT,
                        )
                    )
                )
            elif hotword_path is not None:
                layers.extend(_load_if_present(hotword_path, Scope.HOT))

            base_path = _first_existing(
                (builtin_paths / "base-zh.jsonl", builtin_paths / "base.jsonl")
            )
            if base_path is not None:
                layers.extend(_load_if_present(base_path, Scope.BASE))

        return cls(entries=tuple(layers))

    def by_alias(self, alias: str) -> tuple[LexiconEntry, ...]:
        """Return matching entries in layer precedence order."""
        return self._aliases.get(_normalized_alias(alias), ())


def _collection(raw: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must contain only strings")
    return tuple(value)


def _optional_string(raw: Mapping[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _optional_use_count(raw: Mapping[str, Any]) -> int | None:
    """Read a non-negative integer count without accepting booleans."""
    value = raw.get("use_count")
    if isinstance(value, bool) or (value is not None and not isinstance(value, int)):
        raise ValueError("use_count must be a non-negative integer or null")
    if value is not None and value < 0:
        raise ValueError("use_count must be a non-negative integer or null")
    return value


def parse_entry(
    raw: Mapping[str, Any], expected_scope: Scope | None = None
) -> LexiconEntry:
    """Validate one JSON-compatible object and convert it to an immutable entry."""
    if not isinstance(raw, Mapping):
        raise ValueError("entry must be a JSON object")

    keys = set(raw)
    missing = _REQUIRED_FIELDS - keys
    unknown = keys - _ALL_FIELDS
    if missing:
        raise ValueError(f"missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")

    canonical = raw["canonical"]
    if not isinstance(canonical, str):
        raise ValueError("canonical must be a string")
    scope = raw["scope"]
    status = raw["status"]
    weight = raw["weight"]
    if not isinstance(scope, str):
        raise ValueError("scope must be a string")
    if not isinstance(status, str):
        raise ValueError("status must be a string")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise ValueError("weight must be a number")

    try:
        entry = LexiconEntry(
            canonical=canonical,
            scope=Scope(scope),
            aliases=_collection(raw, "aliases"),
            domains=_collection(raw, "domains"),
            weight=weight,
            status=EntryStatus(status),
            phonetics=_collection(raw, "phonetics"),
            project_id=_optional_string(raw, "project_id"),
            source=_optional_string(raw, "source"),
            use_count=_optional_use_count(raw),
            notes=_optional_string(raw, "notes"),
            negative_aliases=_collection(raw, "negative_aliases"),
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if expected_scope is not None and entry.scope is not Scope(expected_scope):
        raise ValueError(
            f"scope {entry.scope.value!r} does not match expected scope "
            f"{Scope(expected_scope).value!r}"
        )
    return entry


def load_jsonl(
    path: str | Path, expected_scope: Scope | None = None
) -> tuple[LexiconEntry, ...]:
    """Load a strict UTF-8 JSONL lexicon, adding location details to every error."""
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"{source}: line 1: unable to read lexicon: {exc}") from exc
    return load_jsonl_bytes(data, source, expected_scope=expected_scope)


def load_jsonl_bytes(
    data: bytes,
    source: str | Path,
    expected_scope: Scope | None = None,
) -> tuple[LexiconEntry, ...]:
    """Parse strict UTF-8 JSONL bytes already bound to an exact file identity."""
    if not isinstance(data, bytes):
        raise TypeError("lexicon data must be bytes")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        line_number = exc.object[: exc.start].count(b"\n") + 1
        raise ValueError(f"{source}: line {line_number}: invalid UTF-8") from exc

    entries: list[LexiconEntry] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(
                f"{source}: line {line_number}: blank lines are not allowed"
            )
        try:
            raw = json.loads(line)
            entries.append(parse_entry(raw, expected_scope=expected_scope))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{source}: line {line_number}: {exc}") from exc
    return tuple(entries)


def _entry_data(entry: LexiconEntry) -> dict[str, Any]:
    return {
        "aliases": list(entry.aliases),
        "canonical": entry.canonical,
        "domains": list(entry.domains),
        "negative_aliases": list(entry.negative_aliases),
        "notes": entry.notes,
        "phonetics": list(entry.phonetics),
        "project_id": entry.project_id,
        "scope": entry.scope.value,
        "source": entry.source,
        "status": entry.status.value,
        "use_count": entry.use_count,
        "weight": entry.weight,
    }


def write_jsonl_atomic(path: str | Path, entries: Sequence[LexiconEntry]) -> None:
    """Atomically replace *path* with deterministic UTF-8 JSONL serialization."""
    target = Path(path)
    serialized = "\n".join(
        json.dumps(
            _entry_data(entry),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for entry in entries
    )
    if serialized:
        serialized += "\n"

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

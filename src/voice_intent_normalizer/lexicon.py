"""Strict JSONL parsing and persistence for lexicon entries."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import EntryStatus, LexiconEntry, Scope

_REQUIRED_FIELDS = frozenset(
    {"canonical", "scope", "aliases", "domains", "weight", "status"}
)
_OPTIONAL_FIELDS = frozenset(
    {"phonetics", "project_id", "source", "negative_aliases"}
)
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


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
        lines = source.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        line_number = exc.object[: exc.start].count(b"\n") + 1
        raise ValueError(f"{source}: line {line_number}: invalid UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"{source}: line 1: unable to read lexicon: {exc}") from exc

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
        "phonetics": list(entry.phonetics),
        "project_id": entry.project_id,
        "scope": entry.scope.value,
        "source": entry.source,
        "status": entry.status.value,
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

"""Explicit, auditable learning controls for voice-intent lexicons."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lexicon import load_jsonl, write_jsonl_atomic
from .models import EntryStatus, LexiconEntry, Scope

_OUTER_PUNCTUATION = " \t\r\n，。！？；：,.!?;:"
_MAPPING_PATTERNS = (
    (
        "confirm",
        re.compile(r"^我说的是\s*(?P<canonical>.+?)\s*[,，]\s*不是\s*(?P<alias>.+?)$"),
    ),
    (
        "confirm",
        re.compile(r"^不是\s*(?P<alias>.+?)\s*[,，]\s*是\s*(?P<canonical>.+?)$"),
    ),
    (
        "confirm",
        re.compile(r"^以后把\s*(?P<alias>.+?)\s*理解为\s*(?P<canonical>.+?)$"),
    ),
    (
        "reject",
        re.compile(r"^不要(?:再)?把\s*(?P<alias>.+?)\s*改成\s*(?P<canonical>.+?)$"),
    ),
)
_SIMPLE_CONTROLS = {
    "撤销刚才的纠正": "undo",
    "删除你学到的这个词": "delete",
    "查看最近学到的词": "list_recent",
}
_LEARNING_ACTIONS = frozenset({"confirm", "reject", "delete"})
_LEARNING_SOURCE = "explicit-learning"


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """One explicitly phrased user control command."""

    kind: str
    alias: str | None = None
    canonical: str | None = None


@dataclass(frozen=True, slots=True)
class LearningEvent:
    """One immutable record in the local learning audit log."""

    event_id: str
    timestamp: str
    action: str
    alias: str
    canonical: str
    status: EntryStatus
    scope: Scope | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EntryStatus(self.status))
        if self.scope is not None:
            object.__setattr__(self, "scope", Scope(self.scope))


def parse_control(text: str) -> ControlCommand | None:
    """Parse only explicit learning and management sentence patterns."""
    if not isinstance(text, str):
        return None
    normalized = text.strip(_OUTER_PUNCTUATION)
    if normalized in _SIMPLE_CONTROLS:
        return ControlCommand(kind=_SIMPLE_CONTROLS[normalized])
    for kind, pattern in _MAPPING_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        alias = match.group("alias").strip()
        canonical = match.group("canonical").strip()
        if alias and canonical:
            return ControlCommand(kind=kind, alias=alias, canonical=canonical)
    return None


class LearningStore:
    """Append-only event storage with derived personal and project lexicons."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def for_root(cls, root: str | Path) -> LearningStore:
        """Create a store rooted at an already resolved shared-state directory."""
        return cls(root)

    @property
    def events_file(self) -> Path:
        return self.root / "learning-events.jsonl"

    @property
    def personal_file(self) -> Path:
        return self.root / "personal.jsonl"

    def confirm(
        self,
        alias: str,
        canonical: str,
        scope: Scope,
        project_id: str | None = None,
    ) -> LearningEvent:
        """Persist an explicit positive mapping in the requested lexicon scope."""
        resolved_scope = Scope(scope)
        if resolved_scope not in {Scope.PERSONAL, Scope.PROJECT}:
            raise ValueError("confirmed mappings must be personal or project scoped")
        if resolved_scope is Scope.PROJECT:
            project_id = _project_id(project_id)
        elif project_id is not None:
            raise ValueError("personal mappings must not have a project_id")
        event = self._append_event(
            "confirm",
            alias,
            canonical,
            status=EntryStatus.CONFIRMED,
            scope=resolved_scope,
            project_id=project_id,
        )
        self._materialize()
        return event

    def observe(self, alias: str, canonical: str, source: str) -> EntryStatus:
        """Record an unconfirmed sighting without changing active lexicons."""
        status = (
            EntryStatus.REPEATED
            if self._latest_observation(alias, canonical) is not None
            else EntryStatus.CANDIDATE
        )
        self._append_event("observe", alias, canonical, status=status, source=source)
        return status

    def reject(self, alias: str, canonical: str) -> LearningEvent:
        """Persist an explicit personal negative mapping."""
        event = self._append_event(
            "reject",
            alias,
            canonical,
            status=EntryStatus.REJECTED,
            scope=Scope.PERSONAL,
        )
        self._materialize()
        return event

    def undo_last(self) -> LearningEvent | None:
        """Append a compensating event for the latest effective learning action."""
        operations = self._effective_operations()
        if not operations:
            return None
        target = operations[-1]
        event = _event_from_raw(target)
        self._append_event(
            "undo",
            event.alias,
            event.canonical,
            status=event.status,
            scope=event.scope,
            project_id=event.project_id,
            target_event_id=event.event_id,
        )
        self._materialize()
        return event

    def personal_entries(self) -> tuple[LexiconEntry, ...]:
        """Return active personal entries, or no entries before the first mapping."""
        if not self.personal_file.is_file():
            return ()
        return load_jsonl(self.personal_file, expected_scope=Scope.PERSONAL)

    def list_recent(self, limit: int = 20) -> tuple[LearningEvent, ...]:
        """Return active learning records in newest-first order."""
        if limit <= 0:
            return ()
        events = [_event_from_raw(event) for event in self._active_mapping_events()]
        return tuple(reversed(events[-limit:]))

    def delete(self, canonical: str) -> LearningEvent | None:
        """Append a deletion event and rebuild all derived lexicons."""
        targets = [
            event["event_id"]
            for event in self._active_mapping_events()
            if event["canonical"] == canonical
        ]
        if not targets:
            return None
        event = self._append_event(
            "delete",
            canonical,
            canonical,
            status=EntryStatus.CONFIRMED,
            target_event_ids=targets,
        )
        self._materialize()
        return event

    def export(self, path: str | Path) -> None:
        """Atomically export the currently active personal lexicon."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl_atomic(destination, self.personal_entries())

    def _append_event(
        self,
        action: str,
        alias: str,
        canonical: str,
        *,
        status: EntryStatus,
        scope: Scope | None = None,
        project_id: str | None = None,
        source: str | None = None,
        target_event_id: str | None = None,
        target_event_ids: list[str] | None = None,
    ) -> LearningEvent:
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        if source is not None:
            source = _required_text(source, "source")
        event = LearningEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            action=action,
            alias=alias,
            canonical=canonical,
            status=status,
            scope=scope,
            project_id=project_id,
        )
        raw: dict[str, Any] = {
            "action": event.action,
            "alias": event.alias,
            "canonical": event.canonical,
            "event_id": event.event_id,
            "project_id": event.project_id,
            "scope": event.scope.value if event.scope is not None else None,
            "source": source,
            "status": event.status.value,
            "timestamp": event.timestamp,
        }
        if target_event_id is not None:
            raw["target_event_id"] = target_event_id
        if target_event_ids is not None:
            raw["target_event_ids"] = target_event_ids
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def _latest_observation(self, alias: str, canonical: str) -> dict[str, Any] | None:
        for event in reversed(self._events()):
            if (
                event["action"] == "observe"
                and event["alias"] == alias
                and event["canonical"] == canonical
            ):
                return event
        return None

    def _events(self) -> list[dict[str, Any]]:
        if not self.events_file.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.events_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line:
                raise ValueError(f"{self.events_file}: line {line_number}: blank event")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{self.events_file}: line {line_number}: invalid JSON"
                ) from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    f"{self.events_file}: line {line_number}: invalid event"
                )
            events.append(raw)
        return events

    def _effective_operations(self) -> list[dict[str, Any]]:
        events = self._events()
        undone = {
            event["target_event_id"]
            for event in events
            if event.get("action") == "undo" and "target_event_id" in event
        }
        return [
            event
            for event in events
            if event.get("action") in _LEARNING_ACTIONS
            and event.get("event_id") not in undone
        ]

    def _active_mapping_events(self) -> list[dict[str, Any]]:
        active: dict[str, dict[str, Any]] = {}
        for event in self._effective_operations():
            action = event["action"]
            if action in {"confirm", "reject"}:
                active[event["event_id"]] = event
            elif action == "delete":
                for target in event.get("target_event_ids", []):
                    active.pop(target, None)
        return list(active.values())

    def _materialize(self) -> None:
        entries_by_scope: dict[tuple[Scope, str | None], list[LexiconEntry]] = {}
        grouped: dict[tuple[Scope, str | None, str], dict[str, list[str]]] = {}
        project_ids: set[str] = set()
        for event in self._events():
            if event.get("action") == "confirm" and event.get("scope") == "project":
                project_ids.add(_project_id(event.get("project_id")))
        for event in self._active_mapping_events():
            action = event["action"]
            scope = Scope(event["scope"])
            project_id = event.get("project_id")
            key = (scope, project_id, event["canonical"])
            mapping = grouped.setdefault(key, {"aliases": [], "negative_aliases": []})
            field = "aliases" if action == "confirm" else "negative_aliases"
            mapping[field].append(event["alias"])

        for (scope, project_id, canonical), mapping in grouped.items():
            aliases = _unique(mapping["aliases"])
            negative_aliases = _unique(mapping["negative_aliases"])
            if not aliases:
                aliases = (canonical,)
            status = (
                EntryStatus.CONFIRMED if mapping["aliases"] else EntryStatus.REJECTED
            )
            entries_by_scope.setdefault((scope, project_id), []).append(
                LexiconEntry(
                    canonical=canonical,
                    scope=scope,
                    aliases=aliases,
                    domains=(),
                    weight=1.0,
                    status=status,
                    project_id=project_id,
                    source=_LEARNING_SOURCE,
                    negative_aliases=negative_aliases,
                )
            )

        personal_entries = entries_by_scope.get((Scope.PERSONAL, None), [])
        self._write_entries(
            self.personal_file,
            self._merge_entries(
                self.personal_file, Scope.PERSONAL, personal_entries
            ),
        )
        for project_id in project_ids:
            path = self.root / "projects" / project_id / "project.jsonl"
            self._write_entries(
                path,
                self._merge_entries(
                    path,
                    Scope.PROJECT,
                    entries_by_scope.get((Scope.PROJECT, project_id), []),
                ),
            )

    @staticmethod
    def _merge_entries(
        path: Path,
        scope: Scope,
        learned_entries: list[LexiconEntry],
    ) -> list[LexiconEntry]:
        preserved = (
            ()
            if not path.is_file()
            else tuple(
                entry
                for entry in load_jsonl(path, expected_scope=scope)
                if not _is_derived_learning_entry(entry)
            )
        )
        merged: dict[tuple[str, Scope, str | None], LexiconEntry] = {}
        for entry in (*preserved, *learned_entries):
            key = (entry.canonical, entry.scope, entry.project_id)
            previous = merged.get(key)
            merged[key] = entry if previous is None else _merge_entry(previous, entry)
        return list(merged.values())

    @staticmethod
    def _write_entries(path: Path, entries: list[LexiconEntry]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl_atomic(path, entries)


def _event_from_raw(raw: dict[str, Any]) -> LearningEvent:
    return LearningEvent(
        event_id=raw["event_id"],
        timestamp=raw["timestamp"],
        action=raw["action"],
        alias=raw["alias"],
        canonical=raw["canonical"],
        status=EntryStatus(raw["status"]),
        scope=Scope(raw["scope"]) if raw.get("scope") is not None else None,
        project_id=raw.get("project_id"),
    )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    return value


def _project_id(project_id: str | None) -> str:
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", project_id
    ):
        raise ValueError(
            "project_id must contain only letters, numbers, underscores, or hyphens"
        )
    return project_id


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _is_derived_learning_entry(entry: LexiconEntry) -> bool:
    """Recognize current and pre-marker learning materializations."""
    return entry.source == _LEARNING_SOURCE or (
        entry.source is None
        and entry.status in {EntryStatus.CONFIRMED, EntryStatus.REJECTED}
    )


def _merge_entry(left: LexiconEntry, right: LexiconEntry) -> LexiconEntry:
    """Merge equal lexicon identities without retaining duplicate JSONL records."""
    if (left.canonical, left.scope, left.project_id) != (
        right.canonical,
        right.scope,
        right.project_id,
    ):
        raise ValueError("only equal lexicon identities can be merged")
    left_is_learning = left.source == _LEARNING_SOURCE
    existing = right if left_is_learning else left
    learned = left if left_is_learning else right
    return LexiconEntry(
        canonical=existing.canonical,
        scope=existing.scope,
        aliases=_unique([*existing.aliases, *learned.aliases]),
        domains=_unique([*existing.domains, *learned.domains]),
        weight=max(existing.weight, learned.weight),
        status=learned.status,
        phonetics=_unique([*existing.phonetics, *learned.phonetics]),
        project_id=existing.project_id,
        source=existing.source,
        use_count=existing.use_count,
        notes=existing.notes,
        negative_aliases=_unique(
            [*existing.negative_aliases, *learned.negative_aliases]
        ),
    )

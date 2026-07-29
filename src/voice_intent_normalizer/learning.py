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

from .lexicon import load_jsonl, parse_entry, write_jsonl_atomic
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
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        baseline_captured, baseline, migrated_ids = self._baseline_capture_for(
            resolved_scope, project_id, canonical
        )
        event = self._append_event(
            "confirm",
            alias,
            canonical,
            status=EntryStatus.CONFIRMED,
            scope=resolved_scope,
            project_id=project_id,
            baseline=baseline,
            baseline_captured=baseline_captured,
            baseline_migrated_event_ids=migrated_ids,
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
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        baseline_captured, baseline, migrated_ids = self._baseline_capture_for(
            Scope.PERSONAL, None, canonical
        )
        event = self._append_event(
            "reject",
            alias,
            canonical,
            status=EntryStatus.REJECTED,
            scope=Scope.PERSONAL,
            baseline=baseline,
            baseline_captured=baseline_captured,
            baseline_migrated_event_ids=migrated_ids,
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
        migrations = self._migration_baselines_for((target,))
        scope, project_id = self._compensation_location(target)
        self._append_event(
            "undo",
            event.alias,
            event.canonical,
            status=event.status,
            scope=scope,
            project_id=project_id,
            target_event_id=event.event_id,
            migration_fallback=(
                "remove-historical-learning-aliases" if migrations else None
            ),
            migration_baselines=migrations,
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
        canonical = _required_text(canonical, "canonical")
        target_events = [
            event
            for event in self._active_mapping_events()
            if event["canonical"] == canonical
        ]
        if not target_events:
            return None
        targets = [event["event_id"] for event in target_events]
        migrations = self._migration_baselines_for(tuple(target_events))
        scope, project_id = _common_event_location(target_events)
        event = self._append_event(
            "delete",
            canonical,
            canonical,
            status=EntryStatus.CONFIRMED,
            scope=scope,
            project_id=project_id,
            target_event_ids=targets,
            migration_fallback=(
                "remove-historical-learning-aliases" if migrations else None
            ),
            migration_baselines=migrations,
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
        baseline: LexiconEntry | None = None,
        baseline_captured: bool = False,
        baseline_migrated_event_ids: list[str] | None = None,
        migration_fallback: str | None = None,
        migration_baselines: list[dict[str, Any]] | None = None,
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
        if baseline_captured:
            raw["baseline_captured"] = True
            raw["baseline"] = (
                None if baseline is None else _entry_snapshot(baseline)
            )
        elif baseline is not None:
            raw["baseline"] = _entry_snapshot(baseline)
        if baseline_migrated_event_ids:
            raw["baseline_migrated_event_ids"] = baseline_migrated_event_ids
        if migration_fallback is not None:
            raw["migration_fallback"] = migration_fallback
        if migration_baselines:
            raw["migration_baselines"] = migration_baselines
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

    def _entry_at(
        self, scope: Scope, project_id: str | None, canonical: str
    ) -> LexiconEntry | None:
        path = self.personal_file
        if scope is Scope.PROJECT:
            assert project_id is not None
            path = self.root / "projects" / project_id / "project.jsonl"
        if not path.is_file():
            return None
        key = (canonical, scope, project_id)
        for entry in load_jsonl(path, expected_scope=scope):
            if _entry_key(entry) == key:
                return entry
        return None

    def _baseline_capture_for(
        self, scope: Scope, project_id: str | None, canonical: str
    ) -> tuple[bool, LexiconEntry | None, list[str]]:
        """Capture one generation baseline, conservatively cleaning legacy overlays."""
        events = self._events()
        key = (canonical, scope, project_id)
        active = [
            event
            for event in self._active_mapping_events()
            if _raw_event_key(event) == key
        ]
        current = self._entry_at(scope, project_id, canonical)
        candidate = _active_provenance_candidate(events, active)
        manual_overlay = (
            current is not None
            and current.source != _LEARNING_SOURCE
            and _contains_contributions(current, active)
        )
        if (
            candidate is not None
            and _candidate_is_clean(candidate, events, active)
            and not manual_overlay
        ):
            return False, None, []
        if not active:
            return True, current, []
        source = (
            current
            if candidate is None or manual_overlay
            else candidate[2]
        )
        return (
            True,
            _conservative_baseline(source, tuple(active)),
            [event["event_id"] for event in active],
        )

    def _migration_baselines_for(
        self, targets: tuple[dict[str, Any], ...]
    ) -> list[dict[str, Any]]:
        """Create bounded provenance for ambiguous pre-snapshot contributions."""
        events = self._events()
        by_id = {
            event["event_id"]: event
            for event in events
            if isinstance(event.get("event_id"), str)
        }
        mapping_targets = _expand_mapping_targets(targets, by_id)
        target_keys = {_raw_event_key(event) for event in mapping_targets}
        active = self._active_mapping_events()
        migrations: list[dict[str, Any]] = []
        for key in sorted(
            target_keys,
            key=lambda value: (value[1].value, value[2] or "", value[0]),
        ):
            cohort = _unique_events(
                [
                    event
                    for event in (*active, *mapping_targets)
                    if _raw_event_key(event) == key
                ]
            )
            candidate = _active_provenance_candidate(events, cohort)
            current = self._entry_at(key[1], key[2], key[0])
            manual_overlay = (
                current is not None
                and current.source != _LEARNING_SOURCE
                and _contains_contributions(current, cohort)
            )
            if (
                candidate is not None
                and _candidate_is_clean(candidate, events, cohort)
                and not manual_overlay
            ):
                continue
            source = (
                current
                if candidate is None or manual_overlay
                else candidate[2]
            )
            baseline = _conservative_baseline(source, tuple(cohort))
            migrations.append(
                {
                    "baseline": (
                        None if baseline is None else _entry_snapshot(baseline)
                    ),
                    "before": (
                        None if current is None else _entry_snapshot(current)
                    ),
                    "canonical": key[0],
                    "event_ids": [event["event_id"] for event in cohort],
                    "project_id": key[2],
                    "reason": "legacy-state-split-unknown",
                    "scope": key[1].value,
                }
            )
        return migrations

    def _compensation_location(
        self, target: dict[str, Any]
    ) -> tuple[Scope | None, str | None]:
        if target.get("action") != "delete":
            return Scope(target["scope"]), target.get("project_id")
        by_id = {
            event["event_id"]: event
            for event in self._events()
            if isinstance(event.get("event_id"), str)
        }
        return _common_event_location(_expand_mapping_targets((target,), by_id))

    def _materialize(self) -> None:
        events = self._events()
        entries_by_scope: dict[tuple[Scope, str | None], list[LexiconEntry]] = {}
        project_ids: set[str] = set()
        for event in events:
            if (
                event.get("action") in {"confirm", "reject"}
                and event.get("scope") == "project"
            ):
                project_ids.add(_project_id(event.get("project_id")))
        active_events = self._active_mapping_events()
        for entry in _entries_from_learning_events(active_events, _LEARNING_SOURCE):
            entries_by_scope.setdefault((entry.scope, entry.project_id), []).append(
                entry
            )
        baselines = _provenance_baselines(events, active_events)
        pending = _pending_migration_events(events)
        pending_keys = {
            _migration_key(migration)
            for event in pending
            for migration in event["migration_baselines"]
        }

        personal_entries = entries_by_scope.get((Scope.PERSONAL, None), [])
        self._write_entries(
            self.personal_file,
            self._merge_entries(
                self.personal_file,
                Scope.PERSONAL,
                personal_entries,
                baselines,
                active_events,
                pending_keys,
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
                    baselines,
                    active_events,
                    pending_keys,
                ),
            )
        for event in pending:
            self._append_event(
                "migration_applied",
                event["alias"],
                event["canonical"],
                status=EntryStatus(event["status"]),
                scope=(
                    Scope(event["scope"])
                    if event.get("scope") is not None
                    else None
                ),
                project_id=event.get("project_id"),
                target_event_id=event["event_id"],
                migration_fallback=event.get("migration_fallback"),
            )

    @staticmethod
    def _merge_entries(
        path: Path,
        scope: Scope,
        learned_entries: list[LexiconEntry],
        baselines: dict[
            tuple[str, Scope, str | None], LexiconEntry | None
        ],
        active_events: list[dict[str, Any]],
        pending_keys: set[tuple[str, Scope, str | None]],
    ) -> list[LexiconEntry]:
        current = (
            ()
            if not path.is_file()
            else load_jsonl(path, expected_scope=scope)
        )
        merged = {_entry_key(entry): entry for entry in current}
        learned_by_key = {
            _entry_key(entry): entry for entry in learned_entries
        }
        active_by_key: dict[
            tuple[str, Scope, str | None], list[dict[str, Any]]
        ] = {}
        for event in active_events:
            key = _raw_event_key(event)
            active_by_key.setdefault(key, []).append(event)

        for key, learned in learned_by_key.items():
            if key[1] is not scope:
                continue
            existing = merged.get(key)
            if (
                key not in pending_keys
                and existing is not None
                and existing.source != _LEARNING_SOURCE
                and _contains_contributions(existing, active_by_key[key])
            ):
                continue
            if key in baselines:
                baseline = baselines[key]
                if baseline is None:
                    merged.pop(key, None)
                else:
                    merged[key] = baseline
            else:
                baseline = _conservative_baseline(
                    existing, tuple(active_by_key[key])
                )
                if baseline is None:
                    merged.pop(key, None)
                else:
                    merged[key] = baseline
            previous = merged.get(key)
            merged[key] = (
                learned
                if previous is None
                else _merge_overlay(previous, learned)
            )

        for key, baseline in baselines.items():
            if key[1] is not scope or key in learned_by_key:
                continue
            existing = merged.get(key)
            if (
                key not in pending_keys
                and (
                    existing is None
                    or existing.source != _LEARNING_SOURCE
                )
            ):
                continue
            if baseline is None:
                merged.pop(key, None)
            else:
                merged[key] = baseline
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


def _entry_key(entry: LexiconEntry) -> tuple[str, Scope, str | None]:
    return (entry.canonical, entry.scope, entry.project_id)


def _entry_snapshot(entry: LexiconEntry) -> dict[str, Any]:
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


def _entries_from_learning_events(
    events: list[dict[str, Any]], source: str | None
) -> tuple[LexiconEntry, ...]:
    grouped: dict[tuple[Scope, str | None, str], dict[str, list[str]]] = {}
    for event in events:
        action = event["action"]
        scope = Scope(event["scope"])
        project_id = event.get("project_id")
        key = (scope, project_id, event["canonical"])
        mapping = grouped.setdefault(key, {"aliases": [], "negative_aliases": []})
        field = "aliases" if action == "confirm" else "negative_aliases"
        mapping[field].append(event["alias"])

    entries: list[LexiconEntry] = []
    for (scope, project_id, canonical), mapping in grouped.items():
        aliases = _unique(mapping["aliases"])
        if not aliases:
            aliases = (canonical,)
        entries.append(
            LexiconEntry(
                canonical=canonical,
                scope=scope,
                aliases=aliases,
                domains=(),
                weight=1.0,
                status=(
                    EntryStatus.CONFIRMED
                    if mapping["aliases"]
                    else EntryStatus.REJECTED
                ),
                project_id=project_id,
                source=source,
                negative_aliases=_unique(mapping["negative_aliases"]),
            )
        )
    return tuple(entries)


def _raw_event_key(
    event: dict[str, Any],
) -> tuple[str, Scope, str | None]:
    canonical = _required_text(event.get("canonical"), "event canonical")
    try:
        scope = Scope(event.get("scope"))
    except (TypeError, ValueError) as exc:
        raise ValueError("mapping event scope must be personal or project") from exc
    if scope not in {Scope.PERSONAL, Scope.PROJECT}:
        raise ValueError("mapping event scope must be personal or project")
    project_id = event.get("project_id")
    if scope is Scope.PROJECT:
        project_id = _project_id(project_id)
    elif project_id is not None:
        raise ValueError("personal mapping event must not have a project_id")
    return canonical, scope, project_id


def _mapping_baseline(
    event: dict[str, Any],
) -> LexiconEntry | None | object:
    if event.get("baseline_captured") is True:
        raw = event.get("baseline")
        baseline = None if raw is None else parse_entry(raw)
    else:
        raw = event.get("baseline")
        if raw is None:
            return _NO_BASELINE
        baseline = parse_entry(raw)
    if baseline is not None and _entry_key(baseline) != _raw_event_key(event):
        raise ValueError("event baseline identity does not match mapping event")
    return baseline


_NO_BASELINE = object()


def _migration_key(
    migration: dict[str, Any],
) -> tuple[str, Scope, str | None]:
    return _raw_event_key(migration)


def _migration_baseline(
    migration: dict[str, Any],
) -> LexiconEntry | None:
    raw = migration.get("baseline")
    baseline = None if raw is None else parse_entry(raw)
    if baseline is not None and _entry_key(baseline) != _migration_key(migration):
        raise ValueError("migration baseline identity does not match migration")
    return baseline


def _migration_records(
    events: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, event in enumerate(events):
        raw_records = event.get("migration_baselines", [])
        if not isinstance(raw_records, list):
            raise ValueError("migration_baselines must be a JSON array")
        for migration in raw_records:
            if not isinstance(migration, dict):
                raise ValueError("migration baseline must be a JSON object")
            event_ids = migration.get("event_ids")
            if not isinstance(event_ids, list) or any(
                not isinstance(event_id, str) for event_id in event_ids
            ):
                raise ValueError("migration event_ids must contain only strings")
            _migration_key(migration)
            _migration_baseline(migration)
            records.append((index, event, migration))
    return records


def _active_provenance_candidate(
    events: list[dict[str, Any]],
    active: list[dict[str, Any]],
) -> tuple[str, int, LexiconEntry | None, dict[str, Any]] | None:
    if not active:
        return None
    active_ids = {event["event_id"] for event in active}
    candidates: list[
        tuple[str, int, LexiconEntry | None, dict[str, Any]]
    ] = []
    for index, event in enumerate(events):
        if event.get("event_id") not in active_ids:
            continue
        baseline = _mapping_baseline(event)
        if baseline is not _NO_BASELINE:
            candidates.append(("mapping", index, baseline, event))
    for index, _owner, migration in _migration_records(events):
        if active_ids.intersection(migration["event_ids"]):
            candidates.append(
                ("migration", index, _migration_baseline(migration), migration)
            )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[1])


def _candidate_is_clean(
    candidate: tuple[str, int, LexiconEntry | None, dict[str, Any]],
    events: list[dict[str, Any]],
    cohort: list[dict[str, Any]],
) -> bool:
    kind, anchor_index, _baseline, owner = candidate
    if kind == "migration":
        return True
    if owner.get("baseline_migrated_event_ids"):
        return True
    positions = {
        event["event_id"]: index
        for index, event in enumerate(events)
        if isinstance(event.get("event_id"), str)
    }
    return not any(
        positions.get(event["event_id"], anchor_index) < anchor_index
        for event in cohort
        if event["event_id"] != owner.get("event_id")
    )


def _provenance_baselines(
    events: list[dict[str, Any]],
    active_events: list[dict[str, Any]],
) -> dict[tuple[str, Scope, str | None], LexiconEntry | None]:
    latest: dict[
        tuple[str, Scope, str | None],
        tuple[int, LexiconEntry | None],
    ] = {}
    for index, event in enumerate(events):
        if event.get("action") not in {"confirm", "reject"}:
            continue
        baseline = _mapping_baseline(event)
        if baseline is _NO_BASELINE:
            continue
        latest[_raw_event_key(event)] = (index, baseline)
    for index, _owner, migration in _migration_records(events):
        key = _migration_key(migration)
        previous = latest.get(key)
        if previous is None or index > previous[0]:
            latest[key] = (index, _migration_baseline(migration))

    active_by_key: dict[
        tuple[str, Scope, str | None], list[dict[str, Any]]
    ] = {}
    for event in active_events:
        active_by_key.setdefault(_raw_event_key(event), []).append(event)
    for key, active in active_by_key.items():
        candidate = _active_provenance_candidate(events, active)
        if candidate is not None:
            latest[key] = (candidate[1], candidate[2])
    return {key: value[1] for key, value in latest.items()}


def _pending_migration_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    consumed = {
        event.get("target_event_id")
        for event in events
        if event.get("action") == "migration_applied"
    }
    return [
        event
        for event in events
        if event.get("migration_baselines")
        and event.get("event_id") not in consumed
    ]


def _expand_mapping_targets(
    targets: tuple[dict[str, Any], ...],
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for target in targets:
        action = target.get("action")
        if action in {"confirm", "reject"}:
            expanded.append(target)
        elif action == "delete":
            target_ids = target.get("target_event_ids", [])
            if not isinstance(target_ids, list):
                raise ValueError("delete target_event_ids must be a JSON array")
            for target_id in target_ids:
                mapping = by_id.get(target_id)
                if mapping is not None and mapping.get("action") in {
                    "confirm",
                    "reject",
                }:
                    expanded.append(mapping)
    return _unique_events(expanded)


def _unique_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise ValueError("learning event_id must be a string")
        unique.setdefault(event_id, event)
    return list(unique.values())


def _common_event_location(
    events: list[dict[str, Any]],
) -> tuple[Scope | None, str | None]:
    locations = {
        (_raw_event_key(event)[1], _raw_event_key(event)[2])
        for event in events
    }
    if len(locations) != 1:
        return None, None
    return next(iter(locations))


def _contains_contributions(
    entry: LexiconEntry, events: list[dict[str, Any]]
) -> bool:
    return all(
        (
            event["alias"] in entry.aliases
            if event["action"] == "confirm"
            else event["alias"] in entry.negative_aliases
        )
        for event in events
    )


def _same_except_source(left: LexiconEntry, right: LexiconEntry) -> bool:
    return (
        left.canonical,
        left.scope,
        left.aliases,
        left.domains,
        left.weight,
        left.status,
        left.phonetics,
        left.project_id,
        left.use_count,
        left.notes,
        left.negative_aliases,
    ) == (
        right.canonical,
        right.scope,
        right.aliases,
        right.domains,
        right.weight,
        right.status,
        right.phonetics,
        right.project_id,
        right.use_count,
        right.notes,
        right.negative_aliases,
    )


def _conservative_baseline(
    entry: LexiconEntry | None, events: tuple[dict[str, Any], ...]
) -> LexiconEntry | None:
    """Separate the known event overlay without inventing an unknowable baseline."""
    if entry is None:
        return None
    derived = _entries_from_learning_events(list(events), _LEARNING_SOURCE)
    if len(derived) == 1 and _same_except_source(entry, derived[0]):
        return None
    baseline = _remove_historical_contributions(entry, events)
    if baseline is None or baseline.source != _LEARNING_SOURCE:
        return baseline
    raw = _entry_snapshot(baseline)
    raw["source"] = None
    return parse_entry(raw)


def _remove_historical_contributions(
    entry: LexiconEntry, events: tuple[dict[str, Any], ...]
) -> LexiconEntry | None:
    """Remove only pre-snapshot aliases explicitly owned by audit events."""
    aliases = {
        event["alias"] for event in events if event["action"] == "confirm"
    }
    negative_aliases = {
        event["alias"] for event in events if event["action"] == "reject"
    }
    remaining_aliases = tuple(alias for alias in entry.aliases if alias not in aliases)
    if not remaining_aliases:
        return None
    return LexiconEntry(
        canonical=entry.canonical,
        scope=entry.scope,
        aliases=remaining_aliases,
        domains=entry.domains,
        weight=entry.weight,
        status=entry.status,
        phonetics=entry.phonetics,
        project_id=entry.project_id,
        source=entry.source,
        use_count=entry.use_count,
        notes=entry.notes,
        negative_aliases=tuple(
            alias for alias in entry.negative_aliases if alias not in negative_aliases
        ),
    )


def _merge_overlay(existing: LexiconEntry, learned: LexiconEntry) -> LexiconEntry:
    """Apply one deterministic learning overlay to its preserved baseline."""
    if (existing.canonical, existing.scope, existing.project_id) != (
        learned.canonical,
        learned.scope,
        learned.project_id,
    ):
        raise ValueError("only equal lexicon identities can be merged")
    return LexiconEntry(
        canonical=existing.canonical,
        scope=existing.scope,
        aliases=_unique([*existing.aliases, *learned.aliases]),
        domains=_unique([*existing.domains, *learned.domains]),
        weight=max(existing.weight, learned.weight),
        status=learned.status,
        phonetics=_unique([*existing.phonetics, *learned.phonetics]),
        project_id=existing.project_id,
        source=_LEARNING_SOURCE,
        use_count=existing.use_count,
        notes=existing.notes,
        negative_aliases=_unique(
            [*existing.negative_aliases, *learned.negative_aliases]
        ),
    )

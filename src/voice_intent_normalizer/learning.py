"""Explicit, auditable V1 learning controls for voice-intent lexicons."""

from __future__ import annotations

import json
import os
import re
import tempfile
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
        re.compile(
            r"^我说的是\s*(?P<canonical>.+?)\s*[,，]\s*不是\s*(?P<alias>.+?)$"
        ),
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

_SCHEMA_VERSION = 1
_LEARNING_SOURCE = "explicit-learning-v1"
_LEARNING_ACTIONS = frozenset({"confirm", "reject", "delete"})
_MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_MAX_EVENT_BYTES = 64 * 1024
_MAX_EVENTS = 20_000
_COMMON_EVENT_FIELDS = frozenset(
    {
        "action",
        "alias",
        "canonical",
        "event_id",
        "project_id",
        "schema_version",
        "scope",
        "source",
        "status",
        "timestamp",
    }
)
_ACTION_FIELDS = {
    "observe": _COMMON_EVENT_FIELDS,
    "confirm": _COMMON_EVENT_FIELDS | {"baseline", "generation_id"},
    "reject": _COMMON_EVENT_FIELDS | {"baseline", "generation_id"},
    "delete": _COMMON_EVENT_FIELDS | {"target_event_ids"},
    "undo": _COMMON_EVENT_FIELDS | {"target_event_id"},
}
_SNAPSHOT_FIELDS = frozenset(
    {
        "aliases",
        "canonical",
        "domains",
        "negative_aliases",
        "notes",
        "phonetics",
        "project_id",
        "scope",
        "source",
        "status",
        "use_count",
        "weight",
    }
)


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
    """A strict V1 event journal with deterministically derived lexicons."""

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
        """Persist an explicit positive mapping in personal or project scope."""
        self._replay_and_verify()
        resolved_scope = Scope(scope)
        if resolved_scope not in {Scope.PERSONAL, Scope.PROJECT}:
            raise ValueError("confirmed mappings must be personal or project scoped")
        if resolved_scope is Scope.PROJECT:
            project_id = _project_id(project_id)
        elif project_id is not None:
            raise ValueError("personal mappings must not have a project_id")
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        generation_id, baseline = self._capture_generation(
            resolved_scope, project_id, canonical
        )
        event = self._append_event(
            "confirm",
            alias,
            canonical,
            EntryStatus.CONFIRMED,
            scope=resolved_scope,
            project_id=project_id,
            generation_id=generation_id,
            baseline=baseline,
        )
        self._replay_and_verify()
        return event

    def observe(self, alias: str, canonical: str, source: str) -> EntryStatus:
        """Record an unconfirmed sighting without changing active lexicons."""
        self._replay_and_verify()
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        source = _required_text(source, "source")
        status = (
            EntryStatus.REPEATED
            if self._latest_observation(alias, canonical) is not None
            else EntryStatus.CANDIDATE
        )
        self._append_event(
            "observe",
            alias,
            canonical,
            status,
            source=source,
        )
        return status

    def reject(self, alias: str, canonical: str) -> LearningEvent:
        """Persist an explicit personal negative mapping."""
        self._replay_and_verify()
        alias = _required_text(alias, "alias")
        canonical = _required_text(canonical, "canonical")
        generation_id, baseline = self._capture_generation(
            Scope.PERSONAL, None, canonical
        )
        event = self._append_event(
            "reject",
            alias,
            canonical,
            EntryStatus.REJECTED,
            scope=Scope.PERSONAL,
            generation_id=generation_id,
            baseline=baseline,
        )
        self._replay_and_verify()
        return event

    def undo_last(self) -> LearningEvent | None:
        """Append a compensating event for the latest effective learning action."""
        self._replay_and_verify()
        events = self._events()
        operations = _effective_operations(events)
        if not operations:
            return None
        target = operations[-1]
        event = _event_from_raw(target)
        self._append_event(
            "undo",
            event.alias,
            event.canonical,
            event.status,
            scope=event.scope,
            project_id=event.project_id,
            target_event_id=event.event_id,
        )
        self._replay_and_verify()
        return event

    def personal_entries(self) -> tuple[LexiconEntry, ...]:
        """Return active personal entries, or no entries before first use."""
        if not self.personal_file.is_file():
            return ()
        return load_jsonl(self.personal_file, expected_scope=Scope.PERSONAL)

    def list_recent(self, limit: int = 20) -> tuple[LearningEvent, ...]:
        """Return active mapping records in newest-first order."""
        if limit <= 0:
            return ()
        events = [
            _event_from_raw(event)
            for event in _active_mapping_events(self._events())
        ]
        return tuple(reversed(events[-limit:]))

    def delete(self, canonical: str) -> LearningEvent | None:
        """Append a deletion targeting the exact active mapping event IDs."""
        self._replay_and_verify()
        canonical = _required_text(canonical, "canonical")
        target_events = [
            event
            for event in _active_mapping_events(self._events())
            if event["canonical"] == canonical
        ]
        if not target_events:
            return None
        scope, project_id = _common_location(target_events)
        event = self._append_event(
            "delete",
            canonical,
            canonical,
            EntryStatus.CONFIRMED,
            scope=scope,
            project_id=project_id,
            target_event_ids=[event["event_id"] for event in target_events],
        )
        self._replay_and_verify()
        return event

    def export(self, path: str | Path) -> None:
        """Atomically export the currently materialized personal lexicon."""
        self._replay_and_verify()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = self.personal_entries()
        write_jsonl_atomic(destination, expected)
        try:
            actual = load_jsonl(destination, expected_scope=Scope.PERSONAL)
        except ValueError as exc:
            raise RuntimeError(
                f"export verification failed for {destination}"
            ) from exc
        if actual != expected:
            raise RuntimeError(f"export verification failed for {destination}")

    def _append_event(
        self,
        action: str,
        alias: str,
        canonical: str,
        status: EntryStatus,
        *,
        scope: Scope | None = None,
        project_id: str | None = None,
        source: str | None = None,
        generation_id: str | None = None,
        baseline: LexiconEntry | None = None,
        target_event_id: str | None = None,
        target_event_ids: list[str] | None = None,
    ) -> LearningEvent:
        event = LearningEvent(
            event_id=str(uuid.uuid4()),
            timestamp=_timestamp(),
            action=action,
            alias=_required_text(alias, "alias"),
            canonical=_required_text(canonical, "canonical"),
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
            "schema_version": _SCHEMA_VERSION,
            "scope": event.scope.value if event.scope is not None else None,
            "source": source,
            "status": event.status.value,
            "timestamp": event.timestamp,
        }
        if action in {"confirm", "reject"}:
            raw["baseline"] = (
                None if baseline is None else _entry_snapshot(baseline)
            )
            raw["generation_id"] = generation_id
        elif action == "delete":
            raw["target_event_ids"] = target_event_ids
        elif action == "undo":
            raw["target_event_id"] = target_event_id
        self._append_raw_atomic(raw)
        return event

    def _append_raw_atomic(self, raw: dict[str, Any]) -> None:
        """Atomically replace the bounded journal with one additional V1 event."""
        existing = self._events()
        seen = {event["event_id"]: event for event in existing}
        _validate_v1_event(raw, seen, _undone_ids(existing))
        combined = [*existing, raw]
        _validate_generations(combined)

        encoded = _serialized_event(raw)
        if len(encoded.rstrip(b"\n")) > _MAX_EVENT_BYTES:
            raise ValueError(f"event exceeds {_MAX_EVENT_BYTES} bytes")
        if len(existing) >= _MAX_EVENTS:
            raise ValueError(f"journal exceeds {_MAX_EVENTS} events")
        prior = self.events_file.read_bytes() if self.events_file.is_file() else b""
        separator = b"\n" if prior and not prior.endswith(b"\n") else b""
        if len(prior) + len(separator) + len(encoded) > _MAX_JOURNAL_BYTES:
            raise ValueError(f"journal exceeds {_MAX_JOURNAL_BYTES} bytes")

        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.events_file.name}.",
            suffix=".tmp",
            dir=self.root,
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(prior)
                temporary.write(separator)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.events_file)
            if self._events() != combined:
                raise RuntimeError("journal append verification failed")
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _events(self) -> list[dict[str, Any]]:
        """Load and strictly validate the complete bounded V1 journal."""
        if not self.events_file.is_file():
            return []
        try:
            content = self.events_file.read_bytes()
        except OSError as exc:
            raise ValueError(f"{self.events_file}: unable to read journal") from exc
        if len(content) > _MAX_JOURNAL_BYTES:
            raise ValueError(
                f"{self.events_file}: journal exceeds {_MAX_JOURNAL_BYTES} bytes"
            )
        lines = content.splitlines()
        if len(lines) > _MAX_EVENTS:
            raise ValueError(
                f"{self.events_file}: journal exceeds {_MAX_EVENTS} events"
            )

        events: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        undone: set[str] = set()
        for line_number, encoded in enumerate(lines, start=1):
            if len(encoded) > _MAX_EVENT_BYTES:
                raise ValueError(
                    f"{self.events_file}: line {line_number}: event exceeds "
                    f"{_MAX_EVENT_BYTES} bytes"
                )
            if not encoded:
                raise ValueError(f"{self.events_file}: line {line_number}: blank event")
            try:
                line = encoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"{self.events_file}: line {line_number}: invalid UTF-8"
                ) from exc
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{self.events_file}: line {line_number}: invalid JSON"
                ) from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    f"{self.events_file}: line {line_number}: event must be an object"
                )
            try:
                _validate_v1_event(raw, seen, undone)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{self.events_file}: line {line_number}: {exc}"
                ) from exc
            events.append(raw)
            seen[raw["event_id"]] = raw
            if raw["action"] == "undo":
                undone.add(raw["target_event_id"])
        _validate_generations(events)
        return events

    def _latest_observation(
        self, alias: str, canonical: str
    ) -> dict[str, Any] | None:
        for event in reversed(self._events()):
            if (
                event["action"] == "observe"
                and event["alias"] == alias
                and event["canonical"] == canonical
            ):
                return event
        return None

    def _entry_at(
        self, scope: Scope, project_id: str | None, canonical: str
    ) -> LexiconEntry | None:
        path = _lexicon_path(self.root, scope, project_id)
        if not path.is_file():
            return None
        key = (canonical, scope, project_id)
        return next(
            (
                entry
                for entry in load_jsonl(path, expected_scope=scope)
                if _entry_key(entry) == key
            ),
            None,
        )

    def _capture_generation(
        self, scope: Scope, project_id: str | None, canonical: str
    ) -> tuple[str, LexiconEntry | None]:
        """Reuse active ownership or capture a complete new baseline from disk."""
        events = self._events()
        key = (canonical, scope, project_id)
        active = [
            event
            for event in _active_mapping_events(events)
            if _raw_event_key(event) == key
        ]
        current = self._entry_at(scope, project_id, canonical)
        if active:
            generation_id = active[0]["generation_id"]
            baseline = _baseline_from_event(active[0])
            _assert_active_current(key, current, baseline)
            return generation_id, baseline
        if current is not None and current.source == _LEARNING_SOURCE:
            raise RuntimeError(
                f"stale V1-owned materialization for {canonical!r}; replay required"
            )
        return str(uuid.uuid4()), current

    def _materialize(self) -> None:
        """Derive all V1-owned overlays while preserving unrelated disk records."""
        events = self._events()
        active_events = _active_mapping_events(events)
        active_by_key: dict[
            tuple[str, Scope, str | None], list[dict[str, Any]]
        ] = {}
        for event in active_events:
            active_by_key.setdefault(_raw_event_key(event), []).append(event)
        latest_baselines = _latest_baselines(events)

        locations = {
            (key[1], key[2])
            for key in latest_baselines
        }
        for scope, project_id in sorted(
            locations,
            key=lambda item: (item[0].value, item[1] or ""),
        ):
            path = _lexicon_path(self.root, scope, project_id)
            current_entries = (
                load_jsonl(path, expected_scope=scope) if path.is_file() else ()
            )
            merged = {_entry_key(entry): entry for entry in current_entries}
            location_keys = [
                key
                for key in latest_baselines
                if key[1] is scope and key[2] == project_id
            ]
            for key in location_keys:
                current = merged.get(key)
                active = active_by_key.get(key, [])
                if active:
                    baseline = _baseline_from_event(active[0])
                    _assert_active_current(key, current, baseline)
                    merged[key] = _overlay_entry(baseline, active)
                elif current is not None and current.source == _LEARNING_SOURCE:
                    baseline = latest_baselines[key]
                    if baseline is None:
                        merged.pop(key)
                    else:
                        merged[key] = baseline
            expected = tuple(merged.values())
            if expected != current_entries:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_jsonl_atomic(path, expected)
            try:
                actual = (
                    load_jsonl(path, expected_scope=scope)
                    if path.is_file()
                    else ()
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"materialization verification failed for {path}"
                ) from exc
            if actual != expected:
                raise RuntimeError(
                    f"materialization verification failed for {path}"
                )

    def _replay_and_verify(self) -> None:
        """Reconcile the authoritative V1 journal before exposing new state."""
        self._materialize()


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    return value.strip()


def _project_id(value: Any) -> str:
    project_id = _required_text(value, "project_id")
    if (
        len(project_id) > 128
        or project_id in {".", ".."}
        or "/" in project_id
        or "\\" in project_id
    ):
        raise ValueError("project_id must be one safe path segment")
    return project_id


def _serialized_event(raw: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


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


def _entry_key(entry: LexiconEntry) -> tuple[str, Scope, str | None]:
    return (entry.canonical, entry.scope, entry.project_id)


def _raw_event_key(event: dict[str, Any]) -> tuple[str, Scope, str | None]:
    return (
        event["canonical"],
        Scope(event["scope"]),
        event["project_id"],
    )


def _baseline_from_event(event: dict[str, Any]) -> LexiconEntry | None:
    baseline = event["baseline"]
    return None if baseline is None else parse_entry(baseline)


def _event_from_raw(raw: dict[str, Any]) -> LearningEvent:
    return LearningEvent(
        event_id=raw["event_id"],
        timestamp=raw["timestamp"],
        action=raw["action"],
        alias=raw["alias"],
        canonical=raw["canonical"],
        status=EntryStatus(raw["status"]),
        scope=Scope(raw["scope"]) if raw["scope"] is not None else None,
        project_id=raw["project_id"],
    )


def _lexicon_path(
    root: Path, scope: Scope, project_id: str | None
) -> Path:
    if scope is Scope.PERSONAL:
        return root / "personal.jsonl"
    if scope is Scope.PROJECT:
        assert project_id is not None
        return root / "projects" / project_id / "project.jsonl"
    raise ValueError("learning materialization supports personal and project scopes")


def _undone_ids(events: list[dict[str, Any]]) -> set[str]:
    return {
        event["target_event_id"]
        for event in events
        if event["action"] == "undo"
    }


def _effective_operations(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    undone = _undone_ids(events)
    return [
        event
        for event in events
        if event["action"] in _LEARNING_ACTIONS
        and event["event_id"] not in undone
    ]


def _active_mapping_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for event in _effective_operations(events):
        if event["action"] in {"confirm", "reject"}:
            active[event["event_id"]] = event
        else:
            for target in event["target_event_ids"]:
                active.pop(target, None)
    return list(active.values())


def _latest_baselines(
    events: list[dict[str, Any]],
) -> dict[tuple[str, Scope, str | None], LexiconEntry | None]:
    latest: dict[
        tuple[str, Scope, str | None], tuple[str, LexiconEntry | None]
    ] = {}
    for event in events:
        if event["action"] not in {"confirm", "reject"}:
            continue
        key = _raw_event_key(event)
        generation_id = event["generation_id"]
        if key not in latest or latest[key][0] != generation_id:
            latest[key] = (generation_id, _baseline_from_event(event))
    return {key: owned[1] for key, owned in latest.items()}


def _common_location(
    events: list[dict[str, Any]],
) -> tuple[Scope | None, str | None]:
    locations = {
        (_raw_event_key(event)[1], _raw_event_key(event)[2])
        for event in events
    }
    if len(locations) != 1:
        return None, None
    return next(iter(locations))


def _assert_active_current(
    key: tuple[str, Scope, str | None],
    current: LexiconEntry | None,
    baseline: LexiconEntry | None,
) -> None:
    if current == baseline:
        return
    if current is not None and current.source == _LEARNING_SOURCE:
        return
    raise RuntimeError(
        "external edit conflict for active V1 learning identity "
        f"{key[0]!r} ({key[1].value})"
    )


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _overlay_entry(
    baseline: LexiconEntry | None,
    events: list[dict[str, Any]],
) -> LexiconEntry:
    first = events[0]
    scope = Scope(first["scope"])
    project_id = first["project_id"]
    canonical = first["canonical"]
    has_confirmation = any(event["action"] == "confirm" for event in events)
    aliases = list(
        baseline.aliases
        if baseline is not None
        else (() if has_confirmation else (canonical,))
    )
    negative_aliases = list(
        baseline.negative_aliases if baseline is not None else ()
    )
    for event in events:
        if event["action"] == "confirm":
            aliases.append(event["alias"])
        else:
            negative_aliases.append(event["alias"])
    return LexiconEntry(
        canonical=canonical,
        scope=scope,
        aliases=_unique(aliases),
        domains=baseline.domains if baseline is not None else (),
        weight=max(baseline.weight if baseline is not None else 0.0, 1.0),
        status=(
            EntryStatus.CONFIRMED if has_confirmation else EntryStatus.REJECTED
        ),
        phonetics=baseline.phonetics if baseline is not None else (),
        project_id=project_id,
        source=_LEARNING_SOURCE,
        use_count=baseline.use_count if baseline is not None else None,
        notes=baseline.notes if baseline is not None else None,
        negative_aliases=_unique(negative_aliases),
    )


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID string") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ValueError(f"{field_name} must be a canonical UUID4 string")
    return value


def _validate_timestamp(value: Any) -> None:
    timestamp = _required_text(value, "timestamp")
    if not timestamp.endswith("Z"):
        raise ValueError("timestamp must be UTC RFC3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be UTC RFC3339 ending in Z") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must be UTC RFC3339 ending in Z")


def _validate_v1_event(
    raw: dict[str, Any],
    seen: dict[str, dict[str, Any]],
    undone: set[str],
) -> None:
    """Validate one exact V1 action and all of its backward references."""
    if raw.get("schema_version") != _SCHEMA_VERSION or isinstance(
        raw.get("schema_version"), bool
    ):
        raise ValueError("schema_version must be 1")
    action = raw.get("action")
    if action not in _ACTION_FIELDS:
        raise ValueError("action is not supported by schema_version 1")
    expected_fields = _ACTION_FIELDS[action]
    missing = expected_fields - set(raw)
    unknown = set(raw) - expected_fields
    if missing:
        raise ValueError(f"missing event fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown event fields: {', '.join(sorted(unknown))}")

    event_id = _validate_uuid(raw["event_id"], "event_id")
    if event_id in seen:
        raise ValueError("event_id must be unique")
    _validate_timestamp(raw["timestamp"])
    alias = _required_text(raw["alias"], "alias")
    canonical = _required_text(raw["canonical"], "canonical")
    try:
        status = EntryStatus(raw["status"])
    except (TypeError, ValueError) as exc:
        raise ValueError("status is invalid") from exc
    scope_value = raw["scope"]
    try:
        scope = None if scope_value is None else Scope(scope_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("scope is invalid") from exc
    project_id = raw["project_id"]
    if project_id is not None:
        project_id = _project_id(project_id)
    source = raw["source"]
    if source is not None:
        source = _required_text(source, "source")

    if action == "observe":
        if scope is not None or project_id is not None:
            raise ValueError("observe must not have a scope or project_id")
        if status not in {EntryStatus.CANDIDATE, EntryStatus.REPEATED}:
            raise ValueError("observe status must be candidate or repeated")
        if source is None:
            raise ValueError("observe source must be explicit")
        return

    if source is not None:
        raise ValueError(f"{action} source must be null")
    if action == "confirm":
        if scope not in {Scope.PERSONAL, Scope.PROJECT}:
            raise ValueError("confirm scope must be personal or project")
        if status is not EntryStatus.CONFIRMED:
            raise ValueError("confirm status must be confirmed")
    elif action == "reject":
        if scope is not Scope.PERSONAL or project_id is not None:
            raise ValueError("reject scope must be personal")
        if status is not EntryStatus.REJECTED:
            raise ValueError("reject status must be rejected")
    elif action == "delete":
        if status is not EntryStatus.CONFIRMED or alias != canonical:
            raise ValueError("delete identity or status is invalid")
    elif status not in {EntryStatus.CONFIRMED, EntryStatus.REJECTED}:
        raise ValueError("undo status is invalid")

    if scope is Scope.PROJECT and project_id is None:
        raise ValueError("project scope requires project_id")
    if scope is Scope.PERSONAL and project_id is not None:
        raise ValueError("personal scope must not have project_id")
    if scope is None and project_id is not None:
        raise ValueError("project_id requires project scope")

    if action in {"confirm", "reject"}:
        _validate_uuid(raw["generation_id"], "generation_id")
        baseline_raw = raw["baseline"]
        if baseline_raw is not None:
            if not isinstance(baseline_raw, dict):
                raise ValueError("baseline must be a complete entry object or null")
            if set(baseline_raw) != _SNAPSHOT_FIELDS:
                raise ValueError("baseline must be a complete entry snapshot")
            baseline = parse_entry(baseline_raw, expected_scope=scope)
            if baseline.source == _LEARNING_SOURCE:
                raise ValueError("baseline must not contain the V1 ownership marker")
            if _entry_key(baseline) != (canonical, scope, project_id):
                raise ValueError("baseline identity does not match mapping event")
        return

    if action == "delete":
        targets = raw["target_event_ids"]
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(target, str) for target in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("delete target_event_ids must be unique event IDs")
        if any(
            target not in seen
            or seen[target]["action"] not in {"confirm", "reject"}
            for target in targets
        ):
            raise ValueError("delete targets must reference prior mapping events")
        return

    target = raw["target_event_id"]
    if (
        not isinstance(target, str)
        or target not in seen
        or seen[target]["action"] not in _LEARNING_ACTIONS
    ):
        raise ValueError("undo target must reference a prior learning action")
    if target in undone:
        raise ValueError("learning action must not be undone more than once")
    target_event = seen[target]
    if (
        alias != target_event["alias"]
        or canonical != target_event["canonical"]
        or raw["status"] != target_event["status"]
        or raw["scope"] != target_event["scope"]
        or project_id != target_event["project_id"]
    ):
        raise ValueError("undo identity must match its target action")


def _validate_generations(events: list[dict[str, Any]]) -> None:
    """Require one identity/baseline per generation and per active identity."""
    owners: dict[str, tuple[tuple[str, Scope, str | None], Any]] = {}
    for event in events:
        if event["action"] not in {"confirm", "reject"}:
            continue
        owner = (_raw_event_key(event), event["baseline"])
        previous = owners.setdefault(event["generation_id"], owner)
        if previous != owner:
            raise ValueError(
                "generation_id must use one identity and one complete baseline"
            )

    active_owners: dict[
        tuple[str, Scope, str | None], tuple[str, Any]
    ] = {}
    for event in _active_mapping_events(events):
        key = _raw_event_key(event)
        owner = (event["generation_id"], event["baseline"])
        previous = active_owners.setdefault(key, owner)
        if previous != owner:
            raise ValueError(
                "active identity must have one generation_id and one baseline"
            )

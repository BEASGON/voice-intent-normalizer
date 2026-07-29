from __future__ import annotations

import json

import pytest

from voice_intent_normalizer.learning import LearningStore, parse_control
from voice_intent_normalizer.lexicon import load_jsonl
from voice_intent_normalizer.models import EntryStatus, Scope


@pytest.mark.parametrize(
    ("text", "kind", "alias", "canonical"),
    [
        ("我说的是 OpenClaw，不是 Open Cloud。", "confirm", "Open Cloud", "OpenClaw"),
        ("以后把 Work body 理解为 WorkBuddy。", "confirm", "Work body", "WorkBuddy"),
        ("不要把龙虾改成 OpenClaw。", "reject", "龙虾", "OpenClaw"),
    ],
)
def test_parse_control(text: str, kind: str, alias: str, canonical: str):
    """Catch a control parser that misses an explicit feedback pattern."""
    command = parse_control(text)

    assert command is not None
    assert command.kind == kind
    assert command.alias == alias
    assert command.canonical == canonical


def test_undo_removes_last_learning_event(tmp_path):
    """Catch undo that leaves the last confirmed mapping active."""
    store = LearningStore.for_root(tmp_path)
    store.confirm("Open Cloud", "OpenClaw", Scope.PERSONAL)

    event = store.undo_last()

    assert event is not None
    assert event.canonical == "OpenClaw"
    assert store.list_recent() == ()


def test_repetition_does_not_silently_confirm(tmp_path):
    """Catch observations becoming active lexicon mappings without confirmation."""
    store = LearningStore.for_root(tmp_path)

    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.CANDIDATE
    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.REPEATED
    assert store.personal_entries() == ()


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("撤销刚才的纠正。", "undo"),
        ("删除你学到的这个词。", "delete"),
        ("查看最近学到的词。", "list_recent"),
    ],
)
def test_parse_control_recognizes_non_mapping_controls(text: str, kind: str):
    """Catch explicit management controls being treated as ordinary text."""
    command = parse_control(text)

    assert command is not None
    assert command.kind == kind
    assert command.alias is None
    assert command.canonical is None


def test_parse_control_accepts_explicit_not_a_b_correction_with_mixed_punctuation():
    """Catch loss of an explicit correction when Chinese and English punctuation mix."""
    command = parse_control("不是 Open Cloud, 是 OpenClaw!  ")

    assert command is not None
    assert command.kind == "confirm"
    assert command.alias == "Open Cloud"
    assert command.canonical == "OpenClaw"


def test_parse_control_preserves_ordinary_sentences():
    """Catch permanent learning inferred from text that lacks a control grammar."""
    assert parse_control("请帮我为 Open Cloud 写一个介绍。") is None


def test_confirm_appends_auditable_event_and_materializes_personal_entry(tmp_path):
    """Catch confirmed feedback that is not persisted in the active personal lexicon."""
    store = LearningStore.for_root(tmp_path)

    event = store.confirm("Open Cloud", "OpenClaw", Scope.PERSONAL)

    assert event.status is EntryStatus.CONFIRMED
    assert event.scope is Scope.PERSONAL
    assert event.timestamp.endswith("Z")
    assert store.personal_entries()[0].aliases == ("Open Cloud",)
    assert store.personal_entries()[0].canonical == "OpenClaw"
    assert store.list_recent() == (event,)
    raw_event = json.loads((tmp_path / "learning-events.jsonl").read_text("utf-8"))
    assert raw_event["event_id"] == event.event_id
    assert raw_event["action"] == "confirm"


def test_confirm_materializes_project_entry_in_its_isolated_lexicon(tmp_path):
    """Catch a project confirmation leaking into shared personal state."""
    store = LearningStore.for_root(tmp_path)

    store.confirm("Work body", "WorkBuddy", Scope.PROJECT, project_id="project-7")

    assert store.personal_entries() == ()
    entries = load_jsonl(tmp_path / "projects" / "project-7" / "project.jsonl")
    assert entries[0].scope is Scope.PROJECT
    assert entries[0].project_id == "project-7"


def test_reject_materializes_a_negative_personal_mapping(tmp_path):
    """Catch rejection that fails to persist the alias that must not be corrected."""
    store = LearningStore.for_root(tmp_path)

    event = store.reject("龙虾", "OpenClaw")

    assert event.status is EntryStatus.REJECTED
    entry = store.personal_entries()[0]
    assert entry.canonical == "OpenClaw"
    assert entry.negative_aliases == ("龙虾",)
    assert entry.aliases == ("OpenClaw",)


def test_delete_rebuilds_personal_lexicon_and_export_copies_active_entries(tmp_path):
    """Catch deletion or export leaving obsolete confirmed mappings visible."""
    store = LearningStore.for_root(tmp_path)
    store.confirm("Open Cloud", "OpenClaw", Scope.PERSONAL)
    export_path = tmp_path / "exports" / "personal.jsonl"

    store.export(export_path)
    event = store.delete("OpenClaw")

    assert event is not None
    assert event.action == "delete"
    assert store.personal_entries() == ()
    exported = load_jsonl(export_path, expected_scope=Scope.PERSONAL)
    assert exported[0].canonical == "OpenClaw"

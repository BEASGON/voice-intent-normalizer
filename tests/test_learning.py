from __future__ import annotations

import json

import pytest

from voice_intent_normalizer.learning import LearningStore, parse_control
from voice_intent_normalizer.lexicon import LexiconSet, load_jsonl, write_jsonl_atomic
from voice_intent_normalizer.matching import MatchContext, generate_candidates
from voice_intent_normalizer.models import (
    DecisionAction,
    EntryStatus,
    LexiconEntry,
    Scope,
)
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.policy import decide
from voice_intent_normalizer.project_scan import scan_project


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


def test_project_confirmation_preserves_scanner_entries(tmp_path):
    """Catch learning materialization replacing the project scanner's cache."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# ExistingWidget\n", encoding="utf-8")
    scan_project(workspace, paths)
    project_id = paths.for_project(workspace).project_id

    LearningStore.for_root(paths.root).confirm(
        "widget engine", "WidgetEngine", Scope.PROJECT, project_id=project_id
    )

    entries = load_jsonl(paths.for_project(workspace).lexicon_file, Scope.PROJECT)
    by_canonical = {entry.canonical: entry for entry in entries}
    assert by_canonical["ExistingWidget"].source == "README.md"
    assert by_canonical["WidgetEngine"].aliases == ("widget engine",)
    assert len(by_canonical) == len(entries)


def test_personal_confirmation_preserves_manually_curated_entries(tmp_path):
    """Catch learning materialization replacing personal curated lexicon records."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    paths.root.mkdir()
    write_jsonl_atomic(
        paths.personal_file,
        (
            LexiconEntry(
                canonical="ExistingTool",
                scope=Scope.PERSONAL,
                aliases=("existing tool",),
                domains=("ai",),
                weight=0.8,
                status=EntryStatus.CURATED,
                source="manual",
            ),
        ),
    )

    LearningStore.for_root(paths.root).confirm(
        "new tool", "NewTool", Scope.PERSONAL
    )

    entries = {entry.canonical: entry for entry in load_jsonl(paths.personal_file)}
    assert entries["ExistingTool"].source == "manual"
    assert entries["NewTool"].aliases == ("new tool",)


def test_rejected_mapping_suppresses_the_same_lower_layer_mapping(tmp_path):
    """Catch a personal rejection that leaves a base alias eligible to apply."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    builtins = tmp_path / "builtins"
    base_file = builtins / "base-zh.jsonl"
    base_file.parent.mkdir()
    write_jsonl_atomic(
        base_file,
        (
            LexiconEntry(
                canonical="OpenClaw",
                scope=Scope.BASE,
                aliases=("龙虾",),
                domains=("ai",),
                weight=0.9,
                status=EntryStatus.CURATED,
            ),
        ),
    )
    LearningStore.for_root(paths.root).reject("龙虾", "OpenClaw")
    text = "给龙虾安装 Agent 技能"
    candidates = generate_candidates(
        text,
        LexiconSet.load(paths, builtins),
        MatchContext(
            domains=frozenset({"ai"}), conversation_terms=frozenset({"OpenClaw"})
        ),
    )

    decision = decide(
        text,
        candidates,
        MatchContext(
            domains=frozenset({"ai"}), conversation_terms=frozenset({"OpenClaw"})
        ),
    )

    assert decision.action is DecisionAction.KEEP
    blocked = [
        candidate
        for candidate in candidates
        if candidate.canonical == "OpenClaw" and candidate.original == "龙虾"
    ]
    assert blocked
    assert all("alias:negative" in candidate.evidence for candidate in blocked)


def test_rejected_mapping_does_not_suppress_a_different_canonical_target(tmp_path):
    """Catch a negative alias blocking unrelated corrections with the same wording."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    builtins = tmp_path / "builtins"
    base_file = builtins / "base-zh.jsonl"
    base_file.parent.mkdir()
    write_jsonl_atomic(
        base_file,
        (
            LexiconEntry(
                canonical="OpenClaw",
                scope=Scope.BASE,
                aliases=("龙虾",),
                domains=("ai",),
                weight=0.9,
                status=EntryStatus.CURATED,
            ),
            LexiconEntry(
                canonical="LobsterTool",
                scope=Scope.BASE,
                aliases=("龙虾",),
                domains=("ai",),
                weight=0.9,
                status=EntryStatus.CURATED,
            ),
        ),
    )
    LearningStore.for_root(paths.root).reject("龙虾", "OpenClaw")
    text = "给龙虾安装 Agent 技能"
    context = MatchContext(domains=frozenset({"ai"}))

    decision = decide(
        text,
        generate_candidates(text, LexiconSet.load(paths, builtins), context),
        context,
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "给LobsterTool安装 Agent 技能"

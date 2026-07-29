from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import voice_intent_normalizer.learning as learning_module
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


def _read_events(root: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (root / "learning-events.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]


def _write_events(root: Path, events: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "learning-events.jsonl").write_text(
        "".join(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def _manual_entry(
    canonical: str = "OpenClaw",
    *,
    aliases: tuple[str, ...] = ("curated",),
    scope: Scope = Scope.PERSONAL,
    project_id: str | None = None,
    source: str | None = "manual",
    notes: str | None = "preserve exactly",
) -> LexiconEntry:
    return LexiconEntry(
        canonical=canonical,
        scope=scope,
        aliases=aliases,
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CURATED,
        project_id=project_id,
        source=source,
        use_count=7,
        notes=notes,
        negative_aliases=("never-this",),
    )


@pytest.mark.parametrize(
    ("text", "kind", "alias", "canonical"),
    [
        ("我说的是 OpenClaw，不是 Open Cloud。", "confirm", "Open Cloud", "OpenClaw"),
        ("以后把 Work body 理解为 WorkBuddy。", "confirm", "Work body", "WorkBuddy"),
        ("不要把龙虾改成 OpenClaw。", "reject", "龙虾", "OpenClaw"),
        ("不是 Open Cloud, 是 OpenClaw!", "confirm", "Open Cloud", "OpenClaw"),
    ],
)
def test_parse_control_mapping_patterns(
    text: str, kind: str, alias: str, canonical: str
):
    command = parse_control(text)

    assert command is not None
    assert (command.kind, command.alias, command.canonical) == (
        kind,
        alias,
        canonical,
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("撤销刚才的纠正。", "undo"),
        ("删除你学到的这个词。", "delete"),
        ("查看最近学到的词。", "list_recent"),
    ],
)
def test_parse_control_management_patterns(text: str, kind: str):
    command = parse_control(text)

    assert command is not None
    assert command.kind == kind
    assert command.alias is None
    assert command.canonical is None


def test_parse_control_preserves_ordinary_sentences():
    assert parse_control("请帮我为 Open Cloud 写一个介绍。") is None


def test_repetition_never_materializes_without_confirmation(tmp_path):
    store = LearningStore.for_root(tmp_path)

    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.CANDIDATE
    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.REPEATED
    assert store.personal_entries() == ()
    assert [event["action"] for event in _read_events(tmp_path)] == [
        "observe",
        "observe",
    ]


def test_confirm_writes_clean_v1_generation_and_materializes(tmp_path):
    store = LearningStore.for_root(tmp_path)

    event = store.confirm("Open Cloud", "OpenClaw", Scope.PERSONAL)

    raw = _read_events(tmp_path)[0]
    assert raw["schema_version"] == 1
    assert raw["event_id"] == event.event_id
    assert uuid.UUID(raw["generation_id"]).version == 4
    assert "baseline" in raw and raw["baseline"] is None
    assert store.personal_entries()[0].aliases == ("Open Cloud",)
    assert store.personal_entries()[0].source == "explicit-learning-v1"
    assert store.list_recent() == (event,)


def test_unversioned_event_fails_closed(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    events = _read_events(tmp_path)
    events[0].pop("schema_version")
    _write_events(tmp_path, events)

    with pytest.raises(ValueError, match="schema_version"):
        LearningStore.for_root(tmp_path).undo_last()


def test_unknown_intermediate_event_field_fails_closed(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    events = _read_events(tmp_path)
    events[0]["baseline_captured"] = True
    _write_events(tmp_path, events)

    with pytest.raises(ValueError, match="unknown event fields"):
        LearningStore.for_root(tmp_path).list_recent()


def test_active_identity_with_mismatched_generation_fails_closed(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("first", "OpenClaw", Scope.PERSONAL)
    store.confirm("second", "OpenClaw", Scope.PERSONAL)
    events = _read_events(tmp_path)
    events[1]["generation_id"] = str(uuid.uuid4())
    _write_events(tmp_path, events)

    with pytest.raises(ValueError, match="active identity"):
        LearningStore.for_root(tmp_path).export(tmp_path / "export.jsonl")


def test_baseline_snapshot_must_be_complete_and_match_identity(tmp_path):
    manual = _manual_entry()
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    events = _read_events(tmp_path)
    events[0]["baseline"].pop("notes")
    _write_events(tmp_path, events)

    with pytest.raises(ValueError, match="complete entry snapshot"):
        LearningStore.for_root(tmp_path).list_recent()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"x" * (64 * 1024 + 1), "event exceeds"),
        (b"{}\n" * 20_001, "journal exceeds 20000 events"),
        (b"x" * (8 * 1024 * 1024 + 1), "journal exceeds 8388608 bytes"),
    ],
    ids=("line-bytes", "event-count", "journal-bytes"),
)
def test_journal_bounds_fail_closed(tmp_path, content: bytes, message: str):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "learning-events.jsonl").write_bytes(content)

    with pytest.raises(ValueError, match=message):
        LearningStore.for_root(tmp_path).list_recent()


@pytest.mark.parametrize("project_id", ["../escape", r"folder\escape", ".", ".."])
def test_project_id_must_be_one_safe_path_segment(tmp_path, project_id: str):
    with pytest.raises(ValueError, match="safe path segment"):
        LearningStore.for_root(tmp_path).confirm(
            "alias", "Canonical", Scope.PROJECT, project_id=project_id
        )

    assert not (tmp_path.parent / "escape").exists()


def test_project_confirmation_is_isolated_from_personal_state(tmp_path):
    store = LearningStore.for_root(tmp_path)

    store.confirm("Work body", "WorkBuddy", Scope.PROJECT, project_id="project-7")

    assert store.personal_entries() == ()
    entry = load_jsonl(tmp_path / "projects" / "project-7" / "project.jsonl")[0]
    assert entry.scope is Scope.PROJECT
    assert entry.project_id == "project-7"
    assert entry.aliases == ("Work body",)


def test_reject_materializes_personal_negative_mapping(tmp_path):
    event = LearningStore.for_root(tmp_path).reject("龙虾", "OpenClaw")

    entry = LearningStore.for_root(tmp_path).personal_entries()[0]
    assert event.status is EntryStatus.REJECTED
    assert entry.aliases == ("OpenClaw",)
    assert entry.negative_aliases == ("龙虾",)


def test_sequential_undo_targets_latest_effective_mapping(tmp_path):
    store = LearningStore.for_root(tmp_path)
    first = store.confirm("first", "OpenClaw", Scope.PERSONAL)
    second = store.confirm("second", "OpenClaw", Scope.PERSONAL)

    assert store.undo_last() == second
    assert store.personal_entries()[0].aliases == ("first",)
    assert store.list_recent() == (first,)
    assert store.undo_last() == first
    assert store.personal_entries() == ()
    assert store.undo_last() is None


def test_delete_and_undo_delete_restore_exact_targets(tmp_path):
    store = LearningStore.for_root(tmp_path)
    confirmation = store.confirm("learned", "OpenClaw", Scope.PERSONAL)

    deletion = store.delete("OpenClaw")

    assert deletion is not None and deletion.action == "delete"
    assert store.personal_entries() == ()
    assert store.undo_last() == deletion
    assert store.personal_entries()[0].aliases == ("learned",)
    assert store.list_recent() == (confirmation,)
    assert _read_events(tmp_path)[-1]["target_event_id"] == deletion.event_id


def test_export_copies_replayed_personal_entries(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    destination = tmp_path / "exports" / "personal.jsonl"

    store.export(destination)

    assert load_jsonl(destination) == store.personal_entries()


def test_unrelated_manual_entries_are_preserved(tmp_path):
    manual = _manual_entry(
        "ExistingTool",
        aliases=("existing tool",),
        source=None,
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))

    LearningStore.for_root(tmp_path).confirm(
        "new tool", "NewTool", Scope.PERSONAL
    )

    entries = {
        entry.canonical: entry
        for entry in load_jsonl(tmp_path / "personal.jsonl")
    }
    assert entries["ExistingTool"] == manual
    assert entries["NewTool"].aliases == ("new tool",)


def test_same_key_manual_baseline_restores_exactly_after_undo(tmp_path):
    manual = _manual_entry()
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))
    store = LearningStore.for_root(tmp_path)

    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    overlaid = store.personal_entries()[0]
    assert overlaid.aliases == ("curated", "learned")
    assert overlaid.notes == manual.notes
    assert store.undo_last() is not None

    assert load_jsonl(tmp_path / "personal.jsonl") == (manual,)


def test_manual_baseline_restores_after_delete_and_undo_delete(tmp_path):
    manual = _manual_entry()
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)

    deletion = store.delete("OpenClaw")

    assert deletion is not None
    assert load_jsonl(tmp_path / "personal.jsonl") == (manual,)
    assert store.undo_last() == deletion
    assert store.personal_entries()[0].aliases == ("curated", "learned")


def test_active_generation_reuses_generation_and_baseline(tmp_path):
    manual = _manual_entry()
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))
    store = LearningStore.for_root(tmp_path)

    store.confirm("first", "OpenClaw", Scope.PERSONAL)
    store.reject("blocked", "OpenClaw")
    store.confirm("second", "OpenClaw", Scope.PERSONAL)

    mappings = [
        event
        for event in _read_events(tmp_path)
        if event["action"] in {"confirm", "reject"}
    ]
    assert len({event["generation_id"] for event in mappings}) == 1
    assert all(event["baseline"] == mappings[0]["baseline"] for event in mappings)
    entry = store.personal_entries()[0]
    assert entry.aliases == ("curated", "first", "second")
    assert entry.negative_aliases == ("never-this", "blocked")


def test_new_generation_captures_latest_inactive_manual_baseline(tmp_path):
    initial = _manual_entry(aliases=("manual v0",), notes="v0")
    updated = _manual_entry(
        aliases=("manual v1",),
        notes="v1",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (initial,))
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned v0", "OpenClaw", Scope.PERSONAL)
    first_generation = _read_events(tmp_path)[0]["generation_id"]
    store.delete("OpenClaw")
    write_jsonl_atomic(tmp_path / "personal.jsonl", (updated,))

    store.confirm("learned v1", "OpenClaw", Scope.PERSONAL)

    latest = _read_events(tmp_path)[-1]
    assert latest["generation_id"] != first_generation
    assert latest["baseline"]["notes"] == "v1"
    assert store.personal_entries()[0].aliases == ("manual v1", "learned v1")
    store.delete("OpenClaw")
    assert store.personal_entries() == (updated,)


def test_unrelated_replay_keeps_active_overlay_generation(tmp_path):
    manual = _manual_entry()
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual,))
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)

    store.confirm("other", "OtherTool", Scope.PERSONAL)

    entries = {entry.canonical: entry for entry in store.personal_entries()}
    assert entries["OpenClaw"].aliases == ("curated", "learned")
    assert entries["OpenClaw"].notes == manual.notes
    assert entries["OtherTool"].aliases == ("other",)


def test_project_confirmation_preserves_scanner_entries(tmp_path):
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")},
        home=tmp_path,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# ExistingWidget\n", encoding="utf-8")
    scan_project(workspace, paths)
    project_id = paths.for_project(workspace).project_id

    LearningStore.for_root(paths.root).confirm(
        "widget engine",
        "WidgetEngine",
        Scope.PROJECT,
        project_id=project_id,
    )

    entries = {
        entry.canonical: entry
        for entry in load_jsonl(paths.for_project(workspace).lexicon_file)
    }
    assert entries["ExistingWidget"].source == "README.md"
    assert entries["WidgetEngine"].aliases == ("widget engine",)


def test_external_same_key_edit_during_active_generation_is_a_conflict(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    external = _manual_entry(aliases=("external",), notes="external")
    write_jsonl_atomic(tmp_path / "personal.jsonl", (external,))

    with pytest.raises(RuntimeError, match="external edit conflict"):
        store.confirm("second", "OpenClaw", Scope.PERSONAL)

    assert load_jsonl(tmp_path / "personal.jsonl") == (external,)
    assert len(_read_events(tmp_path)) == 1


def test_unrelated_external_edit_is_preserved_during_active_replay(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    external = _manual_entry("ExternalTool", aliases=("external",))
    write_jsonl_atomic(
        tmp_path / "personal.jsonl",
        (*store.personal_entries(), external),
    )

    store.observe("seen", "SeenTool", "conversation")

    entries = {entry.canonical: entry for entry in store.personal_entries()}
    assert entries["ExternalTool"] == external
    assert entries["OpenClaw"].aliases == ("learned",)


def test_inactive_external_same_key_edit_remains_authoritative(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    store.delete("OpenClaw")
    external = _manual_entry(aliases=("external",), notes="external")
    write_jsonl_atomic(tmp_path / "personal.jsonl", (external,))

    store.export(tmp_path / "export.jsonl")

    assert store.personal_entries() == (external,)


def test_atomic_journal_append_failure_changes_neither_journal_nor_lexicon(
    tmp_path, monkeypatch
):
    store = LearningStore.for_root(tmp_path)
    store.observe("seen", "SeenTool", "conversation")
    prior = (tmp_path / "learning-events.jsonl").read_bytes()
    real_replace = learning_module.os.replace

    def fail_journal(source, destination):
        if Path(destination) == store.events_file:
            raise OSError("journal replace")
        real_replace(source, destination)

    monkeypatch.setattr(learning_module.os, "replace", fail_journal)
    with pytest.raises(OSError, match="journal replace"):
        store.confirm("learned", "OpenClaw", Scope.PERSONAL)

    assert (tmp_path / "learning-events.jsonl").read_bytes() == prior
    assert not (tmp_path / "personal.jsonl").exists()
    monkeypatch.setattr(learning_module.os, "replace", real_replace)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    assert store.personal_entries()[0].aliases == ("learned",)


def test_atomic_append_handles_valid_journal_without_terminal_newline(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.observe("first", "FirstTool", "conversation")
    journal = tmp_path / "learning-events.jsonl"
    journal.write_bytes(journal.read_bytes().rstrip(b"\n"))

    store.observe("second", "SecondTool", "conversation")

    assert [event["alias"] for event in _read_events(tmp_path)] == [
        "first",
        "second",
    ]


def test_failed_replay_retries_before_export(tmp_path, monkeypatch):
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic
    calls = 0

    def fail_once(path, entries):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected replay failure")
        real_write(path, entries)

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", fail_once)
    with pytest.raises(OSError, match="injected replay failure"):
        store.confirm("first", "OpenClaw", Scope.PERSONAL)
    assert _read_events(tmp_path)[0]["alias"] == "first"

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    destination = tmp_path / "export" / "personal.jsonl"
    LearningStore.for_root(tmp_path).export(destination)

    assert load_jsonl(destination)[0].aliases == ("first",)


def test_replay_detects_writer_that_did_not_persist(tmp_path, monkeypatch):
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic
    monkeypatch.setattr(
        learning_module,
        "write_jsonl_atomic",
        lambda path, entries: None,
    )

    with pytest.raises(RuntimeError, match="verification"):
        store.confirm("first", "OpenClaw", Scope.PERSONAL)

    assert _read_events(tmp_path)[0]["alias"] == "first"
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    LearningStore.for_root(tmp_path).export(tmp_path / "export.jsonl")
    assert LearningStore.for_root(tmp_path).personal_entries()[0].aliases == (
        "first",
    )


def test_noop_undo_preplays_pending_mapping_before_compensation(
    tmp_path, monkeypatch
):
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic

    def fail(path, entries):
        raise OSError("first replay")

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", fail)
    with pytest.raises(OSError, match="first replay"):
        store.confirm("first", "OpenClaw", Scope.PERSONAL)

    writes: list[tuple[LexiconEntry, ...]] = []

    def record(path, entries):
        writes.append(tuple(entries))
        real_write(path, entries)

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", record)
    target = LearningStore.for_root(tmp_path).undo_last()

    assert target is not None and target.action == "confirm"
    assert [entries[0].aliases if entries else () for entries in writes] == [
        ("first",),
        (),
    ]
    assert LearningStore.for_root(tmp_path).personal_entries() == ()


def test_partial_personal_project_replay_recovers_before_export(
    tmp_path, monkeypatch
):
    store = LearningStore.for_root(tmp_path)
    project_id = "project-7"
    store.confirm("personal", "OpenClaw", Scope.PERSONAL)
    store.confirm("project", "OpenClaw", Scope.PROJECT, project_id=project_id)
    real_write = learning_module.write_jsonl_atomic
    calls = 0

    def fail_second(path, entries):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second file")
        real_write(path, entries)

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", fail_second)
    with pytest.raises(OSError, match="second file"):
        store.delete("OpenClaw")

    project_file = tmp_path / "projects" / project_id / "project.jsonl"
    assert load_jsonl(tmp_path / "personal.jsonl") == ()
    assert load_jsonl(project_file)[0].aliases == ("project",)

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    destination = tmp_path / "export" / "personal.jsonl"
    restarted = LearningStore.for_root(tmp_path)
    restarted.export(destination)

    assert load_jsonl(project_file) == ()
    assert load_jsonl(destination) == ()
    deletion = restarted.undo_last()
    assert deletion is not None and deletion.action == "delete"
    assert load_jsonl(tmp_path / "personal.jsonl")[0].aliases == ("personal",)
    assert load_jsonl(project_file)[0].aliases == ("project",)


def test_export_detects_writer_that_did_not_persist(tmp_path, monkeypatch):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    monkeypatch.setattr(
        learning_module,
        "write_jsonl_atomic",
        lambda path, entries: None,
    )

    with pytest.raises(RuntimeError, match="export verification"):
        store.export(tmp_path / "missing" / "export.jsonl")


def test_rejected_mapping_suppresses_same_lower_layer_mapping(tmp_path):
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")},
        home=tmp_path,
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
    context = MatchContext(
        domains=frozenset({"ai"}),
        conversation_terms=frozenset({"OpenClaw"}),
    )
    candidates = generate_candidates(
        text,
        LexiconSet.load(paths, builtins),
        context,
    )

    decision = decide(text, candidates, context)

    assert decision.action is DecisionAction.KEEP
    blocked = [
        candidate
        for candidate in candidates
        if candidate.canonical == "OpenClaw" and candidate.original == "龙虾"
    ]
    assert blocked
    assert all("alias:negative" in candidate.evidence for candidate in blocked)


def test_rejected_mapping_does_not_suppress_different_canonical(tmp_path):
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")},
        home=tmp_path,
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

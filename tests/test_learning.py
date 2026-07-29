from __future__ import annotations

import json

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


def _legacy_mapping_event(
    event_id: str,
    action: str,
    alias: str,
    canonical: str,
    *,
    scope: Scope = Scope.PERSONAL,
    project_id: str | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "alias": alias,
        "canonical": canonical,
        "event_id": event_id,
        "project_id": project_id,
        "scope": scope.value,
        "source": None,
        "status": (
            EntryStatus.CONFIRMED.value
            if action == "confirm"
            else EntryStatus.REJECTED.value
        ),
        "timestamp": "2026-07-29T00:00:00.000000Z",
    }


def _write_learning_events(root, *events: dict[str, object]) -> None:
    (root / "learning-events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _read_learning_events(root) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (root / "learning-events.jsonl").read_text("utf-8").splitlines()
    ]


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


def test_undo_delete_restores_state_and_appends_scoped_compensation(tmp_path):
    """Catch undo crashing on a scope-less delete instead of restoring its targets."""
    store = LearningStore.for_root(tmp_path)
    confirmation = store.confirm(
        "Open Cloud", "OpenClaw", Scope.PERSONAL
    )
    deletion = store.delete("OpenClaw")
    assert deletion is not None
    assert store.personal_entries() == ()

    undone = store.undo_last()

    assert undone == deletion
    assert store.personal_entries()[0].aliases == ("Open Cloud",)
    assert store.list_recent() == (confirmation,)
    audit = [
        json.loads(line)
        for line in (tmp_path / "learning-events.jsonl").read_text("utf-8").splitlines()
    ]
    assert audit[-1]["action"] == "undo"
    assert audit[-1]["target_event_id"] == deletion.event_id
    assert audit[-1]["scope"] == Scope.PERSONAL.value


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


def test_personal_confirmation_preserves_source_less_confirmed_manual_entry(tmp_path):
    """Catch broad ownership heuristics deleting a legacy manual confirmation."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    paths.root.mkdir()
    manual_entry = LexiconEntry(
        canonical="LegacyTool",
        scope=Scope.PERSONAL,
        aliases=("legacy tool",),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
    )
    write_jsonl_atomic(paths.personal_file, (manual_entry,))

    LearningStore.for_root(paths.root).confirm(
        "new tool", "NewTool", Scope.PERSONAL
    )

    entries = {entry.canonical: entry for entry in load_jsonl(paths.personal_file)}
    assert entries["LegacyTool"] == manual_entry
    assert entries["NewTool"].aliases == ("new tool",)


def test_undo_restores_the_exact_manual_entry_overlaid_by_learning(tmp_path):
    """Catch undo leaving a learned alias or status on a manual record."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}, home=tmp_path
    )
    paths.root.mkdir()
    manual_entry = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias",),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CURATED,
        source="manual",
        notes="hand-maintained",
    )
    write_jsonl_atomic(paths.personal_file, (manual_entry,))
    store = LearningStore.for_root(paths.root)

    store.confirm("learned alias", "OpenClaw", Scope.PERSONAL)
    store.undo_last()

    assert load_jsonl(paths.personal_file) == (manual_entry,)


def test_new_overlay_generation_uses_current_manual_baseline(tmp_path):
    """Catch a completed overlay generation replaying its stale first baseline."""
    store = LearningStore.for_root(tmp_path)
    initial = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("manual v0",),
        domains=("ai",),
        weight=0.7,
        status=EntryStatus.CURATED,
        source="manual",
        notes="v0",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (initial,))
    store.confirm("learned v0", "OpenClaw", Scope.PERSONAL)
    store.delete("OpenClaw")
    updated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("manual v1",),
        domains=("software-development",),
        weight=0.9,
        status=EntryStatus.CURATED,
        source="manual",
        notes="v1",
    )
    write_jsonl_atomic(tmp_path / "personal.jsonl", (updated,))

    store.confirm("learned v1", "OpenClaw", Scope.PERSONAL)

    overlaid = load_jsonl(tmp_path / "personal.jsonl")[0]
    assert overlaid.aliases == ("manual v1", "learned v1")
    assert overlaid.domains == ("software-development",)
    assert overlaid.weight == 1.0
    assert overlaid.notes == "v1"
    store.delete("OpenClaw")
    assert load_jsonl(tmp_path / "personal.jsonl") == (updated,)


def test_unrelated_learning_rebuild_keeps_active_overlay_generation(tmp_path):
    """Catch another mapping rebuild replacing an active overlay's baseline."""
    store = LearningStore.for_root(tmp_path)
    manual_entry = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("manual alias",),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CURATED,
        source="manual",
        notes="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual_entry,))
    store.confirm("learned alias", "OpenClaw", Scope.PERSONAL)

    store.confirm("other alias", "OtherTool", Scope.PERSONAL)

    entries = {
        entry.canonical: entry for entry in load_jsonl(tmp_path / "personal.jsonl")
    }
    assert entries["OpenClaw"].aliases == ("manual alias", "learned alias")
    assert entries["OpenClaw"].domains == ("ai",)
    assert entries["OpenClaw"].notes == "manual"
    assert entries["OtherTool"].aliases == ("other alias",)


def test_undo_migrates_pre_snapshot_merged_mapping_conservatively(tmp_path):
    """Catch undo retaining a learned alias in valid pre-snapshot state."""
    manual_merged = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "learned alias"),
        domains=("ai",),
        weight=1.0,
        status=EntryStatus.CONFIRMED,
        source="manual",
        notes="manual provenance",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual_merged,))
    prior_event = {
        "action": "confirm",
        "alias": "learned alias",
        "canonical": "OpenClaw",
        "event_id": "prior-confirm",
        "project_id": None,
        "scope": "personal",
        "source": None,
        "status": "confirmed",
        "timestamp": "2026-07-29T00:00:00.000000Z",
    }
    (tmp_path / "learning-events.jsonl").write_text(
        json.dumps(prior_event) + "\n", encoding="utf-8"
    )

    LearningStore.for_root(tmp_path).undo_last()

    migrated = load_jsonl(tmp_path / "personal.jsonl")[0]
    assert migrated.aliases == ("curated alias",)
    assert migrated.domains == ("ai",)
    assert migrated.weight == 1.0
    assert migrated.source == "manual"
    assert migrated.notes == "manual provenance"
    audit = [
        json.loads(line)
        for line in (tmp_path / "learning-events.jsonl").read_text("utf-8").splitlines()
    ]
    assert audit[-1]["migration_fallback"] == "remove-historical-learning-aliases"


def test_delete_migrates_pre_snapshot_merged_rejection_conservatively(tmp_path):
    """Catch delete retaining a historical negative mapping without a baseline."""
    manual_merged = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "OpenClaw"),
        domains=("ai",),
        weight=1.0,
        status=EntryStatus.REJECTED,
        source="manual",
        negative_aliases=("龙虾",),
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual_merged,))
    prior_event = {
        "action": "reject",
        "alias": "龙虾",
        "canonical": "OpenClaw",
        "event_id": "prior-reject",
        "project_id": None,
        "scope": "personal",
        "source": None,
        "status": "rejected",
        "timestamp": "2026-07-29T00:00:00.000000Z",
    }
    (tmp_path / "learning-events.jsonl").write_text(
        json.dumps(prior_event) + "\n", encoding="utf-8"
    )

    LearningStore.for_root(tmp_path).delete("OpenClaw")

    migrated = load_jsonl(tmp_path / "personal.jsonl")[0]
    assert migrated.aliases == ("curated alias", "OpenClaw")
    assert migrated.negative_aliases == ()
    audit = [
        json.loads(line)
        for line in (tmp_path / "learning-events.jsonl").read_text("utf-8").splitlines()
    ]
    assert audit[-1]["migration_fallback"] == "remove-historical-learning-aliases"


def test_legacy_project_contributions_are_independently_reversible(tmp_path):
    """Catch one legacy contribution preventing cleanup of its siblings."""
    project_id = "project-legacy"
    path = tmp_path / "projects" / project_id / "project.jsonl"
    manual_merged = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PROJECT,
        aliases=("curated alias", "learned one", "learned two"),
        domains=("ai",),
        weight=1.0,
        status=EntryStatus.CONFIRMED,
        project_id=project_id,
        source="manual",
        notes="project curated",
        negative_aliases=("blocked alias",),
    )
    path.parent.mkdir(parents=True)
    write_jsonl_atomic(path, (manual_merged,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-one",
            "confirm",
            "learned one",
            "OpenClaw",
            scope=Scope.PROJECT,
            project_id=project_id,
        ),
        _legacy_mapping_event(
            "legacy-two",
            "confirm",
            "learned two",
            "OpenClaw",
            scope=Scope.PROJECT,
            project_id=project_id,
        ),
        _legacy_mapping_event(
            "legacy-reject",
            "reject",
            "blocked alias",
            "OpenClaw",
            scope=Scope.PROJECT,
            project_id=project_id,
        ),
    )
    store = LearningStore.for_root(tmp_path)

    store.undo_last()
    after_reject = load_jsonl(path, Scope.PROJECT)[0]
    assert after_reject.aliases == ("curated alias", "learned one", "learned two")
    assert after_reject.negative_aliases == ()

    LearningStore.for_root(tmp_path).undo_last()
    after_second_confirm = load_jsonl(path, Scope.PROJECT)[0]
    assert after_second_confirm.aliases == ("curated alias", "learned one")
    assert after_second_confirm.negative_aliases == ()

    LearningStore.for_root(tmp_path).undo_last()
    assert load_jsonl(path, Scope.PROJECT) == (
        LexiconEntry(
            canonical="OpenClaw",
            scope=Scope.PROJECT,
            aliases=("curated alias",),
            domains=("ai",),
            weight=1.0,
            status=EntryStatus.CONFIRMED,
            project_id=project_id,
            source="manual",
            notes="project curated",
        ),
    )


def test_delete_cleans_legacy_alias_from_a_new_overlay_baseline(tmp_path):
    """Catch a modern snapshot restoring legacy state after mixed-generation delete."""
    manual_merged = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy alias"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
        notes="manual provenance",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual_merged,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-confirm", "confirm", "legacy alias", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    store.confirm("modern alias", "OpenClaw", Scope.PERSONAL)

    store.delete("OpenClaw")

    assert load_jsonl(tmp_path / "personal.jsonl") == (
        LexiconEntry(
            canonical="OpenClaw",
            scope=Scope.PERSONAL,
            aliases=("curated alias",),
            domains=("ai",),
            weight=0.8,
            status=EntryStatus.CONFIRMED,
            source="manual",
            notes="manual provenance",
        ),
    )


def test_consumed_legacy_migration_does_not_strip_a_manual_readd(tmp_path):
    """Catch historical migration rules acting as permanent alias tombstones."""
    contaminated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy alias"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
        notes="manual provenance",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (contaminated,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-confirm", "confirm", "legacy alias", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    store.undo_last()
    manually_readded = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy alias"),
        domains=("manual",),
        weight=0.7,
        status=EntryStatus.CURATED,
        source="manual",
        notes="intentionally re-added",
    )
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manually_readded,))

    LearningStore.for_root(tmp_path).confirm(
        "other alias", "OtherTool", Scope.PERSONAL
    )

    entries = {
        entry.canonical: entry
        for entry in load_jsonl(tmp_path / "personal.jsonl", Scope.PERSONAL)
    }
    assert entries["OpenClaw"] == manually_readded
    assert entries["OtherTool"].aliases == ("other alias",)


def test_manual_readd_survives_while_a_sibling_legacy_mapping_is_active(tmp_path):
    """Catch active provenance overwriting a manual edit after partial legacy undo."""
    contaminated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy one", "legacy two"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (contaminated,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-one", "confirm", "legacy one", "OpenClaw"
        ),
        _legacy_mapping_event(
            "legacy-two", "confirm", "legacy two", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    store.undo_last()
    manually_readded = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy one", "legacy two"),
        domains=("manual",),
        weight=0.7,
        status=EntryStatus.CURATED,
        source="manual",
        notes="legacy two intentionally restored",
    )
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manually_readded,))

    store.confirm("other alias", "OtherTool", Scope.PERSONAL)

    entries = {
        entry.canonical: entry
        for entry in load_jsonl(tmp_path / "personal.jsonl", Scope.PERSONAL)
    }
    assert entries["OpenClaw"] == manually_readded
    store.undo_last()
    store.undo_last()
    final = load_jsonl(tmp_path / "personal.jsonl", Scope.PERSONAL)[0]
    assert final.aliases == ("curated alias", "legacy two")
    assert final.domains == ("manual",)
    assert final.notes == "legacy two intentionally restored"


def test_new_overlay_adopts_manual_edits_over_an_active_legacy_mapping(tmp_path):
    """Catch a new same-key mapping reverting a manually replaced active overlay."""
    contaminated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy one", "legacy two"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (contaminated,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-one", "confirm", "legacy one", "OpenClaw"
        ),
        _legacy_mapping_event(
            "legacy-two", "confirm", "legacy two", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    store.undo_last()
    manually_replaced = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy one", "legacy two"),
        domains=("manual",),
        weight=0.7,
        status=EntryStatus.CURATED,
        source="manual",
        notes="manual replacement",
    )
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manually_replaced,))

    store.confirm("modern alias", "OpenClaw", Scope.PERSONAL)

    overlaid = load_jsonl(tmp_path / "personal.jsonl", Scope.PERSONAL)[0]
    assert overlaid.aliases == (
        "curated alias",
        "legacy two",
        "legacy one",
        "modern alias",
    )
    assert overlaid.domains == ("manual",)
    assert overlaid.notes == "manual replacement"
    store.delete("OpenClaw")
    restored = load_jsonl(tmp_path / "personal.jsonl", Scope.PERSONAL)[0]
    assert restored.aliases == ("curated alias", "legacy two")
    assert restored.domains == ("manual",)
    assert restored.notes == "manual replacement"


def test_failed_migration_retries_before_noop_undo_or_new_baseline(
    tmp_path, monkeypatch
):
    """Catch a failed migration becoming inactive but never reconciled after restart."""
    contaminated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy alias"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (contaminated,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-confirm", "confirm", "legacy alias", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic

    def fail_before_replace(path, entries):
        raise OSError("injected pre-replace failure")

    monkeypatch.setattr(
        learning_module, "write_jsonl_atomic", fail_before_replace
    )
    with pytest.raises(OSError, match="injected pre-replace failure"):
        store.undo_last()

    assert load_jsonl(tmp_path / "personal.jsonl") == (contaminated,)
    assert "migration_applied" not in {
        event["action"] for event in _read_learning_events(tmp_path)
    }
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)

    restarted = LearningStore.for_root(tmp_path)
    restarted.confirm("modern alias", "OpenClaw", Scope.PERSONAL)
    final = load_jsonl(tmp_path / "personal.jsonl")[0]
    assert final.aliases == ("curated alias", "modern alias")
    assert [
        event["action"] for event in _read_learning_events(tmp_path)[-2:]
    ] == ["migration_applied", "confirm"]


def test_migration_is_not_marked_applied_when_writer_does_not_persist(
    tmp_path, monkeypatch
):
    """Catch a successful writer return being trusted without persisted verification."""
    contaminated = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("curated alias", "legacy alias"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (contaminated,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "legacy-confirm", "confirm", "legacy alias", "OpenClaw"
        ),
    )
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic
    monkeypatch.setattr(
        learning_module, "write_jsonl_atomic", lambda path, entries: None
    )

    with pytest.raises(RuntimeError, match="verification"):
        store.undo_last()

    assert load_jsonl(tmp_path / "personal.jsonl") == (contaminated,)
    assert "migration_applied" not in {
        event["action"] for event in _read_learning_events(tmp_path)
    }
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    assert LearningStore.for_root(tmp_path).undo_last() is None
    assert load_jsonl(tmp_path / "personal.jsonl")[0].aliases == ("curated alias",)


def test_partial_personal_project_migration_recovers_idempotently(
    tmp_path, monkeypatch
):
    """Catch a partial multi-file migration being consumed or left unrecoverable."""
    project_id = "project-recovery"
    personal_path = tmp_path / "personal.jsonl"
    project_path = tmp_path / "projects" / project_id / "project.jsonl"
    personal = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PERSONAL,
        aliases=("personal curated", "personal legacy"),
        domains=("ai",),
        weight=0.8,
        status=EntryStatus.CONFIRMED,
        source="manual",
    )
    project = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PROJECT,
        aliases=("project curated", "project legacy"),
        domains=("project",),
        weight=0.7,
        status=EntryStatus.CONFIRMED,
        project_id=project_id,
        source="manual",
    )
    tmp_path.mkdir(exist_ok=True)
    project_path.parent.mkdir(parents=True)
    write_jsonl_atomic(personal_path, (personal,))
    write_jsonl_atomic(project_path, (project,))
    _write_learning_events(
        tmp_path,
        _legacy_mapping_event(
            "personal-legacy", "confirm", "personal legacy", "OpenClaw"
        ),
        _legacy_mapping_event(
            "project-legacy",
            "confirm",
            "project legacy",
            "OpenClaw",
            scope=Scope.PROJECT,
            project_id=project_id,
        ),
    )
    store = LearningStore.for_root(tmp_path)
    real_write = learning_module.write_jsonl_atomic
    calls = 0

    def fail_second_write(path, entries):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file failure")
        real_write(path, entries)

    monkeypatch.setattr(learning_module, "write_jsonl_atomic", fail_second_write)
    with pytest.raises(OSError, match="injected second-file failure"):
        store.delete("OpenClaw")

    assert load_jsonl(personal_path)[0].aliases == ("personal curated",)
    assert load_jsonl(project_path, Scope.PROJECT) == (project,)
    assert "migration_applied" not in {
        event["action"] for event in _read_learning_events(tmp_path)
    }
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)

    restarted = LearningStore.for_root(tmp_path)
    assert restarted.delete("OpenClaw") is None
    assert load_jsonl(personal_path)[0].aliases == ("personal curated",)
    assert load_jsonl(project_path, Scope.PROJECT)[0].aliases == (
        "project curated",
    )

    restored = restarted.undo_last()
    assert restored is not None and restored.action == "delete"
    assert load_jsonl(personal_path)[0].aliases == (
        "personal curated",
        "personal legacy",
    )
    assert load_jsonl(project_path, Scope.PROJECT)[0].aliases == (
        "project curated",
        "project legacy",
    )


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

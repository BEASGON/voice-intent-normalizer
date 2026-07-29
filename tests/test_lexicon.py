from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_intent_normalizer.lexicon import (
    LexiconSet,
    load_jsonl,
    parse_entry,
    write_jsonl_atomic,
)
from voice_intent_normalizer.models import Candidate, EntryStatus, Scope
from voice_intent_normalizer.paths import StatePaths


def _raw_entry(**overrides: object) -> dict[str, object]:
    """Return one valid JSON-compatible entry, overridden for each test."""
    entry: dict[str, object] = {
        "canonical": "OpenClaw",
        "scope": "hot",
        "aliases": ["Open Cloud", "榫欒櫨"],
        "domains": ["ai", "agent"],
        "weight": 0.9,
        "status": "curated",
    }
    entry.update(overrides)
    return entry


def test_parse_entry_normalizes_collections():
    """Catch a parser that leaves mutable JSON lists on the entry."""
    entry = parse_entry(_raw_entry())

    assert entry.canonical == "OpenClaw"
    assert entry.scope is Scope.HOT
    assert entry.aliases == ("Open Cloud", "榫欒櫨")
    assert entry.domains == ("ai", "agent")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical", "  "),
        ("aliases", ["valid", ""]),
        ("weight", 1.1),
        ("weight", -0.1),
        ("scope", "unknown"),
        ("status", "unknown"),
    ],
)
def test_parse_entry_rejects_invalid_required_values(field: str, value: object):
    """Catch acceptance of values that cannot form a valid lexicon entry."""
    with pytest.raises(ValueError):
        parse_entry(_raw_entry(**{field: value}))


def test_load_jsonl_rejects_scope_mismatch(tmp_path):
    """Catch a loader that lets one lexicon scope leak into another."""
    path = tmp_path / "personal.jsonl"
    path.write_text(
        '{"canonical":"Codex","scope":"hot","aliases":["code X"],'
        '"domains":["ai"],"weight":0.9,"status":"curated"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scope") as exc_info:
        load_jsonl(path, expected_scope=Scope.PERSONAL)

    assert str(path) in str(exc_info.value)
    assert "line 1" in str(exc_info.value)


def test_load_jsonl_reports_path_and_line_for_invalid_json(tmp_path):
    """Catch unlocatable errors when an operator must repair JSONL."""
    path = tmp_path / "lexicon.jsonl"
    path.write_text("{bad json}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_jsonl(path)

    assert str(path) in str(exc_info.value)
    assert "line 1" in str(exc_info.value)


def test_write_jsonl_atomic_emits_stable_unicode_jsonl(tmp_path):
    """Catch non-deterministic or ASCII-escaped serialized lexicon entries."""
    path = tmp_path / "lexicon.jsonl"
    entry = parse_entry(
        _raw_entry(phonetics=["opən klɔː"], project_id="project-7", source="seed")
    )

    write_jsonl_atomic(path, (entry,))

    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert content.count("\n") == 1
    assert "榫欒櫨" in content
    assert "\\u" not in content
    assert list(json.loads(content)) == [
        "aliases",
        "canonical",
        "domains",
        "negative_aliases",
        "phonetics",
        "project_id",
        "scope",
        "source",
        "status",
        "weight",
    ]
    assert load_jsonl(path) == (entry,)


def test_parse_entry_accepts_optional_collections():
    """Catch loss of optional phonetic and negative-alias matching data."""
    entry = parse_entry(
        _raw_entry(phonetics=["open claw"], negative_aliases=["open door"])
    )

    assert entry.phonetics == ("open claw",)
    assert entry.negative_aliases == ("open door",)
    assert entry.status is EntryStatus.CURATED


def test_candidate_normalizes_mutable_matching_metadata():
    """Catch mutable evidence or span data leaking into a frozen candidate."""
    candidate = Candidate(
        canonical="OpenClaw",
        original="Open Cloud",
        replacement_span=[0, 10],
        score=0.9,
        evidence=["alias"],
        entry=parse_entry(_raw_entry()),
    )

    assert candidate.replacement_span == (0, 10)
    assert candidate.evidence == ("alias",)


def _write_entries(path: Path, *entries: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )


@pytest.fixture
def layer_fixture(tmp_path):
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    builtins_root = tmp_path / "builtins"
    project_root = tmp_path / "project"
    project = paths.for_project(project_root)

    _write_entries(
        paths.personal_file,
        _raw_entry(
            canonical="Personal WorkBuddy", scope="personal", aliases=["work body"]
        ),
    )
    _write_entries(
        project.lexicon_file,
        _raw_entry(
            canonical="Project WorkBuddy",
            scope="project",
            aliases=["work body"],
            project_id=project.project_id,
        ),
        _raw_entry(
            canonical="Other Project",
            scope="project",
            aliases=["other project"],
            project_id="another-project",
        ),
    )
    _write_entries(
        builtins_root / "domains" / "ai.jsonl",
        _raw_entry(
            canonical="Industry WorkBuddy", scope="industry", aliases=["work body"]
        ),
    )
    _write_entries(
        paths.hotwords_file,
        _raw_entry(canonical="Hot WorkBuddy", scope="hot", aliases=["work body"]),
    )
    _write_entries(
        builtins_root / "base-zh.jsonl",
        _raw_entry(canonical="Base WorkBuddy", scope="base", aliases=["work body"]),
    )
    return {
        "state_paths": paths,
        "builtins_root": builtins_root,
        "project_root": project_root,
        "domains": ("ai",),
    }


def test_personal_alias_wins_over_hot_alias(layer_fixture):
    """Catch lower-precedence public data overriding a personal correction."""
    lexicons = LexiconSet.load(**layer_fixture)

    entries = lexicons.by_alias("work body")

    assert entries[0].scope is Scope.PERSONAL


def test_layered_entries_follow_precedence_and_exclude_other_projects(layer_fixture):
    """Catch wrong layer ordering or loading another workspace's state."""
    lexicons = LexiconSet.load(**layer_fixture)

    assert [entry.scope for entry in lexicons.entries] == [
        Scope.PERSONAL,
        Scope.PROJECT,
        Scope.INDUSTRY,
        Scope.HOT,
        Scope.BASE,
    ]
    assert all(entry.canonical != "Other Project" for entry in lexicons.entries)


def test_layer_loader_keeps_last_duplicate_record_inside_one_file(layer_fixture):
    """Catch duplicate records retaining stale, earlier data from the same file."""
    paths = layer_fixture["state_paths"]
    _write_entries(
        paths.personal_file,
        _raw_entry(canonical="Old personal", scope="personal", aliases=["old"]),
        _raw_entry(canonical="Old personal", scope="personal", aliases=["new"]),
    )

    lexicons = LexiconSet.load(**layer_fixture)

    assert lexicons.by_alias("old") == ()
    assert lexicons.by_alias("NEW") == (lexicons.entries[0],)

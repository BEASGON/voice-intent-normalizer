from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import voice_intent_normalizer.lexicon as lexicon_module
import voice_intent_normalizer.project_scan as project_scan_module
from voice_intent_normalizer.lexicon import (
    LexiconSet,
    load_jsonl,
    parse_entry,
    write_jsonl_atomic,
)
from voice_intent_normalizer.models import Candidate, EntryStatus, Scope
from voice_intent_normalizer.paths import StatePaths, StateRootLease
from voice_intent_normalizer.project_scan import scan_project


def _replace_project_root_with_directory_alias(root: Path, target: Path) -> None:
    root.rmdir()
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            pytest.skip(f"cannot create Windows junction: {created.stderr}")
        return
    root.symlink_to(target, target_is_directory=True)


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
        "notes",
        "phonetics",
        "project_id",
        "scope",
        "source",
        "status",
        "use_count",
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


def test_scan_metadata_round_trips_through_jsonl(tmp_path):
    """Catch JSONL persistence that drops scan kind or exact use count."""
    path = tmp_path / "project.jsonl"
    entry = parse_entry(
        _raw_entry(
            scope="project",
            project_id="project-7",
            source="src/widget.py",
            notes="camel-case",
            use_count=3,
        )
    )

    write_jsonl_atomic(path, (entry,))

    assert load_jsonl(path) == (entry,)
    assert entry.notes == "camel-case"
    assert entry.use_count == 3


@pytest.mark.parametrize(
    ("field", "value"),
    (("use_count", -1), ("use_count", True), ("notes", 3)),
)
def test_parse_entry_rejects_invalid_scan_metadata(field: str, value: object):
    """Catch malformed metadata entering the strict project cache schema."""
    with pytest.raises(ValueError):
        parse_entry(_raw_entry(**{field: value}))


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


def _write_transactional_hotword(
    paths: StatePaths, entry: dict[str, object]
) -> None:
    data = (json.dumps(entry) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    payload_name = f"payload-{digest}.jsonl"
    hotwords = paths.hotwords_file.parent
    payloads = hotwords / "payloads"
    payloads.mkdir(parents=True)
    (payloads / payload_name).write_bytes(data)
    paths.hotwords_file.write_bytes(data)
    (hotwords / "current.json").write_text(
        json.dumps(
            {
                "last_check": "2026-07-30T00:00:00+00:00",
                "payload": payload_name,
                "schema_version": 1,
                "sha256": digest,
                "version": "2026.07.30",
            }
        ),
        encoding="utf-8",
    )


def _write_hotword_pointer(
    paths: StatePaths,
    authoritative: dict[str, object],
    *,
    payload: bytes | None,
    raw: bytes | None,
) -> bytes:
    """Write one structurally valid pointer with independently controlled bytes."""
    data = (json.dumps(authoritative) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    payload_name = f"payload-{digest}.jsonl"
    hotwords = paths.hotwords_file.parent
    payloads = hotwords / "payloads"
    payloads.mkdir(parents=True)
    if payload is not None:
        (payloads / payload_name).write_bytes(payload)
    if raw is not None:
        paths.hotwords_file.write_bytes(raw)
    (hotwords / "current.json").write_text(
        json.dumps(
            {
                "last_check": "2026-07-30T00:00:00+00:00",
                "payload": payload_name,
                "schema_version": 1,
                "sha256": digest,
                "version": "2026.07.30",
            }
        ),
        encoding="utf-8",
    )
    return data


def test_corrupt_hotword_pointer_is_diagnostic_even_with_raw_fallback(tmp_path):
    """Catch pointer corruption being hidden by a still-readable legacy cache."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    _write_entries(
        paths.hotwords_file,
        _raw_entry(canonical="RawFallback", scope="hot", aliases=["raw"]),
    )
    (paths.hotwords_file.parent / "current.json").write_text(
        '{"payload":"not-a-valid-pointer"}', encoding="utf-8"
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == ["RawFallback"]
    assert "hotword_state_invalid" in diagnostics


@pytest.mark.parametrize("payload", (None, b"corrupt immutable payload\n"))
def test_valid_pointer_recovers_only_exact_hash_raw_with_transaction_diagnostic(
    tmp_path, payload
):
    """Catch pointer-authoritative recovery hiding an interrupted transaction."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    authoritative = _raw_entry(
        canonical="PointerAuthority", scope="hot", aliases=["authority"]
    )
    data = (json.dumps(authoritative) + "\n").encode()
    _write_hotword_pointer(paths, authoritative, payload=payload, raw=data)

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == ["PointerAuthority"]
    assert diagnostics.count("hotword_transaction_invalid") == 1
    assert "hotword_invalid" not in diagnostics


def test_valid_pointer_can_use_exact_raw_when_corrupt_payload_path_is_unrepairable(
    tmp_path,
):
    """Catch a valid raw fallback being mislabeled because repair itself failed."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    authoritative = _raw_entry(
        canonical="DirectoryPayloadAuthority", scope="hot", aliases=["authority"]
    )
    data = (json.dumps(authoritative) + "\n").encode()
    _write_hotword_pointer(paths, authoritative, payload=None, raw=data)
    digest = hashlib.sha256(data).hexdigest()
    (
        paths.hotwords_file.parent
        / "payloads"
        / f"payload-{digest}.jsonl"
    ).mkdir()

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == [
        "DirectoryPayloadAuthority"
    ]
    assert diagnostics.count("hotword_transaction_invalid") == 1
    assert "hotword_invalid" not in diagnostics


@pytest.mark.parametrize(
    ("raw_state", "expected_diagnostics"),
    (
        ("missing", ()),
        ("valid", ()),
        ("mismatched", ("hotword_invalid",)),
        ("directory", ("hotword_invalid",)),
    ),
)
def test_lexicon_keeps_valid_payload_authority_for_every_secondary_raw_state(
    tmp_path, raw_state, expected_diagnostics
):
    """Catch LexiconSet treating a raw materialization cache as authoritative."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    authoritative = _raw_entry(
        canonical="ImmutableLexiconAuthority",
        scope="hot",
        aliases=["authority"],
    )
    data = (json.dumps(authoritative) + "\n").encode()
    raw: bytes | None
    if raw_state == "valid":
        raw = data
    elif raw_state == "mismatched":
        raw = (
            json.dumps(
                _raw_entry(
                    canonical="MismatchedSecondary",
                    scope="hot",
                    aliases=["secondary"],
                )
            )
            + "\n"
        ).encode()
    else:
        raw = None
    _write_hotword_pointer(paths, authoritative, payload=data, raw=raw)
    if raw_state == "directory":
        paths.hotwords_file.mkdir()

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == [
        "ImmutableLexiconAuthority"
    ]
    assert diagnostics == expected_diagnostics
    if raw_state == "directory":
        assert paths.hotwords_file.is_dir()
    else:
        assert paths.hotwords_file.read_bytes() == data


@pytest.mark.parametrize("payload", (None, b"corrupt immutable payload\n"))
def test_valid_pointer_never_loads_mismatched_raw_fallback(tmp_path, payload):
    """Catch readable raw bytes overriding the valid pointer's checksum authority."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    authoritative = _raw_entry(
        canonical="PointerAuthority", scope="hot", aliases=["authority"]
    )
    mismatched = (
        json.dumps(
            _raw_entry(canonical="MismatchedRaw", scope="hot", aliases=["raw"])
        )
        + "\n"
    ).encode()
    _write_hotword_pointer(paths, authoritative, payload=payload, raw=mismatched)
    _write_entries(
        tmp_path / "builtins" / "hotwords-snapshot.jsonl",
        _raw_entry(canonical="BuiltinFallback", scope="hot", aliases=["builtin"]),
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == ["BuiltinFallback"]
    assert diagnostics.count("hotword_transaction_invalid") == 1
    assert diagnostics.count("hotword_invalid") == 1


def test_missing_pointer_payload_without_raw_still_reports_transaction_invalid(
    tmp_path,
):
    """Catch built-in fallback erasing evidence of an interrupted pointer commit."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    _write_hotword_pointer(
        paths,
        _raw_entry(canonical="PointerAuthority", scope="hot", aliases=["authority"]),
        payload=None,
        raw=None,
    )
    _write_entries(
        tmp_path / "builtins" / "hotwords-snapshot.jsonl",
        _raw_entry(canonical="BuiltinFallback", scope="hot", aliases=["builtin"]),
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins"
    )

    assert [entry.canonical for entry in lexicons.entries] == ["BuiltinFallback"]
    assert diagnostics.count("hotword_transaction_invalid") == 1
    assert "hotword_invalid" not in diagnostics


def test_invalid_project_alias_omits_every_project_layer(tmp_path):
    """Catch project.jsonl loading before direct-root authority is retained."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = paths.for_project(project_root)
    _write_entries(
        paths.personal_file,
        _raw_entry(
            canonical="PersonalSafe",
            scope="personal",
            aliases=["personal safe"],
            domains=[],
            weight=1.0,
            status="confirmed",
        ),
    )
    _write_entries(
        project.lexicon_file,
        _raw_entry(
            canonical="OldProjectAuthority",
            scope="project",
            aliases=["old project"],
            domains=[],
            weight=1.0,
            status="confirmed",
            project_id=project.project_id,
        ),
    )
    outside = tmp_path / "outside-project"
    outside.mkdir()
    _replace_project_root_with_directory_alias(project_root, outside)

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths,
        tmp_path / "builtins",
        project_root,
    )

    assert [entry.canonical for entry in lexicons.entries] == ["PersonalSafe"]
    assert diagnostics.count("project_root_invalid") == 1


def test_unverified_scanner_cache_is_not_loaded(tmp_path):
    """Catch scanner bytes being parsed without transaction metadata/hash proof."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = paths.for_project(project_root)
    _write_entries(
        project.scan_lexicon_file,
        _raw_entry(
            canonical="InjectedScannerTerm",
            scope="project",
            aliases=["injected"],
            project_id=project.project_id,
        ),
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins", project_root
    )

    assert "InjectedScannerTerm" not in {entry.canonical for entry in lexicons.entries}
    assert "project_scan_invalid" in diagnostics


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("generation", -1),
        ("generation", True),
        ("max_files", 0),
        ("max_files", True),
        ("max_files", 5_001),
        ("max_text_bytes", 0),
        ("max_text_bytes", True),
        ("max_text_bytes", 2_000_001),
        ("fingerprint", "not-a-sha256"),
        ("cache_sha256", "not-a-sha256"),
        ("truncated", 0),
    ),
)
def test_invalid_scanner_metadata_never_drives_source_traversal(
    tmp_path, monkeypatch, field, value
):
    """Catch malformed transaction metadata becoming traversal configuration."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "widget.py").write_text(
        "class MetadataWidget:\n", encoding="utf-8"
    )
    scan_project(project_root, paths)
    project = paths.for_project(project_root)
    state = json.loads(project.scan_state_file.read_text(encoding="utf-8"))
    state[field] = value
    project.scan_state_file.write_text(json.dumps(state), encoding="utf-8")

    def reject_metadata_traversal(*_args, **_kwargs):
        raise AssertionError("invalid scanner metadata drove source traversal")

    monkeypatch.setattr(
        project_scan_module, "_project_fingerprint", reject_metadata_traversal
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins", project_root
    )

    assert "MetadataWidget" not in {
        entry.canonical for entry in lexicons.entries
    }
    assert diagnostics.count("project_scan_invalid") == 1


def test_scanner_metadata_project_binding_must_match_the_caller(
    tmp_path, monkeypatch
):
    """Catch replaying a valid scanner transaction under another project ID."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "widget.py").write_text(
        "class ProjectBoundWidget:\n", encoding="utf-8"
    )
    scan_project(project_root, paths)
    project = paths.for_project(project_root)
    state = json.loads(project.scan_state_file.read_text(encoding="utf-8"))
    state["project_id"] = "0" * 16
    project.scan_state_file.write_text(json.dumps(state), encoding="utf-8")

    def reject_replayed_traversal(*_args, **_kwargs):
        raise AssertionError("wrong-project metadata drove source traversal")

    monkeypatch.setattr(
        project_scan_module, "_project_fingerprint", reject_replayed_traversal
    )

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins", project_root
    )

    assert "ProjectBoundWidget" not in {
        entry.canonical for entry in lexicons.entries
    }
    assert diagnostics.count("project_scan_invalid") == 1


@pytest.mark.parametrize("orphan", ("cache", "state"))
def test_orphan_scanner_transaction_is_always_diagnostic(tmp_path, orphan):
    """Catch half-published scanner state being mistaken for a missing cache."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "widget.py").write_text("class OrphanWidget:\n", encoding="utf-8")
    scan_project(project_root, paths)
    project = paths.for_project(project_root)
    if orphan == "cache":
        project.scan_state_file.unlink()
    else:
        project.scan_lexicon_file.unlink()

    lexicons, diagnostics = LexiconSet.load_with_diagnostics(
        paths, tmp_path / "builtins", project_root
    )

    assert "OrphanWidget" not in {entry.canonical for entry in lexicons.entries}
    assert diagnostics.count("project_scan_invalid") == 1


@pytest.fixture
def layer_fixture(tmp_path):
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    builtins_root = tmp_path / "builtins"
    project_root = tmp_path / "project"
    project_root.mkdir()
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


def test_all_state_layers_use_one_continuous_root_identity(tmp_path, monkeypatch):
    """Catch Q hotwords being combined with P personal data across two leases."""
    paths = StatePaths(root=tmp_path / "state")
    q_paths = StatePaths(root=tmp_path / "q-state")
    p_saved = tmp_path / "p-saved"
    q_saved = tmp_path / "q-saved"
    _write_entries(
        paths.personal_file,
        _raw_entry(
            canonical="P Personal",
            scope="personal",
            aliases=["p-personal"],
            status="confirmed",
        ),
    )
    _write_transactional_hotword(
        paths,
        _raw_entry(canonical="P Hot", scope="hot", aliases=["p-hot"]),
    )
    _write_entries(
        q_paths.personal_file,
        _raw_entry(
            canonical="Q Personal",
            scope="personal",
            aliases=["q-personal"],
            status="confirmed",
        ),
    )
    _write_transactional_hotword(
        q_paths,
        _raw_entry(canonical="Q Hot", scope="hot", aliases=["q-hot"]),
    )
    real_resolve = lexicon_module.resolve_hotword_file
    attempted = False

    def swap_to_q_only_during_hotword_resolution(authority, **kwargs):
        nonlocal attempted
        attempted = True
        swapped = False
        try:
            paths.root.rename(p_saved)
            try:
                q_paths.root.rename(paths.root)
            except BaseException:
                p_saved.rename(paths.root)
                raise
            swapped = True
        except OSError:
            pass
        try:
            return real_resolve(authority, **kwargs)
        finally:
            if swapped:
                paths.root.rename(q_saved)
                p_saved.rename(paths.root)

    monkeypatch.setattr(
        lexicon_module,
        "resolve_hotword_file",
        swap_to_q_only_during_hotword_resolution,
    )

    lexicons = LexiconSet.load(paths, tmp_path / "builtins")

    assert attempted
    assert [entry.canonical for entry in lexicons.entries] == [
        "P Personal",
        "P Hot",
    ]


def test_hotword_load_uses_the_bytes_validated_by_authority_resolution(
    tmp_path, monkeypatch
):
    """Catch reopening a replaced cache after its authoritative pointer was read."""
    paths = StatePaths(root=tmp_path / "state")
    authoritative = _raw_entry(
        canonical="Authoritative", scope="hot", aliases=["authority"]
    )
    replacement = _raw_entry(
        canonical="Replacement", scope="hot", aliases=["replacement"]
    )
    from voice_intent_normalizer.updater import UpdateStatus, update_hotwords

    data = (json.dumps(authoritative) + "\n").encode()

    class _Response:
        status = 200
        location = None

        def __init__(self, content):
            self.content = content
            self.offset = 0

        def read(self, size):
            chunk = self.content[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self):
            return None

    class _Transport:
        def open_no_redirect(self, url):
            if url.endswith("manifest.json"):
                content = json.dumps(
                    {
                        "schema_version": 1,
                        "version": "2026.07.30",
                        "data_url": (
                            "https://github.com/BEASGON/voice-intent-normalizer/"
                            "releases/download/data/zh-ai.jsonl"
                        ),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ).encode()
            else:
                content = data
            return _Response(content)

    result = update_hotwords(
        paths,
        (
            "https://github.com/BEASGON/voice-intent-normalizer/"
            "releases/download/data/manifest.json"
        ),
        _Transport(),
        datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    assert result.status is UpdateStatus.UPDATED
    real_resolve = lexicon_module.resolve_hotword_file

    def replace_after_resolution(authority, **kwargs):
        resolved = real_resolve(authority, **kwargs)
        (paths.hotwords_file.parent / "current.json").unlink()
        _write_entries(paths.hotwords_file, replacement)
        return resolved

    monkeypatch.setattr(
        lexicon_module, "resolve_hotword_file", replace_after_resolution
    )

    lexicons = LexiconSet.load(paths, tmp_path / "builtins")

    assert [entry.canonical for entry in lexicons.entries] == ["Authoritative"]


def test_missing_project_directory_stays_unavailable_for_the_whole_load(
    tmp_path, monkeypatch
):
    """Catch a missing retained project layer degrading to an alias-following path."""
    paths = StatePaths(root=tmp_path / "state")
    paths.root.mkdir()
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    project = paths.for_project(project_root)
    alias_target = tmp_path / "alias-project"
    _write_entries(
        alias_target / "project.jsonl",
        _raw_entry(
            canonical="Injected project",
            scope="project",
            project_id=project.project_id,
            aliases=["injected"],
        ),
    )
    real_guard = lexicon_module.guard_state_root

    @contextmanager
    def inject_after_acquisition(*args, **kwargs):
        with real_guard(*args, **kwargs) as lease:
            project.root.parent.mkdir(exist_ok=True)
            try:
                project.root.symlink_to(alias_target, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlinks unavailable: {exc}")
            yield lease

    monkeypatch.setattr(lexicon_module, "guard_state_root", inject_after_acquisition)

    lexicons = LexiconSet.load(
        paths, tmp_path / "builtins", project_root=project_root
    )

    assert lexicons.entries == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained root handle")
def test_lexicon_load_never_follows_root_swap_to_alias(tmp_path, monkeypatch):
    direct_paths = StatePaths(root=tmp_path / "direct-state")
    alias_root = tmp_path / "alias-state"
    moved_root = tmp_path / "moved-direct-state"
    _write_entries(
        direct_paths.personal_file,
        _raw_entry(canonical="Direct", scope="personal", aliases=["direct"]),
    )
    _write_entries(
        alias_root / "personal.jsonl",
        _raw_entry(canonical="Alias", scope="personal", aliases=["alias"]),
    )
    real_read_bytes = StateRootLease.read_bytes
    attempted = False
    blocked = False

    def swap_before_personal_read(self, relative, limit, label):
        nonlocal attempted, blocked
        if not attempted and Path(relative) == Path("personal.jsonl"):
            attempted = True
            try:
                direct_paths.root.rename(moved_root)
                direct_paths.root.symlink_to(alias_root, target_is_directory=True)
            except OSError:
                blocked = True
        return real_read_bytes(self, relative, limit, label)

    monkeypatch.setattr(StateRootLease, "read_bytes", swap_before_personal_read)

    lexicons = LexiconSet.load(direct_paths, tmp_path / "builtins")

    assert attempted
    assert blocked
    assert [entry.canonical for entry in lexicons.entries] == ["Direct"]


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

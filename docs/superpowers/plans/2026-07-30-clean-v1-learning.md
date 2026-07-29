# Clean V1 Learning Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all unreleased Task 6 migration compatibility with one strict,
restart-safe V1 learning journal and deterministic materializer.

**Architecture:** `learning-events.jsonl` is a bounded, schema-versioned journal.
Every confirmed/rejected mapping carries its generation ID and complete baseline;
materialization derives owned overlays from the journal and restores baselines when
inactive. Every mutation replays and verifies the journal before capture/append, and
state-changing events replay again after atomic journal append.

**Tech Stack:** Python 3.10+, dataclasses, JSONL, atomic `os.replace`, pytest, Ruff.

## Global Constraints

- Support only `schema_version: 1`; reject every intermediate/unversioned format.
- Preserve the public `LearningStore`, `LearningEvent`, `ControlCommand`, and
  `parse_control` interfaces.
- Preserve unrelated manual/scanner entries byte-semantically through model
  round-trip and restore same-key baselines exactly.
- Never silently overwrite an external same-key edit while a generation is active.
- Bound the journal to 8 MiB, 20,000 events, and 64 KiB per line.
- Do not add Task 7 behavior or migration compatibility.

---

### Task 1: Define strict V1 journal and generation behavior

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: `LexiconEntry`, `Scope`, `EntryStatus`, `parse_entry`.
- Produces: V1 raw events with `schema_version`, `generation_id`, and `baseline`.

- [x] **Step 1: Write failing V1 contract tests**

```python
def test_confirm_writes_versioned_generation_and_explicit_null_baseline(tmp_path):
    event = LearningStore.for_root(tmp_path).confirm(
        "Open Cloud", "OpenClaw", Scope.PERSONAL
    )
    raw = json.loads((tmp_path / "learning-events.jsonl").read_text("utf-8"))
    assert raw["schema_version"] == 1
    assert raw["event_id"] == event.event_id
    assert isinstance(raw["generation_id"], str)
    assert "baseline" in raw and raw["baseline"] is None


def test_unversioned_event_fails_closed(tmp_path):
    _write_learning_events(
        tmp_path,
        _v1_mapping_event(
            "00000000-0000-4000-8000-000000000001",
            "confirm",
            "learned",
            "OpenClaw",
            generation_id="00000000-0000-4000-8000-000000000002",
            baseline=None,
        ),
    )
    raw = _read_learning_events(tmp_path)[0]
    raw.pop("schema_version")
    _write_learning_events(tmp_path, raw)
    with pytest.raises(ValueError, match="schema_version"):
        LearningStore.for_root(tmp_path).undo_last()
```

- [x] **Step 2: Run the V1 contract tests and verify RED**

Run: `python -m pytest tests/test_learning.py -k "versioned_generation or unversioned_event" -vv`

Expected: FAIL because current events lack the strict V1 shape.

- [x] **Step 3: Implement the bounded V1 journal**

Implement strict action-specific validation, UUID generation ownership, explicit
baseline snapshots, bounded binary reads, and atomic journal replacement. Remove
all `migration_*`, fallback, provenance checkpoint, and intermediate event parsing.

- [x] **Step 4: Run the V1 contract tests and verify GREEN**

Run: `python -m pytest tests/test_learning.py -k "versioned_generation or unversioned_event" -vv`

Expected: PASS.

### Task 2: Materialize deterministic overlays and conflicts

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: validated V1 event tuple and current personal/project JSONL entries.
- Produces: exact expected entries for each affected lexicon.

- [x] **Step 1: Write failing overlay/restoration/conflict tests**

```python
def test_manual_baseline_restores_after_delete_and_undo_delete(tmp_path):
    write_jsonl_atomic(tmp_path / "personal.jsonl", (manual_entry,))
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    deletion = store.delete("OpenClaw")
    assert load_jsonl(tmp_path / "personal.jsonl") == (manual_entry,)
    assert store.undo_last() == deletion
    assert load_jsonl(tmp_path / "personal.jsonl")[0].aliases == (
        "curated",
        "learned",
    )


def test_external_same_key_edit_during_active_generation_is_a_conflict(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (external_entry,))
    with pytest.raises(RuntimeError, match="external edit conflict"):
        store.confirm("second", "OpenClaw", Scope.PERSONAL)
```

- [x] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/test_learning.py -k "baseline_restores or external_same_key" -vv`

Expected: FAIL against migration-era ownership behavior.

- [x] **Step 3: Implement V1 replay/materialization**

Group effective confirm/reject events by exact identity, require one generation and
baseline per active group, overlay aliases/negative aliases onto the baseline, and
restore only records marked `explicit-learning-v1`. Preserve inactive non-learning
records and reject active same-key records that are neither the baseline nor V1-owned.

- [x] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/test_learning.py -k "baseline_restores or external_same_key" -vv`

Expected: PASS.

### Task 3: Make append and replay failure-safe

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: atomic journal writer and atomic lexicon writer.
- Produces: pre-mutation replay, post-append replay, and exact persisted verification.

- [x] **Step 1: Write failing fault-injection tests**

```python
def test_failed_replay_retries_before_same_key_baseline_capture(tmp_path, monkeypatch):
    store = LearningStore.for_root(tmp_path)
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", fail_once)
    with pytest.raises(OSError):
        store.confirm("first", "OpenClaw", Scope.PERSONAL)
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    store.confirm("second", "OpenClaw", Scope.PERSONAL)
    assert store.personal_entries()[0].aliases == ("first", "second")


def test_partial_personal_project_replay_is_idempotent(tmp_path, monkeypatch):
    store = _store_with_personal_and_project_generations(tmp_path)
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
    monkeypatch.setattr(learning_module, "write_jsonl_atomic", real_write)
    restored = LearningStore.for_root(tmp_path).undo_last()
    assert restored is not None and restored.action == "delete"
    assert _aliases(tmp_path / "personal.jsonl") == ("personal",)
    assert _aliases(_project_file(tmp_path)) == ("project",)
```

- [x] **Step 2: Run fault tests and verify RED**

Run: `python -m pytest tests/test_learning.py -k "failed_replay or partial_personal_project" -vv`

Expected: FAIL until all mutations replay before capture and all writes verify.

- [x] **Step 3: Implement atomic append and verified replay**

Atomically replace the journal with prior bytes plus one V1 line. Replay before every
mutation/export, append state events before post-replay, write all required lexicons
atomically, reload all paths, and raise without further journal mutation on any
failure or mismatch.

- [x] **Step 4: Run fault tests and verify GREEN**

Run: `python -m pytest tests/test_learning.py -k "failed_replay or partial_personal_project" -vv`

Expected: PASS.

### Task 4: Remove compatibility scope and verify release readiness

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`
- Modify: `.superpowers/sdd/2026-07-29-voice-intent-normalizer/task-6-report.md`

**Interfaces:**
- Consumes: the approved clean V1 contract.
- Produces: no migration compatibility symbols or fixtures.

- [x] **Step 1: Delete compatibility-only tests and helpers**

Remove fixtures containing unversioned/pre-snapshot events and tests named around
legacy migration, fallback, contaminated intermediate baselines, or migration
checkpoint consumption. Retain/rewrite product tests for manual/scanner preservation,
personal/project scopes, rejection, generations, undo/delete, and write recovery.

- [x] **Step 2: Confirm migration code is absent**

Run: `rg -n "migration|fallback|pre-snapshot|legacy-state" src/voice_intent_normalizer/learning.py tests/test_learning.py`

Expected: no matches.

- [x] **Step 3: Run complete verification**

Run:

```text
python -m pytest tests/test_learning.py -v
python -m pytest -v
python -m ruff check src tests
git diff --check
```

Expected: all commands exit 0.

- [x] **Step 4: Commit the clean V1**

```text
git add src/voice_intent_normalizer/learning.py tests/test_learning.py docs/superpowers/plans/2026-07-30-clean-v1-learning.md
git commit -m "refactor: define clean v1 learning journal"
```

# Clean V1 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use completed checkbox syntax for tracking.

**Goal:** Close six clean-V1 integrity and path-safety findings without adding
support for any unreleased intermediate journal format.

**Architecture:** Keep `learning-events.jsonl` authoritative, but validate every
path-bearing identity before append and read the journal through one bounded,
regular-file snapshot. Reconcile lexicons using the same last-record-wins
effective identity as `LexiconSet` while retaining unrelated duplicate records,
and validate marker-owned state, delete cohorts, and export destinations against
their exact expected ownership.

**Tech Stack:** Python 3.10+, pathlib/os/stat, dataclasses, JSONL, pytest, Ruff.

## Global Constraints

- Accept only the clean `schema_version: 1` journal.
- Project IDs match `[A-Za-z0-9_-]+`, are at most 64 characters, and exclude
  case-insensitive Windows device names.
- A malformed public input must not append an event.
- Mixed personal/project canonical-wide deletes remain represented by a
  `scope: null, project_id: null` delete only when that is the exact common
  location derived from its currently active targets.
- Export destinations must not resolve to the state root, journal, personal or
  preferences file, or anything in projects/hotwords/adapters, and must not be
  symlinks. Dedicated export directories under the state root remain valid.
- Journal reads allocate at most 8 MiB plus one byte and reject symlinks,
  non-regular files, oversize files, and size changes during the read.
- Do not add Task 7 behavior or intermediate-format compatibility.

---

### Task 1: Validate project identity and bound journal reads

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: public `LearningStore.confirm`, raw V1 replay, and
  `learning-events.jsonl`.
- Produces: `_project_id(value) -> str` and
  `_read_regular_file_bounded(path, limit, label) -> bytes`.

- [x] **Step 1: Write failing project-ID tests**

```python
@pytest.mark.parametrize(
    "project_id",
    ["bad:id", "bad\nid", "bad\x00id", "CON", "com1", "项目", "a" * 65],
)
def test_invalid_project_id_never_reaches_the_journal(tmp_path, project_id):
    store = LearningStore.for_root(tmp_path)
    with pytest.raises(ValueError, match="project_id"):
        store.confirm("alias", "Canonical", Scope.PROJECT, project_id=project_id)
    assert not store.events_file.exists()
```

- [x] **Step 2: Verify project-ID RED**

Run: `python -m pytest tests/test_learning.py -k "invalid_project_id" -vv`

Expected: colon/newline/device-name cases either append before failing or succeed.

- [x] **Step 3: Implement the exact project-ID contract**

Use a full-match ASCII regular expression, length 1–64, and a case-insensitive
device-name set containing `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and
`LPT1`–`LPT9`. Reuse `_project_id` for public inputs and journal replay.

- [x] **Step 4: Verify project-ID GREEN**

Run: `python -m pytest tests/test_learning.py -k "project_id" -vv`

Expected: PASS with no journal created for every rejected public ID.

- [x] **Step 5: Write failing bounded-reader tests**

```python
def test_journal_reader_does_not_use_unbounded_read_bytes(tmp_path, monkeypatch):
    store = LearningStore.for_root(tmp_path)
    store.observe("seen", "SeenTool", "conversation")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("unbounded read")),
    )
    assert store.list_recent() == ()


def test_journal_symlink_is_rejected(tmp_path):
    external = tmp_path / "external.jsonl"
    source = tmp_path / "source"
    LearningStore.for_root(source).observe("seen", "SeenTool", "conversation")
    external.write_bytes((source / "learning-events.jsonl").read_bytes())
    root = tmp_path / "state"
    root.mkdir()
    (root / "learning-events.jsonl").symlink_to(external)
    with pytest.raises(ValueError, match="regular file"):
        LearningStore.for_root(root).list_recent()
```

- [x] **Step 6: Verify bounded-reader RED**

Run: `python -m pytest tests/test_learning.py -k "journal_reader or journal_symlink" -vv`

Expected: FAIL because replay calls `Path.read_bytes` and follows the symlink.

- [x] **Step 7: Implement one bounded journal snapshot**

Open the path only after `lstat` rejects links/non-regular files, use a bounded
read loop capped at `limit + 1`, compare `fstat().st_size` with the bytes read,
and return both parsed events and the same content snapshot to atomic append.

- [x] **Step 8: Verify bounded-reader GREEN**

Run: `python -m pytest tests/test_learning.py -k "journal_bounds or journal_reader or journal_symlink or atomic_append" -vv`

Expected: PASS.

### Task 2: Enforce exact overlay ownership and duplicate semantics

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: ordered `tuple[LexiconEntry, ...]`, active mapping events, baseline.
- Produces: `_effective_entry` using the last matching record and
  `_replace_effective_entry` changing only that effective record.

- [x] **Step 1: Write failing marker and duplicate tests**

```python
def test_tampered_v1_marker_is_an_external_conflict(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    tampered = replace(store.personal_entries()[0], aliases=("tampered",))
    write_jsonl_atomic(store.personal_file, (tampered,))
    with pytest.raises(RuntimeError, match="external edit conflict"):
        store.observe("seen", "SeenTool", "conversation")
    assert load_jsonl(store.personal_file) == (tampered,)


def test_duplicate_baseline_uses_last_record_and_preserves_all_records(tmp_path):
    first = _manual_entry(aliases=("first",), source="manual")
    last = _manual_entry(aliases=("last",), source=None)
    write_jsonl_atomic(tmp_path / "personal.jsonl", (first, last))
    LearningStore.for_root(tmp_path).confirm(
        "learned", "OpenClaw", Scope.PERSONAL
    )
    entries = load_jsonl(tmp_path / "personal.jsonl")
    assert entries[0] == first
    assert entries[1].aliases == ("last", "learned")
```

- [x] **Step 2: Verify ownership/duplicate RED**

Run: `python -m pytest tests/test_learning.py -k "tampered_v1_marker or duplicate_baseline" -vv`

Expected: marker tampering is overwritten and duplicate baseline capture conflicts.

- [x] **Step 3: Implement exact ownership and ordered replacement**

Compute the exact expected overlay before accepting a marker-owned current
record. Permit only the exact baseline, exact current overlay, or exact prior
overlay derived from the journal prefix during the post-append transition. Find
the last matching record for capture/replay; replace/remove only that record and
leave all earlier same-key and unrelated duplicate records byte-semantically
represented by their parsed entries.

- [x] **Step 4: Verify ownership/duplicate GREEN**

Run: `python -m pytest tests/test_learning.py -k "marker or duplicate or baseline or external" -vv`

Expected: PASS.

### Task 3: Validate delete cohorts and protect export destinations

**Files:**
- Modify: `tests/test_learning.py`
- Modify: `src/voice_intent_normalizer/learning.py`

**Interfaces:**
- Consumes: prior validated events, currently active mapping IDs, state root,
  requested export path.
- Produces: strict delete cohort validation and
  `_export_destination(path) -> Path`.

- [x] **Step 1: Write failing delete-cohort replay tests**

```python
def test_delete_target_must_be_active_and_match_identity(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("a", "ToolA", Scope.PERSONAL)
    store.confirm("b", "ToolB", Scope.PERSONAL)
    store.delete("ToolA")
    events = _read_events(tmp_path)
    events[-1]["target_event_ids"] = [events[1]["event_id"]]
    _write_events(tmp_path, events)
    with pytest.raises(ValueError, match="delete targets"):
        LearningStore.for_root(tmp_path).list_recent()


def test_delete_target_must_still_be_active(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("a", "ToolA", Scope.PERSONAL)
    store.delete("ToolA")
    events = _read_events(tmp_path)
    duplicate = {**events[-1], "event_id": str(uuid.uuid4())}
    _write_events(tmp_path, [*events, duplicate])
    with pytest.raises(ValueError, match="currently active"):
        LearningStore.for_root(tmp_path).list_recent()


def test_delete_location_must_match_its_target_cohort(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("personal", "ToolA", Scope.PERSONAL)
    store.confirm("project", "ToolA", Scope.PROJECT, project_id="p")
    store.delete("ToolA")
    events = _read_events(tmp_path)
    events[-1]["scope"] = Scope.PERSONAL.value
    _write_events(tmp_path, events)
    with pytest.raises(ValueError, match="delete location"):
        LearningStore.for_root(tmp_path).list_recent()
```

- [x] **Step 2: Verify delete-cohort RED**

Run: `python -m pytest tests/test_learning.py -k "delete_target" -vv`

Expected: malformed journals replay without a validation error.

- [x] **Step 3: Implement delete sequence validation**

Derive active mappings from the prior event sequence. Require every target to be
active, to share the delete canonical, and to derive exactly the serialized
scope/project common location. Preserve null/null only for a genuinely mixed
cohort.

- [x] **Step 4: Verify delete-cohort GREEN**

Run: `python -m pytest tests/test_learning.py -k "delete" -vv`

Expected: PASS, including canonical-wide personal/project recovery.

- [x] **Step 5: Write failing protected-export tests**

```python
@pytest.mark.parametrize(
    "relative",
    [
        "learning-events.jsonl",
        "personal.jsonl",
        "projects/p/project.jsonl",
        "projects/p/scan-state.json",
        "preferences.json",
        "hotwords/zh-ai.jsonl",
    ],
)
def test_export_rejects_every_state_owned_destination(tmp_path, relative):
    store = LearningStore.for_root(tmp_path / "state")
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    prior = store.events_file.read_bytes()
    with pytest.raises(ValueError, match="protected state"):
        store.export(store.root / relative)
    assert store.events_file.read_bytes() == prior


def test_export_rejects_destination_symlink(tmp_path):
    store = LearningStore.for_root(tmp_path / "state")
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    target = tmp_path / "target.jsonl"
    target.write_text("keep", encoding="utf-8")
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        store.export(alias)
    assert target.read_text("utf-8") == "keep"


def test_export_rejects_symlink_alias_into_state_root(tmp_path):
    store = LearningStore.for_root(tmp_path / "state")
    store.confirm("learned", "OpenClaw", Scope.PERSONAL)
    alias = tmp_path / "state-alias"
    alias.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ValueError, match="protected state"):
        store.export(alias / "personal.jsonl")
```

- [x] **Step 6: Verify protected-export RED**

Run: `python -m pytest tests/test_learning.py -k "export_rejects or export_symlink" -vv`

Expected: direct authoritative files can be replaced and symlink destinations
are not rejected.

- [x] **Step 7: Implement canonical export protection**

Validate before replay or directory creation. Reject lexical or resolved
matches for the root, journal, personal/preferences files, and the complete
projects/hotwords/adapters subtrees; reject an existing destination symlink and
return an absolute canonical destination for the existing atomic writer.

- [x] **Step 8: Verify protected-export GREEN**

Run: `python -m pytest tests/test_learning.py -k "export" -vv`

Expected: PASS.

### Task 4: Verify and commit review-fix round 1

**Files:**
- Modify: `docs/superpowers/specs/2026-07-29-voice-intent-normalizer-design.md`
- Modify: `.superpowers/sdd/2026-07-29-voice-intent-normalizer/task-6-report.md`

**Interfaces:**
- Consumes: the clean-V1 review contract.
- Produces: consistent concrete control phrase and final evidence.

- [x] **Step 1: Align the design phrase**

Change “以后把 A 理解成 B。” to the already approved and tested
“以后把 A 理解为 B。” without broadening parser behavior.

- [x] **Step 2: Confirm compatibility code remains absent**

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

- [x] **Step 4: Update the ignored Task 6 report**

Append root causes, strict red/green evidence, verification counts, limitations,
and the eventual fix commit hash to
`.superpowers/sdd/2026-07-29-voice-intent-normalizer/task-6-report.md`.

- [x] **Step 5: Commit fix round 1**

```text
git add src/voice_intent_normalizer/learning.py tests/test_learning.py docs/superpowers/specs/2026-07-29-voice-intent-normalizer-design.md docs/superpowers/plans/2026-07-30-clean-v1-review-fixes.md
git commit -m "fix: harden clean v1 learning integrity"
```

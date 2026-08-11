# Prepublication Ownership Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every staged or published generic-adapter generation durably owned before its managed-state name can exist, and make restart recovery exact, bounded, and fail-closed.

**Architecture:** Replace the unreleased status-digest marker with a canonical clean-V1 write-ahead ownership journal. The journal binds the baseline status, one candidate generation, one capsule contract, and the exact before/after digests for every status transition; the installer writes it before creating candidate state paths and retains it until a terminal status is durable. Cleanup first moves the retained exact candidate into the journal-derived private staging namespace; Windows removes retained file handles exactly, while Linux and macOS remove validated private names under the cooperative global-lock contract. Public bootstrap continues to trust only terminal `status.json`, while `install`, `doctor`, and `uninstall` recover the journal under the existing global adapter lock.

**Tech Stack:** Python 3.10+, standard library only at runtime, pytest, Ruff, setuptools/build, GitHub Actions.

## Global Constraints

- Treat `docs/superpowers/specs/2026-08-11-prepublication-ownership-journal-design.md` as authoritative.
- Preserve every public CLI option, JSON result shape, skill invocation, correction, lexicon, learning, and shared-data behavior.
- Keep the public bootstrap dependent only on protected terminal `status.json`; it must never read `transaction.json`.
- Accept only the new canonical clean-V1 journal. Reject the unreleased `{status_digest, transaction_id}` marker; do not migrate it.
- Derive staging and final names only from the validated direct-local state root, selected skill root, transaction ID, and generation ID. Never serialize or scan arbitrary paths.
- Keep first install unavailable until terminal activation and keep the old terminal generation launchable throughout upgrade preparation.
- A mismatch, alias, replacement, extra entry, non-canonical document, duplicate key, size overflow, or unsupported format fails closed without deleting or activating the object.
- Recheck that a public final name remains absent after its exact candidate is moved into private staging. A raced replacement preserves both objects and the journal.
- Windows permanent file deletion must use the retained exact handle. Linux and macOS may use name-based unlink only inside the installer-owned private staging namespace while holding the global lock; do not claim protection from a hostile same-identity process that bypasses that lock.
- POSIX validation opens potentially raced objects with `O_NONBLOCK` and `O_NOFOLLOW` before type checks, so a FIFO cannot hang a public operation.
- Preserve personal, project, preference, negative, and downloaded hotword data on recovery and normal uninstall.
- Use no new runtime dependency. Python 3.10 syntax remains the floor.
- Run pytest with a repository-external `--basetemp` and `-p no:cacheprovider`; do not recreate `.pytest_cache` inside the worktree.
- Never stage or modify the five protected pre-existing entries: `src/voice_intent_normalizer/matching.py`, `tests/test_learning.py`, `tests/test_matching.py`, `tests/test_policy.py`, and `tests/test_project_metadata.py`.
- Every production change requires a recorded RED failure, minimal GREEN, focused regression run, independent specification review, independent code-quality review, and a scoped commit.

---

### Task 1: Canonical Ownership Journal Contract

**Files:**
- Modify: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Modify: `tests/test_generic_layout.py`

**Interfaces:**
- Consumes: `GenerationRef`, `CapsuleRef`, `canonical_json_bytes()`, `status_skill_root()`, and the existing clean-V1 identifier/digest validators.
- Produces: `JournalTransition`, `OwnershipJournal`, `ownership_journal_bytes()`, and `validate_ownership_journal()`.

- [ ] **Step 1: Add failing exact-byte round-trip tests**

Add fixtures with literal references and canonical status bytes:

```python
def test_ownership_journal_round_trip_is_exact_and_status_anchored(
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    before_payload = _status(skill_root)
    before_payload["transaction"] = None
    before = canonical_json_bytes(before_payload)
    after_payload = _status(skill_root)
    after_payload["transaction"] = {
        "id": f"t-{'0' * 32}",
        "phase": "generation-published",
    }
    after = canonical_json_bytes(after_payload)
    payload = ownership_journal_bytes(
        operation="upgrade",
        transaction_id=f"t-{'0' * 32}",
        skill_root=skill_root,
        baseline_status_bytes=before,
        before_status_bytes=before,
        after_status_bytes=after,
        capsule=CapsuleRef(1, _DIGEST, "e" * 64),
        candidate=GenerationRef(
            _GENERATION_ID, _DIGEST, "d" * 64, "1.2.3"
        ),
    )

    journal = validate_ownership_journal(
        payload,
        skill_root=skill_root,
        generations_root=tmp_path / "state" / "adapters" / "generic" / "generations",
    )

    assert journal.operation == "upgrade"
    assert journal.baseline_status_digest == hashlib.sha256(before).hexdigest()
    assert journal.transition.before_digest == hashlib.sha256(before).hexdigest()
    assert journal.transition.after_digest == hashlib.sha256(after).hexdigest()
    assert payload == ownership_journal_bytes(
        operation=journal.operation,
        transaction_id=journal.transaction_id,
        skill_root=journal.selected_skill_root,
        baseline_status_bytes=before,
        before_status_bytes=before,
        after_status_bytes=after,
        capsule=journal.capsule,
        candidate=journal.candidate,
    )
```

Add the first-install case with all three before/baseline inputs `None`. Assert its canonical bytes contain exactly `baseline_status_digest`, `candidate`, `capsule`, `format`, `operation`, `selected_skill_root`, `status_transition`, and `transaction_id`.

- [ ] **Step 2: Add failing clean-V1 rejection tests**

Use one parameterized test that mutates the canonical payload and asserts `ValueError` without filesystem mutation for:

```python
@pytest.mark.parametrize(
    "mutation",
    (
        "old-status-digest-marker",
        "duplicate-key",
        "extra-field",
        "format-zero",
        "format-two",
        "unknown-operation",
        "relative-root",
        "different-root",
        "bad-transaction-id",
        "bad-generation-id",
        "bad-capsule-protocol",
        "bad-digest",
        "first-install-non-null-baseline",
        "upgrade-null-baseline",
        "before-after-equal",
        "oversized",
        "non-canonical",
    ),
)
def test_ownership_journal_rejects_invalid_clean_v1_documents(
    mutation: str, tmp_path: Path
):
    payload = _mutated_journal_bytes(mutation, tmp_path)
    with pytest.raises(ValueError, match="ownership journal"):
        validate_ownership_journal(
            payload,
            skill_root=tmp_path / "skills",
            generations_root=tmp_path / "state" / "generations",
        )
```

Also assert Windows case-equivalent canonical roots compare using the existing root contract, while UNC, mapped-drive, ADS, symlink, junction, and POSIX alias cases follow their existing platform gates.

- [ ] **Step 3: Run the contract tests and record RED**

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest tests/test_generic_layout.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task1-red" -k "ownership_journal"
```

Expected: collection or assertions fail because the four ownership-journal interfaces do not exist and the old two-field marker is still accepted by production.

- [ ] **Step 4: Implement the immutable contract**

Add these exact public shapes in `generic_layout.py`:

```python
@dataclass(frozen=True, slots=True)
class JournalTransition:
    before_digest: str | None
    after_digest: str


@dataclass(frozen=True, slots=True)
class OwnershipJournal:
    operation: str
    transaction_id: str
    selected_skill_root: Path
    baseline_status_digest: str | None
    transition: JournalTransition
    capsule: CapsuleRef
    candidate: GenerationRef
    staging_root: Path
    generation_root: Path
```

Implement:

```python
def ownership_journal_bytes(
    *,
    operation: str,
    transaction_id: str,
    skill_root: Path,
    baseline_status_bytes: bytes | None,
    before_status_bytes: bytes | None,
    after_status_bytes: bytes,
    capsule: CapsuleRef | VersionedArtifact,
    candidate: GenerationRef | VersionedArtifact,
) -> bytes:
    """Build the sole canonical clean-V1 ownership journal."""


def validate_ownership_journal(
    payload: bytes,
    *,
    skill_root: Path,
    generations_root: Path,
) -> OwnershipJournal:
    """Validate exact journal bytes and derive its only owned paths."""
```

Use the existing duplicate-key parser, constant rejection, bounded read limits, reference validators, and `canonical_json_bytes()`. Require first install to retain a null baseline for its whole transaction; its initial transition has a null before digest and its later transitions have a non-null before digest. Require upgrade to retain a non-null baseline; its initial before digest equals that baseline and later transitions use the exact current nonterminal digest. Require distinct before/after digests. Derive `staging_root` as the fixed sibling `staging/<generation-id>` and `generation_root` as `generations/<generation-id>` from the already validated generation ID.

Delete `recovery_marker_bytes()` and `validate_recovery_marker()` only after all call sites move in Task 2; during this task, leave them private/deprecated so the independently testable contract commit does not break the installer.

- [ ] **Step 5: Verify GREEN and commit the contract**

Run:

```powershell
python -m pytest tests/test_generic_layout.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task1-green" -k "ownership_journal or generic_layout_paths"
python -m ruff check src/voice_intent_normalizer/adapters/generic_layout.py tests/test_generic_layout.py
git diff --check
```

Expected: selected tests and Ruff pass; old installer tests remain unchanged.

Commit only the two task files:

```powershell
git add src/voice_intent_normalizer/adapters/generic_layout.py tests/test_generic_layout.py
git commit -m "feat: define adapter ownership journal"
```

---

### Task 2: Write-Ahead Publication and Status Transitions

**Files:**
- Modify: `src/voice_intent_normalizer/adapters/generic.py`
- Modify: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Modify: `tests/test_installer.py`
- Modify: `tests/test_generic_layout.py`

**Interfaces:**
- Consumes: Task 1's canonical journal types/builders and the existing `StateRootLease`, status-v5, artifact, smoke, and no-replace directory primitives.
- Produces: `_write_ownership_journal()`, `_advance_transaction_status()`, journal-first generation staging/publication, and terminal-status-first journal retirement.

- [ ] **Step 1: Add failing ordering and commit-boundary tests**

Instrument `StateRootLease` and adapter methods with an event list. Require this prefix on both first install and upgrade:

```python
assert events[:7] == [
    "smoke-complete",
    "journal-write",
    "journal-parent-fsync",
    "stage-created",
    "manifest-written",
    "generation-published",
    "status-generation-published",
]
```

Add concrete tests named:

- `test_first_install_journals_before_candidate_stage_exists`
- `test_upgrade_keeps_old_terminal_status_until_candidate_publication`
- `test_journal_write_failure_publishes_no_stage_or_generation`
- `test_reported_journal_fsync_failure_proceeds_only_with_exact_committed_bytes`
- `test_generation_publication_callback_failure_never_removes_journal_anchor`
- `test_each_status_transition_journals_exact_before_and_after_digests`
- `test_terminal_status_commits_before_journal_retirement`
- `test_terminal_journal_unlink_failure_returns_validated_committed_result`
- `test_terminal_journal_parent_fsync_failure_is_restart_recoverable`

For failure tests assert exact final status, bootstrap behavior, journal bytes, staging/final names, `changed_paths`, and old-generation availability. Do not accept directory-name scans in assertions or fixtures.

- [ ] **Step 2: Run the publication tests and record RED**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_generic_layout.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task2-red" -k "journals_before or journal_write_failure or journal_fsync_failure or publication_callback_failure or status_transition_journals or terminal_status_commits or terminal_journal"
```

Expected: ordering assertions fail because current code publishes before the marker exists, writes status before marker, removes the marker before terminal status, and swallows candidate cleanup failures.

- [ ] **Step 3: Replace marker writes with write-ahead transitions**

In `GenericAdapter`, replace `_write_transaction_status()` and `_write_terminal_status()` with:

```python
def _write_ownership_journal(self, payload: bytes) -> None:
    """Commit exact journal bytes or prove that the same bytes committed."""


def _advance_transaction_status(
    self,
    root: Path,
    *,
    journal: OwnershipJournal,
    before_payload: dict[str, object] | None,
    after_payload: dict[str, object],
    terminal: bool = False,
) -> OwnershipJournal:
    """Journal one exact status transition, commit status, then retire terminal metadata."""
```

`_write_ownership_journal()` must atomically replace `transaction.json`, fsync `adapters/generic`, and on `OSError` continue only when a bounded direct read equals the exact intended bytes. `_advance_transaction_status()` must validate and canonicalize both statuses, rewrite the journal with their SHA-256 digests before writing status, and for `terminal=True` validate the complete capsule/generation state before writing terminal status. Remove the journal only after the exact terminal bytes are readable and durable. A failure after terminal commit uses the existing committed-result fallback; a failure before terminal commit does not.

- [ ] **Step 4: Move journal creation ahead of managed staging**

Change first install and upgrade to build the baseline and first intended status payload before mutation, then:

```python
baseline = self._read_status_bytes()
baseline_payload = (
    None
    if current_status is None
    else status_v5_payload(
        skill_root=current_status.selected_skill_root,
        capability=current_status.capability,
        capsule=current_status.capsule,
        active=current_status.active,
        previous=current_status.previous,
    )
)
journal_bytes = ownership_journal_bytes(
    operation="first-install" if baseline is None else "upgrade",
    transaction_id=transaction_id,
    skill_root=root,
    baseline_status_bytes=baseline,
    before_status_bytes=baseline,
    after_status_bytes=canonical_json_bytes(generation_published),
    capsule=artifacts.capsule,
    candidate=artifacts.generation,
)
self._write_ownership_journal(journal_bytes)
self._stage_and_publish_generation(artifacts.generation, recovering=False)
journal = validate_ownership_journal(
    journal_bytes,
    skill_root=root,
    generations_root=generic_layout_paths(self.state_paths).generations,
)
self._advance_transaction_status(
    root,
    journal=journal,
    before_payload=baseline_payload,
    after_payload=generation_published,
)
```

Create the private stage only after the journal. In the new exclusive directory, write `generation.json` directly and fsync it first, then write only its declared files and directories, validate every hash, fsync bottom-up, and publish no-replace. Do not call `_cleanup_staged_artifact()` on a final generation after callback failure; the journal is now the durable owner. Preserve same-operation cleanup only for an identity-retained staging directory, and propagate cleanup/fsync failure instead of swallowing it.

- [ ] **Step 5: Remove the old marker contract and verify GREEN**

Delete all imports, calls, definitions, messages, and tests for `recovery_marker_bytes()`, `validate_recovery_marker()`, `_ensure_recovery_marker()`, and the status-anchored two-field marker. `transaction.json` now always means the clean-V1 ownership journal.

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_generic_layout.py tests/test_capsule_bootstrap.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task2-green" -k "journal or transaction or publication or activation or bootstrap or first_install or upgrade"
python -m ruff check src/voice_intent_normalizer/adapters/generic.py src/voice_intent_normalizer/adapters/generic_layout.py tests/test_installer.py tests/test_generic_layout.py
git diff --check
```

Expected: focused tests pass; public bootstrap still ignores `transaction.json`; all protected user files remain unstaged.

Commit only the four task files:

```powershell
git add src/voice_intent_normalizer/adapters/generic.py src/voice_intent_normalizer/adapters/generic_layout.py tests/test_installer.py tests/test_generic_layout.py
git commit -m "fix: journal generations before publication"
```

---

### Task 3: Restart Recovery, Cleanup, and Tamper Boundaries

**Files:**
- Modify: `src/voice_intent_normalizer/adapters/generic.py`
- Modify: `tests/test_installer.py`
- Modify: `tests/test_capsule_bootstrap.py`

**Interfaces:**
- Consumes: Task 2's journal-first writer and status transition helper.
- Produces: `_recover_ownership_journal()`, exact initial cleanup, later-transition continuation, terminal journal retirement, and public-operation recovery results.

- [ ] **Step 1: Add failing restarted-process recovery matrix**

Build the failure state with one adapter instance, discard it, and use a fresh `GenericAdapter` for each public operation. Add this focused test helper before parameterizing `install`, `doctor`, and `uninstall` over the states:

```python
@dataclass
class _JournalScenario:
    state_paths: StatePaths
    repository: Path
    skill_root: Path

    def restart(
        self, operation: str, state: str
    ) -> tuple[GenericAdapter, Callable[[], AdapterResult]]:
        _materialize_journal_state(self, state)
        adapter = GenericAdapter(self.repository, self.state_paths)
        if operation == "install":
            return adapter, lambda: adapter.install(
                InstallOptions(output_dir=self.skill_root)
            )
        if operation == "doctor":
            return adapter, adapter.doctor
        return adapter, lambda: adapter.uninstall(
            UninstallOptions(output_dir=self.skill_root)
        )
```

```python
@pytest.mark.parametrize("operation", ("install", "doctor", "uninstall"))
@pytest.mark.parametrize(
    "state",
    (
        "initial-journal-only",
        "initial-journal-empty-stage",
        "initial-journal-partial-stage",
        "initial-journal-final-generation",
        "later-before-status",
        "matching-after-status",
        "matching-terminal-status",
    ),
)
def test_fresh_process_recovers_exact_ownership_journal(
    operation: str, state: str, journal_scenario: _JournalScenario
):
    adapter, invoke = journal_scenario.restart(operation, state)
    result = invoke()
    _assert_complete_old_or_new_state(journal_scenario)
    _assert_no_unowned_generation(journal_scenario)
    assert len(result.changed_paths) == len(set(result.changed_paths))
```

For the initial baseline branch require exact candidate cleanup and no activation. For a later nonterminal `before` digest require bounded status-driven continuation or its defined rollback. For matching nonterminal `after`, continue recovery. For matching terminal `after`, validate the installed tree and remove only redundant journal metadata.

- [ ] **Step 2: Add failing cleanup-retry and conflict matrices**

Inject each failure once and twice: file unlink, directory removal, transaction unlink, staging-parent fsync, generations-parent fsync, and generic-parent fsync. Assert the journal remains byte-identical until every required cleanup/fsync succeeds, repeated recovery is idempotent, and the transition bound is enforced.

Parameterize preserved conflicts for status before/after digest mismatch, baseline mismatch, operation mismatch, selected-root mismatch, transaction mismatch, capsule mismatch, generation reference mismatch, final manifest mismatch, staging manifest mismatch, directory identity replacement, both staging and final names present, extra direct entry, alias/reparse entry, unknown format, old marker format, and oversized journal. Assert `doctor` is `degraded`, mutating commands fail, and every conflicting byte remains unchanged.

- [ ] **Step 3: Run recovery tests and record RED**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_capsule_bootstrap.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task3-red" -k "ownership_journal or fresh_process or cleanup_retry or journal_conflict or bootstrap_ignores"
```

Expected: fresh public operations reject or strand the new journal because `_recover_pending()` still begins from status rather than the independent ownership record.

- [ ] **Step 4: Implement journal-first public recovery**

Add:

```python
def _recover_ownership_journal(
    self,
    root: Path,
    *,
    allow_capsule_publication: bool,
) -> None:
    """Resolve one exact journal under the global lock within eight transitions."""
```

Call it before ordinary status handling in locked `install`, `doctor`, and `uninstall`. Read the journal with the existing bounded direct-file lease, validate the protected selected root before trusting other fields, and compare exact current status bytes to the transition digests:

- initial `before` equals the baseline: remove only an empty stage or a manifest-anchored, alias-free exact candidate tree; preserve the journal on any cleanup/fsync error; never activate;
- later nonterminal `before`: cross-check transaction/root/capsule/candidate/phase, then commit the exact `after` transition or its already defined rollback;
- nonterminal `after`: continue `_recover_activation()` or `_recover_uninstall()` with the journal cross-check required before every mutation;
- terminal `after`: validate the complete terminal status, capsule, active generation, and previous generation, then retire only the redundant journal;
- anything else: raise a conflict without mutating status, journal, stage, generation, or capsule.

Keep the existing eight-transition ceiling. Recovery diagnostics must distinguish incomplete recoverable work from tamper/manual-action states without exposing private editing instructions.

- [ ] **Step 5: Verify GREEN, full lifecycle, and commit**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_generic_layout.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task3-green"
python -m ruff check src/voice_intent_normalizer/adapters/generic.py tests/test_installer.py tests/test_capsule_bootstrap.py
git diff --check
```

Expected: all generic lifecycle, bootstrap, CLI, and layout tests pass with only legitimate platform skips.

Commit only the three task files:

```powershell
git add src/voice_intent_normalizer/adapters/generic.py tests/test_installer.py tests/test_capsule_bootstrap.py
git commit -m "fix: recover journal-owned adapter generations"
```

---

### Task 4: Private-Namespace Cleanup Boundary

**Files:**
- Modify: `src/voice_intent_normalizer/paths.py`
- Modify: `src/voice_intent_normalizer/adapters/generic.py`
- Modify: `tests/test_paths.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: Task 3's journal-derived staging path, retained `StateRootLease` directories, `publish_directory_no_replace()`, `publish_file_no_replace_exact()`, and bound `.journal-clean-*` names.
- Produces: `StateRootLease.remove_private_file(relative_path: Path, *, expected_bytes: bytes) -> None`, source-absence validation after exact directory moves, nonblocking POSIX destination validation, and journal-preserving conflict results.

- [ ] **Step 1: Keep the recorded RED boundary reproductions**

Retain the already recorded failing tests and split their expectations by the approved platform contract:

```python
@pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle deletion")
@pytest.mark.parametrize("relative_path", (Path("SKILL.md"), Path("generation.json")))
def test_windows_private_cleanup_deletes_only_retained_file_handle(
    relative_path: Path,
    journal_scenario: _JournalScenario,
):
    result, replacement = _replace_bound_name_at_permanent_delete(
        journal_scenario,
        relative_path,
    )
    assert replacement.exists()
    assert replacement.read_bytes() == b"replacement"
    assert journal_scenario.transaction_path.exists()
    assert result.status in {"failed", "degraded"}
```

```python
def test_exact_directory_move_preserves_raced_public_replacement(
    journal_scenario: _JournalScenario,
):
    result, isolated, replacement = _replace_final_at_native_move(
        journal_scenario
    )
    assert isolated.is_dir()
    assert replacement.is_dir()
    assert journal_scenario.transaction_path.exists()
    assert result.status in {"failed", "degraded"}
```

```python
@pytest.mark.skipif(os.name == "nt", reason="POSIX nonblocking open contract")
def test_posix_exact_publication_rejects_raced_fifo_without_blocking(
    tmp_path: Path,
):
    completed = subprocess.run(
        [sys.executable, "-c", _POSIX_FIFO_RACE_SCRIPT, str(tmp_path)],
        check=False,
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
```

The production mutations caught are: reverting Windows deletion to name-based `unlink`, omitting the post-move public-name absence check, and omitting `O_NONBLOCK` before POSIX destination type validation. Expected RED on the current Windows checkout: the two exact-handle cases delete the replacement, and the final-move case retires the journal while leaving the raced public replacement. The FIFO case is a real Linux/macOS gate and must time out or fail before the fix when run there.

- [ ] **Step 2: Implement exact Windows private-file removal**

Add this lease boundary without exposing raw paths:

```python
def remove_private_file(
    self,
    relative_path: Path,
    *,
    expected_bytes: bytes,
) -> None:
    """Remove one validated file inside an already isolated private tree."""
```

On Windows, open the direct file with `GENERIC_READ | DELETE`, `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, and `FILE_FLAG_OPEN_REPARSE_POINT`. Reject reparse points and non-regular objects, compare the complete bytes and retained file identity, then call `SetFileInformationByHandle(..., FileDispositionInfo, ...)` while the same handle remains open. Close the handle only after the disposition commit. If the bound name was replaced, the retained original may be deleted but the replacement must remain; the caller's complete-tree revalidation detects it, preserves the journal, and returns conflict.

On Linux and macOS, resolve only relative to the retained private-stage parent descriptor. Open with `O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC`, reject non-regular `fstat`, compare exact bytes, close, and call `unlinkat` for that same private name while the global adapter lock remains held. This is the approved cooperative same-identity boundary; no check-then-unlink claim is made outside the private namespace.

Replace the final name-based deletion loop in `_remove_journal_candidate()` with `remove_private_file()` for every bound data file and the bound manifest. Preserve data-first and manifest-last order, exact/deduplicated `changed_paths`, and journal-byte restitution after a committed cleanup error.

- [ ] **Step 3: Make exact directory moves preserve raced public replacements**

Strengthen `publish_directory_no_replace()` on every platform:

```python
self._native_move_retained_directory_no_replace(source, destination)
self._require_destination_identity(destination, retained_source_identity)
if self._entry_exists(source):
    raise StateRootBoundaryError("source name replaced during exact move")
```

The source check is not permission to undo the move or delete either object. In journal recovery, catch this boundary result only after recording the isolated staging path, leave both staging and final names untouched, retain exact journal bytes, and return the existing conflict diagnostic. On POSIX, destination identity mismatch after `renameat2`/`renamex_np` must use the existing no-replace restitution path before raising; on Windows, the retained handle remains the identity authority.

- [ ] **Step 4: Make POSIX destination validation nonblocking**

In `publish_file_no_replace_exact()`, add `O_NONBLOCK` to every POSIX destination open that occurs before `fstat` proves a regular file. Keep `O_NOFOLLOW` and the exact descriptor byte/identity checks. A raced FIFO, socket, device, directory, or alias raises `StateRootBoundaryError`; it must not wait for another process and must not remove the unexpected destination.

- [ ] **Step 5: Verify focused GREEN and restart safety**

Run:

```powershell
python -m pytest tests/test_paths.py tests/test_installer.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task4-green" -k "private_cleanup or exact_directory_move or raced_fifo or permanent_delete or cleanup_retry or ownership_journal"
python -m pytest tests/test_installer.py tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_generic_layout.py tests/test_paths.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task4-lifecycle"
python -m ruff check src/voice_intent_normalizer/paths.py src/voice_intent_normalizer/adapters/generic.py tests/test_paths.py tests/test_installer.py
git diff --check
```

Expected: the Windows exact-handle tests, native directory replacement test, all restart/cleanup/conflict matrices, and adjacent lifecycle tests pass; POSIX-only cases remain legitimate local skips on Windows and run in Task 4 CI.

- [ ] **Step 6: Review and commit the boundary revision**

Give a fresh specification reviewer the approved threat boundary plus only the Task 4 diff. Require separate verdicts for Windows exact deletion, POSIX cooperative private cleanup, public-name replacement preservation, FIFO nonblocking behavior, crash retry, and unchanged public workflows. Give a different code-quality reviewer the four-file diff and focused evidence. Fix Critical or Important findings with a new RED test; if a finding would require adversarial exact POSIX unlink, reject it as outside the approved contract and cite the written design.

Commit only the four task files:

```powershell
git add src/voice_intent_normalizer/paths.py src/voice_intent_normalizer/adapters/generic.py tests/test_paths.py tests/test_installer.py
git commit -m "fix: isolate journal cleanup across platforms"
```

---

### Task 5: Native CI Evidence and Task 11 Closure

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `tests/test_distribution_bundle.py`
- Modify: `.superpowers/sdd/2026-08-03-versioned-adapter-install-layout/progress.md` (ignored evidence ledger; do not force-add it)
- Create: `.superpowers/sdd/2026-08-11-prepublication-ownership-journal/final-review.md` (ignored review evidence; do not force-add it)

**Interfaces:**
- Consumes: complete journal lifecycle and existing native Windows/Linux/Darwin no-replace publication/restitution tests.
- Produces: deterministic GitHub Actions gates, recorded three-OS native evidence, final independent review, and a clean boundary for original Task 12.

- [ ] **Step 1: Add a failing workflow contract test**

Add `test_ci_runs_native_adapter_contract_on_all_supported_operating_systems()` that reads `.github/workflows/ci.yml` as UTF-8 and requires these literal jobs/runners and commands:

```python
workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
for runner in ("windows-latest", "ubuntu-latest", "macos-latest"):
    assert runner in workflow
assert "python-version: '3.10'" in workflow
assert "python -m pytest -q" in workflow
assert "test_publish_directory_no_replace" in workflow
assert "ownership_journal" in workflow
assert "python -m ruff check" in workflow
assert "python -m build --no-isolation" in workflow
```

Run:

```powershell
python -m pytest tests/test_distribution_bundle.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task5-red" -k "ci_runs_native"
```

Expected: FAIL because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 2: Create the deterministic CI workflow**

Create four jobs:

1. `quality` on `ubuntu-latest`, Python 3.12: install `.[dev]`, run Ruff, full pytest, build, and the root skill validator.
2. `python310` on `ubuntu-latest`, Python 3.10: install `.[dev]` and run the full suite.
3. `native-adapter` with `fail-fast: false`, Python 3.12, and matrix `windows-latest`, `ubuntu-latest`, `macos-latest`: run the exact no-replace publication/restitution selectors plus all ownership-journal tests with an OS-native temporary base.
4. `fresh-wheel` on `ubuntu-latest`, Python 3.12: build and run the isolated fresh-wheel lifecycle test.

Set top-level `permissions: contents: read`. Use `actions/checkout@v6`, `actions/setup-python@v6`, `python -m pip install --upgrade pip`, and `python -m pip install -e .[dev]`. Do not mark platform tests xfail or convert native failures to skips.

- [ ] **Step 3: Run local final gates**

Run fresh commands with external temp roots:

```powershell
python -m pytest tests/test_distribution_bundle.py -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-task5-green" -k "ci_runs_native"
python -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\vin-journal-full"
python -m ruff check src tests setup.py scripts/voice_intent.py
python -m build --no-isolation
python -X utf8 "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .
git diff --check
```

Build a new virtual environment outside the repository, install the just-built wheel with no checkout on `PYTHONPATH`, and run the existing full generic-adapter fresh-wheel lifecycle. Record exact pass/skip counts and the local platform in the plan ledger.

- [ ] **Step 4: Run independent review and commit CI**

Give a fresh specification reviewer the approved design, this plan, Task 1-4 commits, and the residual whole-revision finding. Require an explicit verdict for every acceptance criterion. Then give a different fresh code-quality reviewer only the final Task 1-4 diff and test evidence. Fix Important or Critical findings with RED/GREEN and repeat only the affected review.

Commit the workflow and its contract test:

```powershell
git add .github/workflows/ci.yml tests/test_distribution_bundle.py
git commit -m "ci: verify native adapter publication"
```

- [ ] **Step 5: Create the private repository and record native evidence**

Create private GitHub repository `BEASGON/voice-intent-normalizer`, add it as `origin`, and push `codex/voice-intent-normalizer`. Keep it private until the original Tasks 12-15 and all release gates pass. Wait for `quality`, `python310`, `fresh-wheel`, and all three `native-adapter` matrix jobs; inspect failures rather than rerunning blindly.

When every job is green, record links, commit SHA, OS job names, pass/skip counts, and review verdicts in the ignored evidence ledger. Append the closure to `.superpowers/sdd/2026-08-03-versioned-adapter-install-layout/progress.md`, mark original Task 11 complete, and resume Task 12 of `docs/superpowers/plans/2026-07-29-voice-intent-normalizer.md`.

## Plan Completion Gate

The follow-up is complete only when:

1. the old two-field marker is absent from production and tests;
2. a journal is durable before any managed candidate path exists;
3. every status write uses an exact write-ahead before/after digest pair;
4. initial-baseline recovery never activates the candidate and preserves the old upgrade terminal status;
5. an exact public candidate moves into private staging before permanent cleanup, and a raced public replacement preserves both objects plus the journal;
6. Windows permanent file removal targets the retained exact handle, while POSIX cleanup stays inside the private namespace under the cooperative global-lock contract;
7. POSIX destination validation rejects FIFOs and other unsafe objects without blocking;
8. later, after, terminal, cleanup-failure, restart, and tamper branches pass the exhaustive matrix;
9. public bootstrap remains terminal-status-only and shared data is untouched;
10. full tests, Ruff, build, skill validation, fresh-wheel lifecycle, and diff checks pass;
11. independent specification and code-quality reviews are clean;
12. native Windows, Linux, and macOS GitHub Actions jobs pass on the same pushed commit;
13. the original Task 11 ledger is closed and Task 12 resumes without another user decision.

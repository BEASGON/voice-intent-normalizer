# Versioned Adapter Install Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic adapter's file-by-file live package transactions with a stable V1 host capsule and complete immutable runtime generations without changing the public adapter, CLI, skill, lexicon, or learning behavior.

**Architecture:** Keep one immutable, host-visible V1 capsule containing the agent-facing skill contract and a self-contained bootstrap. Store complete runtime generations in the canonical private state root, validate and fsync them before publication, and activate a generation only through protected status. Normal upgrades switch the active generation; rollback switches back; uninstall deactivates first and removes only the verified capsule and private installer-owned generations.

**Tech Stack:** Python 3.10+, standard library only at runtime, pytest, Ruff, setuptools, Agent Skills root package.

## Global Constraints

- Preserve `PlatformAdapter.detect()`, `install(options)`, `doctor()`, `uninstall(options)`, `InstallOptions`, `UninstallOptions`, `AdapterResult`, and all current CLI options and JSON result shapes.
- Preserve the clean-V1 fenced JSON response contract, skill name, Codex/OpenClaw/WorkBuddy/generic invocation workflow, normalization behavior, explicit learning, project scanning, and hotword updates.
- Keep personal, project, preference, negative, and downloaded hotword data only under the shared `VOICE_INTENT_HOME`; never copy them into a capsule or runtime generation.
- Continue to require a direct canonical local state root and reject symlink, junction/reparse, UNC/network, mapped-drive, path-escape, alternate-data-stream, and unsupported alias forms.
- Use no runtime dependency beyond the Python 3.10 standard library. `tomli` remains development-only for Python 3.10 packaging tests.
- Treat the approved design at `docs/superpowers/specs/2026-08-03-versioned-adapter-install-layout-design.md` as authoritative.
- Clean V1 supports only the new status/layout format. Unreleased Task 11 intermediate status, staging, quarantine, and per-file transaction formats fail closed; do not add migration code for them.
- Never stage or modify the five pre-existing stat-only worktree entries in `matching.py`, `test_learning.py`, `test_matching.py`, `test_policy.py`, and `test_project_metadata.py`.
- Every production behavior change follows RED, verified failure reason, minimal GREEN, refactor, focused verification, and an independent task review.

---

### Task 1: Versioned Layout, Manifest, and Status Contracts

**Files:**
- Create: `src/voice_intent_normalizer/adapters/generic_contract.py`
- Create: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Modify: `src/voice_intent_normalizer/paths.py`
- Create: `tests/test_generic_layout.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: `StatePaths`, `StateRootLease`, canonical JSON helpers, direct-local state-root contract.
- Produces: dependency-free `generic_contract.py` with `GenerationRef`, `CapsuleRef`, `build_manifest()`, `validate_manifest()`, `validate_status_v5()`, and `canonical_json_bytes()`; `generic_layout.py` with `GenericLayoutPaths` and `generic_layout_paths()`.

- [ ] **Step 1: Write failing pure-contract tests**

Add literal fixtures that require the private layout and do not create it during resolution:

```python
def test_generic_layout_paths_are_private_and_read_only(tmp_path: Path):
    state = StatePaths(tmp_path / "state")
    layout = generic_layout_paths(state)
    assert layout.adapter_root == state.root / "adapters" / "generic"
    assert layout.status == layout.adapter_root / "status.json"
    assert layout.transaction == layout.adapter_root / "transaction.json"
    assert layout.generations == layout.adapter_root / "generations"
    assert layout.staging == layout.adapter_root / "staging"
    assert layout.retired == layout.adapter_root / "retired"
    assert not state.root.exists()
```

Add tests with hand-written manifests/status objects. Require:

- `CAPSULE_PROTOCOL == 1`, `GENERATION_FORMAT == 1`, `STATUS_FORMAT == 5`, and layout string `versioned-v1`;
- generation IDs matching `g-[0-9a-f]{64}-[0-9a-f]{32}` and transaction IDs matching `t-[0-9a-f]{32}`;
- canonical manifest keys `format`, `kind`, `identifier`, `package_version`, `package_hash`, `files`, and `file_hashes`;
- sorted unique POSIX-relative files, maximum 4096 files, path length 512, depth 32, lowercase SHA-256, canonical JSON, and exact aggregate hash;
- status roots reconstructed from trusted `skill_root` plus validated identifiers, never arbitrary generation paths from JSON;
- rejection of unknown keys, duplicate JSON keys, aliases, traversal, Windows drive/UNC/device/ADS strings, future formats, mismatched digests, and unanchored previous generations.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m pytest tests/test_generic_layout.py tests/test_installer.py -q -k "layout or manifest or status_v5"
```

Expected: collection/import failures for the missing `generic_layout` module and missing layout/status-v5 behavior.

- [ ] **Step 3: Implement the immutable value contracts**

Create dependency-free frozen slot dataclasses and exact constants in `generic_contract.py`:

```python
CAPSULE_PROTOCOL = 1
GENERATION_FORMAT = 1
STATUS_FORMAT = 5
LAYOUT_NAME = "versioned-v1"

@dataclass(frozen=True, slots=True)
class GenericLayoutPaths:
    adapter_root: Path
    status: Path
    transaction: Path
    generations: Path
    staging: Path
    retired: Path

@dataclass(frozen=True, slots=True)
class GenerationRef:
    generation_id: str
    manifest_digest: str
    package_hash: str
    package_version: str

@dataclass(frozen=True, slots=True)
class CapsuleRef:
    protocol: int
    manifest_digest: str
    package_hash: str
```

The contract module may import only Python 3.10 standard-library modules and must use no package-relative imports, filesystem mutation, or host state. This exact source file will be copied into the installed capsule in Task 2. Implement bounded validation without quadratic duplicate checks. Return immutable tuples/mappings or fresh ordinary objects; never retain caller-owned mutable dictionaries.

In `generic_layout.py`, add `StatePaths.generic_adapter_root()` integration and expose the future `adapters/generic/status.json` path through `generic_layout_paths()`. Keep the public `adapter_status_file("generic")` accessor on its existing legacy path during Tasks 1 and 2 so the unchanged installer and its `changed_paths` contract remain internally consistent. Task 3 switches that accessor atomically with the versioned installer migration. Other fixed adapter identifiers remain on their existing contract until their own tasks adopt private layouts.

- [ ] **Step 4: Verify GREEN and compatibility**

Run:

```powershell
python -m pytest tests/test_generic_layout.py tests/test_paths.py tests/test_installer.py -q -k "layout or manifest or status_v5 or adapter_status"
python -m ruff check src/voice_intent_normalizer/adapters/generic_contract.py src/voice_intent_normalizer/adapters/generic_layout.py src/voice_intent_normalizer/paths.py tests/test_generic_layout.py tests/test_installer.py
```

Expected: all selected tests pass and Ruff reports no findings.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/voice_intent_normalizer/adapters/generic_contract.py src/voice_intent_normalizer/adapters/generic_layout.py src/voice_intent_normalizer/paths.py tests/test_generic_layout.py tests/test_installer.py
git commit -m "refactor: define versioned adapter layout"
```

### Task 2: Stable V1 Capsule and Dual-Mode Bootstrap

**Files:**
- Modify: `scripts/voice_intent.py`
- Modify: `src/voice_intent_normalizer/adapters/generic_contract.py`
- Modify: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Create: `tests/test_capsule_bootstrap.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_distribution_bundle.py`

**Interfaces:**
- Consumes: validated status-v5 schema and generation identifiers from Task 1, existing source-only bootstrap rules from Task 9.
- Produces: `capsule_source_files(repository) -> dict[str, bytes]`, `generation_source_files(repository) -> dict[str, bytes]`, checkout-mode and installed-capsule-mode `scripts/voice_intent.py`.

- [ ] **Step 1: Write failing capsule/bootstrap tests**

Construct a real temporary capsule and generation, then run the copied bootstrap in a subprocess:

```python
def test_capsule_bootstrap_runs_only_the_status_anchored_generation(
    tmp_path: Path,
    built_capsule: Path,
    built_generation: Path,
):
    env = os.environ.copy()
    env["VOICE_INTENT_HOME"] = str(tmp_path / "state")
    result = subprocess.run(
        [sys.executable, str(built_capsule / "scripts" / "voice_intent.py"),
         "doctor", "--json"],
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "ok"
```

Add subprocess marker tests proving installed-capsule mode rejects:

- absent/duplicate-key/malformed/future status;
- arbitrary absolute or traversal generation identifiers;
- generation directory, `src`, package, `__init__.py`, `cli.py`, dependency, bytecode, native-module, symlink, junction, and reparse escapes;
- manifest/file hash mismatch and status/manifest anchor mismatch;
- ambient `PYTHONPATH`, preloaded modules, and existing `__pycache__` artifacts.

Keep checkout-mode tests proving the current root script behavior is unchanged. Assert `capsule_source_files()` contains exactly `SKILL.md`, `agents/openai.yaml`, the three direct references, `scripts/voice_intent.py`, and generated `capsule.json`. Assert generation files are the exact allowlisted runtime repository needed by `cli._runtime_repository()` and contain no tests, `.git`, `.superpowers`, personal state, project state, or downloaded hotwords.

- [ ] **Step 2: Run bootstrap tests and verify RED**

Run:

```powershell
python -m pytest tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_distribution_bundle.py -q
```

Expected: installed-capsule cases fail because the current bootstrap requires a sibling `src` tree and the capsule/generation builders do not exist.

- [ ] **Step 3: Implement dual-mode bootstrap**

Keep the bootstrap self-contained. Select checkout mode only when a direct sibling `src/voice_intent_normalizer` passes the existing preflight. Otherwise require a direct capsule with canonical `capsule.json`, verify `scripts/_voice_intent_contract.py` against the protected capsule digest, load that exact helper by physical path, resolve the canonical local state root, parse `adapters/generic/status.json` with the shared recursive duplicate-key rejection, reconstruct `generations/<validated-id>`, validate the generation manifest and package tree, isolate bytecode, clear preloaded modules, and import the exact generation `cli.py`.

Use this decision boundary:

```python
def _runtime_source(entry_script: Path) -> Path:
    capsule_or_checkout = entry_script.resolve(strict=True).parents[1]
    checkout_src = capsule_or_checkout / "src"
    if _is_complete_direct_checkout(checkout_src):
        return _trusted_source(checkout_src)
    return _installed_generation_source(capsule_or_checkout)
```

The installed branch must derive paths from validated fixed names and IDs only. It must not import `voice_intent_normalizer` before source origin verification.

- [ ] **Step 4: Implement capsule and generation allowlists**

In `generic_layout.py`, define explicit POSIX-relative allowlists for agent-visible capsule files and runtime repository roots. Include `generic_contract.py` byte-for-byte as capsule `scripts/_voice_intent_contract.py`. Read every source through direct-file/no-alias checks, enforce byte/file/depth limits, and build canonical manifests from immutable bytes. Ensure the generated capsule manifest hashes the bootstrap, shared validator, and all agent-visible files but never hashes itself recursively.

- [ ] **Step 5: Verify GREEN**

Run:

```powershell
python -m pytest tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_distribution_bundle.py -q
python -m ruff check scripts/voice_intent.py src/voice_intent_normalizer/adapters/generic_contract.py src/voice_intent_normalizer/adapters/generic_layout.py tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_distribution_bundle.py
```

Expected: checkout and installed-capsule modes pass; malicious/ambient sources never create marker files.

- [ ] **Step 6: Commit Task 2**

```powershell
git add scripts/voice_intent.py src/voice_intent_normalizer/adapters/generic_contract.py src/voice_intent_normalizer/adapters/generic_layout.py tests/test_capsule_bootstrap.py tests/test_cli.py tests/test_distribution_bundle.py
git commit -m "feat: add stable capsule bootstrap"
```

### Task 3: Complete Generation Publication and First Activation

**Files:**
- Modify: `src/voice_intent_normalizer/adapters/generic.py`
- Modify: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Modify: `src/voice_intent_normalizer/paths.py`
- Modify: `tests/test_installer.py`
- Modify: `tests/test_generic_layout.py`

**Interfaces:**
- Consumes: Task 1 schemas and paths; Task 2 capsule/generation byte sets and bootstrap.
- Produces: status-v5 first install, complete generation publication, complete no-replace capsule publication, exact `changed_paths`, idempotent current-package detection.

- [ ] **Step 1: Write failing first-install transaction tests**

Add deterministic fault-injection tests for each phase. The observable contract is:

```python
def test_failed_generation_write_exposes_no_partial_runtime(
    tmp_path: Path, generic_adapter: GenericAdapter, monkeypatch: pytest.MonkeyPatch
):
    # Inject a write/fsync failure after a prefix has been written in private staging.
    result = generic_adapter.install(InstallOptions(output_dir=tmp_path / "skills"))
    assert result.status == "failed"
    assert not (tmp_path / "skills" / "voice-intent-normalizer").exists()
    assert not any((tmp_path / "state" / "adapters" / "generic" /
                    "generations").glob("g-*"))
```

Cover:

- staged files are private and no final generation name exists before full fsync/validation;
- a generated directory is published whole under a unique no-replace name;
- first capsule publication is complete-or-absent and rejects a concurrently appearing target;
- status activation occurs only after capsule, generation, manifest validation, and smoke test;
- a failed status write leaves an inert complete capsule and generation that recovery can safely adopt or retire, never a reported successful install;
- the installer migration and public `adapter_status_file("generic")` switch to `adapters/generic/status.json` land together, so no intermediate commit reports a status path the active installer does not write;
- first-use `changed_paths` includes state root, adapter directories, generation, capsule, status, and transaction paths exactly once;
- repeated install returns `already-installed`, creates no staging/generation residue, and changes no files;
- strict generic mode remains fail-closed before mutation.

- [ ] **Step 2: Run first-install tests and verify RED**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_generic_layout.py -q -k "generation or capsule or first_install or already_installed or strict"
```

Expected: the current adapter publishes individual files into the live skill directory and fails the versioned layout assertions.

- [ ] **Step 3: Add retained directory publication primitives**

Implement `StateRootLease.publish_directory_no_replace(source, destination, expected_identity)` for direct child directories. Use exact-handle Windows rename, Linux `renameat2(RENAME_NOREPLACE)`, and Darwin `renamex_np(RENAME_EXCL)` with verified signatures. Unsupported platforms fail before mutation. Both source and destination parents remain under retained direct-directory authority; destination must not exist; the moved directory identity must equal `expected_identity` after publication. Do not add a stat-then-generic-rename fallback.

- [ ] **Step 4: Replace first-install orchestration**

Refactor `GenericAdapter._install_locked()` around these operations:

```python
artifacts = self._prepare_versioned_artifacts()
generation = self._stage_and_publish_generation(artifacts.generation)
capsule = self._ensure_capsule(artifacts.capsule)
self._smoke_generation(capsule, generation)
status_path = self._activate_generation(capsule, generation, options)
return self._verified_result("installed", options, changed_paths)
```

Write all runtime bytes only inside the private staging directory. Write each file via a sibling temporary name, fsync, rename inside staging, then fsync the parent. Write the generation manifest last. Publish the complete generation directory to its unique ID, publish a complete capsule directory no-replace on first install, and atomically write status v5 last. In this same integration step, switch `adapter_status_file("generic")` to `adapters/generic/status.json` and update installer result accounting/tests together. Remove the obsolete status-v4/per-file install path; clean V1 does not branch on old formats.

- [ ] **Step 5: Verify first-install GREEN**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_generic_layout.py tests/test_capsule_bootstrap.py -q -k "generation or capsule or first_install or already_installed or strict or bootstrap"
python -m ruff check src/voice_intent_normalizer/adapters/generic.py src/voice_intent_normalizer/adapters/generic_layout.py src/voice_intent_normalizer/paths.py tests/test_installer.py tests/test_generic_layout.py
```

Expected: selected tests pass; no live partial file exists in any failure case.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/voice_intent_normalizer/adapters/generic.py src/voice_intent_normalizer/adapters/generic_layout.py src/voice_intent_normalizer/paths.py tests/test_installer.py tests/test_generic_layout.py
git commit -m "refactor: publish complete runtime generations"
```

### Task 4: Upgrade, Recovery, Rollback, Doctor, and Uninstall

**Files:**
- Modify: `src/voice_intent_normalizer/adapters/generic.py`
- Modify: `src/voice_intent_normalizer/adapters/generic_layout.py`
- Modify: `tests/test_installer.py`
- Modify: `adapters/generic/README.zh-CN.md`

**Interfaces:**
- Consumes: complete capsule/generation publication and status v5.
- Produces: status-anchored generation switching, rollback, idempotent recovery, verified doctor states, capsule-level uninstall, bounded private cleanup.

- [ ] **Step 1: Write failing lifecycle tests**

Use two literal package versions and real temporary layouts. Require:

```python
def test_upgrade_switches_complete_generation_and_keeps_previous(
    installed_v1: GenericAdapter, repository_v2: Path
):
    result = GenericAdapter(repository_v2, installed_v1.state_paths).install(
        InstallOptions(output_dir=installed_v1.skill_root)
    )
    status = read_status_v5(installed_v1.state_paths)
    assert result.status == "upgraded"
    assert status.active.package_version == "0.2.0"
    assert status.previous.package_version == "0.1.0"
    assert installed_v1.capsule_bytes_after == installed_v1.capsule_bytes_before
```

Add phase-by-phase interruption tests for generation published, capsule published, activation pending, status write failed, rollback pending, deactivation pending, capsule retired, generation cleanup, and status removal. For every restart through `install`, `doctor`, and `uninstall`, assert an idempotent old-or-new complete state, never a mixed state.

Cover:

- upgrade never rewrites the stable V1 capsule;
- failed new-generation smoke or activation leaves the previous generation active;
- explicit recovery can activate a fully anchored generation or restore previous status, never infer ownership from a name;
- doctor validates capsule, active and previous manifests, direct identities, bootstrap reachability, capability, and shared-state access;
- missing/malformed/duplicate-key/future/unanchored/tampered status or manifest returns degraded and never re-anchors;
- uninstall deactivates first, quarantines/removes the whole verified capsule, removes only status-anchored private generations, preserves shared lexicons by default, and returns success only after doctor reports not-installed;
- capsule identity replacement or an unknown file in the capsule makes uninstall fail without deleting the replacement; on POSIX a provisional no-replace move to a unique tombstone must verify the retained directory identity and restore the moved replacement no-replace before returning failure;
- one adapter uninstall does not affect any other adapter state;
- private staging/retired cleanup is bounded, identity-checked, and may be reported as deferred without changing the active result.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run:

```powershell
python -m pytest tests/test_installer.py -q -k "upgrade or recovery or rollback or doctor or uninstall or capsule_identity"
```

Expected: current per-file journal behavior and missing generation-switch lifecycle fail the new assertions.

- [ ] **Step 3: Implement status-anchored lifecycle**

Use one status-v5 transaction object embedded in protected status and an independent recovery marker containing only transaction ID plus status digest. Permit these explicit phases:

```python
TRANSACTION_PHASES = (
    "generation-published",
    "capsule-published",
    "activation-pending",
    "rollback-pending",
    "deactivation-pending",
    "capsule-retired",
    "cleanup-pending",
)
```

Each recovery transition validates root, capsule, generation IDs, manifest digests, and directory identities from the trusted status anchor before mutation. Upgrade publishes a new generation and changes only status. Rollback changes only status. Uninstall first writes inactive transaction state, then removes the single verified capsule unit, then removes/retains only anchored private generations, and finally removes adapter state.

For POSIX capsule removal, atomically rename the capsule no-replace to a unique tombstone, compare the tombstone directory identity with the retained expected identity, and rename the tombstone back no-replace before failure when they differ. Recursively remove only an identity-confirmed tombstone under retained authority. Windows uses the exact retained handle. Unknown or replaced objects survive and produce a failed/degraded result.

- [ ] **Step 4: Remove obsolete per-file transaction machinery**

Delete status-v4 parsing, per-file quarantine manifests, `move_no_replace()` use from generic install/upgrade/uninstall, old staging/quarantine naming, and unreachable recovery branches. Keep platform file primitives only where other state-root features still consume them. Ensure the new `generic.py` delegates schema/path validation to `generic_layout.py` rather than duplicating it.

- [ ] **Step 5: Update generic adapter documentation**

Document the unchanged commands and user workflow in `adapters/generic/README.zh-CN.md`. Explain only the observable guarantees: complete-version activation, automatic rollback, shared-data preservation, and manual action after detected third-party interference. Do not expose internal recovery instructions that encourage manual editing of private status.

- [ ] **Step 6: Verify lifecycle GREEN**

Run:

```powershell
python -m pytest tests/test_installer.py tests/test_cli.py tests/test_capsule_bootstrap.py -q
python -m ruff check src tests/test_installer.py tests/test_capsule_bootstrap.py
```

Expected: all installer, CLI, and bootstrap tests pass; only platform-gated tests skip.

- [ ] **Step 7: Commit Task 4**

```powershell
git add src/voice_intent_normalizer/adapters/generic.py src/voice_intent_normalizer/adapters/generic_layout.py tests/test_installer.py adapters/generic/README.zh-CN.md
git commit -m "feat: activate and recover immutable skill versions"
```

### Task 5: Distribution, Skill Validation, and Cross-Platform Regression

**Files:**
- Modify: `setup.py`
- Modify: `src/voice_intent_normalizer/cli.py`
- Modify: `tests/test_distribution_bundle.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_installer.py`
- Modify: `tests/test_skill_package.py`

**Interfaces:**
- Consumes: completed versioned adapter layout and stable capsule.
- Produces: exact wheel/sdist bundle, fresh-venv installation, public CLI compatibility, skill validation, platform contract coverage.

- [ ] **Step 1: Write failing distribution and compatibility tests**

Require the wheel and sdist to contain the exact allowlisted source bundle needed to build both capsule and generations, with no stale generated files. In a fresh virtual environment:

1. install the built wheel;
2. create an isolated `VOICE_INTENT_HOME` and generic skill root;
3. call `default_installer().install()`;
4. execute the installed capsule bootstrap for `doctor --json` and `normalize --json`;
5. perform no-op reinstall, a fixture upgrade, rollback/recovery interruption, and uninstall;
6. assert shared personal/project fixtures survive uninstall.

Add Python 3.10 AST/grammar checks and platform-gated contract tests for Windows exact-handle/no-replace directory publication, Linux `renameat2(RENAME_NOREPLACE)`, and Darwin `renamex_np(RENAME_EXCL)`. Each unavailable native primitive must fail before mutation.

- [ ] **Step 2: Run distribution tests and verify RED**

Run:

```powershell
python -m pytest tests/test_distribution_bundle.py tests/test_cli.py tests/test_skill_package.py tests/test_installer.py -q
```

Expected: the current build bundle and fresh-wheel flow do not implement the versioned capsule lifecycle.

- [ ] **Step 3: Update deterministic package assembly**

Make `setup.py` clear only the exact generated `_skill_bundle`, copy the explicit allowlist, and include every Python source required for generation creation. Reject missing, duplicate, aliased, stale, or extra bundled files.

Define the generation manifest filename as `generation.json`. Update `cli._runtime_repository()` so it accepts exactly three physical source forms:

1. a checkout whose imported module is the exact `repository/src/voice_intent_normalizer` tree;
2. the exact `importlib.resources` `_skill_bundle` shipped by the installed wheel;
3. a generation root whose `generation.json` validates the exact imported module tree and complete runtime allowlist.

Reject a directory that merely resembles one of those roots, an ambient parent checkout, a mismatched generation manifest, and a generation selected only through `PYTHONPATH`.

- [ ] **Step 4: Verify the root skill and full project**

Run fresh, complete commands:

```powershell
python -X utf8 C:\Users\Administrator\.codex\skills\.system\skill-creator\scripts\quick_validate.py .
python -m pytest -q
python -m ruff check src tests setup.py scripts/voice_intent.py
python -m build --no-isolation
git diff --check
```

Then create a new temporary virtual environment, install the just-built wheel without the checkout on `PYTHONPATH`, and run the fresh-wheel lifecycle test. Expected: validator, full tests, Ruff, build, diff check, and isolated lifecycle all pass. Record exact passed/skipped counts and platform skips.

- [ ] **Step 5: Mutation and forward checks**

Temporarily mutate test fixtures—not committed production—to prove tests fail for: activation before fsync, status selecting staging, capsule rewrite during upgrade, partial final-name publication, unanchored generation adoption, uninstall deleting an identity replacement, ambient checkout selection, and duplicate JSON keys. Restore fixtures and rerun the focused suites.

Run two isolated forward checks with temporary state only:

- install and normalize the OpenClaw/WorkBuddy homophone examples through the installed capsule;
- learn a project-scoped correction in project A, verify it is absent in project B and after generic adapter reinstall/upgrade.

Delete temporary evaluation artifacts after capturing results.

- [ ] **Step 6: Commit Task 5**

```powershell
git add setup.py src/voice_intent_normalizer/cli.py tests/test_distribution_bundle.py tests/test_cli.py tests/test_installer.py tests/test_skill_package.py
git commit -m "test: verify versioned adapter distribution"
```

## Plan Completion Gate

After all five tasks pass their independent task reviews:

1. Run one review package from the pre-revision base `8013eaa` to the revision head.
2. Dispatch a fresh high-capability whole-revision reviewer with this plan, the approved design, the new ledger, and all deferred Task 11 findings.
3. Permit one consolidated final fix wave and one scoped re-review, following `subagent-driven-development`.
4. When clean, append the approved-scope result to the original Task 11 ledger, mark original Task 11 complete, and resume Task 12 of `2026-07-29-voice-intent-normalizer.md` without changing the user's workflow.

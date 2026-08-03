# Versioned Adapter Install Layout Design

**Status:** Approved on 2026-08-03

**Scope:** Task 11 scope revision for the approved clean V1

**Supersedes:** Only the file-by-file publication and removal mechanics in section 12 of `2026-07-29-voice-intent-normalizer-design.md`

**Preserves:** The public adapter API, CLI, skill behavior, lexicons, learning data, host invocation, and user workflow

## 1. Goal

Replace the generic adapter's file-by-file live-directory transactions with a versioned, immutable installation layout. A version becomes visible only after its complete contents have been written, flushed, validated, and smoke-tested. Upgrades and rollbacks activate complete versions instead of replacing individual live files.

The change must resolve both open Task 11 findings:

1. POSIX upgrade and uninstall must not depend on a non-portable rename-or-delete-by-open-file-descriptor operation.
2. A failed write must never expose or strand a partial runtime file at the host-visible final path.

## 2. Non-negotiable compatibility

The following behavior remains unchanged:

- Users run the same `install`, `doctor`, and `uninstall` commands with the same options.
- `PlatformAdapter.detect()`, `install(options)`, `doctor()`, and `uninstall(options)` keep their current signatures and result types.
- Codex, OpenClaw, WorkBuddy, and generic Agent Skills hosts discover the same skill name and invoke the same correction workflow.
- The root `SKILL.md` response contract remains the clean-V1 fenced JSON contract.
- Normalization, risk policy, project scanning, explicit learning, and hotword updates do not change.
- `VOICE_INTENT_HOME` remains the only shared state root. Personal, project, negative, preference, and hotword data are not copied into version directories.
- Installing or uninstalling one adapter does not modify another adapter's installation or shared lexicons.
- Normal successful uninstall removes the selected platform's discoverable skill capsule while preserving shared data unless `remove_shared_data=True`.

## 3. Considered approaches

### 3.1 Continue hardening file-by-file transactions

Rejected. Windows can bind a rename to an open handle, but portable POSIX cannot unlink or rename an already validated directory entry by file descriptor. Additional `stat` checks only move the race window.

### 3.2 Point the host directly at versioned skill directories

Rejected for the generic adapter. Generic hosts discover directories rather than a shared activation registry; leaving several versions visible can create duplicate skills, while changing a symlink or common directory name recreates the same live-name problem.

### 3.3 Stable V1 capsule plus immutable runtime generations

Selected. The host-visible capsule is installed as one complete directory and stays byte-for-byte stable throughout clean V1. It contains the stable skill instructions and a small bootstrap. Runtime generations live in an installer-owned private namespace and are activated through protected adapter state. Ordinary upgrades never mutate the discoverable capsule.

## 4. Filesystem layout

For a selected host skill root `<skill-root>` and shared state root `<state-root>`:

```text
<skill-root>/
└── voice-intent-normalizer/             # immutable V1 host capsule
    ├── SKILL.md                          # stable V1 invocation contract
    ├── agents/openai.yaml                # stable V1 discovery metadata
    ├── references/                       # stable V1 agent-facing contracts
    │   ├── correction-policy.md
    │   ├── domain-packs.md
    │   └── lexicon-schema.md
    ├── scripts/voice_intent.py           # stable V1 bootstrap
    └── capsule.json                      # canonical capsule manifest

<state-root>/
└── adapters/generic/
    ├── status.json                       # protected adapter status + active generation
    ├── transaction.json                  # status-anchored recovery marker
    ├── generations/
    │   ├── <generation-id>/              # complete allowlisted runtime repository
    │   └── <previous-generation-id>/
    ├── staging/
    │   └── <transaction-id>/             # incomplete, never host-visible
    └── retired/
        └── <generation-id>/              # inactive installer-owned generations
```

`generation-id` is derived from the validated package digest plus an installer nonce. Directory names accept only the documented bounded ASCII grammar. Every generation contains a canonical manifest binding its format version, package version, file list, file digests, and generation ID.

The capsule contains every file an agent reads directly: `SKILL.md`, UI metadata, and the three referenced V1 contracts. These files are versioned together as the stable capsule protocol. A runtime generation contains the complete allowlisted repository subset required to run the CLI and build adapters: Python source, built-in lexicon assets, license/package metadata, and protocol files. Some immutable protocol files therefore appear in both the capsule and the self-contained generation, but only the capsule is host-visible. A generation never contains personal, project, preference, negative, or downloaded hotword state.

The capsule bootstrap resolves `VOICE_INTENT_HOME`, validates protected generic-adapter status, opens the selected generation inside the retained state-root authority, verifies its manifest anchor, and launches the runtime through the existing source-only bootstrap boundary. It never trusts `PYTHONPATH`, ambient checkout files, symlinks, junctions, or a generation path supplied by user text.

## 5. Installation and activation

### 5.1 Build a generation

1. Acquire the existing canonical state-root lease and generic-adapter transaction lock.
2. Create a unique private staging directory exclusively under `adapters/generic/staging/`.
3. Write every runtime file to a temporary name under retained directory authority.
4. Flush each file, rename it to its generation-relative final name, and flush every affected directory.
5. Write the canonical generation manifest last, flush it, then validate the complete tree from retained handles.
6. Run the existing package/bootstrap smoke test against the staged generation.
7. Publish the complete staging directory under its unique generation ID without replacing any existing name. A publication failure leaves the active generation unchanged.

No final host-visible runtime filename exists while its bytes are incomplete.

### 5.2 Install the stable capsule

On first installation, build the entire capsule in a unique sibling staging directory, validate and flush it, then publish the whole directory to `voice-intent-normalizer` with a platform-supported no-replace directory operation. If the target name appears concurrently, fail closed and preserve it.

The capsule is a V1 protocol component, not an ordinary runtime generation. Clean-V1 runtime upgrades must not rewrite it. A future incompatible capsule protocol requires an explicit major-version migration design; it is not silently handled as a routine update.

### 5.3 Activate

After generation and capsule validation succeed, atomically write protected status containing:

- capsule digest and identity,
- active generation ID and manifest digest,
- previous generation ID when present,
- selected skill root,
- platform capability,
- transaction ID and phase while activation is incomplete.

Activation changes only protected state. The stable bootstrap observes either the old complete generation or the new complete generation. It must never observe staging.

## 6. Upgrade, repair, rollback, and recovery

- Reinstalling the current package performs no staging and returns `already-installed` when capsule, status, and active generation anchors agree.
- Upgrade builds and validates a new immutable generation before changing status.
- Status publication retains the previous valid generation for rollback.
- A smoke-test, validation, write, or activation failure leaves the previous generation active.
- `doctor()` reports `degraded` for missing, malformed, unanchored, aliased, or digest-mismatched capsule/status/generation state. It never re-anchors an untrusted manifest.
- Repair may rebuild missing protected status only when the stable capsule and exactly one complete generation prove the same installer-owned package identity. Ambiguous or conflicting state fails closed.
- Recovery is status-journal-driven and idempotent. It may finish publishing a complete generation or restore the previous active generation, but it never guesses ownership from a directory name alone.
- Old generations are retired only after the new generation is active and verified. Cleanup is bounded and may be deferred without affecting correctness.

## 7. Uninstall

Uninstall is logically ordered as follows:

1. Validate protected status, capsule manifest, active generation, and selected root.
2. Mark the adapter inactive in a status-anchored transaction so the bootstrap cannot launch a half-removed runtime.
3. Remove or quarantine the single immutable capsule directory as one installer-owned unit; do not remove its files one by one.
4. Retire/remove only generation directories whose IDs and manifest digests are anchored in protected status.
5. Remove adapter status and transaction records only after the discoverable capsule is gone.
6. Preserve shared lexicons and project data unless explicitly requested.

Platform-specific adapters should prefer an official host uninstall API. The generic adapter uses its managed capsule transaction. If any capsule or generation identity changes during uninstall, the operation returns a degraded/failed result, preserves the conflicting object, and reports manual action; it must not claim success or delete an unverified replacement.

## 8. Threat and trust boundary

The design protects against:

- crashes and process termination at every journaled phase,
- partial writes and partial directory population,
- symlink, junction, reparse-point, UNC, mapped-drive, and path-escape aliases already rejected by clean V1,
- stale or tampered manifests and status records,
- concurrent cooperative installer processes,
- a target name appearing before an exclusive publication,
- detected replacement of a retained capsule or generation identity.

No user-space installer can guarantee continued ownership against a malicious process running simultaneously as the same operating-system account and intentionally rewriting its private state between kernel operations. The implementation must not claim that guarantee. It minimizes that surface by making normal upgrades append-only in a private installer namespace and by never mutating a host-visible capsule during V1 upgrades. Any detected interference fails closed.

## 9. Result and diagnostics contract

- `changed_paths` reports every created, activated, retired, removed, or status path exactly once.
- A returned `installed`, `repaired`, `upgraded`, or `uninstalled` status is emitted only after `doctor()` can verify the corresponding final state.
- A deferred cleanup does not invalidate an active generation, but it is reported as a non-fatal diagnostic with its retained path.
- A partial staging directory is never reported as installed and is never selected by the bootstrap.
- JSON mode remains one valid UTF-8 JSON result frame with the existing stable error classification.

## 10. Migration scope

The repository has not published V1. Therefore the new layout is the only supported clean-V1 generic-adapter layout. On-disk transaction/status formats produced by unreleased Task 11 intermediate commits are intentionally unsupported and must fail closed rather than trigger legacy migration machinery.

This does not alter personal/project/hotword state formats, which remain governed by their existing clean-V1 contracts.

## 11. Test strategy

TDD must add tests that fail against the current file-by-file implementation before production changes:

- no staged runtime file is visible through the capsule before complete validation;
- an injected write/fsync failure leaves the old generation active and exposes no partial final runtime file;
- first install publishes a complete capsule or nothing;
- repeated install creates no new generation or staging residue;
- upgrade switches from one complete generation to another and preserves the previous version for rollback;
- interrupted activation recovers idempotently before `install`, `doctor`, or `uninstall` returns;
- rollback restores the previous generation without rewriting shared lexicons;
- normal uninstall removes the selected discoverable capsule and only anchored generations;
- identity or manifest replacement during uninstall fails without deleting the replacement;
- tampered status/manifest cannot select an ambient or external generation;
- the stable capsule bootstrap ignores `PYTHONPATH`, bytecode caches, native-module shadowing, symlinks, and junctions under the existing source-only rules;
- `changed_paths`, strict mode, Python 3.10 compatibility, wheel/sdist bundle contents, and fresh-venv installation retain their existing contracts;
- Codex, OpenClaw, WorkBuddy, and generic adapter tests continue to exercise the same public install/doctor/uninstall API.

Platform-specific branches must be exercised in Linux, macOS, and Windows CI where the local host cannot execute them.

## 12. Acceptance criteria

The scope revision is complete only when:

1. The two open Task 11 Important findings have independent regression tests and are closed by an independent reviewer.
2. No prior Task 11 Critical or Important finding regresses.
3. The full test suite, Ruff, build, package validation, fresh-wheel bootstrap, and diff checks pass.
4. The skill-creator validator accepts the root skill package.
5. The user-facing command, skill invocation, correction results, lexicon ownership, and learning behavior remain unchanged.
6. Task 11 receives a clean independent review before Task 12 begins.

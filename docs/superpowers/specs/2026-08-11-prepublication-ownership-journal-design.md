# Prepublication Ownership Journal Design

**Date:** 2026-08-11
**Status:** Approved
**Scope:** Close the remaining clean-V1 generic-adapter publication gap without changing the public CLI, skill invocation, correction, lexicon, or learning workflow.

## 1. Problem

The generic adapter publishes a complete immutable generation directory before its pending activation status is durable. The current implementation tries to delete that directory if the status callback fails. If deletion, directory removal, or parent-directory fsync also fails, the published generation has no durable ownership record. Later recovery and uninstall cannot safely adopt or remove it, because clean V1 never guesses ownership from a directory name.

The fix must establish durable installer ownership before the final generation name can exist. A failed operation remains inactive, preserves the old active version during upgrade, and is retried automatically by the next `install`, `doctor`, or `uninstall` call.

## 2. Approved architecture

Use `adapters/generic/transaction.json` as an independent prepublication ownership journal. Write and fsync the exact journal bytes after artifacts and the isolated smoke test succeed, but before publishing the generation directory. The public bootstrap continues to read only protected `status.json`; it never reads or trusts this journal.

The journal owns exactly one candidate generation under the existing global adapter transaction lock. It does not authorize arbitrary name discovery, prefix scanning, content-based adoption, or external paths.

## 3. Clean-V1 journal format

The repository has not published V1. Replace the unreleased status-digest-only marker format; do not migrate or accept it. Unknown, duplicate-key, oversized, non-canonical, future, or old journal formats fail closed.

The canonical UTF-8 JSON object has exactly these fields:

```json
{
  "baseline_status_digest": null,
  "candidate": {
    "generation_id": "g-<bounded-id>",
    "manifest_digest": "<sha256>",
    "package_hash": "<sha256>",
    "package_version": "0.1.0"
  },
  "capsule": {
    "manifest_digest": "<sha256>",
    "package_hash": "<sha256>",
    "protocol": 1
  },
  "format": 1,
  "operation": "first-install",
  "status_transition": {
    "after_digest": "<sha256>",
    "before_digest": null
  },
  "selected_skill_root": "<canonical-direct-local-root>",
  "transaction_id": "t-<32-lowercase-hex>"
}
```

`operation` is exactly `first-install` or `upgrade`. `baseline_status_digest` is `null` for first install and the digest of the exact pre-upgrade terminal status bytes for upgrade. `status_transition` is a write-ahead pair: `before_digest` binds the status that may exist before the next transition (`null` only when no first-install status exists), and `after_digest` binds the exact canonical status intended next. Before each later status transition, the journal is atomically replaced and fsynced with the exact current and next status digests. This lets recovery distinguish a transition that has not committed, one that committed despite reporting an error, and a conflict without guessing. `candidate` and `capsule` use the existing bounded clean-V1 reference grammars. `selected_skill_root` must pass the existing direct canonical local-root validation.

The final and staging paths are derived only from the trusted state root plus the validated candidate generation ID. They are never serialized as arbitrary paths.

## 4. Write and activation sequence

### First install

1. Acquire the canonical state-root lease and generic-adapter lock.
2. Recover or reject any existing journal before creating new artifacts.
3. Prepare, validate, fsync, and smoke the capsule and generation in the existing isolated temporary area. No candidate staging or final path may exist in the managed state root yet.
4. Build the exact intended `generation-published` status bytes.
5. Atomically write and fsync the prepublication journal with `before_digest=null` and `after_digest=<generation-published digest>`. If the write reports failure, proceed only when the exact canonical bytes are securely readable; otherwise publish nothing.
6. Create the journal-owned staging path. In that private directory, write and fsync the exact manifest first, then its declared files. This lets restart recovery validate an empty or partial stage against the journal-anchored manifest without scanning unrelated names. Validate the complete tree, then publish it no-replace.
7. Write the `generation-published` status. Before every later status write, durably replace the journal's `status_transition` with the exact current and next digests, then continue the existing capsule publication and terminal activation state machine.
8. After the exact terminal status is durable, remove the journal and fsync its parent. If removal or fsync reports failure, return the already-committed result only when the exact terminal status and installed artifacts validate; the next public operation removes the redundant journal.

Until terminal activation, first install remains unavailable to the public bootstrap.

### Upgrade

The same sequence applies, but both `baseline_status_digest` and the initial `before_digest` bind the exact pre-upgrade terminal status. The public status remains the old terminal status until the new generation has been published and the pending activation status commits. Therefore the bootstrap continues to run the old complete generation during preparation. No `generation-prepared` public status is introduced.

## 5. Recovery state machine

Every public `install`, `doctor`, and `uninstall` operation processes the journal under the existing lock before starting new work.

### Journal with the initial `before` status

- This branch applies only when `before_digest` equals `baseline_status_digest` (including both being `null` for first install) and the protected status is exactly that baseline.
- Validate the journal, selected skill root, baseline status digest, transition digest, capsule reference, candidate reference, and derived direct-local paths.
- If the exact candidate exists at the final name and its manifest matches the journal, retain its direct-directory identity and retire/remove it only while that identity remains unchanged.
- If the exact candidate exists only in staging, remove only that journal-derived direct directory. An empty stage is removable; a partial stage is removable only when its exact manifest matches the journal and every observed direct entry is declared by that manifest. Aliases or extra entries are conflicts.
- If both staging and final names exist, preserve both and report a conflict.
- If both names are absent, remove the journal.
- If any cleanup operation or parent-directory fsync fails, preserve the journal and return `failed` or `degraded`. The next public operation retries the same bounded recovery.
- Never activate the candidate in this branch.

### Journal with a later nonterminal `before` status

- This branch applies when a later write-ahead journal replacement committed but its intended status write did not.
- Require the current nonterminal status digest to equal `before_digest`, and cross-check its transaction ID, selected root, capsule, candidate, and phase against the journal.
- Continue the existing bounded status-driven recovery by safely committing the exact `after` status or its defined rollback path. Do not infer a status, candidate, or path from directory contents.
- Any mismatch is a conflict, not a recoverable transition.

### Journal with the exact transition `after` status

- Require the status transaction ID, selected root, capsule, candidate, and exact after-status digest to match the journal.
- If the after-status is nonterminal, continue the existing status-driven activation or rollback recovery. Before its next status write, replace and fsync the journal with that exact before/after digest pair.
- If the after-status is terminal, validate the complete installed capsule and generations, remove and fsync the redundant journal, and retain the committed result.
- A status write that committed despite reporting an I/O error is therefore recoverable without deleting an anchored generation.

### Conflicts and tampering

- A journal/status mismatch, identity replacement, manifest mismatch, unknown extra final name, unsafe root, or unrecognized format is preserved and reported as degraded/manual action.
- Recovery never deletes or re-anchors the conflicting object.
- Recovery remains bounded by the existing transition limit and never loops indefinitely inside one command.

## 6. Error and result contract

- A failed first install never becomes launchable.
- A failed upgrade leaves the old terminal generation launchable.
- Automatic recovery reports exact changed paths and a diagnostic; it requires no user confirmation when every identity and digest matches.
- Shared personal, project, preference, negative, and hotword data are never part of the journal and are never removed by adapter recovery.
- Normal user commands and JSON result shapes remain unchanged.

## 7. Validation strategy

TDD must prove RED before production changes for:

- first-install and upgrade status-callback failure followed by file unlink failure;
- directory removal failure and parent-directory fsync failure after publication;
- process restart with the durable journal and exact final candidate;
- repeated `install`, `doctor`, and `uninstall` recovery attempts;
- marker write failure before publication;
- exact committed marker despite a reported fsync error;
- each write-ahead `before`/`after` state, including a status committed despite a callback error and a terminal status with redundant journal metadata;
- journal/status digest, operation, root, capsule, candidate, manifest, and identity mismatches;
- no adoption of an unjournaled generation name;
- old active bootstrap availability throughout upgrade preparation;
- exact, unique `changed_paths` and bounded cleanup.

The full suite, Ruff, build, skill validation, fresh-wheel lifecycle, and diff checks must pass. Native publication and restitution tests must run on Windows, Linux, and macOS CI before Task 11 closes.

## 8. Cross-platform CI and repository publication

Create a private GitHub repository named `voice-intent-normalizer` and push the development branch for CI. Add GitHub Actions jobs that run the native publication/restitution contract on `windows-latest`, `ubuntu-latest`, and `macos-latest`; retain a Python 3.10 compatibility job and the current supported Python full-suite job. Do not claim native coverage from mocks or platform skips.

After Tasks 12–15, corpus metrics, documentation, privacy checks, release builds, and all CI jobs pass, make the repository public and publish tag `v0.1.0`.

## 9. Approved downstream defaults

- Codex uses implicit skill/context correction by default; the pre-submit strict hook is explicit opt-in and never bypasses hook trust.
- OpenClaw installs globally by default and retains an explicit workspace option.
- WorkBuddy receives a deterministic manual-import ZIP until a stable public automation API exists.
- Hotword updates are enabled by default, fail open, and never upload conversations, personal lexicons, or project content.
- Adapter uninstall preserves shared learning and preference data unless the user explicitly requests shared-data removal.
- Public documentation is Chinese-first and bilingual, keeps Apache-2.0, and states that the skill processes post-transcription text rather than editing live speech characters in host input fields.

## 10. Acceptance criteria

1. No complete generation can become unowned after its final name is published.
2. Cleanup failure preserves a durable, exact ownership record that a later process can retry.
3. Mismatch or tampering fails closed without deletion or activation.
4. First install remains inactive and failed upgrade preserves the old active generation.
5. All existing public workflow, shared-data ownership, and clean-V1 constraints remain unchanged.
6. An independent scoped review closes the residual whole-revision finding.
7. Native Windows, Linux, and macOS CI evidence is recorded before Task 12 begins.

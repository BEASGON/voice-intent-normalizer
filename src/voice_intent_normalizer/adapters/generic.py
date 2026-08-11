"""Safe versioned installation of the portable generic skill capsule."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..paths import StatePaths, StateRootLease, guard_state_root, validate_state_root
from ..updater import _retained_lease_update_lock
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions
from .generic_contract import (
    GenerationRef,
    StatusV5,
    canonical_json_bytes,
    status_skill_root,
)
from .generic_layout import (
    OwnershipJournal,
    VersionedArtifact,
    VersionedArtifacts,
    canonical_status_v5,
    generic_layout_paths,
    ownership_journal_bytes,
    ownership_journal_skill_root,
    ownership_journal_transition_bytes,
    prepare_versioned_artifacts,
    status_v5_payload,
    validate_anchored_manifest,
    validate_ownership_journal,
)

_NAME = "voice-intent-normalizer"
_GENERIC_RELATIVE = Path("adapters") / "generic"
_STATUS_RELATIVE = _GENERIC_RELATIVE / "status.json"
_TRANSACTION_RELATIVE = _GENERIC_RELATIVE / "transaction.json"
_GENERATIONS_RELATIVE = _GENERIC_RELATIVE / "generations"
_STAGING_RELATIVE = _GENERIC_RELATIVE / "staging"
_RETIRED_RELATIVE = _GENERIC_RELATIVE / "retired"
_MANIFEST_LIMIT = 8 * 1024 * 1024
_MANAGED_FILE_LIMIT = 64 * 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class _ValidatedTree:
    identity: tuple[int, int]
    manifest: Mapping[str, object]
    files: tuple[str, ...]
    directories: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _CleanupTree:
    directories: tuple[Path, ...]
    identities: Mapping[Path, tuple[int, int]]
    files: tuple[Path, ...]
    children: Mapping[Path, frozenset[str]]


@dataclass(frozen=True, slots=True)
class _JournalCleanupTree:
    identity: tuple[int, int]
    manifest_bytes: bytes | None
    directories: tuple[Path, ...]
    identities: Mapping[Path, tuple[int, int]]
    files: tuple[Path, ...]
    children: Mapping[Path, frozenset[str]]


class GenericAdapter:
    """Install one stable capsule selecting immutable private generations."""

    platform = "generic"

    def __init__(self, repository: str | Path, state_paths: StatePaths) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths
        self._configured_skill_root: Path | None = None
        self._active_state_lease: StateRootLease | None = None
        self._change_events: list[Path] = []
        self._recovery_changes: tuple[Path, ...] = ()
        self._committed_install_status_bytes: bytes | None = None
        self._committed_install_result: AdapterResult | None = None

    def detect(self) -> AdapterResult:
        return self.doctor()

    def install(self, options: InstallOptions) -> AdapterResult:
        self._reset_operation()
        if options.strict:
            return self._failed(
                "strict installation is unavailable for the generic adapter"
            )
        result: AdapterResult | None = None
        try:
            with self._state_operation(create=True):
                result = self._install_locked(options)
        except Exception:
            if (
                self._committed_install_result is not None
                and self._committed_install_status_bytes is not None
                and self._status_bytes_are_active(
                    self._committed_install_status_bytes
                )
            ):
                result = self._committed_install_result
            else:
                result = self._failed("generic installation was not completed")
        return self._with_recovery_changes(result)

    def _install_locked(self, options: InstallOptions) -> AdapterResult:
        if options.output_dir is None:
            return self._failed("a skill root is required")
        root = self._safe_skill_root(options.output_dir)
        self._configured_skill_root = root
        self._recover_ownership_journal(
            root, allow_capsule_publication=True
        )
        status = self._read_versioned_status(root)
        if status is None:
            if self._direct_entry_exists(root / _NAME):
                return self._failed(
                    "existing skill capsule is not anchored by adapter status"
                )
            return self._first_install(root, options)

        self._validate_complete_status(root, status, smoke=False)
        artifacts = self._prepare_versioned_artifacts(secrets.token_hex(16))
        self._require_capsule_protocol(status, artifacts.capsule)
        capability = self._capability(options)
        if (
            status.active.package_hash == artifacts.generation.package_hash
            and status.active.package_version == artifacts.generation.package_version
        ):
            if status.capability != capability.value:
                payload = status_v5_payload(
                    skill_root=status.selected_skill_root,
                    capability=capability.value,
                    capsule=status.capsule,
                    active=status.active,
                    previous=status.previous,
                )
                self._write_status_payload(payload)
            self._validate_complete_status(
                root, self._read_required_status(root), smoke=True
            )
            return AdapterResult(
                self.platform,
                "already-installed",
                capability,
                ("managed immutable generation is already active",),
            )
        return self._upgrade(root, status, artifacts, options)

    def _first_install(
        self, root: Path, options: InstallOptions
    ) -> AdapterResult:
        transaction_id = f"t-{secrets.token_hex(16)}"
        artifacts = self._prepare_versioned_artifacts(secrets.token_hex(16))
        self._smoke_generation(artifacts.capsule, artifacts.generation)
        baseline = self._read_status_bytes()
        if baseline is not None:
            raise ValueError("first-install baseline status appeared")
        payload = status_v5_payload(
            skill_root=root,
            capability=self._capability(options).value,
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
            transaction_id=transaction_id,
            transaction_phase="generation-published",
        )
        journal_bytes = ownership_journal_bytes(
            operation="first-install",
            transaction_id=transaction_id,
            skill_root=root,
            baseline_status_bytes=baseline,
            before_status_bytes=baseline,
            after_status_bytes=canonical_json_bytes(payload),
            capsule=artifacts.capsule,
            candidate=artifacts.generation,
        )
        self._write_ownership_journal(journal_bytes)
        journal = validate_ownership_journal(
            journal_bytes,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        self._stage_and_publish_generation(
            artifacts.generation,
            recovering=False,
        )
        journal = self._advance_transaction_status(
            root,
            journal=journal,
            before_payload=None,
            after_payload=payload,
        )
        self._ensure_capsule(
            root, artifacts.capsule, transaction_id, recovering=False
        )
        next_payload = self._replace_phase(payload, "capsule-published")
        journal = self._advance_transaction_status(
            root,
            journal=journal,
            before_payload=payload,
            after_payload=next_payload,
        )
        payload = next_payload
        next_payload = self._replace_phase(payload, "activation-pending")
        journal = self._advance_transaction_status(
            root,
            journal=journal,
            before_payload=payload,
            after_payload=next_payload,
        )
        payload = next_payload
        final = status_v5_payload(
            skill_root=root,
            capability=self._capability(options).value,
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
        )
        pending = self._read_required_status(root)
        self._validate_capsule_ref(root, pending)
        self._validate_generation_ref(pending.active)
        result = self._committed_result("installed", options)
        self._advance_transaction_status(
            root,
            journal=journal,
            before_payload=payload,
            after_payload=final,
            terminal=True,
        )
        self._record_install_commit(result, final)
        return result

    def _upgrade(
        self,
        root: Path,
        status: StatusV5,
        artifacts: VersionedArtifacts,
        options: InstallOptions,
    ) -> AdapterResult:
        self._smoke_generation(artifacts.capsule, artifacts.generation)
        transaction_id = f"t-{secrets.token_hex(16)}"
        baseline = self._read_status_bytes()
        if baseline is None:
            raise ValueError("upgrade baseline status is missing")
        baseline_payload = status_v5_payload(
            skill_root=status.selected_skill_root,
            capability=status.capability,
            capsule=status.capsule,
            active=status.active,
            previous=status.previous,
        )
        payload = status_v5_payload(
            skill_root=status.selected_skill_root,
            capability=self._capability(options).value,
            capsule=status.capsule,
            active=status.active,
            previous=artifacts.generation,
            transaction_id=transaction_id,
            transaction_phase="generation-published",
        )
        journal_bytes = ownership_journal_bytes(
            operation="upgrade",
            transaction_id=transaction_id,
            skill_root=root,
            baseline_status_bytes=baseline,
            before_status_bytes=baseline,
            after_status_bytes=canonical_json_bytes(payload),
            capsule=artifacts.capsule,
            candidate=artifacts.generation,
        )
        try:
            self._write_ownership_journal(journal_bytes)
            journal = validate_ownership_journal(
                journal_bytes,
                skill_root=root,
                generations_root=generic_layout_paths(self.state_paths).generations,
            )
            self._stage_and_publish_generation(
                artifacts.generation,
                recovering=False,
            )
            journal = self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=baseline_payload,
                after_payload=payload,
            )
            next_payload = self._replace_phase(payload, "activation-pending")
            journal = self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=payload,
                after_payload=next_payload,
            )
            payload = next_payload
            final = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=self._capability(options).value,
                capsule=status.capsule,
                active=artifacts.generation,
                previous=status.active,
            )
            pending = self._read_required_status(root)
            self._validate_capsule_ref(root, pending)
            self._validate_generation_ref(pending.active)
            assert pending.previous is not None
            self._validate_generation_ref(pending.previous)
            result = self._committed_result("upgraded", options)
            self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=payload,
                after_payload=final,
                terminal=True,
            )
            self._record_install_commit(result, final)
            return result
        except Exception:
            final_bytes = self._committed_install_status_bytes
            if final_bytes is not None and self._status_bytes_are_active(final_bytes):
                assert self._committed_install_result is not None
                return self._committed_install_result
            return self._failed("generic upgrade was not completed")

    def _prepare_versioned_artifacts(
        self, generation_nonce: str
    ) -> VersionedArtifacts:
        return prepare_versioned_artifacts(self.repository, generation_nonce)

    def _require_capsule_protocol(
        self, status: StatusV5, capsule: VersionedArtifact
    ) -> None:
        if (
            status.capsule.manifest_digest != capsule.manifest_digest
            or status.capsule.package_hash != capsule.package_hash
        ):
            raise ValueError("installed capsule protocol differs from this build")

    def _read_versioned_status(self, root: Path) -> StatusV5 | None:
        raw = self._read_status_bytes()
        if raw is None:
            return None
        with guard_state_root(root) as skill_root_lease:
            if not skill_root_lease.root_exists:
                raise ValueError("selected skill root is unavailable")
            status, _ = canonical_status_v5(
                raw,
                skill_root=skill_root_lease.root,
                generations_root=generic_layout_paths(self.state_paths).generations,
            )
        return status

    def _read_status_bytes(self) -> bytes | None:
        lease = self._state_lease()
        if (
            not lease.root_exists
            or not lease.available(_GENERIC_RELATIVE)
            or not lease.exists(_STATUS_RELATIVE)
        ):
            return None
        return lease.read_bytes(
            _STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status"
        )

    def _protected_skill_root(self) -> Path | None:
        raw = self._read_status_bytes()
        if raw is None:
            return None
        return self._safe_skill_root(status_skill_root(raw))

    def _read_required_status(self, root: Path) -> StatusV5:
        status = self._read_versioned_status(root)
        if status is None:
            raise ValueError("adapter status is missing")
        return status

    @staticmethod
    def _replace_phase(
        payload: dict[str, object], phase: str
    ) -> dict[str, object]:
        transaction = payload.get("transaction")
        if not isinstance(transaction, dict) or not isinstance(
            transaction.get("id"), str
        ):
            raise ValueError("adapter transaction is missing")
        return {
            **payload,
            "transaction": {"id": transaction["id"], "phase": phase},
        }

    @staticmethod
    def _status_payload(status: StatusV5) -> dict[str, object]:
        return status_v5_payload(
            skill_root=status.selected_skill_root,
            capability=status.capability,
            capsule=status.capsule,
            active=status.active,
            previous=status.previous,
            transaction_id=status.transaction_id,
            transaction_phase=status.transaction_phase,
        )

    def _write_ownership_journal(self, payload: bytes) -> None:
        """Commit exact journal bytes or prove that the same bytes committed."""
        lease = self._state_lease()
        if lease.exists(_TRANSACTION_RELATIVE):
            current = lease.read_bytes(
                _TRANSACTION_RELATIVE,
                _MANIFEST_LIMIT,
                "adapter ownership journal",
            )
            if current != payload:
                raise ValueError("adapter ownership journal conflicts with intent")
            lease.fsync_directory(_GENERIC_RELATIVE)
            self._record_changes(
                (generic_layout_paths(self.state_paths).transaction,)
            )
            return
        try:
            lease.write_bytes_atomic(_TRANSACTION_RELATIVE, payload)
            lease.fsync_directory(_GENERIC_RELATIVE)
        except OSError:
            committed = False
            try:
                committed = (
                    lease.read_bytes(
                        _TRANSACTION_RELATIVE,
                        _MANIFEST_LIMIT,
                        "adapter ownership journal",
                    )
                    == payload
                )
            except (OSError, ValueError):
                pass
            if not committed:
                raise
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _replace_ownership_journal(
        self,
        root: Path,
        *,
        current_journal: OwnershipJournal,
        payload: bytes,
    ) -> None:
        lease = self._state_lease()
        if not lease.exists(_TRANSACTION_RELATIVE):
            raise FileNotFoundError("adapter ownership journal is missing")
        current_bytes = lease.read_bytes(
            _TRANSACTION_RELATIVE,
            _MANIFEST_LIMIT,
            "adapter ownership journal",
        )
        observed = validate_ownership_journal(
            current_bytes,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        if observed != current_journal:
            raise ValueError("adapter ownership journal changed before transition")
        if current_bytes == payload:
            return
        try:
            lease.write_bytes_atomic(_TRANSACTION_RELATIVE, payload)
            lease.fsync_directory(_GENERIC_RELATIVE)
        except OSError:
            committed = lease.read_bytes(
                _TRANSACTION_RELATIVE,
                _MANIFEST_LIMIT,
                "adapter ownership journal",
            )
            if committed != payload:
                raise
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _advance_transaction_status(
        self,
        root: Path,
        *,
        journal: OwnershipJournal,
        before_payload: dict[str, object] | None,
        after_payload: dict[str, object],
        terminal: bool = False,
    ) -> OwnershipJournal:
        """Journal one exact status transition, commit status, then retire metadata."""
        before_bytes = (
            None if before_payload is None else canonical_json_bytes(before_payload)
        )
        if before_bytes is not None:
            canonical_status_v5(
                before_bytes,
                skill_root=root,
                generations_root=generic_layout_paths(self.state_paths).generations,
            )
        after_bytes = canonical_json_bytes(after_payload)
        after_status, _ = canonical_status_v5(
            after_bytes,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        if after_status.transaction_id not in {journal.transaction_id, None}:
            raise ValueError("status transaction does not match ownership journal")
        if terminal != (after_status.transaction_id is None):
            raise ValueError("terminal journal transition has an invalid status")
        current = self._read_status_bytes()
        if current != before_bytes:
            raise ValueError("protected status changed before journal transition")
        if terminal:
            self._validate_terminal_transition(
                root, journal, after_status, require_committed=False
            )
        journal_bytes = ownership_journal_transition_bytes(
            journal,
            before_status_bytes=before_bytes,
            after_status_bytes=after_bytes,
        )
        self._replace_ownership_journal(
            root,
            current_journal=journal,
            payload=journal_bytes,
        )
        advanced = validate_ownership_journal(
            journal_bytes,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        self._write_status_payload(after_payload)
        if terminal:
            self._require_exact_terminal_status(after_status)
            try:
                self._remove_ownership_journal(missing_ok=False)
            except OSError:
                self._validate_terminal_transition(
                    root, advanced, after_status, require_committed=True
                )
        return advanced

    def _validate_terminal_transition(
        self,
        root: Path,
        journal: OwnershipJournal,
        status: StatusV5,
        *,
        require_committed: bool,
    ) -> None:
        expected = canonical_json_bytes(
            status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
            )
        )
        current = self._read_status_bytes()
        if require_committed and current != expected:
            raise ValueError("terminal status is not exact")
        self._validate_capsule_ref(root, status)
        self._validate_generation_ref(status.active)
        if status.previous is not None:
            self._validate_generation_ref(status.previous)
        if journal.capsule != status.capsule:
            raise ValueError("terminal status capsule differs from ownership journal")
        candidates = (status.active, status.previous)
        if journal.candidate not in candidates:
            raise ValueError("terminal status does not retain the journal candidate")

    def _require_exact_terminal_status(self, status: StatusV5) -> None:
        expected = canonical_json_bytes(
            status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
            )
        )
        if self._read_status_bytes() != expected:
            raise ValueError("terminal status is not exact")

    def _write_status_payload(self, payload: dict[str, object]) -> None:
        data = canonical_json_bytes(payload)
        lease = self._state_lease()
        try:
            lease.write_bytes_atomic(_STATUS_RELATIVE, data)
            lease.fsync_directory(_GENERIC_RELATIVE)
        except OSError:
            if not self._status_bytes_are_active(data):
                raise
        self._record_changes((generic_layout_paths(self.state_paths).status,))

    def _record_install_commit(
        self, result: AdapterResult, payload: dict[str, object]
    ) -> None:
        self._committed_install_status_bytes = canonical_json_bytes(payload)
        self._committed_install_result = result

    def _status_bytes_are_active(self, expected: bytes) -> bool:
        try:
            if self._active_state_lease is not None:
                return (
                    self._active_state_lease.read_bytes(
                        _STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status"
                    )
                    == expected
                )
            with guard_state_root(
                self.state_paths.root, retained_dirs=(_GENERIC_RELATIVE,)
            ) as lease:
                return (
                    lease.root_exists
                    and lease.available(_GENERIC_RELATIVE)
                    and lease.read_bytes(
                        _STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status"
                    )
                    == expected
                )
        except (OSError, ValueError):
            return False

    def _journal_exists(self) -> bool:
        lease = self._state_lease()
        return (
            lease.root_exists
            and lease.available(_GENERIC_RELATIVE)
            and lease.exists(_TRANSACTION_RELATIVE)
        )

    def _read_ownership_journal(self, root: Path) -> OwnershipJournal:
        lease = self._state_lease()
        payload = lease.read_bytes(
            _TRANSACTION_RELATIVE,
            _MANIFEST_LIMIT,
            "adapter ownership journal",
        )
        return validate_ownership_journal(
            payload,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )

    def _retire_redundant_terminal_journal(
        self, root: Path, status: StatusV5
    ) -> None:
        journal = self._read_ownership_journal(root)
        status_bytes = self._read_status_bytes()
        if (
            status_bytes is None
            or journal.transition.after_digest
            != hashlib.sha256(status_bytes).hexdigest()
        ):
            raise ValueError("ownership journal does not match terminal status")
        self._validate_terminal_transition(
            root, journal, status, require_committed=True
        )
        self._remove_ownership_journal(missing_ok=False)

    def _remove_ownership_journal(self, *, missing_ok: bool) -> None:
        lease = self._state_lease()
        if not lease.exists(_TRANSACTION_RELATIVE):
            if missing_ok:
                return
            raise FileNotFoundError("adapter ownership journal is missing")
        payload = lease.read_bytes(
            _TRANSACTION_RELATIVE,
            _MANIFEST_LIMIT,
            "adapter ownership journal",
        )
        try:
            lease.unlink(_TRANSACTION_RELATIVE)
            lease.fsync_directory(_GENERIC_RELATIVE)
        except OSError:
            if not lease.exists(_TRANSACTION_RELATIVE):
                lease.write_bytes_exclusive(_TRANSACTION_RELATIVE, payload)
                try:
                    lease.fsync_directory(_GENERIC_RELATIVE)
                except OSError:
                    pass
                self._record_changes(
                    (generic_layout_paths(self.state_paths).transaction,)
                )
            raise
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _recover_ownership_journal(
        self,
        root: Path,
        *,
        allow_capsule_publication: bool,
    ) -> None:
        """Resolve one exact journal under the global lock within eight transitions."""
        generations_root = generic_layout_paths(self.state_paths).generations
        for _ in range(8):
            status_bytes = self._read_status_bytes()
            if not self._journal_exists():
                if status_bytes is None:
                    return
                status, _ = canonical_status_v5(
                    status_bytes,
                    skill_root=root,
                    generations_root=generations_root,
                )
                phase = status.transaction_phase
                if phase is None:
                    return
                if phase not in {
                    "deactivation-pending",
                    "capsule-retired",
                    "cleanup-pending",
                }:
                    raise ValueError(
                        "activation status is missing its ownership journal"
                    )
                self._recover_uninstall(root, status)
                continue

            journal_bytes = self._state_lease().read_bytes(
                _TRANSACTION_RELATIVE,
                _MANIFEST_LIMIT,
                "adapter ownership journal",
            )
            journal = validate_ownership_journal(
                journal_bytes,
                skill_root=root,
                generations_root=generations_root,
            )
            status_digest = (
                None
                if status_bytes is None
                else hashlib.sha256(status_bytes).hexdigest()
            )
            if (
                journal.transition.before_digest
                == journal.baseline_status_digest
                == status_digest
            ):
                self._recover_initial_journal(
                    root, journal, journal_bytes, status_bytes
                )
                continue
            if status_digest == journal.transition.before_digest:
                if status_bytes is None:
                    raise ValueError("journal transition status is missing")
                status, _ = canonical_status_v5(
                    status_bytes,
                    skill_root=root,
                    generations_root=generations_root,
                )
                self._validate_journal_status(journal, status)
                expected = self._expected_activation_after(status, journal)
                if (
                    hashlib.sha256(canonical_json_bytes(expected)).hexdigest()
                    != journal.transition.after_digest
                ):
                    raise ValueError(
                        "ownership journal does not bind the next status"
                    )
                self._recover_activation(
                    root,
                    status,
                    journal,
                    allow_capsule_publication=allow_capsule_publication,
                )
                continue
            if status_digest == journal.transition.after_digest:
                if status_bytes is None:
                    raise ValueError("journal after-status is missing")
                status, _ = canonical_status_v5(
                    status_bytes,
                    skill_root=root,
                    generations_root=generations_root,
                )
                self._validate_journal_status(journal, status)
                if status.transaction_phase is None:
                    self._retire_redundant_terminal_journal(root, status)
                else:
                    self._recover_activation(
                        root,
                        status,
                        journal,
                        allow_capsule_publication=allow_capsule_publication,
                    )
                continue
            raise ValueError("ownership journal conflicts with protected status")
        raise OSError("adapter recovery exceeded its transition bound")

    def _recover_initial_journal(
        self,
        root: Path,
        journal: OwnershipJournal,
        journal_bytes: bytes,
        status_bytes: bytes | None,
    ) -> None:
        if status_bytes is None:
            if journal.operation != "first-install":
                raise ValueError("upgrade journal baseline status is missing")
            if self._direct_entry_exists(root / _NAME):
                raise ValueError("first-install journal conflicts with a capsule")
        else:
            if journal.operation != "upgrade":
                raise ValueError("first-install journal has a baseline status")
            status, _ = canonical_status_v5(
                status_bytes,
                skill_root=root,
                generations_root=generic_layout_paths(
                    self.state_paths
                ).generations,
            )
            if status.transaction_phase is not None:
                raise ValueError("journal baseline status is not terminal")
            if status.capsule != journal.capsule:
                raise ValueError("journal capsule differs from its baseline")
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            if status.previous is not None:
                self._validate_generation_ref(status.previous)

        stage_relative = _STAGING_RELATIVE / journal.candidate.generation_id
        final_relative = _GENERATIONS_RELATIVE / journal.candidate.generation_id
        lease = self._state_lease()
        stage_exists = lease.exists(stage_relative)
        final_exists = lease.exists(final_relative)
        if stage_exists and final_exists:
            raise ValueError(
                "journal candidate exists at both staging and final names"
            )
        selected = stage_relative if stage_exists else final_relative
        if stage_exists or final_exists:
            tree = self._preflight_journal_cleanup(selected, journal)
            self._remove_journal_candidate(selected, tree)

        # Conservatively make a prior successful rmdir durable before the
        # journal itself is retired, even when this process observed no name.
        lease.fsync_directory(_STAGING_RELATIVE)
        lease.fsync_directory(_GENERATIONS_RELATIVE)
        current = lease.read_bytes(
            _TRANSACTION_RELATIVE,
            _MANIFEST_LIMIT,
            "adapter ownership journal",
        )
        if current != journal_bytes:
            raise ValueError("adapter ownership journal changed during cleanup")
        self._remove_ownership_journal(missing_ok=False)

    def _validate_journal_status(
        self, journal: OwnershipJournal, status: StatusV5
    ) -> None:
        if (
            not self._same_selected_root(
                status.selected_skill_root, journal.selected_skill_root
            )
            or status.capsule != journal.capsule
        ):
            raise ValueError("ownership journal status references differ")
        if status.transaction_phase is None:
            if journal.operation == "first-install":
                if status.active != journal.candidate or status.previous is not None:
                    raise ValueError("terminal status has the wrong journal operation")
            elif (
                status.active != journal.candidate
                or status.previous is None
                or status.previous == journal.candidate
            ):
                raise ValueError("terminal upgrade status differs from its journal")
            return
        if status.transaction_id != journal.transaction_id:
            raise ValueError("status transaction does not match ownership journal")
        if status.transaction_phase not in {
            "generation-published",
            "capsule-published",
            "activation-pending",
            "rollback-pending",
        }:
            raise ValueError("journal has an invalid activation phase")
        if journal.operation == "first-install":
            if status.active != journal.candidate or status.previous is not None:
                raise ValueError("first-install journal candidate differs")
        elif (
            status.previous != journal.candidate
            or status.active == journal.candidate
        ):
            raise ValueError("upgrade journal candidate differs")

    def _expected_activation_after(
        self, status: StatusV5, journal: OwnershipJournal
    ) -> dict[str, object]:
        phase = status.transaction_phase
        if phase == "generation-published":
            return self._replace_phase(
                self._status_payload(status),
                "capsule-published"
                if journal.operation == "first-install"
                else "activation-pending",
            )
        if phase == "capsule-published":
            if journal.operation != "first-install":
                raise ValueError("upgrade journal has a capsule publication phase")
            return self._replace_phase(
                self._status_payload(status), "activation-pending"
            )
        if phase == "activation-pending":
            active = (
                status.active if status.previous is None else status.previous
            )
            previous = None if status.previous is None else status.active
            return status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=active,
                previous=previous,
            )
        if phase == "rollback-pending":
            return status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=None,
            )
        raise ValueError("invalid activation recovery phase")

    def _recover_pending(
        self, root: Path, *, allow_capsule_publication: bool
    ) -> None:
        self._recover_ownership_journal(
            root, allow_capsule_publication=allow_capsule_publication
        )

    def _recover_activation(
        self,
        root: Path,
        status: StatusV5,
        journal: OwnershipJournal,
        *,
        allow_capsule_publication: bool,
    ) -> None:
        assert status.transaction_id is not None
        phase = status.transaction_phase
        if phase == "generation-published":
            self._validate_generation_ref(status.active)
            if status.previous is None:
                if not self._direct_entry_exists(root / _NAME):
                    if allow_capsule_publication:
                        artifacts = self._prepare_versioned_artifacts(
                            status.active.generation_id[-32:]
                        )
                        if (
                            artifacts.generation.identifier
                            != status.active.generation_id
                            or artifacts.generation.manifest_digest
                            != status.active.manifest_digest
                            or artifacts.capsule.manifest_digest
                            != status.capsule.manifest_digest
                            or artifacts.capsule.package_hash
                            != status.capsule.package_hash
                        ):
                            self._rollback_first_install(root, status)
                            return
                        self._ensure_capsule(
                            root,
                            artifacts.capsule,
                            status.transaction_id,
                            recovering=False,
                        )
                    else:
                        self._rollback_first_install(root, status)
                        return
                self._validate_capsule_ref(root, status)
                payload = status_v5_payload(
                    skill_root=status.selected_skill_root,
                    capability=status.capability,
                    capsule=status.capsule,
                    active=status.active,
                    previous=None,
                    transaction_id=status.transaction_id,
                    transaction_phase="capsule-published",
                )
                self._advance_transaction_status(
                    root,
                    journal=journal,
                    before_payload=self._status_payload(status),
                    after_payload=payload,
                )
                return
            self._validate_capsule_ref(root, status)
            try:
                self._validate_generation_ref(status.previous)
            except Exception:
                payload = status_v5_payload(
                    skill_root=status.selected_skill_root,
                    capability=status.capability,
                    capsule=status.capsule,
                    active=status.active,
                    previous=status.previous,
                    transaction_id=status.transaction_id,
                    transaction_phase="rollback-pending",
                )
                self._advance_transaction_status(
                    root,
                    journal=journal,
                    before_payload=self._status_payload(status),
                    after_payload=payload,
                )
                return
            payload = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="activation-pending",
            )
            self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=self._status_payload(status),
                after_payload=payload,
            )
            return

        if phase == "capsule-published":
            if status.previous is not None:
                raise ValueError("capsule publication phase has an invalid candidate")
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            payload = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=None,
                transaction_id=status.transaction_id,
                transaction_phase="activation-pending",
            )
            self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=self._status_payload(status),
                after_payload=payload,
            )
            return

        if phase == "activation-pending":
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            if status.previous is None:
                active = status.active
                previous = None
            else:
                try:
                    self._validate_generation_ref(status.previous)
                except Exception:
                    payload = status_v5_payload(
                        skill_root=status.selected_skill_root,
                        capability=status.capability,
                        capsule=status.capsule,
                        active=status.active,
                        previous=status.previous,
                        transaction_id=status.transaction_id,
                        transaction_phase="rollback-pending",
                    )
                    self._advance_transaction_status(
                        root,
                        journal=journal,
                        before_payload=self._status_payload(status),
                        after_payload=payload,
                    )
                    return
                active = status.previous
                previous = status.active
            final = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=active,
                previous=previous,
            )
            self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=self._status_payload(status),
                after_payload=final,
                terminal=True,
            )
            return

        if phase == "rollback-pending":
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            final = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=None,
            )
            self._advance_transaction_status(
                root,
                journal=journal,
                before_payload=self._status_payload(status),
                after_payload=final,
                terminal=True,
            )
            return
        raise ValueError("invalid activation recovery phase")

    def _rollback_first_install(self, root: Path, status: StatusV5) -> None:
        assert status.transaction_id is not None
        self._retire_generation(status.active, status.transaction_id)
        self._remove_ownership_journal(missing_ok=True)
        self._remove_status()

    def doctor(self) -> AdapterResult:
        self._reset_operation()
        try:
            with self._state_operation(create=False, lock_missing=False):
                result = self._doctor_locked()
        except Exception:
            result = AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                ("adapter status or an anchored artifact is invalid",),
            )
        return self._with_recovery_changes(result)

    def _doctor_locked(self) -> AdapterResult:
        root = self._configured_skill_root
        if root is None:
            if not self._status_exists() and not self._journal_exists():
                return AdapterResult(
                    self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
                )
            root = self._protected_skill_root()
            if root is None:
                payload = self._state_lease().read_bytes(
                    _TRANSACTION_RELATIVE,
                    _MANIFEST_LIMIT,
                    "adapter ownership journal",
                )
                root = ownership_journal_skill_root(payload)
        root = self._safe_skill_root(root)
        self._recover_ownership_journal(
            root, allow_capsule_publication=False
        )
        status = self._read_versioned_status(root)
        if status is None:
            if self._direct_entry_exists(root / _NAME):
                return AdapterResult(
                    self.platform,
                    "degraded",
                    CapabilityLevel.UNAVAILABLE,
                    ("installed objects are not anchored by adapter status",),
                )
            return AdapterResult(
                self.platform,
                "not-installed",
                CapabilityLevel.UNAVAILABLE,
                (
                    "installed files: absent",
                    "shared personal and project data were preserved",
                ),
            )
        try:
            self._validate_complete_status(root, status, smoke=True)
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.MANUAL,
                (
                    "managed package: hash mismatch",
                    "required file SKILL.md: invalid",
                    "manual action required after detected interference",
                ),
            )
        capability = CapabilityLevel(status.capability)
        return AdapterResult(
            self.platform,
            "installed",
            capability,
            (
                "capsule and active generation: valid",
                "previous generation: valid"
                if status.previous is not None
                else "previous generation: absent",
                "shared state: available",
                "automatic trigger: unavailable",
                self._manual_message_for(capability),
            ),
        )

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed(
                "shared-data removal is intentionally separate and was not performed"
            )
        self._reset_operation()
        try:
            with self._state_operation(create=True):
                result = self._uninstall_locked(options)
        except ValueError:
            result = self._failed(
                "managed ownership cannot be verified; manual action is required"
            )
        except Exception:
            result = AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                ("generic uninstall was not completed; manual action is required",),
            )
        return self._with_recovery_changes(result)

    def _uninstall_locked(self, options: UninstallOptions) -> AdapterResult:
        root_value = options.output_dir or self._configured_skill_root
        if root_value is None:
            return self._failed("skill root is required for safe uninstall")
        root = self._safe_skill_root(root_value)
        self._configured_skill_root = root
        self._recover_ownership_journal(
            root, allow_capsule_publication=False
        )
        status = self._read_versioned_status(root)
        if status is None:
            if self._direct_entry_exists(root / _NAME):
                return self._failed("managed ownership cannot be verified")
            return AdapterResult(
                self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
            )
        self._validate_complete_status(root, status, smoke=False)
        transaction_id = f"t-{secrets.token_hex(16)}"
        payload = status_v5_payload(
            skill_root=status.selected_skill_root,
            capability=status.capability,
            capsule=status.capsule,
            active=status.active,
            previous=status.previous,
            transaction_id=transaction_id,
            transaction_phase="deactivation-pending",
        )
        self._write_status_payload(payload)
        self._recover_pending(root, allow_capsule_publication=False)
        if self._read_versioned_status(root) is not None:
            raise OSError("uninstall status removal did not complete")
        if self._direct_entry_exists(root / _NAME) or self._journal_exists():
            raise OSError("uninstall left a discoverable capsule or journal")
        return AdapterResult(
            self.platform,
            "uninstalled",
            CapabilityLevel.MANUAL,
            ("shared personal and project data were preserved",),
        )

    def _recover_uninstall(self, root: Path, status: StatusV5) -> None:
        assert status.transaction_id is not None
        phase = status.transaction_phase
        if phase == "deactivation-pending":
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            if status.previous is not None:
                self._validate_generation_ref(status.previous)
            self._retire_capsule(root, status)
            payload = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="capsule-retired",
            )
            self._write_status_payload(payload)
            return
        if phase == "capsule-retired":
            if self._direct_entry_exists(root / _NAME):
                raise ValueError("capsule name was replaced after retirement")
            self._remove_retired_capsule(root, status)
            payload = status_v5_payload(
                skill_root=status.selected_skill_root,
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="cleanup-pending",
            )
            self._write_status_payload(payload)
            return
        if phase == "cleanup-pending":
            if self._direct_entry_exists(root / _NAME):
                raise ValueError("capsule name was replaced during cleanup")
            self._remove_retired_capsule(root, status)
            self._retire_generation(status.active, status.transaction_id)
            if status.previous is not None:
                self._retire_generation(status.previous, status.transaction_id)
            self._remove_status()
            return
        raise ValueError("invalid uninstall recovery phase")

    def _capsule_tombstone(self, root: Path, transaction_id: str) -> Path:
        return root / f".{_NAME}.retired-{transaction_id[2:]}"

    def _retire_capsule(self, root: Path, status: StatusV5) -> None:
        assert status.transaction_id is not None
        source = Path(_NAME)
        tombstone = self._capsule_tombstone(root, status.transaction_id)
        tombstone_relative = Path(tombstone.name)
        source_exists = self._direct_entry_exists(root / source)
        tombstone_exists = self._direct_entry_exists(tombstone)
        if source_exists and tombstone_exists:
            raise ValueError("capsule retirement destination already exists")
        if not source_exists:
            if not tombstone_exists:
                raise ValueError("anchored capsule is missing")
            self._validate_capsule_ref(root, status, relative=tombstone_relative)
            return
        tree = self._validate_capsule_ref(root, status)
        with guard_state_root(root) as lease:
            lease.publish_directory_no_replace(
                source,
                tombstone_relative,
                tree.identity,
                on_committed=lambda: self._record_changes(
                    (root / source, tombstone)
                ),
            )
            lease.fsync_directory(Path("."))
        retired = self._validate_capsule_ref(
            root, status, relative=tombstone_relative
        )
        if retired.identity != tree.identity:
            raise OSError("retired capsule identity changed")

    def _remove_retired_capsule(self, root: Path, status: StatusV5) -> None:
        assert status.transaction_id is not None
        tombstone = self._capsule_tombstone(root, status.transaction_id)
        if not self._direct_entry_exists(tombstone):
            return
        relative = Path(tombstone.name)
        tree = self._validate_capsule_ref(root, status, relative=relative)
        self._remove_validated_tree(root, relative, tree)

    def _retire_generation(
        self, reference: GenerationRef, transaction_id: str
    ) -> None:
        source = _GENERATIONS_RELATIVE / reference.generation_id
        tombstone = _RETIRED_RELATIVE / (
            f"{reference.generation_id}.{transaction_id}"
        )
        lease = self._state_lease()
        source_exists = lease.exists(source)
        tombstone_exists = lease.exists(tombstone)
        if source_exists and tombstone_exists:
            raise ValueError("generation retirement destination already exists")
        if source_exists:
            tree = self._validate_generation_ref(reference)
            with guard_state_root(
                self.state_paths.root,
                retained_dirs=(_GENERATIONS_RELATIVE, _RETIRED_RELATIVE),
            ) as publication:
                publication.publish_directory_no_replace(
                    source,
                    tombstone,
                    tree.identity,
                    on_committed=lambda: self._record_changes(
                        (
                            self.state_paths.root / source,
                            self.state_paths.root / tombstone,
                        )
                    ),
                )
                publication.fsync_directory(_GENERATIONS_RELATIVE)
                publication.fsync_directory(_RETIRED_RELATIVE)
        elif not tombstone_exists:
            return
        retired = self._validate_generation_ref(reference, relative=tombstone)
        self._remove_validated_tree(self.state_paths.root, tombstone, retired)

    def _remove_status(self) -> None:
        lease = self._state_lease()
        if not lease.exists(_STATUS_RELATIVE):
            raise FileNotFoundError("adapter status is missing")
        lease.unlink(_STATUS_RELATIVE)
        lease.fsync_directory(_GENERIC_RELATIVE)
        self._record_changes((generic_layout_paths(self.state_paths).status,))

    def _status_exists(self) -> bool:
        lease = self._state_lease()
        return (
            lease.root_exists
            and lease.available(_GENERIC_RELATIVE)
            and lease.exists(_STATUS_RELATIVE)
        )

    def _validate_complete_status(
        self, root: Path, status: StatusV5, *, smoke: bool
    ) -> None:
        if status.transaction_phase is not None:
            raise ValueError("adapter transaction is incomplete")
        if self._journal_exists():
            raise ValueError("ownership journal is not status anchored")
        self._validate_capsule_ref(root, status)
        self._validate_generation_ref(status.active)
        if status.previous is not None:
            self._validate_generation_ref(status.previous)
        if smoke:
            self._smoke_installed(root)

    def _validate_capsule_ref(
        self,
        root: Path,
        status: StatusV5,
        *,
        relative: Path = Path(_NAME),
    ) -> _ValidatedTree:
        return self._validate_anchored_tree(
            root,
            relative,
            kind="capsule",
            identifier=_NAME,
            manifest_name="capsule.json",
            manifest_digest=status.capsule.manifest_digest,
            package_hash=status.capsule.package_hash,
            package_version=None,
        )

    def _validate_generation_ref(
        self,
        reference: GenerationRef,
        *,
        relative: Path | None = None,
    ) -> _ValidatedTree:
        selected = (
            _GENERATIONS_RELATIVE / reference.generation_id
            if relative is None
            else relative
        )
        return self._validate_anchored_tree(
            self.state_paths.root,
            selected,
            kind="generation",
            identifier=reference.generation_id,
            manifest_name="generation.json",
            manifest_digest=reference.manifest_digest,
            package_hash=reference.package_hash,
            package_version=reference.package_version,
        )

    def _validate_anchored_tree(
        self,
        root: Path,
        relative: Path,
        *,
        kind: str,
        identifier: str,
        manifest_name: str,
        manifest_digest: str,
        package_hash: str,
        package_version: str | None,
    ) -> _ValidatedTree:
        with guard_state_root(root, retained_dirs=(relative,)) as lease:
            info = lease.stat(relative)
            self._require_direct_directory(info)
            identity = self._directory_identity(info)
            manifest_bytes = lease.read_bytes(
                relative / manifest_name,
                _MANIFEST_LIMIT,
                f"{kind} manifest",
            )
        manifest = validate_anchored_manifest(
            manifest_bytes,
            kind=kind,
            identifier=identifier,
            expected_manifest_digest=manifest_digest,
            expected_package_hash=package_hash,
            expected_package_version=package_version,
        )
        files = tuple(str(value) for value in manifest["files"])
        directories = self._manifest_directories(relative, files)
        children: dict[Path, set[str]] = {
            directory: set() for directory in directories
        }
        for directory in directories:
            if directory != relative:
                children[directory.parent].add(directory.name)
        children[relative].add(manifest_name)
        for name in files:
            path = relative / Path(name)
            children[path.parent].add(path.name)
        with guard_state_root(root, retained_dirs=directories) as lease:
            current = lease.stat(relative)
            self._require_direct_directory(current)
            if self._directory_identity(current) != identity:
                raise ValueError(f"{kind} directory identity changed")
            for directory in directories:
                if set(lease.listdir(directory)) != children[directory]:
                    raise ValueError(f"{kind} tree contains unknown or missing entries")
            hashes = manifest["file_hashes"]
            assert isinstance(hashes, Mapping)
            total = 0
            for name in files:
                path = relative / Path(name)
                info = lease.stat(path)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & _WINDOWS_REPARSE_POINT
                ):
                    raise ValueError(f"{kind} tree contains an unsafe file")
                data = lease.read_bytes(
                    path, _MANAGED_FILE_LIMIT, f"{kind} file"
                )
                total += len(data)
                if total > _MANAGED_FILE_LIMIT:
                    raise ValueError(f"{kind} tree exceeds size limit")
                if hashlib.sha256(data).hexdigest() != hashes[name]:
                    raise ValueError(f"{kind} file hash changed")
            if (
                lease.read_bytes(
                    relative / manifest_name,
                    _MANIFEST_LIMIT,
                    f"{kind} manifest",
                )
                != manifest_bytes
            ):
                raise ValueError(f"{kind} manifest identity changed")
        return _ValidatedTree(identity, manifest, files, directories)

    @staticmethod
    def _manifest_directories(
        root: Path, files: Sequence[str]
    ) -> tuple[Path, ...]:
        directories = {root}
        for name in files:
            parent = root / Path(name).parent
            while parent != root:
                directories.add(parent)
                parent = parent.parent
        return tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        )

    def _remove_validated_tree(
        self, root: Path, relative: Path, tree: _ValidatedTree
    ) -> None:
        manifest_name = (
            "capsule.json"
            if tree.manifest["kind"] == "capsule"
            else "generation.json"
        )
        changed = [root / relative / name for name in tree.files]
        changed.append(root / relative / manifest_name)
        changed.extend(root / directory for directory in tree.directories)
        with guard_state_root(root, retained_dirs=tree.directories) as lease:
            current = lease.stat(relative)
            self._require_direct_directory(current)
            if self._directory_identity(current) != tree.identity:
                raise ValueError("retired directory identity changed")
            for name in tree.files:
                lease.unlink(relative / Path(name))
            lease.unlink(relative / manifest_name)
        for directory in sorted(
            tree.directories,
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        ):
            parent = directory.parent
            retained = () if parent == Path(".") else (parent,)
            with guard_state_root(root, retained_dirs=retained) as lease:
                current = lease.stat(directory)
                self._require_direct_directory(current)
                if directory == relative and (
                    self._directory_identity(current) != tree.identity
                ):
                    raise ValueError("retired directory identity changed")
                lease.rmdir(directory)
                lease.fsync_directory(parent)
        self._record_changes(tuple(changed))

    def _stage_and_publish_generation(
        self,
        artifact: VersionedArtifact,
        *,
        recovering: bool,
    ) -> Path:
        final_relative = _GENERATIONS_RELATIVE / artifact.identifier
        final = self.state_paths.root / final_relative
        if self._state_lease().exists(final_relative):
            if not recovering:
                raise FileExistsError("generation destination already exists")
            self._validate_generation_directory(
                self.state_paths.root, final_relative, artifact
            )
            return final
        stage_relative = _STAGING_RELATIVE / artifact.identifier

        def record_publication() -> None:
            self._record_changes(self._artifact_changed_paths(final, artifact))

        self._stage_and_publish_artifact(
            self.state_paths.root,
            stage_relative,
            final_relative,
            artifact,
            on_published=record_publication,
            preserve_published=True,
        )
        self._validate_generation_directory(
            self.state_paths.root, final_relative, artifact
        )
        return final

    def _ensure_capsule(
        self,
        root: Path,
        artifact: VersionedArtifact,
        transaction_id: str,
        *,
        recovering: bool,
    ) -> Path:
        target = root / _NAME
        if self._direct_entry_exists(target):
            if not recovering:
                raise FileExistsError("skill capsule destination already exists")
            self._validate_capsule_directory(root, Path(_NAME), artifact)
            return target
        stage_relative = Path(f".{_NAME}.capsule-{transaction_id[2:]}")
        self._stage_and_publish_artifact(
            root,
            stage_relative,
            Path(_NAME),
            artifact,
            on_published=lambda: self._record_changes(
                self._artifact_changed_paths(target, artifact)
            ),
        )
        self._validate_capsule_directory(root, Path(_NAME), artifact)
        return target

    def _stage_and_publish_artifact(
        self,
        root: Path,
        stage_relative: Path,
        final_relative: Path,
        artifact: VersionedArtifact,
        *,
        on_published: Callable[[], None] | None = None,
        preserve_published: bool = False,
    ) -> None:
        directories = self._artifact_directories(stage_relative, artifact)
        expected_identity: tuple[int, int] | None = None
        publication_committed = False
        publication_callback_completed = False

        def notify_publication() -> None:
            nonlocal publication_committed, publication_callback_completed
            publication_committed = True
            if on_published is not None:
                on_published()
            publication_callback_completed = True

        try:
            with guard_state_root(
                root,
                retained_dirs=tuple(
                    path
                    for path in (stage_relative.parent, stage_relative)
                    if path != Path(".")
                ),
                create_retained=True,
                exclusive_create_retained=(stage_relative,),
            ) as lease:
                expected_identity = self._directory_identity(
                    lease.stat(stage_relative)
                )
                lease.write_bytes_exclusive(
                    stage_relative / artifact.manifest_name,
                    artifact.files[artifact.manifest_name],
                )
                lease.fsync_directory(stage_relative)
            for directory in directories:
                if directory == stage_relative:
                    continue
                retained = {
                    stage_relative.parent,
                    stage_relative,
                    directory.parent,
                }
                retained.discard(Path("."))
                with guard_state_root(
                    root,
                    retained_dirs=tuple(
                        sorted(
                            retained,
                            key=lambda path: (len(path.parts), str(path)),
                        )
                    ),
                ) as lease:
                    if self._directory_identity(lease.stat(stage_relative)) != (
                        expected_identity
                    ):
                        raise ValueError("staging directory identity changed")
                    lease.mkdir(directory)
                    lease.fsync_directory(directory.parent)
            with guard_state_root(
                root,
                retained_dirs=tuple(
                    path
                    for path in (stage_relative.parent, *directories)
                    if path != Path(".")
                ),
            ) as lease:
                if self._directory_identity(lease.stat(stage_relative)) != (
                    expected_identity
                ):
                    raise ValueError("staging directory identity changed")
                for relative, data in artifact.files.items():
                    if relative == artifact.manifest_name:
                        continue
                    self._write_staged_file(
                        lease, stage_relative / Path(relative), data
                    )
                for directory in sorted(
                    directories,
                    key=lambda path: (len(path.parts), str(path)),
                    reverse=True,
                ):
                    lease.fsync_directory(directory)
                if artifact.kind == "generation":
                    self._validate_generation_directory(
                        root, stage_relative, artifact
                    )
                else:
                    self._validate_capsule_directory(
                        root, stage_relative, artifact
                    )
            parents = {
                parent
                for parent in (stage_relative.parent, final_relative.parent)
                if parent != Path(".")
            }
            with guard_state_root(
                root,
                retained_dirs=tuple(
                    sorted(parents, key=lambda path: (len(path.parts), str(path)))
                ),
            ) as lease:
                lease.publish_directory_no_replace(
                    stage_relative,
                    final_relative,
                    expected_identity,
                    on_committed=notify_publication,
                )
                lease.fsync_directory(stage_relative.parent)
                if final_relative.parent != stage_relative.parent:
                    lease.fsync_directory(final_relative.parent)
        except BaseException:
            if expected_identity is not None:
                if publication_committed and not publication_callback_completed:
                    if not preserve_published:
                        self._cleanup_staged_artifact(
                            root, final_relative, artifact, expected_identity
                        )
                self._cleanup_staged_artifact(
                    root, stage_relative, artifact, expected_identity
                )
            raise

    def _write_staged_file(
        self, lease: StateRootLease, relative: Path, data: bytes
    ) -> None:
        temporary = relative.parent / f".{relative.name}.tmp"
        lease.write_bytes_exclusive(temporary, data)
        lease.replace(temporary, relative)
        lease.fsync_directory(relative.parent)

    def _cleanup_staged_artifact(
        self,
        root: Path,
        stage_relative: Path,
        artifact: VersionedArtifact,
        expected_identity: tuple[int, int],
    ) -> None:
        tree = self._preflight_staged_cleanup(
            root, stage_relative, artifact, expected_identity
        )
        if tree is None:
            return
        retained = tuple(
            path
            for path in (stage_relative.parent, *tree.directories)
            if path != Path(".")
        )
        with guard_state_root(root, retained_dirs=retained) as lease:
            for directory in tree.directories:
                info = lease.stat(directory)
                self._require_direct_directory(info)
                if self._directory_identity(info) != tree.identities[directory]:
                    raise ValueError("cleanup directory identity changed")
                if frozenset(lease.listdir(directory)) != tree.children[directory]:
                    raise ValueError("cleanup tree changed after preflight")
            for path in tree.files:
                info = lease.stat(path)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & _WINDOWS_REPARSE_POINT
                ):
                    raise ValueError("unsafe cleanup file")
            for path in tree.files:
                lease.unlink(path)
        for directory in sorted(
            tree.directories,
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        ):
            parent = directory.parent
            retained = () if parent == Path(".") else (parent,)
            with guard_state_root(root, retained_dirs=retained) as lease:
                info = lease.stat(directory)
                self._require_direct_directory(info)
                if self._directory_identity(info) != tree.identities[directory]:
                    raise ValueError("cleanup directory identity changed")
                lease.rmdir(directory)
                lease.fsync_directory(parent)

    def _preflight_journal_cleanup(
        self, relative: Path, journal: OwnershipJournal
    ) -> _JournalCleanupTree:
        """Anchor one exact partial candidate before any recovery deletion."""
        root = self.state_paths.root
        absolute = root / relative
        root_info = absolute.lstat()
        self._require_direct_directory(root_info)
        root_identity = self._directory_identity(root_info)
        root_names = frozenset(os.listdir(absolute))
        if not root_names:
            return _JournalCleanupTree(
                identity=root_identity,
                manifest_bytes=None,
                directories=(relative,),
                identities={relative: root_identity},
                files=(),
                children={relative: frozenset()},
            )
        if "generation.json" not in root_names:
            raise ValueError("journal candidate content has no anchored manifest")
        with guard_state_root(root, retained_dirs=(relative,)) as lease:
            manifest_bytes = lease.read_bytes(
                relative / "generation.json",
                _MANIFEST_LIMIT,
                "journal candidate manifest",
            )
        manifest = validate_anchored_manifest(
            manifest_bytes,
            kind="generation",
            identifier=journal.candidate.generation_id,
            expected_manifest_digest=journal.candidate.manifest_digest,
            expected_package_hash=journal.candidate.package_hash,
            expected_package_version=journal.candidate.package_version,
        )
        names = tuple(str(value) for value in manifest["files"])
        expected_directories = set(self._manifest_directories(relative, names))
        declared_files = {relative / Path(name) for name in names}
        temporary_files = {
            path.parent / f".{path.name}.tmp" for path in declared_files
        }
        allowed_files = declared_files | temporary_files | {
            relative / "generation.json"
        }
        identities: dict[Path, tuple[int, int]] = {}
        children: dict[Path, frozenset[str]] = {}
        files: list[Path] = []
        pending = [relative]
        while pending:
            directory = pending.pop()
            directory_path = root / directory
            before = directory_path.lstat()
            self._require_direct_directory(before)
            identity = self._directory_identity(before)
            identities[directory] = identity
            observed: set[str] = set()
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    observed.add(entry.name)
                    path = directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and not self._is_alias(info):
                        if path not in expected_directories:
                            raise ValueError("unexpected journal cleanup directory")
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode) and not self._is_alias(info):
                        if path not in allowed_files:
                            raise ValueError("unexpected journal cleanup file")
                        if path != relative / "generation.json":
                            files.append(path)
                    else:
                        raise ValueError("unsafe journal cleanup entry")
            after = directory_path.lstat()
            self._require_direct_directory(after)
            if self._directory_identity(after) != identity:
                raise ValueError("journal cleanup directory identity changed")
            children[directory] = frozenset(observed)

        directories = tuple(
            sorted(identities, key=lambda path: (len(path.parts), str(path)))
        )
        retained = tuple(
            sorted(
                {relative.parent, *directories},
                key=lambda path: (len(path.parts), str(path)),
            )
        )
        hashes = manifest["file_hashes"]
        assert isinstance(hashes, Mapping)
        with guard_state_root(root, retained_dirs=retained) as lease:
            for directory in directories:
                current = lease.stat(directory)
                self._require_direct_directory(current)
                if self._directory_identity(current) != identities[directory]:
                    raise ValueError("journal cleanup directory identity changed")
                if frozenset(lease.listdir(directory)) != children[directory]:
                    raise ValueError("journal cleanup tree changed during preflight")
            for path in files:
                info = lease.stat(path)
                if not stat.S_ISREG(info.st_mode) or self._is_alias(info):
                    raise ValueError("unsafe journal cleanup file")
                data = lease.read_bytes(
                    path, _MANAGED_FILE_LIMIT, "journal candidate file"
                )
                if path in declared_files:
                    name = path.relative_to(relative).as_posix()
                    if hashlib.sha256(data).hexdigest() != hashes[name]:
                        raise ValueError("journal candidate file hash changed")
            if (
                lease.read_bytes(
                    relative / "generation.json",
                    _MANIFEST_LIMIT,
                    "journal candidate manifest",
                )
                != manifest_bytes
            ):
                raise ValueError("journal candidate manifest changed")
        return _JournalCleanupTree(
            identity=root_identity,
            manifest_bytes=manifest_bytes,
            directories=directories,
            identities=identities,
            files=tuple(sorted(files, key=str)),
            children=children,
        )

    def _remove_journal_candidate(
        self, relative: Path, tree: _JournalCleanupTree
    ) -> None:
        """Remove only one preflighted candidate, keeping its manifest last."""
        root = self.state_paths.root
        if tree.manifest_bytes is None:
            with guard_state_root(root, retained_dirs=(relative.parent,)) as lease:
                current = lease.stat(relative)
                self._require_direct_directory(current)
                if self._directory_identity(current) != tree.identity:
                    raise ValueError("journal candidate identity changed")
                lease.rmdir(relative)
                lease.fsync_directory(relative.parent)
            self._record_changes((root / relative,))
            return

        retained = tuple(
            sorted(
                {relative.parent, *tree.directories},
                key=lambda path: (len(path.parts), str(path)),
            )
        )
        with guard_state_root(root, retained_dirs=retained) as lease:
            for directory in tree.directories:
                current = lease.stat(directory)
                self._require_direct_directory(current)
                if self._directory_identity(current) != tree.identities[directory]:
                    raise ValueError("journal cleanup directory identity changed")
                if frozenset(lease.listdir(directory)) != tree.children[directory]:
                    raise ValueError("journal cleanup tree changed after preflight")
            for path in tree.files:
                lease.unlink(path)
                self._record_changes((root / path,))

        for directory in sorted(
            (path for path in tree.directories if path != relative),
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        ):
            with guard_state_root(
                root, retained_dirs=(directory.parent,)
            ) as lease:
                current = lease.stat(directory)
                self._require_direct_directory(current)
                if self._directory_identity(current) != tree.identities[directory]:
                    raise ValueError("journal cleanup directory identity changed")
                lease.rmdir(directory)
                lease.fsync_directory(directory.parent)
            self._record_changes((root / directory,))

        manifest = relative / "generation.json"
        with guard_state_root(root, retained_dirs=(relative,)) as lease:
            current = lease.stat(relative)
            self._require_direct_directory(current)
            if self._directory_identity(current) != tree.identity:
                raise ValueError("journal candidate identity changed")
            if frozenset(lease.listdir(relative)) != {"generation.json"}:
                raise ValueError("journal candidate changed before retirement")
            try:
                lease.unlink(manifest)
            except OSError:
                if not lease.exists(manifest):
                    lease.write_bytes_exclusive(manifest, tree.manifest_bytes)
                    lease.fsync_directory(relative)
                raise
        self._record_changes((root / manifest,))
        try:
            with guard_state_root(root, retained_dirs=(relative.parent,)) as lease:
                current = lease.stat(relative)
                self._require_direct_directory(current)
                if self._directory_identity(current) != tree.identity:
                    raise ValueError("journal candidate identity changed")
                lease.rmdir(relative)
                lease.fsync_directory(relative.parent)
        except OSError:
            if self._direct_directory_exists(root / relative):
                with guard_state_root(root, retained_dirs=(relative,)) as lease:
                    current = lease.stat(relative)
                    self._require_direct_directory(current)
                    if self._directory_identity(current) != tree.identity:
                        raise ValueError("journal candidate identity changed")
                    if lease.listdir(relative):
                        raise ValueError("journal candidate changed after cleanup")
                    lease.write_bytes_exclusive(manifest, tree.manifest_bytes)
                    lease.fsync_directory(relative)
            raise
        self._record_changes((root / relative,))

    def _preflight_staged_cleanup(
        self,
        root: Path,
        stage_relative: Path,
        artifact: VersionedArtifact,
        expected_identity: tuple[int, int],
    ) -> _CleanupTree | None:
        stage = root / stage_relative
        try:
            root_info = stage.lstat()
        except FileNotFoundError:
            return None
        self._require_direct_directory(root_info)
        if self._directory_identity(root_info) != expected_identity:
            raise ValueError("staging directory identity changed")

        expected_directories = set(
            self._artifact_directories(stage_relative, artifact)
        )
        expected_files = {
            stage_relative / Path(relative) for relative in artifact.files
        }
        expected_temporaries = {
            path.parent / f".{path.name}.tmp" for path in expected_files
        }
        allowed_files = expected_files | expected_temporaries
        identities: dict[Path, tuple[int, int]] = {}
        children: dict[Path, frozenset[str]] = {}
        files: list[Path] = []
        pending = [stage_relative]
        while pending:
            directory = pending.pop()
            absolute = root / directory
            before = absolute.lstat()
            self._require_direct_directory(before)
            identity = self._directory_identity(before)
            identities[directory] = identity
            names: set[str] = set()
            with os.scandir(absolute) as entries:
                for entry in entries:
                    names.add(entry.name)
                    path = directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and not (
                        stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0)
                        & _WINDOWS_REPARSE_POINT
                    ):
                        if path not in expected_directories:
                            raise ValueError("unexpected cleanup entry")
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode) and not (
                        stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0)
                        & _WINDOWS_REPARSE_POINT
                    ):
                        if path not in allowed_files:
                            raise ValueError("unexpected cleanup entry")
                        files.append(path)
                    else:
                        raise ValueError("unsafe cleanup entry")
            after = absolute.lstat()
            self._require_direct_directory(after)
            if self._directory_identity(after) != identity:
                raise ValueError("cleanup directory identity changed")
            children[directory] = frozenset(names)

        directories = tuple(
            sorted(identities, key=lambda path: (len(path.parts), str(path)))
        )
        retained = tuple(
            path
            for path in (stage_relative.parent, *directories)
            if path != Path(".")
        )
        with guard_state_root(root, retained_dirs=retained) as lease:
            for directory in directories:
                info = lease.stat(directory)
                self._require_direct_directory(info)
                if self._directory_identity(info) != identities[directory]:
                    raise ValueError("cleanup directory identity changed")
                if frozenset(lease.listdir(directory)) != children[directory]:
                    raise ValueError("cleanup tree changed during preflight")
        return _CleanupTree(
            directories=directories,
            identities=identities,
            files=tuple(sorted(files, key=str)),
            children=children,
        )

    def _validate_generation_directory(
        self, root: Path, relative: Path, artifact: VersionedArtifact
    ) -> None:
        if artifact.kind != "generation":
            raise ValueError("invalid generation artifact")
        self._validate_artifact_directory(root, relative, artifact)

    def _validate_capsule_directory(
        self, root: Path, relative: Path, artifact: VersionedArtifact
    ) -> None:
        if artifact.kind != "capsule":
            raise ValueError("invalid capsule artifact")
        self._validate_artifact_directory(root, relative, artifact)

    def _validate_artifact_directory(
        self, root: Path, relative: Path, artifact: VersionedArtifact
    ) -> None:
        directories = self._artifact_directories(relative, artifact)
        children: dict[Path, set[str]] = {
            directory: set() for directory in directories
        }
        for directory in directories:
            if directory != relative:
                children[directory.parent].add(directory.name)
        for name in artifact.files:
            path = relative / Path(name)
            children[path.parent].add(path.name)
        with guard_state_root(root, retained_dirs=directories) as lease:
            for directory in directories:
                if set(lease.listdir(directory)) != children[directory]:
                    raise ValueError("published artifact tree is not exact")
            for name, expected in artifact.files.items():
                path = relative / Path(name)
                info = lease.stat(path)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & _WINDOWS_REPARSE_POINT
                ):
                    raise ValueError("published artifact contains an unsafe file")
                actual = lease.read_bytes(
                    path, _MANAGED_FILE_LIMIT, "published artifact file"
                )
                if actual != expected:
                    raise ValueError("published artifact file hash changed")

    @staticmethod
    def _artifact_directories(
        root: Path, artifact: VersionedArtifact
    ) -> tuple[Path, ...]:
        return GenericAdapter._manifest_directories(root, tuple(artifact.files))

    @staticmethod
    def _artifact_changed_paths(
        root: Path, artifact: VersionedArtifact
    ) -> tuple[Path, ...]:
        directories = {root}
        files: list[Path] = []
        for name in artifact.files:
            path = root / Path(name)
            files.append(path)
            parent = path.parent
            while parent != root:
                directories.add(parent)
                parent = parent.parent
        return tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        ) + tuple(sorted(files, key=str))

    def _smoke_generation(
        self, capsule: VersionedArtifact, generation: VersionedArtifact
    ) -> None:
        capsule_manifest = capsule.files.get("capsule.json")
        generation_manifest = generation.files.get("generation.json")
        if capsule_manifest is None or generation_manifest is None:
            raise ValueError("prepared runtime is incomplete")
        validate_anchored_manifest(
            capsule_manifest,
            kind="capsule",
            identifier=capsule.identifier,
            expected_manifest_digest=capsule.manifest_digest,
            expected_package_hash=capsule.package_hash,
            expected_package_version=capsule.package_version,
        )
        validate_anchored_manifest(
            generation_manifest,
            kind="generation",
            identifier=generation.identifier,
            expected_manifest_digest=generation.manifest_digest,
            expected_package_hash=generation.package_hash,
            expected_package_version=generation.package_version,
        )
        skill_metadata = capsule.files.get("SKILL.md", b"").decode("utf-8")
        if "name: voice-intent-normalizer" not in skill_metadata:
            raise ValueError("prepared capsule metadata is invalid")
        with tempfile.TemporaryDirectory(prefix="voice-intent-smoke-") as sandbox:
            sandbox_root = Path(sandbox)
            state = sandbox_root / "state"
            capsule_root = sandbox_root / "skills" / _NAME
            generation_root = (
                state / _GENERATIONS_RELATIVE / generation.identifier
            )
            for artifact, destination in (
                (capsule, capsule_root),
                (generation, generation_root),
            ):
                for relative, data in artifact.files.items():
                    target = destination / Path(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            status = status_v5_payload(
                skill_root=capsule_root.parent,
                capability=CapabilityLevel.MANUAL.value,
                capsule=capsule,
                active=generation,
                previous=None,
            )
            status_path = state / _STATUS_RELATIVE
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_bytes(canonical_json_bytes(status))
            working = sandbox_root / "cwd"
            working.mkdir()
            result = self._run_capsule(capsule_root, state, working)
        self._require_successful_capsule_diagnostic(
            result,
            expected_state_root=state,
            error="published generation smoke test failed",
        )

    def _smoke_installed(self, root: Path) -> None:
        result = self._run_capsule(root / _NAME, self.state_paths.root, root)
        self._require_successful_capsule_diagnostic(
            result,
            expected_state_root=self.state_paths.root,
            error="installed capsule cannot reach its active generation",
        )

    @staticmethod
    def _require_successful_capsule_diagnostic(
        result: subprocess.CompletedProcess[str],
        *,
        expected_state_root: Path,
        error: str,
    ) -> None:
        if result.returncode != 0:
            raise ValueError(error)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(error) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"status", "state_root", "diagnostics"}
            or payload.get("status") != "ok"
            or payload.get("state_root") != os.fspath(expected_state_root)
            or payload.get("diagnostics") != []
        ):
            raise ValueError(error)

    @staticmethod
    def _run_capsule(
        capsule: Path, state: Path, working: Path
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "VOICE_INTENT_HOME": str(state),
            "PYTHONUTF8": "1",
        }
        if os.name == "nt":
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(capsule / "scripts" / "voice_intent.py"),
                "doctor",
                "--json",
            ],
            cwd=working,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    @contextmanager
    def _state_operation(
        self, *, create: bool, lock_missing: bool = True
    ) -> Iterator[StateRootLease | None]:
        tracked = (
            self.state_paths.root,
            self.state_paths.root / "adapters",
            self.state_paths.root / _GENERIC_RELATIVE,
            self.state_paths.root / _GENERATIONS_RELATIVE,
            self.state_paths.root / _STAGING_RELATIVE,
            self.state_paths.root / _RETIRED_RELATIVE,
        )
        existed = {path: self._direct_directory_exists(path) for path in tracked}
        retained = (
            Path("adapters"),
            _GENERIC_RELATIVE,
            _GENERATIONS_RELATIVE,
            _STAGING_RELATIVE,
            _RETIRED_RELATIVE,
        )
        try:
            with guard_state_root(
                self.state_paths.root,
                create=create,
                retained_dirs=retained,
                create_retained=create,
            ) as lease:
                if create:
                    self._record_created_state_directories(existed)
                if not lease.root_exists and not lock_missing:
                    previous = self._active_state_lease
                    self._active_state_lease = lease
                    try:
                        yield lease
                    finally:
                        self._active_state_lease = previous
                    return
                with _retained_lease_update_lock(lease):
                    previous = self._active_state_lease
                    self._active_state_lease = lease
                    try:
                        yield lease
                    finally:
                        self._active_state_lease = previous
        except BaseException:
            if create:
                self._record_created_state_directories(existed)
            raise

    def _record_created_state_directories(
        self, existed: dict[Path, bool]
    ) -> None:
        self._record_changes(
            tuple(
                path
                for path, was_present in existed.items()
                if not was_present and self._direct_directory_exists(path)
            )
        )

    def _state_lease(self) -> StateRootLease:
        if self._active_state_lease is None:
            raise RuntimeError("adapter state operation is not locked")
        return self._active_state_lease

    def _safe_skill_root(self, value: Path) -> Path:
        root = validate_state_root(value)
        if not root.exists() or not root.is_dir():
            raise ValueError("skill root must be an existing direct directory")
        return root

    @staticmethod
    def _direct_entry_exists(path: Path) -> bool:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _direct_directory_exists(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISDIR(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT
        )

    @staticmethod
    def _require_direct_directory(info: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT
        ):
            raise ValueError("artifact root must be a direct directory")

    @staticmethod
    def _is_alias(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        )

    @staticmethod
    def _same_selected_root(left: Path, right: Path) -> bool:
        if os.name == "nt":
            return os.path.normcase(os.fspath(left)) == os.path.normcase(
                os.fspath(right)
            )
        return os.fspath(left) == os.fspath(right)

    @staticmethod
    def _directory_identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    def _reset_operation(self) -> None:
        self._change_events = []
        self._recovery_changes = ()
        self._committed_install_status_bytes = None
        self._committed_install_result = None

    def _committed_result(
        self, operation: str, options: InstallOptions
    ) -> AdapterResult:
        return AdapterResult(
            self.platform,
            operation,
            self._capability(options),
            (
                "skill discovery must be enabled by the selected host",
                self._manual_message(options),
            ),
        )

    @staticmethod
    def _capability(options: InstallOptions) -> CapabilityLevel:
        return (
            CapabilityLevel.IMPLICIT
            if options.implicit_invocation_confirmed
            else CapabilityLevel.MANUAL
        )

    def _failed(
        self, message: str, changed_paths: tuple[Path, ...] = ()
    ) -> AdapterResult:
        return AdapterResult(
            self.platform,
            "failed",
            CapabilityLevel.UNAVAILABLE,
            (message,),
            changed_paths,
        )

    @staticmethod
    def _manual_message(options: InstallOptions) -> str:
        return GenericAdapter._manual_message_for(GenericAdapter._capability(options))

    @staticmethod
    def _manual_message_for(capability: CapabilityLevel) -> str:
        if capability is CapabilityLevel.IMPLICIT:
            return "manual action: host-confirmed description-based invocation"
        return "manual action: invoke this skill through the host"

    @staticmethod
    def _dedupe_paths(*groups: Sequence[Path]) -> tuple[Path, ...]:
        seen: set[Path] = set()
        result: list[Path] = []
        for group in groups:
            for path in group:
                if path not in seen:
                    seen.add(path)
                    result.append(path)
        return tuple(result)

    def _with_recovery_changes(self, result: AdapterResult) -> AdapterResult:
        if not self._change_events and not self._recovery_changes:
            return result
        return AdapterResult(
            result.platform,
            result.status,
            result.capability,
            result.messages,
            self._dedupe_paths(
                tuple(self._change_events),
                self._recovery_changes,
                result.changed_paths,
            ),
        )

    def _record_changes(self, paths: Sequence[Path]) -> None:
        for path in paths:
            if path not in self._change_events:
                self._change_events.append(path)

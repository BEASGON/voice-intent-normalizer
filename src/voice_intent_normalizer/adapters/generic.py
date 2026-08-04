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
from .generic_contract import GenerationRef, StatusV5, canonical_json_bytes
from .generic_layout import (
    VersionedArtifact,
    VersionedArtifacts,
    canonical_status_v5,
    generic_layout_paths,
    prepare_versioned_artifacts,
    recovery_marker_bytes,
    status_v5_payload,
    validate_anchored_manifest,
    validate_recovery_marker,
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
        status = self._read_versioned_status(root)
        if status is None:
            if self._marker_exists():
                return self._failed("unanchored recovery metadata requires review")
            if self._direct_entry_exists(root / _NAME):
                return self._failed(
                    "existing skill capsule is not anchored by adapter status"
                )
            return self._first_install(root, options)

        if status.transaction_phase is not None:
            self._recover_pending(root, allow_capsule_publication=True)
            status = self._read_versioned_status(root)
            if status is None:
                if self._direct_entry_exists(root / _NAME):
                    return self._failed(
                        "incomplete installation could not be recovered"
                    )
                return self._first_install(root, options)
        elif self._marker_exists():
            return self._failed("unanchored recovery metadata requires review")

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
        payload = status_v5_payload(
            capability=self._capability(options).value,
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
            transaction_id=transaction_id,
            transaction_phase="generation-published",
        )
        self._stage_and_publish_generation(
            artifacts.generation,
            recovering=False,
            on_published=lambda: self._write_transaction_status(root, payload),
        )
        self._ensure_capsule(
            root, artifacts.capsule, transaction_id, recovering=False
        )
        payload = self._replace_phase(payload, "capsule-published")
        self._write_transaction_status(root, payload)
        payload = self._replace_phase(payload, "activation-pending")
        self._write_transaction_status(root, payload)
        final = status_v5_payload(
            capability=self._capability(options).value,
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
        )
        pending = self._read_required_status(root)
        self._validate_capsule_ref(root, pending)
        self._validate_generation_ref(pending.active)
        self._smoke_installed(root)
        result = self._committed_result("installed", options)
        self._write_terminal_status(root, final)
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
        payload = status_v5_payload(
            capability=self._capability(options).value,
            capsule=status.capsule,
            active=status.active,
            previous=artifacts.generation,
            transaction_id=transaction_id,
            transaction_phase="generation-published",
        )
        try:
            self._stage_and_publish_generation(
                artifacts.generation,
                recovering=False,
                on_published=lambda: self._write_transaction_status(root, payload),
            )
            payload = self._replace_phase(payload, "activation-pending")
            self._write_transaction_status(root, payload)
            final = status_v5_payload(
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
            self._smoke_installed(root)
            result = self._committed_result("upgraded", options)
            self._write_terminal_status(root, final)
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
        lease = self._state_lease()
        if (
            not lease.root_exists
            or not lease.available(_GENERIC_RELATIVE)
            or not lease.exists(_STATUS_RELATIVE)
        ):
            return None
        raw = lease.read_bytes(_STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status")
        status, _ = canonical_status_v5(
            raw,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        return status

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

    def _write_transaction_status(
        self, root: Path, payload: dict[str, object]
    ) -> None:
        status_bytes = canonical_json_bytes(payload)
        status, canonical = canonical_status_v5(
            status_bytes,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        if status.transaction_id is None:
            raise ValueError("transaction status is missing its transaction")
        self._write_status_payload(payload)
        marker = recovery_marker_bytes(status.transaction_id, canonical)
        lease = self._state_lease()
        try:
            lease.write_bytes_atomic(_TRANSACTION_RELATIVE, marker)
        except OSError:
            committed = False
            try:
                committed = (
                    lease.read_bytes(
                        _TRANSACTION_RELATIVE,
                        _MANIFEST_LIMIT,
                        "adapter recovery marker",
                    )
                    == marker
                )
            except (OSError, ValueError):
                pass
            if not committed:
                raise
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))
        lease.fsync_directory(_GENERIC_RELATIVE)

    def _write_terminal_status(
        self, root: Path, payload: dict[str, object]
    ) -> None:
        canonical_status_v5(
            canonical_json_bytes(payload),
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )
        self._remove_recovery_marker(missing_ok=True)
        self._write_status_payload(payload)

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

    def _marker_exists(self) -> bool:
        lease = self._state_lease()
        return (
            lease.root_exists
            and lease.available(_GENERIC_RELATIVE)
            and lease.exists(_TRANSACTION_RELATIVE)
        )

    def _ensure_recovery_marker(self, root: Path, status: StatusV5) -> None:
        if status.transaction_id is None:
            if self._marker_exists():
                raise ValueError("recovery marker is not status anchored")
            return
        status_payload = status_v5_payload(
            capability=status.capability,
            capsule=status.capsule,
            active=status.active,
            previous=status.previous,
            transaction_id=status.transaction_id,
            transaction_phase=status.transaction_phase,
        )
        status_bytes = canonical_json_bytes(status_payload)
        lease = self._state_lease()
        if lease.exists(_TRANSACTION_RELATIVE):
            marker = lease.read_bytes(
                _TRANSACTION_RELATIVE,
                _MANIFEST_LIMIT,
                "adapter recovery marker",
            )
            validate_recovery_marker(
                marker,
                transaction_id=status.transaction_id,
                status_bytes=status_bytes,
            )
            return
        marker = recovery_marker_bytes(status.transaction_id, status_bytes)
        lease.write_bytes_atomic(_TRANSACTION_RELATIVE, marker)
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _remove_recovery_marker(self, *, missing_ok: bool) -> None:
        lease = self._state_lease()
        if not lease.exists(_TRANSACTION_RELATIVE):
            if missing_ok:
                return
            raise FileNotFoundError("adapter recovery marker is missing")
        lease.unlink(_TRANSACTION_RELATIVE)
        lease.fsync_directory(_GENERIC_RELATIVE)
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _recover_pending(
        self, root: Path, *, allow_capsule_publication: bool
    ) -> None:
        for _ in range(8):
            status = self._read_required_status(root)
            phase = status.transaction_phase
            if phase is None:
                if self._marker_exists():
                    raise ValueError("recovery marker is not status anchored")
                return
            self._ensure_recovery_marker(root, status)
            if phase in {
                "generation-published",
                "capsule-published",
                "activation-pending",
                "rollback-pending",
            }:
                self._recover_activation(
                    root,
                    status,
                    allow_capsule_publication=allow_capsule_publication,
                )
            elif phase in {
                "deactivation-pending",
                "capsule-retired",
                "cleanup-pending",
            }:
                self._recover_uninstall(root, status)
            else:
                raise ValueError("unsupported adapter transaction phase")
            if self._read_versioned_status(root) is None:
                return
        raise OSError("adapter recovery exceeded its transition bound")

    def _recover_activation(
        self,
        root: Path,
        status: StatusV5,
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
                    capability=status.capability,
                    capsule=status.capsule,
                    active=status.active,
                    previous=None,
                    transaction_id=status.transaction_id,
                    transaction_phase="capsule-published",
                )
                self._write_transaction_status(root, payload)
                return
            self._validate_capsule_ref(root, status)
            try:
                self._validate_generation_ref(status.previous)
            except Exception:
                payload = status_v5_payload(
                    capability=status.capability,
                    capsule=status.capsule,
                    active=status.active,
                    previous=status.previous,
                    transaction_id=status.transaction_id,
                    transaction_phase="rollback-pending",
                )
                self._write_transaction_status(root, payload)
                return
            payload = status_v5_payload(
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="activation-pending",
            )
            self._write_transaction_status(root, payload)
            return

        if phase == "capsule-published":
            if status.previous is not None:
                raise ValueError("capsule publication phase has an invalid candidate")
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            payload = status_v5_payload(
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=None,
                transaction_id=status.transaction_id,
                transaction_phase="activation-pending",
            )
            self._write_transaction_status(root, payload)
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
                        capability=status.capability,
                        capsule=status.capsule,
                        active=status.active,
                        previous=status.previous,
                        transaction_id=status.transaction_id,
                        transaction_phase="rollback-pending",
                    )
                    self._write_transaction_status(root, payload)
                    return
                active = status.previous
                previous = status.active
            final = status_v5_payload(
                capability=status.capability,
                capsule=status.capsule,
                active=active,
                previous=previous,
            )
            self._write_terminal_status(root, final)
            return

        if phase == "rollback-pending":
            self._validate_capsule_ref(root, status)
            self._validate_generation_ref(status.active)
            final = status_v5_payload(
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=None,
            )
            self._write_terminal_status(root, final)
            return
        raise ValueError("invalid activation recovery phase")

    def _rollback_first_install(self, root: Path, status: StatusV5) -> None:
        assert status.transaction_id is not None
        self._retire_generation(status.active, status.transaction_id)
        self._remove_recovery_marker(missing_ok=True)
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
            if not self._status_exists() and not self._marker_exists():
                return AdapterResult(
                    self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
                )
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                ("selected skill root is required to verify installed state",),
            )
        root = self._safe_skill_root(root)
        status = self._read_versioned_status(root)
        if status is None:
            if self._marker_exists() or self._direct_entry_exists(root / _NAME):
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
        if status.transaction_phase is not None:
            self._recover_pending(root, allow_capsule_publication=False)
            status = self._read_versioned_status(root)
            if status is None:
                return AdapterResult(
                    self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
                )
        elif self._marker_exists():
            raise ValueError("recovery marker is not status anchored")
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
        status = self._read_versioned_status(root)
        if status is None:
            if self._marker_exists() or self._direct_entry_exists(root / _NAME):
                return self._failed("managed ownership cannot be verified")
            return AdapterResult(
                self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
            )
        if status.transaction_phase is not None:
            self._recover_pending(root, allow_capsule_publication=False)
            status = self._read_versioned_status(root)
            if status is None:
                return AdapterResult(
                    self.platform,
                    "uninstalled",
                    CapabilityLevel.MANUAL,
                    ("shared personal and project data were preserved",),
                )
        elif self._marker_exists():
            return self._failed("unanchored recovery metadata requires review")

        self._validate_complete_status(root, status, smoke=False)
        transaction_id = f"t-{secrets.token_hex(16)}"
        payload = status_v5_payload(
            capability=status.capability,
            capsule=status.capsule,
            active=status.active,
            previous=status.previous,
            transaction_id=transaction_id,
            transaction_phase="deactivation-pending",
        )
        self._write_transaction_status(root, payload)
        self._recover_pending(root, allow_capsule_publication=False)
        if self._read_versioned_status(root) is not None:
            raise OSError("uninstall status removal did not complete")
        if self._direct_entry_exists(root / _NAME) or self._marker_exists():
            raise OSError("uninstall left a discoverable capsule or marker")
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
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="capsule-retired",
            )
            self._write_transaction_status(root, payload)
            return
        if phase == "capsule-retired":
            if self._direct_entry_exists(root / _NAME):
                raise ValueError("capsule name was replaced after retirement")
            self._remove_retired_capsule(root, status)
            payload = status_v5_payload(
                capability=status.capability,
                capsule=status.capsule,
                active=status.active,
                previous=status.previous,
                transaction_id=status.transaction_id,
                transaction_phase="cleanup-pending",
            )
            self._write_transaction_status(root, payload)
            return
        if phase == "cleanup-pending":
            if self._direct_entry_exists(root / _NAME):
                raise ValueError("capsule name was replaced during cleanup")
            self._remove_retired_capsule(root, status)
            self._retire_generation(status.active, status.transaction_id)
            if status.previous is not None:
                self._retire_generation(status.previous, status.transaction_id)
            self._remove_recovery_marker(missing_ok=True)
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
        if self._marker_exists():
            raise ValueError("recovery marker is not status anchored")
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
        on_published: Callable[[], None] | None = None,
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
            if on_published is not None:
                on_published()

        self._stage_and_publish_artifact(
            self.state_paths.root,
            stage_relative,
            final_relative,
            artifact,
            on_published=record_publication,
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
    ) -> None:
        directories = set(self._artifact_directories(stage_relative, artifact))
        if final_relative.parent != Path("."):
            directories.add(final_relative.parent)
        retained = tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        )
        expected_identity: tuple[int, int] | None = None
        try:
            with guard_state_root(
                root,
                retained_dirs=retained,
                create_retained=True,
                exclusive_create_retained=(stage_relative,),
            ) as lease:
                expected_identity = self._directory_identity(
                    lease.stat(stage_relative)
                )
                for relative, data in artifact.files.items():
                    if relative == artifact.manifest_name:
                        continue
                    self._write_staged_file(
                        lease, stage_relative / Path(relative), data
                    )
                self._write_staged_file(
                    lease,
                    stage_relative / artifact.manifest_name,
                    artifact.files[artifact.manifest_name],
                )
                for directory in sorted(
                    self._artifact_directories(stage_relative, artifact),
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
                    on_committed=on_published,
                )
                lease.fsync_directory(stage_relative.parent)
                if final_relative.parent != stage_relative.parent:
                    lease.fsync_directory(final_relative.parent)
        except BaseException:
            if expected_identity is not None:
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
        directories = self._artifact_directories(stage_relative, artifact)
        try:
            with guard_state_root(root, retained_dirs=directories) as lease:
                if self._directory_identity(lease.stat(stage_relative)) != (
                    expected_identity
                ):
                    return
                for relative in artifact.files:
                    path = stage_relative / Path(relative)
                    temporary = path.parent / f".{path.name}.tmp"
                    lease.unlink(temporary, missing_ok=True)
                    lease.unlink(path, missing_ok=True)
        except (FileNotFoundError, OSError, ValueError):
            return
        for directory in sorted(
            directories,
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        ):
            parent = directory.parent
            retained = () if parent == Path(".") else (parent,)
            try:
                with guard_state_root(root, retained_dirs=retained) as lease:
                    info = lease.stat(directory)
                    self._require_direct_directory(info)
                    if directory == stage_relative and (
                        self._directory_identity(info) != expected_identity
                    ):
                        return
                    lease.rmdir(directory)
                    lease.fsync_directory(parent)
            except (FileNotFoundError, OSError, ValueError):
                return

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
        if result.returncode != 0:
            raise ValueError("published generation smoke test failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("published generation smoke test failed") from exc
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ok",
            "degraded",
        }:
            raise ValueError("published generation smoke test failed")

    def _smoke_installed(self, root: Path) -> None:
        result = self._run_capsule(root / _NAME, self.state_paths.root, root)
        if result.returncode != 0:
            raise ValueError("installed capsule cannot reach its active generation")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("installed capsule returned invalid diagnostics") from exc
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ok",
            "degraded",
        }:
            raise ValueError("installed capsule returned invalid diagnostics")

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

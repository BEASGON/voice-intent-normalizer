"""Safe standards-only installation of the portable skill package."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib

from ..paths import (
    StatePaths,
    StateRootLease,
    guard_state_root,
    state_root_lock_key,
    validate_state_root,
)
from ..updater import _retained_lease_update_lock
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions

_MANIFEST = ".voice-intent-normalizer-install.json"
_NAME = "voice-intent-normalizer"
_STATUS_RELATIVE = Path("adapters") / "generic.json"
_RECOVERY_RELATIVE = Path("adapters") / "generic-recovery.json"
_PACKAGE_FILES = ("SKILL.md", "pyproject.toml", "LICENSE")
_PACKAGE_DIRECTORIES = ("agents", "assets", "references")
_REQUIRED_FILES = (
    "SKILL.md",
    "scripts/voice_intent.py",
    "src/voice_intent_normalizer/__init__.py",
    "src/voice_intent_normalizer/cli.py",
)
_MANIFEST_LIMIT = 2 * 1024 * 1024
_MANAGED_FILE_LIMIT = 64 * 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class _Inspection:
    state: str
    manifest: dict[str, object] | None = None
    root_identity: tuple[int, int] | None = None
    target_identity: tuple[int, int] | None = None


class GenericAdapter:
    """Install exactly one self-contained Agent Skills-compatible directory."""

    platform = "generic"

    def __init__(self, repository: str | Path, state_paths: StatePaths) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths
        self._configured_skill_root: Path | None = None
        self._active_state_lease: StateRootLease | None = None
        self._active_skill_lease: StateRootLease | None = None
        self._active_skill_root: Path | None = None
        self._active_moved: tuple[Path, ...] = ()
        self._recovery_changes: tuple[Path, ...] = ()

    def detect(self) -> AdapterResult:
        return self.doctor()

    def install(self, options: InstallOptions) -> AdapterResult:
        self._recovery_changes = ()
        try:
            with self._state_operation(create=True):
                result = self._install_locked(options)
        except Exception:
            result = self._failed("generic installation was not completed")
        return self._with_recovery_changes(result)

    def _install_locked(self, options: InstallOptions) -> AdapterResult:
        if options.output_dir is None:
            return self._failed("a skill root is required")
        root = self._safe_skill_root(options.output_dir)
        self._configured_skill_root = root
        self._recover_pending(root)
        target = root / _NAME
        staging = root / f".{_NAME}.staging-{secrets.token_hex(12)}"
        try:
            self._copy_runtime(staging)
            self._validate_staging(staging)
            self._write_manifest(staging, root)
            staged = self._inspect_package(root, staging, expected_name=staging.name)
            if staged.state != "valid" or staged.manifest is None:
                raise ValueError("staged package failed integrity validation")
            if not target.exists() and not target.is_symlink():
                return self._commit_new_install(
                    root,
                    target,
                    staging,
                    staged.manifest,
                    options,
                    staged.root_identity,
                    staged.target_identity,
                )
            return self._repair_or_reuse(
                root,
                target,
                staging,
                staged.manifest,
                options,
                staged.target_identity,
            )
        except Exception:
            return self._failed(
                "generic installation was not completed; "
                "a staging directory may remain for safe manual cleanup",
                (staging,),
            )

    def doctor(self) -> AdapterResult:
        self._recovery_changes = ()
        try:
            with self._state_operation(create=False, lock_missing=False):
                result = self._doctor_locked()
        except Exception:
            result = self._failed("shared adapter status is unavailable")
        return self._with_recovery_changes(result)

    def _doctor_locked(self) -> AdapterResult:
        self._recover_pending(self._configured_skill_root)
        recovery = self._read_recovery()
        status_error = False
        try:
            status = self._read_status()
        except Exception:
            status = None
            status_error = True

        root = self._configured_skill_root
        if root is None and status is not None:
            raw_root = status.get("skill_root")
            if isinstance(raw_root, str):
                root = Path(raw_root)

        if root is None:
            messages = ["installed files: absent"]
            if status_error:
                messages.append("adapter status: invalid")
            else:
                messages.append("adapter status: missing")
            if recovery is not None:
                messages.append(
                    "recovery required: installer transaction is incomplete"
                )
            messages.append("manual action required")
            return AdapterResult(
                self.platform,
                "degraded" if status_error or recovery is not None else "not-installed",
                CapabilityLevel.UNAVAILABLE,
                tuple(messages),
            )

        try:
            root = self._safe_skill_root(root)
            target = root / _NAME
            inspection = self._inspect_package(root, target)
        except Exception:
            inspection = _Inspection("invalid")

        messages = [
            f"managed package: {self._inspection_message(inspection.state)}",
            (
                "required file SKILL.md: valid"
                if inspection.state == "valid"
                else "required file SKILL.md: invalid"
            ),
            "skill discovery: host-dependent",
            "automatic trigger: unavailable",
            "strict hook: unavailable",
            "shared state: available",
        ]
        if status_error:
            messages.append("adapter status: invalid")
        elif status is None:
            messages.append("adapter status: missing")
        else:
            messages.append("adapter status: present")
        if recovery is not None:
            messages.append("recovery required: installer transaction is incomplete")

        capability = CapabilityLevel.MANUAL
        if status is not None:
            try:
                capability = CapabilityLevel(str(status["capability"]))
            except (KeyError, ValueError):
                status_error = True
        messages.append(self._manual_message_for(capability))
        healthy = (
            inspection.state == "valid"
            and status is not None
            and not status_error
            and recovery is None
            and self._status_matches(status, root, inspection.manifest)
        )
        return AdapterResult(
            self.platform,
            "installed" if healthy else "degraded",
            capability if healthy else CapabilityLevel.MANUAL,
            tuple(messages),
        )

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed(
                "shared-data removal is intentionally separate and was not performed"
            )
        self._recovery_changes = ()
        try:
            with self._state_operation(create=True):
                result = self._uninstall_locked(options)
        except Exception:
            result = self._failed("generic uninstall was not completed")
        return self._with_recovery_changes(result)

    def _uninstall_locked(self, options: UninstallOptions) -> AdapterResult:
        self._recover_pending(options.output_dir or self._configured_skill_root)
        status = self._read_status()
        if status is None:
            return AdapterResult(
                self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
            )
        root_value = options.output_dir or self._configured_skill_root
        if root_value is None:
            return self._failed("skill root is required for safe uninstall")
        root = self._safe_skill_root(root_value)
        self._configured_skill_root = root
        target = root / _NAME
        inspection = self._inspect_package(root, target)
        if inspection.state != "valid" or inspection.manifest is None:
            return self._failed("managed ownership cannot be verified")
        manifest = inspection.manifest
        if not self._status_matches(status, root, manifest):
            return self._failed("shared status does not match selected skill root")

        paths = self._manifest_paths(target, manifest)
        quarantine = root / f".{_NAME}.quarantine-{secrets.token_hex(12)}"
        self._prepare_quarantine(quarantine, target, paths)
        recovery_path = self._write_recovery(
            "uninstall", root, target, quarantine, paths, manifest
        )
        moved: tuple[Path, ...] = ()
        status_path: Path | None = None
        status_failure: str | None = None
        try:
            with self._skill_transaction(
                root,
                target,
                quarantine,
                paths,
                (),
                expected_root_identity=inspection.root_identity,
                expected_target_identity=inspection.target_identity,
            ) as lease:
                self._verify_manifest_under_lease(
                    lease, target, manifest, require_hashes=True
                )
                moved = self._quarantine_managed_files(target, quarantine)
                try:
                    status_path = self._remove_status()
                except Exception:
                    self._restore_quarantine(quarantine, target, moved)
                    status_failure = "adapter status could not be updated"
                if status_failure is None:
                    try:
                        self._discard_quarantine(quarantine)
                    except Exception:
                        changed = self._changed_after_failed_restore(
                            target, quarantine, moved
                        )
                        return AdapterResult(
                            self.platform,
                            "degraded",
                            CapabilityLevel.UNAVAILABLE,
                            (
                                "uninstall cleanup is incomplete",
                                "recovery is recorded; run doctor before retrying",
                            ),
                            self._dedupe_paths(
                                changed, (status_path, recovery_path)
                            ),
                        )
        except Exception:
            changed = self._changed_after_failed_restore(target, quarantine, paths)
            if changed:
                return AdapterResult(
                    self.platform,
                    "degraded",
                    CapabilityLevel.UNAVAILABLE,
                    (
                        "managed files could not be quarantined",
                        "recovery is recorded; run doctor before retrying",
                    ),
                    self._dedupe_paths(changed, (recovery_path,)),
                )
            self._cleanup_quarantine_dirs(root, quarantine, paths)
            self._remove_recovery(missing_ok=True)
            return self._failed(
                "managed files could not be quarantined; files restored"
            )

        if status_failure is not None:
            return self._restoration_result(
                root,
                target,
                quarantine,
                moved,
                status_failure,
            )

        try:
            self._remove_recovery()
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                (
                    "skill files were removed but recovery metadata remains",
                    "run doctor before retrying",
                ),
                self._dedupe_paths(
                    paths,
                    (status_path,) if status_path is not None else (),
                    (recovery_path,),
                ),
            )
        assert status_path is not None
        return AdapterResult(
            self.platform,
            "uninstalled",
            CapabilityLevel.MANUAL,
            (
                "shared personal and project data were preserved",
                "benign empty managed directories may remain",
            ),
            self._dedupe_paths(paths, (status_path,)),
        )

    @contextmanager
    def _state_operation(
        self, *, create: bool, lock_missing: bool = True
    ) -> Iterator[StateRootLease | None]:
        with guard_state_root(
            self.state_paths.root,
            create=create,
            retained_dirs=("adapters",),
            create_retained=create,
        ) as lease:
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

    def _state_lease(self) -> StateRootLease:
        if self._active_state_lease is None:
            raise RuntimeError("adapter state operation is not locked")
        return self._active_state_lease

    def _safe_skill_root(self, value: Path) -> Path:
        root = validate_state_root(value)
        if not root.exists() or not root.is_dir():
            raise ValueError("skill root must be an existing direct directory")
        return root

    def _commit_new_install(
        self,
        root: Path,
        target: Path,
        staging: Path,
        manifest: dict[str, object],
        options: InstallOptions,
        expected_root_identity: tuple[int, int] | None,
        expected_staging_identity: tuple[int, int] | None,
    ) -> AdapterResult:
        with guard_state_root(root) as lease:
            if (
                expected_root_identity is not None
                and self._directory_identity(lease.stat("."))
                != expected_root_identity
            ):
                raise OSError("skill root identity changed before commit")
            if (
                expected_staging_identity is not None
                and self._directory_identity(lease.stat(staging.name))
                != expected_staging_identity
            ):
                raise OSError("staging identity changed before commit")
            if lease.exists(_NAME):
                raise FileExistsError("skill destination appeared during installation")
            lease.rename_no_replace(staging.name, _NAME)
        inspection = self._inspect_package(root, target)
        if (
            inspection.state != "valid"
            or inspection.manifest is None
            or inspection.manifest["install_id"] != manifest["install_id"]
        ):
            raise ValueError("installed package identity changed during commit")
        changed = self._package_changed_paths(target, manifest)
        try:
            status_path = self._write_status(target, options)
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                self._capability(options),
                (
                    "skill files installed; shared status recording is pending",
                    "run install again to repair adapter status",
                ),
                changed,
            )
        return AdapterResult(
            self.platform,
            "installed",
            self._capability(options),
            (
                "skill discovery must be enabled by the selected host",
                self._manual_message(options),
            ),
            self._dedupe_paths(changed, (status_path,)),
        )

    def _repair_or_reuse(
        self,
        root: Path,
        target: Path,
        staging: Path,
        desired: dict[str, object],
        options: InstallOptions,
        expected_staging_identity: tuple[int, int] | None,
    ) -> AdapterResult:
        current = self._inspect_package(root, target, allow_hash_failure=True)
        if current.manifest is None or current.state in {
            "missing",
            "manifest-invalid",
            "alias-invalid",
        }:
            if (
                current.target_identity is not None
                and self._is_benign_empty_target(
                    root, target, desired, current.target_identity
                )
            ):
                return self._commit_into_empty_target(
                    root,
                    target,
                    staging,
                    desired,
                    options,
                    current.root_identity,
                    current.target_identity,
                )
            return self._failed("existing skill directory is not installer-managed")
        same_package = (
            current.state == "valid"
            and current.manifest.get("format") == 3
            and current.manifest.get("package_hash") == desired.get("package_hash")
            and current.manifest.get("package_version")
            == desired.get("package_version")
        )
        status = None
        try:
            status = self._read_status()
        except Exception:
            status = None
        desired_capability = self._capability(options).value
        if same_package:
            if (
                status is not None
                and self._status_matches(status, root, current.manifest)
                and status.get("capability") == desired_capability
            ):
                staging_dirs = self._discard_staging_package(
                    root, staging, desired, expected_staging_identity
                )
                return AdapterResult(
                    self.platform,
                    "already-installed",
                    self._capability(options),
                    (
                        "managed skill files are already installed",
                        "benign empty staging directories may remain",
                    ),
                    staging_dirs,
                )
            status_path = self._write_status(target, options)
            staging_dirs = self._discard_staging_package(
                root, staging, desired, expected_staging_identity
            )
            return AdapterResult(
                self.platform,
                "repaired",
                self._capability(options),
                (
                    "adapter status and capability were rebuilt",
                    "benign empty staging directories may remain",
                ),
                self._dedupe_paths((status_path,), staging_dirs),
            )
        return self._upgrade_package(
            root,
            target,
            staging,
            current,
            desired,
            options,
            expected_staging_identity,
        )

    def _upgrade_package(
        self,
        root: Path,
        target: Path,
        staging: Path,
        current: _Inspection,
        desired: dict[str, object],
        options: InstallOptions,
        expected_staging_identity: tuple[int, int] | None,
    ) -> AdapterResult:
        assert current.manifest is not None
        old_paths = self._manifest_paths(target, current.manifest)
        new_paths = self._manifest_paths(target, desired)
        old_relatives = {path.relative_to(target) for path in old_paths}
        for path in new_paths:
            if path.exists() and path.relative_to(target) not in old_relatives:
                return self._failed("managed upgrade would overwrite an unknown file")

        assert current.target_identity is not None
        created_dirs = self._ensure_target_directories(
            root, target, desired, current.target_identity
        )
        quarantine = root / f".{_NAME}.quarantine-{secrets.token_hex(12)}"
        self._prepare_quarantine(quarantine, target, old_paths)
        recovery_path = self._write_recovery(
            "upgrade", root, target, quarantine, old_paths, current.manifest
        )
        staged_paths = self._manifest_paths(staging, desired)
        moved_old: tuple[Path, ...] = ()
        moved_new: list[Path] = []
        move_failure: str | None = None
        try:
            with self._skill_transaction(
                root,
                target,
                quarantine,
                old_paths,
                tuple(staged_paths),
                staging=staging,
                desired_paths=new_paths,
                expected_root_identity=current.root_identity,
                expected_target_identity=current.target_identity,
            ) as lease:
                self._verify_manifest_under_lease(
                    lease, target, current.manifest, require_hashes=False
                )
                moved_old = self._quarantine_managed_files(
                    target, quarantine, allow_missing=True
                )
                try:
                    for source, destination in zip(
                        staged_paths, new_paths, strict=True
                    ):
                        lease.publish_no_replace(
                            source.relative_to(root), destination.relative_to(root)
                        )
                        moved_new.append(destination)
                except Exception:
                    for destination, source in reversed(
                        list(zip(moved_new, staged_paths, strict=False))
                    ):
                        if lease.exists(destination.relative_to(root)):
                            lease.publish_no_replace(
                                destination.relative_to(root),
                                source.relative_to(root),
                            )
                    self._restore_quarantine(quarantine, target, moved_old)
                    move_failure = "managed upgrade was rolled back"
                if move_failure is None:
                    try:
                        status_path = self._write_status(target, options)
                    except Exception:
                        return AdapterResult(
                            self.platform,
                            "degraded",
                            self._capability(options),
                            (
                                "managed package was repaired; "
                                "status recording is pending",
                                "recovery is recorded; run install again",
                                "benign empty staging directories may remain",
                            ),
                            self._dedupe_paths(
                                tuple(new_paths),
                                created_dirs,
                                self._staging_directories(staging, desired),
                                (recovery_path,),
                            ),
                        )
                    try:
                        self._discard_quarantine(quarantine)
                    except Exception:
                        return AdapterResult(
                            self.platform,
                            "degraded",
                            self._capability(options),
                            (
                                "managed package was repaired but "
                                "backup cleanup failed",
                                "recovery is recorded; run doctor before retrying",
                                "benign empty staging directories may remain",
                            ),
                            self._dedupe_paths(
                                tuple(new_paths),
                                created_dirs,
                                self._staging_directories(staging, desired),
                                self._quarantine_changed_paths(
                                quarantine, target, moved_old
                                ),
                                (status_path, recovery_path),
                            ),
                        )
        except Exception:
            if moved_old:
                return self._restoration_result(
                    root,
                    target,
                    quarantine,
                    moved_old,
                    "managed upgrade could not be completed",
                )
            raise

        if move_failure is not None:
            staging_dirs = self._discard_staging_package(
                root, staging, desired, expected_staging_identity
            )
            return self._restoration_result(
                root,
                target,
                quarantine,
                moved_old,
                move_failure,
                staging_dirs,
            )

        staging_dirs = self._staging_directories(staging, desired)
        self._cleanup_quarantine_dirs(root, quarantine, old_paths)
        self._remove_recovery(missing_ok=True)
        operation = (
            "upgraded"
            if current.state == "valid"
            and current.manifest.get("package_version")
            != desired.get("package_version")
            else "repaired"
        )
        obsolete = tuple(path for path in old_paths if path not in set(new_paths))
        return AdapterResult(
            self.platform,
            operation,
            self._capability(options),
            (
                "managed package integrity and adapter status were refreshed",
                "benign empty staging directories may remain",
            ),
            self._dedupe_paths(
                tuple(new_paths),
                obsolete,
                created_dirs,
                (status_path,),
                staging_dirs,
            ),
        )

    def _commit_into_empty_target(
        self,
        root: Path,
        target: Path,
        staging: Path,
        manifest: dict[str, object],
        options: InstallOptions,
        expected_root_identity: tuple[int, int] | None,
        expected_target_identity: tuple[int, int],
    ) -> AdapterResult:
        created_dirs = self._ensure_target_directories(
            root, target, manifest, expected_target_identity
        )
        staged_paths = self._manifest_paths(staging, manifest)
        target_paths = self._manifest_paths(target, manifest)
        recovery_path = self._write_recovery(
            "install", root, target, staging, target_paths, manifest
        )
        retained = set(self._parent_directories(root, staged_paths))
        retained.update(self._parent_directories(root, target_paths))
        retained.discard(Path("."))
        published: list[Path] = []
        try:
            with guard_state_root(
                root,
                retained_dirs=tuple(
                    sorted(retained, key=lambda path: (len(path.parts), str(path)))
                ),
            ) as lease:
                if (
                    expected_root_identity is not None
                    and self._directory_identity(lease.stat("."))
                    != expected_root_identity
                ):
                    raise OSError("skill root identity changed before publish")
                if (
                    self._directory_identity(lease.stat(_NAME))
                    != expected_target_identity
                ):
                    raise OSError("empty managed target identity changed")
                for source, destination in zip(
                    staged_paths, target_paths, strict=True
                ):
                    lease.publish_no_replace(
                        source.relative_to(root), destination.relative_to(root)
                    )
                    published.append(destination)
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.MANUAL,
                (
                    "installation into retained empty directories is incomplete",
                    "recovery is recorded; retry install",
                ),
                self._dedupe_paths(
                    tuple(published),
                    created_dirs,
                    (staging, recovery_path),
                ),
            )
        status_path = self._write_status(target, options)
        self._remove_recovery()
        staging_dirs = self._staging_directories(staging, manifest)
        return AdapterResult(
            self.platform,
            "installed",
            self._capability(options),
            (
                "skill discovery must be enabled by the selected host",
                self._manual_message(options),
            ),
            self._dedupe_paths(
                self._package_changed_paths(target, manifest),
                (status_path,),
                staging_dirs,
            ),
        )

    def _is_benign_empty_target(
        self,
        root: Path,
        target: Path,
        manifest: dict[str, object],
        expected_target_identity: tuple[int, int],
    ) -> bool:
        directories = {Path(_NAME)}
        for relative in self._manifest_files(manifest) or ():
            parent = Path(_NAME) / Path(relative).parent
            while parent != Path("."):
                directories.add(parent)
                if parent == Path(_NAME):
                    break
                parent = parent.parent
        children: dict[Path, set[str]] = {directory: set() for directory in directories}
        for directory in directories:
            if directory != Path(_NAME):
                children[directory.parent].add(directory.name)
        try:
            for directory in sorted(
                directories, key=lambda path: (len(path.parts), str(path))
            ):
                with guard_state_root(root, retained_dirs=(directory,)) as lease:
                    if (
                        self._directory_identity(lease.stat(_NAME))
                        != expected_target_identity
                    ):
                        return False
                    if not lease.available(directory):
                        continue
                    if set(lease.listdir(directory)) - children[directory]:
                        return False
            return True
        except (OSError, ValueError):
            return False

    def _ensure_target_directories(
        self,
        root: Path,
        target: Path,
        manifest: dict[str, object],
        expected_target_identity: tuple[int, int],
    ) -> tuple[Path, ...]:
        directories: set[Path] = set()
        for relative in self._manifest_files(manifest) or ():
            parent = Path(_NAME) / Path(relative).parent
            while parent != Path(_NAME):
                directories.add(parent)
                parent = parent.parent
        created: list[Path] = []
        for directory in sorted(
            directories, key=lambda path: (len(path.parts), str(path))
        ):
            retained = {Path(_NAME), directory.parent}
            with guard_state_root(root, retained_dirs=tuple(retained)) as lease:
                if (
                    self._directory_identity(lease.stat(_NAME))
                    != expected_target_identity
                ):
                    raise OSError("managed target identity changed")
                if lease.exists(directory):
                    info = lease.stat(directory)
                    self._require_direct_directory(info)
                    continue
                lease.mkdir(directory)
                created.append(root / directory)
        return tuple(created)

    @contextmanager
    def _skill_transaction(
        self,
        root: Path,
        target: Path,
        quarantine: Path,
        old_paths: Sequence[Path],
        staged_paths: Sequence[Path],
        *,
        staging: Path | None = None,
        desired_paths: Sequence[Path] = (),
        expected_root_identity: tuple[int, int] | None = None,
        expected_target_identity: tuple[int, int] | None = None,
    ) -> Iterator[StateRootLease]:
        retained = set(self._parent_directories(root, old_paths))
        retained.update(self._parent_directories(root, desired_paths))
        retained.update(
            self._parent_directories(
                root,
                tuple(quarantine / path.relative_to(target) for path in old_paths),
            )
        )
        if staging is not None:
            retained.update(self._parent_directories(root, staged_paths))
        retained.discard(Path("."))
        with guard_state_root(
            root,
            retained_dirs=tuple(sorted(retained, key=lambda p: (len(p.parts), str(p)))),
        ) as lease:
            if (
                expected_root_identity is not None
                and self._directory_identity(lease.stat(".")) != expected_root_identity
            ):
                raise OSError("skill root identity changed before transaction")
            if (
                expected_target_identity is not None
                and self._directory_identity(lease.stat(_NAME))
                != expected_target_identity
            ):
                raise OSError("managed target identity changed before transaction")
            previous_lease = self._active_skill_lease
            previous_root = self._active_skill_root
            previous_moved = self._active_moved
            self._active_skill_lease = lease
            self._active_skill_root = root
            self._active_moved = tuple(old_paths)
            try:
                yield lease
            finally:
                self._active_skill_lease = previous_lease
                self._active_skill_root = previous_root
                self._active_moved = previous_moved

    def _copy_runtime(self, staging: Path) -> None:
        staging.mkdir(mode=0o700)
        for name in _PACKAGE_FILES:
            self._copy_file(self.repository / name, staging / name)
        for directory in _PACKAGE_DIRECTORIES:
            self._copy_tree(self.repository / directory, staging / directory)
        self._copy_file(
            self.repository / "scripts" / "voice_intent.py",
            staging / "scripts" / "voice_intent.py",
        )
        python_source = self.repository / "src" / "voice_intent_normalizer"
        if not python_source.is_dir():
            python_source = Path(__file__).resolve().parents[1]
        self._copy_python_tree(
            python_source,
            staging / "src" / "voice_intent_normalizer",
        )

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        info = os.lstat(source)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        ):
            raise ValueError("runtime source must be a direct regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        after = os.lstat(source)
        if (info.st_dev, info.st_ino, info.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise OSError("runtime source changed while it was copied")
        destination.write_bytes(data)

    def _copy_tree(self, source: Path, destination: Path) -> None:
        for current, directories, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            current_info = os.lstat(current_path)
            self._require_direct_directory(current_info)
            for name in tuple(directories):
                info = os.lstat(current_path / name)
                self._require_direct_directory(info)
                (destination / (current_path / name).relative_to(source)).mkdir(
                    parents=True, exist_ok=True
                )
            for name in files:
                path = current_path / name
                self._copy_file(path, destination / path.relative_to(source))

    def _copy_python_tree(self, source: Path, destination: Path) -> None:
        for current, directories, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            self._require_direct_directory(os.lstat(current_path))
            for name in tuple(directories):
                self._require_direct_directory(os.lstat(current_path / name))
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = current_path / name
                self._copy_file(path, destination / path.relative_to(source))

    @staticmethod
    def _require_direct_directory(info: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        ):
            raise ValueError("runtime source contains a directory alias")

    @staticmethod
    def _validate_staging(staging: Path) -> None:
        for relative in _REQUIRED_FILES:
            path = staging / relative
            info = os.lstat(path)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            ):
                raise ValueError("staged skill is incomplete")
        skill_text = (staging / "SKILL.md").read_text(encoding="utf-8")
        if "name: voice-intent-normalizer" not in skill_text:
            raise ValueError("staged skill metadata is invalid")
        with tempfile.TemporaryDirectory(prefix="voice-intent-stage-") as sandbox:
            state = Path(sandbox) / "state"
            working = Path(sandbox) / "cwd"
            working.mkdir()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(sandbox) / "untrusted"),
                "VOICE_INTENT_HOME": str(state),
            }
            if os.name == "nt":
                environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(staging / "scripts" / "voice_intent.py"),
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
        if result.returncode != 0:
            raise ValueError("staged skill bootstrap validation failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ok",
            "degraded",
        }:
            raise ValueError("staged skill bootstrap validation failed")

    @staticmethod
    def _write_manifest(target: Path, root: Path) -> None:
        files = tuple(
            sorted(
                str(path.relative_to(target)).replace("\\", "/")
                for path in target.rglob("*")
                if path.is_file()
            )
        )
        hashes = {
            relative: hashlib.sha256((target / relative).read_bytes()).hexdigest()
            for relative in files
        }
        version_payload = tomllib.loads(
            (target / "pyproject.toml").read_text(encoding="utf-8")
        )
        package_version = version_payload["project"]["version"]
        package_hash = GenericAdapter._package_hash(
            str(package_version), hashes, _REQUIRED_FILES
        )
        payload = {
            "files": list(files),
            "format": 3,
            "hashes": hashes,
            "install_id": secrets.token_hex(16),
            "owner": _NAME,
            "package_hash": package_hash,
            "package_version": str(package_version),
            "required_files": list(_REQUIRED_FILES),
            "skill_root": str(root),
            "skill_root_key": state_root_lock_key(root),
        }
        (target / _MANIFEST).write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def _inspect_package(
        self,
        root: Path,
        target: Path,
        *,
        allow_hash_failure: bool = False,
        expected_name: str = _NAME,
    ) -> _Inspection:
        raw = b""
        root_identity: tuple[int, int] | None = None
        target_identity: tuple[int, int] | None = None
        if target.name != expected_name:
            return _Inspection("manifest-invalid")
        try:
            with guard_state_root(root, retained_dirs=(expected_name,)) as lease:
                if not lease.available(expected_name):
                    return _Inspection("missing")
                root_identity = self._directory_identity(lease.stat("."))
                target_identity = self._directory_identity(lease.stat(expected_name))
                raw = lease.read_bytes(
                    Path(expected_name) / _MANIFEST,
                    _MANIFEST_LIMIT,
                    "installer manifest",
                )
            payload = json.loads(raw.decode("utf-8"))
            manifest = self._validate_manifest_structure(payload, root)
            retained = self._manifest_parent_directories(expected_name, manifest)
            with guard_state_root(root, retained_dirs=retained) as lease:
                if (
                    self._directory_identity(lease.stat(".")) != root_identity
                    or self._directory_identity(lease.stat(expected_name))
                    != target_identity
                ):
                    raise OSError("managed target identity changed during inspection")
                intact = self._verify_manifest_under_lease(
                    lease,
                    target,
                    manifest,
                    require_hashes=not allow_hash_failure,
                )
            if not intact:
                return _Inspection(
                    "hash-mismatch",
                    manifest,
                    root_identity,
                    target_identity,
                )
            if manifest.get("format") != 3:
                return _Inspection("legacy", manifest, root_identity, target_identity)
            return _Inspection("valid", manifest, root_identity, target_identity)
        except FileNotFoundError:
            if allow_hash_failure:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    manifest = self._validate_manifest_structure(payload, root)
                    return _Inspection(
                        "hash-mismatch",
                        manifest,
                        root_identity,
                        target_identity,
                    )
                except Exception:
                    return _Inspection(
                        "manifest-invalid", None, root_identity, target_identity
                    )
            return _Inspection(
                "hash-mismatch", None, root_identity, target_identity
            )
        except ValueError:
            if allow_hash_failure:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    manifest = self._validate_manifest_structure(payload, root)
                    return _Inspection(
                        "hash-mismatch",
                        manifest,
                        root_identity,
                        target_identity,
                    )
                except Exception:
                    return _Inspection(
                        "manifest-invalid", None, root_identity, target_identity
                    )
            return _Inspection(
                "hash-mismatch", None, root_identity, target_identity
            )
        except (OSError, json.JSONDecodeError, UnicodeError):
            return _Inspection("alias-invalid")

    def _validate_manifest_structure(
        self, payload: object, root: Path
    ) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("invalid installer manifest")
        format_value = payload.get("format")
        expected = {
            "files",
            "format",
            "hashes",
            "install_id",
            "owner",
            "skill_root",
        }
        if format_value == 3:
            expected |= {
                "package_hash",
                "package_version",
                "required_files",
                "skill_root_key",
            }
        elif format_value != 2:
            raise ValueError("unsupported installer manifest")
        if set(payload) != expected:
            raise ValueError("invalid installer manifest fields")
        files = self._manifest_files(payload)
        hashes = payload.get("hashes")
        if (
            payload.get("owner") != _NAME
            or not isinstance(payload.get("install_id"), str)
            or not payload["install_id"]
            or not isinstance(hashes, dict)
            or files is None
            or set(hashes) != set(files)
            or not all(isinstance(hashes[name], str) for name in files)
        ):
            raise ValueError("invalid installer manifest")
        supplied_root = payload.get("skill_root")
        if not isinstance(supplied_root, str):
            raise ValueError("invalid installer root")
        if state_root_lock_key(supplied_root) != state_root_lock_key(root):
            raise ValueError("installer manifest root mismatch")
        if format_value == 3:
            required = payload.get("required_files")
            if required != list(_REQUIRED_FILES) or not set(_REQUIRED_FILES) <= set(
                files
            ):
                raise ValueError("required managed files are missing")
            if (
                payload.get("skill_root_key") != state_root_lock_key(root)
                or not isinstance(payload.get("package_version"), str)
                or not isinstance(payload.get("package_hash"), str)
                or payload["package_hash"]
                != self._package_hash(
                    str(payload["package_version"]),
                    {name: str(hashes[name]) for name in files},
                    _REQUIRED_FILES,
                )
            ):
                raise ValueError("invalid package identity")
        return payload

    def _verify_manifest_under_lease(
        self,
        lease: StateRootLease,
        target: Path,
        manifest: dict[str, object],
        *,
        require_hashes: bool,
    ) -> bool:
        root = lease.root
        hashes = manifest["hashes"]
        assert isinstance(hashes, dict)
        missing = False
        mismatch = False
        for relative in self._manifest_files(manifest) or ():
            path = target / relative
            lease_relative = path.relative_to(root)
            try:
                info = lease.stat(lease_relative)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
                ):
                    raise ValueError("managed entry is not a direct regular file")
                data = lease.read_bytes(
                    lease_relative, _MANAGED_FILE_LIMIT, "managed package file"
                )
            except FileNotFoundError:
                missing = True
                continue
            if hashlib.sha256(data).hexdigest() != hashes[relative]:
                mismatch = True
        if require_hashes and (missing or mismatch):
            raise ValueError("managed package hash mismatch")
        return not missing and not mismatch

    @staticmethod
    def _manifest_files(payload: object) -> tuple[str, ...] | None:
        if not isinstance(payload, dict):
            return None
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            return None
        files: list[str] = []
        for value in raw_files:
            if not isinstance(value, str):
                return None
            normalized = value.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                return None
            normalized = str(path)
            if normalized in files or normalized == _MANIFEST:
                return None
            files.append(normalized)
        return tuple(files)

    @staticmethod
    def _package_hash(
        version: str, hashes: dict[str, str], required: Sequence[str]
    ) -> str:
        raw = json.dumps(
            {"hashes": hashes, "required": list(required), "version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _directory_identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    def _write_status(self, target: Path, options: InstallOptions) -> Path:
        root = target.parent
        inspection = self._inspect_package(root, target)
        if inspection.state != "valid" or inspection.manifest is None:
            raise ValueError("cannot record status for an invalid package")
        manifest = inspection.manifest
        payload = {
            "capability": self._capability(options).value,
            "format": 3,
            "install_id": manifest["install_id"],
            "manifest_digest": self._manifest_digest(manifest),
            "managed_directory": str(target),
            "owner": _NAME,
            "package_hash": manifest["package_hash"],
            "package_version": manifest["package_version"],
            "skill_root": str(root),
            "skill_root_key": state_root_lock_key(root),
        }
        self._state_lease().write_bytes_atomic(
            _STATUS_RELATIVE,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        return self.state_paths.adapter_status_file(self.platform)

    def _read_status(self) -> dict[str, object] | None:
        lease = self._state_lease()
        if not lease.root_exists or not lease.available("adapters"):
            return None
        if not lease.exists(_STATUS_RELATIVE):
            return None
        raw = lease.read_bytes(_STATUS_RELATIVE, 16 * 1024, "adapter status")
        payload = json.loads(raw.decode("utf-8"))
        expected = {
            "capability",
            "format",
            "install_id",
            "manifest_digest",
            "managed_directory",
            "owner",
            "package_hash",
            "package_version",
            "skill_root",
            "skill_root_key",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("format") != 3
            or payload.get("owner") != _NAME
            or not all(
                isinstance(payload.get(name), str)
                for name in (
                    "capability",
                    "install_id",
                    "manifest_digest",
                    "managed_directory",
                    "package_hash",
                    "package_version",
                    "skill_root",
                    "skill_root_key",
                )
            )
        ):
            raise ValueError("invalid adapter status")
        if payload["capability"] not in {
            CapabilityLevel.MANUAL.value,
            CapabilityLevel.IMPLICIT.value,
        }:
            raise ValueError("invalid generic adapter capability")
        return payload

    def _remove_status(self) -> Path:
        lease = self._state_lease()
        if not lease.exists(_STATUS_RELATIVE):
            raise FileNotFoundError("adapter status is missing")
        lease.unlink(_STATUS_RELATIVE)
        return self.state_paths.adapter_status_file(self.platform)

    def _status_matches(
        self,
        status: dict[str, object],
        root: Path,
        manifest: dict[str, object] | None,
    ) -> bool:
        if manifest is None:
            return False
        target = root / _NAME
        try:
            return (
                status.get("owner") == _NAME
                and status.get("format") == 3
                and status.get("install_id") == manifest.get("install_id")
                and status.get("manifest_digest")
                == self._manifest_digest(manifest)
                and status.get("package_hash") == manifest.get("package_hash")
                and status.get("package_version")
                == manifest.get("package_version")
                and status.get("skill_root_key") == state_root_lock_key(root)
                and state_root_lock_key(str(status.get("skill_root")))
                == state_root_lock_key(root)
                and state_root_lock_key(
                    str(Path(str(status.get("managed_directory"))).parent)
                )
                == state_root_lock_key(root)
                and Path(str(status.get("managed_directory"))).name == target.name
            )
        except (OSError, ValueError):
            return False

    def _write_recovery(
        self,
        operation: str,
        root: Path,
        target: Path,
        quarantine: Path,
        paths: Sequence[Path],
        manifest: dict[str, object],
    ) -> Path:
        relative_files = [
            str(path.relative_to(target)).replace("\\", "/") for path in paths
        ]
        manifest_hashes = manifest["hashes"]
        assert isinstance(manifest_hashes, dict)
        file_hashes = {
            relative: (
                self._manifest_digest(manifest)
                if relative == _MANIFEST
                else str(manifest_hashes[relative])
            )
            for relative in relative_files
        }
        payload = {
            "file_hashes": file_hashes,
            "files": relative_files,
            "format": 2,
            "install_id": manifest["install_id"],
            "managed_directory": str(target),
            "manifest_digest": self._manifest_digest(manifest),
            "operation": operation,
            "owner": _NAME,
            "package_hash": manifest.get("package_hash", ""),
            "package_version": manifest.get("package_version", ""),
            "quarantine": str(quarantine),
            "quarantine_identity": list(
                self._directory_identity(os.stat(quarantine, follow_symlinks=False))
            ),
            "root_identity": list(
                self._directory_identity(os.stat(root, follow_symlinks=False))
            ),
            "skill_root": str(root),
            "skill_root_key": state_root_lock_key(root),
            "target_identity": list(
                self._directory_identity(os.stat(target, follow_symlinks=False))
            ),
        }
        self._state_lease().write_bytes_atomic(
            _RECOVERY_RELATIVE,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return self.state_paths.root / _RECOVERY_RELATIVE

    def _read_recovery(self) -> dict[str, object] | None:
        lease = self._state_lease()
        if not lease.root_exists or not lease.available("adapters"):
            return None
        if not lease.exists(_RECOVERY_RELATIVE):
            return None
        raw = lease.read_bytes(_RECOVERY_RELATIVE, 64 * 1024, "adapter recovery")
        payload = json.loads(raw.decode("utf-8"))
        expected = {
            "file_hashes",
            "files",
            "format",
            "install_id",
            "managed_directory",
            "manifest_digest",
            "operation",
            "owner",
            "package_hash",
            "package_version",
            "quarantine",
            "quarantine_identity",
            "root_identity",
            "skill_root",
            "skill_root_key",
            "target_identity",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("format") != 2
            or payload.get("owner") != _NAME
            or payload.get("operation") not in {"install", "upgrade", "uninstall"}
        ):
            raise ValueError("invalid adapter recovery record")
        string_fields = (
            "install_id",
            "managed_directory",
            "manifest_digest",
            "package_hash",
            "package_version",
            "quarantine",
            "skill_root",
            "skill_root_key",
        )
        identities = (
            payload.get("root_identity"),
            payload.get("target_identity"),
            payload.get("quarantine_identity"),
        )
        files = self._recovery_files(payload.get("files"))
        hashes = payload.get("file_hashes")
        if (
            not all(isinstance(payload.get(name), str) for name in string_fields)
            or not all(
                isinstance(identity, list)
                and len(identity) == 2
                and all(isinstance(value, int) for value in identity)
                for identity in identities
            )
            or files is None
            or not isinstance(hashes, dict)
            or set(hashes) != set(files)
            or not all(
                isinstance(hashes.get(relative), str)
                and len(str(hashes[relative])) == 64
                for relative in files
            )
        ):
            raise ValueError("invalid adapter recovery record")
        root = Path(str(payload["skill_root"]))
        target = Path(str(payload["managed_directory"]))
        quarantine = Path(str(payload["quarantine"]))
        if (
            state_root_lock_key(root) != payload["skill_root_key"]
            or state_root_lock_key(target.parent) != payload["skill_root_key"]
            or target.name != _NAME
            or state_root_lock_key(quarantine.parent) != payload["skill_root_key"]
            or not quarantine.name.startswith(
                (
                    f".{_NAME}.quarantine-",
                    f".{_NAME}.staging-",
                )
            )
        ):
            raise ValueError("invalid adapter recovery paths")
        return payload

    def _remove_recovery(self, *, missing_ok: bool = False) -> None:
        self._state_lease().unlink(_RECOVERY_RELATIVE, missing_ok=missing_ok)

    def _recover_pending(self, expected_root: Path | None) -> None:
        recovery = self._read_recovery()
        if recovery is None:
            return
        root = self._safe_skill_root(Path(str(recovery["skill_root"])))
        if (
            expected_root is not None
            and state_root_lock_key(expected_root) != state_root_lock_key(root)
        ):
            raise ValueError("recovery belongs to a different skill root")
        self._configured_skill_root = root
        target = root / _NAME
        quarantine = Path(str(recovery["quarantine"]))
        files = self._recovery_files(recovery["files"])
        assert files is not None
        target_paths = tuple(target / relative for relative in files)
        quarantine_paths = tuple(quarantine / relative for relative in files)
        retained = set(self._parent_directories(root, target_paths))
        retained.update(self._parent_directories(root, quarantine_paths))
        retained.discard(Path("."))
        inspection = self._inspect_package(root, target, allow_hash_failure=True)
        try:
            status = self._read_status()
        except Exception:
            status = None
        finalize = (
            recovery["operation"] == "uninstall"
            and status is None
        ) or (
            recovery["operation"] == "upgrade"
            and inspection.state == "valid"
            and inspection.manifest is not None
            and inspection.manifest.get("install_id") != recovery["install_id"]
        ) or (
            recovery["operation"] == "install"
            and inspection.state == "valid"
            and inspection.manifest is not None
            and inspection.manifest.get("install_id") == recovery["install_id"]
        )
        with guard_state_root(
            root,
            retained_dirs=tuple(
                sorted(retained, key=lambda path: (len(path.parts), str(path)))
            ),
        ) as lease:
            if list(self._directory_identity(lease.stat("."))) != recovery[
                "root_identity"
            ]:
                raise OSError("recovery root identity changed")
            if list(self._directory_identity(lease.stat(_NAME))) != recovery[
                "target_identity"
            ]:
                raise OSError("recovery target identity changed")
            quarantine_relative = quarantine.relative_to(root)
            if list(
                self._directory_identity(lease.stat(quarantine_relative))
            ) != recovery["quarantine_identity"]:
                raise OSError("recovery quarantine identity changed")
            if finalize:
                changed = self._discard_recovery_files(
                    lease, root, quarantine_paths, recovery
                )
            elif recovery["operation"] == "install":
                changed = self._complete_recovery_install(
                    lease,
                    root,
                    target_paths,
                    quarantine_paths,
                    recovery,
                )
            else:
                changed = self._restore_recovery_files(
                    lease,
                    root,
                    target_paths,
                    quarantine_paths,
                    recovery,
                )
        self._remove_recovery()
        self._recovery_changes = self._dedupe_paths(
            changed, (self.state_paths.root / _RECOVERY_RELATIVE,)
        )

    def _complete_recovery_install(
        self,
        lease: StateRootLease,
        root: Path,
        target_paths: Sequence[Path],
        staging_paths: Sequence[Path],
        recovery: dict[str, object],
    ) -> tuple[Path, ...]:
        actions: list[tuple[str, Path, Path]] = []
        for target_path, staging_path in zip(
            target_paths, staging_paths, strict=True
        ):
            target_relative = target_path.relative_to(root)
            staging_relative = staging_path.relative_to(root)
            expected = self._recovery_expected_hash(
                staging_path, staging_paths, recovery
            )
            target_exists = lease.exists(target_relative)
            staging_exists = lease.exists(staging_relative)
            if target_exists and self._lease_file_hash(
                lease, target_relative
            ) != expected:
                raise ValueError("install recovery found an unknown destination")
            if staging_exists and self._lease_file_hash(
                lease, staging_relative
            ) != expected:
                raise ValueError("install recovery staging was modified")
            if target_exists and staging_exists:
                actions.append(("unlink", staging_path, target_path))
            elif staging_exists:
                actions.append(("publish", staging_path, target_path))
            elif not target_exists:
                raise ValueError("install recovery file is missing")
        changed: list[Path] = []
        for action, staging_path, target_path in actions:
            target_relative = target_path.relative_to(root)
            staging_relative = staging_path.relative_to(root)
            if action == "unlink":
                lease.unlink(staging_relative)
                changed.append(staging_path)
            else:
                lease.publish_no_replace(staging_relative, target_relative)
                changed.extend((staging_path, target_path))
        return tuple(changed)

    @staticmethod
    def _recovery_files(payload: object) -> tuple[str, ...] | None:
        if not isinstance(payload, list) or not payload:
            return None
        files: list[str] = []
        for value in payload:
            if not isinstance(value, str):
                return None
            normalized = value.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or normalized in files
            ):
                return None
            files.append(normalized)
        if _MANIFEST not in files:
            return None
        return tuple(files)

    def _discard_recovery_files(
        self,
        lease: StateRootLease,
        root: Path,
        quarantine_paths: Sequence[Path],
        recovery: dict[str, object],
    ) -> tuple[Path, ...]:
        present: list[tuple[Path, Path]] = []
        for path in quarantine_paths:
            relative = path.relative_to(root)
            if not lease.exists(relative):
                continue
            expected = self._recovery_expected_hash(path, quarantine_paths, recovery)
            if self._lease_file_hash(lease, relative) != expected:
                raise ValueError("recovery quarantine contains an unknown file")
            present.append((path, relative))
        changed: list[Path] = []
        for path, relative in present:
            lease.unlink(relative)
            changed.append(path)
        return tuple(changed)

    def _restore_recovery_files(
        self,
        lease: StateRootLease,
        root: Path,
        target_paths: Sequence[Path],
        quarantine_paths: Sequence[Path],
        recovery: dict[str, object],
    ) -> tuple[Path, ...]:
        actions: list[tuple[str, Path, Path]] = []
        for target_path, quarantine_path in zip(
            target_paths, quarantine_paths, strict=True
        ):
            target_relative = target_path.relative_to(root)
            quarantine_relative = quarantine_path.relative_to(root)
            expected = self._recovery_expected_hash(
                quarantine_path, quarantine_paths, recovery
            )
            target_exists = lease.exists(target_relative)
            quarantine_exists = lease.exists(quarantine_relative)
            if target_exists and self._lease_file_hash(
                lease, target_relative
            ) != expected:
                raise ValueError("recovery would overwrite an unknown file")
            if quarantine_exists and self._lease_file_hash(
                lease, quarantine_relative
            ) != expected:
                raise ValueError("recovery quarantine contains an unknown file")
            if target_exists and quarantine_exists:
                actions.append(("unlink", quarantine_path, target_path))
            elif quarantine_exists:
                actions.append(("publish", quarantine_path, target_path))
            elif not target_exists:
                raise ValueError("recovery file is missing from both locations")
        changed: list[Path] = []
        for action, quarantine_path, target_path in actions:
            target_relative = target_path.relative_to(root)
            quarantine_relative = quarantine_path.relative_to(root)
            if action == "unlink":
                lease.unlink(quarantine_relative)
                changed.append(quarantine_path)
            else:
                lease.publish_no_replace(quarantine_relative, target_relative)
                changed.extend((quarantine_path, target_path))
        return tuple(changed)

    @staticmethod
    def _recovery_expected_hash(
        path: Path,
        quarantine_paths: Sequence[Path],
        recovery: dict[str, object],
    ) -> str:
        quarantine_root = Path(str(recovery["quarantine"]))
        relative = str(path.relative_to(quarantine_root)).replace("\\", "/")
        hashes = recovery["file_hashes"]
        assert isinstance(hashes, dict)
        return str(hashes[relative])

    @staticmethod
    def _lease_file_hash(lease: StateRootLease, relative: Path) -> str:
        return hashlib.sha256(
            lease.read_bytes(relative, _MANAGED_FILE_LIMIT, "recovery file")
        ).hexdigest()

    def _prepare_quarantine(
        self, quarantine: Path, target: Path, paths: Sequence[Path]
    ) -> None:
        quarantine.mkdir(mode=0o700)
        for path in paths:
            (quarantine / path.relative_to(target)).parent.mkdir(
                parents=True, exist_ok=True
            )

    def _prepare_target_directories(
        self, target: Path, manifest: dict[str, object]
    ) -> tuple[Path, ...]:
        before = {path for path in target.rglob("*") if path.is_dir()}
        for relative in self._manifest_files(manifest) or ():
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
        after = {path for path in target.rglob("*") if path.is_dir()}
        return tuple(
            sorted(after - before, key=lambda path: (len(path.parts), str(path)))
        )

    def _quarantine_managed_files(
        self,
        target: Path,
        quarantine: Path | None = None,
        *,
        allow_missing: bool = False,
    ) -> tuple[Path, ...]:
        if quarantine is None:
            raise ValueError("quarantine directory is required")
        lease = self._skill_lease()
        moved: list[Path] = []
        try:
            for path in self._active_moved:
                relative = path.relative_to(self._active_skill_root)
                if not lease.exists(relative):
                    if allow_missing:
                        continue
                    raise FileNotFoundError(path)
                destination = quarantine / path.relative_to(target)
                lease.publish_no_replace(
                    relative, destination.relative_to(self._active_skill_root)
                )
                moved.append(path)
        except Exception:
            self._restore_quarantine(quarantine, target, tuple(moved))
            raise
        return tuple(moved)

    def _restore_quarantine(
        self, quarantine: Path, target: Path, moved: tuple[Path, ...]
    ) -> None:
        lease = self._skill_lease()
        root = self._active_skill_root
        assert root is not None
        for path in reversed(moved):
            try:
                source = quarantine / path.relative_to(target)
                if lease.exists(source.relative_to(root)):
                    lease.publish_no_replace(
                        source.relative_to(root), path.relative_to(root)
                    )
            except OSError:
                continue

    def _discard_quarantine(self, quarantine: Path) -> None:
        lease = self._skill_lease()
        root = self._active_skill_root
        assert root is not None
        target = root / _NAME
        for path in self._active_moved:
            candidate = quarantine / path.relative_to(target)
            lease.unlink(candidate.relative_to(root), missing_ok=True)

    def _skill_lease(self) -> StateRootLease:
        if self._active_skill_lease is None:
            raise RuntimeError("skill transaction is not bound")
        return self._active_skill_lease

    def _restoration_result(
        self,
        root: Path,
        target: Path,
        quarantine: Path,
        moved: tuple[Path, ...],
        message: str,
        additional_changed: Sequence[Path] = (),
    ) -> AdapterResult:
        changed = self._changed_after_failed_restore(target, quarantine, moved)
        if not changed:
            self._cleanup_quarantine_dirs(root, quarantine, moved)
            try:
                self._remove_recovery(missing_ok=True)
            except Exception:
                changed = (self.state_paths.root / _RECOVERY_RELATIVE,)
        if changed:
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                (
                    message,
                    "recovery is recorded; run doctor before retrying",
                ),
                self._dedupe_paths(
                    changed,
                    additional_changed,
                    (self.state_paths.root / _RECOVERY_RELATIVE,),
                ),
            )
        return self._failed(
            f"{message}; files restored", self._dedupe_paths(additional_changed)
        )

    @staticmethod
    def _changed_after_failed_restore(
        target: Path, quarantine: Path, moved: Sequence[Path]
    ) -> tuple[Path, ...]:
        changed: set[Path] = set()
        for path in moved:
            if not path.exists():
                changed.add(path)
            candidate = quarantine / path.relative_to(target)
            if candidate.exists():
                changed.add(candidate)
                for parent in candidate.parents:
                    if parent == quarantine or quarantine in parent.parents:
                        changed.add(parent)
                    if parent == quarantine:
                        break
        return tuple(sorted(changed, key=lambda path: (len(path.parts), str(path))))

    @staticmethod
    def _quarantine_changed_paths(
        quarantine: Path, target: Path, moved: Sequence[Path]
    ) -> tuple[Path, ...]:
        return tuple(
            quarantine / path.relative_to(target)
            for path in moved
            if (quarantine / path.relative_to(target)).exists()
        )

    @staticmethod
    def _cleanup_quarantine_dirs(
        root: Path, quarantine: Path, paths: Sequence[Path]
    ) -> tuple[Path, ...]:
        # Empty transaction directories are intentionally retained. Reopening
        # their live path after releasing the retained authority would create a
        # directory-swap deletion primitive.
        return ()

    def _discard_staging_package(
        self,
        root: Path,
        staging: Path,
        manifest: dict[str, object],
        expected_identity: tuple[int, int] | None,
    ) -> tuple[Path, ...]:
        paths = self._manifest_paths(staging, manifest)
        retained = set(self._parent_directories(root, paths))
        retained.discard(Path("."))
        with guard_state_root(
            root,
            retained_dirs=tuple(
                sorted(retained, key=lambda path: (len(path.parts), str(path)))
            ),
        ) as lease:
            if (
                expected_identity is None
                or self._directory_identity(lease.stat(staging.name))
                != expected_identity
            ):
                raise OSError("staging identity changed before cleanup")
            self._verify_manifest_under_lease(
                lease, staging, manifest, require_hashes=True
            )
            for path in paths:
                lease.unlink(path.relative_to(root))
        return self._staging_directories(staging, manifest)

    @staticmethod
    def _staging_directories(
        staging: Path, manifest: dict[str, object]
    ) -> tuple[Path, ...]:
        directories = {staging}
        for relative in GenericAdapter._manifest_files(manifest) or ():
            parent = staging / Path(relative).parent
            while parent != staging.parent:
                directories.add(parent)
                if parent == staging:
                    break
                parent = parent.parent
        return tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        )

    @staticmethod
    def _manifest_paths(target: Path, manifest: dict[str, object]) -> tuple[Path, ...]:
        files = GenericAdapter._manifest_files(manifest)
        if files is None:
            raise ValueError("invalid installer manifest")
        return tuple(target / relative for relative in files) + (target / _MANIFEST,)

    @staticmethod
    def _manifest_parent_directories(
        target_name: str, manifest: dict[str, object]
    ) -> tuple[Path, ...]:
        files = GenericAdapter._manifest_files(manifest) or ()
        retained = {Path(target_name)}
        for relative in files:
            parent = Path(target_name) / Path(relative).parent
            while parent != Path("."):
                retained.add(parent)
                if parent == Path(target_name):
                    break
                parent = parent.parent
        return tuple(sorted(retained, key=lambda path: (len(path.parts), str(path))))

    @staticmethod
    def _parent_directories(root: Path, paths: Sequence[Path]) -> tuple[Path, ...]:
        retained: set[Path] = set()
        for path in paths:
            relative_parent = path.relative_to(root).parent
            while relative_parent != Path("."):
                retained.add(relative_parent)
                relative_parent = relative_parent.parent
        return tuple(retained)

    @staticmethod
    def _package_changed_paths(
        target: Path, manifest: dict[str, object]
    ) -> tuple[Path, ...]:
        paths = GenericAdapter._manifest_paths(target, manifest)
        directories = {target}
        for path in paths:
            for parent in path.parents:
                if target in (parent, *parent.parents):
                    directories.add(parent)
                if parent == target:
                    break
        return (
            tuple(sorted(directories, key=lambda path: (len(path.parts), str(path))))
            + paths
        )

    @staticmethod
    def _manifest_digest(manifest: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

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
        if not self._recovery_changes:
            return result
        return AdapterResult(
            result.platform,
            result.status,
            result.capability,
            result.messages,
            self._dedupe_paths(self._recovery_changes, result.changed_paths),
        )

    @staticmethod
    def _inspection_message(state: str) -> str:
        return {
            "valid": "valid",
            "hash-mismatch": "hash mismatch",
            "missing": "missing",
            "legacy": "upgrade required",
            "alias-invalid": "unsafe path",
            "manifest-invalid": "manifest invalid",
            "invalid": "invalid",
        }.get(state, "invalid")

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

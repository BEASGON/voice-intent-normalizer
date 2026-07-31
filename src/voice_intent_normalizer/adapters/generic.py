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
from pathlib import Path

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

    def detect(self) -> AdapterResult:
        return self.doctor()

    def install(self, options: InstallOptions) -> AdapterResult:
        try:
            with self._state_operation(create=True):
                return self._install_locked(options)
        except Exception:
            return self._failed("generic installation was not completed")

    def _install_locked(self, options: InstallOptions) -> AdapterResult:
        if options.output_dir is None:
            return self._failed("a skill root is required")
        root = self._safe_skill_root(options.output_dir)
        self._configured_skill_root = root
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
                root, target, staging, staged.manifest, options
            )
        except Exception:
            self._remove_staging(staging)
            raise

    def doctor(self) -> AdapterResult:
        try:
            with self._state_operation(create=False, lock_missing=False):
                return self._doctor_locked()
        except Exception:
            return self._failed("shared adapter status is unavailable")

    def _doctor_locked(self) -> AdapterResult:
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
        try:
            with self._state_operation(create=True):
                return self._uninstall_locked(options)
        except Exception:
            return self._failed("generic uninstall was not completed")

    def _uninstall_locked(self, options: UninstallOptions) -> AdapterResult:
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
                            changed + (status_path, recovery_path),
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
                    changed + (recovery_path,),
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

        removed_dirs = self._remove_known_empty_dirs(root, target, paths)
        quarantine_dirs = self._cleanup_quarantine_dirs(root, quarantine, paths)
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
                paths
                + removed_dirs
                + quarantine_dirs
                + ((status_path,) if status_path is not None else ())
                + (recovery_path,),
            )
        assert status_path is not None
        return AdapterResult(
            self.platform,
            "uninstalled",
            CapabilityLevel.MANUAL,
            ("shared personal and project data were preserved",),
            paths + removed_dirs + (status_path,),
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
            lease.replace(staging.name, _NAME)
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
            changed + (status_path,),
        )

    def _repair_or_reuse(
        self,
        root: Path,
        target: Path,
        staging: Path,
        desired: dict[str, object],
        options: InstallOptions,
    ) -> AdapterResult:
        current = self._inspect_package(root, target, allow_hash_failure=True)
        if current.manifest is None or current.state in {
            "missing",
            "manifest-invalid",
            "alias-invalid",
        }:
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
                self._remove_staging(staging)
                return AdapterResult(
                    self.platform,
                    "already-installed",
                    self._capability(options),
                    ("managed skill files are already installed",),
                )
            status_path = self._write_status(target, options)
            self._remove_staging(staging)
            return AdapterResult(
                self.platform,
                "repaired",
                self._capability(options),
                ("adapter status and capability were rebuilt",),
                (status_path,),
            )
        return self._upgrade_package(root, target, staging, current, desired, options)

    def _upgrade_package(
        self,
        root: Path,
        target: Path,
        staging: Path,
        current: _Inspection,
        desired: dict[str, object],
        options: InstallOptions,
    ) -> AdapterResult:
        assert current.manifest is not None
        old_paths = self._manifest_paths(target, current.manifest)
        new_paths = self._manifest_paths(target, desired)
        old_relatives = {path.relative_to(target) for path in old_paths}
        for path in new_paths:
            if path.exists() and path.relative_to(target) not in old_relatives:
                return self._failed("managed upgrade would overwrite an unknown file")

        created_dirs = self._prepare_target_directories(target, desired)
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
                        lease.replace(
                            source.relative_to(root), destination.relative_to(root)
                        )
                        moved_new.append(destination)
                except Exception:
                    for destination, source in reversed(
                        list(zip(moved_new, staged_paths, strict=False))
                    ):
                        if destination.exists():
                            lease.replace(
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
                            ),
                            tuple(new_paths) + created_dirs + (recovery_path,),
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
                            ),
                            tuple(new_paths)
                            + created_dirs
                            + self._quarantine_changed_paths(
                                quarantine, target, moved_old
                            )
                            + (status_path, recovery_path),
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
            self._remove_staging(staging)
            return self._restoration_result(
                root,
                target,
                quarantine,
                moved_old,
                move_failure,
            )

        self._remove_staging(staging)
        self._cleanup_quarantine_dirs(root, quarantine, old_paths)
        self._remove_recovery(missing_ok=True)
        operation = (
            "upgraded"
            if current.state == "valid"
            and current.manifest.get("package_version")
            != desired.get("package_version")
            else "repaired"
        )
        return AdapterResult(
            self.platform,
            operation,
            self._capability(options),
            ("managed package integrity and adapter status were refreshed",),
            tuple(new_paths) + created_dirs + (status_path,),
        )

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
        self._copy_python_tree(
            self.repository / "src" / "voice_intent_normalizer",
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
                    return _Inspection("manifest-invalid")
            return _Inspection("hash-mismatch")
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
                    return _Inspection("manifest-invalid")
            return _Inspection("hash-mismatch")
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
            path = Path(value)
            if (
                path.anchor
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                return None
            normalized = str(path).replace("\\", "/")
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
            "format": 2,
            "install_id": manifest["install_id"],
            "managed_directory": str(target),
            "owner": _NAME,
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
            "managed_directory",
            "owner",
            "skill_root",
            "skill_root_key",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("format") != 2
            or payload.get("owner") != _NAME
            or not all(
                isinstance(payload.get(name), str)
                for name in (
                    "capability",
                    "install_id",
                    "managed_directory",
                    "skill_root",
                    "skill_root_key",
                )
            )
        ):
            raise ValueError("invalid adapter status")
        CapabilityLevel(str(payload["capability"]))
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
                and status.get("format") == 2
                and status.get("install_id") == manifest.get("install_id")
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
        payload = {
            "files": [
                str(path.relative_to(target)).replace("\\", "/") for path in paths
            ],
            "format": 1,
            "install_id": manifest["install_id"],
            "managed_directory": str(target),
            "operation": operation,
            "owner": _NAME,
            "quarantine": str(quarantine),
            "skill_root": str(root),
            "skill_root_key": state_root_lock_key(root),
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
        if (
            not isinstance(payload, dict)
            or payload.get("format") != 1
            or payload.get("owner") != _NAME
        ):
            raise ValueError("invalid adapter recovery record")
        return payload

    def _remove_recovery(self, *, missing_ok: bool = False) -> None:
        self._state_lease().unlink(_RECOVERY_RELATIVE, missing_ok=missing_ok)

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
                lease.replace(
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
                    lease.replace(source.relative_to(root), path.relative_to(root))
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
                changed + (self.state_paths.root / _RECOVERY_RELATIVE,),
            )
        return self._failed(f"{message}; files restored")

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

    def _cleanup_quarantine_dirs(
        self, root: Path, quarantine: Path, paths: Sequence[Path]
    ) -> tuple[Path, ...]:
        directories = {
            quarantine,
            *(
                parent
                for path in paths
                for parent in (quarantine / path.relative_to(root / _NAME)).parents
                if quarantine in (parent, *parent.parents)
            ),
        }
        return self._remove_directories(root, directories)

    def _remove_known_empty_dirs(
        self, root: Path, target: Path, paths: Sequence[Path]
    ) -> tuple[Path, ...]:
        directories = {
            target,
            *(
                parent
                for path in paths
                for parent in path.parents
                if target in (parent, *parent.parents)
            ),
        }
        return self._remove_directories(root, directories)

    @staticmethod
    def _remove_directories(root: Path, directories: set[Path]) -> tuple[Path, ...]:
        removed: list[Path] = []
        for directory in sorted(
            directories, key=lambda path: (len(path.parts), str(path)), reverse=True
        ):
            if directory == root:
                continue
            try:
                relative = directory.relative_to(root)
                parent = relative.parent
                retained = () if parent == Path(".") else (parent,)
                with guard_state_root(root, retained_dirs=retained) as lease:
                    lease.rmdir(relative)
                removed.append(directory)
            except (FileNotFoundError, OSError, ValueError):
                continue
        return tuple(removed)

    @staticmethod
    def _remove_staging(staging: Path) -> None:
        if not staging.exists() or staging.is_symlink():
            return
        for current, directories, files in os.walk(
            staging, topdown=False, followlinks=False
        ):
            current_path = Path(current)
            for name in files:
                path = current_path / name
                try:
                    info = os.lstat(path)
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        path.unlink()
                except OSError:
                    continue
            for name in directories:
                path = current_path / name
                try:
                    info = os.lstat(path)
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        path.rmdir()
                except OSError:
                    continue
        try:
            staging.rmdir()
        except OSError:
            return

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

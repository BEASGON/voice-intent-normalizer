"""Safe standards-only installation of the portable skill package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..paths import (
    StatePaths,
    StateRootLease,
    guard_state_root,
    state_root_lock_key,
    validate_state_root,
)
from ..updater import _retained_lease_update_lock
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions
from .generic_contract import (
    LAYOUT_NAME,
    STATUS_FORMAT,
    StatusV5,
    canonical_json_bytes,
    manifest_digest,
    validate_manifest,
    validate_status_v5,
)
from .generic_layout import (
    VersionedArtifact,
    VersionedArtifacts,
    generic_layout_paths,
    prepare_versioned_artifacts,
)

_MANIFEST = ".voice-intent-normalizer-install.json"
_NAME = "voice-intent-normalizer"
_GENERIC_RELATIVE = Path("adapters") / "generic"
_STATUS_RELATIVE = _GENERIC_RELATIVE / "status.json"
_TRANSACTION_RELATIVE = _GENERIC_RELATIVE / "transaction.json"
_GENERATIONS_RELATIVE = _GENERIC_RELATIVE / "generations"
_STAGING_RELATIVE = _GENERIC_RELATIVE / "staging"
_RETIRED_RELATIVE = _GENERIC_RELATIVE / "retired"
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
_MANIFEST_MAX_ENTRIES = 4096
_MANIFEST_MAX_PATH_LENGTH = 512
_MANIFEST_MAX_DEPTH = 32
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
        self._recovery_changes: tuple[Path, ...] = ()
        self._unanchored_recovery = False
        self._recovered_uninstall = False
        self._change_events: list[Path] = []

    def detect(self) -> AdapterResult:
        return self.doctor()

    def install(self, options: InstallOptions) -> AdapterResult:
        self._recovery_changes = ()
        self._change_events = []
        if options.strict:
            return self._failed(
                "strict installation is unavailable for the generic adapter"
            )
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
        status = self._read_versioned_status(root)
        transaction = self._read_first_install_transaction(root)
        if status is not None:
            return self._reuse_versioned_install(
                root, status, transaction, options
            )

        recovering = transaction is not None
        if not recovering and self._direct_entry_exists(root / _NAME):
            return self._failed(
                "existing skill capsule is not anchored by a first-install transaction"
            )
        if recovering:
            generation_id = str(transaction["status"]["active"]["generation_id"])
            generation_nonce = generation_id[-32:]
            transaction_id = str(transaction["transaction_id"])
        else:
            generation_nonce = secrets.token_hex(16)
            transaction_id = f"t-{secrets.token_hex(16)}"
        artifacts = self._prepare_versioned_artifacts(generation_nonce)
        status_payload = self._versioned_status_payload(artifacts, options)
        if recovering:
            self._validate_first_install_transaction(
                transaction, root, status_payload
            )
        else:
            transaction = self._begin_first_install_transaction(
                root, transaction_id, status_payload
            )

        published = False

        def mark_generation_published() -> None:
            nonlocal published
            published = True

        try:
            generation = self._stage_and_publish_generation(
                artifacts.generation,
                recovering=recovering,
                on_published=mark_generation_published,
            )
            published = True
            capsule = self._ensure_capsule(
                root,
                artifacts.capsule,
                transaction_id,
                recovering=recovering,
            )
            self._validate_generation_directory(
                self.state_paths.root,
                generation.relative_to(self.state_paths.root),
                artifacts.generation,
            )
            self._validate_capsule_directory(
                root, capsule.relative_to(root), artifacts.capsule
            )
            self._smoke_generation(capsule, generation)
            self._activate_generation(status_payload)
            self._remove_first_install_transaction()
            return self._verified_result("installed", root, artifacts, options)
        except Exception:
            if not published:
                self._remove_first_install_transaction(missing_ok=True)
            return self._failed(
                "generic installation was not completed",
                tuple(self._change_events),
            )

    def _prepare_versioned_artifacts(
        self, generation_nonce: str
    ) -> VersionedArtifacts:
        return prepare_versioned_artifacts(self.repository, generation_nonce)

    def _versioned_status_payload(
        self, artifacts: VersionedArtifacts, options: InstallOptions
    ) -> dict[str, object]:
        return {
            "format": STATUS_FORMAT,
            "layout": LAYOUT_NAME,
            "capability": self._capability(options).value,
            "capsule": {
                "protocol": 1,
                "manifest_digest": artifacts.capsule.manifest_digest,
                "package_hash": artifacts.capsule.package_hash,
            },
            "active": {
                "generation_id": artifacts.generation.identifier,
                "manifest_digest": artifacts.generation.manifest_digest,
                "package_hash": artifacts.generation.package_hash,
                "package_version": artifacts.generation.package_version,
            },
            "previous": None,
            "transaction": None,
        }

    def _begin_first_install_transaction(
        self,
        root: Path,
        transaction_id: str,
        status_payload: dict[str, object],
    ) -> dict[str, object]:
        payload = {
            "format": 1,
            "transaction_id": transaction_id,
            "skill_root_key": state_root_lock_key(root),
            "status": status_payload,
        }
        self._state_lease().write_bytes_atomic(
            _TRANSACTION_RELATIVE, canonical_json_bytes(payload)
        )
        self._state_lease().fsync_directory(_GENERIC_RELATIVE)
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))
        return payload

    def _read_first_install_transaction(
        self, root: Path
    ) -> dict[str, object] | None:
        lease = self._state_lease()
        if not lease.exists(_TRANSACTION_RELATIVE):
            return None
        raw = lease.read_bytes(
            _TRANSACTION_RELATIVE, _MANIFEST_LIMIT, "adapter transaction"
        )
        try:
            payload = json.loads(
                raw,
                object_pairs_hook=self._unique_json_object,
                parse_constant=self._reject_json_constant,
            )
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid first-install transaction") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid first-install transaction")
        status_payload = payload.get("status")
        self._validate_first_install_transaction(payload, root, status_payload)
        return payload

    def _validate_first_install_transaction(
        self,
        payload: object,
        root: Path,
        expected_status: object,
    ) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"format", "transaction_id", "skill_root_key", "status"}
            or payload.get("format") != 1
            or type(payload.get("format")) is not int
            or not isinstance(payload.get("transaction_id"), str)
            or re.fullmatch(r"t-[0-9a-f]{32}", str(payload["transaction_id"]))
            is None
            or payload.get("skill_root_key") != state_root_lock_key(root)
            or payload.get("status") != expected_status
        ):
            raise ValueError("invalid first-install transaction")
        validate_status_v5(
            expected_status,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def _read_versioned_status(self, root: Path) -> StatusV5 | None:
        lease = self._state_lease()
        if not lease.exists(_STATUS_RELATIVE):
            return None
        raw = lease.read_bytes(_STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status")
        return validate_status_v5(
            raw,
            skill_root=root,
            generations_root=generic_layout_paths(self.state_paths).generations,
        )

    def _reuse_versioned_install(
        self,
        root: Path,
        status: StatusV5,
        transaction: dict[str, object] | None,
        options: InstallOptions,
    ) -> AdapterResult:
        artifacts = self._prepare_versioned_artifacts(
            status.active.generation_id[-32:]
        )
        if (
            status.capability != self._capability(options).value
            or status.capsule.manifest_digest
            != artifacts.capsule.manifest_digest
            or status.capsule.package_hash != artifacts.capsule.package_hash
            or status.active.generation_id != artifacts.generation.identifier
            or status.active.manifest_digest
            != artifacts.generation.manifest_digest
            or status.active.package_hash != artifacts.generation.package_hash
            or status.active.package_version != artifacts.generation.package_version
            or status.previous is not None
            or status.transaction_id is not None
        ):
            return self._failed(
                "the installed generic package requires lifecycle recovery or upgrade"
            )
        self._validate_capsule_directory(
            root, Path(_NAME), artifacts.capsule
        )
        generation_relative = _GENERATIONS_RELATIVE / status.active.generation_id
        self._validate_generation_directory(
            self.state_paths.root,
            generation_relative,
            artifacts.generation,
        )
        if transaction is not None:
            expected = self._versioned_status_payload(artifacts, options)
            self._validate_first_install_transaction(transaction, root, expected)
            self._remove_first_install_transaction()
            return self._verified_result("installed", root, artifacts, options)
        return AdapterResult(
            self.platform,
            "already-installed",
            self._capability(options),
            ("managed immutable generation is already active",),
        )

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
        stage_relative = _STAGING_RELATIVE / f".{artifact.identifier}.staging"
        self._stage_and_publish_artifact(
            self.state_paths.root,
            stage_relative,
            final_relative,
            artifact,
            on_published=on_published,
        )
        self._validate_generation_directory(
            self.state_paths.root, final_relative, artifact
        )
        self._record_changes(self._artifact_changed_paths(final, artifact))
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
        stage_relative = Path(f".{_NAME}.staging-{transaction_id[2:]}")
        self._stage_and_publish_artifact(
            root, stage_relative, Path(_NAME), artifact
        )
        self._validate_capsule_directory(root, Path(_NAME), artifact)
        self._record_changes(self._artifact_changed_paths(target, artifact))
        return target

    @staticmethod
    def _direct_entry_exists(path: Path) -> bool:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        return True

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
            publication_parents = {
                parent
                for parent in (stage_relative.parent, final_relative.parent)
                if parent != Path(".")
            }
            with guard_state_root(
                root,
                retained_dirs=tuple(
                    sorted(
                        publication_parents,
                        key=lambda path: (len(path.parts), str(path)),
                    )
                ),
            ) as lease:
                lease.publish_directory_no_replace(
                    stage_relative, final_relative, expected_identity
                )
                if on_published is not None:
                    on_published()
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
                if (
                    self._directory_identity(lease.stat(stage_relative))
                    != expected_identity
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
        manifest_bytes = artifact.files[artifact.manifest_name]
        manifest = validate_manifest(manifest_bytes)
        if (
            canonical_json_bytes(manifest) != manifest_bytes
            or manifest["kind"] != artifact.kind
            or manifest["identifier"] != artifact.identifier
            or manifest["package_hash"] != artifact.package_hash
            or manifest["package_version"] != artifact.package_version
        ):
            raise ValueError("published artifact manifest is invalid")

    @staticmethod
    def _artifact_directories(
        root: Path, artifact: VersionedArtifact
    ) -> tuple[Path, ...]:
        directories = {root}
        for name in artifact.files:
            parent = root / Path(name).parent
            while parent != root:
                directories.add(parent)
                parent = parent.parent
        return tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        )

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

    def _smoke_generation(self, capsule: Path, generation: Path) -> None:
        if not (capsule / "capsule.json").is_file() or not (
            generation / "generation.json"
        ).is_file():
            raise ValueError("published runtime is incomplete")
        skill_metadata = (capsule / "SKILL.md").read_text(encoding="utf-8")
        if "name: voice-intent-normalizer" not in skill_metadata:
            raise ValueError("published capsule metadata is invalid")
        with tempfile.TemporaryDirectory(prefix="voice-intent-smoke-") as sandbox:
            sandbox_root = Path(sandbox)
            state = sandbox_root / "state"
            skill_root = sandbox_root / "skills"
            smoke_capsule = skill_root / _NAME
            generation_manifest = validate_manifest(
                (generation / "generation.json").read_bytes()
            )
            capsule_manifest = validate_manifest(
                (capsule / "capsule.json").read_bytes()
            )
            smoke_generation = (
                state
                / _GENERATIONS_RELATIVE
                / str(generation_manifest["identifier"])
            )
            shutil.copytree(capsule, smoke_capsule)
            shutil.copytree(generation, smoke_generation)
            smoke_status = {
                "format": STATUS_FORMAT,
                "layout": LAYOUT_NAME,
                "capability": CapabilityLevel.MANUAL.value,
                "capsule": {
                    "protocol": 1,
                    "manifest_digest": manifest_digest(capsule_manifest),
                    "package_hash": capsule_manifest["package_hash"],
                },
                "active": {
                    "generation_id": generation_manifest["identifier"],
                    "manifest_digest": manifest_digest(generation_manifest),
                    "package_hash": generation_manifest["package_hash"],
                    "package_version": generation_manifest["package_version"],
                },
                "previous": None,
                "transaction": None,
            }
            status = state / _STATUS_RELATIVE
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_bytes(canonical_json_bytes(smoke_status))
            working = sandbox_root / "cwd"
            working.mkdir()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "",
                "VOICE_INTENT_HOME": str(state),
                "PYTHONUTF8": "1",
            }
            if os.name == "nt":
                environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(smoke_capsule / "scripts" / "voice_intent.py"),
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
            raise ValueError("published generation smoke test failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ok",
            "degraded",
        }:
            raise ValueError("published generation smoke test failed")

    def _activate_generation(self, status_payload: dict[str, object]) -> Path:
        self._write_status_payload(status_payload)
        return generic_layout_paths(self.state_paths).status

    def _remove_first_install_transaction(self, *, missing_ok: bool = False) -> None:
        lease = self._state_lease()
        if not lease.exists(_TRANSACTION_RELATIVE):
            if missing_ok:
                return
            raise FileNotFoundError("first-install transaction is missing")
        lease.unlink(_TRANSACTION_RELATIVE)
        lease.fsync_directory(_GENERIC_RELATIVE)
        self._record_changes((generic_layout_paths(self.state_paths).transaction,))

    def _verified_result(
        self,
        operation: str,
        root: Path,
        artifacts: VersionedArtifacts,
        options: InstallOptions,
    ) -> AdapterResult:
        status = self._read_versioned_status(root)
        if (
            status is None
            or status.active.generation_id != artifacts.generation.identifier
        ):
            raise ValueError("activated generation status is unavailable")
        self._validate_capsule_directory(root, Path(_NAME), artifacts.capsule)
        self._validate_generation_directory(
            self.state_paths.root,
            _GENERATIONS_RELATIVE / artifacts.generation.identifier,
            artifacts.generation,
        )
        return AdapterResult(
            self.platform,
            operation,
            self._capability(options),
            (
                "skill discovery must be enabled by the selected host",
                self._manual_message(options),
            ),
        )

    def doctor(self) -> AdapterResult:
        self._recovery_changes = ()
        self._change_events = []
        try:
            with self._state_operation(create=False, lock_missing=False):
                result = self._doctor_locked()
        except Exception:
            result = self._failed("shared adapter status is unavailable")
        return self._with_recovery_changes(result)

    def _doctor_locked(self) -> AdapterResult:
        recovery_error = False
        try:
            self._recover_pending(None)
        except Exception:
            recovery_error = True
        recovery = None if recovery_error else self._read_recovery()
        recovery_pending = recovery is not None or self._unanchored_recovery
        status_error = recovery_error
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
            if recovery_pending:
                messages.append(
                    "recovery required: installer transaction is incomplete"
                )
            messages.append("manual action required")
            return AdapterResult(
                self.platform,
                "degraded" if status_error or recovery_pending else "not-installed",
                CapabilityLevel.UNAVAILABLE,
                tuple(messages),
            )

        if (
            self._recovered_uninstall
            and status is None
            and not recovery_pending
            and not status_error
        ):
            return AdapterResult(
                self.platform,
                "not-installed",
                CapabilityLevel.UNAVAILABLE,
                (
                    "installed files: absent",
                    "adapter status: missing",
                    "shared personal and project data were preserved",
                ),
                self._recovery_changes,
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
        if recovery_pending:
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
            and not recovery_pending
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
        self._change_events = []
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
        transaction_id = secrets.token_hex(16)
        quarantine = root / f".{_NAME}.quarantine-{transaction_id}"
        quarantine_identity = self._prepare_transaction_directory(
            root, quarantine, manifest
        )
        transaction = self._begin_transaction(
            "uninstall",
            root,
            target,
            quarantine,
            quarantine,
            old_manifest=manifest,
            new_manifest=None,
            options=None,
            expected_root_identity=inspection.root_identity,
            expected_target_identity=inspection.target_identity,
            expected_staging_identity=quarantine_identity,
            expected_quarantine_identity=quarantine_identity,
        )
        recovery_path = self._write_recovery(transaction)
        try:
            changed = self._resume_transaction(
                self._read_status(), transaction, expected_root=root
            )
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                (
                    "generic uninstall is incomplete",
                    "run doctor before retrying",
                ),
                self._dedupe_paths((recovery_path,), self._recovery_changes),
            )
        return AdapterResult(
            self.platform,
            "uninstalled",
            CapabilityLevel.MANUAL,
            (
                "shared personal and project data were preserved",
                "benign empty managed directories may remain",
            ),
            self._dedupe_paths(paths, changed),
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
        existed = {
            path: self._direct_directory_exists(path) for path in tracked
        }
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
        with guard_state_root(
            root,
            retained_dirs=(_NAME,),
            create_retained=True,
            exclusive_create_retained=(_NAME,),
        ) as lease:
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
            self._record_changes((target,))
            target_identity = self._directory_identity(lease.stat(_NAME))
        return self._commit_into_empty_target(
            root,
            target,
            staging,
            manifest,
            options,
            expected_root_identity,
            target_identity,
            expected_staging_identity,
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
        reusable = self._reuse_current_package(root, target, desired, options)
        if reusable is not None:
            return reusable
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
                    expected_staging_identity,
                )
            return self._failed("existing skill directory is not installer-managed")
        status = None
        try:
            status = self._read_status()
        except Exception:
            return self._failed("protected adapter status is invalid")
        if status is None or not self._status_matches(
            status, root, current.manifest
        ):
            return self._failed(
                "managed package does not match protected adapter status"
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

    def _reuse_current_package(
        self,
        root: Path,
        target: Path,
        desired: dict[str, object],
        options: InstallOptions,
    ) -> AdapterResult | None:
        """Return a current-package result without materializing staging."""
        current = self._inspect_package(root, target, allow_hash_failure=True)
        if (
            current.state != "valid"
            or current.manifest is None
            or current.manifest.get("format") != 3
            or current.manifest.get("package_hash") != desired.get("package_hash")
            or current.manifest.get("package_version")
            != desired.get("package_version")
        ):
            return None
        status = None
        try:
            status = self._read_status()
        except Exception:
            return self._failed("protected adapter status is invalid")
        desired_capability = self._capability(options).value
        if status is not None and not self._status_matches(
            status, root, current.manifest
        ):
            return self._failed(
                "managed package does not match protected adapter status"
            )
        if (
            status is not None
            and status.get("capability") == desired_capability
        ):
            return AdapterResult(
                self.platform,
                "already-installed",
                self._capability(options),
                ("managed skill files are already installed",),
            )
        status_path = self._write_status(target, options)
        return AdapterResult(
            self.platform,
            "repaired",
            self._capability(options),
            ("adapter status and capability were rebuilt",),
            (status_path,),
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
        staging_prefix = f".{_NAME}.staging-"
        if not staging.name.startswith(staging_prefix):
            raise ValueError("invalid staging directory")
        transaction_id = staging.name.removeprefix(staging_prefix)
        quarantine = root / f".{_NAME}.quarantine-{transaction_id}"
        quarantine_identity = self._prepare_transaction_directory(
            root, quarantine, current.manifest
        )
        transaction = self._begin_transaction(
            "upgrade",
            root,
            target,
            staging,
            quarantine,
            old_manifest=current.manifest,
            new_manifest=desired,
            options=options,
            expected_root_identity=current.root_identity,
            expected_target_identity=current.target_identity,
            expected_staging_identity=expected_staging_identity,
            expected_quarantine_identity=quarantine_identity,
        )
        recovery_path = self._write_recovery(transaction)
        try:
            changed = self._resume_transaction(
                self._read_status(), transaction, expected_root=root
            )
        except Exception:
            return AdapterResult(
                self.platform,
                "degraded",
                self._capability(options),
                (
                    "managed upgrade is incomplete",
                    "recovery is recorded; retry install",
                    "transaction directories are retained for manual inspection",
                ),
                self._dedupe_paths(
                    created_dirs,
                    (recovery_path,),
                    self._recovery_changes,
                ),
            )

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
                changed,
                self._staging_directories(staging, desired),
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
        expected_staging_identity: tuple[int, int] | None,
    ) -> AdapterResult:
        created_dirs = self._ensure_target_directories(
            root, target, manifest, expected_target_identity
        )
        transaction = self._begin_transaction(
            "install",
            root,
            target,
            staging,
            staging,
            old_manifest=None,
            new_manifest=manifest,
            options=options,
            expected_root_identity=expected_root_identity,
            expected_target_identity=expected_target_identity,
            expected_staging_identity=expected_staging_identity,
            expected_quarantine_identity=expected_staging_identity,
        )
        recovery_path = self._write_recovery(transaction)
        try:
            changed = self._resume_transaction(
                self._read_status(), transaction, expected_root=root
            )
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
                    created_dirs,
                    (staging, recovery_path),
                    self._recovery_changes,
                ),
            )
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
                changed,
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
                self._record_changes((root / directory,))
        return tuple(created)

    def _stage_runtime(
        self,
        root: Path,
        staging: Path,
        *,
        runtime: dict[str, bytes] | None = None,
        manifest: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], tuple[int, int], tuple[int, int]]:
        if runtime is None or manifest is None:
            runtime, manifest = self._prepare_runtime(root)
        assert manifest is not None
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        directories = self._runtime_directories(staging.name, (*runtime, _MANIFEST))
        with guard_state_root(
            root,
            retained_dirs=directories,
            create_retained=True,
            exclusive_create_retained=(staging.name,),
        ) as lease:
            root_identity = self._directory_identity(lease.stat("."))
            staging_identity = self._directory_identity(lease.stat(staging.name))
            self._record_changes(tuple(root / relative for relative in directories))
            for relative, data in runtime.items():
                lease.write_bytes_exclusive(
                    Path(staging.name) / Path(relative), data
                )
                self._record_changes((staging / relative,))
            lease.write_bytes_exclusive(
                Path(staging.name) / _MANIFEST, manifest_bytes
            )
            self._record_changes((staging / _MANIFEST,))
            self._verify_manifest_under_lease(
                lease, staging, manifest, require_hashes=True
            )
        return manifest, root_identity, staging_identity

    def _prepare_runtime(
        self, root: Path
    ) -> tuple[dict[str, bytes], dict[str, object]]:
        """Collect and smoke-test the package before creating live staging."""
        runtime = self._runtime_source_files()
        with tempfile.TemporaryDirectory(prefix="voice-intent-stage-") as sandbox:
            validation = Path(sandbox) / "package"
            for relative, data in runtime.items():
                destination = validation / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            self._validate_staging(validation)
        manifest = self._manifest_for_runtime(runtime, root)
        return runtime, manifest

    def _runtime_source_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for name in _PACKAGE_FILES:
            files[name] = self._read_source_file(self.repository / name)
        for directory in _PACKAGE_DIRECTORIES:
            self._collect_source_tree(
                self.repository / directory, Path(directory), files
            )
        files["scripts/voice_intent.py"] = self._read_source_file(
            self.repository / "scripts" / "voice_intent.py"
        )
        python_source = self.repository / "src" / "voice_intent_normalizer"
        if not python_source.is_dir():
            python_source = Path(__file__).resolve().parents[1]
        self._collect_source_tree(
            python_source,
            Path("src") / "voice_intent_normalizer",
            files,
            python_only=True,
        )
        return dict(sorted(files.items()))

    @staticmethod
    def _read_source_file(source: Path) -> bytes:
        info = os.lstat(source)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        ):
            raise ValueError("runtime source must be a direct regular file")
        data = source.read_bytes()
        after = os.lstat(source)
        if (info.st_dev, info.st_ino, info.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise OSError("runtime source changed while it was copied")
        return data

    def _collect_source_tree(
        self,
        source: Path,
        destination: Path,
        collected: dict[str, bytes],
        *,
        python_only: bool = False,
    ) -> None:
        for current, directories, filenames in os.walk(
            source, followlinks=False
        ):
            current_path = Path(current)
            current_info = os.lstat(current_path)
            self._require_direct_directory(current_info)
            for name in tuple(directories):
                info = os.lstat(current_path / name)
                self._require_direct_directory(info)
            for name in filenames:
                if python_only and not name.endswith(".py"):
                    continue
                path = current_path / name
                relative = destination / path.relative_to(source)
                normalized = str(relative).replace("\\", "/")
                if normalized in collected:
                    raise ValueError("duplicate runtime source path")
                collected[normalized] = self._read_source_file(path)

    @staticmethod
    def _runtime_directories(
        staging_name: str, relatives: Sequence[str]
    ) -> tuple[Path, ...]:
        directories = {Path(staging_name)}
        for relative in relatives:
            parent = Path(staging_name) / Path(relative).parent
            while parent != Path("."):
                directories.add(parent)
                if parent == Path(staging_name):
                    break
                parent = parent.parent
        return tuple(
            sorted(directories, key=lambda path: (len(path.parts), str(path)))
        )

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
    def _manifest_for_runtime(
        runtime: dict[str, bytes], root: Path
    ) -> dict[str, object]:
        files = tuple(sorted(runtime))
        hashes = {
            relative: hashlib.sha256(runtime[relative]).hexdigest()
            for relative in files
        }
        metadata = runtime["pyproject.toml"].decode("utf-8")
        version_matches = re.findall(
            r'(?m)^version\s*=\s*"([0-9A-Za-z][0-9A-Za-z.+-]*)"\s*$',
            metadata,
        )
        if len(version_matches) != 1:
            raise ValueError("runtime package version is invalid")
        package_version = version_matches[0]
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
        return payload

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
        return GenericAdapter._safe_relative_files(
            payload.get("files"), manifest_policy="forbid"
        )

    @staticmethod
    def _safe_relative_files(
        payload: object, *, manifest_policy: str
    ) -> tuple[str, ...] | None:
        if (
            manifest_policy not in {"forbid", "require"}
            or not isinstance(payload, list)
            or not payload
            or len(payload) > _MANIFEST_MAX_ENTRIES
        ):
            return None
        files: list[str] = []
        seen: set[str] = set()
        windows_reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        for value in payload:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > _MANIFEST_MAX_PATH_LENGTH
                or ":" in value
            ):
                return None
            windows_path = PureWindowsPath(value)
            normalized = value.replace("\\", "/")
            posix_path = PurePosixPath(normalized)
            raw_parts = normalized.split("/")
            if (
                windows_path.is_absolute()
                or bool(windows_path.drive)
                or bool(windows_path.root)
                or posix_path.is_absolute()
                or len(raw_parts) > _MANIFEST_MAX_DEPTH
                or any(part in {"", ".", ".."} for part in raw_parts)
                or any(part.endswith((" ", ".")) for part in raw_parts)
                or any(
                    part.split(".", 1)[0].upper() in windows_reserved
                    for part in raw_parts
                )
            ):
                return None
            normalized = "/".join(raw_parts)
            if normalized in seen:
                return None
            seen.add(normalized)
            files.append(normalized)
        if manifest_policy == "forbid" and _MANIFEST in seen:
            return None
        if manifest_policy == "require" and _MANIFEST not in seen:
            return None
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
        payload = self._status_payload(
            target, manifest, self._capability(options).value
        )
        self._write_status_payload(payload)
        return self.state_paths.adapter_status_file(self.platform)

    def _status_payload(
        self,
        target: Path,
        manifest: dict[str, object],
        capability: str,
        *,
        transaction: dict[str, object] | None = None,
    ) -> dict[str, object]:
        root = target.parent
        return {
            "capability": capability,
            "format": 4,
            "install_id": manifest["install_id"],
            "manifest_digest": self._manifest_digest(manifest),
            "managed_directory": str(target),
            "owner": _NAME,
            "package_hash": manifest["package_hash"],
            "package_version": manifest["package_version"],
            "skill_root": str(root),
            "skill_root_key": state_root_lock_key(root),
            "transaction": transaction,
        }

    def _write_status_payload(self, payload: dict[str, object]) -> None:
        data = canonical_json_bytes(payload)
        lease = self._state_lease()
        try:
            lease.write_bytes_atomic(_STATUS_RELATIVE, data)
            lease.fsync_directory(_GENERIC_RELATIVE)
        except OSError:
            # Atomic replacement is the activation commit point.  A directory
            # flush can report failure after that point (and POSIX's atomic
            # writer performs its own parent flush).  Never report a failed,
            # inert install when the exact active status is already visible.
            try:
                committed = (
                    lease.read_bytes(
                        _STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status"
                    )
                    == data
                )
            except (OSError, ValueError):
                committed = False
            if not committed:
                raise
        self._record_changes((self.state_paths.adapter_status_file(self.platform),))

    def _read_status(self) -> dict[str, object] | None:
        lease = self._state_lease()
        if not lease.root_exists or not lease.available("adapters"):
            return None
        if not lease.exists(_STATUS_RELATIVE):
            return None
        raw = lease.read_bytes(_STATUS_RELATIVE, _MANIFEST_LIMIT, "adapter status")
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("format") == 3:
            payload = {**payload, "format": 4, "transaction": None}
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
            "transaction",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("format") != 4
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
        root = Path(str(payload["skill_root"]))
        managed = Path(str(payload["managed_directory"]))
        if (
            state_root_lock_key(root) != payload["skill_root_key"]
            or managed.name != _NAME
            or state_root_lock_key(managed.parent) != payload["skill_root_key"]
        ):
            raise ValueError("invalid adapter status paths")
        transaction = payload.get("transaction")
        if transaction is not None:
            self._validate_transaction(transaction, payload)
        return payload

    def _remove_status(self) -> Path:
        lease = self._state_lease()
        if not lease.exists(_STATUS_RELATIVE):
            raise FileNotFoundError("adapter status is missing")
        lease.unlink(_STATUS_RELATIVE)
        path = self.state_paths.adapter_status_file(self.platform)
        self._record_changes((path,))
        return path

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
                and status.get("format") == 4
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

    def _begin_transaction(
        self,
        operation: str,
        root: Path,
        target: Path,
        staging: Path,
        quarantine: Path,
        *,
        old_manifest: dict[str, object] | None,
        new_manifest: dict[str, object] | None,
        options: InstallOptions | None,
        expected_root_identity: tuple[int, int] | None,
        expected_target_identity: tuple[int, int] | None,
        expected_staging_identity: tuple[int, int] | None,
        expected_quarantine_identity: tuple[int, int] | None,
    ) -> dict[str, object]:
        if operation not in {"install", "upgrade", "uninstall"}:
            raise ValueError("unsupported installer transaction")
        staging_prefix = f".{_NAME}.staging-"
        quarantine_prefix = f".{_NAME}.quarantine-"
        if staging.name.startswith(staging_prefix):
            transaction_id = staging.name.removeprefix(staging_prefix)
        elif staging.name.startswith(quarantine_prefix):
            transaction_id = staging.name.removeprefix(quarantine_prefix)
        else:
            raise ValueError("transaction staging name is invalid")
        if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            raise ValueError("transaction id is invalid")
        if quarantine.name not in {
            f".{_NAME}.quarantine-{transaction_id}",
            staging.name,
        }:
            raise ValueError("transaction directory is not derived from its id")
        capability = (
            self._capability(options).value
            if options is not None
            else str((self._read_status() or {}).get("capability", "manual"))
        )
        with guard_state_root(
            root, retained_dirs=(target.name, staging.name, quarantine.name)
        ) as lease:
            identities = {
                "root_identity": self._directory_identity(lease.stat(".")),
                "target_identity": self._directory_identity(lease.stat(target.name)),
                "staging_identity": self._directory_identity(lease.stat(staging.name)),
                "quarantine_identity": self._directory_identity(
                    lease.stat(quarantine.name)
                ),
            }
            expected_identities = {
                "root_identity": expected_root_identity,
                "target_identity": expected_target_identity,
                "staging_identity": expected_staging_identity,
                "quarantine_identity": expected_quarantine_identity,
            }
            for name, expected in expected_identities.items():
                if expected is not None and identities[name] != expected:
                    raise OSError(f"{name.replace('_', ' ')} changed before journal")
        transaction: dict[str, object] = {
            "capability": capability,
            "digest": "",
            "format": 1,
            "id": transaction_id,
            "new": self._transaction_package(new_manifest),
            "old": self._transaction_package(old_manifest),
            "operation": operation,
            "phase": "publishing",
            "quarantine_identity": list(identities["quarantine_identity"]),
            "quarantine_name": quarantine.name,
            "root_identity": list(identities["root_identity"]),
            "root_key": state_root_lock_key(root),
            "staging_identity": list(identities["staging_identity"]),
            "staging_name": staging.name,
            "target_identity": list(identities["target_identity"]),
            "target_name": _NAME,
        }
        transaction["digest"] = self._transaction_digest(transaction)
        base_manifest = old_manifest or new_manifest
        if base_manifest is None:
            raise ValueError("transaction has no package identity")
        status = self._status_payload(
            target,
            base_manifest,
            capability,
            transaction=transaction,
        )
        self._write_status_payload(status)
        return transaction

    @staticmethod
    def _transaction_package(
        manifest: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if manifest is None:
            return None
        files = GenericAdapter._manifest_files(manifest)
        hashes = manifest.get("hashes")
        if files is None or not isinstance(hashes, dict):
            raise ValueError("invalid transaction manifest")
        file_hashes = {relative: str(hashes[relative]) for relative in files}
        file_hashes[_MANIFEST] = GenericAdapter._manifest_digest(manifest)
        return {
            "file_hashes": file_hashes,
            "files": [*files, _MANIFEST],
            "install_id": manifest["install_id"],
            "manifest_digest": GenericAdapter._manifest_digest(manifest),
            "package_hash": manifest["package_hash"],
            "package_version": manifest["package_version"],
        }

    @staticmethod
    def _transaction_digest(transaction: dict[str, object]) -> str:
        unsigned = {**transaction, "digest": ""}
        return hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _validate_transaction(
        self, transaction: object, status: dict[str, object]
    ) -> None:
        expected = {
            "capability",
            "digest",
            "format",
            "id",
            "new",
            "old",
            "operation",
            "phase",
            "quarantine_identity",
            "quarantine_name",
            "root_identity",
            "root_key",
            "staging_identity",
            "staging_name",
            "target_identity",
            "target_name",
        }
        if (
            not isinstance(transaction, dict)
            or set(transaction) != expected
            or transaction.get("format") != 1
            or transaction.get("operation")
            not in {"install", "upgrade", "uninstall"}
            or transaction.get("phase")
            not in {"prepared", "quarantined", "publishing"}
            or transaction.get("target_name") != _NAME
            or transaction.get("root_key") != status.get("skill_root_key")
            or transaction.get("capability")
            not in {"manual", "implicit"}
            or not isinstance(transaction.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", str(transaction["id"]))
            or transaction.get("digest") != self._transaction_digest(transaction)
        ):
            raise ValueError("invalid installer transaction")
        transaction_id = str(transaction["id"])
        allowed_names = {
            f".{_NAME}.staging-{transaction_id}",
            f".{_NAME}.quarantine-{transaction_id}",
        }
        if (
            transaction.get("staging_name") not in allowed_names
            or transaction.get("quarantine_name") not in allowed_names
        ):
            raise ValueError("invalid installer transaction directories")
        operation = transaction["operation"]
        staging_name = transaction["staging_name"]
        quarantine_name = transaction["quarantine_name"]
        expected_staging = f".{_NAME}.staging-{transaction_id}"
        expected_quarantine = f".{_NAME}.quarantine-{transaction_id}"
        if (
            operation == "install"
            and not (
                staging_name == expected_staging
                and quarantine_name == expected_staging
                and transaction.get("old") is None
                and transaction.get("new") is not None
            )
        ) or (
            operation == "upgrade"
            and not (
                staging_name == expected_staging
                and quarantine_name == expected_quarantine
                and transaction.get("old") is not None
                and transaction.get("new") is not None
            )
        ) or (
            operation == "uninstall"
            and not (
                staging_name == expected_quarantine
                and quarantine_name == expected_quarantine
                and transaction.get("old") is not None
                and transaction.get("new") is None
            )
        ):
            raise ValueError("invalid installer transaction shape")
        for name in (
            "root_identity",
            "staging_identity",
            "target_identity",
            "quarantine_identity",
        ):
            identity = transaction.get(name)
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(value, int) for value in identity)
            ):
                raise ValueError("invalid installer transaction identity")
        for name in ("old", "new"):
            package = transaction.get(name)
            if package is None:
                continue
            if not isinstance(package, dict):
                raise ValueError("invalid installer transaction package")
            files = self._recovery_files(package.get("files"))
            hashes = package.get("file_hashes")
            if (
                files is None
                or not isinstance(hashes, dict)
                or set(hashes) != set(files)
                or not all(
                    isinstance(hashes.get(path), str)
                    and re.fullmatch(r"[0-9a-f]{64}", str(hashes[path]))
                    for path in files
                )
            ):
                raise ValueError("invalid installer transaction package")

    def _write_recovery(self, transaction: dict[str, object]) -> Path:
        """Write a non-authoritative marker for one status-anchored transaction."""
        payload = {
            "format": 3,
            "owner": _NAME,
            "transaction_digest": transaction["digest"],
            "transaction_id": transaction["id"],
        }
        self._state_lease().write_bytes_atomic(
            _RECOVERY_RELATIVE,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        path = self.state_paths.root / _RECOVERY_RELATIVE
        self._record_changes((path,))
        return path

    def _read_recovery(self) -> dict[str, object] | None:
        """Return only a recovery transaction authenticated by protected status."""
        self._unanchored_recovery = False
        lease = self._state_lease()
        if not lease.root_exists or not lease.available("adapters"):
            return None
        status = self._read_status()
        transaction = status.get("transaction") if status is not None else None
        marker_exists = lease.exists(_RECOVERY_RELATIVE)
        if transaction is None:
            self._unanchored_recovery = marker_exists
            return None
        assert isinstance(transaction, dict)
        if not marker_exists:
            return transaction
        try:
            raw = lease.read_bytes(
                _RECOVERY_RELATIVE, 16 * 1024, "adapter recovery"
            )
            marker = json.loads(raw.decode("utf-8"))
        except Exception:
            self._unanchored_recovery = True
            return None
        if (
            not isinstance(marker, dict)
            or set(marker)
            != {"format", "owner", "transaction_digest", "transaction_id"}
            or marker.get("format") != 3
            or marker.get("owner") != _NAME
            or marker.get("transaction_id") != transaction.get("id")
            or marker.get("transaction_digest") != transaction.get("digest")
        ):
            self._unanchored_recovery = True
            return None
        return transaction

    def _remove_recovery(self, *, missing_ok: bool = False) -> None:
        lease = self._state_lease()
        existed = lease.exists(_RECOVERY_RELATIVE)
        lease.unlink(_RECOVERY_RELATIVE, missing_ok=missing_ok)
        if existed:
            self._record_changes((self.state_paths.root / _RECOVERY_RELATIVE,))

    def _recover_pending(self, expected_root: Path | None) -> None:
        status = self._read_status()
        transaction = self._read_recovery()
        if self._unanchored_recovery:
            if expected_root is None:
                return
            raise ValueError("unanchored recovery metadata requires manual review")
        if transaction is None:
            return
        assert status is not None
        changed = self._resume_transaction(
            status, transaction, expected_root=expected_root
        )
        self._recovery_changes = self._dedupe_paths(
            self._recovery_changes, changed
        )

    def _resume_transaction(
        self,
        status: dict[str, object] | None,
        transaction: dict[str, object],
        *,
        expected_root: Path | None,
    ) -> tuple[Path, ...]:
        """Idempotently complete a transaction using status-bound identities."""
        if status is None or status.get("transaction") != transaction:
            raise ValueError("installer transaction is not anchored by status")
        self._validate_transaction(transaction, status)
        root = self._safe_skill_root(Path(str(status["skill_root"])))
        if (
            expected_root is not None
            and state_root_lock_key(expected_root) != state_root_lock_key(root)
        ):
            raise ValueError("recovery belongs to a different skill root")
        self._configured_skill_root = root
        target = root / _NAME
        staging = root / str(transaction["staging_name"])
        quarantine = root / str(transaction["quarantine_name"])
        old = transaction.get("old")
        new = transaction.get("new")
        old_package = old if isinstance(old, dict) else None
        new_package = new if isinstance(new, dict) else None
        old_files = self._transaction_files(old_package)
        new_files = self._transaction_files(new_package)
        all_files = tuple(dict.fromkeys((*old_files, *new_files)))
        retained = {Path(_NAME), Path(staging.name), Path(quarantine.name)}
        for directory in (target, staging, quarantine):
            for relative in all_files:
                parent = Path(directory.name) / Path(relative).parent
                while parent != Path("."):
                    retained.add(parent)
                    if parent == Path(directory.name):
                        break
                    parent = parent.parent
        changed: list[Path] = []
        with guard_state_root(
            root,
            retained_dirs=tuple(
                sorted(retained, key=lambda path: (len(path.parts), str(path)))
            ),
        ) as lease:
            for relative, expected in (
                (Path("."), transaction["root_identity"]),
                (Path(_NAME), transaction["target_identity"]),
                (Path(staging.name), transaction["staging_identity"]),
                (Path(quarantine.name), transaction["quarantine_identity"]),
            ):
                if list(self._directory_identity(lease.stat(relative))) != expected:
                    raise OSError("transaction directory identity changed")

            states: dict[str, tuple[str | None, str | None, str | None]] = {}
            for relative in all_files:
                target_hash = self._lease_hash_or_none(
                    lease, Path(_NAME) / relative
                )
                staging_hash = self._lease_hash_or_none(
                    lease, Path(staging.name) / relative
                )
                quarantine_hash = self._lease_hash_or_none(
                    lease, Path(quarantine.name) / relative
                )
                old_hash = self._transaction_expected_hash(old_package, relative)
                new_hash = self._transaction_expected_hash(new_package, relative)
                if target_hash is not None and target_hash not in {
                    value for value in (old_hash, new_hash) if value is not None
                }:
                    raise ValueError(
                        f"transaction found an unknown managed destination: {relative}"
                    )
                if staging_hash is not None and new_hash is not None:
                    if staging_hash != new_hash:
                        raise ValueError("transaction staging was modified")
                if (
                    quarantine.name != staging.name
                    and quarantine_hash is not None
                ):
                    if old_hash is None or quarantine_hash != old_hash:
                        raise ValueError("transaction quarantine was modified")
                if old_hash is not None:
                    if target_hash == old_hash:
                        pass
                    elif quarantine_hash == old_hash:
                        pass
                    elif target_hash == new_hash and new_hash is not None:
                        raise ValueError("transaction backup is missing")
                    elif transaction["operation"] == "upgrade":
                        # A repair upgrade may start with missing managed files.
                        # The desired staged package remains authoritative.
                        pass
                    else:
                        raise ValueError("transaction source is missing")
                if (
                    new_hash is not None
                    and target_hash != new_hash
                    and staging_hash != new_hash
                ):
                    raise ValueError("transaction publish source is missing")
                states[relative] = (
                    target_hash,
                    staging_hash,
                    quarantine_hash,
                )

            for relative in old_files:
                target_relative = Path(_NAME) / relative
                quarantine_relative = Path(quarantine.name) / relative
                old_hash = self._transaction_expected_hash(old_package, relative)
                target_hash, _, quarantine_hash = states[relative]
                if target_hash == old_hash:
                    if quarantine_hash is None:
                        source_removed = lease.move_no_replace(
                            target_relative,
                            quarantine_relative,
                            expected_sha256=str(old_hash),
                            limit=self._transaction_file_limit(relative),
                            canonical_json=relative == _MANIFEST,
                        )
                        if (
                            self._lease_hash_or_none(lease, quarantine_relative)
                            != old_hash
                        ):
                            raise ValueError(
                                "transaction quarantine captured an unknown source"
                            )
                        changed.append(quarantine / relative)
                        self._record_changes((quarantine / relative,))
                        if source_removed:
                            changed.append(target / relative)
                            self._record_changes((target / relative,))
                        else:
                            raise OSError(
                                "identity-bound source removal is unavailable "
                                "on this platform"
                            )

            for relative in new_files:
                target_relative = Path(_NAME) / relative
                staging_relative = Path(staging.name) / relative
                expected_hash = self._transaction_expected_hash(
                    new_package, relative
                )
                current_hash = self._lease_hash_or_none(lease, target_relative)
                if current_hash is None:
                    source_removed = lease.move_no_replace(
                        staging_relative,
                        target_relative,
                        expected_sha256=str(expected_hash),
                        limit=self._transaction_file_limit(relative),
                        canonical_json=relative == _MANIFEST,
                    )
                    if (
                        self._lease_hash_or_none(lease, target_relative)
                        != expected_hash
                    ):
                        raise ValueError(
                            "transaction publication captured an unknown source"
                        )
                    changed.append(target / relative)
                    self._record_changes((target / relative,))
                    if source_removed:
                        changed.append(staging / relative)
                        self._record_changes((staging / relative,))
                elif current_hash != expected_hash:
                    raise ValueError(
                        "transaction would overwrite an unknown destination"
                    )

            for relative in new_files:
                if self._lease_hash_or_none(
                    lease, Path(_NAME) / relative
                ) != self._transaction_expected_hash(new_package, relative):
                    raise ValueError("transaction final package is incomplete")
            if new_package is None:
                for relative in old_files:
                    if lease.exists(Path(_NAME) / relative):
                        raise ValueError("transaction uninstall is incomplete")

        # Remove the optional marker before clearing the authoritative journal.
        # A crash between these writes still leaves a resumable protected status.
        marker_existed = self._state_lease().exists(_RECOVERY_RELATIVE)
        self._remove_recovery(missing_ok=True)
        if marker_existed:
            changed.append(self.state_paths.root / _RECOVERY_RELATIVE)
        if new_package is None:
            changed.append(self._remove_status())
            self._recovered_uninstall = True
        else:
            final_status = {
                **status,
                "capability": transaction["capability"],
                "install_id": new_package["install_id"],
                "manifest_digest": new_package["manifest_digest"],
                "package_hash": new_package["package_hash"],
                "package_version": new_package["package_version"],
                "transaction": None,
            }
            self._write_status_payload(final_status)
            changed.append(self.state_paths.adapter_status_file(self.platform))
        result = self._dedupe_paths(changed)
        self._recovery_changes = self._dedupe_paths(
            self._recovery_changes, result
        )
        return result

    @staticmethod
    def _transaction_files(
        package: dict[str, object] | None,
    ) -> tuple[str, ...]:
        if package is None:
            return ()
        files = GenericAdapter._recovery_files(package.get("files"))
        if files is None:
            raise ValueError("invalid transaction file list")
        return files

    @staticmethod
    def _transaction_expected_hash(
        package: dict[str, object] | None, relative: str
    ) -> str | None:
        if package is None:
            return None
        hashes = package.get("file_hashes")
        if not isinstance(hashes, dict):
            raise ValueError("invalid transaction hashes")
        value = hashes.get(relative)
        return str(value) if isinstance(value, str) else None

    @staticmethod
    def _transaction_file_limit(relative: str) -> int:
        return _MANIFEST_LIMIT if relative == _MANIFEST else _MANAGED_FILE_LIMIT

    @staticmethod
    def _lease_hash_or_none(
        lease: StateRootLease, relative: Path
    ) -> str | None:
        if not lease.exists(relative):
            return None
        if relative.name == _MANIFEST:
            raw = lease.read_bytes(relative, _MANIFEST_LIMIT, "installer manifest")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid installer manifest")
            return GenericAdapter._manifest_digest(payload)
        return GenericAdapter._lease_file_hash(lease, relative)

    @staticmethod
    def _recovery_files(payload: object) -> tuple[str, ...] | None:
        return GenericAdapter._safe_relative_files(
            payload, manifest_policy="require"
        )

    @staticmethod
    def _lease_file_hash(lease: StateRootLease, relative: Path) -> str:
        return hashlib.sha256(
            lease.read_bytes(relative, _MANAGED_FILE_LIMIT, "recovery file")
        ).hexdigest()

    def _prepare_transaction_directory(
        self,
        root: Path,
        directory: Path,
        manifest: dict[str, object],
    ) -> tuple[int, int]:
        files = (*(self._manifest_files(manifest) or ()), _MANIFEST)
        retained = self._runtime_directories(directory.name, files)
        with guard_state_root(
            root,
            retained_dirs=retained,
            create_retained=True,
            exclusive_create_retained=(directory.name,),
        ) as lease:
            identity = self._directory_identity(lease.stat(directory.name))
        self._record_changes(tuple(root / relative for relative in retained))
        return identity

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
        if not self._recovery_changes and not self._change_events:
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

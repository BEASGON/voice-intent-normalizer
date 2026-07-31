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
from pathlib import Path

from ..paths import StatePaths, guard_state_root, validate_state_root
from ..updater import _retained_lease_update_lock
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions

_MANIFEST = ".voice-intent-normalizer-install.json"
_NAME = "voice-intent-normalizer"
_STATUS_RELATIVE = Path("adapters") / "generic.json"
_PACKAGE_FILES = ("SKILL.md", "pyproject.toml", "LICENSE")
_PACKAGE_DIRECTORIES = ("agents", "assets", "references")


class GenericAdapter:
    """Install exactly one self-contained Agent Skills-compatible directory."""

    platform = "generic"

    def __init__(self, repository: str | Path, state_paths: StatePaths) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths
        self._configured_skill_root: Path | None = None

    def detect(self) -> AdapterResult:
        return self.doctor()

    def install(self, options: InstallOptions) -> AdapterResult:
        with guard_state_root(
            self.state_paths.root,
            create=True,
            retained_dirs=("adapters",),
            create_retained=True,
        ) as lease:
            with _retained_lease_update_lock(lease):
                return self._install_locked(options)

    def _install_locked(self, options: InstallOptions) -> AdapterResult:
        if options.output_dir is None:
            return self._failed("a skill root is required")
        try:
            root = self._safe_skill_root(options.output_dir)
            self._configured_skill_root = root
            target = root / _NAME
            self._ensure_direct_target(target)
            if target.exists():
                if not self._managed(target):
                    return self._failed(
                        "existing skill directory is not installer-managed"
                    )
                return AdapterResult(
                    self.platform,
                    "already-installed",
                    self._capability(options),
                    ("managed skill files are already installed",),
                )
            staging = root / f".{_NAME}.staging-{secrets.token_hex(12)}"
            try:
                self._copy_runtime(staging)
                self._validate_staging(staging)
                self._write_manifest(staging, root)
                os.replace(staging, target)
            except Exception:
                self._remove_staging(staging)
                raise
            try:
                status_path = self._write_status(target, options)
            except Exception:
                return AdapterResult(
                    self.platform,
                    "degraded",
                    self._capability(options),
                    ("skill files installed; shared status recording is pending",),
                    self._managed_files(target),
                )
            return AdapterResult(
                self.platform,
                "installed",
                self._capability(options),
                (
                    "skill discovery must be enabled by the selected host",
                    self._manual_message(options),
                ),
                self._managed_files(target) + (status_path,),
            )
        except Exception:
            return self._failed("generic installation was not completed")

    def doctor(self) -> AdapterResult:
        try:
            status = self._read_status()
        except Exception:
            return self._failed("shared adapter status is unavailable")
        if status is None:
            return AdapterResult(
                self.platform,
                "not-installed",
                CapabilityLevel.UNAVAILABLE,
                (
                    "installed files: absent",
                    "shared state: unavailable",
                    "manual action required",
                ),
            )
        target = Path(status["managed_directory"])
        try:
            self._ensure_direct_target(target)
        except (OSError, ValueError):
            return self._failed("managed skill directory is not a direct directory")
        installed = self._managed(target)
        capability = CapabilityLevel(
            status.get("capability", CapabilityLevel.MANUAL.value)
        )
        return AdapterResult(
            self.platform,
            "installed" if installed else "degraded",
            capability,
            (
                f"installed files: {'present' if installed else 'missing'}",
                "skill discovery: host-dependent",
                "automatic trigger: unavailable",
                "strict hook: unavailable",
                "shared state: available",
                self._manual_message_for(capability),
            ),
        )

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        with guard_state_root(
            self.state_paths.root,
            create=True,
            retained_dirs=("adapters",),
            create_retained=True,
        ) as lease:
            with _retained_lease_update_lock(lease):
                return self._uninstall_locked(options)

    def _uninstall_locked(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed("shared-data removal requires a separate manual action")
        try:
            status = self._read_status()
            if status is None:
                return AdapterResult(
                    self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
                )
            root_value = options.output_dir or self._configured_skill_root
            if root_value is None:
                return self._failed("skill root is required for safe uninstall")
            root = self._safe_skill_root(root_value)
            target = root / _NAME
            if Path(status["managed_directory"]) != target:
                return self._failed("shared status does not match selected skill root")
            if Path(status["skill_root"]) != root:
                return self._failed("shared status does not match selected skill root")
            self._ensure_direct_target(target)
            if not self._managed(target):
                return self._failed("managed ownership cannot be verified")
            manifest = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
            if status["install_id"] != manifest["install_id"]:
                return self._failed("shared status does not match installed package")
            quarantine, moved = self._quarantine_managed_files(target)
            try:
                status_path = self._remove_status()
            except Exception:
                self._restore_quarantine(quarantine, target, moved)
                return self._failed(
                    "adapter status could not be updated; files restored"
                )
            self._discard_quarantine(quarantine)
            self._remove_empty_dirs(target)
            changed = tuple(moved)
            return AdapterResult(
                self.platform,
                "uninstalled",
                CapabilityLevel.MANUAL,
                ("shared personal and project data were preserved",),
                changed + (() if status_path is None else (status_path,)),
            )
        except Exception:
            return self._failed("generic uninstall was not completed")

    def _safe_skill_root(self, value: Path) -> Path:
        root = validate_state_root(value)
        if not root.exists() or not root.is_dir():
            raise ValueError("skill root must be an existing direct directory")
        return root

    @staticmethod
    def _ensure_direct_target(target: Path) -> None:
        if target.exists() or target.is_symlink():
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("skill destination must be a direct directory")

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
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("runtime source must be a direct regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    def _copy_tree(self, source: Path, destination: Path) -> None:
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            target = destination / relative
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("runtime source contains an alias")
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(parents=True, exist_ok=True)
            elif stat.S_ISREG(info.st_mode):
                self._copy_file(path, target)
            else:
                raise ValueError("runtime source contains a non-regular file")

    def _copy_python_tree(self, source: Path, destination: Path) -> None:
        for path in source.rglob("*.py"):
            self._copy_file(path, destination / path.relative_to(source))

    @staticmethod
    def _remove_staging(staging: Path) -> None:
        """Best-effort cleanup which never follows an injected alias."""
        if not staging.exists() or staging.is_symlink():
            return
        for path in sorted(
            staging.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    path.unlink()
                elif stat.S_ISREG(info.st_mode):
                    path.unlink()
                elif stat.S_ISDIR(info.st_mode):
                    path.rmdir()
            except OSError:
                continue
        try:
            staging.rmdir()
        except OSError:
            return

    @staticmethod
    def _validate_staging(staging: Path) -> None:
        """Prove the copied skill can bootstrap without the development checkout."""
        skill = staging / "SKILL.md"
        script = staging / "scripts" / "voice_intent.py"
        for path in (skill, script):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError("staged skill is incomplete")
        with tempfile.TemporaryDirectory(prefix="voice-intent-stage-") as state:
            result = subprocess.run(
                [sys.executable, "-I", str(script), "doctor", "--json"],
                cwd=staging,
                env={"PATH": os.environ.get("PATH", ""), "VOICE_INTENT_HOME": state},
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
        files = sorted(
            str(path.relative_to(target)).replace("\\", "/")
            for path in target.rglob("*")
            if path.is_file()
        )
        hashes = {
            relative: hashlib.sha256((target / relative).read_bytes()).hexdigest()
            for relative in files
        }
        (target / _MANIFEST).write_text(
            json.dumps(
                {
                    "files": files,
                    "format": 2,
                    "hashes": hashes,
                    "install_id": secrets.token_hex(16),
                    "owner": _NAME,
                    "skill_root": str(root),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _managed(target: Path) -> bool:
        try:
            manifest = target / _MANIFEST
            info = os.lstat(manifest)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return False
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            files = GenericAdapter._manifest_files(payload)
            hashes = payload.get("hashes")
            if (
                payload.get("owner") != _NAME
                or payload.get("format") != 2
                or not isinstance(payload.get("install_id"), str)
                or not isinstance(hashes, dict)
                or files is None
                or set(hashes) != set(files)
            ):
                return False
            return all(
                isinstance(hashes[name], str)
                and hashlib.sha256((target / name).read_bytes()).hexdigest()
                == hashes[name]
                for name in files
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def _write_status(self, target: Path, options: InstallOptions) -> Path:
        capability = self._capability(options)
        manifest = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
        payload = json.dumps(
            {
                "capability": capability.value,
                "install_id": manifest["install_id"],
                "managed_directory": str(target),
                "skill_root": manifest["skill_root"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with guard_state_root(
            self.state_paths.root,
            create=True,
            retained_dirs=("adapters",),
            create_retained=True,
        ) as lease:
            lease.write_bytes_atomic(_STATUS_RELATIVE, payload)
        return self.state_paths.adapter_status_file(self.platform)

    def _read_status(self) -> dict[str, str] | None:
        with guard_state_root(
            self.state_paths.root, retained_dirs=("adapters",)
        ) as lease:
            if not lease.available("adapters") or not lease.exists(_STATUS_RELATIVE):
                return None
            raw = lease.read_bytes(_STATUS_RELATIVE, 16 * 1024, "adapter status")
        payload = json.loads(raw.decode("utf-8"))
        if set(payload) != {
            "capability",
            "install_id",
            "managed_directory",
            "skill_root",
        }:
            raise ValueError("invalid adapter status")
        return payload

    def _remove_status(self) -> Path | None:
        with guard_state_root(
            self.state_paths.root, retained_dirs=("adapters",)
        ) as lease:
            if not lease.available("adapters") or not lease.exists(_STATUS_RELATIVE):
                return None
            lease.unlink(_STATUS_RELATIVE)
        return self.state_paths.adapter_status_file(self.platform)

    @staticmethod
    def _managed_files(target: Path) -> tuple[Path, ...]:
        payload = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
        files = GenericAdapter._manifest_files(payload)
        if files is None:
            raise ValueError("invalid installer manifest")
        return tuple(target / Path(relative) for relative in files) + (
            target / _MANIFEST,
        )

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
            if normalized in files:
                return None
            files.append(normalized)
        return tuple(files)

    def _quarantine_managed_files(self, target: Path) -> tuple[Path, tuple[Path, ...]]:
        """Preflight every owned file, then atomically move it aside for rollback."""
        paths = self._managed_files(target)
        for path in paths:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError("managed file ownership cannot be verified")
        quarantine = target.parent / f".{_NAME}.quarantine-{secrets.token_hex(12)}"
        quarantine.mkdir(mode=0o700)
        moved: list[Path] = []
        try:
            for path in paths:
                destination = quarantine / path.relative_to(target)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)
                moved.append(path)
        except Exception:
            self._restore_quarantine(quarantine, target, tuple(moved))
            raise
        return quarantine, tuple(moved)

    @staticmethod
    def _restore_quarantine(
        quarantine: Path, target: Path, moved: tuple[Path, ...]
    ) -> None:
        for path in reversed(moved):
            try:
                source = quarantine / path.relative_to(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, path)
            except OSError:
                continue

    @staticmethod
    def _discard_quarantine(quarantine: Path) -> None:
        for path in sorted(
            quarantine.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                path.unlink()
            elif stat.S_ISDIR(info.st_mode):
                path.rmdir()
        quarantine.rmdir()

    @staticmethod
    def _remove_empty_dirs(target: Path) -> None:
        for path in sorted(
            target.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if path.is_dir() and not path.is_symlink():
                try:
                    path.rmdir()
                except OSError:
                    continue
        try:
            target.rmdir()
        except OSError:
            return

    def _remove_managed_files(self, target: Path) -> tuple[Path, ...]:
        changed: list[Path] = []
        for path in self._managed_files(target):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError("managed file ownership cannot be verified")
            path.unlink()
            changed.append(path)
        for directory in sorted(
            (path for path in target.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                continue
        try:
            target.rmdir()
        except OSError:
            pass
        return tuple(changed)

    @staticmethod
    def _capability(options: InstallOptions) -> CapabilityLevel:
        return (
            CapabilityLevel.IMPLICIT
            if options.implicit_invocation_confirmed
            else CapabilityLevel.MANUAL
        )

    def _failed(self, message: str) -> AdapterResult:
        return AdapterResult(
            self.platform, "failed", CapabilityLevel.UNAVAILABLE, (message,)
        )

    @staticmethod
    def _manual_message(options: InstallOptions) -> str:
        return GenericAdapter._manual_message_for(GenericAdapter._capability(options))

    @staticmethod
    def _manual_message_for(capability: CapabilityLevel) -> str:
        if capability is CapabilityLevel.IMPLICIT:
            return "manual action: host-confirmed description-based invocation"
        return "manual action: invoke this skill through the host"

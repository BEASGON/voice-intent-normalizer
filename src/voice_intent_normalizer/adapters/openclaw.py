"""OpenClaw skill installation through its public command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..paths import StatePaths
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions

_NAME = "voice-intent-normalizer"
_STATUS_FORMAT = 1
_MAX_JSON_BYTES = 1024 * 1024
_MAX_SOURCE_FILES = 4096
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x400

_Run = Callable[[tuple[str, ...]], object]


class OpenClawAdapter:
    """Manage this skill without relying on OpenClaw's private directories.

    OpenClaw owns its installation root.  This adapter stores only a small,
    private ownership receipt in ``VOICE_INTENT_HOME`` and invokes its public
    argument-array CLI; it never opens a shell or treats a Codex skills
    directory as an OpenClaw destination.
    """

    platform = "openclaw"

    def __init__(
        self,
        repository: str | Path,
        state_paths: StatePaths,
        *,
        run: _Run | None = None,
    ) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths
        self._run = self._subprocess_run if run is None else run

    def detect(self) -> AdapterResult:
        return self.doctor()

    def doctor(self) -> AdapterResult:
        try:
            skill = self._checked_skill()
        except FileNotFoundError:
            return self._unavailable("OpenClaw CLI is not available")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                ("OpenClaw CLI verification failed",),
            )
        if skill is None:
            return AdapterResult(
                self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
            )
        return AdapterResult(
            self.platform,
            "installed",
            CapabilityLevel.IMPLICIT,
            ("OpenClaw reports voice-intent-normalizer as eligible",),
        )

    def install(self, options: InstallOptions) -> AdapterResult:
        if options.strict:
            return self._failed("strict installation is unavailable for OpenClaw")
        try:
            source = self._source()
            receipt = self._read_receipt()
            previous = self._checked_skill() if receipt is not None else None
            previous_digest = None if receipt is None else receipt["source_digest"]
            source_digest = self._source_digest(source)
            if (
                receipt is not None
                and previous is not None
                and receipt["source"] == os.fspath(source)
                and previous_digest == source_digest
                and self._scope_matches(receipt, options)
            ):
                return AdapterResult(
                    self.platform,
                    "already-installed",
                    CapabilityLevel.IMPLICIT,
                    ("OpenClaw reports the managed skill as eligible",),
                )
            if receipt is not None:
                self._backup_receipt()
            command = ["openclaw", "skills", "install", os.fspath(source)]
            command.extend(("--as", _NAME))
            if options.workspace is None:
                command.append("--global")
            self._require_success(tuple(command), "OpenClaw skill installation")
            skill = self._checked_skill()
            if skill is None:
                raise ValueError("OpenClaw did not report an eligible installed skill")
            receipt_payload = self._receipt_payload(
                source=source,
                source_digest=source_digest,
                workspace=options.workspace,
                target=self._skill_target(skill),
            )
            self._write_receipt(receipt_payload)
            return AdapterResult(
                self.platform,
                "upgraded" if receipt is not None else "installed",
                CapabilityLevel.IMPLICIT,
                self._install_messages(options.workspace is None),
                (self._receipt_path(),),
            )
        except FileNotFoundError:
            return self._unavailable("OpenClaw CLI is not available")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self._failed("OpenClaw installation was not completed")

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed("shared data was not removed")
        try:
            receipt = self._read_receipt()
            if receipt is None:
                return AdapterResult(
                    self.platform,
                    "not-installed",
                    CapabilityLevel.UNAVAILABLE,
                    ("manual fallback: no verified managed OpenClaw target",),
                )
            skill = self._checked_skill()
            if self._uninstall_is_available():
                self._require_success(
                    ("openclaw", "skills", "uninstall", _NAME),
                    "OpenClaw skill removal",
                )
                if self._checked_skill() is not None:
                    raise ValueError("OpenClaw still reports the skill as eligible")
                self._remove_receipt()
                return AdapterResult(
                    self.platform,
                    "uninstalled",
                    CapabilityLevel.UNAVAILABLE,
                    ("shared personal and project data were preserved",),
                )
            if skill is not None and self._remove_verified_target(receipt, skill):
                self._remove_receipt()
                return AdapterResult(
                    self.platform,
                    "uninstalled",
                    CapabilityLevel.UNAVAILABLE,
                    (
                        "OpenClaw has no official uninstall command; removed the "
                        "verified managed target",
                    ),
                )
            return AdapterResult(
                self.platform,
                "not-installed" if skill is None else "degraded",
                CapabilityLevel.UNAVAILABLE,
                (
                    "manual fallback: this OpenClaw version has no official "
                    "uninstall command and no verified managed target was removed",
                ),
            )
        except FileNotFoundError:
            return self._unavailable("OpenClaw CLI is not available")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self._failed(
                "OpenClaw uninstall was not completed; manual action is required"
            )

    @staticmethod
    def _subprocess_run(args: tuple[str, ...]) -> object:
        return subprocess.run(
            args,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _checked_skill(self) -> Mapping[str, object] | None:
        result = self._command(("openclaw", "skills", "check", "--json"))
        self._require_returncode(result, "OpenClaw skill check")
        payload = self._json_stdout(result, "OpenClaw skill check")
        if set(payload) != {"skills"} or not isinstance(payload["skills"], list):
            raise ValueError("OpenClaw skill check JSON is invalid")
        found: Mapping[str, object] | None = None
        for item in payload["skills"]:
            if not isinstance(item, Mapping):
                raise ValueError("OpenClaw skill entry is invalid")
            if item.get("name") != _NAME:
                continue
            if found is not None:
                raise ValueError("OpenClaw reported the managed skill more than once")
            if type(item.get("eligible")) is not bool:
                raise ValueError("OpenClaw skill eligibility is invalid")
            found = dict(item)
        if found is None:
            return None
        return found if found["eligible"] else None

    def _uninstall_is_available(self) -> bool:
        result = self._command(("openclaw", "skills", "--help"))
        self._require_returncode(result, "OpenClaw skills help")
        stdout = self._stdout(result)
        return any(token == "uninstall" for token in stdout.split())

    def _command(self, args: tuple[str, ...]) -> object:
        result = self._run(args)
        if result is None:
            raise ValueError("OpenClaw CLI returned no result")
        return result

    @staticmethod
    def _returncode(result: object) -> int:
        value = result.get("returncode") if isinstance(result, Mapping) else getattr(
            result, "returncode", None
        )
        if type(value) is not int:
            raise ValueError("OpenClaw CLI result is invalid")
        return value

    @classmethod
    def _require_returncode(cls, result: object, label: str) -> None:
        if cls._returncode(result) != 0:
            raise ValueError(f"{label} failed")

    def _require_success(self, args: tuple[str, ...], label: str) -> None:
        self._require_returncode(self._command(args), label)

    @staticmethod
    def _stdout(result: object) -> str:
        value = result.get("stdout") if isinstance(result, Mapping) else getattr(
            result, "stdout", None
        )
        if not isinstance(value, str):
            raise ValueError("OpenClaw CLI stdout is invalid")
        return value

    @classmethod
    def _json_stdout(cls, result: object, label: str) -> Mapping[str, object]:
        source = cls._stdout(result)
        if len(source.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError(f"{label} JSON is too large")
        try:
            payload = json.loads(
                source,
                object_pairs_hook=cls._reject_duplicate_keys,
                parse_constant=cls._reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{label} JSON is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"{label} JSON is invalid")
        return payload

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def _source(self) -> Path:
        source = self.repository
        self._require_direct_directory(source, "OpenClaw skill source")
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        protected = (
            Path(codex_home) if codex_home else Path.home() / ".codex"
        ) / "skills"
        if self._same_path(source, protected):
            raise ValueError("Codex native skills directory is not an OpenClaw root")
        return source

    def _source_digest(self, source: Path) -> str:
        digest = hashlib.sha256()
        total = 0
        files = 0
        pending = [source]
        while pending:
            directory = pending.pop()
            self._require_direct_directory(directory, "OpenClaw skill source")
            for entry in os.scandir(directory):
                path = Path(entry.path)
                if path == self.state_paths.root:
                    continue
                info = path.lstat()
                if self._is_alias(info):
                    raise ValueError("OpenClaw skill source contains an alias")
                relative = path.relative_to(source).as_posix().encode("utf-8")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    total += info.st_size
                    if files > _MAX_SOURCE_FILES or total > _MAX_SOURCE_BYTES:
                        raise ValueError("OpenClaw skill source exceeds limits")
                    data = path.read_bytes()
                    if len(data) != info.st_size:
                        raise ValueError("OpenClaw skill source changed while read")
                    digest.update(b"F\0" + relative + b"\0" + data)
                else:
                    raise ValueError("OpenClaw skill source contains an unsafe entry")
        return digest.hexdigest()

    def _read_receipt(self) -> dict[str, object] | None:
        path = self._receipt_path()
        if not path.exists():
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or self._is_alias(info):
            raise ValueError("OpenClaw adapter receipt is unsafe")
        return self._validate_receipt(path.read_bytes())

    def _receipt_path(self) -> Path:
        return self.state_paths.adapter_status_file(self.platform)

    def _backup_receipt(self) -> None:
        source = self._receipt_path()
        receipt = self._read_receipt()
        if receipt is None:
            return
        backup = source.with_name(f"{source.name}.bak")
        self._write_bytes_atomic(backup, source.read_bytes())

    def _write_receipt(self, value: dict[str, object]) -> None:
        self._write_bytes_atomic(
            self._receipt_path(),
            (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )

    def _remove_receipt(self) -> None:
        path = self._receipt_path()
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or self._is_alias(info):
                raise ValueError("OpenClaw adapter receipt is unsafe")
            path.unlink()

    def _receipt_payload(
        self,
        *,
        source: Path,
        source_digest: str,
        workspace: Path | None,
        target: str | None,
    ) -> dict[str, object]:
        return {
            "format": _STATUS_FORMAT,
            "scope": "global" if workspace is None else "workspace",
            "source": os.fspath(source),
            "source_digest": source_digest,
            "target": target,
        }

    def _validate_receipt(self, source: bytes) -> dict[str, object]:
        if len(source) > _MAX_JSON_BYTES:
            raise ValueError("OpenClaw adapter receipt is too large")
        payload = self._json_stdout(
            {"returncode": 0, "stdout": source.decode("utf-8")},
            "OpenClaw adapter receipt",
        )
        if set(payload) != {"format", "scope", "source", "source_digest", "target"}:
            raise ValueError("OpenClaw adapter receipt is invalid")
        if (
            payload["format"] != _STATUS_FORMAT
            or payload["scope"] not in {"global", "workspace"}
            or not isinstance(payload["source"], str)
            or not isinstance(payload["source_digest"], str)
            or len(payload["source_digest"]) != 64
            or any(
                char not in "0123456789abcdef"
                for char in payload["source_digest"]
            )
            or payload["target"] is not None
            and not isinstance(payload["target"], str)
        ):
            raise ValueError("OpenClaw adapter receipt is invalid")
        return dict(payload)

    @staticmethod
    def _skill_target(skill: Mapping[str, object]) -> str | None:
        target = skill.get("path")
        return target if isinstance(target, str) else None

    @staticmethod
    def _scope_matches(receipt: Mapping[str, object], options: InstallOptions) -> bool:
        expected = "global" if options.workspace is None else "workspace"
        return receipt["scope"] == expected

    def _remove_verified_target(
        self, receipt: Mapping[str, object], skill: Mapping[str, object]
    ) -> bool:
        target = receipt.get("target")
        if not isinstance(target, str) or target != self._skill_target(skill):
            return False
        path = Path(target)
        self._require_direct_directory(path, "OpenClaw managed target")
        if self._same_path(path, self.repository):
            return False
        self._remove_direct_tree(path)
        return True

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def _remove_direct_tree(cls, root: Path) -> None:
        cls._tree_bytes(root)
        shutil.rmtree(root)

    @classmethod
    def _tree_bytes(cls, root: Path) -> dict[str, bytes]:
        cls._require_direct_directory(root, "OpenClaw managed target")
        result: dict[str, bytes] = {}
        pending = [root]
        while pending:
            directory = pending.pop()
            cls._require_direct_directory(directory, "OpenClaw managed target")
            for entry in os.scandir(directory):
                path = Path(entry.path)
                info = path.lstat()
                if cls._is_alias(info):
                    raise ValueError("OpenClaw managed target contains an alias")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    result[path.relative_to(root).as_posix()] = path.read_bytes()
                else:
                    raise ValueError("OpenClaw managed target contains an unsafe entry")
        return result

    @staticmethod
    def _require_direct_directory(path: Path, label: str) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or OpenClawAdapter._is_alias(info):
            raise ValueError(f"{label} is not a direct directory")

    @staticmethod
    def _is_alias(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            left_path = os.path.normcase(os.fspath(left.resolve(strict=True)))
            right_path = os.path.normcase(
                os.fspath(right.expanduser().resolve(strict=True))
            )
            return left_path == right_path
        except OSError:
            return False

    def _unavailable(self, message: str) -> AdapterResult:
        return AdapterResult(
            self.platform, "unavailable", CapabilityLevel.UNAVAILABLE, (message,)
        )

    def _failed(self, message: str) -> AdapterResult:
        return AdapterResult(
            self.platform, "failed", CapabilityLevel.UNAVAILABLE, (message,)
        )

    @staticmethod
    def _install_messages(global_scope: bool) -> tuple[str, ...]:
        scope = "global" if global_scope else "workspace"
        return (
            f"OpenClaw {scope} skill installation verified as eligible",
            "OpenClaw may require a new session or skill refresh before discovery",
        )

"""Deterministic WorkBuddy manual-import package adapter."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions
from .generic_layout import generation_source_files

_ARCHIVE = "voice-intent-normalizer-workbuddy.zip"
_SIDECAR = f"{_ARCHIVE}.sha256"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WINDOWS_REPARSE_POINT = 0x400


class WorkBuddyAdapter:
    """Build a local package; WorkBuddy import remains user-controlled."""

    platform = "workbuddy"

    def __init__(self, repository: str | Path, state_paths: object) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths
        self._confirmed = False

    def detect(self) -> AdapterResult:
        return self.doctor()

    def doctor(self) -> AdapterResult:
        if self._confirmed:
            return AdapterResult(
                self.platform,
                "confirmed",
                CapabilityLevel.MANUAL,
                ("visible WorkBuddy skill test was confirmed by the user",),
            )
        return AdapterResult(
            self.platform,
            "manual-action-required",
            CapabilityLevel.MANUAL,
            (
                "Run the visible WorkBuddy skill test, then provide explicit "
                "confirmation; this adapter does not inspect client files",
            ),
        )

    def install(self, options: InstallOptions) -> AdapterResult:
        if options.strict:
            return self._failed("strict installation is unavailable for WorkBuddy")
        try:
            output = self._output_dir(options)
            sources = generation_source_files(self.repository)
            archive = output / _ARCHIVE
            self._write_archive(archive, sources)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self._write_bytes_atomic(output / _SIDECAR, f"{digest}\n".encode("ascii"))
            self._confirmed = options.implicit_invocation_confirmed
            return AdapterResult(
                self.platform,
                "package-created",
                CapabilityLevel.MANUAL,
                self._instructions(),
                (archive, output / _SIDECAR),
            )
        except (OSError, TypeError, ValueError, zipfile.BadZipFile):
            return self._failed("WorkBuddy package was not created")

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed("shared data was not removed")
        return AdapterResult(
            self.platform,
            "manual-action-required",
            CapabilityLevel.MANUAL,
            (
                "In WorkBuddy, open Skills, disable or uninstall the uploaded "
                "skill. The shared personal lexicon is preserved.",
            ),
        )

    def _output_dir(self, options: InstallOptions) -> Path:
        output = (
            self.repository / "dist"
            if options.output_dir is None
            else options.output_dir
        )
        output = Path(output).absolute()
        output.mkdir(parents=True, exist_ok=True)
        info = output.lstat()
        if not stat.S_ISDIR(info.st_mode) or self._is_alias(info):
            raise ValueError("WorkBuddy output directory is unsafe")
        return output

    def _write_archive(self, archive: Path, sources: dict[str, bytes]) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{archive.name}-", dir=archive.parent
        )
        os.close(descriptor)
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as bundle:
                for name, data in sorted(sources.items()):
                    self._validate_archive_name(name)
                    entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    bundle.writestr(
                        entry, data, compress_type=zipfile.ZIP_DEFLATED
                    )
            os.replace(temporary, archive)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate_archive_name(name: str) -> None:
        parts = name.split("/")
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("WorkBuddy archive file name is unsafe")

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
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

    @staticmethod
    def _is_alias(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
        )

    def _failed(self, message: str) -> AdapterResult:
        return AdapterResult(
            self.platform, "failed", CapabilityLevel.UNAVAILABLE, (message,)
        )

    @staticmethod
    def _instructions() -> tuple[str, ...]:
        return (
            "WorkBuddy → Skills → Add Skill → Upload Skill → choose the ZIP → "
            "enable → run: 打开 OpenClaw 技能并纠正 open cloud",
        )

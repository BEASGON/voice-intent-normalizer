"""Deterministic WorkBuddy manual-import package adapter."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from ..paths import guard_state_root
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions
from .generic_layout import generation_source_files

_ARCHIVE = "voice-intent-normalizer-workbuddy.zip"
_SIDECAR = f"{_ARCHIVE}.sha256"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


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
            archive_bytes = self._archive_bytes(sources)
            digest = hashlib.sha256(archive_bytes).hexdigest()
            with guard_state_root(output, create=True) as lease:
                if not lease.root_exists:
                    raise ValueError("WorkBuddy output directory is unavailable")
                lease.write_bytes_atomic(_ARCHIVE, archive_bytes)
                lease.write_bytes_atomic(_SIDECAR, f"{digest}\n".encode("ascii"))
                lease.fsync_directory()
                archive = lease.root / _ARCHIVE
                sidecar = lease.root / _SIDECAR
            self._confirmed = options.implicit_invocation_confirmed
            return AdapterResult(
                self.platform,
                "package-created",
                CapabilityLevel.MANUAL,
                self._instructions(),
                (archive, sidecar),
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
        return Path(output).absolute()

    def _archive_bytes(self, sources: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as bundle:
            for name, data in sorted(sources.items()):
                self._validate_archive_name(name)
                entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                bundle.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
        return buffer.getvalue()

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

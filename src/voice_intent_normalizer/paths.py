"""Read-only shared-state path resolution for voice intent normalization."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DIRECT_STATE_ROOT_ERROR = (
    "state root rejected: direct canonical local path required"
)
_WINDOWS_REPARSE_POINT = 0x400


class StateRootValidationError(ValueError):
    """The configured state root is not a direct canonical local path."""


def validate_state_root(root: str | Path) -> Path:
    """Validate V1's direct-path state-root contract without creating anything."""
    supplied = Path(root)
    raw = os.fspath(supplied)
    if os.name == "nt":
        normalized = _validate_windows_root_spelling(raw)
    else:
        normalized = _validate_posix_root_spelling(raw)
    existing = _reject_alias_components(Path(normalized))
    if os.name == "nt":
        final_path = _windows_final_path(existing)
        if _windows_path_key(final_path) != _windows_path_key(existing):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    return Path(normalized)


def _validate_windows_root_spelling(raw: str) -> str:
    import ntpath

    path = raw.replace("/", "\\")
    folded = path.casefold()
    if folded.startswith("\\\\?\\unc\\") or folded.startswith("\\\\.\\"):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    if folded.startswith("\\\\?\\"):
        path = path[4:]
        if len(path) < 3 or path[1:3] != ":\\" or not path[0].isalpha():
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    elif path.startswith("\\\\"):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    if not ntpath.isabs(path):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    normalized = ntpath.normpath(path)
    if ntpath.normcase(path) != ntpath.normcase(normalized):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    drive, tail = ntpath.splitdrive(normalized)
    if any(
        component.endswith((" ", "."))
        for component in tail.split("\\")
        if component
    ):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    if not drive or _windows_drive_type(f"{drive}\\") in {0, 1, 4}:
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    return normalized


def _validate_posix_root_spelling(raw: str) -> str:
    if not os.path.isabs(raw) or raw.startswith("//"):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    normalized = os.path.normpath(raw)
    if raw != normalized:
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    return normalized


def _reject_alias_components(path: Path) -> Path:
    current = Path(path.anchor)
    existing = current
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return existing
        except OSError as exc:
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR) from exc
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & _WINDOWS_REPARSE_POINT
        ):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
        existing = current
    return existing


def _windows_path_key(path: str | Path) -> str:
    import ntpath

    value = os.fspath(path).replace("/", "\\")
    folded = value.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif folded.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _windows_drive_type(root: str) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return int(kernel32.GetDriveTypeW(root))


def _windows_final_path(path: Path) -> Path:
    """Return the no-follow handle's canonical DOS path and reject reparse data."""
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        os.fspath(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)

    failure: OSError | None = None
    final_path: Path | None = None
    try:
        attributes = _FileAttributeTagInfo()
        ctypes.set_last_error(0)
        if not kernel32.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            not attributes.file_attributes & 0x10  # FILE_ATTRIBUTE_DIRECTORY
            or attributes.file_attributes & _WINDOWS_REPARSE_POINT
        ):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        length = kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), 0
        )
        if length == 0 or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error() or 206)
        final_path = Path(buffer.value)
    except OSError as exc:
        failure = exc
    finally:
        ctypes.set_last_error(0)
        if not kernel32.CloseHandle(handle) and failure is None:
            failure = ctypes.WinError(ctypes.get_last_error())
    if failure is not None:
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR) from failure
    if final_path is None:
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
    return final_path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """All shared-state paths belonging to one normalized project root."""

    project_root: Path
    project_id: str
    root: Path

    @property
    def lexicon_file(self) -> Path:
        """Return the project lexicon location without creating it."""
        return self.root / "project.jsonl"

    @property
    def scan_state_file(self) -> Path:
        """Return the project scanner state location without creating it."""
        return self.root / "scan-state.json"


@dataclass(frozen=True, slots=True)
class StatePaths:
    """Direct canonical locations for shared state, resolved without mutation."""

    root: Path

    @classmethod
    def resolve(
        cls,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
    ) -> StatePaths:
        """Resolve state root from ``VOICE_INTENT_HOME`` or the user home directory."""
        environment = os.environ if environ is None else environ
        configured = environment.get("VOICE_INTENT_HOME", "")
        if configured and configured.strip():
            root = Path(configured).expanduser()
        else:
            base_home = Path.home() if home is None else Path(home)
            root = base_home.expanduser() / ".voice-intent-normalizer"
        return cls(root=validate_state_root(root))

    @property
    def personal_file(self) -> Path:
        """Return the personal lexicon location without creating it."""
        return self.root / "personal.jsonl"

    @property
    def preferences_file(self) -> Path:
        """Return the preferences location without creating it."""
        return self.root / "preferences.json"

    @property
    def hotwords_file(self) -> Path:
        """Return the downloaded public-hotword lexicon without creating it."""
        return self.root / "hotwords" / "zh-ai.jsonl"

    def adapter_status_file(self, adapter: str) -> Path:
        """Return one adapter's status document location without creating it."""
        return self.root / "adapters" / f"{adapter}.json"

    def for_project(self, project_root: str | Path) -> ProjectPaths:
        """Resolve deterministic project paths without creating them."""
        normalized_root = Path(project_root).expanduser().resolve()
        project_id = hashlib.sha256(
            str(normalized_root).encode("utf-8")
        ).hexdigest()[:16]
        return ProjectPaths(
            project_root=normalized_root,
            project_id=project_id,
            root=self.root / "projects" / project_id,
        )

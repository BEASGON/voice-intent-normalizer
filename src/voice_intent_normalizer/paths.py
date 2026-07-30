"""Read-only shared-state path resolution for voice intent normalization."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

_DIRECT_STATE_ROOT_ERROR = (
    "state root rejected: direct canonical local path required"
)
_WINDOWS_REPARSE_POINT = 0x400


class StateRootValidationError(ValueError):
    """The configured state root is not a direct canonical local path."""


@dataclass(frozen=True, slots=True)
class StateRootLease:
    """Retained direct directory identities for one protected state operation."""

    configured_root: Path
    root: Path
    exists: bool
    _directories: Mapping[tuple[str, ...], Path]

    def path(self, relative: str | Path) -> Path:
        """Resolve a state-relative path beneath its deepest retained directory."""
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("state path must be relative")
        parts = tuple(part for part in candidate.parts if part not in ("", "."))
        for length in range(len(parts), -1, -1):
            prefix = parts[:length]
            retained = self._directories.get(prefix)
            if retained is not None:
                return retained.joinpath(*parts[length:])
        return self.root.joinpath(*parts)


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


def state_root_identity(root: str | Path) -> tuple[int, int] | None:
    """Return the validated configured directory identity without creating it."""
    validated = validate_state_root(root)
    try:
        info = os.stat(validated, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


@contextmanager
def guard_state_root(
    root: str | Path,
    *,
    create: bool = False,
    retained_dirs: Sequence[str | Path] = (),
    create_retained: bool = False,
) -> Iterator[StateRootLease]:
    """Retain direct root identities and bind requested subdirectories to them."""
    validated = validate_state_root(root)
    requested = tuple(_relative_directory_parts(value) for value in retained_dirs)
    if os.name == "nt":
        with _guard_windows_state_root(
            validated,
            create=create,
            retained_dirs=requested,
            create_retained=create_retained,
        ) as lease:
            yield lease
        return
    with _guard_posix_state_root(
        validated,
        create=create,
        retained_dirs=requested,
        create_retained=create_retained,
    ) as lease:
        yield lease


def _relative_directory_parts(value: str | Path) -> tuple[str, ...]:
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise ValueError("retained state directory must be a non-empty relative path")
    return tuple(candidate.parts)


@contextmanager
def _guard_windows_state_root(
    root: Path,
    *,
    create: bool,
    retained_dirs: tuple[tuple[str, ...], ...],
    create_retained: bool,
) -> Iterator[StateRootLease]:
    missing: list[str] = []
    existing = root
    while True:
        try:
            os.lstat(existing)
            break
        except FileNotFoundError:
            if not create:
                yield StateRootLease(root, root, False, {})
                return
            if existing == existing.parent:
                raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
            missing.append(existing.name)
            existing = existing.parent

    with ExitStack() as guards:
        guards.enter_context(_windows_directory_guard(existing))
        current = existing
        for component in reversed(missing):
            current /= component
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            guards.enter_context(_windows_directory_guard(current))
        if current != root:
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)

        retained: dict[tuple[str, ...], Path] = {(): root}
        for parts in sorted(set(retained_dirs), key=lambda item: (len(item), item)):
            current = root
            prefix: tuple[str, ...] = ()
            available = True
            for component in parts:
                prefix += (component,)
                if prefix in retained:
                    current = retained[prefix]
                    continue
                current /= component
                if create_retained:
                    try:
                        os.mkdir(current)
                    except FileExistsError:
                        pass
                try:
                    guards.enter_context(_windows_directory_guard(current))
                except FileNotFoundError:
                    available = False
                    break
                retained[prefix] = current
            if not available:
                continue
        validate_state_root(root)
        yield StateRootLease(root, root, True, retained)


@contextmanager
def _windows_directory_guard(path: Path) -> Iterator[None]:
    """Retain a direct Windows directory handle that denies deletion."""
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
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        _extended_windows_path(path),
        0x10080,  # DELETE | FILE_READ_ATTRIBUTES
        0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, os.strerror(error), path)
        raise ctypes.WinError(error)

    active_exception = False
    try:
        attributes = _FileAttributeTagInfo()
        ctypes.set_last_error(0)
        if not kernel32.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not attributes.file_attributes & 0x10 or (
            attributes.file_attributes & _WINDOWS_REPARSE_POINT
        ):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
        yield
    except BaseException:
        active_exception = True
        raise
    finally:
        ctypes.set_last_error(0)
        if not kernel32.CloseHandle(handle) and not active_exception:
            raise ctypes.WinError(ctypes.get_last_error())


def _extended_windows_path(value: str | Path) -> str:
    absolute = os.path.abspath(os.fspath(value)).replace("/", "\\")
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


@contextmanager
def _guard_posix_state_root(
    root: Path,
    *,
    create: bool,
    retained_dirs: tuple[tuple[str, ...], ...],
    create_retained: bool,
) -> Iterator[StateRootLease]:
    flags = os.O_RDONLY
    for required_flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, required_flag):
            raise OSError(f"secure POSIX state roots require {required_flag}")
        flags |= getattr(os, required_flag)

    descriptors: list[int] = []
    retained_fds: dict[tuple[str, ...], int] = {}
    acquired = False
    try:
        descriptor = os.open(root.anchor, flags)
        descriptors.append(descriptor)
        for component in root.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    yield StateRootLease(root, root, False, {})
                    return
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            _verify_posix_directory(child)
            descriptors.append(child)
            descriptor = child
        retained_fds[()] = descriptor

        for parts in sorted(set(retained_dirs), key=lambda item: (len(item), item)):
            current = retained_fds[()]
            prefix: tuple[str, ...] = ()
            available = True
            for component in parts:
                prefix += (component,)
                known = retained_fds.get(prefix)
                if known is not None:
                    current = known
                    continue
                if create_retained:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    available = False
                    break
                _verify_posix_directory(child)
                descriptors.append(child)
                retained_fds[prefix] = child
                current = child
            if not available:
                continue

        configured_info = os.stat(root, follow_symlinks=False)
        descriptor_info = os.fstat(retained_fds[()])
        if (
            configured_info.st_dev,
            configured_info.st_ino,
        ) != (descriptor_info.st_dev, descriptor_info.st_ino):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
        bound = {
            parts: _posix_descriptor_path(fd)
            for parts, fd in retained_fds.items()
        }
        acquired = True
        yield StateRootLease(root, bound[()], True, bound)
    except (NotADirectoryError, OSError) as exc:
        if acquired:
            raise
        if isinstance(exc, StateRootValidationError):
            raise
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_posix_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)


def _posix_descriptor_path(descriptor: int) -> Path:
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        if base.is_dir():
            return base / str(descriptor)
    raise OSError("secure POSIX state roots require descriptor filesystem paths")


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

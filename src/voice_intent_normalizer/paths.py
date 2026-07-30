"""Read-only shared-state path resolution for voice intent normalization."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DIRECT_STATE_ROOT_ERROR = (
    "state root rejected: direct canonical local path required"
)
_DIRECT_PROJECT_ROOT_ERROR = (
    "project root rejected: direct canonical local path required"
)
_WINDOWS_REPARSE_POINT = 0x400
_PROJECT_ROOT_AUTHORITY_TOKEN = object()


class StateRootValidationError(ValueError):
    """The configured state root is not a direct canonical local path."""


class ProjectRootValidationError(ValueError):
    """The supplied project root is not a retained direct local directory."""


@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    """One retained directory, represented without a reopenable POSIX path."""

    path: Path | None = None
    descriptor: int | None = None


@dataclass(frozen=True, slots=True)
class StateRootLease:
    """Retained direct directory identities for one protected state operation."""

    configured_root: Path
    root: Path
    root_exists: bool
    captured_lock_key: str
    _directories: Mapping[tuple[str, ...], _DirectoryBinding]
    _unavailable: frozenset[tuple[str, ...]]

    def available(self, relative: str | Path = ".") -> bool:
        """Return whether *relative* remained bound at lease acquisition."""
        if not self.root_exists:
            return False
        parts = _relative_path_parts(relative)
        return not any(
            parts[: len(prefix)] == prefix for prefix in self._unavailable
        )

    def path(self, relative: str | Path) -> Path:
        """Return a guarded Windows path; POSIX callers must use lease I/O."""
        parts = _relative_path_parts(relative)
        if not self.available(relative):
            raise FileNotFoundError(
                f"state path was unavailable when lease was acquired: {relative}"
            )
        binding, remainder = self._binding_for(parts)
        if len(remainder) > 1:
            raise ValueError(
                "state path parent must be explicitly retained"
            )
        if binding.path is None:
            raise OSError(
                "POSIX retained state has no reopenable path; use lease I/O helpers"
            )
        return binding.path.joinpath(*remainder)

    def exists(self, relative: str | Path) -> bool:
        """Check one bound entry without following aliases."""
        if not self.available(relative):
            return False
        try:
            self.stat(relative)
        except FileNotFoundError:
            return False
        return True

    def stat(self, relative: str | Path) -> os.stat_result:
        """Stat one bound entry without following symlinks."""
        parts = _relative_path_parts(relative)
        if not self.available(relative):
            raise FileNotFoundError(
                f"state path was unavailable when lease was acquired: {relative}"
            )
        retained = self._directories.get(parts)
        if retained is not None:
            if retained.path is not None:
                return os.stat(retained.path, follow_symlinks=False)
            if retained.descriptor is not None:
                return os.fstat(retained.descriptor)
            raise OSError("retained state directory has no usable identity")
        binding, name = self._file_binding(parts)
        if binding.path is not None:
            return os.stat(binding.path / name, follow_symlinks=False)
        if binding.descriptor is None:
            raise OSError("retained state directory has no usable identity")
        _require_posix_dir_fd_support()
        return os.stat(name, dir_fd=binding.descriptor, follow_symlinks=False)

    def read_bytes(self, relative: str | Path, limit: int, label: str) -> bytes:
        """Read one exact regular-file identity through a bounded handle."""
        if limit < 0:
            raise ValueError("read limit must be non-negative")
        with self.open_regular(relative, label) as descriptor:
            info = os.fstat(descriptor)
            if info.st_size > limit:
                raise ValueError(f"{label} exceeds size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"{label} exceeds size limit")
                chunks.append(chunk)

    @contextmanager
    def open_regular(
        self, relative: str | Path, label: str = "state file"
    ) -> Iterator[int]:
        """Yield a no-follow descriptor for one exact retained regular file."""
        parts = _relative_path_parts(relative)
        binding, name = self._file_binding(parts)
        descriptor = self._open_regular_file(binding, name, label)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def mkdir(
        self,
        relative: str | Path,
        *,
        mode: int = 0o700,
        exist_ok: bool = False,
    ) -> None:
        """Securely create one component beneath a retained parent directory."""
        parts = _relative_path_parts(relative)
        binding, name = self._file_binding(parts)
        if binding.path is not None:
            try:
                os.mkdir(binding.path / name, mode)
            except FileExistsError:
                if not exist_ok:
                    raise
            info = os.lstat(binding.path / name)
            attributes = getattr(info, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or attributes & _WINDOWS_REPARSE_POINT
            ):
                raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
            return
        if binding.descriptor is None:
            raise OSError("retained state directory has no usable identity")
        _require_posix_dir_fd_support()
        try:
            os.mkdir(name, mode, dir_fd=binding.descriptor)
        except FileExistsError:
            if not exist_ok:
                raise
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        child = os.open(name, flags, dir_fd=binding.descriptor)
        try:
            _verify_posix_directory(child)
        finally:
            os.close(child)

    def write_bytes_atomic(self, relative: str | Path, data: bytes) -> None:
        """Fsync and atomically replace one file inside a retained directory."""
        if not isinstance(data, bytes):
            raise TypeError("state data must be bytes")
        parts = _relative_path_parts(relative)
        binding, name = self._file_binding(parts)
        if binding.path is not None:
            _write_windows_bytes_atomic(binding.path, name, data)
            return
        if binding.descriptor is None:
            raise OSError("retained state directory has no usable identity")
        _write_posix_bytes_atomic(binding.descriptor, name, data)

    def unlink(self, relative: str | Path, *, missing_ok: bool = False) -> None:
        """Remove one bound directory entry without following it."""
        parts = _relative_path_parts(relative)
        binding, name = self._file_binding(parts)
        try:
            if binding.path is not None:
                os.unlink(binding.path / name)
            elif binding.descriptor is not None:
                _require_posix_dir_fd_support()
                os.unlink(name, dir_fd=binding.descriptor)
            else:
                raise OSError("retained state directory has no usable identity")
        except FileNotFoundError:
            if not missing_ok:
                raise

    def listdir(self, relative: str | Path) -> tuple[str, ...]:
        """List an exactly retained directory identity."""
        parts = _relative_path_parts(relative)
        if not self.available(relative):
            raise FileNotFoundError(
                f"state path was unavailable when lease was acquired: {relative}"
            )
        binding = self._directories.get(parts)
        if binding is None:
            raise ValueError("state directory was not retained")
        if binding.path is not None:
            return tuple(os.listdir(binding.path))
        if binding.descriptor is None:
            raise OSError("retained state directory has no usable identity")
        return tuple(os.listdir(binding.descriptor))

    def _binding_for(
        self, parts: tuple[str, ...]
    ) -> tuple[_DirectoryBinding, tuple[str, ...]]:
        if not self.available(Path(*parts) if parts else Path(".")):
            raise FileNotFoundError("state path was unavailable at lease acquisition")
        for length in range(len(parts), -1, -1):
            binding = self._directories.get(parts[:length])
            if binding is not None:
                return binding, parts[length:]
        raise FileNotFoundError("state root identity was not retained")

    def _file_binding(
        self, parts: tuple[str, ...]
    ) -> tuple[_DirectoryBinding, str]:
        if not parts:
            raise ValueError("state file path must not be empty")
        binding, remainder = self._binding_for(parts)
        if len(remainder) != 1:
            raise ValueError(
                "state file parent must be an explicitly retained directory"
            )
        return binding, remainder[0]

    @staticmethod
    def _open_regular_file(
        binding: _DirectoryBinding, name: str, label: str
    ) -> int:
        try:
            if binding.path is not None:
                descriptor = _open_windows_regular_file(binding.path / name)
            elif binding.descriptor is not None:
                _require_posix_dir_fd_support()
                flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
                descriptor = os.open(name, flags, dir_fd=binding.descriptor)
            else:
                raise OSError("retained state directory has no usable identity")
        except OSError as exc:
            raise ValueError(f"unable to read {label}") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError(f"{label} must be a regular file")
        return descriptor


def _relative_path_parts(value: str | Path) -> tuple[str, ...]:
    candidate = Path(value)
    if candidate.anchor or ".." in candidate.parts:
        raise ValueError("state path must be relative")
    return tuple(part for part in candidate.parts if part not in ("", "."))


def _require_posix_dir_fd_support() -> None:
    """Fail closed when Python cannot express descriptor-relative state I/O."""
    # CPython's ``supports_dir_fd`` records the underlying renameat capability
    # under os.rename, although os.replace exposes the same src/dst dir_fd API.
    required = (os.open, os.mkdir, os.stat, os.unlink, os.rename)
    unsupported = [
        operation.__name__
        for operation in required
        if operation not in os.supports_dir_fd
    ]
    if unsupported:
        raise OSError(
            "secure POSIX state roots require dir_fd support for "
            + ", ".join(unsupported)
        )
    if os.listdir not in os.supports_fd:
        raise OSError("secure POSIX state roots require fd support for listdir")
    if not hasattr(os, "replace"):
        raise OSError("secure POSIX state roots require atomic replace")
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, name):
            raise OSError(f"secure POSIX state roots require {name}")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("state write made no progress")
        view = view[written:]


def _write_windows_bytes_atomic(directory: Path, name: str, data: bytes) -> None:
    target = directory / name
    try:
        if stat.S_ISLNK(os.lstat(target).st_mode):
            raise ValueError("update target must not be a symlink")
    except FileNotFoundError:
        pass
    temporary = directory / f".{name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_posix_bytes_atomic(directory: int, name: str, data: bytes) -> None:
    _require_posix_dir_fd_support()
    try:
        target = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(target.st_mode):
            raise ValueError("update target must not be a symlink")
    except FileNotFoundError:
        pass
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def _open_windows_regular_file(path: Path) -> int:
    """Open one no-follow Windows file handle and convert it to a Python fd."""
    if os.name != "nt":
        raise OSError("Windows file opening is unavailable on this platform")
    import ctypes
    import msvcrt
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
        0x80000000,  # GENERIC_READ
        0x7,  # allow readers, writers, and atomic replacement
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, os.strerror(error), path)
        raise ctypes.WinError(error)
    try:
        attributes = _FileAttributeTagInfo()
        ctypes.set_last_error(0)
        if not kernel32.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.file_attributes & (0x10 | _WINDOWS_REPARSE_POINT):
            raise ValueError("state entry must be a direct regular file")
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        handle = None
        return descriptor
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


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


def state_root_lock_key(root: str | Path) -> str:
    """Return the lexical configured-root key used for external coordination."""
    if os.name == "nt":
        return _windows_path_key(root)
    return os.path.normpath(os.path.abspath(os.fspath(root)))


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
    captured_lock_key = state_root_lock_key(validated)
    requested = tuple(_relative_directory_parts(value) for value in retained_dirs)
    if os.name == "nt":
        with _guard_windows_state_root(
            validated,
            captured_lock_key=captured_lock_key,
            create=create,
            retained_dirs=requested,
            create_retained=create_retained,
        ) as lease:
            yield lease
        return
    with _guard_posix_state_root(
        validated,
        captured_lock_key=captured_lock_key,
        create=create,
        retained_dirs=requested,
        create_retained=create_retained,
    ) as lease:
        yield lease


def _relative_directory_parts(value: str | Path) -> tuple[str, ...]:
    candidate = Path(value)
    if (
        candidate.anchor
        or not candidate.parts
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise ValueError("retained state directory must be a non-empty relative path")
    return tuple(candidate.parts)


@contextmanager
def _guard_windows_state_root(
    root: Path,
    *,
    captured_lock_key: str,
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
                yield StateRootLease(
                    root,
                    root,
                    False,
                    captured_lock_key,
                    {},
                    frozenset(retained_dirs),
                )
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

        retained: dict[tuple[str, ...], _DirectoryBinding] = {
            (): _DirectoryBinding(path=root)
        }
        unavailable: set[tuple[str, ...]] = set()
        for parts in sorted(set(retained_dirs), key=lambda item: (len(item), item)):
            current = root
            prefix: tuple[str, ...] = ()
            available = True
            for component in parts:
                prefix += (component,)
                if prefix in retained:
                    retained_path = retained[prefix].path
                    if retained_path is None:
                        raise OSError("Windows directory binding lost its path")
                    current = retained_path
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
                retained[prefix] = _DirectoryBinding(path=current)
            if not available:
                unavailable.add(parts)
                continue
        validate_state_root(root)
        yield StateRootLease(
            root,
            root,
            True,
            captured_lock_key,
            retained,
            frozenset(unavailable),
        )


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
        0x81,  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES; no DELETE access
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
    captured_lock_key: str,
    create: bool,
    retained_dirs: tuple[tuple[str, ...], ...],
    create_retained: bool,
) -> Iterator[StateRootLease]:
    _require_posix_dir_fd_support()
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
                    yield StateRootLease(
                        root,
                        root,
                        False,
                        captured_lock_key,
                        {},
                        frozenset(retained_dirs),
                    )
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
        unavailable: set[tuple[str, ...]] = set()

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
                unavailable.add(parts)
                continue

        configured_info = os.stat(root, follow_symlinks=False)
        descriptor_info = os.fstat(retained_fds[()])
        if (
            configured_info.st_dev,
            configured_info.st_ino,
        ) != (descriptor_info.st_dev, descriptor_info.st_ino):
            raise StateRootValidationError(_DIRECT_STATE_ROOT_ERROR)
        bound = {
            parts: _DirectoryBinding(descriptor=fd)
            for parts, fd in retained_fds.items()
        }
        acquired = True
        yield StateRootLease(
            root,
            root,
            True,
            captured_lock_key,
            bound,
            frozenset(unavailable),
        )
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


def normalize_project_root(project_root: str | Path) -> Path:
    """Return one absolute project spelling without dereferencing any component.

    Project source authority is acquired separately through no-follow handles.
    This helper exists only to produce the direct lexical spelling used by that
    acquisition and by the deterministic project-state identifier.
    """
    raw = os.path.expanduser(os.fspath(project_root))
    if not raw or "\0" in raw:
        raise ValueError("project root must be a direct local path")
    if os.name == "nt":
        return Path(_normalize_windows_project_root(raw))
    return Path(_normalize_posix_project_root(raw))


def _normalize_windows_project_root(raw: str) -> str:
    import ntpath

    path = raw.replace("/", "\\")
    folded = path.casefold()
    if folded.startswith("\\\\?\\unc\\") or folded.startswith("\\\\.\\"):
        raise ValueError("project root must be a direct local path")
    if folded.startswith("\\\\?\\"):
        path = path[4:]
        if len(path) < 3 or path[1:3] != ":\\" or not path[0].isalpha():
            raise ValueError("project root must be a direct local path")
    elif path.startswith("\\\\"):
        raise ValueError("project root must be a direct local path")
    drive, tail = ntpath.splitdrive(path)
    if drive and not tail.startswith("\\"):
        raise ValueError("project root must not be drive-relative")
    if any(part == ".." for part in tail.split("\\")):
        raise ValueError("project root must not contain parent traversal")
    if not ntpath.isabs(path):
        path = ntpath.join(os.getcwd(), path)
    normalized = ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(normalized)
    if (
        not drive
        or not tail.startswith("\\")
        or _windows_drive_type(f"{drive}\\") in {0, 1, 4}
        or any(
            component.endswith((" ", "."))
            for component in tail.split("\\")
            if component
        )
    ):
        raise ValueError("project root must be a direct local path")
    return normalized


def _normalize_posix_project_root(raw: str) -> str:
    if raw.startswith("//") or any(part == ".." for part in raw.split("/")):
        raise ValueError("project root must be a direct local path")
    anchored = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)
    normalized = os.path.normpath(anchored)
    if not os.path.isabs(normalized) or normalized.startswith("//"):
        raise ValueError("project root must be a direct local path")
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


@dataclass(slots=True)
class ProjectRootAuthority:
    """One verified direct project directory retained by an open handle."""

    canonical_root: Path
    project_id: str
    descriptor: int
    identity: tuple[int, int]
    root_key: str | None
    _verification_token: object


@contextmanager
def guard_project_root(
    project_root: str | Path | ProjectRootAuthority,
) -> Iterator[ProjectRootAuthority]:
    """Retain one direct project root without following any path component."""
    if isinstance(project_root, ProjectRootAuthority):
        _verify_project_root_authority(project_root)
        yield project_root
        return
    authority: ProjectRootAuthority | None = None
    try:
        normalized = normalize_project_root(project_root)
        authority = (
            _acquire_windows_project_root(normalized)
            if os.name == "nt"
            else _acquire_posix_project_root(normalized)
        )
    except ProjectRootValidationError:
        raise
    except (OSError, ValueError) as exc:
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR) from exc
    try:
        yield authority
    finally:
        _close_project_root_authority(authority)


def duplicate_project_root_descriptor(authority: ProjectRootAuthority) -> int:
    """Duplicate a verified authority handle for an independent consumer."""
    _verify_project_root_authority(authority)
    return os.dup(authority.descriptor)


def _project_id(canonical_root: Path) -> str:
    return hashlib.sha256(
        str(canonical_root).encode("utf-8")
    ).hexdigest()[:16]


def _acquire_posix_project_root(root: Path) -> ProjectRootAuthority:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    if not root.is_absolute() or root.anchor != "/":
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptor = os.open("/", flags)
        descriptors.append(descriptor)
        for component in root.parts[1:]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        final_descriptor = descriptors[-1]
        info = os.fstat(final_descriptor)
        authority = ProjectRootAuthority(
            canonical_root=root,
            project_id=_project_id(root),
            descriptor=final_descriptor,
            identity=(info.st_dev, info.st_ino),
            root_key=None,
            _verification_token=_PROJECT_ROOT_AUTHORITY_TOKEN,
        )
        descriptors.pop()
        return authority
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _acquire_windows_project_root(root: Path) -> ProjectRootAuthority:
    import ntpath

    normalized = ntpath.normpath(os.fspath(root))
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith("\\"):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    current = Path(f"{drive}\\")
    descriptors: list[int] = []
    final_path: Path | None = None
    final_identity: tuple[int, int] | None = None
    try:
        descriptor, final_path, final_identity = (
            _open_windows_project_directory(current)
        )
        descriptors.append(descriptor)
        for component in (part for part in tail.split("\\") if part):
            current /= component
            descriptor, final_path, final_identity = (
                _open_windows_project_directory(current)
            )
            descriptors.append(descriptor)
        if final_path is None or final_identity is None:
            raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        final_descriptor = descriptors[-1]
        canonical_root = final_path
        authority = ProjectRootAuthority(
            canonical_root=canonical_root,
            project_id=_project_id(canonical_root),
            descriptor=final_descriptor,
            identity=final_identity,
            root_key=_windows_path_key(canonical_root),
            _verification_token=_PROJECT_ROOT_AUTHORITY_TOKEN,
        )
        descriptors.pop()
        return authority
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_windows_project_directory(
    path: Path,
) -> tuple[int, Path, tuple[int, int]]:
    import ctypes
    import msvcrt

    kernel32, attributes_type, information_type = (
        _windows_project_authority_api()
    )
    ctypes.set_last_error(0)
    native_handle = kernel32.CreateFileW(
        _extended_windows_path(path),
        0x81,  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES
        0x3,  # share read/write but deny delete/rename
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if native_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor: int | None = None
    try:
        attributes = attributes_type()
        if not kernel32.GetFileInformationByHandleEx(
            native_handle,
            9,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            not attributes.file_attributes & 0x10
            or attributes.file_attributes & _WINDOWS_REPARSE_POINT
        ):
            raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        information = information_type()
        if not kernel32.GetFileInformationByHandle(
            native_handle, ctypes.byref(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = (
            int(information.volume_serial_number),
            int(
                (information.file_index_high << 32)
                | information.file_index_low
            ),
        )
        final_path = Path(
            _plain_windows_handle_path(
                _windows_handle_path(kernel32, native_handle)
            )
        )
        if _windows_path_key(final_path) != _windows_path_key(path):
            raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        descriptor = msvcrt.open_osfhandle(
            native_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        native_handle = None
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        return descriptor, final_path, identity
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        if native_handle is not None:
            kernel32.CloseHandle(native_handle)


def _verify_project_root_authority(authority: ProjectRootAuthority) -> None:
    if (
        authority._verification_token is not _PROJECT_ROOT_AUTHORITY_TOKEN
        or authority.project_id != _project_id(authority.canonical_root)
        or authority.descriptor < 0
    ):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    try:
        info = os.fstat(authority.descriptor)
    except OSError as exc:
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    if os.name != "nt":
        if (
            authority.root_key is not None
            or (info.st_dev, info.st_ino) != authority.identity
        ):
            raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
        return
    import ctypes
    import msvcrt

    if (
        authority.root_key is None
        or authority.root_key
        != _windows_path_key(authority.canonical_root)
    ):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    kernel32, attributes_type, information_type = (
        _windows_project_authority_api()
    )
    native_handle = msvcrt.get_osfhandle(authority.descriptor)
    attributes = attributes_type()
    if not kernel32.GetFileInformationByHandleEx(
        native_handle,
        9,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if (
        not attributes.file_attributes & 0x10
        or attributes.file_attributes & _WINDOWS_REPARSE_POINT
    ):
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)
    information = information_type()
    if not kernel32.GetFileInformationByHandle(
        native_handle, ctypes.byref(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    identity = (
        int(information.volume_serial_number),
        int(
            (information.file_index_high << 32)
            | information.file_index_low
        ),
    )
    final_key = _windows_path_key(
        _plain_windows_handle_path(
            _windows_handle_path(kernel32, native_handle)
        )
    )
    if identity != authority.identity or final_key != authority.root_key:
        raise ProjectRootValidationError(_DIRECT_PROJECT_ROOT_ERROR)


def _close_project_root_authority(authority: ProjectRootAuthority) -> None:
    descriptor = authority.descriptor
    if descriptor < 0:
        return
    authority.descriptor = -1
    try:
        os.close(descriptor)
    except OSError:
        return


@lru_cache(maxsize=1)
def _windows_project_authority_api():
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
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
    kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, _FileAttributeTagInfo, _ByHandleFileInformation


def _windows_handle_path(kernel32, native_handle: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetFinalPathNameByHandleW(
        native_handle, buffer, len(buffer), 0
    )
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error() or 206)
    return buffer.value


def _plain_windows_handle_path(value: str) -> str:
    folded = value.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if folded.startswith("\\\\?\\"):
        return value[4:]
    return value


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """All shared-state paths belonging to one normalized project root."""

    project_root: Path
    project_id: str
    root: Path

    @property
    def lexicon_file(self) -> Path:
        """Return the learning/curated project lexicon without creating it."""
        return self.root / "project.jsonl"

    @property
    def scan_lexicon_file(self) -> Path:
        """Return the scanner-owned project cache without creating it."""
        return self.root / "project-scan.jsonl"

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

    def for_project(
        self,
        project_root: str | Path | ProjectRootAuthority,
    ) -> ProjectPaths:
        """Derive project state only from a retained direct-root authority."""
        with guard_project_root(project_root) as authority:
            return ProjectPaths(
                project_root=authority.canonical_root,
                project_id=authority.project_id,
                root=self.root / "projects" / authority.project_id,
            )

"""Safe, recoverable retrieval of the public hotword lexicon.

The transport receives only already allowlisted public URLs.  The updater never
sends local lexicons, project data, or user input over the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from .models import Scope
from .paths import StatePaths


class Response(Protocol):
    """One response returned by a transport that does not auto-follow redirects."""

    status: int
    location: str | None

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """A no-follow transport: the updater validates every next redirect URL."""

    def open_no_redirect(self, url: str) -> Response: ...


_ALLOWED_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"}
)
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_DATA_BYTES = 10 * 1024 * 1024
_MAX_URL_LENGTH = 4096
_MAX_REDIRECTS = 10
_MAX_RETAINED_PAYLOADS = 4
_UPDATE_INTERVAL = timedelta(days=1)
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.02
_STATE_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = frozenset({"schema_version", "version", "data_url", "sha256"})
_CURRENT_FIELDS = frozenset(
    {"schema_version", "version", "sha256", "payload", "last_check"}
)
_ATTEMPT_FIELDS = frozenset({"schema_version", "version", "last_check"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\Z", re.ASCII)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class HotwordManifest:
    """The narrowly validated public release metadata."""

    schema_version: int
    version: str
    data_url: str
    sha256: str


class UpdateStatus(str, Enum):
    UPDATED = "updated"
    CURRENT = "current"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    status: UpdateStatus
    version: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _CurrentState:
    version: str
    sha256: str
    payload: str
    last_check: datetime


@dataclass(frozen=True, slots=True)
class _AttemptState:
    version: str | None
    last_check: datetime


def update_hotwords(
    paths: StatePaths,
    manifest_url: str,
    fetcher: Transport,
    now: datetime,
    *,
    force: bool = False,
) -> UpdateResult:
    """Install a newer validated lexicon without raising into normalization.

    Fetching happens outside the cross-process lease.  The lease is held only
    while recovering local state and committing a fully validated candidate.
    """
    candidate_version: str | None = None
    try:
        checked_at = _utc_now(now)
        if not _allowed_url(manifest_url):
            return UpdateResult(
                UpdateStatus.REJECTED, message="manifest source rejected"
            )
        with _update_lock(paths):
            current = _recover_locked(paths)
            if not force and _check_is_recent(
                current, _read_attempt(paths), checked_at
            ):
                return UpdateResult(
                    UpdateStatus.SKIPPED,
                    version=None if current is None else current.version,
                    message="update check is not due",
                )

        manifest = _parse_manifest(
            _fetch_limited(fetcher, manifest_url, _MAX_MANIFEST_BYTES)
        )
        candidate_version = manifest.version
        with _update_lock(paths):
            current = _recover_locked(paths)
            immediate = _decide_known_version(paths, current, manifest, checked_at)
            if immediate is not None:
                return immediate

        data = _fetch_limited(fetcher, manifest.data_url, _MAX_DATA_BYTES)
        if hashlib.sha256(data).hexdigest() != manifest.sha256:
            raise ValueError("hotword checksum mismatch")
        _validate_hotword_jsonl(data)

        with _update_lock(paths):
            current = _recover_locked(paths)
            immediate = _decide_known_version(paths, current, manifest, checked_at)
            if immediate is not None:
                return immediate

            _commit_locked(paths, manifest, data, checked_at)
            return UpdateResult(UpdateStatus.UPDATED, version=manifest.version)
    except Exception as exc:
        if candidate_version is not None:
            try:
                with _update_lock(paths):
                    current = _recover_locked(paths)
                    if current is not None and current.version == candidate_version:
                        return UpdateResult(
                            UpdateStatus.UPDATED, version=candidate_version
                        )
            except Exception:
                pass
        if not isinstance(exc, TimeoutError):
            _record_failed_attempt(paths, now)
        return UpdateResult(UpdateStatus.REJECTED, message=_safe_message(exc))


def _commit_locked(
    paths: StatePaths, manifest: HotwordManifest, data: bytes, checked_at: datetime
) -> None:
    """Commit one candidate using a journaled pointer to immutable payload bytes."""
    payload = _payload_name(manifest.sha256)
    state = _CurrentState(manifest.version, manifest.sha256, payload, checked_at)
    _stage_payload(paths, state, data)
    _write_bytes_atomic(_pending_file(paths), _serialize_current(state))
    _write_current(paths, state)
    _materialize_current(paths, state)
    _write_attempt(paths, _AttemptState(state.version, checked_at))
    _remove_pending(paths)
    _verify_current_cache(paths, state)
    _cleanup_payloads(paths, state)


def _decide_known_version(
    paths: StatePaths,
    current: _CurrentState | None,
    manifest: HotwordManifest,
    checked_at: datetime,
) -> UpdateResult | None:
    """Return a terminal decision for an already-installed manifest version."""
    if current is None:
        return None
    comparison = _compare_versions(manifest.version, current.version)
    if comparison < 0:
        _write_attempt(paths, _AttemptState(current.version, checked_at))
        return UpdateResult(
            UpdateStatus.REJECTED,
            version=current.version,
            message="hotword version rollback rejected",
        )
    if comparison == 0:
        _write_current(paths, _with_check(current, checked_at))
        _write_attempt(paths, _AttemptState(current.version, checked_at))
        return UpdateResult(UpdateStatus.CURRENT, version=current.version)
    return None


def _recover_locked(paths: StatePaths) -> _CurrentState | None:
    """Reconcile an interrupted commit before any version decision is made."""
    _ensure_storage(paths)
    current = _read_current(paths)
    attempt = _read_attempt(paths)
    try:
        pending = _read_pending(paths)
    except ValueError:
        if current is None:
            raise
        _discard_pending(paths)
        pending = None
    if pending is not None and (current is None or pending != current):
        # A prepared record without the matching authoritative pointer was not
        # committed.  Leaving its immutable payload is harmless; discard only
        # the journal marker.
        _remove_pending(paths)
        pending = None
    if current is None:
        current = _migrate_legacy_locked(paths)
    if current is not None:
        _verify_payload(paths, current)
        _materialize_current(paths, current)
        if attempt is None or attempt.last_check < current.last_check:
            _write_attempt(paths, _AttemptState(current.version, current.last_check))
    if pending is not None:
        _remove_pending(paths)
    if current is not None:
        _cleanup_payloads(paths, current)
    return current


def _migrate_legacy_locked(paths: StatePaths) -> _CurrentState | None:
    """Promote a valid pre-transaction Task 7 installation exactly once."""
    attempt = _read_attempt(paths)
    if attempt is None:
        return None
    if attempt.version is None:
        return None
    if not paths.hotwords_file.is_file():
        raise ValueError("legacy update version has no hotword payload")
    data = _read_regular_file(paths.hotwords_file, _MAX_DATA_BYTES, "hotword file")
    _validate_hotword_jsonl(data)
    digest = hashlib.sha256(data).hexdigest()
    state = _CurrentState(
        attempt.version, digest, _payload_name(digest), attempt.last_check
    )
    _stage_payload(paths, state, data)
    _write_current(paths, state)
    return state


def _allowed_url(value: object) -> bool:
    """Allow only clean direct HTTPS URLs on the explicit public host list."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_URL_LENGTH
        or value != value.strip()
        or _CONTROL_PATTERN.search(value) is not None
    ):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and host in _ALLOWED_HOSTS
        and port in (None, 443)
    )


def _fetch_limited(fetcher: Transport, requested_url: str, limit: int) -> bytes:
    """Follow validated redirects explicitly and drain a bounded response body."""
    if not _allowed_url(requested_url):
        raise ValueError("remote source rejected")
    url = requested_url
    for _ in range(_MAX_REDIRECTS + 1):
        response = fetcher.open_no_redirect(url)
        try:
            status = getattr(response, "status", None)
            location = getattr(response, "location", None)
            if type(status) is not int:
                raise ValueError("transport response has invalid status")
            if status in {301, 302, 303, 307, 308}:
                if not isinstance(location, str) or not location:
                    raise ValueError("redirect response has no location")
                if location != location.strip() or _CONTROL_PATTERN.search(location):
                    raise ValueError("redirect location rejected")
                next_url = urljoin(url, location)
                if not _allowed_url(next_url):
                    raise ValueError("redirect source rejected")
                url = next_url
                continue
            if status != 200:
                raise ValueError("unexpected transport response status")
            return _read_limited(response, limit)
        finally:
            response.close()
    raise ValueError("redirect chain exceeds limit")


def _read_limited(response: Response, limit: int) -> bytes:
    """Drain short reads through EOF while rejecting anomalous or lying streams."""
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = limit + 1 - total
        if remaining <= 0:
            raise ValueError("remote payload exceeds size limit")
        chunk = response.read(min(64 * 1024, remaining))
        if chunk == b"":
            return b"".join(chunks)
        if not isinstance(chunk, bytes):
            raise ValueError("transport response must yield non-empty bytes or EOF")
        if len(chunk) > min(64 * 1024, remaining):
            raise ValueError("transport ignored read bound")
        total += len(chunk)
        if total > limit:
            raise ValueError("remote payload exceeds size limit")
        chunks.append(chunk)


def _parse_manifest(data: bytes) -> HotwordManifest:
    try:
        raw = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid manifest JSON") from exc
    if not isinstance(raw, Mapping) or set(raw) != _MANIFEST_FIELDS:
        raise ValueError("invalid manifest fields")
    schema_version = raw["schema_version"]
    version = raw["version"]
    data_url = raw["data_url"]
    digest = raw["sha256"]
    if type(schema_version) is not int or schema_version != _STATE_SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")
    if not _valid_version(version):
        raise ValueError("invalid manifest version")
    if not _allowed_url(data_url):
        raise ValueError("data source rejected")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("invalid manifest checksum")
    return HotwordManifest(schema_version, version, data_url, digest)


def _validate_hotword_jsonl(data: bytes) -> None:
    # Local import keeps the authoritative resolver usable from lexicon.py.
    from .lexicon import parse_entry

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("hotword file is not valid UTF-8") from exc
    entries = 0
    for line_number, line in enumerate(io.StringIO(text), start=1):
        if not line.strip():
            raise ValueError(f"hotword line {line_number} is blank")
        try:
            raw = json.loads(line, parse_constant=_reject_constant)
            parse_entry(raw, expected_scope=Scope.HOT)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"invalid hotword line {line_number}") from exc
        entries += 1
    if entries == 0:
        raise ValueError("hotword file must contain at least one entry")


def _read_current(paths: StatePaths) -> _CurrentState | None:
    return _read_state_file(_current_file(paths), _CURRENT_FIELDS, _current_from_raw)


def _read_pending(paths: StatePaths) -> _CurrentState | None:
    return _read_state_file(_pending_file(paths), _CURRENT_FIELDS, _current_from_raw)


def _read_attempt(paths: StatePaths) -> _AttemptState | None:
    return _read_state_file(_attempt_file(paths), _ATTEMPT_FIELDS, _attempt_from_raw)


def _read_state_file(path: Path, fields: frozenset[str], parser):
    if not path.exists():
        return None
    raw = _read_json_file(path, _MAX_MANIFEST_BYTES)
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError("invalid update state fields")
    return parser(raw)


def _current_from_raw(raw: Mapping[str, object]) -> _CurrentState:
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported current state schema")
    version = raw["version"]
    digest = raw["sha256"]
    payload = raw["payload"]
    if not _valid_version(version):
        raise ValueError("invalid current state version")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("invalid current state checksum")
    if payload != _payload_name(digest):
        raise ValueError("invalid current payload reference")
    return _CurrentState(version, digest, payload, _parse_time(raw["last_check"]))


def _attempt_from_raw(raw: Mapping[str, object]) -> _AttemptState:
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported attempt state schema")
    version = raw["version"]
    if version is not None and not _valid_version(version):
        raise ValueError("invalid attempt state version")
    return _AttemptState(version, _parse_time(raw["last_check"]))


def _stage_payload(paths: StatePaths, state: _CurrentState, data: bytes) -> None:
    payload = _payload_file(paths, state)
    if payload.exists():
        existing = _read_regular_file(payload, _MAX_DATA_BYTES, "payload")
        if existing != data:
            raise ValueError("immutable payload collision")
        return
    _write_bytes_atomic(payload, data)
    _verify_payload(paths, state)


def _verify_payload(paths: StatePaths, state: _CurrentState) -> bytes:
    payload = _payload_file(paths, state)
    if not payload.exists():
        # The pointer is authoritative. If a crash happened after switching it
        # but before payload cleanup completed, a matching validated cache can
        # reconstruct the immutable payload without accepting stale bytes.
        cached = _read_regular_file(
            paths.hotwords_file, _MAX_DATA_BYTES, "hotword file"
        )
        if hashlib.sha256(cached).hexdigest() != state.sha256:
            raise ValueError("authoritative payload is missing")
        _validate_hotword_jsonl(cached)
        _write_bytes_atomic(payload, cached)
    data = _read_regular_file(payload, _MAX_DATA_BYTES, "payload")
    if hashlib.sha256(data).hexdigest() != state.sha256:
        raise ValueError("payload checksum mismatch")
    _validate_hotword_jsonl(data)
    return data


def _materialize_current(paths: StatePaths, state: _CurrentState) -> None:
    data = _verify_payload(paths, state)
    target = paths.hotwords_file
    if (
        target.exists()
        and _read_regular_file(target, _MAX_DATA_BYTES, "hotword file") == data
    ):
        return
    _write_bytes_atomic(target, data)


def _verify_current_cache(paths: StatePaths, state: _CurrentState) -> None:
    if _read_current(paths) != state:
        raise ValueError("current pointer verification failed")
    if _read_regular_file(
        paths.hotwords_file, _MAX_DATA_BYTES, "hotword file"
    ) != _verify_payload(paths, state):
        raise ValueError("hotword cache verification failed")


def _write_current(paths: StatePaths, state: _CurrentState) -> None:
    _write_bytes_atomic(_current_file(paths), _serialize_current(state))


def _write_attempt(paths: StatePaths, state: _AttemptState) -> None:
    raw = {
        "last_check": state.last_check.isoformat(),
        "schema_version": _STATE_SCHEMA_VERSION,
        "version": state.version,
    }
    _write_bytes_atomic(_attempt_file(paths), _serialize(raw))


def _serialize_current(state: _CurrentState) -> bytes:
    return _serialize(
        {
            "last_check": state.last_check.isoformat(),
            "payload": state.payload,
            "schema_version": _STATE_SCHEMA_VERSION,
            "sha256": state.sha256,
            "version": state.version,
        }
    )


def _serialize(raw: Mapping[str, object]) -> bytes:
    return json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _remove_pending(paths: StatePaths) -> None:
    path = _pending_file(paths)
    if path.exists():
        _reject_symlink(path, "pending state")
        path.unlink()


def _discard_pending(paths: StatePaths) -> None:
    """Discard an invalid non-authoritative journal only after pointer validation."""
    path = _pending_file(paths)
    if path.exists():
        _reject_symlink(path, "pending state")
        path.unlink()


def _cleanup_payloads(paths: StatePaths, current: _CurrentState) -> None:
    """Bound immutable payload retention after a verified authoritative commit."""
    directory = _payloads_dir(paths)
    candidates = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.name.startswith("payload-")
            and path.suffix == ".jsonl"
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    kept = 0
    for path in candidates:
        if path.name == current.payload or kept < _MAX_RETAINED_PAYLOADS:
            kept += 1
            continue
        path.unlink()


def resolve_hotword_file(paths: StatePaths) -> Path | None:
    """Return a recovered authoritative cache for LexiconSet, or fail safely."""
    try:
        with _update_lock(paths):
            current = _recover_locked(paths)
            if current is not None:
                _verify_current_cache(paths, current)
                return paths.hotwords_file
            if paths.hotwords_file.is_file():
                _read_regular_file(paths.hotwords_file, _MAX_DATA_BYTES, "hotword file")
                return paths.hotwords_file
    except Exception:
        return None
    return None


def _record_failed_attempt(paths: StatePaths, now: datetime) -> None:
    try:
        checked_at = _utc_now(now)
        with _update_lock(paths):
            current = _recover_locked(paths)
            _write_attempt(
                paths,
                _AttemptState(None if current is None else current.version, checked_at),
            )
    except Exception:
        # A failed opportunistic update must never disturb local normalization.
        return


def _check_is_recent(
    current: _CurrentState | None, attempt: _AttemptState | None, now: datetime
) -> bool:
    checks = [state.last_check for state in (current, attempt) if state is not None]
    return bool(checks) and now - max(checks) < _UPDATE_INTERVAL


def _with_check(state: _CurrentState, checked_at: datetime) -> _CurrentState:
    return _CurrentState(state.version, state.sha256, state.payload, checked_at)


def _compare_versions(candidate: str, installed: str) -> int:
    candidate_parts = tuple(int(part) for part in candidate.split("."))
    installed_parts = tuple(int(part) for part in installed.split("."))
    return (candidate_parts > installed_parts) - (candidate_parts < installed_parts)


def _valid_version(value: object) -> bool:
    """Accept only calendar releases as ``YYYY.MM.DD`` for numeric ordering."""
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y.%m.%d")
    except ValueError:
        return False
    return True


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid update check timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid update check timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("update check timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _ensure_storage(paths: StatePaths) -> None:
    directory = _hotword_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_symlink(directory, "hotword directory")
    if not directory.is_dir():
        raise ValueError("hotword directory is not a directory")
    payloads = _payloads_dir(paths)
    payloads.mkdir(exist_ok=True)
    _reject_symlink(payloads, "payload directory")
    if not payloads.is_dir():
        raise ValueError("payload directory is not a directory")


@contextmanager
def _update_lock(paths: StatePaths) -> Iterator[None]:
    """Use an OS lease: crash-safe, bounded, and never stolen from an owner."""
    _ensure_storage(paths)
    path = _lock_file(paths)
    _reject_symlink(path, "update lock")
    created = not path.exists()
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_before = path.parent.stat()
    descriptor = os.open(path, flags, 0o600)
    acquired = False
    try:
        if created and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        _lock_descriptor(descriptor)
        acquired = True
        path_after = path.stat()
        parent_after = path.parent.stat()
        descriptor_info = os.fstat(descriptor)
        if os.name != "nt" and (
            (path_after.st_dev, path_after.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
        ):
            raise ValueError("update lock path changed during acquisition")
        yield
    finally:
        try:
            if acquired:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("hotword update lock is busy") from exc
            time.sleep(_LOCK_RETRY_SECONDS)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _read_json_file(path: Path, limit: int) -> object:
    data = _read_regular_file(path, limit, "update state")
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid update state JSON") from exc


def _read_regular_file(path: Path, limit: int, label: str) -> bytes:
    _reject_symlink(path, label)
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if info.st_size > limit:
        raise ValueError(f"{label} exceeds size limit")
    try:
        with path.open("rb") as source:
            data = source.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc
    if len(data) > limit:
        raise ValueError(f"{label} exceeds size limit")
    return data


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    _ensure_parent(path)
    _reject_symlink(path, "update target")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path.parent, "update directory")


def _reject_symlink(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise ValueError(f"{label} must not be a symlink")
    except OSError as exc:
        raise ValueError(f"unable to inspect {label}") from exc


def _payload_name(digest: str) -> str:
    return f"payload-{digest}.jsonl"


def _hotword_dir(paths: StatePaths) -> Path:
    return paths.hotwords_file.parent


def _payloads_dir(paths: StatePaths) -> Path:
    return _hotword_dir(paths) / "payloads"


def _payload_file(paths: StatePaths, state: _CurrentState) -> Path:
    return _payloads_dir(paths) / state.payload


def _current_file(paths: StatePaths) -> Path:
    return _hotword_dir(paths) / "current.json"


def _pending_file(paths: StatePaths) -> Path:
    return _hotword_dir(paths) / "pending.json"


def _attempt_file(paths: StatePaths) -> Path:
    return _hotword_dir(paths) / "last-update.json"


def _lock_file(paths: StatePaths) -> Path:
    return _hotword_dir(paths) / ".update.lock"


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _safe_message(exc: Exception) -> str:
    """Avoid returning arbitrary remote/body content in a diagnostic receipt."""
    return f"update rejected: {exc.__class__.__name__}"

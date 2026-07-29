"""Safe, recoverable retrieval of the public hotword lexicon.

The transport receives only already allowlisted public URLs.  The updater never
sends local lexicons, project data, or user input over the network. POSIX
coordination lives in an owner-only control root outside updateable state.
Replacing that verified root requires the same user's authority and is outside
the lock's threat model; ordinary state-directory replacement remains safe.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import math
import ntpath
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
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
_RECEIPT_CLOCK_SKEW = timedelta(minutes=5)
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.02
_WINDOWS_IDENTITY_RETRIES = 3
_STATE_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = frozenset({"schema_version", "version", "data_url", "sha256"})
_CURRENT_FIELDS = frozenset(
    {"schema_version", "version", "sha256", "payload", "last_check"}
)
_ATTEMPT_FIELDS = frozenset({"schema_version", "version", "last_check"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\Z", re.ASCII)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class UpdateLockTimeout(TimeoutError):
    """A bounded local lease acquisition failure, distinct from transport timeout."""


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
            current = _recover_locked(paths, checked_at)
            if not force and _check_is_recent(
                current, _read_attempt_or_none(paths, checked_at), checked_at
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
            current = _recover_locked(paths, checked_at)
            checked_at = _merged_check_time(paths, current, checked_at)
            immediate = _decide_known_version(paths, current, manifest, checked_at)
            if immediate is not None:
                return immediate

        data = _fetch_limited(fetcher, manifest.data_url, _MAX_DATA_BYTES)
        if hashlib.sha256(data).hexdigest() != manifest.sha256:
            raise ValueError("hotword checksum mismatch")
        _validate_hotword_jsonl(data)

        with _update_lock(paths):
            current = _recover_locked(paths, checked_at)
            checked_at = _merged_check_time(paths, current, checked_at)
            immediate = _decide_known_version(paths, current, manifest, checked_at)
            if immediate is not None:
                return immediate

            _commit_locked(paths, manifest, data, checked_at)
            return UpdateResult(UpdateStatus.UPDATED, version=manifest.version)
    except Exception as exc:
        if candidate_version is not None and not isinstance(exc, UpdateLockTimeout):
            try:
                with _update_lock(paths):
                    current = _recover_locked(paths, checked_at)
                    if current is not None and current.version == candidate_version:
                        return UpdateResult(
                            UpdateStatus.UPDATED, version=candidate_version
                        )
            except Exception:
                pass
        if not isinstance(exc, UpdateLockTimeout):
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
    _write_attempt_best_effort(paths, _AttemptState(state.version, checked_at))
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
        _write_attempt_best_effort(paths, _AttemptState(current.version, checked_at))
        return UpdateResult(
            UpdateStatus.REJECTED,
            version=current.version,
            message="hotword version rollback rejected",
        )
    if comparison == 0:
        _write_current(paths, _with_check(current, checked_at))
        _write_attempt_best_effort(paths, _AttemptState(current.version, checked_at))
        return UpdateResult(UpdateStatus.CURRENT, version=current.version)
    return None


def _recover_locked(paths: StatePaths, operation_now: datetime) -> _CurrentState | None:
    """Reconcile an interrupted commit before any version decision is made."""
    _ensure_storage(paths)
    current = _read_current(paths)
    attempt = _read_attempt_or_none(paths, operation_now)
    if attempt is not None and attempt.last_check > operation_now:
        # A small positive offset can be parsed as clock skew, but advisory
        # future time is never allowed to throttle or advance authority.
        attempt = None
    if attempt is None:
        # Receipts are never authoritative; a verified pointer remains usable.
        _discard_attempt_best_effort(paths)
    try:
        pending = _read_pending(paths)
    except (OSError, OverflowError, ValueError):
        # The prepare journal never grants authority.  A malformed or
        # non-removable entry therefore cannot invalidate a verified pointer.
        _discard_pending_best_effort(paths)
        pending = None
    if pending is not None and (current is None or pending != current):
        # A prepared record without the matching authoritative pointer was not
        # committed.  Leaving its immutable payload is harmless; discard only
        # the journal marker.
        _discard_pending_best_effort(paths)
        pending = None
    if current is None:
        current = _migrate_legacy_locked(paths, attempt)
    if current is not None:
        _verify_payload(paths, current)
        _materialize_current(paths, current)
        if attempt is None or attempt.last_check < current.last_check:
            _write_attempt_best_effort(
                paths, _AttemptState(current.version, current.last_check)
            )
    if pending is not None:
        _discard_pending_best_effort(paths)
    if current is not None:
        _cleanup_payloads(paths, current)
    return current


def _migrate_legacy_locked(
    paths: StatePaths, attempt: _AttemptState | None
) -> _CurrentState | None:
    """Promote a valid pre-transaction Task 7 installation exactly once."""
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
        or any(
            character.isspace()
            or unicodedata.category(character).startswith(("C", "Z"))
            for character in value
        )
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


def _read_attempt_or_none(
    paths: StatePaths, operation_now: datetime
) -> _AttemptState | None:
    """Treat the receipt as advisory even when it is malformed or inaccessible."""
    try:
        attempt = _read_attempt(paths)
    except (OSError, OverflowError, ValueError):
        return None
    if (
        attempt is not None
        and attempt.last_check - operation_now > _RECEIPT_CLOCK_SKEW
    ):
        return None
    return attempt


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


def _write_attempt_best_effort(paths: StatePaths, state: _AttemptState) -> None:
    """Keep the non-authoritative receipt from breaking a valid installation."""
    try:
        _write_attempt(paths, state)
    except (OSError, ValueError):
        return


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
    """Discard a non-authoritative prepare journal when its path is safe."""
    path = _pending_file(paths)
    if path.exists():
        _reject_symlink(path, "pending state")
        path.unlink()


def _discard_pending_best_effort(paths: StatePaths) -> None:
    """Keep an advisory journal path from breaking authoritative recovery."""
    try:
        _discard_pending(paths)
    except (OSError, ValueError):
        return


def _discard_attempt(paths: StatePaths) -> None:
    path = _attempt_file(paths)
    if path.exists():
        _reject_symlink(path, "attempt receipt")
        path.unlink()


def _discard_attempt_best_effort(paths: StatePaths) -> None:
    """Quarantine an invalid receipt only when its path can be removed safely."""
    try:
        _discard_attempt(paths)
    except (OSError, ValueError):
        return


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
    protected = {current.payload}
    try:
        pending = _read_pending(paths)
    except ValueError:
        pending = None
    if pending is not None:
        protected.add(pending.payload)
    kept = set(protected)
    for path in candidates:
        if path.name in kept:
            continue
        if len(kept) < _MAX_RETAINED_PAYLOADS:
            kept.add(path.name)
            continue
        path.unlink()


def resolve_hotword_file(paths: StatePaths) -> Path | None:
    """Return a recovered authoritative cache for LexiconSet, or fail safely."""
    try:
        operation_now = datetime.now(timezone.utc)
        with _update_lock(paths):
            current = _recover_locked(paths, operation_now)
            if current is not None:
                _verify_current_cache(paths, current)
                return paths.hotwords_file
            if paths.hotwords_file.is_file():
                data = _read_regular_file(
                    paths.hotwords_file, _MAX_DATA_BYTES, "hotword file"
                )
                _validate_hotword_jsonl(data)
                return paths.hotwords_file
    except Exception:
        return None
    return None


def _record_failed_attempt(paths: StatePaths, now: datetime) -> None:
    try:
        checked_at = _utc_now(now)
        with _update_lock(paths, timeout=0.05):
            current = _recover_locked(paths, checked_at)
            checked_at = _merged_check_time(paths, current, checked_at)
            _write_attempt_best_effort(
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
    return any(
        timedelta(0) <= now - checked_at < _UPDATE_INTERVAL
        for checked_at in checks
    )


def _merged_check_time(
    paths: StatePaths, current: _CurrentState | None, checked_at: datetime
) -> datetime:
    """Never let a late worker regress either authoritative or receipt time."""
    checks = [checked_at]
    if current is not None:
        checks.append(current.last_check)
    attempt = _read_attempt_or_none(paths, checked_at)
    if attempt is not None and attempt.last_check <= checked_at:
        checks.append(attempt.last_check)
    return max(checks)


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
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("update check timestamp must include a timezone")
        canonical = parsed.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("invalid update check timestamp") from exc
    if value != canonical.isoformat():
        raise ValueError("update check timestamp must be canonical UTC")
    return canonical


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
def _update_lock(paths: StatePaths, *, timeout: float | None = None) -> Iterator[None]:
    """Use a stable OS lease, never a replaceable filesystem lock entry."""
    if os.name == "nt":
        with _windows_mutex(paths, timeout):
            yield
        return

    with _posix_control_lock(paths, timeout):
        _ensure_storage(paths)
        yield


@contextmanager
def _posix_control_lock(paths: StatePaths, timeout: float | None) -> Iterator[None]:
    """Lock a permanent owner-only control entry outside replaceable state."""
    control_descriptor = _open_posix_control_directory()
    lock_descriptor: int | None = None
    acquired = False
    try:
        flags = os.O_RDWR | os.O_CREAT
        for required_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
            if not hasattr(os, required_flag):
                raise OSError(f"secure POSIX locks require {required_flag}")
            flags |= getattr(os, required_flag)
        name = _posix_lock_name(paths)
        lock_descriptor = os.open(name, flags, 0o600, dir_fd=control_descriptor)
        descriptor_info = os.fstat(lock_descriptor)
        entry_info = os.stat(
            name, dir_fd=control_descriptor, follow_symlinks=False
        )
        _verify_posix_lock_file(descriptor_info)
        descriptor_identity = (descriptor_info.st_dev, descriptor_info.st_ino)
        if descriptor_identity != (entry_info.st_dev, entry_info.st_ino):
            raise OSError("POSIX update lock changed during open")
        _lock_descriptor(lock_descriptor, timeout)
        acquired = True
        entry_after = os.stat(
            name, dir_fd=control_descriptor, follow_symlinks=False
        )
        if descriptor_identity != (entry_after.st_dev, entry_after.st_ino):
            raise OSError("POSIX update lock changed during acquisition")
        yield
    finally:
        try:
            if acquired and lock_descriptor is not None:
                _unlock_descriptor(lock_descriptor)
        finally:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            os.close(control_descriptor)


def _posix_control_directory(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    """Choose one per-user control root outside any configured state tree."""
    # XDG_RUNTIME_DIR is session-specific and may differ between a desktop,
    # cron, and service process owned by the same user.  Lock identity must not.
    del environ
    if home is None and os.name == "posix":
        import pwd

        user_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    else:
        user_home = Path.home() if home is None else Path(home)
    parent = Path(os.path.realpath(user_home.expanduser().absolute()))
    return parent / ".voice-intent-normalizer-locks"


def _open_posix_control_directory() -> int:
    """Create and verify the private root, then return a no-follow dir handle."""
    directory = _posix_control_directory()
    parent_info = os.stat(directory.parent)
    _verify_posix_control_parent(parent_info)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    flags = os.O_RDONLY
    for required_flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, required_flag):
            raise OSError(f"secure POSIX control roots require {required_flag}")
        flags |= getattr(os, required_flag)
    before = os.stat(directory, follow_symlinks=False)
    descriptor = os.open(directory, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        after = os.stat(directory, follow_symlinks=False)
        _verify_posix_control_root(descriptor_info)
        identity = (descriptor_info.st_dev, descriptor_info.st_ino)
        if identity != (before.st_dev, before.st_ino) or identity != (
            after.st_dev,
            after.st_ino,
        ):
            raise OSError("POSIX control root changed during open")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_posix_control_parent(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("POSIX control parent must be a directory")
    if info.st_uid != os.geteuid():
        raise PermissionError("POSIX control parent is not owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise PermissionError("POSIX control parent must not be group/world writable")


def _verify_posix_control_root(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("POSIX control root must be a directory")
    if info.st_uid != os.geteuid():
        raise PermissionError("POSIX control root is not owned by this user")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise PermissionError("POSIX control root must have mode 0700")


def _verify_posix_lock_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OSError("POSIX update lock must be a regular file")
    if info.st_uid != os.geteuid():
        raise PermissionError("POSIX update lock is not owned by this user")
    if info.st_nlink != 1:
        raise PermissionError("POSIX update lock must not be hard-linked")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("POSIX update lock must not grant group/world access")


def _posix_lock_name(paths: StatePaths) -> str:
    """Key the external lock by the canonical configured state-root identity."""
    identity = os.path.normpath(
        os.path.realpath(os.path.abspath(os.fspath(paths.root)))
    )
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()
    return f"update-{digest}.lock"


@contextmanager
def _windows_mutex(paths: StatePaths, timeout: float | None) -> Iterator[None]:
    """Hold stable path plus alias-convergent identity mutexes in fixed order."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    lease_timeout = _LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + lease_timeout
    path_name = _windows_path_mutex_name(paths)
    with _acquire_windows_mutex_names(kernel32, (path_name,), timeout=lease_timeout):
        for attempt_number in range(_WINDOWS_IDENTITY_RETRIES):
            remaining = max(0.0, deadline - time.monotonic())
            _ensure_storage(paths)
            identity = _windows_directory_identity(paths.root)
            identity_name = _windows_identity_mutex_name(identity)
            with _acquire_windows_mutex_names(
                kernel32, (identity_name,), timeout=remaining
            ):
                if _windows_directory_identity(paths.root) == identity:
                    yield
                    return
            if (
                attempt_number + 1 >= _WINDOWS_IDENTITY_RETRIES
                or time.monotonic() >= deadline
            ):
                raise OSError("Windows state path changed during lock acquisition")


@contextmanager
def _acquire_windows_mutex_names(
    kernel32, names: tuple[str, ...], *, timeout: float
) -> Iterator[None]:
    """Acquire unique mutex names deterministically and clean partial state."""
    import ctypes

    deadline = time.monotonic() + timeout
    handles: list[list[int | bool]] = []
    try:
        for name in sorted(set(names)):
            handle = _create_windows_mutex(kernel32, name)
            record: list[int | bool] = [handle, False]
            handles.append(record)
            milliseconds = max(
                0, math.ceil(1000 * max(0.0, deadline - time.monotonic()))
            )
            _wait_for_windows_mutex(kernel32, handle, milliseconds)
            record[1] = True
        yield
    finally:
        active_exception = sys.exc_info()[0] is not None
        cleanup_error: OSError | None = None
        for handle_value, acquired_value in reversed(handles):
            handle = int(handle_value)
            if bool(acquired_value):
                ctypes.set_last_error(0)
                if not kernel32.ReleaseMutex(handle) and cleanup_error is None:
                    cleanup_error = ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            if not kernel32.CloseHandle(handle) and cleanup_error is None:
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if cleanup_error is not None and not active_exception:
            raise cleanup_error


def _create_windows_mutex(kernel32, name: str) -> int:
    """Create or securely open the existing mutex, preserving native errors."""
    import ctypes

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if handle:
        return handle
    error = ctypes.get_last_error()
    if error != 5:
        raise ctypes.WinError(error)
    # An existing Global object may deny creation while still allowing this
    # user to open synchronization and release rights.
    handle = kernel32.OpenMutexW(0x00100001, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _wait_for_windows_mutex(kernel32, handle: int, milliseconds: int) -> None:
    """Acquire or raise the precise Win32 timeout/native failure."""
    import ctypes

    ctypes.set_last_error(0)
    result = kernel32.WaitForSingleObject(handle, milliseconds)
    if result in {0, 0x80}:  # WAIT_OBJECT_0 or recovered WAIT_ABANDONED
        return
    if result == 0x102:  # WAIT_TIMEOUT
        raise UpdateLockTimeout("hotword update mutex is busy")
    if result == 0xFFFFFFFF:  # WAIT_FAILED
        error = ctypes.get_last_error()
        raise ctypes.WinError(error or 31)
    raise OSError(f"unexpected Windows mutex wait result: {result:#x}")


def _windows_mutex_name(paths: StatePaths) -> str:
    """Derive a collision-resistant Global name from volume and directory ID."""
    identity = _windows_directory_identity(paths.root)
    return _windows_identity_mutex_name(identity)


def _windows_path_mutex_name(paths: StatePaths) -> str:
    """Name the replacement-stable mutex for one canonical configured path."""
    canonical_path = _normalize_windows_path_for_lock(paths.root)
    digest = hashlib.sha256(
        f"voice-intent-normalizer-path-v1:{canonical_path}".encode()
    ).hexdigest()
    return f"Global\\voice-intent-normalizer-update-0-path-{digest}"


def _windows_identity_mutex_name(identity: str) -> str:
    """Name the alias-convergent mutex for one volume/file identity."""
    digest = hashlib.sha256(
        f"voice-intent-normalizer-state-v1:{identity}".encode("ascii")
    ).hexdigest()
    return f"Global\\voice-intent-normalizer-update-1-identity-{digest}"


def _normalize_windows_path_for_lock(value: str | Path) -> str:
    """Normalize drive, separator, extended-drive, and extended-UNC aliases."""
    return ntpath.normcase(_absolute_windows_path(value))


def _absolute_windows_path(value: str | Path) -> str:
    """Remove Win32 extended prefixes while preserving case for native opens."""
    path = str(value).replace("/", "\\")
    folded = path.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[8:]
    elif folded.startswith("\\\\?\\"):
        path = path[4:]
    path = ntpath.abspath(path)
    return ntpath.normpath(path)


def _extended_windows_path(value: str | Path) -> str:
    """Return an absolute extended path for native no-follow directory opening."""
    normalized = _absolute_windows_path(value)
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def _windows_directory_identity(path: Path) -> str:
    """Query the target directory's volume serial and stable native file ID."""
    import ctypes
    from ctypes import wintypes

    class _FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
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
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        _extended_windows_path(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS; follow junction aliases
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    failure: OSError | None = None
    identity: str | None = None
    try:
        file_id = _FileIdInfo()
        ctypes.set_last_error(0)
        if kernel32.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)
        ):
            identity = (
                f"{file_id.volume_serial_number:016x}:"
                f"{bytes(file_id.file_id.identifier).hex()}"
            )
        else:
            extended_error = ctypes.get_last_error()
            if extended_error not in {1, 50, 87, 120}:
                raise ctypes.WinError(extended_error)
            legacy = _ByHandleFileInformation()
            ctypes.set_last_error(0)
            if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(legacy)):
                raise ctypes.WinError(ctypes.get_last_error())
            file_index = (legacy.file_index_high << 32) | legacy.file_index_low
            identity = f"{legacy.volume_serial_number:08x}:{file_index:016x}"
    except OSError as exc:
        failure = exc
    finally:
        ctypes.set_last_error(0)
        if not kernel32.CloseHandle(handle) and failure is None:
            failure = ctypes.WinError(ctypes.get_last_error())
    if failure is not None:
        raise failure
    if identity is None:
        raise OSError("Windows directory identity query returned no identity")
    return identity


def _lock_descriptor(descriptor: int, timeout: float | None = None) -> None:
    import fcntl

    def acquire(target: int) -> None:
        fcntl.flock(target, fcntl.LOCK_EX | fcntl.LOCK_NB)

    _acquire_posix_lock(descriptor, timeout, acquire)


def _acquire_posix_lock(
    descriptor: int,
    timeout: float | None,
    lock_operation: Callable[[int], None],
) -> None:
    deadline = time.monotonic() + (
        _LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    )
    while True:
        try:
            lock_operation(descriptor)
            return
        except OSError as exc:
            if not _posix_lock_error_is_contention(exc.errno):
                raise
            if time.monotonic() >= deadline:
                raise UpdateLockTimeout("hotword update lock is busy") from exc
            time.sleep(_LOCK_RETRY_SECONDS)


def _unlock_descriptor(descriptor: int) -> None:
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _posix_lock_error_is_contention(error_number: int | None) -> bool:
    return error_number in {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }


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


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _safe_message(exc: Exception) -> str:
    """Avoid returning arbitrary remote/body content in a diagnostic receipt."""
    return f"update rejected: {exc.__class__.__name__}"

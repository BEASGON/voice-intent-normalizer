"""Safe, opt-in retrieval of the public hotword lexicon.

The updater only gives a caller public URLs to fetch.  It never sends local
lexicons, project data, or user input over the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from .lexicon import parse_entry
from .models import Scope
from .paths import StatePaths

Fetcher = Callable[[str], bytes]

_ALLOWED_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"}
)
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_DATA_BYTES = 10 * 1024 * 1024
_MAX_URL_LENGTH = 4096
_MAX_VERSION_LENGTH = 128
_UPDATE_INTERVAL = timedelta(days=1)
_STATE_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = frozenset({"schema_version", "version", "data_url", "sha256"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_VERSION_TOKEN = re.compile(r"\d+|\D+")


@dataclass(frozen=True, slots=True)
class HotwordManifest:
    """The narrowly validated public release metadata."""

    schema_version: int
    version: str
    data_url: str
    sha256: str


class UpdateStatus(str, Enum):
    """The non-exceptional outcome of an update attempt."""

    UPDATED = "updated"
    CURRENT = "current"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """A result suitable for diagnostics without interrupting normalization."""

    status: UpdateStatus
    version: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _UpdateState:
    last_check: datetime | None
    version: str | None


def update_hotwords(
    paths: StatePaths,
    manifest_url: str,
    fetcher: Fetcher,
    now: datetime,
    *,
    force: bool = False,
) -> UpdateResult:
    """Install a newer validated public hotword file without raising errors.

    ``fetcher`` receives only the already allowlisted public manifest and data
    URLs.  All remote bytes are size limited, checksum validated, and parsed
    before the existing local lexicon is atomically replaced.
    """
    state: _UpdateState | None = None
    try:
        checked_at = _utc_now(now)
        if not _allowed_url(manifest_url):
            return UpdateResult(
                UpdateStatus.REJECTED, message="manifest source rejected"
            )

        state = _read_state(_state_file(paths))
        if state.last_check is not None and not force:
            elapsed = checked_at - state.last_check
            if elapsed < _UPDATE_INTERVAL:
                return UpdateResult(
                    UpdateStatus.SKIPPED,
                    version=state.version,
                    message="update check is not due",
                )

        remote_manifest = _parse_manifest(
            _fetch_limited(fetcher, manifest_url, _MAX_MANIFEST_BYTES)
        )
        if state.version == remote_manifest.version and paths.hotwords_file.is_file():
            _record_check(paths, checked_at, state.version)
            return UpdateResult(UpdateStatus.CURRENT, version=state.version)
        if state.version is not None and _version_is_older(
            remote_manifest.version, state.version
        ):
            _record_check(paths, checked_at, state.version)
            return UpdateResult(
                UpdateStatus.REJECTED,
                version=state.version,
                message="hotword version rollback rejected",
            )

        data = _fetch_limited(fetcher, remote_manifest.data_url, _MAX_DATA_BYTES)
        if not hashlib.sha256(data).hexdigest() == remote_manifest.sha256:
            raise ValueError("hotword checksum mismatch")
        _validate_hotword_jsonl(data)
        _write_bytes_atomic(paths.hotwords_file, data)
        recorded = _record_check(paths, checked_at, remote_manifest.version)
        message = None if recorded else "installed but could not record update check"
        return UpdateResult(
            UpdateStatus.UPDATED, version=remote_manifest.version, message=message
        )
    except Exception as exc:
        # Updates are opportunistic: ordinary offline and validation failures
        # must never interrupt a user's local normalization request.
        try:
            checked_at = _utc_now(now)
        except (TypeError, ValueError):
            checked_at = None
        if checked_at is not None and state is not None:
            _record_check(paths, checked_at, state.version)
        return UpdateResult(UpdateStatus.REJECTED, message=_safe_message(exc))


def _allowed_url(value: object) -> bool:
    """Accept only direct HTTPS URLs on the public release allowlist."""
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH:
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


def _fetch_limited(fetcher: Fetcher, url: str, limit: int) -> bytes:
    """Fetch a validated public URL and reject a response over its byte cap."""
    if not _allowed_url(url):
        raise ValueError("remote source rejected")
    response = fetcher(url)
    if not isinstance(response, bytes):
        raise ValueError("fetcher must return bytes")
    if len(response) > limit:
        raise ValueError("remote payload exceeds size limit")
    return response


def _parse_manifest(data: bytes) -> HotwordManifest:
    """Parse a bounded, schema-locked manifest with no permissive defaults."""
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
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("unsupported manifest schema")
    if (
        not isinstance(version, str)
        or not version.strip()
        or version != version.strip()
        or len(version) > _MAX_VERSION_LENGTH
    ):
        raise ValueError("invalid manifest version")
    if not _allowed_url(data_url):
        raise ValueError("data source rejected")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("invalid manifest checksum")
    return HotwordManifest(
        schema_version=schema_version,
        version=version,
        data_url=data_url,
        sha256=digest,
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_hotword_jsonl(data: bytes) -> None:
    """Validate every decoded JSONL record before touching the installed file."""
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


def _state_file(paths: StatePaths) -> Path:
    return paths.hotwords_file.parent / "last-update.json"


def _read_state(path: Path) -> _UpdateState:
    """Read only a complete, bounded state record; fail closed if it is corrupt."""
    if not path.is_file():
        return _UpdateState(last_check=None, version=None)
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("update state exceeds size limit")
        raw = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "version",
            "last_check",
        }:
            raise ValueError("invalid update state fields")
        if (
            isinstance(raw["schema_version"], bool)
            or raw["schema_version"] != _STATE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported update state schema")
        version = raw["version"]
        if version is not None and (
            not isinstance(version, str)
            or not version.strip()
            or version != version.strip()
            or len(version) > _MAX_VERSION_LENGTH
        ):
            raise ValueError("invalid update state version")
        last_check = _parse_time(raw["last_check"])
        return _UpdateState(last_check=last_check, version=version)
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise ValueError("invalid update state") from exc


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


def _record_check(paths: StatePaths, checked_at: datetime, version: str | None) -> bool:
    """Best-effort record of an attempted check, without masking the outcome."""
    state = {
        "last_check": checked_at.isoformat(),
        "schema_version": _STATE_SCHEMA_VERSION,
        "version": version,
    }
    try:
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        _write_bytes_atomic(_state_file(paths), serialized)
    except OSError:
        return False
    return True


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Durably replace one file only after its complete replacement is ready."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _version_is_older(candidate: str, installed: str) -> bool:
    """Compare mixed numeric release labels without allowing a known downgrade."""
    return _version_key(candidate) < _version_key(installed)


def _version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    tokens: list[tuple[int, int | str]] = []
    for token in _VERSION_TOKEN.findall(value.casefold()):
        if token.isdecimal():
            tokens.append((1, int(token)))
        else:
            tokens.append((0, token))
    return tuple(tokens)


def _safe_message(exc: Exception) -> str:
    """Keep diagnostics bounded and avoid exposing arbitrary remote payloads."""
    return str(exc)[:200] or exc.__class__.__name__

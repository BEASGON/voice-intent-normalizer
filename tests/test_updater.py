"""Security, transaction, and recovery tests for public hotword updates."""

import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import voice_intent_normalizer.updater as updater_module
from voice_intent_normalizer.lexicon import LexiconSet
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.updater import UpdateStatus, update_hotwords

MANIFEST_URL = (
    "https://github.com/BEASGON/voice-intent-normalizer/"
    "releases/download/data/manifest.json"
)
DATA_URL = (
    "https://github.com/BEASGON/voice-intent-normalizer/"
    "releases/download/data/zh-ai.jsonl"
)
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


@dataclass
class Route:
    data: bytes
    status: int = 200
    location: str | None = None
    chunks: tuple[object, ...] | None = None


class FakeResponse:
    def __init__(self, route: Route) -> None:
        self._route = route
        self.status = route.status
        self.location = route.location
        self.read_sizes: list[int] = []
        self.closed = False
        self._offset = 0
        self._chunks = list(route.chunks) if route.chunks is not None else None

    def read(self, size: int) -> object:
        self.read_sizes.append(size)
        if self._chunks is not None:
            return self._chunks.pop(0) if self._chunks else b""
        data = self._route.data[self._offset : self._offset + size]
        self._offset += len(data)
        return data

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: dict[str, bytes | Exception | Route]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.opened: list[FakeResponse] = []

    def open_no_redirect(self, url: str) -> FakeResponse:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        route = response if isinstance(response, Route) else Route(response)
        opened = FakeResponse(route)
        self.opened.append(opened)
        return opened


class BlockingTransport(FakeTransport):
    """A picklable transport that pauses the older update after its manifest."""

    def __init__(self, responses, blocked_url, ready, release) -> None:
        super().__init__(responses)
        self.blocked_url = blocked_url
        self.ready = ready
        self.release = release

    def open_no_redirect(self, url: str) -> FakeResponse:
        if url == self.blocked_url:
            self.ready.set()
            if not self.release.wait(10):
                raise TimeoutError("test release was not signalled")
        return super().open_no_redirect(url)


def paths_for(tmp_path):
    return StatePaths(root=tmp_path / ".voice-intent-normalizer")


def hotword_data(*, scope: str = "hot", canonical: str = "OpenClaw") -> bytes:
    return (
        '{"canonical":"'
        + canonical
        + '","scope":"'
        + scope
        + '","aliases":["Open Cloud"],"domains":["ai"],'
        '"weight":0.9,"status":"curated"}\n'
    ).encode()


def manifest(
    data: bytes,
    *,
    version: str = "2026.07.29",
    data_url: str = DATA_URL,
    sha256: str | None = None,
    schema_version: object = 1,
) -> bytes:
    return json.dumps(
        {
            "schema_version": schema_version,
            "version": version,
            "data_url": data_url,
            "sha256": hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
        }
    ).encode()


def transport_for(data: bytes, **manifest_options: object) -> FakeTransport:
    return FakeTransport(
        {
            MANIFEST_URL: manifest(data, **manifest_options),
            DATA_URL: data,
        }
    )


def _child_update(root: str, transport: FakeTransport, results) -> None:
    result = update_hotwords(
        StatePaths(root=Path(root)), MANIFEST_URL, transport, NOW, force=True
    )
    results.put((result.status.value, result.version))


def _child_update_after_signal(
    root: str, transport: FakeTransport, start, results
) -> None:
    if not start.wait(10):
        results.put(("test-timeout", None))
        return
    _child_update(root, transport, results)


def _child_hold_lock(root: str, ready, release) -> None:
    with updater_module._update_lock(StatePaths(root=Path(root))):
        ready.set()
        release.wait(10)


def _child_hold_lock_with_xdg(root: str, runtime: str, ready, release) -> None:
    os.environ["XDG_RUNTIME_DIR"] = runtime
    _child_hold_lock(root, ready, release)


def test_valid_update_replaces_hotwords_and_records_check(tmp_path):
    data = hotword_data()
    paths = paths_for(tmp_path)

    result = update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW)

    assert result.status is UpdateStatus.UPDATED
    assert result.version == "2026.07.29"
    assert paths.hotwords_file.read_bytes() == data
    state = json.loads((paths.hotwords_file.parent / "last-update.json").read_text())
    assert state == {
        "last_check": "2026-07-29T12:00:00+00:00",
        "schema_version": 1,
        "version": "2026.07.29",
    }


def test_transport_reads_only_a_bounded_amount_and_closes_each_response(tmp_path):
    data = hotword_data()
    transport = transport_for(data)

    assert update_hotwords(
        paths_for(tmp_path), MANIFEST_URL, transport, NOW
    ).status is (UpdateStatus.UPDATED)
    assert all(
        response.read_sizes and max(response.read_sizes) <= 64 * 1024
        for response in transport.opened
    )
    assert all(response.closed for response in transport.opened)


def test_hash_failure_preserves_last_valid_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    data = hotword_data()

    result = update_hotwords(
        paths, MANIFEST_URL, transport_for(data, sha256="0" * 64), NOW
    )

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


def test_invalid_hotword_jsonl_preserves_last_valid_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")

    result = update_hotwords(
        paths, MANIFEST_URL, transport_for(hotword_data(scope="base")), NOW
    )

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/manifest.json",
        " https://github.com/manifest.json",
        "https://github.com/manifest.json\n",
        "https://github.com@evil.example/manifest.json",
    ],
)
def test_rejects_unsafe_manifest_sources_without_egress(tmp_path, url):
    transport = FakeTransport({url: manifest(hotword_data())})

    result = update_hotwords(paths_for(tmp_path), url, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == []


@pytest.mark.parametrize(
    "location",
    ["http://github.com/data", "http://localhost/data", "https://evil.example/data"],
)
def test_never_opens_a_disallowed_redirect_target(tmp_path, location):
    transport = FakeTransport({MANIFEST_URL: Route(b"", 302, location)})

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == [MANIFEST_URL]
    assert transport.opened[0].read_sizes == []
    assert transport.opened[0].closed


def test_resolves_relative_redirect_then_opens_only_the_valid_target(tmp_path):
    data = hotword_data()
    redirected_manifest = "https://github.com/manifest-next.json"
    transport = FakeTransport(
        {
            MANIFEST_URL: Route(b"", 302, "/manifest-next.json"),
            redirected_manifest: Route(manifest(data)),
            DATA_URL: Route(data),
        }
    )

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.UPDATED
    assert transport.calls == [MANIFEST_URL, redirected_manifest, DATA_URL]


def test_short_reads_are_drained_until_eof_and_lying_streams_are_rejected(tmp_path):
    data = hotword_data()
    manifest_bytes = manifest(data)
    transport = FakeTransport(
        {
            MANIFEST_URL: Route(
                b"", chunks=(manifest_bytes[:8], manifest_bytes[8:], b"")
            ),
            DATA_URL: Route(b"", chunks=(data[:10], data[10:], b"")),
        }
    )

    assert update_hotwords(
        paths_for(tmp_path), MANIFEST_URL, transport, NOW
    ).status is (UpdateStatus.UPDATED)
    assert len(transport.opened[0].read_sizes) == 3
    assert len(transport.opened[1].read_sizes) == 3


def test_rejects_none_or_oversized_lie_from_a_stream_and_closes_it(tmp_path):
    transport = FakeTransport({MANIFEST_URL: Route(b"", chunks=(None,))})

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.opened[0].closed


def test_rejects_unapproved_manifest_data_url_before_download(tmp_path):
    data = hotword_data()
    transport = FakeTransport(
        {
            MANIFEST_URL: manifest(data, data_url="https://example.com/zh-ai.jsonl"),
        }
    )

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == [MANIFEST_URL]


def test_rejects_oversized_payloads_without_replacing_current_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    oversized_data = b"x" * (10 * 1024 * 1024 + 1)

    result = update_hotwords(paths, MANIFEST_URL, transport_for(oversized_data), NOW)

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


def test_failure_is_throttled_for_a_day_but_force_retries(tmp_path):
    paths = paths_for(tmp_path)
    unavailable = FakeTransport({MANIFEST_URL: OSError("offline")})

    failed = update_hotwords(paths, MANIFEST_URL, unavailable, NOW)
    skipped = update_hotwords(
        paths, MANIFEST_URL, unavailable, NOW + timedelta(hours=23)
    )
    retried = update_hotwords(
        paths, MANIFEST_URL, unavailable, NOW + timedelta(hours=23), force=True
    )

    assert failed.status is UpdateStatus.REJECTED
    assert skipped.status is UpdateStatus.SKIPPED
    assert retried.status is UpdateStatus.REJECTED
    assert unavailable.calls == [MANIFEST_URL, MANIFEST_URL]


def test_newer_failed_receipt_survives_recovery_and_exact_day_boundary(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data()
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    unavailable = FakeTransport({MANIFEST_URL: OSError("offline")})
    assert (
        update_hotwords(
            paths, MANIFEST_URL, unavailable, NOW + timedelta(hours=1), force=True
        ).status
        is UpdateStatus.REJECTED
    )

    skipped = update_hotwords(
        paths, MANIFEST_URL, unavailable, NOW + timedelta(hours=24)
    )
    boundary = update_hotwords(
        paths, MANIFEST_URL, unavailable, NOW + timedelta(hours=25)
    )

    assert skipped.status is UpdateStatus.SKIPPED
    assert boundary.status is UpdateStatus.REJECTED
    assert unavailable.calls == [MANIFEST_URL, MANIFEST_URL]


def test_current_version_does_not_download_data(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_bytes(hotword_data())
    (paths.hotwords_file.parent / "last-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.29",
                "last_check": "2026-07-28T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    transport = FakeTransport({MANIFEST_URL: manifest(hotword_data())})

    result = update_hotwords(paths, MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.CURRENT
    assert transport.calls == [MANIFEST_URL]


def test_loader_recovers_authoritative_pointer_instead_of_stale_cache(tmp_path):
    paths = paths_for(tmp_path)
    authoritative = hotword_data(canonical="Authoritative")
    assert (
        update_hotwords(paths, MANIFEST_URL, transport_for(authoritative), NOW).status
        is UpdateStatus.UPDATED
    )
    paths.hotwords_file.write_bytes(hotword_data(canonical="Stale"))

    loaded = LexiconSet.load(paths, tmp_path / "builtins")

    assert [entry.canonical for entry in loaded.entries] == ["Authoritative"]


def test_authoritative_cache_reconstructs_missing_payload_and_discards_bad_pending(
    tmp_path,
):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Recovered pointer")
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    current = json.loads((paths.hotwords_file.parent / "current.json").read_text())
    (paths.hotwords_file.parent / "payloads" / current["payload"]).unlink()
    (paths.hotwords_file.parent / "pending.json").write_text("not-json")

    loaded = LexiconSet.load(paths, tmp_path / "builtins")

    assert [entry.canonical for entry in loaded.entries] == ["Recovered pointer"]
    assert (paths.hotwords_file.parent / "payloads" / current["payload"]).is_file()
    assert not (paths.hotwords_file.parent / "pending.json").exists()


def test_pointerless_corrupt_cache_falls_back_to_builtin_lexicon(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("not-jsonl\n", encoding="utf-8")
    builtins = tmp_path / "builtins"
    (builtins / "base-zh.jsonl").parent.mkdir(parents=True)
    (builtins / "base-zh.jsonl").write_bytes(
        hotword_data(scope="base", canonical="Builtin")
    )

    loaded = LexiconSet.load(paths, builtins)

    assert [entry.canonical for entry in loaded.entries] == ["Builtin"]


def test_rejects_version_rollback_and_preserves_current_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    (paths.hotwords_file.parent / "last-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.30",
                "last_check": "2026-07-28T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    result = update_hotwords(
        paths, MANIFEST_URL, transport_for(hotword_data(), version="2026.07.29"), NOW
    )

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


@pytest.mark.parametrize(
    "version", ["release-2", "2026.7.29", "2026.07.29-rc1", "2026.13.29"]
)
def test_rejects_ambiguous_release_version_grammar(tmp_path, version):
    transport = transport_for(hotword_data(), version=version)

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == [MANIFEST_URL]


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_rejects_non_integer_manifest_schema_before_data_download(
    tmp_path, schema_version
):
    transport = transport_for(hotword_data(), schema_version=schema_version)

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == [MANIFEST_URL]


def test_corrupt_receipt_is_rebuilt_and_does_not_block_remote_recovery(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    state_file = paths.hotwords_file.parent / "last-update.json"
    state_file.write_text("not-json", encoding="utf-8")
    transport = FakeTransport({MANIFEST_URL: OSError("offline")})

    result = update_hotwords(paths, MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.calls == [MANIFEST_URL]
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "last_check": "2026-07-29T12:00:00+00:00",
        "schema_version": 1,
        "version": None,
    }


def test_corrupt_receipt_cannot_block_an_authoritative_current_pointer(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Authoritative")
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text("not-json", encoding="utf-8")

    resolved = updater_module.resolve_hotword_file(paths)

    assert resolved == paths.hotwords_file
    assert resolved.read_bytes() == data
    assert json.loads(receipt.read_text(encoding="utf-8"))["version"] == "2026.07.29"


def test_same_version_late_worker_preserves_newer_authoritative_timestamp(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data()
    later = NOW + timedelta(days=1)
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), later).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.29",
                "last_check": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW, force=True)

    assert result.status is UpdateStatus.CURRENT
    receipt_state = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_state["last_check"] == later.isoformat()
    current = json.loads((paths.hotwords_file.parent / "current.json").read_text())
    assert current["last_check"] == later.isoformat()


def test_near_future_receipt_does_not_throttle_a_nonforced_check(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": None,
                "last_check": (NOW + timedelta(minutes=4)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    unavailable = FakeTransport({MANIFEST_URL: OSError("offline")})

    result = update_hotwords(paths, MANIFEST_URL, unavailable, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert unavailable.calls == [MANIFEST_URL]


def test_near_future_receipt_cannot_promote_authoritative_time_when_forced(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data()
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.29",
                "last_check": (NOW + timedelta(minutes=4)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW, force=True)

    assert result.status is UpdateStatus.CURRENT
    receipt_state = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_state["last_check"] == NOW.isoformat()
    current = json.loads((paths.hotwords_file.parent / "current.json").read_text())
    assert current["last_check"] == NOW.isoformat()


def test_receipt_directory_cannot_disable_authoritative_resolution(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Authoritative")
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.unlink()
    receipt.mkdir()

    resolved = updater_module.resolve_hotword_file(paths)

    assert resolved == paths.hotwords_file
    assert resolved.read_bytes() == data
    assert receipt.is_dir()


def test_pending_directory_cannot_disable_authoritative_resolution(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Authoritative")
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    pending = paths.hotwords_file.parent / "pending.json"
    pending.mkdir()

    resolved = updater_module.resolve_hotword_file(paths)

    assert resolved == paths.hotwords_file
    assert resolved.read_bytes() == data
    assert pending.is_dir()


def test_receipt_directory_cannot_block_a_forced_current_update(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data()
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.unlink()
    receipt.mkdir()
    transport = transport_for(data)

    result = update_hotwords(paths, MANIFEST_URL, transport, NOW, force=True)

    assert result.status is UpdateStatus.CURRENT
    assert transport.calls == [MANIFEST_URL]
    assert receipt.is_dir()


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59.999999-23:59",
    ],
)
def test_extreme_offset_receipt_cannot_disable_authoritative_resolution(
    tmp_path, timestamp
):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Authoritative")
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.29",
                "last_check": timestamp,
            }
        ),
        encoding="utf-8",
    )

    resolved = updater_module.resolve_hotword_file(paths)

    assert resolved == paths.hotwords_file
    assert resolved.read_bytes() == data


def test_network_timeout_records_a_failed_attempt_and_is_throttled(tmp_path):
    paths = paths_for(tmp_path)
    timeout = FakeTransport({MANIFEST_URL: TimeoutError("network timed out")})

    first = update_hotwords(paths, MANIFEST_URL, timeout, NOW)
    skipped = update_hotwords(paths, MANIFEST_URL, timeout, NOW + timedelta(hours=1))

    assert first.status is UpdateStatus.REJECTED
    assert skipped.status is UpdateStatus.SKIPPED
    assert timeout.calls == [MANIFEST_URL]


@pytest.mark.parametrize(
    "failure_name", ["payload", "pending", "current", "zh-ai", "last-update"]
)
def test_commit_faults_recover_without_mixing_version_and_payload(
    tmp_path, monkeypatch, failure_name
):
    paths = paths_for(tmp_path)
    data = hotword_data(canonical="Recovered")
    transport = transport_for(data, version="2026.07.30")
    real_write = updater_module._write_bytes_atomic
    failed = False

    def fail_named(path, content):
        nonlocal failed
        if not failed and failure_name in Path(path).name:
            failed = True
            raise OSError(f"injected {failure_name} failure")
        real_write(path, content)

    monkeypatch.setattr(updater_module, "_write_bytes_atomic", fail_named)
    result = update_hotwords(paths, MANIFEST_URL, transport, NOW, force=True)
    monkeypatch.setattr(updater_module, "_write_bytes_atomic", real_write)

    expected = (
        UpdateStatus.UPDATED
        if failure_name in {"zh-ai", "last-update"}
        else UpdateStatus.REJECTED
    )
    assert result.status is expected
    assert failed
    restarted = update_hotwords(
        paths, MANIFEST_URL, transport_for(data, version="2026.07.30"), NOW, force=True
    )
    assert restarted.status in {UpdateStatus.UPDATED, UpdateStatus.CURRENT}
    assert paths.hotwords_file.read_bytes() == data
    current = json.loads((paths.hotwords_file.parent / "current.json").read_text())
    assert current["version"] == "2026.07.30"
    assert current["sha256"] == hashlib.sha256(data).hexdigest()


def test_cross_process_late_lower_version_cannot_overwrite_newer_install(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    paths = paths_for(tmp_path)
    older = BlockingTransport(
        {
            MANIFEST_URL: manifest(
                hotword_data(canonical="Older"), version="2026.07.29"
            ),
            DATA_URL: hotword_data(canonical="Older"),
        },
        DATA_URL,
        ready,
        release,
    )
    older_process = context.Process(
        target=_child_update, args=(str(paths.root), older, results)
    )
    older_process.start()
    assert ready.wait(10)

    newer = transport_for(hotword_data(canonical="Newer"), version="2026.07.30")
    newer_process = context.Process(
        target=_child_update, args=(str(paths.root), newer, results)
    )
    newer_process.start()
    newer_process.join(15)
    assert newer_process.exitcode == 0
    release.set()
    older_process.join(15)
    assert older_process.exitcode == 0

    outcomes = {results.get(timeout=3), results.get(timeout=3)}
    assert ("updated", "2026.07.30") in outcomes
    assert ("rejected", "2026.07.30") in outcomes
    assert paths.hotwords_file.read_bytes() == hotword_data(canonical="Newer")


def test_busy_cross_process_lock_is_bounded_and_does_not_break_owner(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)

    blocked = update_hotwords(
        paths, MANIFEST_URL, transport_for(hotword_data()), NOW, force=True
    )

    assert blocked.status is UpdateStatus.REJECTED
    assert holder.is_alive()
    release.set()
    holder.join(10)
    assert holder.exitcode == 0
    assert (
        update_hotwords(
            paths, MANIFEST_URL, transport_for(hotword_data()), NOW, force=True
        ).status
        is UpdateStatus.UPDATED
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel mutex behavior")
def test_windows_extended_path_alias_contends_on_the_same_global_mutex(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    extended_root = Path("\\\\?\\" + str(paths.root))
    alias_paths = StatePaths(root=extended_root)
    transport = transport_for(hotword_data())

    blocked = update_hotwords(
        alias_paths, MANIFEST_URL, transport, NOW, force=True
    )

    assert blocked.status is UpdateStatus.REJECTED
    assert transport.calls == []
    assert holder.is_alive()
    release.set()
    holder.join(10)
    assert holder.exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel mutex behavior")
def test_windows_state_root_replacement_still_contends_on_path_mutex(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    moved = tmp_path / "held-state-root"
    paths.root.rename(moved)
    paths.root.mkdir()
    transport = transport_for(hotword_data())

    try:
        blocked = update_hotwords(
            paths, MANIFEST_URL, transport, NOW, force=True
        )

        assert blocked.status is UpdateStatus.REJECTED
        assert transport.calls == []
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(10)
    assert holder.exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows identity replacement handling")
def test_windows_retries_when_state_identity_changes_before_mutex_creation(
    tmp_path, monkeypatch
):
    paths = paths_for(tmp_path)
    paths.root.mkdir(parents=True)
    moved = tmp_path / "identity-before-mutex"
    real_identity = updater_module._windows_directory_identity
    lookups = 0

    def replace_after_first_lookup(path):
        nonlocal lookups
        identity = real_identity(path)
        lookups += 1
        if lookups == 1:
            paths.root.rename(moved)
            paths.root.mkdir()
        return identity

    monkeypatch.setattr(
        updater_module, "_windows_directory_identity", replace_after_first_lookup
    )

    with updater_module._update_lock(paths):
        assert paths.root.is_dir()

    assert lookups >= 2


@pytest.mark.skipif(os.name != "nt", reason="Windows alias lock ordering")
def test_windows_normal_and_extended_alias_updates_do_not_deadlock(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    paths = paths_for(tmp_path)
    paths.root.mkdir(parents=True)
    extended_root = "\\\\?\\" + str(paths.root)
    processes = [
        context.Process(
            target=_child_update_after_signal,
            args=(
                root,
                transport_for(hotword_data()),
                start,
                results,
            ),
        )
        for root in (str(paths.root), extended_root)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)

    assert all(process.exitcode == 0 for process in processes)
    outcomes = {results.get(timeout=3), results.get(timeout=3)}
    assert outcomes <= {
        ("updated", "2026.07.29"),
        ("current", "2026.07.29"),
    }
    assert outcomes


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_windows_mutex_name_normalizes_drive_and_unc_aliases(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    normal = StatePaths(root=root)
    extended = StatePaths(root=Path("\\\\?\\" + str(root)))

    assert updater_module._windows_mutex_name(normal) == (
        updater_module._windows_mutex_name(extended)
    )
    assert updater_module._windows_mutex_name(normal).startswith("Global\\")
    assert updater_module._normalize_windows_path_for_lock(
        r"\\Server\Share\State"
    ) == updater_module._normalize_windows_path_for_lock(
        r"\\?\UNC\server/share/state"
    )


class _FakeWaitKernel:
    def __init__(self, result, error=0):
        self.result = result
        self.error = error

    def WaitForSingleObject(self, handle, milliseconds):
        ctypes.set_last_error(self.error)
        return self.result


class _FakeCreateMutexKernel:
    def __init__(self, *, opened_handle, open_error=0):
        self.opened_handle = opened_handle
        self.open_error = open_error
        self.opened = []

    def CreateMutexW(self, security, initially_owned, name):
        ctypes.set_last_error(5)
        return 0

    def OpenMutexW(self, access, inherit_handle, name):
        self.opened.append((access, inherit_handle, name))
        ctypes.set_last_error(self.open_error)
        return self.opened_handle


class _FakeMutexSetKernel:
    def __init__(self):
        self.created = []
        self.released = []
        self.closed = []
        self.waits = 0

    def CreateMutexW(self, security, initially_owned, name):
        handle = 11 + len(self.created) * 11
        self.created.append((name, handle))
        return handle

    def OpenMutexW(self, access, inherit_handle, name):
        raise AssertionError("OpenMutexW should not be needed")

    def WaitForSingleObject(self, handle, milliseconds):
        self.waits += 1
        if self.waits == 1:
            return 0
        ctypes.set_last_error(6)
        return 0xFFFFFFFF

    def ReleaseMutex(self, handle):
        self.released.append(handle)
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


@pytest.mark.skipif(os.name != "nt", reason="Windows multi-mutex cleanup")
def test_windows_partial_mutex_acquisition_releases_and_closes_every_handle():
    kernel = _FakeMutexSetKernel()

    with pytest.raises(OSError) as exc_info:
        with updater_module._acquire_windows_mutex_names(
            kernel, ("Global\\z-path", "Global\\a-identity"), timeout=1.0
        ):
            raise AssertionError("partial acquisition must not yield")

    assert exc_info.value.winerror == 6
    assert [name for name, _handle in kernel.created] == [
        "Global\\a-identity",
        "Global\\z-path",
    ]
    assert kernel.released == [11]
    assert kernel.closed == [22, 11]


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex access handling")
def test_windows_create_access_denied_opens_existing_global_mutex():
    kernel = _FakeCreateMutexKernel(opened_handle=91)

    handle = updater_module._create_windows_mutex(kernel, "Global\\test-mutex")

    assert handle == 91
    assert kernel.opened == [(0x00100001, False, "Global\\test-mutex")]


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex access handling")
def test_windows_existing_mutex_access_denied_surfaces_native_error():
    kernel = _FakeCreateMutexKernel(opened_handle=0, open_error=5)

    with pytest.raises(OSError) as exc_info:
        updater_module._create_windows_mutex(kernel, "Global\\test-mutex")

    assert exc_info.value.winerror == 5


@pytest.mark.skipif(os.name != "nt", reason="Windows wait result handling")
def test_windows_wait_failed_surfaces_get_last_error_as_oserror():
    kernel = _FakeWaitKernel(0xFFFFFFFF, error=5)

    with pytest.raises(OSError) as exc_info:
        updater_module._wait_for_windows_mutex(kernel, 1, 10)

    assert exc_info.value.winerror == 5


@pytest.mark.skipif(os.name != "nt", reason="Windows wait result handling")
def test_windows_wait_timeout_is_distinct_but_abandoned_mutex_is_acquired():
    with pytest.raises(updater_module.UpdateLockTimeout):
        updater_module._wait_for_windows_mutex(_FakeWaitKernel(0x102), 1, 10)

    updater_module._wait_for_windows_mutex(_FakeWaitKernel(0x80), 1, 10)


def test_posix_lock_name_is_independent_of_hotword_directory_replacement(tmp_path):
    paths = paths_for(tmp_path)
    paths.root.mkdir(parents=True)
    hotwords = paths.hotwords_file.parent
    hotwords.mkdir()
    before = updater_module._posix_lock_name(paths)
    moved = paths.root / "old-hotwords"
    hotwords.rename(moved)
    hotwords.mkdir()

    after = updater_module._posix_lock_name(paths)

    assert after == before
    assert after.startswith("update-")
    assert after.endswith(".lock")


def test_posix_control_directory_is_outside_configured_state(tmp_path):
    paths = paths_for(tmp_path)
    home = tmp_path / "home"

    without_xdg = updater_module._posix_control_directory(environ={}, home=home)
    with_xdg = updater_module._posix_control_directory(
        environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")}, home=home
    )

    assert without_xdg == with_xdg
    assert without_xdg.is_absolute()
    assert not without_xdg.is_relative_to(paths.root)


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (errno.EACCES, True),
        (errno.EAGAIN, True),
        (getattr(errno, "EWOULDBLOCK", errno.EAGAIN), True),
        (errno.EIO, False),
        (errno.EBADF, False),
    ],
)
def test_posix_lock_retries_only_contention_errors(error_number, expected):
    assert updater_module._posix_lock_error_is_contention(error_number) is expected


def test_posix_lock_loop_raises_noncontention_native_error_without_retry():
    calls = 0

    def fail_with_io_error(descriptor):
        nonlocal calls
        calls += 1
        raise OSError(errno.EIO, "injected I/O failure")

    with pytest.raises(OSError) as exc_info:
        updater_module._acquire_posix_lock(7, 1.0, fail_with_io_error)

    assert exc_info.value.errno == errno.EIO
    assert calls == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock behavior")
def test_posix_lock_survives_hotword_directory_rename_and_replacement(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    hotwords = paths.hotwords_file.parent
    moved = paths.root / "hotwords-held"
    hotwords.rename(moved)
    hotwords.mkdir()
    transport = transport_for(hotword_data())

    blocked = update_hotwords(paths, MANIFEST_URL, transport, NOW, force=True)

    assert blocked.status is UpdateStatus.REJECTED
    assert transport.calls == []
    assert holder.is_alive()
    release.set()
    holder.join(10)
    assert holder.exitcode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process environment behavior")
def test_posix_same_user_contends_across_different_xdg_environments(
    tmp_path, monkeypatch
):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder_runtime = tmp_path / "holder-runtime"
    contender_runtime = tmp_path / "contender-runtime"
    holder_runtime.mkdir(mode=0o700)
    contender_runtime.mkdir(mode=0o700)
    holder = context.Process(
        target=_child_hold_lock_with_xdg,
        args=(str(paths.root), str(holder_runtime), ready, release),
    )
    holder.start()
    assert ready.wait(10)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(contender_runtime))
    transport = transport_for(hotword_data())

    try:
        blocked = update_hotwords(
            paths, MANIFEST_URL, transport, NOW, force=True
        )

        assert blocked.status is UpdateStatus.REJECTED
        assert transport.calls == []
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(10)
    assert holder.exitcode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and flock behavior")
def test_posix_symlink_state_alias_contends_on_the_same_control_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    paths.root.mkdir(parents=True)
    alias_root = tmp_path / "state-alias"
    alias_root.symlink_to(paths.root, target_is_directory=True)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    transport = transport_for(hotword_data())

    blocked = update_hotwords(
        StatePaths(root=alias_root), MANIFEST_URL, transport, NOW, force=True
    )

    assert blocked.status is UpdateStatus.REJECTED
    assert transport.calls == []
    assert holder.is_alive()
    release.set()
    holder.join(10)
    assert holder.exitcode == 0


def test_replacing_a_legacy_lock_path_cannot_split_the_os_lease(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    legacy_lock = paths.hotwords_file.parent / ".update.lock"
    replacement = paths.hotwords_file.parent / ".replacement.lock"
    replacement.write_text("replacement", encoding="utf-8")
    os.replace(replacement, legacy_lock)

    blocked = update_hotwords(
        paths, MANIFEST_URL, transport_for(hotword_data()), NOW, force=True
    )

    assert blocked.status is UpdateStatus.REJECTED
    assert holder.is_alive()
    release.set()
    holder.join(10)
    assert holder.exitcode == 0


def test_crashed_lock_owner_is_recovered_by_the_operating_system(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    paths = paths_for(tmp_path)
    holder = context.Process(
        target=_child_hold_lock, args=(str(paths.root), ready, release)
    )
    holder.start()
    assert ready.wait(10)
    holder.terminate()
    holder.join(10)
    assert holder.exitcode is not None and holder.exitcode != 0

    result = update_hotwords(
        paths, MANIFEST_URL, transport_for(hotword_data()), NOW, force=True
    )

    assert result.status is UpdateStatus.UPDATED

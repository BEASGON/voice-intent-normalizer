"""Security, transaction, and recovery tests for public hotword updates."""

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


def _child_hold_lock(root: str, ready, release) -> None:
    with updater_module._update_lock(StatePaths(root=Path(root))):
        ready.set()
        release.wait(10)


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


def test_same_version_late_worker_cannot_regress_receipt_or_current_timestamp(tmp_path):
    paths = paths_for(tmp_path)
    data = hotword_data()
    assert update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW).status is (
        UpdateStatus.UPDATED
    )
    later = NOW + timedelta(days=1)
    receipt = paths.hotwords_file.parent / "last-update.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2026.07.29",
                "last_check": later.isoformat(),
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


def test_implausibly_future_receipt_is_ignored_before_throttle_or_commit(tmp_path):
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
                "last_check": (
                    datetime.now(timezone.utc) + timedelta(days=366 * 6)
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = update_hotwords(paths, MANIFEST_URL, transport_for(data), NOW, force=True)

    assert result.status is UpdateStatus.CURRENT
    receipt_state = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_state["last_check"] == NOW.isoformat()


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

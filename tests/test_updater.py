"""Security, transaction, and recovery tests for public hotword updates."""

import hashlib
import json
import multiprocessing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import voice_intent_normalizer.updater as updater_module
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
    final_url: str | None = None
    redirects: tuple[str, ...] = ()


class FakeResponse:
    def __init__(self, route: Route) -> None:
        self._route = route
        self.final_url = route.final_url
        self.redirect_chain = route.redirects
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self._route.data[:size]

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: dict[str, bytes | Exception | Route]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.opened: list[FakeResponse] = []

    def open(self, url: str) -> FakeResponse:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        route = response if isinstance(response, Route) else Route(response, url)
        if route.final_url is None:
            route = Route(route.data, url, route.redirects)
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

    def open(self, url: str) -> FakeResponse:
        if url == self.blocked_url:
            self.ready.set()
            if not self.release.wait(10):
                raise TimeoutError("test release was not signalled")
        return super().open(url)


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
    assert [response.read_sizes for response in transport.opened] == [
        [256 * 1024 + 1],
        [10 * 1024 * 1024 + 1],
    ]
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


def test_rejects_any_unapproved_redirect_hop_before_accepting_bytes(tmp_path):
    data = hotword_data()
    transport = FakeTransport(
        {
            MANIFEST_URL: Route(
                manifest(data),
                final_url=MANIFEST_URL,
                redirects=("https://evil.example/redirect",),
            )
        }
    )

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.opened[0].read_sizes == []
    assert transport.opened[0].closed


def test_rejects_an_unbounded_redirect_chain_before_accepting_bytes(tmp_path):
    data = hotword_data()
    transport = FakeTransport(
        {
            MANIFEST_URL: Route(
                manifest(data),
                final_url=MANIFEST_URL,
                redirects=(MANIFEST_URL,) * 11,
            )
        }
    )

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert transport.opened[0].read_sizes == []


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


def test_corrupt_state_is_preserved_and_blocks_an_untrusted_rollback(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    state_file = paths.hotwords_file.parent / "last-update.json"
    state_file.write_text("not-json", encoding="utf-8")
    transport = FakeTransport({})

    result = update_hotwords(paths, MANIFEST_URL, transport, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert state_file.read_text(encoding="utf-8") == "not-json"
    assert transport.calls == []


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

    assert result.status is UpdateStatus.REJECTED
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

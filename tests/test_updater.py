"""Security and resilience tests for public hotword updates."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

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


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def paths_for(tmp_path):
    return StatePaths(root=tmp_path / ".voice-intent-normalizer")


def hotword_data(*, scope: str = "hot") -> bytes:
    return (
        '{"canonical":"OpenClaw","scope":"'
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
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "version": version,
            "data_url": data_url,
            "sha256": hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
        }
    ).encode()


def fetcher_for(data: bytes, **manifest_options: str) -> FakeFetcher:
    return FakeFetcher({
        MANIFEST_URL: manifest(data, **manifest_options),
        DATA_URL: data,
    })


def test_valid_update_replaces_hotwords_and_records_check(tmp_path):
    data = hotword_data()
    paths = paths_for(tmp_path)

    result = update_hotwords(paths, MANIFEST_URL, fetcher_for(data), NOW)

    assert result.status is UpdateStatus.UPDATED
    assert result.version == "2026.07.29"
    assert paths.hotwords_file.read_bytes() == data
    state = json.loads((paths.hotwords_file.parent / "last-update.json").read_text())
    assert state == {
        "last_check": "2026-07-29T12:00:00+00:00",
        "schema_version": 1,
        "version": "2026.07.29",
    }


def test_hash_failure_preserves_last_valid_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    data = hotword_data()
    fetcher = fetcher_for(data, sha256="0" * 64)

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


def test_invalid_hotword_jsonl_preserves_last_valid_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    data = hotword_data(scope="base")

    result = update_hotwords(paths, MANIFEST_URL, fetcher_for(data), NOW)

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


def test_rejects_non_https_or_unapproved_sources_without_egress(tmp_path):
    data = hotword_data()
    fetcher = FakeFetcher({"http://github.com/manifest.json": manifest(data)})

    result = update_hotwords(
        paths_for(tmp_path), "http://github.com/manifest.json", fetcher, NOW
    )

    assert result.status is UpdateStatus.REJECTED
    assert fetcher.calls == []


def test_rejects_unapproved_manifest_data_url_before_download(tmp_path):
    data = hotword_data()
    fetcher = FakeFetcher({
        MANIFEST_URL: manifest(data, data_url="https://example.com/zh-ai.jsonl"),
    })

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert fetcher.calls == [MANIFEST_URL]


def test_rejects_oversized_payloads_without_replacing_current_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    oversized_data = b"x" * (10 * 1024 * 1024 + 1)
    fetcher = fetcher_for(oversized_data)

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"


def test_failure_is_throttled_for_a_day_but_force_retries(tmp_path):
    paths = paths_for(tmp_path)
    unavailable = FakeFetcher({MANIFEST_URL: OSError("offline")})

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
    fetcher = FakeFetcher({MANIFEST_URL: manifest(hotword_data())})

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.CURRENT
    assert fetcher.calls == [MANIFEST_URL]


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
    fetcher = fetcher_for(hotword_data(), version="2026.07.29")

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"
    assert fetcher.calls == [MANIFEST_URL]


def test_corrupt_state_is_preserved_and_blocks_an_untrusted_rollback(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    state_file = paths.hotwords_file.parent / "last-update.json"
    state_file.write_text("not-json", encoding="utf-8")
    fetcher = FakeFetcher({})

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert state_file.read_text(encoding="utf-8") == "not-json"
    assert fetcher.calls == []


def test_rejects_boolean_manifest_schema_before_data_download(tmp_path):
    data = hotword_data()
    fetcher = FakeFetcher({
        MANIFEST_URL: json.dumps(
            {
                "schema_version": True,
                "version": "2026.07.29",
                "data_url": DATA_URL,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ).encode(),
    })

    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert fetcher.calls == [MANIFEST_URL]


def test_rejects_boolean_state_schema_without_fetching(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    state_file = paths.hotwords_file.parent / "last-update.json"
    state_file.write_text(
        json.dumps(
            {
                "schema_version": True,
                "version": "2026.07.29",
                "last_check": "2026-07-28T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    fetcher = FakeFetcher({})

    result = update_hotwords(paths, MANIFEST_URL, fetcher, NOW)

    assert result.status is UpdateStatus.REJECTED
    assert json.loads(state_file.read_text(encoding="utf-8"))["schema_version"] is True
    assert fetcher.calls == []

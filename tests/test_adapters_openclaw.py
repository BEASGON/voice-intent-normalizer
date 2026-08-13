"""OpenClaw host adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

from voice_intent_normalizer.adapters.base import InstallOptions, UninstallOptions
from voice_intent_normalizer.paths import StatePaths


def _adapter(
    tmp_path: Path,
    calls: list[tuple[str, ...]],
    responses: dict[tuple[str, ...], object],
):
    from voice_intent_normalizer.adapters.openclaw import OpenClawAdapter

    def run(args: tuple[str, ...]) -> object:
        calls.append(args)
        value = responses.get(args)
        if isinstance(value, Exception):
            raise value
        return value if value is not None else {"returncode": 0, "stdout": ""}

    return OpenClawAdapter(tmp_path, StatePaths(root=tmp_path / ".state"), run=run)


def test_openclaw_install_uses_argument_array_global_and_eligible_check(
    tmp_path: Path,
):
    calls: list[tuple[str, ...]] = []
    install = (
        "openclaw",
        "skills",
        "install",
        str(tmp_path),
        "--as",
        "voice-intent-normalizer",
        "--global",
    )
    check = ("openclaw", "skills", "check", "--json")
    adapter = _adapter(
        tmp_path,
        calls,
        {
            check: {
                "returncode": 0,
                "stdout": json.dumps(
                    {"skills": [{"name": "voice-intent-normalizer", "eligible": True}]}
                ),
            }
        },
    )

    result = adapter.install(InstallOptions())

    assert result.status == "installed"
    assert calls == [install, check]


def test_openclaw_workspace_install_omits_global(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    adapter = _adapter(
        tmp_path,
        calls,
        {
            check: {
                "returncode": 0,
                "stdout": (
                    '{"skills":[{"name":"voice-intent-normalizer","eligible":true}]}'
                ),
            }
        },
    )

    adapter.install(InstallOptions(workspace=tmp_path / "project"))

    assert "--global" not in calls[0]


def test_openclaw_missing_cli_is_unavailable(tmp_path: Path):
    adapter = _adapter(
        tmp_path,
        [],
        {("openclaw", "skills", "check", "--json"): FileNotFoundError()},
    )
    result = adapter.detect()
    assert result.capability.value == "unavailable"
    assert "OpenClaw CLI" in result.messages[0]


def test_openclaw_rejects_ineligible_check_json(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    adapter = _adapter(
        tmp_path,
        calls,
        {
            check: {
                "returncode": 0,
                "stdout": (
                    '{"skills":[{"name":"voice-intent-normalizer","eligible":false}]}'
                ),
            }
        },
    )
    assert adapter.install(InstallOptions()).status == "failed"


def test_openclaw_uninstall_manual_fallback_preserves_unverified_target(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    adapter = _adapter(tmp_path, calls, {})
    result = adapter.uninstall(UninstallOptions())
    assert result.status == "not-installed"
    assert any("manual" in message for message in result.messages)


def test_openclaw_rejects_duplicate_check_json_keys(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    adapter = _adapter(
        tmp_path,
        calls,
        {check: {"returncode": 0, "stdout": '{"skills":[],"skills":[]}'}},
    )

    assert adapter.detect().status == "degraded"


def test_openclaw_reinstall_is_idempotent_after_eligible_verification(
    tmp_path: Path,
):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    response = {
        "returncode": 0,
        "stdout": '{"skills":[{"name":"voice-intent-normalizer","eligible":true}]}',
    }
    adapter = _adapter(tmp_path, calls, {check: response})

    adapter.install(InstallOptions())
    calls.clear()
    result = adapter.install(InstallOptions())

    assert result.status == "already-installed"
    assert calls == [check]


def test_openclaw_upgrade_backs_up_the_previous_receipt(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    eligible = {
        "returncode": 0,
        "stdout": '{"skills":[{"name":"voice-intent-normalizer","eligible":true}]}',
    }
    adapter = _adapter(tmp_path, calls, {check: eligible})
    adapter.install(InstallOptions())
    source = tmp_path / "new-skill-file"
    source.write_text("new", encoding="utf-8")

    result = adapter.install(InstallOptions())

    assert result.status == "upgraded"
    assert (tmp_path / ".state" / "adapters" / "openclaw.json.bak").exists()


def test_openclaw_source_digest_is_stable_when_directory_order_reverses(
    tmp_path: Path, monkeypatch
):
    from voice_intent_normalizer.adapters import openclaw

    adapter = _adapter(tmp_path, [], {})
    (tmp_path / "a-skill-file").write_text("a", encoding="utf-8")
    (tmp_path / "z-skill-file").write_text("z", encoding="utf-8")
    original = adapter._source_digest(tmp_path)
    native_scandir = openclaw.os.scandir

    def reverse_scandir(path):
        return iter(reversed(tuple(native_scandir(path))))

    monkeypatch.setattr(openclaw.os, "scandir", reverse_scandir)

    assert adapter._source_digest(tmp_path) == original


def test_openclaw_manual_fallback_preserves_a_verified_target(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    target = tmp_path / "foreign-target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("do not remove", encoding="utf-8")
    adapter = _adapter(
        tmp_path,
        calls,
        {
            check: {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {
                                "name": "voice-intent-normalizer",
                                "eligible": True,
                                "path": str(target),
                            }
                        ]
                    }
                ),
            }
        },
    )
    adapter._write_receipt(
        adapter._receipt_payload(
            source=tmp_path,
            source_digest=adapter._source_digest(tmp_path),
            workspace=None,
            target=str(target),
        )
    )

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "degraded"
    assert marker.read_text(encoding="utf-8") == "do not remove"
    assert any("manual fallback" in message for message in result.messages)

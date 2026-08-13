"""OpenClaw host adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

from voice_intent_normalizer.adapters.base import InstallOptions, UninstallOptions
from voice_intent_normalizer.paths import StatePaths

ROOT = Path(__file__).parents[1]


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

    return OpenClawAdapter(ROOT, StatePaths(root=tmp_path / ".state"), run=run)


def test_openclaw_install_uses_argument_array_global_and_eligible_check(
    tmp_path: Path,
):
    calls: list[tuple[str, ...]] = []
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
    assert calls[-1] == check
    install = calls[0]
    assert install[:3] == ("openclaw", "skills", "install")
    assert install[4:] == ("--as", "voice-intent-normalizer", "--global")
    source = Path(install[3])
    assert source != tmp_path
    assert source.is_relative_to(tmp_path / ".state")
    assert not (source / "tests").exists()
    assert (source / "SKILL.md").is_file()


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
        "stdout": json.dumps(
            {
                "skills": [
                    {
                        "name": "voice-intent-normalizer",
                        "eligible": True,
                        "path": str(tmp_path / "managed"),
                    }
                ]
            }
        ),
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
    receipt = adapter._receipt_path()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["source_digest"] = "0" * 64
    adapter._write_receipt(payload)

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


def test_openclaw_official_uninstall_preserves_a_replaced_target(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    original = tmp_path / "managed-target"
    replacement = tmp_path / "replacement-target"
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
                                "path": str(replacement),
                            }
                        ]
                    }
                ),
            },
            ("openclaw", "skills", "--help"): {
                "returncode": 0,
                "stdout": "install uninstall check",
            },
        },
    )
    adapter._write_receipt(
        adapter._receipt_payload(
            source=tmp_path,
            source_digest=adapter._source_digest(tmp_path),
            workspace=None,
            target=str(original),
        )
    )

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "degraded"
    assert ("openclaw", "skills", "uninstall", "voice-intent-normalizer") not in calls


def test_openclaw_official_uninstall_requires_a_current_owned_target(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    adapter = _adapter(
        tmp_path,
        calls,
        {
            check: {"returncode": 0, "stdout": json.dumps({"skills": []})},
            ("openclaw", "skills", "--help"): {
                "returncode": 0,
                "stdout": "install uninstall check",
            },
        },
    )
    adapter._write_receipt(
        adapter._receipt_payload(
            source=tmp_path,
            source_digest=adapter._source_digest(tmp_path),
            workspace=None,
            target=str(tmp_path / "old-target"),
        )
    )

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "degraded"
    assert ("openclaw", "skills", "uninstall", "voice-intent-normalizer") not in calls


def test_openclaw_idempotence_requires_the_current_owned_target(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    check = ("openclaw", "skills", "check", "--json")
    current = {
        "name": "voice-intent-normalizer",
        "path": str(tmp_path / "managed"),
        "eligible": True,
    }
    responses = {
        check: {"returncode": 0, "stdout": json.dumps({"skills": [current]})}
    }
    adapter = _adapter(
        tmp_path,
        calls,
        responses,
    )
    assert adapter.install(InstallOptions()).status == "installed"
    current["path"] = str(tmp_path / "foreign")
    responses[check] = {
        "returncode": 0,
        "stdout": json.dumps({"skills": [current]}),
    }

    result = adapter.install(InstallOptions())

    assert result.status == "upgraded"
    assert sum(call[:3] == ("openclaw", "skills", "install") for call in calls) == 2

"""Release-contract coverage for public failures and recoveries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_intent_normalizer.adapters.base import (
    AdapterResult,
    CapabilityLevel,
    InstallOptions,
    UninstallOptions,
)
from voice_intent_normalizer.adapters.codex import CodexAdapter
from voice_intent_normalizer.adapters.openclaw import OpenClawAdapter
from voice_intent_normalizer.adapters.workbuddy import WorkBuddyAdapter
from voice_intent_normalizer.cli import main
from voice_intent_normalizer.installer import Installer
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.updater import UpdateStatus, update_hotwords

ROOT = Path(__file__).resolve().parents[1]


class _OpenClawRun:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return self.replies[args]


def _openclaw_check(skills, *, returncode=0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps({"skills": skills}),
    )


def test_installer_isolates_adapter_failure_without_skipping_later_platform():
    class BrokenAdapter:
        platform = "broken"

        def detect(self):
            raise OSError("unavailable")

        def install(self, options):
            raise OSError("unavailable")

    class WorkingAdapter:
        platform = "working"

        def detect(self):
            return AdapterResult(
                self.platform, "detected", CapabilityLevel.MANUAL
            )

        def install(self, options):
            return AdapterResult(
                self.platform, "installed", CapabilityLevel.MANUAL
            )

    results = Installer(
        {"broken": BrokenAdapter(), "working": WorkingAdapter()}
    ).install(("broken", "working"), InstallOptions())

    assert [result.status for result in results] == ["degraded", "installed"]


def test_workbuddy_declines_strict_and_shared_data_deletion(tmp_path):
    adapter = WorkBuddyAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    strict = adapter.install(InstallOptions(strict=True))
    removal = adapter.uninstall(UninstallOptions(remove_shared_data=True))

    assert strict.status == "failed"
    assert removal.status == "failed"


def test_openclaw_does_not_treat_codex_skills_as_its_source(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    (codex_home / "skills").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = OpenClawAdapter(
        codex_home / "skills",
        StatePaths(root=tmp_path / "state"),
    )

    result = adapter.install(InstallOptions())

    assert result.status == "failed"


def test_codex_strict_install_and_uninstall_preserve_shared_state(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    installed = adapter.install(InstallOptions(strict=True))
    doctor = adapter.doctor()
    removed = adapter.uninstall(UninstallOptions())

    assert installed.status == "installed"
    assert doctor.capability is CapabilityLevel.AUTOMATIC
    assert removed.status == "uninstalled"


def test_cli_json_install_rejects_implicit_update_choice(tmp_path):
    service = type("Service", (), {"paths": StatePaths(root=tmp_path / "state")})()
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["install", "--platform", "workbuddy", "--json"],
        service=service,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert stdout.getvalue() == ""
    assert "--auto-update" in stderr.getvalue()


def test_update_rejects_untrusted_manifest_before_transport(tmp_path):
    class NoNetwork:
        def open_no_redirect(self, url):
            raise AssertionError("untrusted URL must not be fetched")

    result = update_hotwords(
        StatePaths(root=tmp_path / "state"),
        "https://invalid.example/manifest.json",
        NoNetwork(),
        datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    assert result.status is UpdateStatus.REJECTED
    assert result.message == "manifest source rejected"


@pytest.mark.parametrize(
    ("skills", "status"),
    [
        ([], "not-installed"),
        ([{"name": "voice-intent-normalizer", "eligible": False}], "not-installed"),
        ([{"name": "voice-intent-normalizer", "eligible": True}], "installed"),
    ],
)
def test_openclaw_doctor_reports_verified_eligibility(tmp_path, skills, status):
    command = ("openclaw", "skills", "check", "--json")
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun({command: _openclaw_check(skills)}),
    )

    result = adapter.doctor()

    assert result.status == status


def test_openclaw_doctor_degrades_for_invalid_official_result(tmp_path):
    command = ("openclaw", "skills", "check", "--json")
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun(
            {command: SimpleNamespace(returncode=0, stdout='{"skills": {}}')}
        ),
    )

    assert adapter.doctor().status == "degraded"


def test_openclaw_uninstall_without_receipt_is_manual_and_non_destructive(tmp_path):
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun({}),
    )

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "not-installed"
    assert "manual fallback" in result.messages[0]


@pytest.mark.parametrize(
    ("prepare", "status"),
    [
        (lambda home: None, "not-installed"),
        (lambda home: (home / "AGENTS.md").write_text("user rules\n"), "degraded"),
    ],
)
def test_codex_doctor_distinguishes_absent_and_incomplete_installation(
    tmp_path, monkeypatch, prepare, status
):
    home = tmp_path / "codex-home"
    home.mkdir()
    prepare(home)
    monkeypatch.setenv("CODEX_HOME", str(home))

    result = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state")).doctor()

    assert result.status == status


def test_codex_uninstall_refuses_shared_data_removal(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    result = adapter.uninstall(UninstallOptions(remove_shared_data=True))

    assert result.status == "failed"


def test_codex_strict_install_rejects_malformed_hook_document(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "hooks.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    result = adapter.install(InstallOptions(strict=True))

    assert result.status == "failed"
    assert not (home / "skills" / "voice-intent-normalizer").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--platform", "missing", "--json"],
        [
            "uninstall",
            "--platform",
            "workbuddy",
            "--output-dir",
            "out",
            "--json",
        ],
    ],
)
def test_cli_json_management_commands_return_adapter_results(tmp_path, argv):
    service = type("Service", (), {"paths": StatePaths(root=tmp_path / "state")})()
    stdout = StringIO()

    code = main(argv, service=service, stdout=stdout, stderr=StringIO())

    assert code == 0
    assert json.loads(stdout.getvalue())[0]["platform"] in {
        "missing",
        "workbuddy",
    }


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"text": 3}, TypeError),
        ({"text": "x", "domains": ("x",) * 33}, ValueError),
        ({"text": "x", "notified_pairs": {("a", 3)}}, ValueError),
    ],
)
def test_normalize_request_rejects_invalid_public_inputs(kwargs, exception):
    from voice_intent_normalizer.service import NormalizeRequest

    with pytest.raises(exception):
        NormalizeRequest(**kwargs)

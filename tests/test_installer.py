"""Installer contracts and generic skill-package safety."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from voice_intent_normalizer.adapters.base import (
    AdapterResult,
    CapabilityLevel,
    InstallOptions,
    UninstallOptions,
)
from voice_intent_normalizer.adapters.generic import GenericAdapter
from voice_intent_normalizer.installer import Installer
from voice_intent_normalizer.paths import StatePaths


class _Adapter:
    def __init__(self, platform: str, result: AdapterResult | Exception) -> None:
        self.platform = platform
        self._result = result

    def detect(self) -> AdapterResult:
        return AdapterResult(self.platform, "detected", CapabilityLevel.MANUAL)

    def install(self, options: InstallOptions) -> AdapterResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def doctor(self) -> AdapterResult:
        return self.detect()

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        return self.detect()


def test_installer_preserves_requested_order_and_reports_operational_failure():
    installed = AdapterResult("codex", "installed", CapabilityLevel.IMPLICIT)
    manual = AdapterResult("workbuddy", "installed", CapabilityLevel.MANUAL)
    installer = Installer(
        {
            "codex": _Adapter("codex", installed),
            "openclaw": _Adapter("openclaw", OSError("not available")),
            "workbuddy": _Adapter("workbuddy", manual),
        }
    )

    results = installer.install(("codex", "openclaw", "workbuddy"), InstallOptions())

    assert [result.platform for result in results] == [
        "codex",
        "openclaw",
        "workbuddy",
    ]
    assert results[0] == installed
    assert results[1].status == "degraded"
    assert results[2] == manual


def test_generic_install_copies_runnable_allowlisted_package_and_preserves_files(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    unrelated = root / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)

    result = adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"

    assert result.status == "installed"
    assert target.joinpath("SKILL.md").is_file()
    assert target.joinpath("scripts", "voice_intent.py").is_file()
    assert target.joinpath("src", "voice_intent_normalizer", "cli.py").is_file()
    assert not target.joinpath("tests").exists()
    assert not target.joinpath(".git").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    status = json.loads(state.adapter_status_file("generic").read_text("utf-8"))
    assert status["managed_directory"] == str(target)


def test_generic_install_is_zero_install_runnable(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    GenericAdapter(repository, state).install(InstallOptions(output_dir=root))

    result = subprocess.run(
        [
            sys.executable,
            str(root / "voice-intent-normalizer" / "scripts" / "voice_intent.py"),
            "doctor",
            "--json",
        ],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": "",
            "VOICE_INTENT_HOME": str(tmp_path / "runtime-state"),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}


def test_generic_uninstall_preserves_shared_personal_data(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    state.root.mkdir()
    state.personal_file.write_text('{"canonical":"OpenClaw"}\n', encoding="utf-8")
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    result = adapter.uninstall(UninstallOptions(remove_shared_data=False))

    assert result.status == "uninstalled"
    assert state.personal_file.exists()
    assert not (root / "voice-intent-normalizer").exists()


def test_generic_refuses_to_overwrite_unmanaged_directory(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    target = root / "voice-intent-normalizer"
    target.mkdir(parents=True)
    marker = target / "mine.txt"
    marker.write_text("do not replace", encoding="utf-8")
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})

    result = GenericAdapter(repository, state).install(InstallOptions(output_dir=root))

    assert result.status == "failed"
    assert marker.read_text(encoding="utf-8") == "do not replace"


def test_generic_rolls_back_package_when_status_recording_fails(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    monkeypatch.setattr(
        adapter,
        "_write_status",
        lambda target, options: (_ for _ in ()).throw(OSError("no state")),
    )

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "failed"
    assert not (root / "voice-intent-normalizer").exists()


def test_generic_reinstall_preserves_unmanaged_files_and_uninstall_keeps_them(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    custom = target / "my-host-note.txt"
    custom.write_text("preserve", encoding="utf-8")

    second = adapter.install(InstallOptions(output_dir=root))
    removed = adapter.uninstall(UninstallOptions())

    assert second.status == "already-installed"
    assert custom.read_text(encoding="utf-8") == "preserve"
    assert removed.status == "uninstalled"
    assert custom.read_text(encoding="utf-8") == "preserve"
    assert target.exists()


def test_generic_uninstall_rejects_tampered_manifest_without_touching_outside_file(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    outside = root / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    manifest = target / ".voice-intent-normalizer-install.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"].append("../outside.txt")
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "failed"
    assert outside.read_text(encoding="utf-8") == "keep"
    assert target.exists()


def test_generic_doctor_never_claims_automatic_trigger(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    result = adapter.doctor()

    assert result.capability is CapabilityLevel.MANUAL
    assert any("manual" in message for message in result.messages)
    assert "automatic trigger: unavailable" in result.messages


def test_cli_json_install_requires_explicit_platform_without_prompting(monkeypatch):
    from io import StringIO

    from voice_intent_normalizer import cli

    def no_prompt(*args, **kwargs):
        raise AssertionError("JSON mode must not prompt")

    monkeypatch.setattr("builtins.input", no_prompt)
    stdout = StringIO()
    stderr = StringIO()
    code = cli.main(["install", "--json"], stdout=stdout, stderr=stderr)

    assert code == 2
    assert stdout.getvalue() == ""
    assert "--platform" in stderr.getvalue()


def test_cli_install_forwards_repeated_platforms_to_injected_installer():
    from io import StringIO

    from voice_intent_normalizer import cli

    class RecordingInstaller:
        def __init__(self) -> None:
            self.request = None

        def install(self, platforms, options):
            self.request = (tuple(platforms), options)
            return (
                AdapterResult("generic", "installed", CapabilityLevel.MANUAL),
                AdapterResult("other", "degraded", CapabilityLevel.UNAVAILABLE),
            )

    installer = RecordingInstaller()
    stdout = StringIO()
    code = cli.main(
        [
            "install",
            "--platform",
            "generic",
            "--platform",
            "other",
            "--no-auto-update",
            "--json",
        ],
        stdout=stdout,
        installer=installer,
    )

    assert code == 0
    assert installer.request[0] == ("generic", "other")
    assert installer.request[1].auto_update is False
    assert json.loads(stdout.getvalue())[1]["status"] == "degraded"

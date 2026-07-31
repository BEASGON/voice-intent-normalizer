"""Installer contracts and generic skill-package safety."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from voice_intent_normalizer.adapters.base import (
    AdapterResult,
    CapabilityLevel,
    InstallOptions,
    UninstallOptions,
)
from voice_intent_normalizer.adapters.generic import GenericAdapter
from voice_intent_normalizer.installer import Installer
from voice_intent_normalizer.paths import StatePaths, StateRootLease


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


def test_installer_deduplicates_and_never_stops_other_platforms_in_strict_mode():
    installer = Installer(
        {
            "broken": _Adapter("broken", OSError("offline")),
            "working": _Adapter(
                "working", AdapterResult("working", "installed", CapabilityLevel.MANUAL)
            ),
        }
    )

    results = installer.install(
        ("broken", "broken", "working"), InstallOptions(strict=True)
    )

    assert [result.platform for result in results] == ["broken", "working"]


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


def test_uninstall_uses_explicit_skill_root_not_tampered_status(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    victim = tmp_path / "victim"
    shutil.copytree(root / "voice-intent-normalizer", victim)
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    status = state.adapter_status_file("generic")
    payload = json.loads(status.read_text(encoding="utf-8"))
    payload["managed_directory"] = str(victim)
    status.write_text(json.dumps(payload), encoding="utf-8")

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "failed"
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (root / "voice-intent-normalizer").exists()


def test_uninstall_restores_every_file_when_status_removal_fails(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    marker = target / "SKILL.md"
    before = marker.read_bytes()
    monkeypatch.setattr(
        adapter,
        "_remove_status",
        lambda: (_ for _ in ()).throw(OSError("status unavailable")),
    )

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "failed"
    assert marker.read_bytes() == before
    assert state.adapter_status_file("generic").exists()


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


def test_generic_keeps_identity_bound_package_when_status_recording_fails(
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

    assert result.status == "degraded"
    assert (root / "voice-intent-normalizer").is_dir()


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


def test_cli_rejects_platform_with_all_detected_before_calling_installer():
    from io import StringIO

    from voice_intent_normalizer import cli

    code = cli.main(
        ["uninstall", "--platform", "generic", "--all-detected", "--json"],
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert code == 2


def test_generic_reinstall_repairs_missing_managed_file_and_preserves_unknown(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    first = adapter.install(InstallOptions(output_dir=root))
    assert first.status == "installed"
    target = root / "voice-intent-normalizer"
    custom = target / "host-note.txt"
    custom.write_text("preserve", encoding="utf-8")
    (target / "SKILL.md").unlink()

    repaired = adapter.install(InstallOptions(output_dir=root))

    assert repaired.status == "repaired"
    assert (target / "SKILL.md").read_bytes() == (repository / "SKILL.md").read_bytes()
    assert custom.read_text(encoding="utf-8") == "preserve"
    assert adapter.doctor().status == "installed"


def test_generic_reinstall_rebuilds_missing_status_and_capability(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    status = state.adapter_status_file("generic")
    status.unlink()

    repaired = adapter.install(
        InstallOptions(output_dir=root, implicit_invocation_confirmed=True)
    )

    assert repaired.status == "repaired"
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["capability"] == "implicit"
    assert repaired.changed_paths == (status,)


def test_generic_doctor_reports_missing_status_for_known_managed_target(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    state.adapter_status_file("generic").unlink()

    result = adapter.doctor()

    assert result.status == "degraded"
    assert "adapter status: missing" in result.messages
    assert "managed package: valid" in result.messages


def test_generic_doctor_reports_manifest_hash_failure(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    (target / "SKILL.md").write_text("tampered", encoding="utf-8")

    result = adapter.doctor()

    assert result.status == "degraded"
    assert "managed package: hash mismatch" in result.messages
    assert "required file SKILL.md: invalid" in result.messages


def test_generic_uninstall_restoration_failure_is_recovery_visible(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"

    def fail_status():
        raise OSError("status unavailable")

    monkeypatch.setattr(adapter, "_remove_status", fail_status)
    original_restore = adapter._restore_quarantine

    def incomplete_restore(quarantine, managed_target, moved):
        original_restore(quarantine, managed_target, moved[:-1])

    monkeypatch.setattr(adapter, "_restore_quarantine", incomplete_restore)

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert result.changed_paths
    assert any("recovery" in message for message in result.messages)
    assert adapter.doctor().status == "degraded"
    assert any("recovery" in message for message in adapter.doctor().messages)
    assert target.exists()


def test_generic_uninstall_cleanup_failure_is_recovery_visible(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    monkeypatch.setattr(
        adapter,
        "_discard_quarantine",
        lambda quarantine: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert result.changed_paths
    assert any("cleanup" in message for message in result.messages)
    doctor = adapter.doctor()
    assert doctor.status == "degraded"
    assert any("recovery" in message for message in doctor.messages)


def test_generic_uninstall_rejects_managed_parent_alias_without_external_mutation(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    outside = tmp_path / "outside-assets"
    shutil.copytree(target / "assets", outside)
    shutil.rmtree(target / "assets")
    try:
        if os.name == "nt":
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target / "assets"), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            if created.returncode:
                pytest.skip(f"junction creation unavailable: {created.stderr}")
        else:
            (target / "assets").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory alias creation unavailable: {exc}")
    before = {
        path.relative_to(outside): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    }

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    after = {
        path.relative_to(outside): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert after == before


def test_generic_success_changed_paths_include_files_directories_and_status(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)

    installed = adapter.install(InstallOptions(output_dir=root))

    target = root / "voice-intent-normalizer"
    assert target in installed.changed_paths
    assert target / "src" in installed.changed_paths
    assert target / "SKILL.md" in installed.changed_paths
    assert state.adapter_status_file("generic") in installed.changed_paths

    removed = adapter.uninstall(UninstallOptions(output_dir=root))

    assert target in removed.changed_paths
    assert target / "src" in removed.changed_paths
    assert target / "SKILL.md" in removed.changed_paths
    assert state.adapter_status_file("generic") in removed.changed_paths


def test_cli_uninstall_forwards_strict_and_explicit_shared_data_option():
    from io import StringIO

    from voice_intent_normalizer import cli

    class RecordingInstaller:
        def __init__(self) -> None:
            self.request = None

        def uninstall(self, platforms, options):
            self.request = (tuple(platforms), options)
            return (AdapterResult("generic", "failed", CapabilityLevel.UNAVAILABLE),)

    installer = RecordingInstaller()
    stdout = StringIO()
    code = cli.main(
        [
            "uninstall",
            "--platform",
            "generic",
            "--strict",
            "--remove-shared-data",
            "--json",
        ],
        stdout=stdout,
        installer=installer,
    )

    assert code == 0
    assert installer.request[0] == ("generic",)
    assert installer.request[1].strict is True
    assert installer.request[1].remove_shared_data is True
    assert json.loads(stdout.getvalue())[0]["status"] == "failed"


def test_generic_quarantine_move_failure_restores_every_victim(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    original_replace = StateRootLease.replace
    moves = 0

    def fail_third_move(lease, source, destination):
        nonlocal moves
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.parts
            and source_path.parts[0] == "voice-intent-normalizer"
            and destination_path.parts
            and destination_path.parts[0].startswith(
                ".voice-intent-normalizer.quarantine-"
            )
        ):
            moves += 1
            if moves == 3:
                raise OSError("injected move failure")
        return original_replace(lease, source, destination)

    monkeypatch.setattr(StateRootLease, "replace", fail_third_move)

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert result.changed_paths == ()
    assert after == before
    assert not tuple(root.glob(".voice-intent-normalizer.quarantine-*"))
    assert not (state.root / "adapters" / "generic-recovery.json").exists()


def test_generic_missing_manifest_preflight_has_no_mutation(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    (target / ".voice-intent-normalizer-install.json").unlink()
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    status_before = state.adapter_status_file("generic").read_bytes()

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert result.changed_paths == ()
    assert after == before
    assert state.adapter_status_file("generic").read_bytes() == status_before
    assert not tuple(root.glob(".voice-intent-normalizer.quarantine-*"))


def test_generic_status_failure_never_rolls_back_a_swapped_target(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    target = root / "voice-intent-normalizer"
    displaced = root / "displaced-package"
    victim_marker = target / "victim.txt"

    def swap_then_fail(installed_target, options):
        installed_target.rename(displaced)
        installed_target.mkdir()
        victim_marker.write_text("victim", encoding="utf-8")
        raise OSError("status unavailable")

    monkeypatch.setattr(adapter, "_write_status", swap_then_fail)

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert victim_marker.read_text(encoding="utf-8") == "victim"
    assert displaced.joinpath("SKILL.md").is_file()


def test_generic_uninstall_rejects_target_swapped_after_manifest_validation(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    displaced = root / "displaced-package"
    original_prepare = adapter._prepare_quarantine

    def swap_then_prepare(quarantine, managed_target, paths):
        managed_target.rename(displaced)
        shutil.copytree(displaced, managed_target)
        (managed_target / "victim.txt").write_text("victim", encoding="utf-8")
        original_prepare(quarantine, managed_target, paths)

    monkeypatch.setattr(adapter, "_prepare_quarantine", swap_then_prepare)

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "failed"
    assert (target / "SKILL.md").is_file()
    assert (target / "victim.txt").read_text(encoding="utf-8") == "victim"
    assert (displaced / "SKILL.md").is_file()


def _copy_runtime_repository(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.pyc",
            "dist",
            "build",
        ),
    )


@pytest.mark.parametrize("corruption", ["skill", "bootstrap"])
def test_generic_rejects_invalid_staged_package_before_target_mutation(
    tmp_path: Path, corruption: str
):
    repository = Path(__file__).resolve().parents[1]
    broken = tmp_path / "repository"
    _copy_runtime_repository(repository, broken)
    if corruption == "skill":
        (broken / "SKILL.md").write_text(
            "---\nname: a-different-skill\n---\n", encoding="utf-8"
        )
    else:
        (broken / "scripts" / "voice_intent.py").write_text(
            "raise SystemExit(23)\n", encoding="utf-8"
        )
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})

    result = GenericAdapter(broken, state).install(InstallOptions(output_dir=root))

    assert result.status == "failed"
    assert not (root / "voice-intent-normalizer").exists()
    assert not state.adapter_status_file("generic").exists()
    assert not tuple(root.glob(".voice-intent-normalizer.staging-*"))


def test_installer_detect_and_doctor_isolate_exceptions_and_preserve_order():
    class DetectFailure(_Adapter):
        def detect(self):
            raise OSError("detect failed")

        def doctor(self):
            raise OSError("doctor failed")

    installer = Installer(
        {
            "bad": DetectFailure(
                "bad", AdapterResult("bad", "installed", CapabilityLevel.MANUAL)
            ),
            "good": _Adapter(
                "good", AdapterResult("good", "installed", CapabilityLevel.MANUAL)
            ),
        }
    )

    assert installer.detected() == ("good",)
    results = installer.doctor(("bad", "good", "bad"))

    assert [result.platform for result in results] == ["bad", "good"]
    assert results[0].status == "degraded"
    assert results[1].platform == "good"


def test_generic_remove_shared_data_is_explicitly_refused_and_preserves_data(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    state.root.mkdir()
    state.personal_file.write_text("personal\n", encoding="utf-8")
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    result = adapter.uninstall(
        UninstallOptions(output_dir=root, remove_shared_data=True, strict=True)
    )

    assert result.status == "failed"
    assert "not performed" in result.messages[0]
    assert state.personal_file.read_text(encoding="utf-8") == "personal\n"
    assert (root / "voice-intent-normalizer").exists()


_SUBPROCESS_ADAPTER = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from voice_intent_normalizer.adapters.base import InstallOptions, UninstallOptions
from voice_intent_normalizer.adapters.generic import GenericAdapter
from voice_intent_normalizer.paths import StatePaths

repository = Path(sys.argv[2])
output = Path(sys.argv[3])
state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": sys.argv[4]})
adapter = GenericAdapter(repository, state)
if sys.argv[5] == "install":
    result = adapter.install(InstallOptions(output_dir=output))
else:
    result = adapter.uninstall(UninstallOptions(output_dir=output))
print(json.dumps({"status": result.status, "messages": result.messages}))
"""


def _adapter_process(
    repository: Path, output: Path, state: Path, operation: str
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_ADAPTER,
            str(repository / "src"),
            str(repository),
            str(output),
            str(state),
            operation,
        ],
        cwd=output.parent,
        env={**os.environ, "PYTHONPATH": ""},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _process_result(process: subprocess.Popen[str]) -> dict[str, object]:
    stdout, stderr = process.communicate(timeout=90)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


def test_two_real_process_installs_are_serialized_and_idempotent(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state_root = tmp_path / "state"

    first = _adapter_process(repository, root, state_root, "install")
    second = _adapter_process(repository, root, state_root, "install")
    results = (_process_result(first), _process_result(second))

    assert all(
        result["status"] in {"installed", "already-installed", "repaired"}
        for result in results
    )
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(state_root)})
    doctor = GenericAdapter(repository, state).doctor()
    assert doctor.status == "installed"
    assert (root / "voice-intent-normalizer" / "SKILL.md").is_file()


def test_real_process_install_and_uninstall_are_serialized(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state_root = tmp_path / "state"
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(state_root)})
    GenericAdapter(repository, state).install(InstallOptions(output_dir=root))

    installing = _adapter_process(repository, root, state_root, "install")
    uninstalling = _adapter_process(repository, root, state_root, "uninstall")
    results = (_process_result(installing), _process_result(uninstalling))

    assert all(
        result["status"]
        in {"installed", "already-installed", "repaired", "uninstalled"}
        for result in results
    )
    target = root / "voice-intent-normalizer"
    status = state.adapter_status_file("generic")
    assert target.exists() is status.exists()
    if target.exists():
        assert GenericAdapter(repository, state).doctor().status == "installed"


def test_generic_version_upgrade_preserves_unknown_and_refreshes_manifest(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    unknown = target / "host-owned.txt"
    unknown.write_text("preserve", encoding="utf-8")
    manifest_path = target / ".voice-intent-normalizer-install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_version"] = "0.0.1"
    package_identity = json.dumps(
        {
            "hashes": manifest["hashes"],
            "required": manifest["required_files"],
            "version": "0.0.1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    manifest["package_hash"] = hashlib.sha256(package_identity).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    result = adapter.install(InstallOptions(output_dir=root))

    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "upgraded"
    assert refreshed["package_version"] == "0.1.0"
    assert unknown.read_text(encoding="utf-8") == "preserve"
    assert adapter.doctor().status == "installed"


def test_generic_upgrade_move_failure_fully_restores_old_package(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    manifest_path = target / ".voice-intent-normalizer-install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_version"] = "0.0.1"
    package_identity = json.dumps(
        {
            "hashes": manifest["hashes"],
            "required": manifest["required_files"],
            "version": "0.0.1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    manifest["package_hash"] = hashlib.sha256(package_identity).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    original_replace = StateRootLease.replace
    new_moves = 0

    def fail_new_move(lease, source, destination):
        nonlocal new_moves
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.parts
            and source_path.parts[0].startswith(".voice-intent-normalizer.staging-")
            and destination_path.parts
            and destination_path.parts[0] == "voice-intent-normalizer"
        ):
            new_moves += 1
            if new_moves == 3:
                raise OSError("injected upgrade failure")
        return original_replace(lease, source, destination)

    monkeypatch.setattr(StateRootLease, "replace", fail_new_move)

    result = adapter.install(InstallOptions(output_dir=root))

    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert result.changed_paths == ()
    assert after == before
    assert not tuple(root.glob(".voice-intent-normalizer.quarantine-*"))
    assert not tuple(root.glob(".voice-intent-normalizer.staging-*"))
    assert not (state.root / "adapters" / "generic-recovery.json").exists()


def test_generic_uninstall_never_uses_recursive_deletion(tmp_path: Path, monkeypatch):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recursive deletion is forbidden")
        ),
    )

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "uninstalled"


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical path aliases")
def test_real_process_installs_share_lock_across_case_equivalent_roots(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state_root = tmp_path / "state"

    first = _adapter_process(repository, root, state_root, "install")
    second = _adapter_process(
        repository,
        Path(str(root).upper()),
        Path(str(state_root).upper()),
        "install",
    )
    results = (_process_result(first), _process_result(second))

    assert all(
        result["status"] in {"installed", "already-installed", "repaired"}
        for result in results
    )
    assert (
        GenericAdapter(
            repository,
            StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(state_root)}),
        )
        .doctor()
        .status
        == "installed"
    )

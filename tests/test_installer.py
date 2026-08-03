"""Installer contracts and generic skill-package safety."""

from __future__ import annotations

import hashlib
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
    assert (root / "voice-intent-normalizer").is_dir()
    assert not any(
        path.is_file()
        for path in (root / "voice-intent-normalizer").rglob("*")
    )


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


def test_uninstall_resumes_when_status_removal_fails(
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
    monkeypatch.setattr(
        adapter,
        "_remove_status",
        lambda: (_ for _ in ()).throw(OSError("status unavailable")),
    )

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert not marker.exists()
    assert GenericAdapter(repository, state).doctor().status == "not-installed"


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
    original_write = adapter._write_status_payload
    writes = 0

    def fail_final_status(payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("no state")
        return original_write(payload)

    monkeypatch.setattr(adapter, "_write_status_payload", fail_final_status)

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert (root / "voice-intent-normalizer").is_dir()
    assert json.loads(
        state.adapter_status_file("generic").read_text(encoding="utf-8")
    )["transaction"]
    assert GenericAdapter(repository, state).doctor().status == "installed"


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
    assert state.adapter_status_file("generic") in repaired.changed_paths
    assert not any(".staging-" in path.name for path in repaired.changed_paths)
    assert len(repaired.changed_paths) == len(set(repaired.changed_paths))


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

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert result.changed_paths
    assert any("retry" in message for message in result.messages)
    assert GenericAdapter(repository, state).doctor().status == "not-installed"
    assert not (target / "SKILL.md").exists()


def test_generic_uninstall_retains_identity_bound_quarantine(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "uninstalled"
    assert tuple(
        path
        for path in root.glob(".voice-intent-normalizer.quarantine-*")
        if any(candidate.is_file() for candidate in path.rglob("*"))
    )
    assert adapter.doctor().status == "not-installed"


def test_generic_transaction_never_unlinks_a_replaced_live_source_name(
    tmp_path: Path, monkeypatch
):
    """A quarantine publication must not be followed by a name-only unlink."""
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    assert adapter.install(InstallOptions(output_dir=root)).status == "installed"
    target = root / "voice-intent-normalizer"
    victim_bytes = b"user replacement that must survive"
    original_publish = StateRootLease.publish_no_replace
    original_unlink = StateRootLease.unlink
    quarantine_published = False
    replacement_injected = False

    def observe_quarantine_publish(lease, source, destination):
        nonlocal quarantine_published
        original_publish(lease, source, destination)
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path == Path("voice-intent-normalizer") / "SKILL.md"
            and destination_path.parts[0].startswith(
                ".voice-intent-normalizer.quarantine-"
            )
        ):
            quarantine_published = True

    def replace_before_name_unlink(lease, relative, *, missing_ok=False):
        nonlocal replacement_injected
        relative_path = Path(relative)
        if (
            quarantine_published
            and not replacement_injected
            and relative_path == Path("voice-intent-normalizer") / "SKILL.md"
        ):
            live = root / relative_path
            live.unlink()
            live.write_bytes(victim_bytes)
            replacement_injected = True
        return original_unlink(lease, relative, missing_ok=missing_ok)

    monkeypatch.setattr(
        StateRootLease, "publish_no_replace", observe_quarantine_publish
    )
    monkeypatch.setattr(StateRootLease, "unlink", replace_before_name_unlink)

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert not replacement_injected
    assert result.status == "uninstalled"
    assert not (target / "SKILL.md").exists()


def test_generic_quarantine_never_moves_a_replacement_opened_after_validation(
    tmp_path: Path, monkeypatch
):
    """Replacing a checked live name must not move unrelated bytes."""
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    assert adapter.install(InstallOptions(output_dir=root)).status == "installed"
    live = root / "voice-intent-normalizer" / "SKILL.md"
    replacement = b"unrelated live replacement"
    original_open = StateRootLease._open_regular_file
    original_write_recovery = GenericAdapter._write_recovery
    armed = False
    injected = False

    def arm_after_journal(current, transaction):
        nonlocal armed
        path = original_write_recovery(current, transaction)
        armed = True
        return path

    def replace_after_open(binding, name, label):
        nonlocal injected
        descriptor = original_open(binding, name, label)
        if armed and not injected and name == "SKILL.md":
            live.unlink()
            live.write_bytes(replacement)
            injected = True
        return descriptor

    monkeypatch.setattr(GenericAdapter, "_write_recovery", arm_after_journal)
    monkeypatch.setattr(
        StateRootLease,
        "_open_regular_file",
        staticmethod(replace_after_open),
    )

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    quarantined = tuple(
        path
        for directory in root.glob(".voice-intent-normalizer.quarantine-*")
        for path in directory.rglob("*")
        if path.is_file()
    )
    assert injected
    assert result.status == "degraded"
    assert live.read_bytes() == replacement
    assert all(path.read_bytes() != replacement for path in quarantined)


def test_generic_publication_never_moves_a_replaced_staging_source(
    tmp_path: Path, monkeypatch
):
    """Replacing a checked staged name must not publish unrelated bytes."""
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    replacement = b"unrelated staging replacement"
    original_open = StateRootLease._open_regular_file
    original_write_recovery = GenericAdapter._write_recovery
    armed = False
    injected = False
    replaced_source: Path | None = None

    def arm_after_journal(current, transaction):
        nonlocal armed
        path = original_write_recovery(current, transaction)
        armed = True
        return path

    def replace_after_open(binding, name, label):
        nonlocal injected, replaced_source
        descriptor = original_open(binding, name, label)
        if armed and not injected and name == "SKILL.md":
            staging = next(root.glob(".voice-intent-normalizer.staging-*"))
            replaced_source = staging / "SKILL.md"
            replaced_source.unlink()
            replaced_source.write_bytes(replacement)
            injected = True
        return descriptor

    monkeypatch.setattr(GenericAdapter, "_write_recovery", arm_after_journal)
    monkeypatch.setattr(
        StateRootLease,
        "_open_regular_file",
        staticmethod(replace_after_open),
    )

    result = GenericAdapter(repository, state).install(
        InstallOptions(output_dir=root)
    )

    target = root / "voice-intent-normalizer" / "SKILL.md"
    assert injected
    assert result.status == "degraded"
    assert replaced_source is not None
    assert replaced_source.read_bytes() == replacement
    assert not target.exists() or target.read_bytes() != replacement


def test_generic_same_package_install_id_mismatch_never_reanchors_status(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    assert adapter.install(InstallOptions(output_dir=root)).status == "installed"
    target = root / "voice-intent-normalizer"
    manifest_path = target / ".voice-intent-normalizer-install.json"
    status_path = state.adapter_status_file("generic")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["install_id"] = "0" * 32
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    status_before = status_path.read_bytes()
    package_before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }

    result = adapter.install(InstallOptions(output_dir=root))

    package_after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert result.status in {"failed", "degraded"}
    assert status_path.read_bytes() == status_before
    assert package_after == package_before


def test_generic_upgrade_rejects_manifest_that_disagrees_with_status_anchor(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    upgraded_repository = tmp_path / "upgraded-repository"
    _copy_runtime_repository(repository, upgraded_repository)
    metadata = (upgraded_repository / "pyproject.toml").read_text(encoding="utf-8")
    (upgraded_repository / "pyproject.toml").write_text(
        metadata.replace('version = "0.1.0"', 'version = "0.1.1"'),
        encoding="utf-8",
    )
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    assert (
        GenericAdapter(repository, state)
        .install(InstallOptions(output_dir=root))
        .status
        == "installed"
    )
    target = root / "voice-intent-normalizer"
    manifest_path = target / ".voice-intent-normalizer-install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["install_id"] = "0" * 32
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }

    result = GenericAdapter(upgraded_repository, state).install(
        InstallOptions(output_dir=root)
    )

    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert before == after
    status = json.loads(
        state.adapter_status_file("generic").read_text(encoding="utf-8")
    )
    assert status["transaction"] is None


def test_generic_success_and_noop_never_leave_source_hardlinks(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)

    first = adapter.install(InstallOptions(output_dir=root))
    second = adapter.install(InstallOptions(output_dir=root))

    assert first.status == "installed"
    assert second.status == "already-installed"
    staging = tuple(root.glob(".voice-intent-normalizer.staging-*"))
    if os.name == "nt":
        assert all(
            not any(path.is_file() for path in directory.rglob("*"))
            for directory in staging
        )
    else:
        assert any(
            path.is_file()
            for directory in staging
            for path in directory.rglob("*")
        )
    assert os.stat(root / "voice-intent-normalizer" / "SKILL.md").st_nlink == 1


@pytest.mark.parametrize("operation", ["failed-install", "not-installed-uninstall"])
def test_generic_first_use_reports_shared_state_directories_once(
    tmp_path: Path, operation: str
):
    repository = Path(__file__).resolve().parents[1]
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)

    if operation == "failed-install":
        result = adapter.install(InstallOptions())
        assert result.status == "failed"
    else:
        result = adapter.uninstall(UninstallOptions(output_dir=skill_root))
        assert result.status == "not-installed"

    assert result.changed_paths.count(state.root) == 1
    assert result.changed_paths.count(state.root / "adapters") == 1
    assert len(result.changed_paths) == len(set(result.changed_paths))


def test_generic_strict_install_fails_closed_before_skill_publication(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)

    first = adapter.install(InstallOptions(output_dir=root, strict=True))
    second = adapter.install(InstallOptions(output_dir=root, strict=True))

    assert first.status == second.status == "failed"
    assert first.capability is second.capability is CapabilityLevel.UNAVAILABLE
    assert first.messages == second.messages
    assert any("strict" in message for message in first.messages)
    assert not (root / "voice-intent-normalizer").exists()


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

    assert target not in removed.changed_paths
    assert target / "src" not in removed.changed_paths
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


def test_generic_quarantine_move_failure_is_resumable(
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
    original_replace = StateRootLease.move_no_replace
    moves = 0

    def fail_third_move(lease, source, destination, **contract):
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
        return original_replace(lease, source, destination, **contract)

    monkeypatch.setattr(StateRootLease, "move_no_replace", fail_third_move)

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert result.changed_paths
    monkeypatch.setattr(StateRootLease, "move_no_replace", original_replace)
    assert GenericAdapter(repository, state).doctor().status == "not-installed"
    assert not any(path.is_file() for path in target.rglob("*"))
    assert before


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

    original_write = adapter._write_status_payload
    writes = 0

    def swap_then_fail(payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            target.rename(displaced)
            target.mkdir()
            victim_marker.write_text("victim", encoding="utf-8")
            raise OSError("status unavailable")
        return original_write(payload)

    monkeypatch.setattr(adapter, "_write_status_payload", swap_then_fail)

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
    original_prepare = adapter._prepare_transaction_directory

    def swap_then_prepare(skill_root, quarantine, manifest):
        target.rename(displaced)
        shutil.copytree(displaced, target)
        (target / "victim.txt").write_text("victim", encoding="utf-8")
        return original_prepare(skill_root, quarantine, manifest)

    monkeypatch.setattr(
        adapter, "_prepare_transaction_directory", swap_then_prepare
    )

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
    assert target.is_dir()
    if status.exists():
        assert GenericAdapter(repository, state).doctor().status == "installed"
    else:
        assert not any(path.is_file() for path in target.rglob("*"))


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
    obsolete = target / "obsolete.txt"
    obsolete.write_text("old", encoding="utf-8")
    manifest["files"].append("obsolete.txt")
    manifest["files"].sort()
    manifest["hashes"]["obsolete.txt"] = hashlib.sha256(b"old").hexdigest()
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
    manifest["package_hash"] = hashlib.sha256(package_identity).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    status_path = state.adapter_status_file("generic")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status["package_hash"] = manifest["package_hash"]
    status["package_version"] = manifest["package_version"]
    status_path.write_text(json.dumps(status), encoding="utf-8")

    result = adapter.install(InstallOptions(output_dir=root))

    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "upgraded"
    assert refreshed["package_version"] == "0.1.0"
    assert not obsolete.exists()
    assert result.changed_paths.count(obsolete) == 1
    assert len(result.changed_paths) == len(set(result.changed_paths))
    assert unknown.read_text(encoding="utf-8") == "preserve"
    assert adapter.doctor().status == "installed"


def test_generic_upgrade_move_failure_is_resumable(
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
    status_path = state.adapter_status_file("generic")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status["package_hash"] = manifest["package_hash"]
    status["package_version"] = manifest["package_version"]
    status_path.write_text(json.dumps(status), encoding="utf-8")
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    original_replace = StateRootLease.move_no_replace
    new_moves = 0

    def fail_new_move(lease, source, destination, **contract):
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
        return original_replace(lease, source, destination, **contract)

    monkeypatch.setattr(StateRootLease, "move_no_replace", fail_new_move)

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "degraded"
    assert any(".staging-" in path.name for path in result.changed_paths)
    assert len(result.changed_paths) == len(set(result.changed_paths))
    assert tuple(root.glob(".voice-intent-normalizer.staging-*"))
    monkeypatch.setattr(StateRootLease, "move_no_replace", original_replace)
    resumed = GenericAdapter(repository, state).install(
        InstallOptions(output_dir=root)
    )
    assert resumed.status in {"already-installed", "repaired", "upgraded"}
    assert GenericAdapter(repository, state).doctor().status == "installed"
    assert before


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


def test_generic_failed_staging_cleanup_never_deletes_a_swapped_victim(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    victim = root / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    def swap_staging_then_fail(
        root, target, staging, manifest, options, root_identity, staging_identity
    ):
        displaced = root / "displaced-staging"
        staging.rename(displaced)
        victim.rename(staging)
        raise OSError("injected post-validation failure")

    monkeypatch.setattr(adapter, "_commit_new_install", swap_staging_then_fail)

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "failed"
    assert (root / "displaced-staging" / "SKILL.md").is_file()
    swapped = root / next(path.name for path in root.glob(".*staging-*"))
    assert (swapped / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_generic_staging_copy_never_writes_into_a_swapped_directory(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    victim = root / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    displaced = root / "original-staging"
    original_write = StateRootLease.write_bytes_exclusive
    swapped = False
    swap_denied = False

    def swap_after_first_copy(lease, relative, data):
        nonlocal swapped, swap_denied
        original_write(lease, relative, data)
        relative_path = Path(relative)
        if (
            not swapped
            and relative_path.parts
            and relative_path.parts[0].startswith(
                ".voice-intent-normalizer.staging-"
            )
        ):
            staging = root / relative_path.parts[0]
            try:
                staging.rename(displaced)
                victim.rename(staging)
                swapped = True
            except PermissionError:
                swap_denied = True

    monkeypatch.setattr(
        StateRootLease, "write_bytes_exclusive", swap_after_first_copy
    )

    result = adapter.install(InstallOptions(output_dir=root))

    if swapped:
        staging = next(root.glob(".voice-intent-normalizer.staging-*"))
        assert result.status == "failed"
        assert (staging / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert {path.name for path in staging.iterdir()} == {"keep.txt"}
        assert (displaced / "SKILL.md").is_file()
    else:
        assert swap_denied
        assert result.status == "installed"
        assert marker.read_text(encoding="utf-8") == "keep"
        assert {path.name for path in victim.iterdir()} == {"keep.txt"}


def test_exclusive_publish_keeps_its_source_name(tmp_path: Path):
    from voice_intent_normalizer.paths import guard_state_root

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source"
    destination = root / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "file.txt").write_text("data", encoding="utf-8")

    with guard_state_root(
        root, retained_dirs=("source", "destination")
    ) as lease:
        lease.publish_no_replace("source/file.txt", "destination/file.txt")

    assert (source / "file.txt").read_text(encoding="utf-8") == "data"
    assert (destination / "file.txt").read_text(encoding="utf-8") == "data"


def test_identity_bound_move_is_exclusive_and_preserves_conflicting_source(
    tmp_path: Path,
):
    from voice_intent_normalizer.paths import guard_state_root

    root = tmp_path / "root"
    source = root / "source"
    destination = root / "destination"
    source.mkdir(parents=True)
    destination.mkdir()
    (source / "moved.txt").write_text("managed", encoding="utf-8")
    (source / "conflict.txt").write_text("source", encoding="utf-8")
    (destination / "conflict.txt").write_text("destination", encoding="utf-8")

    with guard_state_root(
        root, retained_dirs=("source", "destination")
    ) as lease:
        lease.move_no_replace(
            "source/moved.txt",
            "destination/moved.txt",
            expected_sha256=hashlib.sha256(b"managed").hexdigest(),
            limit=1024,
        )
        with pytest.raises(OSError):
            lease.move_no_replace(
                "source/conflict.txt",
                "destination/conflict.txt",
                expected_sha256=hashlib.sha256(b"source").hexdigest(),
                limit=1024,
            )

    assert not (source / "moved.txt").exists()
    assert (destination / "moved.txt").read_text(encoding="utf-8") == "managed"
    assert (source / "conflict.txt").read_text(encoding="utf-8") == "source"
    assert (destination / "conflict.txt").read_text(encoding="utf-8") == "destination"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound rename contract")
def test_windows_identity_bound_move_never_renames_a_replacement_name(
    tmp_path: Path, monkeypatch
):
    from contextlib import contextmanager

    import voice_intent_normalizer.paths as paths_module
    from voice_intent_normalizer.paths import guard_state_root

    root = tmp_path / "root"
    source = root / "source"
    destination = root / "destination"
    source.mkdir(parents=True)
    destination.mkdir()
    live = source / "file.txt"
    live.write_bytes(b"managed")
    replacement = b"unrelated replacement"
    original_open = paths_module._open_windows_regular_file_for_move
    injected = False

    @contextmanager
    def replace_after_handle_open(path):
        nonlocal injected
        with original_open(path) as descriptor:
            live.unlink()
            live.write_bytes(replacement)
            injected = True
            yield descriptor

    monkeypatch.setattr(
        paths_module,
        "_open_windows_regular_file_for_move",
        replace_after_handle_open,
    )

    with guard_state_root(
        root, retained_dirs=("source", "destination")
    ) as lease:
        with pytest.raises(OSError):
            lease.move_no_replace(
                "source/file.txt",
                "destination/file.txt",
                expected_sha256=hashlib.sha256(b"managed").hexdigest(),
                limit=1024,
            )

    assert injected
    assert live.read_bytes() == replacement
    assert not (destination / "file.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-copy fallback contract")
def test_posix_identity_bound_move_copies_without_unlinking_the_source(
    tmp_path: Path,
):
    from voice_intent_normalizer.paths import guard_state_root

    root = tmp_path / "root"
    source = root / "source"
    destination = root / "destination"
    source.mkdir(parents=True)
    destination.mkdir()
    live = source / "file.txt"
    live.write_bytes(b"managed")

    with guard_state_root(
        root, retained_dirs=("source", "destination")
    ) as lease:
        removed = lease.move_no_replace(
            "source/file.txt",
            "destination/file.txt",
            expected_sha256=hashlib.sha256(b"managed").hexdigest(),
            limit=1024,
        )

    assert removed is False
    assert live.read_bytes() == b"managed"
    assert (destination / "file.txt").read_bytes() == b"managed"


def test_generic_uninstall_leaves_empty_managed_directories(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"

    result = adapter.uninstall(UninstallOptions(output_dir=root))

    assert result.status == "uninstalled"
    assert target.is_dir()
    assert target / "src" not in result.changed_paths
    assert any("empty" in message for message in result.messages)


def test_generic_can_reinstall_into_benign_empty_managed_directories(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    adapter.uninstall(UninstallOptions(output_dir=root))

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "installed"
    assert (root / "voice-intent-normalizer" / "SKILL.md").is_file()
    assert adapter.doctor().status == "installed"


def test_generic_upgrade_never_overwrites_new_unknown_file(
    tmp_path: Path, monkeypatch
):
    import hashlib

    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    manifest_path = target / ".voice-intent-normalizer-install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].remove("LICENSE")
    manifest["hashes"].pop("LICENSE")
    manifest["package_hash"] = hashlib.sha256(
        json.dumps(
            {
                "hashes": manifest["hashes"],
                "required": manifest["required_files"],
                "version": manifest["package_version"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (target / "LICENSE").unlink()
    status_path = state.adapter_status_file("generic")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status["package_hash"] = manifest["package_hash"]
    status_path.write_text(json.dumps(status), encoding="utf-8")
    original_publish = StateRootLease.move_no_replace
    injected = False

    def inject_unknown(lease, source, destination, **contract):
        nonlocal injected
        if Path(destination) == Path("voice-intent-normalizer/LICENSE"):
            injected = True
            (target / "LICENSE").write_text("user-owned", encoding="utf-8")
        return original_publish(lease, source, destination, **contract)

    monkeypatch.setattr(StateRootLease, "move_no_replace", inject_unknown)

    result = adapter.install(InstallOptions(output_dir=root))

    assert injected
    assert result.status in {"failed", "degraded"}
    assert (target / "LICENSE").read_text(encoding="utf-8") == "user-owned"


def test_generic_fresh_install_never_replaces_a_racing_destination(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    target = root / "voice-intent-normalizer"
    original_commit = adapter._commit_new_install

    def inject_destination(
        skill_root,
        managed_target,
        staging,
        manifest,
        options,
        root_identity,
        staging_identity,
    ):
        target.mkdir()
        (target / "keep.txt").write_text("user-owned", encoding="utf-8")
        return original_commit(
            skill_root,
            managed_target,
            staging,
            manifest,
            options,
            root_identity,
            staging_identity,
        )

    monkeypatch.setattr(adapter, "_commit_new_install", inject_destination)

    result = adapter.install(InstallOptions(output_dir=root))

    assert result.status == "failed"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "user-owned"


def test_generic_status_anchors_manifest_ownership(tmp_path: Path):
    import hashlib

    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"
    user_file = target / "user-owned.txt"
    user_file.write_text("keep", encoding="utf-8")
    manifest_path = target / ".voice-intent-normalizer-install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append("user-owned.txt")
    manifest["files"].sort()
    manifest["hashes"]["user-owned.txt"] = hashlib.sha256(b"keep").hexdigest()
    manifest["package_hash"] = hashlib.sha256(
        json.dumps(
            {
                "hashes": manifest["hashes"],
                "required": manifest["required_files"],
                "version": manifest["package_version"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    assert adapter.doctor().status == "degraded"
    assert adapter.uninstall(UninstallOptions(output_dir=root)).status == "failed"
    assert user_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("capability", ["automatic", "unavailable"])
def test_generic_status_rejects_non_generic_capabilities(
    tmp_path: Path, capability: str
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    status_path = state.adapter_status_file("generic")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["capability"] = capability
    status_path.write_text(json.dumps(status), encoding="utf-8")

    result = adapter.doctor()

    assert result.status == "degraded"
    assert result.capability is CapabilityLevel.MANUAL


@pytest.mark.parametrize("relative", [r"..\outside.txt", "../outside.txt"])
def test_generic_manifest_rejects_mixed_separator_traversal(relative: str):
    assert GenericAdapter._manifest_files({"files": [relative]}) is None


@pytest.mark.parametrize(
    "relative",
    [
        r"C:\outside.txt",
        "C:/outside.txt",
        r"\\server\share\outside.txt",
        "//server/share/outside.txt",
        "name:stream",
        "CON",
        "dir/NUL.txt",
        "trailing.",
        "trailing ",
        "a//b",
        "/absolute",
    ],
)
def test_generic_manifest_rejects_nonportable_paths(relative: str):
    assert GenericAdapter._manifest_files({"files": [relative]}) is None


def test_generic_manifest_rejects_excessive_size_and_depth():
    too_long = "a" * 513
    too_deep = "/".join("a" for _ in range(33))
    too_many = [f"file-{index}" for index in range(4097)]

    assert GenericAdapter._manifest_files({"files": [too_long]}) is None
    assert GenericAdapter._manifest_files({"files": [too_deep]}) is None
    assert GenericAdapter._manifest_files({"files": too_many}) is None


def test_generic_runtime_does_not_require_python_311_tomllib():
    module_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "voice_intent_normalizer"
        / "adapters"
        / "generic.py"
    ).read_text(encoding="utf-8")

    assert "import tomllib" not in module_source


def test_generic_doctor_recovers_crash_after_last_uninstall_move(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    target = root / "voice-intent-normalizer"

    monkeypatch.setattr(
        adapter,
        "_remove_status",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        adapter.uninstall(UninstallOptions(output_dir=root))

    recovered = GenericAdapter(repository, state)
    result = recovered.doctor()
    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }

    assert result.status == "not-installed"
    assert after == {}
    recovery = state.root / "adapters" / "generic-recovery.json"
    assert not recovery.exists()
    assert result.changed_paths.count(recovery) == 0
    assert len(result.changed_paths) == len(set(result.changed_paths))


def test_generic_does_not_consume_unanchored_recovery_record(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    quarantine = root / (
        ".voice-intent-normalizer.quarantine-"
        "0123456789abcdef0123456789abcdef"
    )
    quarantine.mkdir()
    quarantined = quarantine / "keep.txt"
    quarantined.write_text("keep", encoding="utf-8")
    recovery = state.root / "adapters" / "generic-recovery.json"
    recovery.write_text(
        json.dumps(
            {
                "format": 3,
                "owner": "voice-intent-normalizer",
                "transaction_digest": "0" * 64,
                "transaction_id": "0123456789abcdef0123456789abcdef",
            }
        ),
        encoding="utf-8",
    )

    result = GenericAdapter(repository, state).doctor()

    assert result.status == "degraded"
    assert result.capability is CapabilityLevel.MANUAL
    assert recovery.is_file()
    assert quarantined.read_text(encoding="utf-8") == "keep"


def test_generic_resumes_mid_upgrade_without_overwriting_unknown(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    upgraded_repository = tmp_path / "new-repository"
    _copy_runtime_repository(repository, upgraded_repository)
    metadata = (upgraded_repository / "pyproject.toml").read_text(encoding="utf-8")
    (upgraded_repository / "pyproject.toml").write_text(
        metadata.replace('version = "0.1.0"', 'version = "0.1.2"'),
        encoding="utf-8",
    )
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    GenericAdapter(repository, state).install(InstallOptions(output_dir=root))
    interrupted = GenericAdapter(upgraded_repository, state)
    original_publish = StateRootLease.move_no_replace
    publishes = 0

    def crash_during_new_publish(lease, source, destination, **contract):
        nonlocal publishes
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.parts
            and source_path.parts[0].startswith(".voice-intent-normalizer.staging-")
            and destination_path.parts
            and destination_path.parts[0] == "voice-intent-normalizer"
        ):
            publishes += 1
            if publishes == 3:
                raise KeyboardInterrupt()
        return original_publish(lease, source, destination, **contract)

    monkeypatch.setattr(
        StateRootLease, "move_no_replace", crash_during_new_publish
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.install(InstallOptions(output_dir=root))
    interrupted_status = json.loads(
        state.adapter_status_file("generic").read_text(encoding="utf-8")
    )
    assert interrupted_status["transaction"]["phase"] in {
        "quarantined",
        "publishing",
    }
    monkeypatch.setattr(StateRootLease, "move_no_replace", original_publish)

    resumed = GenericAdapter(upgraded_repository, state).install(
        InstallOptions(output_dir=root)
    )

    assert resumed.status in {
        "installed",
        "repaired",
        "upgraded",
        "already-installed",
    }
    assert GenericAdapter(upgraded_repository, state).doctor().status == "installed"
    manifest = json.loads(
        (
            root
            / "voice-intent-normalizer"
            / ".voice-intent-normalizer-install.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["package_version"] == "0.1.2"


def test_generic_retry_finalizes_upgrade_after_status_failure(
    tmp_path: Path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    upgraded_repository = tmp_path / "new-repository"
    _copy_runtime_repository(repository, upgraded_repository)
    metadata = (upgraded_repository / "pyproject.toml").read_text(encoding="utf-8")
    (upgraded_repository / "pyproject.toml").write_text(
        metadata.replace('version = "0.1.0"', 'version = "0.1.1"'),
        encoding="utf-8",
    )
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    GenericAdapter(repository, state).install(InstallOptions(output_dir=root))
    failing = GenericAdapter(upgraded_repository, state)
    original_write = failing._write_status_payload
    writes = 0

    def fail_final_status(payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("status failed")
        return original_write(payload)

    monkeypatch.setattr(failing, "_write_status_payload", fail_final_status)

    interrupted = failing.install(InstallOptions(output_dir=root))

    assert interrupted.status == "degraded"
    recovery = state.root / "adapters" / "generic-recovery.json"
    assert not recovery.exists()
    assert json.loads(
        state.adapter_status_file("generic").read_text(encoding="utf-8")
    )["transaction"]
    repaired = GenericAdapter(upgraded_repository, state).install(
        InstallOptions(output_dir=root)
    )

    assert repaired.status in {"repaired", "already-installed"}
    assert GenericAdapter(upgraded_repository, state).doctor().status == "installed"
    assert not recovery.exists()
    assert repaired.changed_paths.count(recovery) == 0
    assert len(repaired.changed_paths) == len(set(repaired.changed_paths))
    assert tuple(
        path
        for path in root.glob(".voice-intent-normalizer.quarantine-*")
        if any(candidate.is_file() for candidate in path.rglob("*"))
    )


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

"""WorkBuddy manual-import package contracts."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from voice_intent_normalizer.adapters.base import (
    CapabilityLevel,
    InstallOptions,
    UninstallOptions,
)
from voice_intent_normalizer.paths import StatePaths


def _adapter(repository: Path):
    from voice_intent_normalizer.adapters.workbuddy import WorkBuddyAdapter

    return WorkBuddyAdapter(repository, StatePaths(root=repository / ".state"))


def test_workbuddy_archive_has_runtime_skill_at_package_root(tmp_path: Path):
    adapter = _adapter(Path(__file__).resolve().parents[1])

    result = adapter.install(InstallOptions(output_dir=tmp_path))
    archive = tmp_path / "voice-intent-normalizer-workbuddy.zip"

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert names == sorted(names)
        assert "SKILL.md" in names
        assert "scripts/voice_intent.py" in names
        assert "src/voice_intent_normalizer/service.py" in names
        assert all("\\" not in name for name in names)
        assert all(
            item.date_time == (1980, 1, 1, 0, 0, 0) for item in bundle.infolist()
        )

    assert result.status == "package-created"
    assert result.capability is CapabilityLevel.MANUAL
    assert (tmp_path / "voice-intent-normalizer-workbuddy.zip.sha256").read_text(
        encoding="ascii"
    ) == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}\n"


def test_workbuddy_archive_is_byte_deterministic_and_private_by_allowlist(
    tmp_path: Path,
):
    adapter = _adapter(Path(__file__).resolve().parents[1])
    first = tmp_path / "first"
    second = tmp_path / "second"

    adapter.install(InstallOptions(output_dir=first))
    adapter.install(InstallOptions(output_dir=second))

    archive = "voice-intent-normalizer-workbuddy.zip"
    assert (first / archive).read_bytes() == (second / archive).read_bytes()
    with zipfile.ZipFile(first / archive) as bundle:
        names = set(bundle.namelist())
    assert ".git/config" not in names
    assert "tests/test_learning.py" not in names
    assert "docs/superpowers/specs/secret.md" not in names
    assert ".env" not in names
    assert "personal.jsonl" not in names
    assert "build/output.py" not in names


def test_workbuddy_result_gives_exact_public_import_journey(tmp_path: Path):
    adapter = _adapter(Path(__file__).resolve().parents[1])

    result = adapter.install(InstallOptions(output_dir=tmp_path))

    message = "\n".join(result.messages)
    assert "WorkBuddy → Skills → Add Skill → Upload Skill" in message
    assert "choose the ZIP" in message
    assert "enable" in message
    assert "打开 OpenClaw 技能并纠正 open cloud" in message


def test_workbuddy_doctor_requires_explicit_visible_ui_confirmation(tmp_path: Path):
    adapter = _adapter(Path(__file__).resolve().parents[1])

    assert adapter.doctor().status == "manual-action-required"
    result = adapter.install(InstallOptions(implicit_invocation_confirmed=True))
    assert result.status == "package-created"
    assert adapter.doctor().status == "confirmed"


def test_workbuddy_uninstall_is_manual_and_preserves_shared_data(tmp_path: Path):
    adapter = _adapter(Path(__file__).resolve().parents[1])

    result = adapter.uninstall(UninstallOptions(output_dir=tmp_path))

    assert result.status == "manual-action-required"
    assert result.capability is CapabilityLevel.MANUAL
    assert any("shared personal lexicon" in message for message in result.messages)


def test_workbuddy_rejects_an_output_directory_below_an_aliased_parent(
    tmp_path: Path,
):
    target = tmp_path / "outside"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory aliases are unavailable: {exc}")
    adapter = _adapter(Path(__file__).resolve().parents[1])

    result = adapter.install(InstallOptions(output_dir=alias / "packages"))

    assert result.status == "failed"
    assert not (target / "packages" / "voice-intent-normalizer-workbuddy.zip").exists()


def test_workbuddy_archive_write_uses_retained_output_authority(
    tmp_path: Path, monkeypatch
):
    from voice_intent_normalizer.adapters import workbuddy

    adapter = _adapter(Path(__file__).resolve().parents[1])
    calls: list[Path] = []
    native_guard = workbuddy.guard_state_root

    def guarded_output(root, **kwargs):
        calls.append(Path(root))
        return native_guard(root, **kwargs)

    monkeypatch.setattr(workbuddy, "guard_state_root", guarded_output)

    result = adapter.install(InstallOptions(output_dir=tmp_path))

    assert result.status == "package-created"
    assert calls == [tmp_path]
    assert os.path.exists(tmp_path / "voice-intent-normalizer-workbuddy.zip")

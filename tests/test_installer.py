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
from voice_intent_normalizer.adapters.generic_contract import (
    canonical_json_bytes,
    validate_manifest,
    validate_status_v5,
)
from voice_intent_normalizer.adapters.generic_layout import generic_layout_paths
from voice_intent_normalizer.installer import Installer
from voice_intent_normalizer.paths import (
    StatePaths,
    StateRootLease,
    guard_state_root,
)


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


@pytest.fixture
def generic_adapter(tmp_path: Path) -> GenericAdapter:
    repository = Path(__file__).resolve().parents[1]
    state = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}
    )
    return GenericAdapter(repository, state)


def _validated_installed_layout(
    adapter: GenericAdapter, skill_root: Path
) -> tuple[Path, Path, dict[str, object]]:
    layout = generic_layout_paths(adapter.state_paths)
    raw_status = layout.status.read_bytes()
    status = validate_status_v5(
        raw_status,
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    capsule = skill_root / "voice-intent-normalizer"
    generation = layout.generations / status.active.generation_id
    capsule_manifest = validate_manifest((capsule / "capsule.json").read_bytes())
    generation_manifest = validate_manifest(
        (generation / "generation.json").read_bytes()
    )
    assert canonical_json_bytes(capsule_manifest) == (
        capsule / "capsule.json"
    ).read_bytes()
    assert canonical_json_bytes(generation_manifest) == (
        generation / "generation.json"
    ).read_bytes()
    return capsule, generation, json.loads(raw_status)


def test_publish_directory_no_replace_moves_exact_directory_identity(
    tmp_path: Path,
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "complete.txt").write_text("complete", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)

    with guard_state_root(root) as lease:
        lease.publish_directory_no_replace("source", "published", expected_identity)

    published = root / "published"
    assert not source.exists()
    assert (published.stat().st_dev, published.stat().st_ino) == expected_identity
    assert (published / "complete.txt").read_text(encoding="utf-8") == "complete"


def test_publish_directory_no_replace_preserves_concurrent_destination(
    tmp_path: Path,
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "source.txt").write_text("source", encoding="utf-8")
    destination = root / "published"
    destination.mkdir()
    (destination / "racer.txt").write_text("racer", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)

    with guard_state_root(root) as lease, pytest.raises(FileExistsError):
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert (source / "source.txt").read_text(encoding="utf-8") == "source"
    assert (destination / "racer.txt").read_text(encoding="utf-8") == "racer"


def test_publish_directory_no_replace_rejects_replaced_source_identity(
    tmp_path: Path,
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    displaced = root / "displaced"
    source.rename(displaced)
    source.mkdir()
    (source / "replacement.txt").write_text("replacement", encoding="utf-8")

    with guard_state_root(root) as lease, pytest.raises(ValueError):
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert (displaced / "original.txt").read_text(encoding="utf-8") == "original"
    assert (source / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert not (root / "published").exists()


def test_failed_generation_write_exposes_no_partial_runtime(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original = getattr(generic_adapter, "_write_staged_file", None)
    writes = 0

    def fail_after_prefix(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected generation write failure")
        assert original is not None
        return original(*args, **kwargs)

    monkeypatch.setattr(
        generic_adapter, "_write_staged_file", fail_after_prefix, raising=False
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)

    assert result.status == "failed"
    assert not (skill_root / "voice-intent-normalizer").exists()
    assert not tuple(layout.generations.glob("g-*"))
    assert not tuple(layout.staging.iterdir())


def test_generation_validation_failure_precedes_final_name_publication(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    observed = False

    def fail_validation(*args, **kwargs):
        nonlocal observed
        observed = True
        layout = generic_layout_paths(generic_adapter.state_paths)
        assert not tuple(layout.generations.glob("g-*"))
        raise ValueError("injected staged generation validation failure")

    monkeypatch.setattr(
        generic_adapter,
        "_validate_generation_directory",
        fail_validation,
        raising=False,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)

    assert observed
    assert result.status == "failed"
    assert not tuple(layout.generations.glob("g-*"))
    assert not (skill_root / "voice-intent-normalizer").exists()


def test_failed_generation_cleanup_preserves_swapped_staging_victim(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original = StateRootLease.publish_directory_no_replace
    swapped_marker: Path | None = None

    def swap_staging_before_publication(
        lease, source, destination, expected_identity
    ):
        nonlocal swapped_marker
        source_relative = Path(source)
        destination_relative = Path(destination)
        if destination_relative.parent == Path("adapters/generic/generations"):
            source_path = generic_adapter.state_paths.root / source_relative
            displaced = source_path.parent / "displaced-generation"
            victim = source_path.parent / "victim"
            victim.mkdir()
            (victim / "keep.txt").write_text("keep", encoding="utf-8")
            source_path.rename(displaced)
            victim.rename(source_path)
            swapped_marker = source_path / "keep.txt"
            raise OSError("injected publication failure after staging swap")
        return original(lease, source, destination, expected_identity)

    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        swap_staging_before_publication,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)

    assert result.status == "failed"
    assert swapped_marker is not None
    assert swapped_marker.read_text(encoding="utf-8") == "keep"
    assert not tuple(layout.generations.glob("g-*"))
    assert not layout.status.exists()
    assert not (skill_root / "voice-intent-normalizer").exists()


def test_first_install_publishes_complete_generation_and_capsule(
    tmp_path: Path, generic_adapter: GenericAdapter
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    capsule, generation, status = _validated_installed_layout(
        generic_adapter, skill_root
    )

    assert result.status == "installed"
    assert status["previous"] is None
    assert status["transaction"] is None
    capsule_files = {
        path.relative_to(capsule).as_posix()
        for path in capsule.rglob("*")
        if path.is_file()
    }
    generation_files = {
        path.relative_to(generation).as_posix()
        for path in generation.rglob("*")
        if path.is_file()
    }
    assert capsule_files == {
        *validate_manifest((capsule / "capsule.json").read_bytes())["files"],
        "capsule.json",
    }
    assert generation_files == {
        *validate_manifest((generation / "generation.json").read_bytes())["files"],
        "generation.json",
    }


def test_first_capsule_publication_rejects_concurrently_appearing_target(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    target = skill_root / "voice-intent-normalizer"
    original = getattr(StateRootLease, "publish_directory_no_replace", None)

    def publish_with_racer(lease, source, destination, expected_identity):
        if Path(destination).name == "voice-intent-normalizer":
            target.mkdir()
            (target / "racer.txt").write_text("racer", encoding="utf-8")
        assert original is not None
        return original(lease, source, destination, expected_identity)

    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        publish_with_racer,
        raising=False,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert (target / "racer.txt").read_text(encoding="utf-8") == "racer"
    assert not generic_adapter.state_paths.adapter_status_file("generic").exists()


def test_first_install_smoke_failure_never_activates_status(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    observed_complete = False

    def fail_smoke(capsule, generation):
        nonlocal observed_complete
        observed_complete = (
            Path(capsule, "capsule.json").is_file()
            and Path(generation, "generation.json").is_file()
        )
        assert not generic_adapter.state_paths.adapter_status_file("generic").exists()
        raise ValueError("injected smoke failure")

    monkeypatch.setattr(
        generic_adapter, "_smoke_generation", fail_smoke, raising=False
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert observed_complete
    assert result.status == "failed"
    assert not generic_adapter.state_paths.adapter_status_file("generic").exists()


def test_first_install_status_failure_leaves_complete_inert_artifacts(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    monkeypatch.setattr(
        generic_adapter,
        "_write_status_payload",
        lambda payload: (_ for _ in ()).throw(OSError("injected status failure")),
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)
    generations = tuple(layout.generations.glob("g-*"))

    assert result.status == "failed"
    assert not layout.status.exists()
    assert layout.transaction.is_file()
    assert (skill_root / "voice-intent-normalizer" / "capsule.json").is_file()
    assert len(generations) == 1
    validate_manifest((generations[0] / "generation.json").read_bytes())


def test_generation_parent_fsync_failure_preserves_recovery_anchor(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = StateRootLease.fsync_directory
    injected = False

    def fail_after_generation_publish(lease, relative=Path(".")):
        nonlocal injected
        if (
            not injected
            and Path(relative) == Path("adapters/generic/generations")
            and tuple(layout.generations.glob("g-*"))
        ):
            injected = True
            raise OSError("injected generation-parent fsync failure")
        return original(lease, relative)

    monkeypatch.setattr(
        StateRootLease, "fsync_directory", fail_after_generation_publish
    )

    failed = generic_adapter.install(InstallOptions(output_dir=skill_root))
    published = tuple(layout.generations.glob("g-*"))

    assert injected
    assert failed.status == "failed"
    assert len(published) == 1
    assert layout.transaction.is_file()
    assert not layout.status.exists()

    recovered = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert recovered.status == "installed"
    assert tuple(layout.generations.glob("g-*")) == published
    assert not layout.transaction.exists()


def test_status_parent_fsync_failure_after_replace_is_an_activation_commit(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = StateRootLease.fsync_directory
    injected = False

    def fail_after_status_replace(lease, relative=Path(".")):
        nonlocal injected
        if (
            not injected
            and Path(relative) == Path("adapters/generic")
            and layout.status.is_file()
        ):
            injected = True
            raise OSError("injected status-parent fsync failure")
        return original(lease, relative)

    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_after_status_replace)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    _validated_installed_layout(generic_adapter, skill_root)

    assert injected
    assert result.status == "installed"
    assert not layout.transaction.exists()
    assert generic_adapter.install(
        InstallOptions(output_dir=skill_root)
    ).status == "already-installed"


def test_first_install_retry_adopts_only_transaction_anchored_artifacts(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original = generic_adapter._write_status_payload
    monkeypatch.setattr(
        generic_adapter,
        "_write_status_payload",
        lambda payload: (_ for _ in ()).throw(OSError("injected status failure")),
    )
    failed = generic_adapter.install(InstallOptions(output_dir=skill_root))
    monkeypatch.setattr(generic_adapter, "_write_status_payload", original)

    recovered = generic_adapter.install(InstallOptions(output_dir=skill_root))
    capsule, generation, _ = _validated_installed_layout(generic_adapter, skill_root)
    layout = generic_layout_paths(generic_adapter.state_paths)

    assert failed.status == "failed"
    assert recovered.status == "installed"
    assert capsule.is_dir()
    assert generation.is_dir()
    assert len(tuple(layout.generations.glob("g-*"))) == 1
    assert not layout.transaction.exists()


def test_first_install_changed_paths_are_exact_and_unique(
    tmp_path: Path, generic_adapter: GenericAdapter
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    capsule, generation, _ = _validated_installed_layout(generic_adapter, skill_root)
    layout = generic_layout_paths(generic_adapter.state_paths)
    expected = {
        generic_adapter.state_paths.root,
        generic_adapter.state_paths.root / "adapters",
        layout.adapter_root,
        layout.generations,
        layout.staging,
        layout.retired,
        layout.transaction,
        layout.status,
        capsule,
        generation,
        *capsule.rglob("*"),
        *generation.rglob("*"),
    }

    assert len(result.changed_paths) == len(set(result.changed_paths))
    assert set(result.changed_paths) == expected


def test_versioned_already_installed_is_a_zero_mutation_noop(
    tmp_path: Path, generic_adapter: GenericAdapter
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    first = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)
    before = {
        path: path.read_bytes()
        for root in (skill_root, generic_adapter.state_paths.root)
        for path in root.rglob("*")
        if path.is_file()
    }

    second = generic_adapter.install(InstallOptions(output_dir=skill_root))
    after = {
        path: path.read_bytes()
        for root in (skill_root, generic_adapter.state_paths.root)
        for path in root.rglob("*")
        if path.is_file()
    }

    assert first.status == "installed"
    assert second.status == "already-installed"
    assert second.changed_paths == ()
    assert after == before
    assert len(tuple(layout.generations.glob("g-*"))) == 1
    assert not tuple(layout.staging.iterdir())


def test_versioned_strict_mode_fails_before_any_mutation(
    tmp_path: Path, generic_adapter: GenericAdapter
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = generic_adapter.install(
        InstallOptions(output_dir=skill_root, strict=True)
    )

    assert result.status == "failed"
    assert result.changed_paths == ()
    assert not generic_adapter.state_paths.root.exists()
    assert not (skill_root / "voice-intent-normalizer").exists()


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
    status = validate_status_v5(
        state.adapter_status_file("generic").read_bytes(),
        skill_root=root,
        generations_root=generic_layout_paths(state).generations,
    )
    assert status.active_root.joinpath(
        "src", "voice_intent_normalizer", "cli.py"
    ).is_file()
    assert not target.joinpath("tests").exists()
    assert not target.joinpath(".git").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert status.capsule_root == target


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
            "VOICE_INTENT_HOME": str(state.root),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}



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
        if writes == 1:
            raise OSError("no state")
        return original_write(payload)

    monkeypatch.setattr(adapter, "_write_status_payload", fail_final_status)

    result = adapter.install(InstallOptions(output_dir=root))

    layout = generic_layout_paths(state)
    assert result.status == "failed"
    assert (root / "voice-intent-normalizer" / "capsule.json").is_file()
    assert len(tuple(layout.generations.glob("g-*"))) == 1
    assert layout.transaction.is_file()
    assert not layout.status.exists()


def test_generic_reinstall_rejects_unmanaged_capsule_file_without_mutation(
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

    assert second.status == "failed"
    assert custom.read_text(encoding="utf-8") == "preserve"
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


def test_generic_reinstall_rejects_missing_capsule_file_and_preserves_unknown(
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

    assert repaired.status == "failed"
    assert not (target / "SKILL.md").exists()
    assert custom.read_text(encoding="utf-8") == "preserve"


def test_generic_reinstall_does_not_reanchor_capsule_with_missing_status(
    tmp_path: Path,
):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "skills"
    root.mkdir()
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(tmp_path / "state")})
    adapter = GenericAdapter(repository, state)
    adapter.install(InstallOptions(output_dir=root))
    status = state.adapter_status_file("generic")
    status.unlink()

    before_generations = tuple(generic_layout_paths(state).generations.iterdir())
    repaired = adapter.install(
        InstallOptions(output_dir=root, implicit_invocation_confirmed=True)
    )

    assert repaired.status == "failed"
    assert not status.exists()
    assert (
        tuple(generic_layout_paths(state).generations.iterdir())
        == before_generations
    )



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
    assert not tuple(root.glob(".voice-intent-normalizer.staging-*"))
    assert not tuple(generic_layout_paths(state).staging.iterdir())
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
    status = validate_status_v5(
        state.adapter_status_file("generic").read_bytes(),
        skill_root=root,
        generations_root=generic_layout_paths(state).generations,
    )
    assert target in installed.changed_paths
    assert target / "SKILL.md" in installed.changed_paths
    assert status.active_root / "src" in installed.changed_paths
    assert status.active_root / "src" / "voice_intent_normalizer" / "cli.py" in (
        installed.changed_paths
    )
    assert state.adapter_status_file("generic") in installed.changed_paths


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
def test_generic_rejects_invalid_staged_package_with_only_inert_artifacts(
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
    capsule = root / "voice-intent-normalizer"
    layout = generic_layout_paths(state)
    generations = tuple(layout.generations.glob("g-*"))
    assert (capsule / "capsule.json").is_file()
    assert len(generations) == 1
    assert (generations[0] / "generation.json").is_file()
    assert layout.transaction.is_file()
    assert not layout.status.exists()
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
    capsule, generation, _ = _validated_installed_layout(
        GenericAdapter(repository, state), root
    )
    assert capsule.is_dir()
    assert generation.is_dir()
    assert (root / "voice-intent-normalizer" / "SKILL.md").is_file()







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
    state = StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(state_root)})
    capsule, generation, _ = _validated_installed_layout(
        GenericAdapter(repository, state), root
    )
    assert capsule.is_dir()
    assert generation.is_dir()

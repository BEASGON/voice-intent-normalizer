"""Installer contracts and generic skill-package safety."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import voice_intent_normalizer.paths as paths_module
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
from voice_intent_normalizer.adapters.generic_layout import (
    generic_layout_paths,
    ownership_journal_bytes,
    ownership_journal_transition_bytes,
    status_v5_payload,
    validate_ownership_journal,
)
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


@pytest.fixture
def repository_v2(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    repository = tmp_path / "repository-v2"
    shutil.copytree(
        source,
        repository,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".pytest_cache",
            ".ruff_cache",
            ".superpowers",
            "__pycache__",
            "dist",
        ),
    )
    pyproject = repository / "pyproject.toml"
    original = pyproject.read_bytes()
    assert b'version = "0.1.0"' in original
    pyproject.write_bytes(
        original.replace(b'version = "0.1.0"', b'version = "0.2.0"', 1)
    )
    return repository


def _capsule_bytes(capsule: Path) -> dict[str, bytes]:
    return {
        path.relative_to(capsule).as_posix(): path.read_bytes()
        for path in capsule.rglob("*")
        if path.is_file()
    }


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


@dataclass(frozen=True)
class _JournalScenario:
    state_paths: StatePaths
    repository: Path
    skill_root: Path
    candidate_id: str

    def restart(
        self, operation: str
    ) -> tuple[GenericAdapter, Callable[[], AdapterResult]]:
        adapter = GenericAdapter(self.repository, self.state_paths)
        if operation == "install":
            return adapter, lambda: adapter.install(
                InstallOptions(output_dir=self.skill_root)
            )
        if operation == "doctor":
            return adapter, adapter.doctor
        return adapter, lambda: adapter.uninstall(
            UninstallOptions(output_dir=self.skill_root)
        )


def _write_test_artifact(root: Path, files: object) -> None:
    assert hasattr(files, "items")
    for relative, data in files.items():
        target = root / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@dataclass(frozen=True)
class _DirectEntrySnapshot:
    kind: str
    identity: tuple[int, int]
    contents: bytes | None = None


def _snapshot_entry_is_alias(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _file_snapshot(*roots: Path) -> dict[Path, _DirectEntrySnapshot]:
    """Snapshot direct identities without following aliases or losing empty dirs."""
    snapshot: dict[Path, _DirectEntrySnapshot] = {}
    pending = list(roots)
    while pending:
        path = pending.pop()
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        if stat.S_ISDIR(info.st_mode) and not _snapshot_entry_is_alias(info):
            snapshot[path] = _DirectEntrySnapshot("directory", identity)
            with os.scandir(path) as entries:
                pending.extend(path / entry.name for entry in entries)
        elif stat.S_ISREG(info.st_mode) and not _snapshot_entry_is_alias(info):
            snapshot[path] = _DirectEntrySnapshot(
                "file", identity, path.read_bytes()
            )
        else:
            snapshot[path] = _DirectEntrySnapshot("alias", identity)
    return snapshot


def _tree_relative_entries(root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    directories = {Path(".")}
    files: set[Path] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = (directory / entry.name).relative_to(root)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not _snapshot_entry_is_alias(info):
                    directories.add(relative)
                    pending.append(directory / entry.name)
                elif stat.S_ISREG(info.st_mode) and not _snapshot_entry_is_alias(info):
                    files.add(relative)
                else:
                    raise AssertionError(
                        f"unexpected alias in expected tree: {relative}"
                    )
    return (
        tuple(sorted(directories, key=lambda path: (len(path.parts), str(path)))),
        tuple(sorted(files, key=str)),
    )


def _expected_published_tree_paths(root: Path) -> tuple[Path, ...]:
    directories, files = _tree_relative_entries(root)
    changed_directories = tuple(
        root if path == Path(".") else root / path for path in directories
    )
    return changed_directories + tuple(root / path for path in files)


def _expected_removed_tree_paths(
    source: Path, tombstone: Path, manifest_name: str
) -> tuple[Path, ...]:
    directories, files = _tree_relative_entries(source)
    data_files = tuple(path for path in files if path.name != manifest_name)
    return (
        source,
        tombstone,
        *(tombstone / path for path in data_files),
        tombstone / manifest_name,
        *(
            tombstone if path == Path(".") else tombstone / path
            for path in directories
        ),
    )


def _dedupe_expected_paths(*groups: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path for group in groups for path in group))


def _journal_cleanup_bound_path(
    candidate: Path, relative: Path, transaction_id: str
) -> Path:
    digest = hashlib.sha256(
        f"{transaction_id}\0{relative.as_posix()}".encode()
    ).hexdigest()[:32]
    return candidate / relative.parent / f".journal-clean-{digest}"


def _expected_initial_cleanup_paths(
    layout,
    candidate_id: str,
    state: str,
) -> tuple[Path, ...]:
    transaction = (layout.transaction,)
    if state == "initial-journal-only":
        return transaction
    parent = layout.generations if state == "initial-final" else layout.staging
    candidate = parent / candidate_id
    if state == "initial-empty-stage":
        return candidate, *transaction
    directories, files = _tree_relative_entries(candidate)
    data_files = tuple(path for path in files if path.name != "generation.json")
    cleanup_candidate = layout.staging / candidate_id
    transaction_id = json.loads(layout.transaction.read_bytes())["transaction_id"]

    child_directories = tuple(
        sorted(
            (path for path in directories if path != Path(".")),
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        )
    )
    publication = (
        (candidate, cleanup_candidate) if state == "initial-final" else ()
    )
    binding = tuple(
        path
        for relative in data_files
        for path in (
            cleanup_candidate / relative,
            _journal_cleanup_bound_path(
                cleanup_candidate, relative, transaction_id
            ),
        )
    ) + (
        cleanup_candidate / "generation.json",
        _journal_cleanup_bound_path(
            cleanup_candidate, Path("generation.json"), transaction_id
        ),
    )
    return _dedupe_expected_paths(
        publication,
        binding,
        tuple(cleanup_candidate / path for path in child_directories),
        (cleanup_candidate,),
        transaction,
    )


def _expected_recovery_paths(
    layout, candidate_id: str, state: str
) -> tuple[Path, ...]:
    if state.startswith("initial-"):
        return _expected_initial_cleanup_paths(layout, candidate_id, state)
    return {
        "later-before": (layout.status, layout.transaction),
        "matching-nonterminal-after": (layout.transaction, layout.status),
        "matching-terminal-after": (layout.transaction,),
    }[state]


def _materialize_journal_state(
    tmp_path: Path, state: str
) -> _JournalScenario:
    repository = Path(__file__).resolve().parents[1]
    state_paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "state")}
    )
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    builder = GenericAdapter(repository, state_paths)
    artifacts = builder._prepare_versioned_artifacts("6" * 32)
    layout = generic_layout_paths(state_paths)
    transaction_id = f"t-{'7' * 32}"
    generation_published = status_v5_payload(
        skill_root=skill_root,
        capability="manual",
        capsule=artifacts.capsule,
        active=artifacts.generation,
        previous=None,
        transaction_id=transaction_id,
        transaction_phase="generation-published",
    )
    capsule_published = status_v5_payload(
        skill_root=skill_root,
        capability="manual",
        capsule=artifacts.capsule,
        active=artifacts.generation,
        previous=None,
        transaction_id=transaction_id,
        transaction_phase="capsule-published",
    )
    activation_pending = status_v5_payload(
        skill_root=skill_root,
        capability="manual",
        capsule=artifacts.capsule,
        active=artifacts.generation,
        previous=None,
        transaction_id=transaction_id,
        transaction_phase="activation-pending",
    )
    terminal = status_v5_payload(
        skill_root=skill_root,
        capability="manual",
        capsule=artifacts.capsule,
        active=artifacts.generation,
        previous=None,
    )
    initial_bytes = ownership_journal_bytes(
        operation="first-install",
        transaction_id=transaction_id,
        skill_root=skill_root,
        baseline_status_bytes=None,
        before_status_bytes=None,
        after_status_bytes=canonical_json_bytes(generation_published),
        capsule=artifacts.capsule,
        candidate=artifacts.generation,
    )

    with builder._state_operation(create=True):
        builder._write_ownership_journal(initial_bytes)
        initial = validate_ownership_journal(
            initial_bytes,
            skill_root=skill_root,
            generations_root=layout.generations,
        )
        if state == "initial-empty-stage":
            initial.staging_root.mkdir()
        elif state == "initial-partial-stage":
            initial.staging_root.mkdir()
            (initial.staging_root / "generation.json").write_bytes(
                artifacts.generation.files["generation.json"]
            )
            (initial.staging_root / "SKILL.md").write_bytes(
                artifacts.generation.files["SKILL.md"]
            )
        elif state == "initial-complete-stage":
            _write_test_artifact(
                initial.staging_root, artifacts.generation.files
            )
        elif state == "initial-final":
            _write_test_artifact(
                initial.generation_root, artifacts.generation.files
            )
        elif state in {
            "later-before",
            "matching-nonterminal-after",
            "matching-terminal-after",
        }:
            _write_test_artifact(
                initial.generation_root, artifacts.generation.files
            )
            _write_test_artifact(
                skill_root / "voice-intent-normalizer",
                artifacts.capsule.files,
            )
            if state == "matching-terminal-after":
                before = canonical_json_bytes(activation_pending)
                after = canonical_json_bytes(terminal)
            else:
                before = canonical_json_bytes(generation_published)
                after = canonical_json_bytes(capsule_published)
            advanced_bytes = ownership_journal_transition_bytes(
                initial,
                before_status_bytes=before,
                after_status_bytes=after,
            )
            builder._replace_ownership_journal(
                skill_root,
                current_journal=initial,
                payload=advanced_bytes,
            )
            builder._write_status_payload(
                terminal
                if state == "matching-terminal-after"
                else (
                    capsule_published
                    if state == "matching-nonterminal-after"
                    else generation_published
                )
            )
    return _JournalScenario(
        state_paths=state_paths,
        repository=repository,
        skill_root=skill_root,
        candidate_id=artifacts.generation.identifier,
    )


def _materialize_upgrade_initial_state(
    tmp_path: Path, repository_v2: Path
) -> _JournalScenario:
    repository = Path(__file__).resolve().parents[1]
    state_paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "upgrade-state")}
    )
    skill_root = tmp_path / "upgrade-skills"
    skill_root.mkdir()
    installed = GenericAdapter(repository, state_paths)
    assert installed.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(state_paths)
    baseline_bytes = layout.status.read_bytes()
    baseline = validate_status_v5(
        baseline_bytes,
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    builder = GenericAdapter(repository_v2, state_paths)
    artifacts = builder._prepare_versioned_artifacts("9" * 32)
    transaction_id = f"t-{'a' * 32}"
    pending = status_v5_payload(
        skill_root=skill_root,
        capability="manual",
        capsule=baseline.capsule,
        active=baseline.active,
        previous=artifacts.generation,
        transaction_id=transaction_id,
        transaction_phase="generation-published",
    )
    journal_bytes = ownership_journal_bytes(
        operation="upgrade",
        transaction_id=transaction_id,
        skill_root=skill_root,
        baseline_status_bytes=baseline_bytes,
        before_status_bytes=baseline_bytes,
        after_status_bytes=canonical_json_bytes(pending),
        capsule=baseline.capsule,
        candidate=artifacts.generation,
    )
    with builder._state_operation(create=True):
        builder._write_ownership_journal(journal_bytes)
        journal = validate_ownership_journal(
            journal_bytes,
            skill_root=skill_root,
            generations_root=layout.generations,
        )
        _write_test_artifact(journal.generation_root, artifacts.generation.files)
    return _JournalScenario(
        state_paths=state_paths,
        repository=repository_v2,
        skill_root=skill_root,
        candidate_id=artifacts.generation.identifier,
    )


def _inject_post_rename_identity_validation_fault(
    monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> dict[str, bool]:
    original_publish = StateRootLease.publish_directory_no_replace
    original_identity = paths_module._stat_identity
    state = {"armed": False, "injected": False}

    def fail_identity_after_commit(info):
        if state["armed"]:
            state["armed"] = False
            state["injected"] = True
            raise OSError(
                f"injected {target_kind} post-rename identity-validation failure"
            )
        return original_identity(info)

    def publish_with_validation_fault(
        lease,
        source,
        destination,
        expected_identity,
        *,
        on_committed=None,
    ):
        destination_path = Path(destination)
        is_target = (
            destination_path.name == "voice-intent-normalizer"
            if target_kind == "capsule"
            else destination_path.parent == Path("adapters/generic/generations")
        )
        assert on_committed is not None
        if not is_target:
            return original_publish(
                lease,
                source,
                destination,
                expected_identity,
                on_committed=on_committed,
            )

        def record_commit_then_arm_validation_fault():
            on_committed()
            state["armed"] = True

        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=record_commit_then_arm_validation_fault,
        )

    if os.name != "nt":
        def fail_post_rename_recovery(*_args):
            raise OSError("injected post-rename recovery failure")

        monkeypatch.setattr(
            paths_module,
            "_restore_posix_directory_publication",
            fail_post_rename_recovery,
        )
    monkeypatch.setattr(paths_module, "_stat_identity", fail_identity_after_commit)
    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        publish_with_validation_fault,
    )
    return state


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


def test_publish_directory_no_replace_notifies_before_post_rename_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "complete.txt").write_text("complete", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    original_identity = paths_module._stat_identity
    committed = False
    validation_fault_observed = False

    def fail_validation_after_commit(info):
        nonlocal validation_fault_observed
        if committed:
            validation_fault_observed = True
            raise OSError("injected post-rename identity-validation failure")
        return original_identity(info)

    def record_commit():
        nonlocal committed
        committed = True

    with guard_state_root(root) as lease:
        monkeypatch.setattr(
            paths_module, "_stat_identity", fail_validation_after_commit
        )
        with pytest.raises(OSError):
            lease.publish_directory_no_replace(
                "source",
                "published",
                expected_identity,
                on_committed=record_commit,
            )

    assert committed
    assert validation_fault_observed


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory-handle contract")
def test_windows_directory_publication_renames_the_exact_retained_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "managed.txt").write_text("managed", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    original_open = paths_module._open_windows_directory_for_move
    replaced = False

    @contextmanager
    def replace_name_after_handle_open(path):
        nonlocal replaced
        with original_open(path) as descriptor:
            displaced = root / "displaced"
            source.rename(displaced)
            source.mkdir()
            (source / "replacement.txt").write_text("replacement", encoding="utf-8")
            replaced = True
            yield descriptor

    monkeypatch.setattr(
        paths_module,
        "_open_windows_directory_for_move",
        replace_name_after_handle_open,
    )

    with guard_state_root(root) as lease:
        with pytest.raises(
            paths_module.StateRootBoundaryError,
            match="source name replaced during exact move",
        ):
            lease.publish_directory_no_replace(
                "source", "published", expected_identity
            )

    assert replaced
    assert (source / "replacement.txt").read_text(encoding="utf-8") == "replacement"
    assert (root / "published/managed.txt").read_text(encoding="utf-8") == "managed"
    assert not (root / "displaced").exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux renameat2 contract"
)
def test_linux_directory_publication_uses_renameat2_noreplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    original = paths_module._rename_linux_directory_no_replace
    calls = []

    def record_call(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(paths_module, "_rename_linux_directory_no_replace", record_call)

    with guard_state_root(root) as lease:
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert len(calls) == 1
    assert (root / "published").is_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin renamex_np contract")
def test_darwin_directory_publication_uses_renamex_np_excl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    original = paths_module._rename_darwin_directory_no_replace
    calls = []

    def record_call(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(
        paths_module, "_rename_darwin_directory_no_replace", record_call
    )

    with guard_state_root(root) as lease:
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert len(calls) == 1
    assert (root / "published").is_dir()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux renameat2 unavailability"
)
def test_linux_unavailable_renameat2_fails_before_namespace_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import ctypes

    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: object())

    with guard_state_root(root) as lease, pytest.raises(OSError, match="unavailable"):
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert (source / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (root / "published").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin renamex_np unavailability")
def test_darwin_unavailable_renamex_np_fails_before_namespace_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import ctypes

    root = tmp_path / "authority"
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    expected_identity = (source.stat().st_dev, source.stat().st_ino)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: object())

    with guard_state_root(root) as lease, pytest.raises(OSError, match="unavailable"):
        lease.publish_directory_no_replace("source", "published", expected_identity)

    assert (source / "keep.txt").read_text(encoding="utf-8") == "keep"
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
        lease,
        source,
        destination,
        expected_identity,
        *,
        on_committed=None,
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
        return original(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )

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

    def publish_with_racer(
        lease,
        source,
        destination,
        expected_identity,
        *,
        on_committed=None,
    ):
        if Path(destination).name == "voice-intent-normalizer":
            target.mkdir()
            (target / "racer.txt").write_text("racer", encoding="utf-8")
        assert original is not None
        return original(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )

    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        publish_with_racer,
        raising=False,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert (target / "racer.txt").read_text(encoding="utf-8") == "racer"
    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert status.transaction_phase == "generation-published"
    assert layout.transaction.is_file()


def test_first_install_smoke_failure_never_activates_status(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    observed_complete = False
    observed_no_final_names = False

    def fail_smoke(capsule, generation):
        nonlocal observed_complete, observed_no_final_names
        if isinstance(capsule, Path):
            observed_complete = (
                (capsule / "capsule.json").is_file()
                and (Path(generation) / "generation.json").is_file()
            )
        else:
            observed_complete = (
                "capsule.json" in capsule.files
                and "generation.json" in generation.files
            )
        layout = generic_layout_paths(generic_adapter.state_paths)
        observed_no_final_names = (
            not (skill_root / "voice-intent-normalizer").exists()
            and not tuple(layout.generations.glob("g-*"))
        )
        assert not generic_adapter.state_paths.adapter_status_file("generic").exists()
        raise ValueError("injected smoke failure")

    monkeypatch.setattr(
        generic_adapter, "_smoke_generation", fail_smoke, raising=False
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert observed_complete
    assert observed_no_final_names
    assert result.status == "failed"
    assert not generic_adapter.state_paths.adapter_status_file("generic").exists()


def test_real_smoke_runs_before_any_final_runtime_name(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = generic_adapter._smoke_generation
    observed_no_final_names = False

    def observe_smoke(capsule, generation):
        nonlocal observed_no_final_names
        observed_no_final_names = (
            not (skill_root / "voice-intent-normalizer").exists()
            and not tuple(layout.generations.glob("g-*"))
        )
        return original(capsule, generation)

    monkeypatch.setattr(generic_adapter, "_smoke_generation", observe_smoke)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    assert observed_no_final_names
    _validated_installed_layout(generic_adapter, skill_root)


def test_first_install_journals_before_candidate_stage_exists(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch managed staging or publication beginning before journal durability."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    events: list[str] = []
    original_smoke = generic_adapter._smoke_generation
    original_stage = generic_adapter._stage_and_publish_generation
    original_exclusive = StateRootLease.write_bytes_exclusive
    original_atomic = StateRootLease.write_bytes_atomic
    original_fsync = StateRootLease.fsync_directory
    original_publish = StateRootLease.publish_directory_no_replace

    def observe_smoke(capsule, generation):
        original_smoke(capsule, generation)
        events.append("smoke-complete")

    def observe_stage(*args, **kwargs):
        events.append("stage-created")
        return original_stage(*args, **kwargs)

    def observe_exclusive(lease, relative, data):
        if Path(relative).name == "generation.json":
            events.append("manifest-written")
        return original_exclusive(lease, relative, data)

    def observe_atomic(lease, relative, data):
        relative = Path(relative)
        if relative == Path("adapters/generic/transaction.json"):
            events.append("journal-write")
        elif relative == Path("adapters/generic/status.json"):
            value = json.loads(data)
            transaction = value.get("transaction")
            if (
                isinstance(transaction, dict)
                and transaction.get("phase") == "generation-published"
            ):
                events.append("status-generation-published")
        return original_atomic(lease, relative, data)

    def observe_fsync(lease, relative=Path(".")):
        result = original_fsync(lease, relative)
        if (
            Path(relative) == Path("adapters/generic")
            and events
            and events[-1] == "journal-write"
        ):
            events.append("journal-parent-fsync")
        return result

    def observe_publish(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        result = original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )
        if Path(destination).parent == Path("adapters/generic/generations"):
            events.append("generation-published")
        return result

    monkeypatch.setattr(generic_adapter, "_smoke_generation", observe_smoke)
    monkeypatch.setattr(
        generic_adapter, "_stage_and_publish_generation", observe_stage
    )
    monkeypatch.setattr(StateRootLease, "write_bytes_exclusive", observe_exclusive)
    monkeypatch.setattr(StateRootLease, "write_bytes_atomic", observe_atomic)
    monkeypatch.setattr(StateRootLease, "fsync_directory", observe_fsync)
    monkeypatch.setattr(
        StateRootLease, "publish_directory_no_replace", observe_publish
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    assert events[:7] == [
        "smoke-complete",
        "journal-write",
        "journal-parent-fsync",
        "stage-created",
        "manifest-written",
        "generation-published",
        "status-generation-published",
    ]
    assert not layout.transaction.exists()


def test_upgrade_keeps_old_terminal_status_until_candidate_publication(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch upgrade preparation making a nonterminal status public too early."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    baseline = layout.status.read_bytes()
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    original_publish = StateRootLease.publish_directory_no_replace
    observed: list[bytes] = []

    def observe_publish(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        if Path(destination).parent != Path("adapters/generic/generations"):
            return original_publish(
                lease,
                source,
                destination,
                expected_identity,
                on_committed=on_committed,
            )

        def observe_commit():
            observed.append(layout.status.read_bytes())
            if on_committed is not None:
                on_committed()

        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=observe_commit,
        )

    monkeypatch.setattr(
        StateRootLease, "publish_directory_no_replace", observe_publish
    )

    result = upgraded.install(InstallOptions(output_dir=skill_root))

    assert result.status == "upgraded"
    assert observed == [baseline]


def test_journal_write_failure_publishes_no_stage_or_generation(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a failed prepublication journal write leaving a managed candidate."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_atomic = StateRootLease.write_bytes_atomic
    stage_started = False

    def fail_journal(lease, relative, data):
        if Path(relative) == Path("adapters/generic/transaction.json"):
            raise OSError("injected journal write failure")
        return original_atomic(lease, relative, data)

    original_stage = generic_adapter._stage_and_publish_generation

    def observe_stage(*args, **kwargs):
        nonlocal stage_started
        stage_started = True
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(StateRootLease, "write_bytes_atomic", fail_journal)
    monkeypatch.setattr(
        generic_adapter, "_stage_and_publish_generation", observe_stage
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert not stage_started
    assert not layout.status.exists()
    assert not layout.transaction.exists()
    assert not tuple(layout.staging.iterdir())
    assert not tuple(layout.generations.iterdir())


def test_reported_journal_fsync_failure_proceeds_only_with_exact_committed_bytes(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a reported journal fsync error aborting despite exact committed bytes."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_fsync = StateRootLease.fsync_directory
    injected = False

    def fail_once(lease, relative=Path(".")):
        nonlocal injected
        if (
            not injected
            and Path(relative) == Path("adapters/generic")
            and layout.transaction.is_file()
        ):
            injected = True
            raise OSError("reported journal fsync failure")
        return original_fsync(lease, relative)

    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_once)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert injected
    assert result.status == "installed"
    _validated_installed_layout(generic_adapter, skill_root)
    assert not layout.transaction.exists()


def test_generation_publication_callback_failure_never_removes_journal_anchor(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch callback cleanup deleting a generation already owned by the journal."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_publish = StateRootLease.publish_directory_no_replace
    injected = False

    def fail_after_commit(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        if Path(destination).parent != Path("adapters/generic/generations"):
            return original_publish(
                lease,
                source,
                destination,
                expected_identity,
                on_committed=on_committed,
            )

        def callback_then_fail():
            nonlocal injected
            if on_committed is not None:
                on_committed()
            injected = True
            raise OSError("injected generation publication callback failure")

        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=callback_then_fail,
        )

    monkeypatch.setattr(
        StateRootLease, "publish_directory_no_replace", fail_after_commit
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert injected
    assert result.status == "failed"
    assert layout.transaction.is_file()
    journal = validate_ownership_journal(
        layout.transaction.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert journal.generation_root.is_dir()
    assert not layout.status.exists()
    assert result.changed_paths.count(layout.transaction) == 1


def test_conflicting_journal_fails_closed_before_status_transition(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a transition overwriting journal bytes that no longer match ownership."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_stage = generic_adapter._stage_and_publish_generation
    conflict = b'{"conflict":"preserve-exactly"}'

    def conflict_after_publication(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        layout.transaction.write_bytes(conflict)
        return result

    monkeypatch.setattr(
        generic_adapter,
        "_stage_and_publish_generation",
        conflict_after_publication,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert layout.transaction.read_bytes() == conflict
    assert not layout.status.exists()
    assert len(tuple(layout.generations.iterdir())) == 1


def test_unreadable_journal_fails_closed_before_status_transition(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a journal read error being treated as permission to replace bytes."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_stage = generic_adapter._stage_and_publish_generation
    original_read = StateRootLease.read_bytes
    armed = False

    def arm_after_publication(*args, **kwargs):
        nonlocal armed
        result = original_stage(*args, **kwargs)
        armed = True
        return result

    def fail_journal_read(lease, relative, limit, label="state file"):
        if armed and Path(relative) == Path("adapters/generic/transaction.json"):
            raise OSError("injected unreadable journal")
        return original_read(lease, relative, limit, label)

    monkeypatch.setattr(
        generic_adapter, "_stage_and_publish_generation", arm_after_publication
    )
    monkeypatch.setattr(StateRootLease, "read_bytes", fail_journal_read)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert layout.transaction.is_file()
    assert not layout.status.exists()
    assert len(tuple(layout.generations.iterdir())) == 1


def test_callback_internal_failure_preserves_final_even_if_journal_conflicts(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch an incomplete publication callback deleting a journal-owned final."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_prepare = generic_adapter._prepare_versioned_artifacts
    original_record = generic_adapter._record_changes
    conflict = b'{"conflict":"callback-internal"}'
    candidate = None

    def capture_artifacts(nonce: str):
        nonlocal candidate
        artifacts = original_prepare(nonce)
        candidate = artifacts.generation
        return artifacts

    def fail_generation_callback(paths):
        if candidate is not None and any(
            path == layout.generations / candidate.identifier for path in paths
        ):
            layout.transaction.write_bytes(conflict)
            raise OSError("injected failure inside publication callback")
        original_record(paths)

    monkeypatch.setattr(
        generic_adapter, "_prepare_versioned_artifacts", capture_artifacts
    )
    monkeypatch.setattr(generic_adapter, "_record_changes", fail_generation_callback)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert candidate is not None
    final = layout.generations / candidate.identifier
    assert result.status == "failed"
    assert layout.transaction.read_bytes() == conflict
    assert final.is_dir()
    assert (final / "generation.json").is_file()
    assert not layout.status.exists()


def test_generation_manifest_precedes_actual_nested_directory_creation(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch retained-directory setup creating nested stage paths before manifest."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    events: list[tuple[str, Path]] = []
    original_mkdir = StateRootLease.mkdir
    original_write = StateRootLease.write_bytes_exclusive

    def observe_mkdir(lease, relative, *, mode=0o700, exist_ok=False):
        relative = Path(relative)
        if Path("adapters/generic/staging") in relative.parents:
            events.append(("mkdir", relative))
        return original_mkdir(lease, relative, mode=mode, exist_ok=exist_ok)

    def observe_write(lease, relative, data):
        relative = Path(relative)
        if relative.name == "generation.json":
            events.append(("manifest", relative))
        return original_write(lease, relative, data)

    monkeypatch.setattr(StateRootLease, "mkdir", observe_mkdir)
    monkeypatch.setattr(StateRootLease, "write_bytes_exclusive", observe_write)

    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )

    kinds = [kind for kind, _ in events]
    assert "mkdir" in kinds
    assert kinds[0] == "manifest"
    assert all(kind == "mkdir" for kind in kinds[1:])


@pytest.mark.parametrize("failure", ("unlink", "rmdir", "fsync"))
def test_candidate_cleanup_propagates_filesystem_failures(
    failure: str,
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch cleanup swallowing a destructive filesystem operation failure."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_publish = StateRootLease.publish_directory_no_replace
    original_remove = StateRootLease.remove_private_file
    original_rmdir = StateRootLease.rmdir
    original_fsync = StateRootLease.fsync_directory
    cleanup_started = False
    injected = False

    def fail_publication(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        nonlocal cleanup_started
        if Path(destination).parent == Path("adapters/generic/generations"):
            cleanup_started = True
            raise OSError("injected publication failure")
        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )

    def fail_remove(lease, relative, *, expected_identity, expected_bytes):
        nonlocal injected
        if failure == "unlink" and cleanup_started and not injected:
            injected = True
            raise OSError("cleanup unlink failure")
        return original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )

    def fail_rmdir(lease, relative, *, missing_ok=False):
        nonlocal injected
        if failure == "rmdir" and cleanup_started and not injected:
            injected = True
            raise OSError("cleanup rmdir failure")
        return original_rmdir(lease, relative, missing_ok=missing_ok)

    def fail_fsync(lease, relative=Path(".")):
        nonlocal injected
        if failure == "fsync" and cleanup_started and not injected:
            injected = True
            raise OSError("cleanup fsync failure")
        return original_fsync(lease, relative)

    monkeypatch.setattr(
        StateRootLease, "publish_directory_no_replace", fail_publication
    )
    monkeypatch.setattr(StateRootLease, "remove_private_file", fail_remove)
    monkeypatch.setattr(StateRootLease, "rmdir", fail_rmdir)
    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_fsync)

    with generic_adapter._state_operation(create=True):
        with pytest.raises(OSError, match=f"cleanup {failure} failure"):
            generic_adapter._first_install(
                skill_root, InstallOptions(output_dir=skill_root)
            )

    assert injected
    assert layout.transaction.is_file()


def test_failed_generation_stage_cleanup_remains_restart_recoverable(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """A later cleanup failure keeps the manifest needed by fresh recovery."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_prepare = generic_adapter._prepare_versioned_artifacts
    original_publish = StateRootLease.publish_directory_no_replace
    original_rmdir = StateRootLease.rmdir
    candidate = None
    cleanup_started = False
    injected = False

    def capture_artifacts(nonce: str):
        nonlocal candidate
        artifacts = original_prepare(nonce)
        candidate = artifacts.generation
        return artifacts

    def fail_generation_publication(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        nonlocal cleanup_started
        if Path(destination).parent == Path("adapters/generic/generations"):
            cleanup_started = True
            raise OSError("injected generation publication failure")
        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )

    def fail_first_child_rmdir(lease, relative, *, missing_ok=False):
        nonlocal injected
        relative = Path(relative)
        if candidate is not None:
            stage_relative = Path("adapters/generic/staging") / candidate.identifier
            if (
                cleanup_started
                and not injected
                and relative != stage_relative
                and stage_relative in relative.parents
            ):
                injected = True
                raise OSError("injected staged child-directory removal failure")
        return original_rmdir(lease, relative, missing_ok=missing_ok)

    monkeypatch.setattr(
        generic_adapter,
        "_prepare_versioned_artifacts",
        capture_artifacts,
    )
    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        fail_generation_publication,
    )
    monkeypatch.setattr(StateRootLease, "rmdir", fail_first_child_rmdir)

    failed = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert injected
    assert candidate is not None
    stage = layout.staging / candidate.identifier
    manifest_survived = (stage / "generation.json").is_file()
    journal_bytes = layout.transaction.read_bytes()

    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        original_publish,
    )
    monkeypatch.setattr(StateRootLease, "rmdir", original_rmdir)
    recovered = GenericAdapter(
        generic_adapter.repository,
        generic_adapter.state_paths,
    ).doctor()

    assert failed.status == "failed"
    assert manifest_survived
    assert recovered.status == "not-installed"
    assert not stage.exists()
    assert not layout.transaction.exists()
    assert journal_bytes


def test_candidate_cleanup_extra_entry_causes_zero_deletions(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch cleanup deleting declared files before discovering a conflict."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_publish = StateRootLease.publish_directory_no_replace
    original_unlink = StateRootLease.unlink
    original_prepare = generic_adapter._prepare_versioned_artifacts
    candidate = None
    unlink_calls = 0
    conflict_bytes = b"preserve conflict"

    def capture_artifacts(nonce: str):
        nonlocal candidate
        artifacts = original_prepare(nonce)
        candidate = artifacts.generation
        return artifacts

    def add_conflict_then_fail(
        lease, source, destination, expected_identity, *, on_committed=None
    ):
        if Path(destination).parent == Path("adapters/generic/generations"):
            extra = generic_adapter.state_paths.root / Path(source) / "unexpected.bin"
            extra.write_bytes(conflict_bytes)
            raise OSError("injected publication failure with extra entry")
        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=on_committed,
        )

    def count_unlinks(lease, relative, *, missing_ok=False):
        nonlocal unlink_calls
        if Path("adapters/generic/staging") in Path(relative).parents:
            unlink_calls += 1
        return original_unlink(lease, relative, missing_ok=missing_ok)

    monkeypatch.setattr(
        generic_adapter, "_prepare_versioned_artifacts", capture_artifacts
    )
    monkeypatch.setattr(
        StateRootLease, "publish_directory_no_replace", add_conflict_then_fail
    )
    monkeypatch.setattr(StateRootLease, "unlink", count_unlinks)

    with generic_adapter._state_operation(create=True):
        with pytest.raises(ValueError, match="unexpected cleanup entry"):
            generic_adapter._first_install(
                skill_root, InstallOptions(output_dir=skill_root)
            )

    assert candidate is not None
    stage = layout.staging / candidate.identifier
    assert unlink_calls == 0
    assert (stage / "unexpected.bin").read_bytes() == conflict_bytes
    for relative, expected in candidate.files.items():
        assert (stage / relative).read_bytes() == expected
    assert layout.transaction.is_file()


def test_each_status_transition_journals_exact_before_and_after_digests(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch any status transition being committed before its exact digest pair."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    writes: list[tuple[Path, bytes]] = []
    original_atomic = StateRootLease.write_bytes_atomic

    def record_writes(lease, relative, data):
        relative = Path(relative)
        if relative in {
            Path("adapters/generic/transaction.json"),
            Path("adapters/generic/status.json"),
        }:
            writes.append((relative, data))
        return original_atomic(lease, relative, data)

    monkeypatch.setattr(StateRootLease, "write_bytes_atomic", record_writes)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    before: bytes | None = None
    status_writes = 0
    for index, (relative, after) in enumerate(writes):
        if relative != Path("adapters/generic/status.json"):
            continue
        status_writes += 1
        assert index > 0
        journal_relative, journal_bytes = writes[index - 1]
        assert journal_relative == Path("adapters/generic/transaction.json")
        journal = validate_ownership_journal(
            journal_bytes,
            skill_root=skill_root,
            generations_root=generic_layout_paths(
                generic_adapter.state_paths
            ).generations,
        )
        assert journal.transition.before_digest == (
            None if before is None else hashlib.sha256(before).hexdigest()
        )
        assert journal.transition.after_digest == hashlib.sha256(after).hexdigest()
        before = after
    assert status_writes == 4


def test_terminal_status_commits_before_journal_retirement(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch journal retirement exposing a preterminal protected status."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_unlink = StateRootLease.unlink
    terminal_observed = False

    def observe_unlink(lease, relative):
        nonlocal terminal_observed
        if Path(relative) == Path("adapters/generic/transaction.json"):
            terminal_observed = (
                json.loads(layout.status.read_bytes())["transaction"] is None
            )
        return original_unlink(lease, relative)

    monkeypatch.setattr(StateRootLease, "unlink", observe_unlink)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    assert terminal_observed
    assert not layout.transaction.exists()


def test_terminal_journal_unlink_failure_returns_validated_committed_result(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a redundant journal unlink error hiding a durable terminal install."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original_unlink = StateRootLease.unlink

    def fail_journal_unlink(lease, relative):
        if Path(relative) == Path("adapters/generic/transaction.json"):
            raise OSError("injected terminal journal unlink failure")
        return original_unlink(lease, relative)

    monkeypatch.setattr(StateRootLease, "unlink", fail_journal_unlink)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    _validated_installed_layout(generic_adapter, skill_root)
    assert layout.transaction.is_file()
    assert result.changed_paths.count(layout.transaction) == 1


def test_terminal_journal_parent_fsync_failure_is_restart_recoverable(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a post-terminal parent-fsync error rolling back a committed install."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original_unlink = StateRootLease.unlink
    original_fsync = StateRootLease.fsync_directory
    retired = False
    injected = False

    def observe_unlink(lease, relative):
        nonlocal retired
        result = original_unlink(lease, relative)
        if Path(relative) == Path("adapters/generic/transaction.json"):
            retired = True
        return result

    def fail_after_retirement(lease, relative=Path(".")):
        nonlocal injected
        if (
            retired
            and not injected
            and Path(relative) == Path("adapters/generic")
        ):
            injected = True
            raise OSError("injected terminal journal parent fsync failure")
        return original_fsync(lease, relative)

    monkeypatch.setattr(StateRootLease, "unlink", observe_unlink)
    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_after_retirement)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert injected
    assert result.status == "installed"
    _validated_installed_layout(generic_adapter, skill_root)
    restarted = GenericAdapter(generic_adapter.repository, generic_adapter.state_paths)
    assert restarted.doctor().status == "installed"


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
    journal = validate_ownership_journal(
        layout.transaction.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert journal.generation_root == published[0]
    assert not layout.status.exists()
    published_paths = {published[0], *published[0].rglob("*")}
    assert published_paths <= set(failed.changed_paths)
    assert all(failed.changed_paths.count(path) == 1 for path in published_paths)


def test_generation_post_rename_validation_failure_reports_complete_publication(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    fault = _inject_post_rename_identity_validation_fault(
        monkeypatch, "generation"
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    published = tuple(layout.generations.glob("g-*"))

    assert fault["injected"]
    assert result.status == "failed"
    assert len(published) == 1
    assert (published[0] / "generation.json").is_file()
    assert layout.transaction.is_file()
    journal = validate_ownership_journal(
        layout.transaction.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert journal.generation_root == published[0]
    assert not layout.status.exists()
    published_paths = {published[0], *published[0].rglob("*")}
    assert published_paths <= set(result.changed_paths)
    assert all(result.changed_paths.count(path) == 1 for path in published_paths)


def test_capsule_parent_fsync_failure_reports_complete_publication(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    capsule = skill_root / "voice-intent-normalizer"
    original = StateRootLease.fsync_directory
    injected = False

    def fail_after_capsule_publish(lease, relative=Path(".")):
        nonlocal injected
        if (
            not injected
            and Path(relative) == Path(".")
            and (capsule / "capsule.json").is_file()
        ):
            injected = True
            raise OSError("injected capsule-parent fsync failure")
        return original(lease, relative)

    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_after_capsule_publish)

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    generations = tuple(layout.generations.glob("g-*"))

    assert injected
    assert result.status == "failed"
    assert len(generations) == 1
    assert layout.transaction.is_file()
    pending = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert pending.transaction_phase == "generation-published"
    published_paths = {
        capsule,
        *capsule.rglob("*"),
        generations[0],
        *generations[0].rglob("*"),
    }
    assert published_paths <= set(result.changed_paths)
    assert all(result.changed_paths.count(path) == 1 for path in published_paths)


def test_capsule_post_rename_validation_failure_reports_complete_publication(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    capsule = skill_root / "voice-intent-normalizer"
    fault = _inject_post_rename_identity_validation_fault(monkeypatch, "capsule")

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    generations = tuple(layout.generations.glob("g-*"))

    assert fault["injected"]
    assert result.status == "failed"
    assert len(generations) == 1
    assert (capsule / "capsule.json").is_file()
    assert layout.transaction.is_file()
    pending = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert pending.transaction_phase == "generation-published"
    published_paths = {
        capsule,
        *capsule.rglob("*"),
        generations[0],
        *generations[0].rglob("*"),
    }
    assert published_paths <= set(result.changed_paths)
    assert all(result.changed_paths.count(path) == 1 for path in published_paths)


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


def test_transaction_parent_fsync_failure_after_activation_is_committed(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = StateRootLease.fsync_directory
    injected = False

    def fail_after_transaction_unlink(lease, relative=Path(".")):
        nonlocal injected
        if (
            not injected
            and Path(relative) == Path("adapters/generic")
            and layout.status.is_file()
            and not layout.transaction.exists()
        ):
            injected = True
            raise OSError("injected transaction-parent fsync failure")
        return original(lease, relative)

    monkeypatch.setattr(
        StateRootLease, "fsync_directory", fail_after_transaction_unlink
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    _validated_installed_layout(generic_adapter, skill_root)

    assert injected
    assert result.status == "installed"
    assert layout.transaction.is_file()
    validate_ownership_journal(
        layout.transaction.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    restarted = GenericAdapter(generic_adapter.repository, generic_adapter.state_paths)
    assert restarted.doctor().status == "installed"
    assert not layout.transaction.exists()


def test_no_fallible_artifact_validation_runs_after_status_activation(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = generic_adapter._validate_generation_directory

    def reject_post_activation_validation(root, relative, artifact):
        if layout.status.exists():
            status = validate_status_v5(
                layout.status.read_bytes(),
                skill_root=skill_root,
                generations_root=layout.generations,
            )
            if status.transaction_id is None:
                raise OSError("injected post-activation validation failure")
        return original(root, relative, artifact)

    monkeypatch.setattr(
        generic_adapter,
        "_validate_generation_directory",
        reject_post_activation_validation,
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))
    _validated_installed_layout(generic_adapter, skill_root)

    assert result.status == "installed"


def test_no_anchored_tree_validation_runs_after_terminal_activation(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = generic_adapter._validate_anchored_tree
    terminal_validation_attempted = False

    def reject_after_terminal(*args, **kwargs):
        nonlocal terminal_validation_attempted
        if layout.status.exists():
            status = validate_status_v5(
                layout.status.read_bytes(),
                skill_root=skill_root,
                generations_root=layout.generations,
            )
            if status.transaction_id is None:
                terminal_validation_attempted = True
                raise OSError("injected post-terminal anchored validation")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        generic_adapter, "_validate_anchored_tree", reject_after_terminal
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    assert not terminal_validation_attempted
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert status.transaction_id is None


def test_outer_state_operation_teardown_failure_preserves_committed_install(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original = generic_adapter._state_operation

    @contextmanager
    def fail_after_real_teardown(*, create, lock_missing=True):
        with original(create=create, lock_missing=lock_missing) as lease:
            yield lease
        raise OSError("injected outer state-operation teardown failure")

    monkeypatch.setattr(
        generic_adapter, "_state_operation", fail_after_real_teardown
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "installed"
    _validated_installed_layout(generic_adapter, skill_root)


def test_outer_state_operation_teardown_failure_before_activation_is_failed(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = generic_adapter._state_operation

    @contextmanager
    def fail_after_real_teardown(*, create, lock_missing=True):
        with original(create=create, lock_missing=lock_missing) as lease:
            yield lease
        raise OSError("injected outer state-operation teardown failure")

    def fail_before_activation(_payload):
        raise OSError("injected pre-activation status failure")

    monkeypatch.setattr(
        generic_adapter, "_state_operation", fail_after_real_teardown
    )
    monkeypatch.setattr(
        generic_adapter, "_write_status_payload", fail_before_activation
    )

    result = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == "failed"
    assert not layout.status.exists()


def test_callback_failure_cleanup_preserves_replaced_generation_without_adopting_it(
    tmp_path: Path,
    generic_adapter: GenericAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    layout = generic_layout_paths(generic_adapter.state_paths)
    original = generic_adapter._write_status_payload
    replacement: Path | None = None

    def replace_published_generation(_payload: dict[str, object]) -> None:
        nonlocal replacement
        published = tuple(layout.generations.glob("g-*"))
        assert len(published) == 1
        replacement = published[0]
        displaced = layout.generations / "displaced-owned-generation"
        replacement.rename(displaced)
        replacement.mkdir()
        (replacement / "keep.txt").write_text("replacement", encoding="utf-8")
        shutil.rmtree(displaced)
        raise OSError("injected status callback failure after identity replacement")

    monkeypatch.setattr(
        generic_adapter, "_write_status_payload", replace_published_generation
    )
    failed = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert failed.status == "failed"
    assert replacement is not None
    marker = replacement / "keep.txt"
    assert marker.read_text(encoding="utf-8") == "replacement"

    monkeypatch.setattr(generic_adapter, "_write_status_payload", original)
    journal_before = layout.transaction.read_bytes()
    recovered = generic_adapter.install(InstallOptions(output_dir=skill_root))

    assert recovered.status == "failed"
    assert not layout.status.exists()
    assert layout.transaction.read_bytes() == journal_before
    assert marker.read_text(encoding="utf-8") == "replacement"

    uninstalled = generic_adapter.uninstall(UninstallOptions(output_dir=skill_root))

    assert uninstalled.status == "failed"
    assert layout.transaction.read_bytes() == journal_before
    assert marker.read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("operation", ("install", "doctor", "uninstall"))
@pytest.mark.parametrize(
    "state",
    (
        "initial-journal-only",
        "initial-empty-stage",
        "initial-partial-stage",
        "initial-complete-stage",
        "initial-final",
        "later-before",
        "matching-nonterminal-after",
        "matching-terminal-after",
    ),
)
def test_ownership_journal_fresh_process_matrix(
    operation: str,
    state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a fresh public adapter stranding an exactly journal-owned state."""
    scenario = _materialize_journal_state(tmp_path, state)
    layout = generic_layout_paths(scenario.state_paths)
    expected_recovery = _expected_recovery_paths(
        layout, scenario.candidate_id, state
    )
    fixed_token = "b" * 32
    monkeypatch.setattr(secrets, "token_hex", lambda size: "b" * (size * 2))
    uninstall_changes: tuple[Path, ...] = ()
    if operation == "uninstall" and not state.startswith("initial-"):
        status = validate_status_v5(
            layout.status.read_bytes(),
            skill_root=scenario.skill_root,
            generations_root=layout.generations,
        )
        transaction_id = f"t-{fixed_token}"
        capsule = scenario.skill_root / "voice-intent-normalizer"
        capsule_tombstone = scenario.skill_root / (
            f".voice-intent-normalizer.retired-{fixed_token}"
        )
        removals = [
            _expected_removed_tree_paths(
                capsule, capsule_tombstone, "capsule.json"
            )
        ]
        for reference in (status.active, status.previous):
            if reference is None:
                continue
            generation = layout.generations / reference.generation_id
            retired = layout.retired / (
                f"{reference.generation_id}.{transaction_id}"
            )
            removals.append(
                _expected_removed_tree_paths(
                    generation, retired, "generation.json"
                )
            )
        uninstall_changes = _dedupe_expected_paths(
            (layout.transaction, layout.status), *removals
        )
    adapter, invoke = scenario.restart(operation)

    result = invoke()

    initial = state.startswith("initial-")
    expected_status = {
        (True, "install"): "installed",
        (True, "doctor"): "not-installed",
        (True, "uninstall"): "not-installed",
        (False, "install"): "already-installed",
        (False, "doctor"): "installed",
        (False, "uninstall"): "uninstalled",
    }[(initial, operation)]
    assert result.status == expected_status
    assert not layout.transaction.exists()
    assert not (layout.staging / scenario.candidate_id).exists()
    expected_changes = expected_recovery
    if operation == "install" and state.startswith("initial-"):
        terminal_status = validate_status_v5(
            layout.status.read_bytes(),
            skill_root=scenario.skill_root,
            generations_root=layout.generations,
        )
        expected_changes = _dedupe_expected_paths(
            expected_recovery,
            _expected_published_tree_paths(
                layout.generations / terminal_status.active.generation_id
            ),
            (layout.status,),
            _expected_published_tree_paths(
                scenario.skill_root / "voice-intent-normalizer"
            ),
        )
    elif operation == "uninstall" and not state.startswith("initial-"):
        expected_changes = _dedupe_expected_paths(
            expected_recovery, uninstall_changes
        )
    assert result.changed_paths == expected_changes
    if initial:
        assert not (layout.generations / scenario.candidate_id).exists()
    if operation in {"doctor", "uninstall"} and initial or operation == "uninstall":
        assert not layout.status.exists()
        assert not (scenario.skill_root / "voice-intent-normalizer").exists()
        return
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=scenario.skill_root,
        generations_root=layout.generations,
    )
    assert status.transaction_id is None
    anchored = {status.active.generation_id}
    if status.previous is not None:
        anchored.add(status.previous.generation_id)
    assert {path.name for path in layout.generations.iterdir()} == anchored


@pytest.mark.parametrize("operation", ("install", "doctor", "uninstall"))
def test_ownership_journal_fresh_process_preserves_upgrade_baseline(
    operation: str,
    tmp_path: Path,
    repository_v2: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch initial upgrade cleanup replacing the old terminal installation."""
    scenario = _materialize_upgrade_initial_state(tmp_path, repository_v2)
    layout = generic_layout_paths(scenario.state_paths)
    baseline_bytes = layout.status.read_bytes()
    baseline = validate_status_v5(
        baseline_bytes,
        skill_root=scenario.skill_root,
        generations_root=layout.generations,
    )
    expected_recovery = _expected_initial_cleanup_paths(
        layout, scenario.candidate_id, "initial-final"
    )
    fixed_token = "c" * 32
    monkeypatch.setattr(secrets, "token_hex", lambda size: "c" * (size * 2))
    capsule = scenario.skill_root / "voice-intent-normalizer"
    capsule_tombstone = scenario.skill_root / (
        f".voice-intent-normalizer.retired-{fixed_token}"
    )
    generation = layout.generations / baseline.active.generation_id
    retired_generation = layout.retired / (
        f"{baseline.active.generation_id}.t-{fixed_token}"
    )
    uninstall_changes = _dedupe_expected_paths(
        (layout.transaction, layout.status),
        _expected_removed_tree_paths(
            capsule, capsule_tombstone, "capsule.json"
        ),
        _expected_removed_tree_paths(
            generation, retired_generation, "generation.json"
        ),
    )
    adapter, invoke = scenario.restart(operation)

    result = invoke()

    assert result.status == {
        "install": "upgraded",
        "doctor": "installed",
        "uninstall": "uninstalled",
    }[operation]
    assert not layout.transaction.exists()
    assert not (layout.generations / scenario.candidate_id).exists()
    if operation == "doctor":
        assert layout.status.read_bytes() == baseline_bytes
    elif operation == "uninstall":
        assert not layout.status.exists()
    expected_changes = expected_recovery
    if operation == "install":
        terminal = validate_status_v5(
            layout.status.read_bytes(),
            skill_root=scenario.skill_root,
            generations_root=layout.generations,
        )
        expected_changes = _dedupe_expected_paths(
            expected_recovery,
            _expected_published_tree_paths(
                layout.generations / terminal.active.generation_id
            ),
            (layout.status,),
        )
    elif operation == "uninstall":
        expected_changes = _dedupe_expected_paths(
            expected_recovery, uninstall_changes
        )
    assert result.changed_paths == expected_changes


@pytest.mark.parametrize("operation", ("install", "doctor", "uninstall"))
@pytest.mark.parametrize("baseline_ref", ("active", "previous"))
def test_ownership_journal_review_round1_rejects_baseline_candidate_alias(
    operation: str,
    baseline_ref: str,
    tmp_path: Path,
    repository_v2: Path,
):
    """A canonical upgrade journal must never own a launchable baseline ref."""
    scenario = _materialize_upgrade_initial_state(tmp_path, repository_v2)
    layout = generic_layout_paths(scenario.state_paths)
    status_payload = json.loads(layout.status.read_bytes())
    journal_payload = json.loads(layout.transaction.read_bytes())
    if baseline_ref == "previous":
        status_payload["previous"] = journal_payload["candidate"]
        baseline_bytes = canonical_json_bytes(status_payload)
        layout.status.write_bytes(baseline_bytes)
    else:
        baseline_bytes = layout.status.read_bytes()
    journal_payload["candidate"] = status_payload[baseline_ref]
    baseline_digest = hashlib.sha256(baseline_bytes).hexdigest()
    journal_payload["baseline_status_digest"] = baseline_digest
    journal_payload["status_transition"]["before_digest"] = baseline_digest
    layout.transaction.write_bytes(canonical_json_bytes(journal_payload))
    before = _file_snapshot(scenario.state_paths.root, scenario.skill_root)

    adapter, invoke = scenario.restart(operation)
    result = invoke()

    assert result.status in {"failed", "degraded"}
    assert result.changed_paths == ()
    assert _file_snapshot(
        scenario.state_paths.root, scenario.skill_root
    ) == before


@pytest.mark.parametrize(
    "tamper",
    ("missing-declared-file", "staging-temporary"),
)
def test_ownership_journal_review_round1_preserves_partial_final_candidate(
    tamper: str,
    tmp_path: Path,
):
    """An atomically published final candidate is necessarily exact and complete."""
    scenario = _materialize_journal_state(tmp_path, "initial-final")
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.generations / scenario.candidate_id
    if tamper == "missing-declared-file":
        (candidate / "SKILL.md").unlink()
    else:
        (candidate / ".SKILL.md.tmp").write_bytes(b"uncommitted")
    before = _file_snapshot(scenario.state_paths.root, scenario.skill_root)

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert result.status == "degraded"
    assert result.changed_paths == ()
    assert _file_snapshot(
        scenario.state_paths.root, scenario.skill_root
    ) == before


@pytest.mark.parametrize("target_name", ("SKILL.md", "generation.json"))
def test_ownership_journal_review_round1_rechecks_file_identity_before_deletion(
    target_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A same-name replacement after preflight is not journal-owned."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    original = GenericAdapter._preflight_journal_cleanup
    replacement_identity: tuple[int, int] | None = None
    before_paths = set(
        _file_snapshot(scenario.state_paths.root, scenario.skill_root)
    )

    def replace_after_preflight(adapter, relative, journal):
        nonlocal replacement_identity
        tree = original(adapter, relative, journal)
        target = adapter.state_paths.root / relative / target_name
        original_bytes = target.read_bytes()
        displaced = target.with_name(f".{target.name}.displaced")
        target.rename(displaced)
        target.write_bytes(original_bytes)
        displaced.unlink()
        info = target.lstat()
        replacement_identity = (info.st_dev, info.st_ino)
        return tree

    monkeypatch.setattr(
        GenericAdapter,
        "_preflight_journal_cleanup",
        replace_after_preflight,
    )

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    target = candidate / target_name
    assert result.status == "degraded"
    assert result.changed_paths == ()
    assert layout.transaction.read_bytes() == journal_bytes
    assert target.exists()
    assert replacement_identity is not None
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert set(
        _file_snapshot(scenario.state_paths.root, scenario.skill_root)
    ) == before_paths


@pytest.mark.parametrize("target_name", ("SKILL.md", "generation.json"))
def test_ownership_journal_review_round2_preserves_replacement_at_move_boundary(
    target_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Bind the exact owned file before a boundary-time replacement can delete it."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.staging / scenario.candidate_id
    target = candidate / target_name
    displaced = target.with_name(f".{target.name}.displaced-at-boundary")
    original_bytes = target.read_bytes()
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]
    bound_target = _journal_cleanup_bound_path(
        candidate, Path(target_name), transaction_id
    )
    expected_files = {
        path.relative_to(candidate): path.read_bytes()
        for path in candidate.rglob("*")
        if path.is_file()
    }
    injected = False

    def inject_replacement() -> None:
        nonlocal injected
        assert not injected
        target.rename(displaced)
        target.write_bytes(b"replacement at exact boundary")
        injected = True

    if os.name == "nt":
        native = paths_module._move_windows_handle_no_replace

        def move_with_boundary_replacement(descriptor, destination):
            if Path(destination) == bound_target:
                inject_replacement()
            native(descriptor, destination)

        monkeypatch.setattr(
            paths_module,
            "_move_windows_handle_no_replace",
            move_with_boundary_replacement,
        )
    elif sys.platform.startswith("linux"):
        native = paths_module._rename_linux_directory_no_replace

        def move_with_boundary_replacement(
            source_parent, source_name, destination_parent, destination_name
        ):
            if destination_name == bound_target.name:
                inject_replacement()
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )

        monkeypatch.setattr(
            paths_module,
            "_rename_linux_directory_no_replace",
            move_with_boundary_replacement,
        )
    elif sys.platform == "darwin":
        native = paths_module._rename_darwin_directory_no_replace

        def move_with_boundary_replacement(
            source_parent, source_name, destination_parent, destination_name
        ):
            if destination_name == bound_target.name:
                inject_replacement()
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )

        monkeypatch.setattr(
            paths_module,
            "_rename_darwin_directory_no_replace",
            move_with_boundary_replacement,
        )
    else:
        pytest.skip("native exclusive file moves are unsupported")

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert result.status == "degraded"
    assert layout.transaction.read_bytes() == journal_bytes
    assert target.read_bytes() == b"replacement at exact boundary"
    surviving_owned = [
        path
        for path in (displaced, bound_target)
        if path.exists() and path.read_bytes() == original_bytes
    ]
    assert len(surviving_owned) == 1
    for relative, data in expected_files.items():
        if relative == Path(target_name):
            continue
        assert (candidate / relative).read_bytes() == data


@pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle deletion")
@pytest.mark.parametrize("relative_path", (Path("SKILL.md"), Path("generation.json")))
def test_windows_private_cleanup_deletes_only_retained_file_handle(
    relative_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A replacement at permanent deletion must never be unlinked by name."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]
    bound_target = _journal_cleanup_bound_path(
        candidate, relative_path, transaction_id
    )
    displaced = bound_target.with_name(
        f".{relative_path.name}.owned-displaced-at-permanent-delete"
    )
    replacement = b"replacement"
    source_info = (candidate / relative_path).stat()
    source_identity = (source_info.st_dev, source_info.st_ino)
    original_delete = paths_module._delete_windows_file_handle
    injected = False

    def replace_at_permanent_delete(descriptor: int):
        nonlocal injected
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == source_identity and not injected:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            writer = kernel32.CreateFileW(
                paths_module._extended_windows_path(bound_target),
                0x40000000,  # GENERIC_WRITE
                0x7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if writer == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            assert kernel32.CloseHandle(writer)
            bound_target.rename(displaced)
            bound_target.write_bytes(replacement)
            injected = True
        return original_delete(descriptor)

    monkeypatch.setattr(
        paths_module,
        "_delete_windows_file_handle",
        replace_at_permanent_delete,
    )

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert bound_target.exists()
    assert bound_target.read_bytes() == replacement
    assert layout.transaction.exists()
    assert layout.transaction.read_bytes() == journal_bytes
    assert result.status in {"failed", "degraded"}
    assert not displaced.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle deletion")
def test_windows_private_cleanup_preserves_byte_identical_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The anchored identity, not byte equality, authorizes deletion."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    journal_bytes = layout.transaction.read_bytes()
    original_require = GenericAdapter._require_journal_cleanup_file
    original_delete = paths_module._delete_windows_file_handle
    bound_target: Path | None = None
    displaced: Path | None = None
    source_bytes: bytes | None = None
    source_identity: tuple[int, int] | None = None
    replacement_identity: tuple[int, int] | None = None
    injected = False
    replacement_delete_attempted = False

    def replace_after_identity_validation(adapter, lease, path, tree):
        nonlocal bound_target, displaced, injected
        nonlocal replacement_identity, source_bytes, source_identity
        original_require(adapter, lease, path, tree)
        if tree.binding_complete and Path(path) == tree.files[0] and not injected:
            bound_target = scenario.state_paths.root / path
            displaced = bound_target.with_name(
                f".{bound_target.name}.anchored-displaced"
            )
            source_bytes = tree.file_bytes[path]
            source_identity = tree.file_identities[path]
            bound_target.rename(displaced)
            bound_target.write_bytes(source_bytes)
            replacement_info = bound_target.stat()
            replacement_identity = (
                replacement_info.st_dev,
                replacement_info.st_ino,
            )
            injected = True

    def observe_permanent_delete(descriptor):
        nonlocal replacement_delete_attempted
        info = os.fstat(descriptor)
        if replacement_identity == (info.st_dev, info.st_ino):
            replacement_delete_attempted = True
        return original_delete(descriptor)

    monkeypatch.setattr(
        GenericAdapter,
        "_require_journal_cleanup_file",
        replace_after_identity_validation,
    )
    monkeypatch.setattr(
        paths_module,
        "_delete_windows_file_handle",
        observe_permanent_delete,
    )

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert bound_target is not None
    assert displaced is not None
    assert source_bytes is not None
    assert source_identity is not None
    assert replacement_identity is not None
    assert not replacement_delete_attempted
    assert result.status in {"failed", "degraded"}
    assert layout.transaction.read_bytes() == journal_bytes
    assert bound_target.read_bytes() == source_bytes
    current = bound_target.stat()
    assert (current.st_dev, current.st_ino) == replacement_identity
    assert displaced.read_bytes() == source_bytes
    displaced_info = displaced.stat()
    assert (displaced_info.st_dev, displaced_info.st_ino) == source_identity


def test_exact_directory_move_preserves_raced_public_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A replacement at the final-to-stage move boundary remains journaled."""
    scenario = _materialize_journal_state(tmp_path, "initial-final")
    layout = generic_layout_paths(scenario.state_paths)
    final = layout.generations / scenario.candidate_id
    stage = layout.staging / scenario.candidate_id
    displaced = final.with_name(f".{scenario.candidate_id}.owned-displaced")
    journal_bytes = layout.transaction.read_bytes()
    injected = False

    if os.name == "nt":
        native = paths_module._move_windows_handle_no_replace

        def replace_final_at_native_move(descriptor, destination):
            nonlocal injected
            if Path(destination) == stage and not injected:
                final.rename(displaced)
                final.mkdir()
                (final / "replacement.txt").write_bytes(b"replacement")
                injected = True
            native(descriptor, destination)

        monkeypatch.setattr(
            paths_module,
            "_move_windows_handle_no_replace",
            replace_final_at_native_move,
        )
    elif sys.platform.startswith("linux"):
        native = paths_module._rename_linux_directory_no_replace

        def replace_final_at_native_move(
            source_parent, source_name, destination_parent, destination_name
        ):
            nonlocal injected
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
            if destination_name == stage.name and not injected:
                final.mkdir()
                (final / "replacement.txt").write_bytes(b"replacement")
                injected = True

        monkeypatch.setattr(
            paths_module,
            "_rename_linux_directory_no_replace",
            replace_final_at_native_move,
        )
    elif sys.platform == "darwin":
        native = paths_module._rename_darwin_directory_no_replace

        def replace_final_at_native_move(
            source_parent, source_name, destination_parent, destination_name
        ):
            nonlocal injected
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
            if destination_name == stage.name and not injected:
                final.mkdir()
                (final / "replacement.txt").write_bytes(b"replacement")
                injected = True

        monkeypatch.setattr(
            paths_module,
            "_rename_darwin_directory_no_replace",
            replace_final_at_native_move,
        )
    else:
        pytest.skip("native exclusive directory moves are unsupported")

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert stage.exists()
    assert final.exists()
    assert (final / "replacement.txt").read_bytes() == b"replacement"
    assert layout.transaction.exists()
    assert layout.transaction.read_bytes() == journal_bytes
    assert result.status in {"failed", "degraded"}


def test_final_name_reappearance_before_cleanup_preserves_both_and_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Recovery rechecks the public name after move durability, before cleanup."""
    scenario = _materialize_journal_state(tmp_path, "initial-final")
    layout = generic_layout_paths(scenario.state_paths)
    final = layout.generations / scenario.candidate_id
    stage = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    original_fsync = StateRootLease.fsync_directory
    injected = False

    def replace_after_stage_fsync(lease, relative=Path(".")):
        nonlocal injected
        result = original_fsync(lease, relative)
        if (
            Path(relative) == Path("adapters/generic/staging")
            and stage.is_dir()
            and not final.exists()
            and not injected
        ):
            final.mkdir()
            (final / "replacement.txt").write_bytes(b"replacement")
            injected = True
        return result

    monkeypatch.setattr(
        StateRootLease,
        "fsync_directory",
        replace_after_stage_fsync,
    )

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert result.status in {"failed", "degraded"}
    assert stage.is_dir()
    assert (final / "replacement.txt").read_bytes() == b"replacement"
    assert layout.transaction.read_bytes() == journal_bytes


def test_final_name_reappearance_at_retirement_preserves_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Recovery rechecks the public name after cleanup, before retirement."""
    scenario = _materialize_journal_state(tmp_path, "initial-final")
    layout = generic_layout_paths(scenario.state_paths)
    final = layout.generations / scenario.candidate_id
    stage = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    original_read = StateRootLease.read_bytes
    injected = False

    def replace_after_journal_validation(lease, relative, limit, label):
        nonlocal injected
        data = original_read(lease, relative, limit, label)
        if (
            Path(relative) == Path("adapters/generic/transaction.json")
            and not stage.exists()
            and not final.exists()
            and not injected
        ):
            final.mkdir()
            (final / "replacement.txt").write_bytes(b"replacement")
            injected = True
        return data

    monkeypatch.setattr(
        StateRootLease,
        "read_bytes",
        replace_after_journal_validation,
    )

    result = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert result.status in {"failed", "degraded"}
    assert not stage.exists()
    assert (final / "replacement.txt").read_bytes() == b"replacement"
    assert layout.transaction.read_bytes() == journal_bytes
    assert not layout.status.exists()
    assert not (scenario.skill_root / "voice-intent-normalizer").exists()


@pytest.mark.parametrize(
    "fault",
    (
        "data-unlink",
        "child-rmdir",
        "manifest-unlink",
        "root-rmdir",
        "parent-fsync",
    ),
)
def test_ownership_journal_review_round2_final_cleanup_fault_retries_from_stage(
    fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A complete final is relocated before any retryable partial cleanup."""
    scenario = _materialize_journal_state(tmp_path, "initial-final")
    layout = generic_layout_paths(scenario.state_paths)
    final = layout.generations / scenario.candidate_id
    stage = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]

    stage_relative = (
        Path("adapters/generic/staging") / scenario.candidate_id
    )

    def bound(relative: Path) -> Path:
        return _journal_cleanup_bound_path(
            stage_relative, relative, transaction_id
        )

    bound_data = bound(Path("SKILL.md"))
    bound_manifest = bound(Path("generation.json"))
    directories, files = _tree_relative_entries(final)
    data_files = tuple(path for path in files if path.name != "generation.json")
    binding_changes = _dedupe_expected_paths(
        (final, stage),
        tuple(
            path
            for relative in data_files
            for path in (
                stage / relative,
                scenario.state_paths.root / bound(relative),
            )
        ),
        (
            stage / "generation.json",
            scenario.state_paths.root / bound_manifest,
        ),
    )
    child_directories = tuple(
        stage / relative
        for relative in sorted(
            (path for path in directories if path != Path(".")),
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        )
    )
    first_direct_child = next(
        index
        for index, path in enumerate(child_directories)
        if path.parent == stage
    )
    original_remove = StateRootLease.remove_private_file
    original_rmdir = StateRootLease.rmdir
    original_fsync = StateRootLease.fsync_directory
    injected = False

    def remove_then_fail(lease, relative, *, expected_identity, expected_bytes):
        nonlocal injected
        relative = Path(relative)
        original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )
        selected = (fault == "data-unlink" and relative == bound_data) or (
            fault == "manifest-unlink" and relative == bound_manifest
        )
        if selected and not injected:
            injected = True
            raise OSError(f"injected post-commit {fault}")

    def rmdir_then_fail(lease, relative, *, missing_ok=False):
        nonlocal injected
        relative = Path(relative)
        original_rmdir(lease, relative, missing_ok=missing_ok)
        selected = (
            fault == "child-rmdir"
            and relative.parent.name == scenario.candidate_id
        ) or (
            fault == "root-rmdir"
            and relative.name == scenario.candidate_id
        )
        if selected and not injected:
            injected = True
            raise OSError(f"injected post-commit {fault}")

    def fsync_then_fail(lease, relative=Path(".")):
        nonlocal injected
        relative = Path(relative)
        original_fsync(lease, relative)
        if (
            fault == "parent-fsync"
            and relative
            in {Path("adapters/generic/staging"), Path("adapters/generic/generations")}
            and not final.exists()
            and not stage.exists()
            and not injected
        ):
            injected = True
            raise OSError("injected post-commit candidate-parent fsync")

    monkeypatch.setattr(StateRootLease, "remove_private_file", remove_then_fail)
    monkeypatch.setattr(StateRootLease, "rmdir", rmdir_then_fail)
    monkeypatch.setattr(StateRootLease, "fsync_directory", fsync_then_fail)

    first = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert first.status == "degraded"
    assert layout.transaction.read_bytes() == journal_bytes
    assert not final.exists()
    first_directories = (
        child_directories[: first_direct_child + 1]
        if fault == "child-rmdir"
        else child_directories
        if fault in {"manifest-unlink", "root-rmdir", "parent-fsync"}
        else ()
    )
    assert first.changed_paths == _dedupe_expected_paths(
        binding_changes, first_directories
    )

    second = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert second.status == "not-installed"
    ordered_bound_files = tuple(
        sorted((bound(relative) for relative in data_files), key=str)
    )
    data_fault_index = ordered_bound_files.index(bound_data)
    remaining_files = (
        tuple(
            scenario.state_paths.root / path
            for path in ordered_bound_files[data_fault_index + 1 :]
        )
        if fault == "data-unlink"
        else ()
    )
    remaining_directories = {
        "data-unlink": child_directories,
        "child-rmdir": child_directories[first_direct_child + 1 :],
        "manifest-unlink": (),
        "root-rmdir": (),
        "parent-fsync": (),
    }[fault]
    second_tail = (
        (
            scenario.state_paths.root / bound_manifest,
            stage,
            layout.transaction,
        )
        if fault in {"data-unlink", "child-rmdir", "manifest-unlink"}
        else (layout.transaction,)
    )
    assert second.changed_paths == _dedupe_expected_paths(
        remaining_files, remaining_directories, second_tail
    )
    assert not layout.transaction.exists()
    assert not final.exists()
    assert not stage.exists()


def test_private_cleanup_records_post_delete_revalidation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A later validation error must not hide a committed private deletion."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    journal_bytes = layout.transaction.read_bytes()
    original_remove = StateRootLease.remove_private_file
    original_listdir = StateRootLease.listdir

    def stop_after_binding(lease, relative, *, expected_identity, expected_bytes):
        raise OSError("injected pre-commit private cleanup failure")

    monkeypatch.setattr(
        StateRootLease,
        "remove_private_file",
        stop_after_binding,
    )
    first = GenericAdapter(scenario.repository, scenario.state_paths).doctor()
    assert first.status == "degraded"
    assert layout.transaction.read_bytes() == journal_bytes

    deleted: Path | None = None
    injected = False

    def remove_then_mark(lease, relative, *, expected_identity, expected_bytes):
        nonlocal deleted
        original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )
        deleted = Path(relative)

    def fail_revalidation(lease, relative):
        nonlocal injected
        if deleted is not None and not injected:
            injected = True
            raise OSError("injected post-delete tree validation failure")
        return original_listdir(lease, relative)

    monkeypatch.setattr(
        StateRootLease,
        "remove_private_file",
        remove_then_mark,
    )
    monkeypatch.setattr(StateRootLease, "listdir", fail_revalidation)

    second = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert deleted is not None
    assert second.status == "degraded"
    assert second.changed_paths == (scenario.state_paths.root / deleted,)
    assert layout.transaction.read_bytes() == journal_bytes

    monkeypatch.setattr(StateRootLease, "remove_private_file", original_remove)
    monkeypatch.setattr(StateRootLease, "listdir", original_listdir)
    third = GenericAdapter(scenario.repository, scenario.state_paths).doctor()
    assert third.status == "not-installed"


def test_private_cleanup_restores_manifest_after_post_delete_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A post-delete validation error restores exact manifest and journal bytes."""
    scenario = _materialize_journal_state(tmp_path, "initial-complete-stage")
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]
    manifest_bytes = (candidate / "generation.json").read_bytes()
    bound_manifest = _journal_cleanup_bound_path(
        candidate,
        Path("generation.json"),
        transaction_id,
    )
    bound_manifest_relative = bound_manifest.relative_to(
        scenario.state_paths.root
    )
    original_remove = StateRootLease.remove_private_file
    original_listdir = StateRootLease.listdir

    def stop_before_manifest(lease, relative, *, expected_identity, expected_bytes):
        if Path(relative) == bound_manifest_relative:
            raise OSError("injected pre-commit manifest removal failure")
        return original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )

    monkeypatch.setattr(
        StateRootLease,
        "remove_private_file",
        stop_before_manifest,
    )
    first = GenericAdapter(scenario.repository, scenario.state_paths).doctor()
    assert first.status == "degraded"
    assert bound_manifest.read_bytes() == manifest_bytes
    assert layout.transaction.read_bytes() == journal_bytes

    manifest_deleted = False
    injected = False

    def remove_then_mark(lease, relative, *, expected_identity, expected_bytes):
        nonlocal manifest_deleted
        original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )
        if Path(relative) == bound_manifest_relative:
            manifest_deleted = True

    def fail_manifest_revalidation(lease, relative):
        nonlocal injected
        if manifest_deleted and not injected:
            injected = True
            raise OSError("injected post-delete manifest validation failure")
        return original_listdir(lease, relative)

    monkeypatch.setattr(
        StateRootLease,
        "remove_private_file",
        remove_then_mark,
    )
    monkeypatch.setattr(
        StateRootLease,
        "listdir",
        fail_manifest_revalidation,
    )

    second = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert injected
    assert second.status == "degraded"
    assert second.changed_paths == (bound_manifest,)
    assert bound_manifest.read_bytes() == manifest_bytes
    assert layout.transaction.read_bytes() == journal_bytes

    monkeypatch.setattr(StateRootLease, "remove_private_file", original_remove)
    monkeypatch.setattr(StateRootLease, "listdir", original_listdir)
    third = GenericAdapter(scenario.repository, scenario.state_paths).doctor()
    assert third.status == "not-installed"


@pytest.mark.parametrize(
    "fault,state,expected_first,expected_second",
    (
        (
            "file-unlink",
            "initial-partial-stage",
            (
                "candidate/SKILL.md",
                "bound/SKILL.md",
                "candidate/generation.json",
                "bound/generation.json",
            ),
            ("bound/generation.json", "candidate", "transaction"),
        ),
        (
            "directory-removal",
            "initial-empty-stage",
            ("candidate",),
            ("transaction",),
        ),
    ),
)
def test_ownership_journal_review_round1_records_post_commit_cleanup_errors(
    fault: str,
    state: str,
    expected_first: tuple[str, ...],
    expected_second: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A committed unlink/rmdir is reported even when its API raises afterward."""
    scenario = _materialize_journal_state(tmp_path, state)
    layout = generic_layout_paths(scenario.state_paths)
    candidate = layout.staging / scenario.candidate_id
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]
    bound_skill = _journal_cleanup_bound_path(
        candidate, Path("SKILL.md"), transaction_id
    )
    bound_manifest = _journal_cleanup_bound_path(
        candidate, Path("generation.json"), transaction_id
    )
    original_remove = StateRootLease.remove_private_file
    original_rmdir = StateRootLease.rmdir
    injected = False

    def remove_then_fail(lease, relative, *, expected_identity, expected_bytes):
        nonlocal injected
        relative = Path(relative)
        original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )
        if (
            fault == "file-unlink"
            and relative == bound_skill.relative_to(scenario.state_paths.root)
            and not injected
        ):
            injected = True
            raise OSError("injected post-commit file unlink failure")

    def rmdir_then_fail(lease, relative, *, missing_ok=False):
        nonlocal injected
        relative = Path(relative)
        original_rmdir(lease, relative, missing_ok=missing_ok)
        if (
            fault == "directory-removal"
            and relative.name == scenario.candidate_id
            and not injected
        ):
            injected = True
            raise OSError("injected post-commit directory removal failure")

    monkeypatch.setattr(StateRootLease, "remove_private_file", remove_then_fail)
    monkeypatch.setattr(StateRootLease, "rmdir", rmdir_then_fail)

    first = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    aliases = {
        "candidate": candidate,
        "candidate/SKILL.md": candidate / "SKILL.md",
        "candidate/generation.json": candidate / "generation.json",
        "bound/SKILL.md": bound_skill,
        "bound/generation.json": bound_manifest,
        "transaction": layout.transaction,
    }
    assert first.status == "degraded"
    assert first.changed_paths == tuple(aliases[name] for name in expected_first)
    assert layout.transaction.read_bytes() == journal_bytes

    second = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert second.status == "not-installed"
    assert second.changed_paths == tuple(aliases[name] for name in expected_second)
    assert not layout.transaction.exists()
    assert not candidate.exists()


@pytest.mark.parametrize("repeated", (False, True), ids=("fail-once", "repeated"))
@pytest.mark.parametrize(
    "fault",
    (
        "file-unlink",
        "directory-removal",
        "transaction-unlink",
        "staging-parent-fsync",
        "generations-parent-fsync",
        "generic-parent-fsync",
    ),
)
def test_ownership_journal_cleanup_retry_preserves_exact_journal_bytes(
    fault: str,
    repeated: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch cleanup faults losing the durable retry anchor."""
    state = (
        "initial-partial-stage"
        if fault == "file-unlink"
        else "initial-empty-stage"
        if fault == "directory-removal"
        else "initial-journal-only"
    )
    scenario = _materialize_journal_state(tmp_path, state)
    layout = generic_layout_paths(scenario.state_paths)
    success_expected = _expected_initial_cleanup_paths(
        layout, scenario.candidate_id, state
    )
    journal_bytes = layout.transaction.read_bytes()
    transaction_id = json.loads(journal_bytes)["transaction_id"]
    candidate = layout.staging / scenario.candidate_id
    bound_skill = _journal_cleanup_bound_path(
        candidate, Path("SKILL.md"), transaction_id
    )
    bound_manifest = _journal_cleanup_bound_path(
        candidate, Path("generation.json"), transaction_id
    )
    binding_expected = (
        candidate / "SKILL.md",
        bound_skill,
        candidate / "generation.json",
        bound_manifest,
    )
    retry_success_expected = (
        bound_skill,
        bound_manifest,
        candidate,
        layout.transaction,
    )
    original_unlink = StateRootLease.unlink
    original_remove = StateRootLease.remove_private_file
    original_rmdir = StateRootLease.rmdir
    original_fsync = StateRootLease.fsync_directory
    failures = 0

    def should_fail() -> bool:
        nonlocal failures
        if repeated or failures == 0:
            failures += 1
            return True
        return False

    def fail_unlink(lease, relative, *, missing_ok=False):
        relative = Path(relative)
        selected = fault == "transaction-unlink" and relative == Path(
            "adapters/generic/transaction.json"
        )
        if selected and should_fail():
            original_unlink(lease, relative, missing_ok=missing_ok)
            raise OSError(f"injected {fault}")
        return original_unlink(lease, relative, missing_ok=missing_ok)

    def fail_remove(lease, relative, *, expected_identity, expected_bytes):
        relative = Path(relative)
        if (
            fault == "file-unlink"
            and relative == bound_skill.relative_to(scenario.state_paths.root)
            and should_fail()
        ):
            raise OSError(f"injected {fault}")
        return original_remove(
            lease,
            relative,
            expected_identity=expected_identity,
            expected_bytes=expected_bytes,
        )

    def fail_rmdir(lease, relative, *, missing_ok=False):
        relative = Path(relative)
        if (
            fault == "directory-removal"
            and relative.name == scenario.candidate_id
            and should_fail()
        ):
            raise OSError(f"injected {fault}")
        return original_rmdir(lease, relative, missing_ok=missing_ok)

    fsync_targets = {
        "staging-parent-fsync": Path("adapters/generic/staging"),
        "generations-parent-fsync": Path("adapters/generic/generations"),
        "generic-parent-fsync": Path("adapters/generic"),
    }

    def fail_fsync(lease, relative=Path(".")):
        relative = Path(relative)
        if (
            fault in fsync_targets
            and relative == fsync_targets[fault]
            and should_fail()
        ):
            raise OSError(f"injected {fault}")
        return original_fsync(lease, relative)

    monkeypatch.setattr(StateRootLease, "unlink", fail_unlink)
    monkeypatch.setattr(StateRootLease, "remove_private_file", fail_remove)
    monkeypatch.setattr(StateRootLease, "rmdir", fail_rmdir)
    monkeypatch.setattr(StateRootLease, "fsync_directory", fail_fsync)

    first = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    assert first.status == "degraded"
    assert layout.transaction.read_bytes() == journal_bytes
    first_expected = (
        binding_expected
        if fault == "file-unlink"
        else
        (layout.transaction,)
        if fault in {"transaction-unlink", "generic-parent-fsync"}
        else ()
    )
    assert first.changed_paths == first_expected

    second = GenericAdapter(scenario.repository, scenario.state_paths).doctor()

    if repeated:
        assert second.status == "degraded"
        assert layout.transaction.read_bytes() == journal_bytes
        assert second.changed_paths == (
            () if fault == "file-unlink" else first_expected
        )
    else:
        assert second.status == "not-installed"
        assert not layout.transaction.exists()
        assert not (layout.staging / scenario.candidate_id).exists()
        assert not (layout.generations / scenario.candidate_id).exists()
        assert second.changed_paths == (
            retry_success_expected
            if fault == "file-unlink"
            else success_expected
        )


@pytest.mark.parametrize(
    "conflict",
    (
        "status-digest",
        "baseline-mismatch",
        "operation",
        "terminal-operation",
        "selected-root",
        "transaction",
        "capsule",
        "generation-reference",
        "staging-manifest",
        "final-manifest",
        "both-names",
        "extra-direct-entry",
        "alias-entry",
        "unknown-format",
        "old-marker",
        "oversized",
        "directory-identity",
    ),
)
def test_ownership_journal_conflict_preserves_all_observed_objects(
    conflict: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Catch a digest, reference, tree, or identity conflict being adopted."""
    later = {
        "status-digest",
        "operation",
        "terminal-operation",
        "transaction",
        "capsule",
        "generation-reference",
    }
    state = (
        "matching-terminal-after"
        if conflict == "terminal-operation"
        else "later-before"
        if conflict in later
        else "initial-final"
        if conflict in {"final-manifest", "both-names"}
        else "initial-partial-stage"
        if conflict in {"staging-manifest", "extra-direct-entry", "alias-entry"}
        else "initial-empty-stage"
        if conflict == "directory-identity"
        else "initial-journal-only"
    )
    scenario = _materialize_journal_state(tmp_path, state)
    layout = generic_layout_paths(scenario.state_paths)
    if conflict == "status-digest":
        payload = json.loads(layout.status.read_bytes())
        payload["capability"] = "implicit"
        layout.status.write_bytes(canonical_json_bytes(payload))
    elif conflict in {
        "operation",
        "terminal-operation",
        "baseline-mismatch",
        "selected-root",
        "transaction",
        "capsule",
        "generation-reference",
        "unknown-format",
    }:
        payload = json.loads(layout.transaction.read_bytes())
        if conflict == "baseline-mismatch":
            payload["operation"] = "upgrade"
            payload["baseline_status_digest"] = "8" * 64
            payload["status_transition"]["before_digest"] = "8" * 64
        elif conflict == "terminal-operation":
            payload["operation"] = "upgrade"
            payload["baseline_status_digest"] = "8" * 64
        elif conflict == "operation":
            payload["operation"] = "upgrade"
            payload["baseline_status_digest"] = payload["status_transition"][
                "before_digest"
            ]
        elif conflict == "selected-root":
            alternate = tmp_path / "alternate-skills"
            alternate.mkdir()
            payload["selected_skill_root"] = str(alternate)
        elif conflict == "transaction":
            payload["transaction_id"] = f"t-{'8' * 32}"
        elif conflict == "capsule":
            payload["capsule"]["package_hash"] = "8" * 64
        elif conflict == "generation-reference":
            package_hash = payload["candidate"]["package_hash"]
            payload["candidate"]["generation_id"] = (
                f"g-{package_hash}-{'8' * 32}"
            )
        else:
            payload["format"] = 2
        layout.transaction.write_bytes(canonical_json_bytes(payload))
    elif conflict == "staging-manifest":
        (layout.staging / scenario.candidate_id / "generation.json").write_bytes(
            b"{}"
        )
    elif conflict == "final-manifest":
        (
            layout.generations / scenario.candidate_id / "generation.json"
        ).write_bytes(b"{}")
    elif conflict == "both-names":
        (layout.staging / scenario.candidate_id).mkdir()
    elif conflict == "extra-direct-entry":
        (layout.staging / scenario.candidate_id / "unexpected.bin").write_bytes(
            b"preserve"
        )
    elif conflict == "alias-entry":
        external = tmp_path / "external-alias-target"
        external.write_bytes(b"outside journal ownership")
        alias = layout.staging / scenario.candidate_id / "alias-entry"
        try:
            alias.symlink_to(external)
        except OSError as exc:
            pytest.skip(f"native file aliases are unavailable: {exc}")
    elif conflict == "old-marker":
        layout.transaction.write_bytes(
            canonical_json_bytes(
                {"before_digest": None, "after_digest": "8" * 64}
            )
        )
    elif conflict == "oversized":
        layout.transaction.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    else:
        original = GenericAdapter._preflight_journal_cleanup

        def replace_after_preflight(adapter, relative, journal):
            tree = original(adapter, relative, journal)
            candidate = adapter.state_paths.root / relative
            displaced = candidate.parent / "displaced-owned-candidate"
            candidate.rename(displaced)
            candidate.mkdir()
            (candidate / "keep.txt").write_bytes(b"replacement")
            displaced.rmdir()
            return tree

        monkeypatch.setattr(
            GenericAdapter,
            "_preflight_journal_cleanup",
            replace_after_preflight,
        )

    before = _file_snapshot(scenario.state_paths.root, scenario.skill_root)
    journal_bytes = layout.transaction.read_bytes()

    result = GenericAdapter(
        scenario.repository, scenario.state_paths
    ).install(InstallOptions(output_dir=scenario.skill_root))

    assert result.status == "failed"
    assert layout.transaction.read_bytes() == journal_bytes
    if conflict == "directory-identity":
        replacement = layout.staging / scenario.candidate_id / "keep.txt"
        assert replacement.read_bytes() == b"replacement"
    else:
        assert _file_snapshot(
            scenario.state_paths.root, scenario.skill_root
        ) == before
    assert result.changed_paths == ()


@pytest.mark.parametrize("operation", ("first-install", "upgrade"))
def test_preterminal_smoke_uses_only_an_isolated_terminal_status(
    operation: str,
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    expected_status = "installed"
    adapter = generic_adapter
    if operation == "upgrade":
        assert generic_adapter.install(
            InstallOptions(output_dir=skill_root)
        ).status == "installed"
        adapter = GenericAdapter(repository_v2, generic_adapter.state_paths)
        expected_status = "upgraded"
    original_run = adapter._run_capsule
    public_nonterminal_smoke = False

    def observe_run(capsule: Path, state: Path, working: Path):
        nonlocal public_nonterminal_smoke
        if state == adapter.state_paths.root:
            layout = generic_layout_paths(adapter.state_paths)
            status = validate_status_v5(
                layout.status.read_bytes(),
                skill_root=skill_root,
                generations_root=layout.generations,
            )
            public_nonterminal_smoke = status.transaction_phase is not None
        return original_run(capsule, state, working)

    monkeypatch.setattr(adapter, "_run_capsule", observe_run)

    result = adapter.install(InstallOptions(output_dir=skill_root))

    assert result.status == expected_status
    assert not public_nonterminal_smoke


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


def test_upgrade_switches_complete_generation_and_keeps_previous(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    first = generic_adapter.install(InstallOptions(output_dir=skill_root))
    assert first.status == "installed"
    capsule = skill_root / "voice-intent-normalizer"
    capsule_before = _capsule_bytes(capsule)
    layout = generic_layout_paths(generic_adapter.state_paths)
    before = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )

    upgraded_adapter = GenericAdapter(repository_v2, generic_adapter.state_paths)
    result = upgraded_adapter.install(InstallOptions(output_dir=skill_root))
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )

    assert result.status == "upgraded"
    assert status.active.package_version == "0.2.0"
    assert status.previous is not None
    assert status.previous.package_version == "0.1.0"
    assert status.previous.generation_id == before.active.generation_id
    assert status.transaction_id is None
    assert (layout.generations / status.active.generation_id).is_dir()
    assert (layout.generations / status.previous.generation_id).is_dir()
    assert _capsule_bytes(capsule) == capsule_before


def test_status_generation_roots_are_never_derived_from_staging(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    assert upgraded.install(InstallOptions(output_dir=skill_root)).status == "upgraded"
    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )

    assert status.active_root == layout.generations / status.active.generation_id
    assert status.previous is not None
    assert status.previous_root == (
        layout.generations / status.previous.generation_id
    )
    assert layout.staging not in status.active_root.parents
    assert layout.staging not in status.previous_root.parents


def test_failed_upgrade_activation_leaves_previous_generation_selected(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    before = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    upgraded_adapter = GenericAdapter(repository_v2, generic_adapter.state_paths)
    original_write = upgraded_adapter._write_status_payload

    def fail_terminal_activation(payload: dict[str, object]) -> None:
        active = payload.get("active")
        if (
            payload.get("transaction") is None
            and isinstance(active, dict)
            and active.get("package_version") == "0.2.0"
        ):
            raise OSError("injected activation failure")
        original_write(payload)

    monkeypatch.setattr(
        upgraded_adapter, "_write_status_payload", fail_terminal_activation
    )
    result = upgraded_adapter.install(InstallOptions(output_dir=skill_root))
    interrupted = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )

    assert result.status == "failed"
    assert interrupted.active.generation_id == before.active.generation_id
    assert interrupted.active.package_version == "0.1.0"
    assert interrupted.previous is not None
    assert interrupted.previous.package_version == "0.2.0"
    assert interrupted.transaction_phase == "activation-pending"


def test_failed_upgrade_smoke_leaves_status_and_generations_unchanged(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    status_before = layout.status.read_bytes()
    generations_before = tuple(layout.generations.iterdir())
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    smoke_attempted = False

    def fail_smoke(capsule, generation) -> None:
        nonlocal smoke_attempted
        assert capsule.kind == "capsule"
        assert generation.package_version == "0.2.0"
        smoke_attempted = True
        raise ValueError("injected candidate smoke failure")

    monkeypatch.setattr(upgraded, "_smoke_generation", fail_smoke)

    result = upgraded.install(InstallOptions(output_dir=skill_root))

    assert smoke_attempted
    assert result.status == "failed"
    assert layout.status.read_bytes() == status_before
    assert tuple(layout.generations.iterdir()) == generations_before


def test_doctor_validates_active_previous_and_capsule_anchors(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded_adapter = GenericAdapter(repository_v2, generic_adapter.state_paths)
    assert upgraded_adapter.install(
        InstallOptions(output_dir=skill_root)
    ).status == "upgraded"
    assert upgraded_adapter.doctor().status == "installed"

    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert status.previous is not None
    previous_file = layout.generations / status.previous.generation_id / "LICENSE"
    previous_file.write_bytes(b"tampered previous generation")

    result = upgraded_adapter.doctor()
    after = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert result.status == "degraded"
    assert after.active.generation_id == status.active.generation_id


def test_fresh_installer_doctor_uses_protected_selected_skill_root(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    repository = Path(__file__).resolve().parents[1]
    fresh = Installer(
        {"generic": GenericAdapter(repository, generic_adapter.state_paths)}
    )

    result = fresh.doctor(("generic",))[0]

    assert result.status == "installed"
    assert "shared state: available" in result.messages


def test_cli_doctor_uses_protected_root_with_a_fresh_installer(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    from io import StringIO

    from voice_intent_normalizer import cli

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    fresh = cli.default_installer(generic_adapter.state_paths)
    stdout = StringIO()

    code = cli.main(
        ["doctor", "--platform", "generic", "--json"],
        service=object(),
        installer=fresh,
        stdout=stdout,
    )

    assert code == 0
    assert json.loads(stdout.getvalue())[0]["status"] == "installed"


@pytest.mark.parametrize(
    "corruption",
    ("tampered", "relative", "unc-network", "mapped-drive", "ads", "mismatch"),
)
def test_fresh_doctor_rejects_unsafe_selected_roots_without_mutation(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    status_path = generic_layout_paths(generic_adapter.state_paths).status
    payload = json.loads(status_path.read_bytes())
    if corruption == "tampered":
        selected: object = 7
    elif corruption == "relative":
        selected = "relative/skills"
    elif corruption == "unc-network":
        selected = r"\\server\share\skills"
    elif corruption == "mapped-drive":
        selected = r"Z:\mapped\skills"
        if os.name == "nt":
            original_drive_type = paths_module._windows_drive_type
            monkeypatch.setattr(
                paths_module,
                "_windows_drive_type",
                lambda root: 4
                if str(root).casefold().startswith("z:")
                else original_drive_type(root),
            )
    elif corruption == "ads":
        selected = f"{skill_root}:stream"
    else:
        redirected = tmp_path / "other-skills"
        redirected.mkdir()
        selected = str(redirected)
    payload["selected_skill_root"] = selected
    tampered = canonical_json_bytes(payload)
    status_path.write_bytes(tampered)
    repository = Path(__file__).resolve().parents[1]
    fresh = Installer(
        {"generic": GenericAdapter(repository, generic_adapter.state_paths)}
    )

    result = fresh.doctor(("generic",))[0]

    assert result.status == "degraded"
    assert status_path.read_bytes() == tampered
    assert (skill_root / "voice-intent-normalizer").is_dir()


def test_fresh_doctor_rejects_selected_root_alias_without_mutation(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    alias = tmp_path / "skills-alias"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(skill_root)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr
    else:
        alias.symlink_to(skill_root, target_is_directory=True)
    status_path = generic_layout_paths(generic_adapter.state_paths).status
    payload = json.loads(status_path.read_bytes())
    payload["selected_skill_root"] = str(alias)
    tampered = canonical_json_bytes(payload)
    status_path.write_bytes(tampered)
    repository = Path(__file__).resolve().parents[1]
    fresh = Installer(
        {"generic": GenericAdapter(repository, generic_adapter.state_paths)}
    )

    try:
        result = fresh.doctor(("generic",))[0]
    finally:
        if os.name == "nt":
            os.rmdir(alias)
        else:
            alias.unlink()

    assert result.status == "degraded"
    assert status_path.read_bytes() == tampered
    assert (skill_root / "voice-intent-normalizer").is_dir()


def test_public_doctor_stays_degraded_when_capsule_reports_degraded_shared_state(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    repository = Path(__file__).resolve().parents[1]
    fresh_adapter = GenericAdapter(repository, generic_adapter.state_paths)
    diagnostic = {
        "status": "degraded",
        "state_root": str(generic_adapter.state_paths.root),
        "diagnostics": ["state_unavailable"],
    }
    monkeypatch.setattr(
        fresh_adapter,
        "_run_capsule",
        lambda _capsule, _state, _working: subprocess.CompletedProcess(
            args=("capsule", "doctor", "--json"),
            returncode=0,
            stdout=json.dumps(diagnostic),
            stderr="",
        ),
    )

    result = Installer({"generic": fresh_adapter}).doctor(("generic",))[0]

    assert result.status == "degraded"
    assert "shared state: available" not in result.messages


def test_public_doctor_requires_complete_bootstrap_diagnostic_protocol(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    repository = Path(__file__).resolve().parents[1]
    fresh_adapter = GenericAdapter(repository, generic_adapter.state_paths)
    monkeypatch.setattr(
        fresh_adapter,
        "_run_capsule",
        lambda _capsule, _state, _working: subprocess.CompletedProcess(
            args=("capsule", "doctor", "--json"),
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "state_root": str(tmp_path / "wrong-state"),
                    "diagnostics": [],
                }
            ),
            stderr="",
        ),
    )

    result = Installer({"generic": fresh_adapter}).doctor(("generic",))[0]

    assert result.status == "degraded"
    assert "shared state: available" not in result.messages


@pytest.mark.parametrize(
    "bad_status",
    (
        b'{"format":5,"format":5}',
        b'{"format":6}',
        b"not-json",
    ),
)
def test_doctor_rejects_malformed_duplicate_or_future_status_without_reanchoring(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    bad_status: bytes,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    layout.status.write_bytes(bad_status)

    result = generic_adapter.doctor()

    assert result.status == "degraded"
    assert layout.status.read_bytes() == bad_status


def test_doctor_reports_missing_status_without_reanchoring_capsule(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    capsule_before = _capsule_bytes(skill_root / "voice-intent-normalizer")
    layout.status.unlink()

    result = generic_adapter.doctor()

    assert result.status == "degraded"
    assert not layout.status.exists()
    assert _capsule_bytes(skill_root / "voice-intent-normalizer") == capsule_before


@pytest.mark.parametrize(
    "bad_manifest",
    (b'{"format":1,"format":1}', b'{"format":2}', b"not-json"),
)
def test_doctor_rejects_malformed_duplicate_or_future_generation_manifest(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    bad_manifest: bytes,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    status_before = layout.status.read_bytes()
    status = validate_status_v5(
        status_before,
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    manifest = layout.generations / status.active.generation_id / "generation.json"
    manifest.write_bytes(bad_manifest)

    result = generic_adapter.doctor()

    assert result.status == "degraded"
    assert layout.status.read_bytes() == status_before
    assert manifest.read_bytes() == bad_manifest


def test_doctor_rejects_unanchored_generation_reference_without_searching_names(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    layout = generic_layout_paths(generic_adapter.state_paths)
    payload = json.loads(layout.status.read_bytes())
    active = payload["active"]
    assert isinstance(active, dict)
    active["generation_id"] = (
        f"g-{active['package_hash']}-{'f' * 32}"
    )
    unanchored = canonical_json_bytes(payload)
    layout.status.write_bytes(unanchored)

    result = generic_adapter.doctor()

    assert result.status == "degraded"
    assert layout.status.read_bytes() == unanchored


def test_uninstall_removes_only_anchored_private_state_and_preserves_shared_data(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded_adapter = GenericAdapter(repository_v2, generic_adapter.state_paths)
    assert upgraded_adapter.install(
        InstallOptions(output_dir=skill_root)
    ).status == "upgraded"
    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert status.previous is not None
    anchored = {status.active.generation_id, status.previous.generation_id}
    unrelated_generation = layout.generations / "unrelated-adapter-state"
    unrelated_generation.mkdir()
    (unrelated_generation / "owned-elsewhere").write_text("keep", encoding="utf-8")
    shared = generic_adapter.state_paths.personal_file
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text('{"alias":"keep"}\n', encoding="utf-8")
    other_status = generic_adapter.state_paths.root / "adapters" / "other.json"
    other_status.write_text('{"owner":"other"}', encoding="utf-8")

    result = upgraded_adapter.uninstall(UninstallOptions(output_dir=skill_root))

    assert result.status == "uninstalled"
    assert upgraded_adapter.doctor().status == "not-installed"
    assert not (skill_root / "voice-intent-normalizer").exists()
    assert not layout.status.exists()
    assert all(
        not (layout.generations / generation_id).exists()
        for generation_id in anchored
    )
    assert (unrelated_generation / "owned-elsewhere").read_text(
        encoding="utf-8"
    ) == "keep"
    assert shared.read_text(encoding="utf-8") == '{"alias":"keep"}\n'
    assert other_status.read_text(encoding="utf-8") == '{"owner":"other"}'


def test_uninstall_refuses_unknown_capsule_entry_without_deleting_it(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    capsule = skill_root / "voice-intent-normalizer"
    unknown = capsule / "owned-by-someone-else.txt"
    unknown.write_text("preserve", encoding="utf-8")

    result = generic_adapter.uninstall(UninstallOptions(output_dir=skill_root))

    assert result.status in {"failed", "degraded"}
    assert unknown.read_text(encoding="utf-8") == "preserve"
    assert capsule.is_dir()


def test_capsule_identity_replacement_during_retirement_is_never_deleted(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    original_publish = StateRootLease.publish_directory_no_replace
    saved_original = skill_root / "saved-original-capsule"
    replacement_bytes = b"third-party replacement"
    injected = False

    def replace_tombstone_after_commit(
        lease,
        source,
        destination,
        expected_identity,
        *,
        on_committed=None,
    ):
        nonlocal injected
        if Path(source) != Path("voice-intent-normalizer"):
            return original_publish(
                lease,
                source,
                destination,
                expected_identity,
                on_committed=on_committed,
            )

        def replace_published_identity() -> None:
            nonlocal injected
            if on_committed is not None:
                on_committed()
            tombstone = skill_root / Path(destination)
            tombstone.rename(saved_original)
            tombstone.mkdir()
            (tombstone / "replacement.txt").write_bytes(replacement_bytes)
            injected = True

        return original_publish(
            lease,
            source,
            destination,
            expected_identity,
            on_committed=replace_published_identity,
        )

    monkeypatch.setattr(
        StateRootLease,
        "publish_directory_no_replace",
        replace_tombstone_after_commit,
    )

    result = generic_adapter.uninstall(UninstallOptions(output_dir=skill_root))

    assert injected
    assert result.status in {"failed", "degraded"}
    candidates = tuple(skill_root.glob(".voice-intent-normalizer.retired-*")) + (
        skill_root / "voice-intent-normalizer",
    )
    replacement = next(
        candidate / "replacement.txt"
        for candidate in candidates
        if (candidate / "replacement.txt").is_file()
    )
    assert replacement.read_bytes() == replacement_bytes
    assert (saved_original / "capsule.json").is_file()


@pytest.mark.parametrize(
    "phase",
    ("generation-published", "capsule-published", "activation-pending"),
)
def test_first_install_recovery_is_idempotent_after_each_activation_phase(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    original = generic_adapter._advance_transaction_status
    interrupted = False

    def interrupt_after_phase(
        root: Path,
        *,
        journal,
        before_payload,
        after_payload,
        terminal=False,
    ):
        nonlocal interrupted
        result = original(
            root,
            journal=journal,
            before_payload=before_payload,
            after_payload=after_payload,
            terminal=terminal,
        )
        transaction = after_payload.get("transaction")
        if (
            not interrupted
            and isinstance(transaction, dict)
            and transaction.get("phase") == phase
        ):
            interrupted = True
            raise OSError(f"injected interruption after {phase}")
        return result

    monkeypatch.setattr(
        generic_adapter, "_advance_transaction_status", interrupt_after_phase
    )
    failed = generic_adapter.install(InstallOptions(output_dir=skill_root))
    monkeypatch.setattr(
        generic_adapter, "_advance_transaction_status", original
    )

    assert interrupted
    assert failed.status == "failed"
    recovered = generic_adapter.install(InstallOptions(output_dir=skill_root))
    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert recovered.status in {"installed", "already-installed"}
    assert status.active.package_version == "0.1.0"
    assert status.transaction_id is None
    assert generic_adapter.doctor().status == "installed"


@pytest.mark.parametrize(
    "phase", ("generation-published", "activation-pending")
)
def test_upgrade_recovery_completes_only_a_fully_anchored_candidate(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    original = upgraded._advance_transaction_status
    interrupted = False

    def interrupt_after_phase(
        root: Path,
        *,
        journal,
        before_payload,
        after_payload,
        terminal=False,
    ):
        nonlocal interrupted
        result = original(
            root,
            journal=journal,
            before_payload=before_payload,
            after_payload=after_payload,
            terminal=terminal,
        )
        transaction = after_payload.get("transaction")
        if (
            not interrupted
            and isinstance(transaction, dict)
            and transaction.get("phase") == phase
        ):
            interrupted = True
            raise OSError(f"injected interruption after {phase}")
        return result

    monkeypatch.setattr(upgraded, "_advance_transaction_status", interrupt_after_phase)
    assert upgraded.install(InstallOptions(output_dir=skill_root)).status == "failed"
    monkeypatch.setattr(upgraded, "_advance_transaction_status", original)

    assert interrupted
    assert upgraded.doctor().status == "installed"
    layout = generic_layout_paths(generic_adapter.state_paths)
    status = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert status.active.package_version == "0.2.0"
    assert status.previous is not None
    assert status.previous.package_version == "0.1.0"
    assert status.transaction_id is None


def test_recovery_preserves_a_tampered_journal_candidate_as_a_conflict(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    original = upgraded._advance_transaction_status

    def stop_activation(
        root: Path,
        *,
        journal,
        before_payload,
        after_payload,
        terminal=False,
    ):
        result = original(
            root,
            journal=journal,
            before_payload=before_payload,
            after_payload=after_payload,
            terminal=terminal,
        )
        transaction = after_payload.get("transaction")
        if isinstance(transaction, dict) and transaction.get("phase") == (
            "activation-pending"
        ):
            raise OSError("injected activation interruption")
        return result

    monkeypatch.setattr(upgraded, "_advance_transaction_status", stop_activation)
    assert upgraded.install(InstallOptions(output_dir=skill_root)).status == "failed"
    layout = generic_layout_paths(generic_adapter.state_paths)
    pending = validate_status_v5(
        layout.status.read_bytes(),
        skill_root=skill_root,
        generations_root=layout.generations,
    )
    assert pending.previous is not None
    candidate_file = (
        layout.generations / pending.previous.generation_id / "LICENSE"
    )
    candidate_file.write_bytes(b"tampered candidate")
    rollback_seen = False

    def stop_rollback(
        root: Path,
        *,
        journal,
        before_payload,
        after_payload,
        terminal=False,
    ):
        nonlocal rollback_seen
        result = original(
            root,
            journal=journal,
            before_payload=before_payload,
            after_payload=after_payload,
            terminal=terminal,
        )
        transaction = after_payload.get("transaction")
        if isinstance(transaction, dict) and transaction.get("phase") == (
            "rollback-pending"
        ):
            rollback_seen = True
            raise OSError("injected rollback interruption")
        return result

    monkeypatch.setattr(upgraded, "_advance_transaction_status", stop_rollback)
    assert upgraded.doctor().status == "degraded"
    monkeypatch.setattr(upgraded, "_advance_transaction_status", original)
    assert rollback_seen
    status_before = layout.status.read_bytes()
    journal_before = layout.transaction.read_bytes()
    assert upgraded.doctor().status == "degraded"
    assert layout.status.read_bytes() == status_before
    assert layout.transaction.read_bytes() == journal_before
    assert candidate_file.read_bytes() == b"tampered candidate"


@pytest.mark.parametrize(
    "phase", ("deactivation-pending", "capsule-retired", "cleanup-pending")
)
def test_uninstall_recovery_completes_each_deactivation_phase(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    original = generic_adapter._write_status_payload
    interrupted = False

    def interrupt_after_phase(payload: dict[str, object]) -> None:
        nonlocal interrupted
        original(payload)
        transaction = payload.get("transaction")
        if (
            not interrupted
            and isinstance(transaction, dict)
            and transaction.get("phase") == phase
        ):
            interrupted = True
            raise OSError(f"injected interruption after {phase}")

    monkeypatch.setattr(
        generic_adapter, "_write_status_payload", interrupt_after_phase
    )
    first = generic_adapter.uninstall(UninstallOptions(output_dir=skill_root))
    monkeypatch.setattr(
        generic_adapter, "_write_status_payload", original
    )

    assert interrupted
    assert first.status == "degraded"
    retry = generic_adapter.uninstall(UninstallOptions(output_dir=skill_root))
    assert retry.status in {"uninstalled", "not-installed"}
    assert generic_adapter.doctor().status == "not-installed"
    assert not (skill_root / "voice-intent-normalizer").exists()


def test_uninstall_recovery_finishes_generation_cleanup_and_status_removal(
    generic_adapter: GenericAdapter,
    repository_v2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    upgraded = GenericAdapter(repository_v2, generic_adapter.state_paths)
    assert upgraded.install(InstallOptions(output_dir=skill_root)).status == "upgraded"
    original_retire = upgraded._retire_generation
    retired_once = False

    def stop_after_one_generation(reference, transaction_id):
        nonlocal retired_once
        original_retire(reference, transaction_id)
        if not retired_once:
            retired_once = True
            raise OSError("injected generation cleanup interruption")

    monkeypatch.setattr(upgraded, "_retire_generation", stop_after_one_generation)
    assert upgraded.uninstall(
        UninstallOptions(output_dir=skill_root)
    ).status == "degraded"
    monkeypatch.setattr(upgraded, "_retire_generation", original_retire)
    original_remove = upgraded._remove_status
    removed = False

    def stop_after_status_removal() -> None:
        nonlocal removed
        original_remove()
        removed = True
        raise OSError("injected status removal interruption")

    monkeypatch.setattr(upgraded, "_remove_status", stop_after_status_removal)
    assert upgraded.uninstall(
        UninstallOptions(output_dir=skill_root)
    ).status == "degraded"
    monkeypatch.setattr(upgraded, "_remove_status", original_remove)
    assert retired_once and removed
    assert upgraded.uninstall(
        UninstallOptions(output_dir=skill_root)
    ).status == "not-installed"
    assert upgraded.doctor().status == "not-installed"


def test_doctor_rejects_invalid_ownership_journal_without_mutating_it(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    assert generic_adapter.install(InstallOptions(output_dir=skill_root)).status == (
        "installed"
    )
    journal = generic_layout_paths(generic_adapter.state_paths).transaction
    bad_journal = b'{"format":2}'
    journal.write_bytes(bad_journal)

    result = generic_adapter.doctor()

    assert result.status == "degraded"
    assert journal.read_bytes() == bad_journal


def test_ownership_journal_native_first_install_completes_without_masking(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
):
    """Exercise the native first-install path without public error masking."""
    skill_root = tmp_path / "native-skills"
    skill_root.mkdir()

    generic_adapter._reset_operation()
    with generic_adapter._state_operation(create=True):
        result = generic_adapter._install_locked(
            InstallOptions(output_dir=skill_root)
        )

    assert result.status == "installed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX temporary-root alias")
def test_generation_smoke_canonicalizes_temporary_root_alias(
    generic_adapter: GenericAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Never serialize a system temporary-directory alias into smoke status."""
    from contextlib import contextmanager

    real = tmp_path / "real-smoke"
    real.mkdir()
    alias = tmp_path / "alias-smoke"
    alias.symlink_to(real, target_is_directory=True)
    artifacts = generic_adapter._prepare_versioned_artifacts("8" * 32)
    observed: list[Path] = []

    @contextmanager
    def aliased_temporary_directory(*_args, **_kwargs):
        yield os.fspath(alias)

    def successful_capsule(_capsule, state, _working):
        observed.append(state)
        return subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "state_root": os.fspath(state),
                    "diagnostics": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(tempfile, "TemporaryDirectory", aliased_temporary_directory)
    monkeypatch.setattr(generic_adapter, "_run_capsule", successful_capsule)

    generic_adapter._smoke_generation(artifacts.capsule, artifacts.generation)

    assert observed == [real.resolve(strict=True) / "state"]


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


def test_generic_preserves_journal_owned_package_when_status_recording_fails(
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
    assert not (root / "voice-intent-normalizer").exists()
    generations = tuple(layout.generations.glob("g-*"))
    assert len(generations) == 1
    journal = validate_ownership_journal(
        layout.transaction.read_bytes(),
        skill_root=root,
        generations_root=layout.generations,
    )
    assert journal.generation_root == generations[0]
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
    assert not tuple(root.glob(".voice-intent-normalizer.capsule-*"))
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
            ".superpowers",
            "__pycache__",
            "*.pyc",
            "dist",
            "build",
        ),
    )


@pytest.mark.parametrize("corruption", ["skill", "bootstrap"])
def test_generic_rejects_invalid_prepared_package_without_final_runtime_names(
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
    assert not capsule.exists()
    assert not generations
    assert not layout.transaction.exists()
    assert not layout.status.exists()
    assert not tuple(layout.staging.glob("*"))
    assert not tuple(root.glob(".voice-intent-normalizer.capsule-*"))


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
                    ".voice-intent-normalizer.capsule-"
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
        staging = next(root.glob(".voice-intent-normalizer.capsule-*"))
        assert result.status == "failed"
        assert (staging / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert {path.name for path in staging.iterdir()} == {"keep.txt"}
        assert any(displaced.iterdir())
        assert not (displaced / "keep.txt").exists()
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
        source_was_removed = lease.move_no_replace(
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

    assert (source / "moved.txt").exists() is (not source_was_removed)
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
    assert result.capability is CapabilityLevel.UNAVAILABLE


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

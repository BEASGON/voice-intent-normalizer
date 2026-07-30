from __future__ import annotations

import os
import threading
from hashlib import sha256
from pathlib import Path

import pytest

import voice_intent_normalizer.paths as paths_module
from voice_intent_normalizer.paths import StatePaths, guard_state_root


def test_voice_intent_home_overrides_default(tmp_path):
    """Catch ignoring the explicit shared-state location."""
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "custom")},
        home=tmp_path / "home",
    )

    assert paths.root == (tmp_path / "custom").resolve()


def test_blank_voice_intent_home_uses_default_without_creating_it(tmp_path):
    """Catch blank overrides or read-only resolution creating state directories."""
    paths = StatePaths.resolve(environ={"VOICE_INTENT_HOME": "  "}, home=tmp_path)

    assert paths.root == tmp_path / ".voice-intent-normalizer"
    assert not paths.root.exists()


def test_voice_intent_home_rejects_a_symlink_alias_instead_of_resolving_it(tmp_path):
    """Catch configured aliases being silently converted into supported roots."""
    direct_root = tmp_path / "direct-state"
    direct_root.mkdir()
    alias_root = tmp_path / "state-alias"
    try:
        alias_root.symlink_to(direct_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="direct canonical local path required"):
        StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(alias_root)})


def test_existing_regular_file_cannot_be_a_state_root(tmp_path):
    """Catch a non-directory root passing validation and failing later opaquely."""
    root_file = tmp_path / "state-file"
    root_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="direct canonical local path required"):
        StatePaths.resolve(environ={"VOICE_INTENT_HOME": str(root_file)})


@pytest.mark.skipif(os.name != "nt", reason="Windows local-path contract")
@pytest.mark.parametrize(
    "unsupported_root",
    (
        r"\\server\share\state",
        r"\\.\C:\state",
    ),
)
def test_windows_network_and_device_roots_are_rejected(unsupported_root):
    """Catch V1 accepting a network share or Win32 device namespace."""
    with pytest.raises(ValueError, match="direct canonical local path required"):
        paths_module.validate_state_root(unsupported_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical component spelling")
@pytest.mark.parametrize("suffix", ("state.", "state "))
def test_windows_trimmed_component_aliases_are_rejected(tmp_path, suffix):
    """Catch Win32 silently redirecting a noncanonical component spelling."""
    with pytest.raises(ValueError, match="direct canonical local path required"):
        paths_module.validate_state_root(tmp_path / suffix)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive classification")
def test_windows_mapped_drive_root_is_rejected(tmp_path, monkeypatch):
    """Catch a drive-letter spelling bypassing the network-share restriction."""
    monkeypatch.setattr(
        paths_module, "_windows_drive_type", lambda _root: 4, raising=False
    )

    with pytest.raises(ValueError, match="direct canonical local path required"):
        paths_module.validate_state_root(tmp_path / "state")


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical handle paths")
def test_windows_noncanonical_handle_path_is_rejected(tmp_path, monkeypatch):
    """Catch short-name, SUBST, or other non-reparse aliases to a local path."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(
        paths_module,
        "_windows_final_path",
        lambda _path: tmp_path / "different-state",
        raising=False,
    )

    with pytest.raises(ValueError, match="direct canonical local path required"):
        paths_module.validate_state_root(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX path spelling")
def test_posix_double_slash_root_is_rejected():
    """Catch implementation-defined // paths entering the direct-path contract."""
    with pytest.raises(ValueError, match="direct canonical local path required"):
        paths_module.validate_state_root("//tmp/voice-intent-state")


def test_project_ids_are_stable_and_isolated(tmp_path):
    """Catch project state collisions or path-dependent identifiers."""
    paths = StatePaths.resolve(environ={}, home=tmp_path)
    alpha = tmp_path / "alpha"
    first = paths.for_project(alpha)
    second = paths.for_project(tmp_path / "beta")

    assert first.project_id == paths.for_project(alpha).project_id
    assert first.project_id != second.project_id
    expected_id = sha256(str(alpha.resolve()).encode("utf-8")).hexdigest()[:16]
    assert first.project_id == expected_id


def test_state_paths_expose_shared_and_project_files_without_creating_them(tmp_path):
    """Catch a path contract that cannot support adapters or project scanning."""
    paths = StatePaths.resolve(environ={}, home=tmp_path)
    project = paths.for_project(tmp_path / "workspace")

    assert paths.personal_file == paths.root / "personal.jsonl"
    assert paths.preferences_file == paths.root / "preferences.json"
    assert paths.hotwords_file == paths.root / "hotwords" / "zh-ai.jsonl"
    assert paths.adapter_status_file("codex") == paths.root / "adapters" / "codex.json"
    assert project.lexicon_file == (
        paths.root / "projects" / project.project_id / "project.jsonl"
    )
    assert project.scan_state_file == (
        paths.root / "projects" / project.project_id / "scan-state.json"
    )
    assert not paths.root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share semantics")
def test_windows_read_guards_coexist_and_release_every_handle(tmp_path):
    """Catch read guards requesting DELETE access and excluding one another."""
    root = tmp_path / "state"
    root.mkdir()
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def hold_second_guard() -> None:
        try:
            with guard_state_root(root):
                entered.set()
                release.wait(5)
        except BaseException as exc:
            failures.append(exc)

    with guard_state_root(root):
        holder = threading.Thread(target=hold_second_guard)
        holder.start()
        assert entered.wait(3)
        release.set()
        holder.join(5)

    assert not holder.is_alive()
    assert failures == []
    moved = tmp_path / "moved-state"
    root.rename(moved)
    assert moved.is_dir()


def test_create_guard_retains_each_new_component_for_lease_io(tmp_path):
    """Catch create=True returning reopenable names instead of bound directories."""
    root = tmp_path / "state"

    with guard_state_root(
        root,
        create=True,
        retained_dirs=("hotwords", "hotwords/payloads"),
        create_retained=True,
    ) as lease:
        assert lease.root_exists
        assert lease.exists("hotwords")
        assert lease.exists("hotwords/payloads")
        lease.write_bytes_atomic("hotwords/current.json", b"pointer")
        with lease.open_regular("hotwords/current.json") as descriptor:
            assert os.read(descriptor, 7) == b"pointer"
        lease.mkdir("hotwords/staging")
        assert lease.exists("hotwords/staging")

    assert (root / "hotwords" / "current.json").read_bytes() == b"pointer"
    assert (root / "hotwords" / "staging").is_dir()


def test_missing_retained_directory_never_degrades_to_a_later_alias(tmp_path):
    """Catch path() falling back through the root after a requested dir was absent."""
    root = tmp_path / "state"
    root.mkdir()
    alias_target = tmp_path / "alias-target"
    alias_target.mkdir()

    with guard_state_root(root, retained_dirs=("projects/example",)) as lease:
        (root / "projects").mkdir()
        try:
            (root / "projects" / "example").symlink_to(
                alias_target, target_is_directory=True
            )
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        assert not lease.available("projects/example/project.jsonl")
        with pytest.raises(FileNotFoundError):
            lease.path("projects/example/project.jsonl")


def test_guarded_path_rejects_an_unretained_intermediate_directory(tmp_path):
    """Catch path() exposing multi-component traversal outside a retained parent."""
    root = tmp_path / "state"
    root.mkdir()

    with guard_state_root(root) as lease:
        with pytest.raises(ValueError, match="parent must be explicitly retained"):
            lease.path("unretained/file.json")


@pytest.mark.skipif(os.name != "nt", reason="Windows root-relative path syntax")
def test_windows_lease_helpers_reject_root_relative_paths(tmp_path):
    """Catch ``\\name`` escaping a retained drive-root through Path joining."""
    root = tmp_path / "state"
    root.mkdir()

    with guard_state_root(root) as lease:
        with pytest.raises(ValueError, match="state path must be relative"):
            lease.exists(r"\outside")
        with pytest.raises(ValueError, match="state path must be relative"):
            lease.write_bytes_atomic(r"\outside", b"blocked")

    with pytest.raises(
        ValueError, match="retained state directory must be a non-empty relative"
    ):
        with guard_state_root(root, retained_dirs=(r"\outside",)):
            pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative I/O")
def test_posix_lease_io_does_not_require_proc_or_dev_fd(tmp_path, monkeypatch):
    """Catch descriptor retention depending on optional descriptor filesystems."""
    original_is_dir = Path.is_dir

    def hide_descriptor_filesystems(path: Path) -> bool:
        if path in {Path("/proc/self/fd"), Path("/dev/fd")}:
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", hide_descriptor_filesystems)
    root = tmp_path / "state"

    with guard_state_root(
        root,
        create=True,
        retained_dirs=("hotwords",),
        create_retained=True,
    ) as lease:
        lease.write_bytes_atomic("hotwords/current.json", b'{"version":1}')
        assert lease.exists("hotwords/current.json")
        assert lease.read_bytes(
            "hotwords/current.json", 1024, "current pointer"
        ) == b'{"version":1}'
        assert lease.stat("hotwords/current.json").st_size == 13

    assert (root / "hotwords" / "current.json").read_bytes() == b'{"version":1}'


def test_posix_capability_check_uses_renameat_signal_for_replace(monkeypatch):
    """Catch rejecting POSIX because CPython omits replace from supports_dir_fd."""
    monkeypatch.setattr(
        paths_module.os,
        "supports_dir_fd",
        {
            paths_module.os.open,
            paths_module.os.mkdir,
            paths_module.os.stat,
            paths_module.os.unlink,
            paths_module.os.rename,
        },
    )
    monkeypatch.setattr(
        paths_module.os,
        "supports_fd",
        {paths_module.os.listdir},
    )
    for flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        monkeypatch.setattr(paths_module.os, flag, 1, raising=False)

    paths_module._require_posix_dir_fd_support()

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from hashlib import sha256
from pathlib import Path

import pytest

import voice_intent_normalizer.paths as paths_module
from voice_intent_normalizer.paths import StatePaths, guard_state_root


def test_publish_file_no_replace_exact_moves_validated_identity(tmp_path):
    """Catch identity-bound file publication degrading to check-then-rename."""
    root = tmp_path / "state"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"exact owned bytes")
    info = source.stat()
    commits: list[str] = []

    with guard_state_root(root) as lease:
        lease.publish_file_no_replace_exact(
            "source.bin",
            "bound.bin",
            expected_identity=(info.st_dev, info.st_ino),
            expected_bytes=b"exact owned bytes",
            limit=64,
            on_committed=lambda: commits.append("committed"),
        )

    bound = root / "bound.bin"
    assert not source.exists()
    assert bound.read_bytes() == b"exact owned bytes"
    assert (bound.stat().st_dev, bound.stat().st_ino) == (
        info.st_dev,
        info.st_ino,
    )
    assert commits == ["committed"]


def test_publish_file_no_replace_exact_preserves_boundary_replacement(
    tmp_path,
    monkeypatch,
):
    """A source-name replacement at native move time must never be consumed."""
    root = tmp_path / "state"
    root.mkdir()
    source = root / "source.bin"
    displaced = root / "displaced-owned.bin"
    source.write_bytes(b"exact owned bytes")
    info = source.stat()

    if os.name == "nt":
        native = paths_module._move_windows_handle_no_replace

        def replace_at_native_boundary(descriptor, destination):
            source.rename(displaced)
            source.write_bytes(b"replacement bytes")
            native(descriptor, destination)

        monkeypatch.setattr(
            paths_module,
            "_move_windows_handle_no_replace",
            replace_at_native_boundary,
        )
    elif sys.platform.startswith("linux"):
        native = paths_module._rename_linux_directory_no_replace

        def replace_at_native_boundary(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        ):
            source.rename(displaced)
            source.write_bytes(b"replacement bytes")
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )

        monkeypatch.setattr(
            paths_module,
            "_rename_linux_directory_no_replace",
            replace_at_native_boundary,
        )
    elif sys.platform == "darwin":
        native = paths_module._rename_darwin_directory_no_replace

        def replace_at_native_boundary(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        ):
            source.rename(displaced)
            source.write_bytes(b"replacement bytes")
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )

        monkeypatch.setattr(
            paths_module,
            "_rename_darwin_directory_no_replace",
            replace_at_native_boundary,
        )
    else:
        pytest.skip("native exclusive file moves are unsupported")

    with guard_state_root(root) as lease:
        with pytest.raises((OSError, ValueError)):
            lease.publish_file_no_replace_exact(
                "source.bin",
                "bound.bin",
                expected_identity=(info.st_dev, info.st_ino),
                expected_bytes=b"exact owned bytes",
                limit=64,
            )

    assert source.read_bytes() == b"replacement bytes"
    surviving_owned = [
        path
        for path in (displaced, root / "bound.bin")
        if path.exists() and path.read_bytes() == b"exact owned bytes"
    ]
    assert len(surviving_owned) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX nonblocking open contract")
def test_posix_exact_publication_rejects_raced_fifo_without_blocking(
    tmp_path,
):
    """Catch destination validation blocking forever on a raced FIFO."""
    script = textwrap.dedent(
        r"""
        import os
        import stat
        import sys
        from pathlib import Path

        sys.path.insert(0, sys.argv[2])
        import voice_intent_normalizer.paths as paths_module
        from voice_intent_normalizer.paths import (
            StateRootBoundaryError,
            guard_state_root,
        )

        root = Path(sys.argv[1])
        root.mkdir()
        source = root / "source.bin"
        displaced = root / "displaced-owned.bin"
        source.write_bytes(b"exact owned bytes")
        info = source.stat()

        if sys.platform.startswith("linux"):
            native = paths_module._rename_linux_directory_no_replace
            attribute = "_rename_linux_directory_no_replace"
        elif sys.platform == "darwin":
            native = paths_module._rename_darwin_directory_no_replace
            attribute = "_rename_darwin_directory_no_replace"
        else:
            raise AssertionError("POSIX native test ran on an unsupported host")

        def swap_destination_for_fifo(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        ):
            native(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
            os.rename(
                destination_name,
                displaced.name,
                src_dir_fd=destination_parent,
                dst_dir_fd=destination_parent,
            )
            os.mkfifo(destination_name, dir_fd=destination_parent)

        setattr(paths_module, attribute, swap_destination_for_fifo)
        try:
            with guard_state_root(root) as lease:
                lease.publish_file_no_replace_exact(
                    "source.bin",
                    "bound.bin",
                    expected_identity=(info.st_dev, info.st_ino),
                    expected_bytes=b"exact owned bytes",
                    limit=64,
                )
        except StateRootBoundaryError:
            assert stat.S_ISFIFO(source.stat().st_mode)
            assert displaced.read_bytes() == b"exact owned bytes"
            print("failed-closed")
        else:
            raise AssertionError("FIFO destination was accepted")
        """
    )
    source_root = Path(__file__).resolve().parents[1] / "src"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "fifo-child"),
            str(source_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "failed-closed"


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
    alpha.mkdir()
    (tmp_path / "beta").mkdir()
    first = paths.for_project(alpha)
    second = paths.for_project(tmp_path / "beta")

    assert first.project_id == paths.for_project(alpha).project_id
    assert first.project_id != second.project_id
    expected_id = sha256(str(alpha.resolve()).encode("utf-8")).hexdigest()[:16]
    assert first.project_id == expected_id


def test_missing_project_root_has_no_project_identity(tmp_path):
    """Catch project IDs being minted before a direct root is retained."""
    paths = StatePaths.resolve(environ={}, home=tmp_path)

    with pytest.raises(ValueError, match="project root"):
        paths.for_project(tmp_path / "missing-project")


@pytest.mark.skipif(os.name != "nt", reason="Windows project identity aliases")
def test_windows_project_id_uses_retained_canonical_handle_spelling(tmp_path):
    """Catch case, separator, or extended aliases minting different IDs."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "MixedCaseProject"
    project_root.mkdir()
    canonical = project_root.resolve()
    expected_id = sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
    spellings = (
        project_root,
        Path(str(project_root).swapcase()),
        Path(str(project_root).replace("\\", "/")),
        Path("\\\\?\\" + str(project_root)),
    )

    projects = tuple(paths.for_project(spelling) for spelling in spellings)

    assert {project.project_id for project in projects} == {expected_id}
    assert {project.project_root for project in projects} == {canonical}


def test_project_identity_lookup_releases_its_root_handle(tmp_path):
    """Catch one-shot project identity derivation leaking a retained handle."""
    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    project_root = tmp_path / "project"
    project_root.mkdir()

    paths.for_project(project_root)
    moved = tmp_path / "moved-project"
    project_root.rename(moved)

    assert moved.is_dir()


def test_state_paths_expose_shared_and_project_files_without_creating_them(tmp_path):
    """Catch a path contract that cannot support adapters or project scanning."""
    paths = StatePaths.resolve(environ={}, home=tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = paths.for_project(workspace)

    assert paths.personal_file == paths.root / "personal.jsonl"
    assert paths.preferences_file == paths.root / "preferences.json"
    assert paths.hotwords_file == paths.root / "hotwords" / "zh-ai.jsonl"
    assert paths.adapter_status_file("codex") == paths.root / "adapters" / "codex.json"
    assert project.lexicon_file == (
        paths.root / "projects" / project.project_id / "project.jsonl"
    )
    assert project.scan_lexicon_file == (
        paths.root / "projects" / project.project_id / "project-scan.jsonl"
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
    for flag in ("O_CLOEXEC", "O_DIRECTORY", "O_NONBLOCK", "O_NOFOLLOW"):
        monkeypatch.setattr(paths_module.os, flag, 1, raising=False)

    paths_module._require_posix_dir_fd_support()

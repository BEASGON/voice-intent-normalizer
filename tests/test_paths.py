from __future__ import annotations

import os
from hashlib import sha256

import pytest

import voice_intent_normalizer.paths as paths_module
from voice_intent_normalizer.paths import StatePaths


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

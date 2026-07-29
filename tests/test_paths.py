from __future__ import annotations

from hashlib import sha256

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

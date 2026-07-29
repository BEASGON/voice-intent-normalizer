from __future__ import annotations

import os
from pathlib import Path

import pytest

from voice_intent_normalizer.lexicon import load_jsonl
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.project_scan import scan_project


def _state_paths(tmp_path):
    return StatePaths.resolve(environ={}, home=tmp_path / "home")


def test_scanner_extracts_names_without_reading_secrets_or_dependencies(tmp_path):
    """Catch traversal that reads excluded files or dependency trees."""
    (tmp_path / "README.md").write_text(
        "# 星河工作台\n使用 WorkBuddyAdapter\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("API_TOKEN=secret-value\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text(
        "IgnoredSecret", encoding="utf-8"
    )

    result = scan_project(tmp_path, _state_paths(tmp_path))

    canonicals = {entry.canonical for entry in result.entries}
    assert "星河工作台" in canonicals
    assert "WorkBuddyAdapter" in canonicals
    assert "secret-value" not in canonicals
    assert "IgnoredSecret" not in canonicals


def test_scanner_stores_relative_locations_not_source_paragraphs(tmp_path):
    """Catch a cache that preserves document text instead of a file location."""
    (tmp_path / "docs.md").write_text(
        "客户产品名是云雀引擎，后面是一大段说明。", encoding="utf-8"
    )
    state_paths = _state_paths(tmp_path)

    result = scan_project(tmp_path, state_paths)

    entry = next(item for item in result.entries if item.canonical == "云雀引擎")
    cache_path = state_paths.for_project(tmp_path).lexicon_file
    assert entry.source == "docs.md"
    assert "一大段说明" not in entry.source
    assert "一大段说明" not in cache_path.read_text(encoding="utf-8")


def test_scanner_discards_long_markdown_headings(tmp_path):
    """Catch a scanner that saves a source paragraph as a heading term."""
    long_heading = "这是一个超过二十个汉字的长标题用于说明不应复制为术语内容"
    (tmp_path / "notes.md").write_text(f"# {long_heading}\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)

    result = scan_project(tmp_path, state_paths)

    assert long_heading not in {entry.canonical for entry in result.entries}
    assert long_heading not in state_paths.for_project(tmp_path).lexicon_file.read_text(
        encoding="utf-8"
    )


def test_scanner_ignores_binary_content_with_a_text_extension(tmp_path):
    """Catch binary payloads that leak terms by using a source-like suffix."""
    (tmp_path / "payload.py").write_bytes(b"\x00class HiddenBinaryTerm:\x00")

    result = scan_project(tmp_path, _state_paths(tmp_path))

    assert "HiddenBinaryTerm" not in {entry.canonical for entry in result.entries}


def test_scanner_never_opens_extensionless_openssh_private_keys(tmp_path, monkeypatch):
    """Catch a scanner that opens a conventional private-key filename."""
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("class PrivateKeyLeak:\n", encoding="utf-8")
    real_open = Path.open

    def reject_private_key(path, *args, **kwargs):
        if path == private_key:
            raise AssertionError("scanner opened an OpenSSH private key")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_private_key)

    result = scan_project(tmp_path, _state_paths(tmp_path))

    assert "PrivateKeyLeak" not in {entry.canonical for entry in result.entries}


def test_scanner_rejects_a_symlink_supplied_as_the_project_root(tmp_path):
    """Catch scan-root resolution that follows a caller-provided symlink."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "target.py").write_text("class OutsideTarget:\n", encoding="utf-8")
    link = tmp_path / "linked-project"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create test symlink: {exc}")

    result = scan_project(link, _state_paths(tmp_path))

    assert result.entries == ()
    assert result.files_scanned == 0


def test_scanner_persists_extraction_kind_and_exact_frequency(tmp_path):
    """Catch project caches that discard scanner provenance or counts."""
    (tmp_path / "terms.py").write_text(
        "WidgetEngine = object()\nWidgetEngine.run()\n", encoding="utf-8"
    )
    state_paths = _state_paths(tmp_path)

    scan_project(tmp_path, state_paths)

    entry = next(
        item
        for item in load_jsonl(state_paths.for_project(tmp_path).lexicon_file)
        if item.canonical == "WidgetEngine"
    )
    assert entry.notes == "camel-case"
    assert entry.use_count == 2


def test_scanner_counts_rejected_binary_files_toward_its_file_limit(tmp_path):
    """Catch scans that read unlimited binary files without consuming a limit."""
    (tmp_path / "first.py").write_bytes(b"\x00first")
    (tmp_path / "second.py").write_bytes(b"\x00second")

    result = scan_project(tmp_path, _state_paths(tmp_path), max_files=1)

    assert result.truncated is True
    assert result.files_scanned == 1


def test_scanner_enforces_byte_limit_when_file_grows_after_stat(tmp_path, monkeypatch):
    """Catch an unbounded read after a stale size check."""
    source = tmp_path / "changing.py"
    source.write_text("class ChangedAfterStat:\n", encoding="utf-8")
    real_stat = Path.stat

    def stale_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == source:
            values = list(result)
            values[6] = 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stale_stat)

    result = scan_project(tmp_path, _state_paths(tmp_path), max_text_bytes=1)

    assert result.truncated is True
    assert result.files_scanned == 0
    assert result.text_bytes_scanned == 0


def test_scanner_writes_only_current_project_entries_to_its_jsonl_cache(tmp_path):
    """Catch cache entries that are not isolated by the resolved project ID."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "widget_api.py").write_text("class ProjectWidget:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)

    result = scan_project(project, state_paths)
    project_paths = state_paths.for_project(project)
    persisted = load_jsonl(project_paths.lexicon_file)

    assert result.entries == persisted
    assert {entry.project_id for entry in persisted} == {project_paths.project_id}
    assert {entry.scope.value for entry in persisted} == {"project"}
    assert all(entry.source and not entry.source.startswith("/") for entry in persisted)


def test_scanner_returns_truncated_at_file_and_text_byte_limits(tmp_path):
    """Catch scans that exceed limits instead of degrading safely."""
    (tmp_path / "first.py").write_text("class FirstTerm:\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("class SecondTerm:\n", encoding="utf-8")

    file_limited = scan_project(tmp_path, _state_paths(tmp_path), max_files=1)
    byte_limited = scan_project(
        tmp_path, _state_paths(tmp_path), max_text_bytes=1
    )

    assert file_limited.truncated is True
    assert file_limited.files_scanned == 1
    assert byte_limited.truncated is True
    assert byte_limited.text_bytes_scanned == 0

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path

import pytest

import voice_intent_normalizer.project_scan as project_scan_module
from voice_intent_normalizer.lexicon import load_jsonl, write_jsonl_atomic
from voice_intent_normalizer.models import EntryStatus, LexiconEntry, Scope
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.project_scan import project_cache_is_stale, scan_project


def _state_paths(tmp_path):
    return StatePaths.resolve(environ={}, home=tmp_path / "home")


def _child_publish_scan_with_delayed_generation(
    state_root: str,
    project_root: str,
    start,
    results,
) -> None:
    """Expose duplicate generation reads if publications are not process-locked."""
    import voice_intent_normalizer.project_scan as child_scan_module

    real_next_generation = child_scan_module._next_generation
    real_scan_state_bytes = child_scan_module._scan_state_bytes
    observed: list[int] = []

    def delayed_next_generation(lease, relative):
        generation = real_next_generation(lease, relative)
        time.sleep(0.35)
        return generation

    def capture_generation(*args, **kwargs):
        generation = kwargs.get("generation", args[4])
        observed.append(generation)
        return real_scan_state_bytes(*args, **kwargs)

    child_scan_module._next_generation = delayed_next_generation
    child_scan_module._scan_state_bytes = capture_generation
    try:
        if not start.wait(10):
            results.put(("timeout", None))
            return
        child_scan_module.scan_project(
            Path(project_root), StatePaths(root=Path(state_root))
        )
        results.put(("ok", observed[-1]))
    except Exception as exc:
        results.put((exc.__class__.__name__, str(exc)))


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
    cache_path = state_paths.for_project(tmp_path).scan_lexicon_file
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
    cache = state_paths.for_project(tmp_path).scan_lexicon_file
    assert long_heading not in cache.read_text(encoding="utf-8")


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


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction regression")
def test_scanner_ignores_junction_name_content_and_external_fingerprint(tmp_path):
    """Catch Windows junctions contributing names or traversing external state."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "ExternalSecretTerm.py"
    secret.write_text("class ExternalPrivateThing:\n", encoding="utf-8")
    junction = project / "PrivateJunctionName"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create Windows junction: {created.stderr}")
    state_paths = _state_paths(tmp_path)

    result = scan_project(project, state_paths)
    canonicals = {entry.canonical for entry in result.entries}

    assert "PrivateJunctionName" not in canonicals
    assert "ExternalSecretTerm" not in canonicals
    assert "ExternalPrivateThing" not in canonicals
    assert result.files_scanned == 0
    assert project_cache_is_stale(project, state_paths) is False

    secret.write_text(
        "class MutatedExternalPrivateThing:\n" * 3, encoding="utf-8"
    )

    assert project_cache_is_stale(project, state_paths) is False


def test_scanner_never_opens_file_replaced_by_symlink_after_checks(
    tmp_path, monkeypatch
):
    """Catch a final-component check/open race reading outside project scope."""
    project = tmp_path / "project"
    project.mkdir()
    source = project / "inside.py"
    source.write_text("class InsideTerm:\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("class OutsideSecretTerm:\n", encoding="utf-8")
    real_allowed = project_scan_module._is_allowed_text_file
    real_open = Path.open
    allowed_checks = 0
    swapped = False
    outside_opened = False

    def swap_after_regular_file_check(path):
        nonlocal allowed_checks, swapped
        allowed = real_allowed(path)
        if path == source:
            allowed_checks += 1
            if allowed_checks == 2:
                source.unlink()
                try:
                    source.symlink_to(outside)
                except OSError as exc:
                    pytest.skip(f"file symlinks unavailable: {exc}")
                swapped = True
        return allowed

    def record_outside_open(path, *args, **kwargs):
        nonlocal outside_opened
        if path == source and path.is_symlink():
            outside_opened = True
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        project_scan_module, "_is_allowed_text_file", swap_after_regular_file_check
    )
    monkeypatch.setattr(Path, "open", record_outside_open)

    scan_project(project, _state_paths(tmp_path))

    assert swapped
    assert outside_opened is False


def test_scanner_persists_extraction_kind_and_exact_frequency(tmp_path):
    """Catch project caches that discard scanner provenance or counts."""
    (tmp_path / "terms.py").write_text(
        "WidgetEngine = object()\nWidgetEngine.run()\n", encoding="utf-8"
    )
    state_paths = _state_paths(tmp_path)

    scan_project(tmp_path, state_paths)

    entry = next(
        item
        for item in load_jsonl(state_paths.for_project(tmp_path).scan_lexicon_file)
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
    persisted = load_jsonl(project_paths.scan_lexicon_file)

    assert result.entries == persisted
    assert {entry.project_id for entry in persisted} == {project_paths.project_id}
    assert {entry.scope.value for entry in persisted} == {"project"}
    assert all(entry.source and not entry.source.startswith("/") for entry in persisted)


def test_scan_state_binds_cache_to_its_project_identity(tmp_path):
    """Catch a hash-coherent cache being replayable under another source root."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "widget.py").write_text("class BoundWidget:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)

    scan_project(project, state_paths)

    project_paths = state_paths.for_project(project)
    state = json.loads(project_paths.scan_state_file.read_text(encoding="utf-8"))
    assert state["schema_version"] == 4
    assert state["project_id"] == project_paths.project_id
    assert project_cache_is_stale(project, state_paths) is False


def test_concurrent_process_publications_have_unique_monotonic_generations(tmp_path):
    """Catch process-local locks allowing two successful generation-N commits."""
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    state_root = tmp_path / "state"
    project_root = tmp_path / "project"
    state_root.mkdir()
    project_root.mkdir()
    (project_root / "widget.py").write_text(
        "class ConcurrentWidget:\n", encoding="utf-8"
    )
    project_paths = StatePaths(root=state_root).for_project(project_root)
    project_paths.root.mkdir(parents=True)
    spellings = [str(state_root), str(state_root)]
    if os.name == "nt":
        spellings[1] = str(state_root).swapcase()
    workers = [
        context.Process(
            target=_child_publish_scan_with_delayed_generation,
            args=(spelling, str(project_root), start, results),
        )
        for spelling in spellings
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in workers]
    finally:
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)

    assert [worker.exitcode for worker in workers] == [0, 0]
    assert all(status == "ok" for status, _value in outcomes), outcomes
    assert sorted(value for _status, value in outcomes) == [1, 2]
    state = json.loads(project_paths.scan_state_file.read_text(encoding="utf-8"))
    assert state["generation"] == 2


def test_scanner_returns_truncated_at_file_and_text_byte_limits(tmp_path):
    """Catch scans that exceed limits instead of degrading safely."""
    (tmp_path / "first.py").write_text("class FirstTerm:\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("class SecondTerm:\n", encoding="utf-8")

    file_limited = scan_project(tmp_path, _state_paths(tmp_path), max_files=1)
    byte_limited = scan_project(tmp_path, _state_paths(tmp_path), max_text_bytes=1)

    assert file_limited.truncated is True
    assert file_limited.files_scanned == 1
    assert byte_limited.truncated is True
    assert byte_limited.text_bytes_scanned == 0


@pytest.mark.parametrize(
    "limits",
    (
        {"max_files": 0},
        {"max_files": True},
        {"max_files": 5_001},
        {"max_text_bytes": 0},
        {"max_text_bytes": True},
        {"max_text_bytes": 2_000_001},
    ),
)
def test_scanner_rejects_nonpositive_noninteger_or_over_cap_limits(
    tmp_path, limits
):
    """Catch caller-controlled limits bypassing the scanner's immutable bounds."""
    state_paths = _state_paths(tmp_path)

    with pytest.raises(ValueError, match="scan limits"):
        scan_project(tmp_path, state_paths, **limits)

    assert not state_paths.root.exists()


def test_scan_cache_hash_mismatch_is_stale_and_next_scan_recovers(tmp_path):
    """Catch metadata publication that can declare an altered cache fresh."""
    (tmp_path / "widget.py").write_text("class WidgetEngine:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)
    scan_project(tmp_path, state_paths)
    project_paths = state_paths.for_project(tmp_path)
    project_paths.scan_lexicon_file.write_text("{}\n", encoding="utf-8")

    assert project_cache_is_stale(tmp_path, state_paths) is True

    scan_project(tmp_path, state_paths)
    assert project_cache_is_stale(tmp_path, state_paths) is False


def test_first_refresh_removes_only_legacy_scanner_records(tmp_path):
    """Catch a migration that erases a learned project entry with old shapes."""
    (tmp_path / "widget.py").write_text("class Widget:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)
    project_paths = state_paths.for_project(tmp_path)
    project_paths.root.mkdir(parents=True)
    legacy = LexiconEntry(
        canonical="Widget",
        scope=Scope.PROJECT,
        aliases=("Widget",),
        domains=(),
        weight=0.5,
        status=EntryStatus.CANDIDATE,
        project_id=project_paths.project_id,
        source="widget.py",
        use_count=1,
        notes="camel-case",
    )
    learned = LexiconEntry(
        canonical="WidgetEngine",
        scope=Scope.PROJECT,
        aliases=("WidgetEngine",),
        domains=(),
        weight=0.9,
        status=EntryStatus.CONFIRMED,
        project_id=project_paths.project_id,
        source="user-learning",
    )
    write_jsonl_atomic(project_paths.lexicon_file, (legacy, learned))

    scan_project(tmp_path, state_paths)

    assert [entry.canonical for entry in load_jsonl(project_paths.lexicon_file)] == [
        "WidgetEngine"
    ]


def test_empty_scanned_directory_makes_the_cache_stale(tmp_path):
    """Catch fingerprints that ignore directory-stem producing entries."""
    (tmp_path / "source.py").write_text("class SourceTerm:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)
    scan_project(tmp_path, state_paths)

    (tmp_path / "WidgetDirectory").mkdir()

    assert project_cache_is_stale(tmp_path, state_paths) is True


def test_legacy_cleanup_preserves_nondefault_manual_candidate(tmp_path):
    """Catch broad scanner cleanup deleting manually tuned candidate records."""
    (tmp_path / "widget.py").write_text("class Widget:\n", encoding="utf-8")
    state_paths = _state_paths(tmp_path)
    project_paths = state_paths.for_project(tmp_path)
    project_paths.root.mkdir(parents=True)
    manual = LexiconEntry(
        canonical="Widget", scope=Scope.PROJECT, aliases=("Widget",),
        domains=(), weight=0.99, status=EntryStatus.CANDIDATE,
        project_id=project_paths.project_id, source="widget.py",
        use_count=77, notes="camel-case",
    )
    write_jsonl_atomic(project_paths.lexicon_file, (manual,))

    scan_project(tmp_path, state_paths)

    assert load_jsonl(project_paths.lexicon_file) == (manual,)

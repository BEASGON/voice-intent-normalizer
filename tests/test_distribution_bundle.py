from __future__ import annotations

import ast
import io
import json
import os
import posixpath
import runpy
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest
import setuptools

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from voice_intent_normalizer.adapters import generic_layout

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_native_adapter_contract_on_all_supported_operating_systems():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    for runner in ("windows-latest", "ubuntu-latest", "macos-latest"):
        assert runner in workflow
    assert "python-version: '3.10'" in workflow
    assert "python -m pytest -q" in workflow
    assert "test_publish_directory_no_replace" in workflow
    assert "ownership_journal" in workflow
    assert "python -m ruff check" in workflow
    assert "python -m build --no-isolation" in workflow
    assert "setuptools>=77" in project["project"]["optional-dependencies"]["dev"]


def test_ci_combines_full_coverage_from_all_supported_operating_systems():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))

    assert "coverage:" in workflow
    assert "needs: coverage" in workflow
    assert "coverage-${{ matrix.os }}" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "merge-multiple: true" in workflow
    assert "python -m coverage combine" in workflow
    assert "python -m coverage report --fail-under=90" in workflow
    assert project["tool"]["coverage"]["run"] == {
        "parallel": True,
        "relative_files": True,
        "source": ["voice_intent_normalizer"],
    }


BUNDLE_FILES = (
    "SKILL.md",
    "LICENSE",
    "pyproject.toml",
    "agents/openai.yaml",
    "assets/lexicons/base-zh.jsonl",
    "assets/lexicons/hotwords-snapshot.jsonl",
    "assets/lexicons/domains/ai.jsonl",
    "assets/lexicons/domains/product-design.jsonl",
    "assets/lexicons/domains/software-development.jsonl",
    "references/correction-policy.md",
    "references/domain-packs.md",
    "references/lexicon-schema.md",
    "references/platform-compatibility.md",
    "scripts/voice_intent.py",
)
PYTHON_FILES = (
    "src/voice_intent_normalizer/__init__.py",
    "src/voice_intent_normalizer/cli.py",
    "src/voice_intent_normalizer/hook.py",
    "src/voice_intent_normalizer/installer.py",
    "src/voice_intent_normalizer/learning.py",
    "src/voice_intent_normalizer/lexicon.py",
    "src/voice_intent_normalizer/matching.py",
    "src/voice_intent_normalizer/models.py",
    "src/voice_intent_normalizer/paths.py",
    "src/voice_intent_normalizer/policy.py",
    "src/voice_intent_normalizer/project_scan.py",
    "src/voice_intent_normalizer/service.py",
    "src/voice_intent_normalizer/updater.py",
    "src/voice_intent_normalizer/adapters/__init__.py",
    "src/voice_intent_normalizer/adapters/base.py",
    "src/voice_intent_normalizer/adapters/codex.py",
    "src/voice_intent_normalizer/adapters/openclaw.py",
    "src/voice_intent_normalizer/adapters/workbuddy.py",
    "src/voice_intent_normalizer/adapters/generic.py",
    "src/voice_intent_normalizer/adapters/generic_contract.py",
    "src/voice_intent_normalizer/adapters/generic_layout.py",
)
RUNTIME_FILES = (*BUNDLE_FILES, *PYTHON_FILES)

CAPSULE_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/correction-policy.md",
    "references/domain-packs.md",
    "references/lexicon-schema.md",
    "scripts/_voice_intent_contract.py",
    "scripts/voice_intent.py",
    "capsule.json",
}

_WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _build_distributions(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.tar.gz")), next(output.glob("*.whl"))


def _load_setup_namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(setuptools, "setup", lambda **_kwargs: None)
    return runpy.run_path(str(ROOT / "setup.py"), run_name="task5_setup")


def _copy_runtime_sources(destination: Path) -> Path:
    for relative in RUNTIME_FILES:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def _isolated_environment(python: Path, state: Path) -> dict[str, str]:
    environment = {
        "PATH": str(python.parent),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "VOICE_INTENT_HOME": str(state),
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def _run_isolated(
    python: Path,
    cwd: Path,
    environment: dict[str, str],
    *arguments: str,
    timeout: int = 90,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-I", *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _assert_sdist_runtime_inventory(sdist: Path) -> None:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        assert names
        assert len(names) == len(set(names))
        parts = [_portable_sdist_member_parts(name) for name in names]
        collision_keys = [name.casefold() for name in names]
        assert len(collision_keys) == len(set(collision_keys))
        roots = {components[0] for components in parts}
        assert len(roots) == 1
        root = roots.pop()
        assert all(name == root or name.startswith(f"{root}/") for name in names)
        assert not any("_skill_bundle" in components for components in parts)
        for relative in RUNTIME_FILES:
            anchored = f"{root}/{relative}"
            matches = [
                member
                for member in members
                if member.name == relative
                or member.name.endswith(f"/{relative}")
            ]
            assert len(matches) == 1
            assert matches[0].name == anchored
            assert matches[0].isfile()


def _portable_sdist_member_parts(name: str) -> tuple[str, ...]:
    assert name
    assert not name.startswith(('/', '\\'))
    assert "\\" not in name
    assert posixpath.normpath(name) == name
    components = tuple(name.split("/"))
    assert all(
        component not in {"", ".", ".."}
        and ":" not in component
        and "\x00" not in component
        and not component.endswith((".", " "))
        and component.split(".", 1)[0].upper() not in _WINDOWS_DEVICE_NAMES
        and all(ord(character) >= 0x20 for character in component)
        for component in components
    )
    return components


def _write_synthetic_sdist(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_setup_validates_the_exact_explicit_runtime_source_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    namespace = _load_setup_namespace(monkeypatch)
    validate = namespace["_validated_bundle_sources"]
    repository = _copy_runtime_sources(tmp_path / "repository")

    sources = validate(repository)

    assert tuple(sources) == RUNTIME_FILES
    assert all(
        sources[relative] == (repository / relative).read_bytes()
        for relative in sources
    )


def test_setup_rejects_same_size_in_place_mutation_during_retained_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    namespace = _load_setup_namespace(monkeypatch)
    read_direct_source = namespace["_read_direct_source"]
    repository = _copy_runtime_sources(tmp_path / "repository")
    source = repository / "SKILL.md"
    original = source.read_bytes()
    replacement = bytes(byte ^ 1 for byte in original)
    before = source.stat()
    mutated = False

    def mutate_source_in_place() -> None:
        nonlocal mutated
        if mutated:
            return
        mutated = True
        with source.open("r+b", buffering=0) as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(
            source,
            ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000),
        )

    original_path_read_bytes = Path.read_bytes

    def path_read_bytes_then_mutate(path: Path) -> bytes:
        data = original_path_read_bytes(path)
        if path == source:
            mutate_source_in_place()
        return data

    original_os_read = os.read

    def retained_read_then_mutate(descriptor: int, size: int) -> bytes:
        data = original_os_read(descriptor, size)
        if data:
            mutate_source_in_place()
        return data

    monkeypatch.setattr(Path, "read_bytes", path_read_bytes_then_mutate)
    monkeypatch.setattr(os, "read", retained_read_then_mutate)

    with pytest.raises(RuntimeError, match="changed while read"):
        read_direct_source(repository, "SKILL.md")

    after = source.stat()
    assert mutated
    assert (after.st_dev, after.st_ino, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
    )
    assert after.st_mtime_ns != before.st_mtime_ns


def test_setup_rejects_missing_duplicate_aliased_and_extra_runtime_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    namespace = _load_setup_namespace(monkeypatch)
    validate = namespace["_validated_bundle_sources"]

    missing = _copy_runtime_sources(tmp_path / "missing")
    (missing / "src/voice_intent_normalizer/adapters/generic_layout.py").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        validate(missing)

    duplicate = _copy_runtime_sources(tmp_path / "duplicate")
    validate.__globals__["_BUNDLE_FILES"] = (*RUNTIME_FILES, RUNTIME_FILES[-1])
    with pytest.raises(RuntimeError, match="duplicate"):
        validate(duplicate)
    validate.__globals__["_BUNDLE_FILES"] = RUNTIME_FILES

    aliased = _copy_runtime_sources(tmp_path / "aliased")
    alias = aliased / "SKILL.md"
    target = aliased / "real-skill.md"
    alias.replace(target)
    try:
        alias.symlink_to(target)
    except OSError as exc:  # pragma: no cover - a required platform capability
        pytest.fail(f"test platform cannot create the required file alias: {exc}")
    with pytest.raises(RuntimeError, match="alias"):
        validate(aliased)

    extra = _copy_runtime_sources(tmp_path / "extra")
    (extra / "src/voice_intent_normalizer/ambient.py").write_text(
        "raise RuntimeError('must never ship')\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="extra"):
        validate(extra)

    extra_asset = _copy_runtime_sources(tmp_path / "extra-asset")
    (extra_asset / "assets/lexicons/ambient.jsonl").write_text(
        '{}\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="extra"):
        validate(extra_asset)


def test_public_python_sources_parse_with_python_310_grammar():
    sources = [
        ROOT / "setup.py",
        ROOT / "scripts/voice_intent.py",
        *(ROOT / relative for relative in PYTHON_FILES),
    ]

    for source in sources:
        ast.parse(
            source.read_text(encoding="utf-8"),
            filename=str(source),
            feature_version=(3, 10),
        )


def test_capsule_and_generation_builders_use_exact_bounded_allowlists():
    capsule_builder = getattr(generic_layout, "capsule_source_files", None)
    generation_builder = getattr(generic_layout, "generation_source_files", None)

    assert callable(capsule_builder), "capsule_source_files() is not implemented"
    assert callable(generation_builder), "generation_source_files() is not implemented"
    capsule = capsule_builder(ROOT)
    generation = generation_builder(ROOT)

    assert set(capsule) == CAPSULE_FILES
    assert capsule["scripts/_voice_intent_contract.py"] == (
        ROOT
        / "src"
        / "voice_intent_normalizer"
        / "adapters"
        / "generic_contract.py"
    ).read_bytes()
    assert tuple(generation) == tuple(sorted(RUNTIME_FILES))
    forbidden = (
        "tests/",
        ".git/",
        ".superpowers/",
        "personal.jsonl",
        "preferences.json",
        "projects/",
        "hotwords/",
    )
    assert not any(
        any(part in relative for part in forbidden) for relative in generation
    )


def test_distribution_contains_synced_runtime_skill_bundle(tmp_path: Path):
    sdist, wheel = _build_distributions(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        expected_bundle = {
            f"voice_intent_normalizer/_skill_bundle/{relative}"
            for relative in RUNTIME_FILES
        }
        for relative in RUNTIME_FILES:
            bundled = f"voice_intent_normalizer/_skill_bundle/{relative}"
            assert bundled in names
            assert archive.read(bundled) == (ROOT / relative).read_bytes()
        actual_bundle = {
            name
            for name in names
            if name.startswith("voice_intent_normalizer/_skill_bundle/")
            and not name.endswith("/")
        }
        assert actual_bundle == expected_bundle
        assert not any(
            "/_skill_bundle/tests/" in name
            or "/_skill_bundle/.git/" in name
            or "/_skill_bundle/.superpowers/" in name
            for name in names
        )
    _assert_sdist_runtime_inventory(sdist)

    with tarfile.open(sdist, "r:gz") as archive:
        names = set(archive.getnames())
    assert any(name.endswith("adapters/codex/AGENTS.snippet.md") for name in names)
    assert any(name.endswith("adapters/codex/hooks.template.json") for name in names)
    assert not any("/tests/" in name for name in names)


@pytest.mark.parametrize(
    ("archive_root", "invalid_member"),
    [
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "second-root/PKG-INFO",
            id="second-top-level-root",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/SKILL.md",
            id="duplicate-required-member",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/misplaced/SKILL.md",
            id="misplaced-required-member",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/"
            "src/voice_intent_normalizer/_skill_bundle/SKILL.md",
            id="generated-package-bundle",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/_skill_bundle/stale.txt",
            id="stale-root-bundle",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/skill.md",
            id="casefold-collision",
        ),
        pytest.param("C:", "C:/README.md", id="drive-style-root"),
        pytest.param(
            "/absolute-root", "/absolute-root/README.md", id="absolute-root"
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs/./README.md",
            id="dot-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs/../README.md",
            id="dotdot-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs\\README.md",
            id="backslash-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs/README.md:stream",
            id="ads-colon-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs/NUL.txt",
            id="device-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs./README.md",
            id="trailing-dot-component",
        ),
        pytest.param(
            "voice_intent_normalizer-0.1.0",
            "voice_intent_normalizer-0.1.0/docs /README.md",
            id="trailing-space-component",
        ),
    ],
)
def test_sdist_runtime_inventory_rejects_misplaced_duplicate_and_stale_members(
    tmp_path: Path, archive_root: str, invalid_member: str
):
    entries = [
        (f"{archive_root}/{relative}", f"runtime:{relative}".encode())
        for relative in RUNTIME_FILES
    ]
    entries.extend(
        [
            (f"{archive_root}/PKG-INFO", b"metadata"),
            (f"{archive_root}/setup.cfg", b"standard sdist metadata"),
            (
                f"{archive_root}/"
                "src/voice_intent_normalizer.egg-info/SOURCES.txt",
                b"standard egg metadata",
            ),
            (invalid_member, b"invalid duplicate or misplaced member"),
        ]
    )
    sdist = tmp_path / "synthetic.tar.gz"
    _write_synthetic_sdist(sdist, entries)

    with pytest.raises(AssertionError):
        _assert_sdist_runtime_inventory(sdist)


def test_rebuild_clears_stale_generated_bundle_files(tmp_path: Path):
    stale = (
        ROOT
        / "build"
        / "lib"
        / "voice_intent_normalizer"
        / "_skill_bundle"
        / "stale.txt"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("must not ship", encoding="utf-8")

    _, wheel = _build_distributions(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        assert (
            "voice_intent_normalizer/_skill_bundle/stale.txt"
            not in archive.namelist()
        )


def test_installed_wheel_default_installer_and_bootstrap_are_runnable(
    tmp_path: Path,
):
    _, wheel = _build_distributions(tmp_path)
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    skills = tmp_path / "skills"
    skills.mkdir()
    state = tmp_path / "state"
    isolated = _isolated_environment(python, state)
    skills_literal = json.dumps(str(skills))
    code = f"""
import json
from pathlib import Path
from voice_intent_normalizer.adapters.base import InstallOptions
from voice_intent_normalizer.cli import default_installer

root = Path({skills_literal})
result = default_installer().install(
    ("generic",), InstallOptions(output_dir=root)
)[0]
print(json.dumps({{"status": result.status}}))
"""
    installed = _run_isolated(
        python, tmp_path, isolated, "-c", code, timeout=60
    )

    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["status"] == "installed"
    capsule = skills / "voice-intent-normalizer"
    bootstrap = capsule / "scripts" / "voice_intent.py"
    doctor = _run_isolated(
        python, tmp_path, isolated, str(bootstrap), "doctor", "--json"
    )
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout) == {
        "status": "ok",
        "state_root": str(state),
        "diagnostics": [],
    }
    normalized = _run_isolated(
        python,
        tmp_path,
        isolated,
        str(bootstrap),
        "normalize",
        "--text",
        "配置 open cloud",
        "--domain",
        "ai",
        "--json",
    )
    assert normalized.returncode == 0, normalized.stderr
    decision = json.loads(normalized.stdout)
    assert set(decision) == {
        "action",
        "original_text",
        "corrected_text",
        "notices",
        "question",
        "diagnostics",
        "candidates",
    }
    assert decision["action"] == "apply"
    assert decision["original_text"] == "配置 open cloud"
    assert decision["corrected_text"] == "配置 OpenClaw"

    lifecycle = f"""
import json
import shutil
from pathlib import Path

from voice_intent_normalizer import cli
from voice_intent_normalizer.adapters.base import InstallOptions, UninstallOptions
from voice_intent_normalizer.adapters.generic import GenericAdapter
from voice_intent_normalizer.adapters.generic_contract import validate_status_v5
from voice_intent_normalizer.adapters.generic_layout import generic_layout_paths
from voice_intent_normalizer.cli import default_installer
from voice_intent_normalizer.paths import StatePaths

root = Path({skills_literal})
state = StatePaths.resolve()
options = InstallOptions(output_dir=root)
layout = generic_layout_paths(state)
before_noop = layout.status.read_bytes()
generations_before_noop = sorted(path.name for path in layout.generations.iterdir())
noop = default_installer().install(("generic",), options)[0]
assert layout.status.read_bytes() == before_noop
assert (
    sorted(path.name for path in layout.generations.iterdir())
    == generations_before_noop
)

shared = {{
    state.personal_file: b'{{"fixture":"personal"}}\\n',
    state.preferences_file: b'{{"fixture":"preferences"}}',
    state.hotwords_file: b'{{"fixture":"hotwords"}}\\n',
    state.root / "projects/project-a/lexicon.jsonl": b'{{"fixture":"project"}}\\n',
}}
for path, data in shared.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

capsule = root / "voice-intent-normalizer"
capsule_before = {{
    path.relative_to(capsule).as_posix(): path.read_bytes()
    for path in capsule.rglob("*")
    if path.is_file()
}}
upgrade = Path({json.dumps(str(tmp_path / "upgrade-repository"))})
shutil.copytree(
    cli._runtime_repository(),
    upgrade,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
)
pyproject = upgrade / "pyproject.toml"
pyproject.write_bytes(
    pyproject.read_bytes().replace(b'version = "0.1.0"', b'version = "0.2.0"', 1)
)
upgraded = GenericAdapter(upgrade, state)
original_advance = upgraded._advance_transaction_status
interrupted = {{"activation": False, "rollback": False}}

def stop_activation(
    root_path,
    *,
    journal,
    before_payload,
    after_payload,
    terminal=False,
):
    result = original_advance(
        root_path,
        journal=journal,
        before_payload=before_payload,
        after_payload=after_payload,
        terminal=terminal,
    )
    transaction = after_payload.get("transaction")
    if (
        not interrupted["activation"]
        and isinstance(transaction, dict)
        and transaction.get("phase") == "activation-pending"
    ):
        interrupted["activation"] = True
        raise OSError("injected activation interruption")
    return result

upgraded._advance_transaction_status = stop_activation
failed_upgrade = upgraded.install(options)
upgraded._advance_transaction_status = original_advance
pending = validate_status_v5(
    layout.status.read_bytes(),
    skill_root=root,
    generations_root=layout.generations,
)
candidate = next(
    reference
    for reference in (pending.active, pending.previous)
    if reference is not None and reference.package_version == "0.2.0"
)
candidate_file = layout.generations / candidate.generation_id / "LICENSE"
candidate_file.write_bytes(b"tampered candidate")

def stop_rollback(
    root_path,
    *,
    journal,
    before_payload,
    after_payload,
    terminal=False,
):
    result = original_advance(
        root_path,
        journal=journal,
        before_payload=before_payload,
        after_payload=after_payload,
        terminal=terminal,
    )
    transaction = after_payload.get("transaction")
    if (
        not interrupted["rollback"]
        and isinstance(transaction, dict)
        and transaction.get("phase") == "rollback-pending"
    ):
        interrupted["rollback"] = True
        raise OSError("injected rollback interruption")
    return result

upgraded._advance_transaction_status = stop_rollback
rollback_interrupted = upgraded.doctor()
upgraded._advance_transaction_status = original_advance
rollback_recovered = upgraded.doctor()
rolled_back = validate_status_v5(
    layout.status.read_bytes(),
    skill_root=root,
    generations_root=layout.generations,
)
conflict_capsule_after = {{
    path.relative_to(capsule).as_posix(): path.read_bytes()
    for path in capsule.rglob("*")
    if path.is_file()
}}

clean_root = Path({json.dumps(str(tmp_path / "clean-skills"))})
clean_root.mkdir()
clean_state = StatePaths(root=Path({json.dumps(str(tmp_path / "clean-state"))}))
clean_options = InstallOptions(output_dir=clean_root)
clean_install = default_installer(clean_state).install(("generic",), clean_options)[0]
clean_shared = {{
    clean_state.personal_file: b'{{"fixture":"clean-personal"}}\\n',
    clean_state.preferences_file: b'{{"fixture":"clean-preferences"}}',
    clean_state.hotwords_file: b'{{"fixture":"clean-hotwords"}}\\n',
    (
        clean_state.root / "projects/project-b/lexicon.jsonl"
    ): b'{{"fixture":"clean-project"}}\\n',
}}
for path, data in clean_shared.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
clean_capsule = clean_root / "voice-intent-normalizer"
clean_capsule_before = {{
    path.relative_to(clean_capsule).as_posix(): path.read_bytes()
    for path in clean_capsule.rglob("*")
    if path.is_file()
}}
clean_upgraded = GenericAdapter(upgrade, clean_state)
clean_upgrade = clean_upgraded.install(clean_options)
clean_layout = generic_layout_paths(clean_state)
activated = validate_status_v5(
    clean_layout.status.read_bytes(),
    skill_root=clean_root,
    generations_root=clean_layout.generations,
)
clean_capsule_after = {{
    path.relative_to(clean_capsule).as_posix(): path.read_bytes()
    for path in clean_capsule.rglob("*")
    if path.is_file()
}}
uninstalled = default_installer(clean_state).uninstall(
    ("generic",), UninstallOptions(output_dir=clean_root)
)[0]
print(json.dumps({{
    "noop": noop.status,
    "failed_upgrade": failed_upgrade.status,
    "activation_interrupted": interrupted["activation"],
    "rollback_interrupted_status": rollback_interrupted.status,
    "rollback_interrupted": interrupted["rollback"],
    "rollback_recovered": rollback_recovered.status,
    "rolled_back_version": rolled_back.active.package_version,
    "tampered_candidate_preserved": (
        candidate_file.read_bytes() == b"tampered candidate"
    ),
    "journal_preserved": layout.transaction.is_file(),
    "conflict_capsule_unchanged": capsule_before == conflict_capsule_after,
    "clean_install": clean_install.status,
    "clean_upgrade": clean_upgrade.status,
    "activated_version": activated.active.package_version,
    "capsule_unchanged": clean_capsule_before == clean_capsule_after,
    "uninstalled": uninstalled.status,
    "capsule_removed": not clean_capsule.exists(),
    "shared_survived": (
        all(path.read_bytes() == data for path, data in shared.items())
        and all(path.read_bytes() == data for path, data in clean_shared.items())
    ),
}}, ensure_ascii=False))
"""
    completed = _run_isolated(
        python, tmp_path, isolated, "-c", lifecycle, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "noop": "already-installed",
        "failed_upgrade": "failed",
        "activation_interrupted": True,
        "rollback_interrupted_status": "degraded",
        "rollback_interrupted": True,
        "rollback_recovered": "degraded",
        "rolled_back_version": "0.1.0",
        "tampered_candidate_preserved": True,
        "journal_preserved": True,
        "conflict_capsule_unchanged": True,
        "clean_install": "installed",
        "clean_upgrade": "upgraded",
        "activated_version": "0.2.0",
        "capsule_unchanged": True,
        "uninstalled": "uninstalled",
        "capsule_removed": True,
        "shared_survived": True,
    }

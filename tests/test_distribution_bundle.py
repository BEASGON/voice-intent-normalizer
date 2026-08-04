from __future__ import annotations

import ast
import json
import os
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

from voice_intent_normalizer.adapters import generic_layout

ROOT = Path(__file__).resolve().parents[1]
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
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        for relative in RUNTIME_FILES:
            assert any(name.endswith(f"/{relative}") for name in names)


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
original_write = upgraded._write_transaction_status
interrupted = {{"activation": False, "rollback": False}}

def stop_activation(root_path, payload):
    original_write(root_path, payload)
    transaction = payload.get("transaction")
    if (
        not interrupted["activation"]
        and isinstance(transaction, dict)
        and transaction.get("phase") == "activation-pending"
    ):
        interrupted["activation"] = True
        raise OSError("injected activation interruption")

upgraded._write_transaction_status = stop_activation
failed_upgrade = upgraded.install(options)
upgraded._write_transaction_status = original_write
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
(layout.generations / candidate.generation_id / "LICENSE").write_bytes(
    b"tampered candidate"
)

def stop_rollback(root_path, payload):
    original_write(root_path, payload)
    transaction = payload.get("transaction")
    if (
        not interrupted["rollback"]
        and isinstance(transaction, dict)
        and transaction.get("phase") == "rollback-pending"
    ):
        interrupted["rollback"] = True
        raise OSError("injected rollback interruption")

upgraded._write_transaction_status = stop_rollback
rollback_interrupted = upgraded.doctor()
upgraded._write_transaction_status = original_write
rollback_recovered = upgraded.doctor()
rolled_back = validate_status_v5(
    layout.status.read_bytes(),
    skill_root=root,
    generations_root=layout.generations,
)
clean_upgrade = upgraded.install(options)
activated = validate_status_v5(
    layout.status.read_bytes(),
    skill_root=root,
    generations_root=layout.generations,
)
capsule_after = {{
    path.relative_to(capsule).as_posix(): path.read_bytes()
    for path in capsule.rglob("*")
    if path.is_file()
}}
uninstalled = default_installer().uninstall(
    ("generic",), UninstallOptions(output_dir=root)
)[0]
print(json.dumps({{
    "noop": noop.status,
    "failed_upgrade": failed_upgrade.status,
    "activation_interrupted": interrupted["activation"],
    "rollback_interrupted_status": rollback_interrupted.status,
    "rollback_interrupted": interrupted["rollback"],
    "rollback_recovered": rollback_recovered.status,
    "rolled_back_version": rolled_back.active.package_version,
    "clean_upgrade": clean_upgrade.status,
    "activated_version": activated.active.package_version,
    "capsule_unchanged": capsule_before == capsule_after,
    "uninstalled": uninstalled.status,
    "capsule_removed": not capsule.exists(),
    "shared_survived": all(path.read_bytes() == data for path, data in shared.items()),
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
        "rollback_recovered": "installed",
        "rolled_back_version": "0.1.0",
        "clean_upgrade": "upgraded",
        "activated_version": "0.2.0",
        "capsule_unchanged": True,
        "uninstalled": "uninstalled",
        "capsule_removed": True,
        "shared_survived": True,
    }

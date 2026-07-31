from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

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


def test_distribution_contains_synced_runtime_skill_bundle(tmp_path: Path):
    sdist, wheel = _build_distributions(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for relative in BUNDLE_FILES:
            bundled = f"voice_intent_normalizer/_skill_bundle/{relative}"
            assert bundled in names
            assert archive.read(bundled) == (ROOT / relative).read_bytes()
        for source in (ROOT / "src" / "voice_intent_normalizer").rglob("*.py"):
            relative = source.relative_to(ROOT)
            bundled = f"voice_intent_normalizer/_skill_bundle/{relative.as_posix()}"
            assert bundled in names
            assert archive.read(bundled) == source.read_bytes()
        assert not any(
            "/_skill_bundle/tests/" in name
            or "/_skill_bundle/.git/" in name
            or "/_skill_bundle/.superpowers/" in name
            for name in names
        )
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        for relative in BUNDLE_FILES:
            assert any(name.endswith(f"/{relative}") for name in names)


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
    skills_literal = json.dumps(str(skills))
    state_literal = json.dumps(str(state))
    code = f"""
import json
from pathlib import Path
from voice_intent_normalizer.adapters.base import InstallOptions
from voice_intent_normalizer.cli import default_installer
from voice_intent_normalizer.paths import StatePaths

root = Path({skills_literal})
state = StatePaths.resolve(environ={{"VOICE_INTENT_HOME": {state_literal}}})
result = default_installer(state).install(
    ("generic",), InstallOptions(output_dir=root)
)[0]
print(json.dumps({{"status": result.status}}))
"""
    installed = subprocess.run(
        [str(python), "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["status"] == "installed"
    bootstrap = subprocess.run(
        [
            str(python),
            "-I",
            str(skills / "voice-intent-normalizer" / "scripts" / "voice_intent.py"),
            "doctor",
            "--json",
        ],
        env={
            "PATH": str(python.parent),
            "VOICE_INTENT_HOME": str(tmp_path / "bootstrap-state"),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert json.loads(bootstrap.stdout)["status"] in {"ok", "degraded"}

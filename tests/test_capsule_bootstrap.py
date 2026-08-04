"""Subprocess coverage for the stable capsule's installed bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from voice_intent_normalizer.adapters import generic_layout
from voice_intent_normalizer.adapters.generic_contract import (
    build_manifest,
    canonical_json_bytes,
    manifest_digest,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BuiltRuntime:
    capsule: Path
    generation: Path
    state: Path
    status: Path
    generation_manifest: Path
    generation_id: str
    marker: Path


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def _manifest_dict(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


@pytest.fixture
def built_runtime(tmp_path: Path) -> BuiltRuntime:
    capsule_builder = getattr(generic_layout, "capsule_source_files", None)
    generation_builder = getattr(generic_layout, "generation_source_files", None)
    assert callable(capsule_builder), "capsule_source_files() is not implemented"
    assert callable(generation_builder), "generation_source_files() is not implemented"

    skill_root = tmp_path / "skills"
    capsule = skill_root / "voice-intent-normalizer"
    capsule_files = capsule_builder(ROOT)
    _write_files(capsule, capsule_files)

    generation_files = generation_builder(ROOT)
    package_version = "0.1.0"
    nonce = "1" * 32
    provisional = build_manifest(
        "generation", f"g-{'0' * 64}-{nonce}", package_version, generation_files
    )
    generation_id = f"g-{provisional['package_hash']}-{nonce}"
    generation_manifest = build_manifest(
        "generation", generation_id, package_version, generation_files
    )
    state = tmp_path / "state"
    generation = (
        state
        / "adapters"
        / "generic"
        / "generations"
        / generation_id
    )
    _write_files(generation, generation_files)
    generation_manifest_path = generation / "generation.json"
    generation_manifest_path.write_bytes(canonical_json_bytes(generation_manifest))

    capsule_manifest = _manifest_dict(capsule / "capsule.json")
    status_payload = {
        "format": 5,
        "layout": "versioned-v1",
        "selected_skill_root": str(skill_root),
        "capability": "manual",
        "capsule": {
            "protocol": 1,
            "manifest_digest": manifest_digest(capsule_manifest),
            "package_hash": capsule_manifest["package_hash"],
        },
        "active": {
            "generation_id": generation_id,
            "manifest_digest": manifest_digest(generation_manifest),
            "package_hash": generation_manifest["package_hash"],
            "package_version": package_version,
        },
        "previous": None,
        "transaction": None,
    }
    status = state / "adapters" / "generic" / "status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_bytes(canonical_json_bytes(status_payload))
    return BuiltRuntime(
        capsule=capsule,
        generation=generation,
        state=state,
        status=status,
        generation_manifest=generation_manifest_path,
        generation_id=generation_id,
        marker=tmp_path / "untrusted-marker",
    )


@pytest.fixture
def built_capsule(built_runtime: BuiltRuntime) -> Path:
    return built_runtime.capsule


@pytest.fixture
def built_generation(built_runtime: BuiltRuntime) -> Path:
    return built_runtime.generation


def _run_bootstrap(
    built: BuiltRuntime, *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["VOICE_INTENT_HOME"] = str(built.state)
    environment["VOICE_INTENT_UNTRUSTED_MARKER"] = str(built.marker)
    environment["PYTHONUTF8"] = "1"
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            str(built.capsule / "scripts" / "voice_intent.py"),
            "doctor",
            "--json",
        ],
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def _assert_rejected_without_marker(built: BuiltRuntime) -> None:
    result = _run_bootstrap(built)
    assert result.returncode != 0
    assert not built.marker.exists()


def test_capsule_bootstrap_runs_only_the_status_anchored_generation(
    built_runtime: BuiltRuntime,
    built_capsule: Path,
    built_generation: Path,
):
    assert built_capsule == built_runtime.capsule
    assert built_generation == built_runtime.generation

    result = _run_bootstrap(built_runtime)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"


@pytest.mark.parametrize("failure", ["absent", "duplicate", "malformed", "future"])
def test_capsule_bootstrap_rejects_untrusted_status(
    built_runtime: BuiltRuntime, failure: str
):
    if failure == "absent":
        built_runtime.status.unlink()
    elif failure == "duplicate":
        raw = built_runtime.status.read_text(encoding="utf-8")
        built_runtime.status.write_text(
            raw.replace('"format":5', '"format":5,"format":5'),
            encoding="utf-8",
        )
    elif failure == "malformed":
        built_runtime.status.write_text("{", encoding="utf-8")
    else:
        payload = _manifest_dict(built_runtime.status)
        payload["format"] = 6
        built_runtime.status.write_text(json.dumps(payload), encoding="utf-8")

    _assert_rejected_without_marker(built_runtime)


@pytest.mark.parametrize(
    ("selector", "unsupported"),
    [
        ("format", 6),
        ("format", True),
        ("layout", "versioned-v2"),
        ("protocol", 2),
        ("protocol", True),
    ],
)
def test_capsule_bootstrap_rejects_future_selector_before_loading_helper(
    built_runtime: BuiltRuntime, selector: str, unsupported: object
):
    helper_path = (
        built_runtime.capsule / "scripts" / "_voice_intent_contract.py"
    )
    helper_path.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n",
        encoding="utf-8",
    )
    capsule_manifest = _manifest_dict(built_runtime.capsule / "capsule.json")
    capsule_files = {
        relative: (built_runtime.capsule / relative).read_bytes()
        for relative in capsule_manifest["files"]
    }
    substituted_manifest = build_manifest(
        "capsule", "voice-intent-normalizer", "0.1.0", capsule_files
    )
    (built_runtime.capsule / "capsule.json").write_bytes(
        canonical_json_bytes(substituted_manifest)
    )
    status = _manifest_dict(built_runtime.status)
    status["capsule"]["manifest_digest"] = manifest_digest(substituted_manifest)
    status["capsule"]["package_hash"] = substituted_manifest["package_hash"]
    if selector == "protocol":
        status["capsule"]["protocol"] = unsupported
    else:
        status[selector] = unsupported
    built_runtime.status.write_bytes(canonical_json_bytes(status))

    _assert_rejected_without_marker(built_runtime)


@pytest.mark.parametrize(
    "generation_id",
    ["C:/outside", "/outside", "../outside", "g-../outside"],
)
def test_capsule_bootstrap_rejects_untrusted_generation_identifiers(
    built_runtime: BuiltRuntime, generation_id: str
):
    payload = _manifest_dict(built_runtime.status)
    payload["active"]["generation_id"] = generation_id
    built_runtime.status.write_text(json.dumps(payload), encoding="utf-8")

    _assert_rejected_without_marker(built_runtime)


@pytest.mark.parametrize(
    "relative",
    [
        "src",
        "src/voice_intent_normalizer",
        "src/voice_intent_normalizer/__init__.py",
        "src/voice_intent_normalizer/cli.py",
        "src/voice_intent_normalizer/service.py",
    ],
)
def test_capsule_bootstrap_rejects_generation_aliases(
    built_runtime: BuiltRuntime, relative: str, tmp_path: Path
):
    target = built_runtime.generation / relative
    external = tmp_path / "external"
    external.mkdir(exist_ok=True)
    if target.is_dir():
        shutil.rmtree(target)
        alias_target = external / target.name
        alias_target.mkdir()
        (alias_target / "marker.py").write_text(
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n",
            encoding="utf-8",
        )
        try:
            target.symlink_to(alias_target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlink unavailable: {exc}")
    else:
        target.unlink()
        alias_target = external / target.name
        alias_target.write_text(
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n",
            encoding="utf-8",
        )
        try:
            target.symlink_to(alias_target)
        except OSError as exc:
            pytest.skip(f"file symlink unavailable: {exc}")

    _assert_rejected_without_marker(built_runtime)


def test_capsule_bootstrap_rejects_generation_directory_alias(
    built_runtime: BuiltRuntime, tmp_path: Path
):
    external = tmp_path / "external-generation"
    built_runtime.generation.rename(external)
    try:
        built_runtime.generation.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    _assert_rejected_without_marker(built_runtime)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_capsule_bootstrap_rejects_generation_directory_junction(
    built_runtime: BuiltRuntime, tmp_path: Path
):
    external = tmp_path / "external-junction-generation"
    built_runtime.generation.rename(external)
    created = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(built_runtime.generation),
            str(external),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"junction unavailable: {created.stderr}")
    try:
        _assert_rejected_without_marker(built_runtime)
    finally:
        os.rmdir(built_runtime.generation)


@pytest.mark.parametrize("artifact", ["payload.pyc", "payload.pyd", "payload.so"])
def test_capsule_bootstrap_rejects_loader_artifacts(
    built_runtime: BuiltRuntime, artifact: str
):
    (built_runtime.generation / artifact).write_bytes(b"not a trusted loader")

    _assert_rejected_without_marker(built_runtime)


def test_capsule_bootstrap_rejects_existing_pycache_bytecode(
    built_runtime: BuiltRuntime, tmp_path: Path
):
    source = tmp_path / "evil.py"
    source.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n",
        encoding="utf-8",
    )
    cache = (
        built_runtime.generation
        / "src"
        / "voice_intent_normalizer"
        / "__pycache__"
        / "cli.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(
        str(source),
        cfile=str(cache),
        dfile=str(
            built_runtime.generation
            / "src"
            / "voice_intent_normalizer"
            / "cli.py"
        ),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )

    _assert_rejected_without_marker(built_runtime)


@pytest.mark.parametrize(
    "mismatch", ["file", "manifest", "status", "capsule", "skill-root"]
)
def test_capsule_bootstrap_rejects_hash_and_anchor_mismatches(
    built_runtime: BuiltRuntime, mismatch: str
):
    if mismatch == "file":
        cli = (
            built_runtime.generation
            / "src"
            / "voice_intent_normalizer"
            / "cli.py"
        )
        cli.write_bytes(b"raise RuntimeError('substituted')\n")
    elif mismatch == "manifest":
        payload = _manifest_dict(built_runtime.generation_manifest)
        payload["file_hashes"]["src/voice_intent_normalizer/cli.py"] = "0" * 64
        built_runtime.generation_manifest.write_text(
            json.dumps(payload), encoding="utf-8"
        )
    elif mismatch == "status":
        payload = _manifest_dict(built_runtime.status)
        payload["active"]["manifest_digest"] = "0" * 64
        built_runtime.status.write_text(json.dumps(payload), encoding="utf-8")
    elif mismatch == "capsule":
        payload = _manifest_dict(built_runtime.status)
        payload["capsule"]["manifest_digest"] = "0" * 64
        built_runtime.status.write_text(json.dumps(payload), encoding="utf-8")
    else:
        payload = _manifest_dict(built_runtime.status)
        payload["selected_skill_root"] = str(built_runtime.capsule.parent.parent)
        built_runtime.status.write_bytes(canonical_json_bytes(payload))

    _assert_rejected_without_marker(built_runtime)


def test_capsule_bootstrap_ignores_ambient_pythonpath_package(
    built_runtime: BuiltRuntime, tmp_path: Path
):
    ambient = tmp_path / "ambient" / "voice_intent_normalizer"
    ambient.mkdir(parents=True)
    (ambient / "__init__.py").write_text("", encoding="utf-8")
    (ambient / "cli.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n"
        "def main(): return 91\n",
        encoding="utf-8",
    )

    result = _run_bootstrap(
        built_runtime, extra_env={"PYTHONPATH": str(ambient.parent)}
    )

    assert result.returncode == 0, result.stderr
    assert not built_runtime.marker.exists()


def test_capsule_bootstrap_discards_preloaded_package(
    built_runtime: BuiltRuntime, tmp_path: Path
):
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "sitecustomize.py").write_text(
        "import sys, types\n"
        "package = types.ModuleType('voice_intent_normalizer')\n"
        "package.__path__ = []\n"
        "cli = types.ModuleType('voice_intent_normalizer.cli')\n"
        "cli.main = lambda: 92\n"
        "sys.modules['voice_intent_normalizer'] = package\n"
        "sys.modules['voice_intent_normalizer.cli'] = cli\n",
        encoding="utf-8",
    )

    result = _run_bootstrap(
        built_runtime, extra_env={"PYTHONPATH": str(ambient)}
    )

    assert result.returncode == 0, result.stderr
    assert not built_runtime.marker.exists()


def test_capsule_bootstrap_rejects_duplicate_generation_manifest_keys(
    built_runtime: BuiltRuntime,
):
    raw = built_runtime.generation_manifest.read_text(encoding="utf-8")
    built_runtime.generation_manifest.write_text(
        raw.replace('"format":1', '"format":1,"format":1'), encoding="utf-8"
    )

    _assert_rejected_without_marker(built_runtime)


def test_capsule_bootstrap_rejects_contract_substitution(
    built_runtime: BuiltRuntime,
):
    helper = built_runtime.capsule / "scripts" / "_voice_intent_contract.py"
    helper.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['VOICE_INTENT_UNTRUSTED_MARKER']).write_text('ran')\n",
        encoding="utf-8",
    )

    _assert_rejected_without_marker(built_runtime)


def test_capsule_manifest_is_canonical_and_does_not_hash_itself(
    built_runtime: BuiltRuntime,
):
    manifest_path = built_runtime.capsule / "capsule.json"
    manifest = _manifest_dict(manifest_path)

    assert manifest_path.read_bytes() == canonical_json_bytes(manifest)
    assert "capsule.json" not in manifest["files"]
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == manifest_digest(
        manifest
    )

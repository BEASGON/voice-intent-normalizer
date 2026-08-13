"""Public command-line behavior for the local correction service."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_intent_normalizer.adapters.generic_layout import (
    generation_source_files,
    prepare_versioned_artifacts,
)
from voice_intent_normalizer.learning import LearningEvent
from voice_intent_normalizer.models import (
    CorrectionDecision,
    DecisionAction,
    EntryStatus,
    Scope,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_runtime_files(root: Path, files: dict[str, bytes]) -> Path:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _wheel_bundle(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "site-packages" / "voice_intent_normalizer"
    module = package / "cli.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes((ROOT / "src/voice_intent_normalizer/cli.py").read_bytes())
    bundle = _write_runtime_files(
        package / "_skill_bundle", generation_source_files(ROOT)
    )
    return module, bundle


def _generation_root(tmp_path: Path) -> Path:
    artifact = prepare_versioned_artifacts(ROOT, "0" * 32).generation
    return _write_runtime_files(
        tmp_path / "generations" / artifact.identifier, dict(artifact.files)
    )


def test_runtime_repository_ignores_ambient_parent_skill(monkeypatch, tmp_path: Path):
    from voice_intent_normalizer import cli

    ambient = tmp_path / "ambient"
    (ambient / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (ambient / "SKILL.md").write_text("ambient", encoding="utf-8")
    fake_module, bundle = _wheel_bundle(ambient / "installed")
    monkeypatch.setattr(cli, "__file__", str(fake_module))
    monkeypatch.setattr(cli.resources, "files", lambda package: fake_module.parent)

    assert cli._runtime_repository() == bundle.resolve()


def test_runtime_repository_rejects_resource_lookalike_outside_imported_package(
    monkeypatch, tmp_path: Path
):
    from voice_intent_normalizer import cli

    fake_module, _ = _wheel_bundle(tmp_path / "installed")
    lookalike = _write_runtime_files(
        tmp_path / "resource-lookalike" / "_skill_bundle",
        generation_source_files(ROOT),
    )
    monkeypatch.setattr(cli, "__file__", str(fake_module))
    monkeypatch.setattr(cli.resources, "files", lambda package: lookalike.parent)

    with pytest.raises(RuntimeError, match="bundle"):
        cli._runtime_repository()


def test_runtime_repository_rejects_extra_wheel_bundle_file(
    monkeypatch, tmp_path: Path
):
    from voice_intent_normalizer import cli

    fake_module, bundle = _wheel_bundle(tmp_path / "installed")
    (bundle / "ambient.py").write_text("raise RuntimeError\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(fake_module))
    monkeypatch.setattr(cli.resources, "files", lambda package: fake_module.parent)

    with pytest.raises(RuntimeError, match="bundle"):
        cli._runtime_repository()


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(
            "src/voice_intent_normalizer/__pycache__/ambient."
            f"{sys.implementation.cache_tag}.pyc",
            id="orphan",
        ),
        pytest.param(
            "src/voice_intent_normalizer/__pycache__/cli.cpython-000.pyc",
            id="wrong-cache-tag",
        ),
        pytest.param(
            "src/voice_intent_normalizer/__pycache__/cli."
            f"{sys.implementation.cache_tag}.PYC",
            id="uppercase-extension",
        ),
        pytest.param(
            "src/voice_intent_normalizer/misplaced/__pycache__/cli."
            f"{sys.implementation.cache_tag}.pyc",
            id="misplaced-cache",
        ),
        pytest.param(
            "references/__pycache__/correction-policy."
            f"{sys.implementation.cache_tag}.pyc",
            id="arbitrary-non-python-sibling",
        ),
        pytest.param(
            "src/voice_intent_normalizer/__pycache__/cli."
            f"{sys.implementation.cache_tag}.pyd",
            id="native-loader-extra",
        ),
    ],
)
def test_runtime_repository_rejects_non_allowlisted_wheel_bytecode(
    monkeypatch, tmp_path: Path, relative: str
):
    from voice_intent_normalizer import cli

    fake_module, bundle = _wheel_bundle(tmp_path / "installed")
    artifact = bundle / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"attacker-supplied bytecode")
    monkeypatch.setattr(cli, "__file__", str(fake_module))
    monkeypatch.setattr(cli.resources, "files", lambda package: fake_module.parent)

    with pytest.raises(RuntimeError, match="bundle"):
        cli._runtime_repository()


def test_runtime_repository_rejects_allowlisted_wheel_bytecode_alias(
    monkeypatch, tmp_path: Path
):
    from voice_intent_normalizer import cli

    fake_module, bundle = _wheel_bundle(tmp_path / "installed")
    cache = bundle / "src/voice_intent_normalizer/__pycache__"
    cache.mkdir()
    external = tmp_path / "external.pyc"
    external.write_bytes(b"attacker-supplied bytecode")
    alias = cache / f"cli.{sys.implementation.cache_tag}.pyc"
    try:
        alias.symlink_to(external)
    except OSError as exc:  # pragma: no cover - required alias capability
        pytest.fail(f"test platform cannot create the required bytecode alias: {exc}")
    monkeypatch.setattr(cli, "__file__", str(fake_module))
    monkeypatch.setattr(cli.resources, "files", lambda package: fake_module.parent)

    with pytest.raises(RuntimeError, match="bundle"):
        cli._runtime_repository()


def test_runtime_repository_accepts_only_a_manifest_anchored_generation(
    monkeypatch, tmp_path: Path
):
    from voice_intent_normalizer import cli

    generation = _generation_root(tmp_path)
    module = generation / "src/voice_intent_normalizer/cli.py"
    monkeypatch.setattr(cli, "__file__", str(module))

    assert cli._runtime_repository() == generation.resolve()

    manifest = generation / "generation.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b'"format":1', b'"format":1,"format":1', 1))
    with pytest.raises(RuntimeError, match="generation"):
        cli._runtime_repository()


def test_runtime_repository_rejects_generation_selected_only_by_pythonpath(
    tmp_path: Path,
):
    generation = _generation_root(tmp_path)
    source = generation / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from voice_intent_normalizer import cli; "
                "print(cli._runtime_repository())"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "generation" in result.stderr


class FakeService:
    def __init__(self, decision: CorrectionDecision) -> None:
        self.decision = decision
        self.requests = []

    def normalize(self, request):
        self.requests.append(request)
        return self.decision


@pytest.fixture
def apply_service() -> FakeService:
    return FakeService(
        CorrectionDecision(
            action=DecisionAction.APPLY,
            original_text="使用 code X",
            corrected_text="使用 Codex",
            notices=("已按 Codex 理解。",),
            diagnostics=("optional_pinyin_unavailable",),
        )
    )


def run_cli(args: list[str], service: FakeService | None = None):
    from voice_intent_normalizer.cli import main

    stdout = StringIO()
    stderr = StringIO()
    code = main(args, service=service, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_normalize_json_output_is_stable_and_utf8(apply_service):
    code, stdout, stderr = run_cli(
        [
            "normalize",
            "--text",
            "使用 code X",
            "--domain",
            "software-development",
            "--json",
        ],
        apply_service,
    )

    payload = json.loads(stdout)
    assert code == 0
    assert stderr == ""
    assert list(payload) == [
        "action",
        "original_text",
        "corrected_text",
        "notices",
        "question",
        "diagnostics",
        "candidates",
    ]
    assert payload["action"] == "apply"
    assert payload["corrected_text"] == "使用 Codex"
    assert apply_service.requests[0].domains == ("software-development",)
    assert "使用 Codex" in stdout


def test_normalize_human_mode_only_prints_corrected_text(apply_service):
    code, stdout, stderr = run_cli(
        ["normalize", "--text", "使用 code X"], apply_service
    )

    assert code == 0
    assert stdout == "使用 Codex\n"
    assert stderr == ""


def test_cli_validation_error_uses_stderr_and_exit_two():
    code, stdout, stderr = run_cli(["normalize"])

    assert code == 2
    assert stdout == ""
    assert "--text" in stderr


@pytest.mark.parametrize(
    "command",
    [
        "learn",
        "reject",
        "undo",
        "list",
        "scan-project",
        "update",
        "doctor",
        "install",
        "uninstall",
        "hook",
    ],
)
def test_declared_command_names_are_exposed(command):
    from voice_intent_normalizer.cli import build_parser

    parser = build_parser()
    assert command in parser._subparsers._group_actions[0].choices


def test_bootstrap_imports_the_repository_src_without_package_install(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "voice_intent.py"
    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env={"PYTHONPATH": "", "PATH": str(Path(sys.executable).parent)},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}


def test_checkout_bootstrap_does_not_require_capsule_state(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "voice_intent.py"

    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env={
            "PYTHONPATH": "",
            "PATH": str(Path(sys.executable).parent),
            "VOICE_INTENT_HOME": str(tmp_path / "absent-state"),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}


def test_bootstrap_prefers_its_own_src_over_an_earlier_pythonpath_package(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "voice_intent.py"
    evil = tmp_path / "evil" / "voice_intent_normalizer"
    evil.mkdir(parents=True)
    (evil / "__init__.py").write_text("", encoding="utf-8")
    (evil / "cli.py").write_text(
        "def main():\n    return 99\n", encoding="utf-8"
    )
    environment = {
        "PYTHONPATH": os.pathsep.join(
            (str(tmp_path / "evil"), str(repository / "src"))
        ),
        "PATH": str(Path(sys.executable).parent),
    }

    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}


def test_bootstrap_discards_a_preloaded_shadow_package_from_sitecustomize(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "voice_intent.py"
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "sitecustomize.py").write_text(
        "import sys\n"
        "import types\n"
        "package = types.ModuleType('voice_intent_normalizer')\n"
        "package.__path__ = []\n"
        "cli = types.ModuleType('voice_intent_normalizer.cli')\n"
        "cli.main = lambda: 99\n"
        "sys.modules['voice_intent_normalizer'] = package\n"
        "sys.modules['voice_intent_normalizer.cli'] = cli\n",
        encoding="utf-8",
    )
    environment = {
        "PYTHONPATH": os.pathsep.join((str(evil), str(repository / "src"))),
        "PATH": str(Path(sys.executable).parent),
    }

    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"ok", "degraded"}


@pytest.fixture
def bootstrap_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "voice_intent.py"
    spec = importlib.util.spec_from_file_location("test_voice_intent_bootstrap", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("candidate", "root", "expected"),
    [
        ("/repo/src/voice_intent_normalizer/cli.py", "/repo/src", True),
        ("/repo/src", "/repo/src", True),
        ("/repo/src-sibling/cli.py", "/repo/src", False),
        ("/repo/src/../secret.py", "/repo/src", False),
        ("/repo/SRC/cli.py", "/repo/src", False),
    ],
)
def test_bootstrap_posix_containment_is_lexical_and_case_sensitive(
    bootstrap_module, candidate, root, expected
):
    assert (
        bootstrap_module._is_below(candidate, root, flavor="posix") is expected
    )


@pytest.mark.parametrize(
    ("candidate", "root", "expected"),
    [
        (r"C:\Repo\src\voice_intent_normalizer\cli.py", r"C:\Repo\src", True),
        (r"C:/REPO/src/voice_intent_normalizer/cli.py", r"C:\Repo\src", True),
        (r"\\?\C:\repo\src\voice_intent_normalizer\cli.py", r"C:\Repo\src", True),
        (r"\\?\UNC\server\share\src\cli.py", r"\\server\share\src", True),
        (r"\\?\UNC\server\other\src\cli.py", r"\\server\share\src", False),
        (r"\\?\UNC\server\share\src\..\secret.py", r"\\server\share\src", False),
        (r"C:\Repo\src-sibling\cli.py", r"C:\Repo\src", False),
        (r"D:\Repo\src\cli.py", r"C:\Repo\src", False),
        (r"\\server\share\src\cli.py", r"C:\Repo\src", False),
    ],
)
def test_bootstrap_windows_containment_normalizes_equivalent_paths(
    bootstrap_module, candidate, root, expected
):
    assert (
        bootstrap_module._is_below(candidate, root, flavor="windows") is expected
    )


def _bootstrap_test_repository(tmp_path):
    repository = tmp_path / "repository"
    script_source = Path(__file__).resolve().parents[1] / "scripts" / "voice_intent.py"
    script_target = repository / "scripts" / "voice_intent.py"
    script_target.parent.mkdir(parents=True)
    shutil.copyfile(script_source, script_target)
    return repository, script_target


@pytest.mark.parametrize("linked_target", ["package", "cli-file"])
def test_bootstrap_rejects_physical_cli_origins_outside_trusted_src(
    tmp_path, linked_target
):
    repository, script = _bootstrap_test_repository(tmp_path)
    source = repository / "src"
    external = tmp_path / "external"
    package = external / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "external-main-ran"
    (package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('ran')\n"
        "    return 99\n",
        encoding="utf-8",
    )
    source.mkdir(parents=True)
    if linked_target == "package":
        try:
            (source / "voice_intent_normalizer").symlink_to(
                package, target_is_directory=True
            )
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    else:
        trusted_package = source / "voice_intent_normalizer"
        trusted_package.mkdir()
        (trusted_package / "__init__.py").write_text("", encoding="utf-8")
        try:
            (trusted_package / "cli.py").symlink_to(package / "cli.py")
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")

    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "VOICE_INTENT_EXTERNAL_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "alias_kind", ["src-root", "init-file", "helper-file", "helper-directory"]
)
def test_bootstrap_preflight_rejects_transitive_python_aliases_before_import(
    tmp_path, alias_kind
):
    repository, script = _bootstrap_test_repository(tmp_path)
    source = repository / "src"
    marker = tmp_path / "external-import-ran"
    external = tmp_path / "external"
    external.mkdir()

    if alias_kind == "src-root":
        external_source = external / "src"
        package = external_source / "voice_intent_normalizer"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "cli.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('imported')\n"
            "def main():\n    return 0\n",
            encoding="utf-8",
        )
        try:
            source.symlink_to(external_source, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    elif alias_kind == "helper-directory":
        package = source / "voice_intent_normalizer"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "cli.py").write_text(
            "from . import helper\n\ndef main():\n    return 0\n",
            encoding="utf-8",
        )
        external_helper = external / "helper"
        external_helper.mkdir()
        (external_helper / "__init__.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('imported')\n",
            encoding="utf-8",
        )
        try:
            (package / "helper").symlink_to(external_helper, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    else:
        package = source / "voice_intent_normalizer"
        package.mkdir(parents=True)
        (package / "cli.py").write_text(
            "from . import helper\n\ndef main():\n    return 0\n",
            encoding="utf-8",
        )
        external_file = external / (
            "__init__.py" if alias_kind == "init-file" else "helper.py"
        )
        external_file.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('imported')\n",
            encoding="utf-8",
        )
        if alias_kind == "init-file":
            (package / "helper.py").write_text("", encoding="utf-8")
            link = package / "__init__.py"
        else:
            (package / "__init__.py").write_text("", encoding="utf-8")
            link = package / "helper.py"
        try:
            link.symlink_to(external_file)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")

    result = subprocess.run(
        [sys.executable, str(script), "doctor", "--json"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "VOICE_INTENT_EXTERNAL_MARKER": str(marker),
            "PYTHONPATH": os.pathsep.join(()),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize("bytecode_kind", ["unchecked", "symlink"])
def test_bootstrap_does_not_execute_existing_cli_bytecode(
    tmp_path, bytecode_kind
):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    Path(os.environ['VOICE_INTENT_SOURCE_MARKER']).write_text('trusted')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    malicious_source = tmp_path / "malicious_cli.py"
    malicious_source.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('malicious')\n"
        "def main():\n    return 99\n",
        encoding="utf-8",
    )
    cache = Path(importlib.util.cache_from_source(str(package / "cli.py")))
    cache.parent.mkdir()
    external_marker = tmp_path / "external-bytecode-ran"
    source_marker = tmp_path / "source-ran"
    if bytecode_kind == "unchecked":
        py_compile.compile(
            str(malicious_source),
            cfile=str(cache),
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
    else:
        external_pyc = tmp_path / "malicious_cli.pyc"
        py_compile.compile(
            str(malicious_source),
            cfile=str(external_pyc),
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        try:
            cache.symlink_to(external_pyc)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    original_cache = cache.read_bytes()

    environment = {
        "PATH": str(Path(sys.executable).parent),
        "VOICE_INTENT_EXTERNAL_MARKER": str(external_marker),
        "VOICE_INTENT_SOURCE_MARKER": str(source_marker),
    }
    first = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    second = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert first.returncode == 0
    assert second.returncode == 0
    assert source_marker.read_text(encoding="utf-8") == "trusted"
    assert not external_marker.exists()
    assert cache.read_bytes() == original_cache


def test_bootstrap_rejects_missing_init_before_namespace_package_merges(tmp_path):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "cli.py").write_text(
        "from . import helper\n\ndef main():\n    return 0\n", encoding="utf-8"
    )
    evil_root = tmp_path / "evil"
    evil_package = evil_root / "voice_intent_normalizer"
    evil_package.mkdir(parents=True)
    marker = tmp_path / "namespace-helper-ran"
    (evil_package / "helper.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['VOICE_INTENT_EXTERNAL_MARKER']).write_text('ran')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": os.pathsep.join((str(evil_root),)),
            "VOICE_INTENT_EXTERNAL_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_bootstrap_rejects_cli_extension_before_native_loader_runs(tmp_path):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    Path(os.environ['VOICE_INTENT_SOURCE_MARKER']).write_text('trusted')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    extension = package / f"cli{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    extension.write_bytes(b"not a native module")
    marker = tmp_path / "source-ran"

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "VOICE_INTENT_SOURCE_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "DLL load failed" not in result.stderr


def test_bootstrap_rejects_helper_extension_before_cli_import(tmp_path):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "from . import helper\n\ndef main():\n    return 0\n", encoding="utf-8"
    )
    extension = package / f"helper{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    extension.write_bytes(b"not a native module")

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert "DLL load failed" not in result.stderr


@pytest.mark.parametrize(
    "suffix",
    [
        ".PYD",
        "".join(
            character.upper() if index % 2 else character.lower()
            for index, character in enumerate(
                max(importlib.machinery.EXTENSION_SUFFIXES, key=len)
            )
        ),
    ],
)
@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows FileFinder treats artifact suffixes case-insensitively",
)
def test_bootstrap_rejects_windows_case_variant_cli_extensions(tmp_path, suffix):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    Path(os.environ['VOICE_INTENT_SOURCE_MARKER']).write_text('trusted')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (package / f"cli{suffix}").write_bytes(b"not a native module")
    marker = tmp_path / "source-ran"

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "VOICE_INTENT_SOURCE_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "DLL load failed" not in result.stderr


@pytest.mark.parametrize("artifact", ["helper.PYC", "helper.PYO"])
@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows FileFinder treats artifact suffixes case-insensitively",
)
def test_bootstrap_rejects_windows_case_variant_helper_bytecode(tmp_path, artifact):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (package / artifact).write_bytes(b"sourceless bytecode")

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0


@pytest.mark.parametrize("artifact", ["cli.PYD", "helper.PYC", "helper.PYO"])
@pytest.mark.skipif(
    os.name == "nt", reason="POSIX keeps differently cased artifact names distinct"
)
def test_bootstrap_accepts_posix_case_distinct_artifact_names(tmp_path, artifact):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    Path(os.environ['VOICE_INTENT_SOURCE_MARKER']).write_text('trusted')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (package / artifact).write_bytes(b"harmless case-distinct artifact")
    marker = tmp_path / "source-ran"

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": os.pathsep.join(()),
            "VOICE_INTENT_SOURCE_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "trusted"


@pytest.mark.parametrize("suffix", importlib.machinery.EXTENSION_SUFFIXES)
def test_loader_artifact_matching_preserves_platform_case_rules(
    bootstrap_module, suffix
):
    case_variant = suffix.swapcase()

    assert bootstrap_module._is_loader_artifact(
        f"module{case_variant}", flavor="windows"
    )
    assert not bootstrap_module._is_loader_artifact(
        f"module{case_variant}", flavor="posix"
    )
    assert bootstrap_module._is_loader_artifact("helper.PYC", flavor="windows")
    assert not bootstrap_module._is_loader_artifact("helper.PYC", flavor="posix")
    assert bootstrap_module._is_loader_artifact("helper.PYO", flavor="windows")
    assert not bootstrap_module._is_loader_artifact("helper.PYO", flavor="posix")


@pytest.mark.parametrize("artifact", ["cli.pyc", "helper.pyo"])
def test_bootstrap_rejects_top_level_sourceless_bytecode(tmp_path, artifact):
    repository, script = _bootstrap_test_repository(tmp_path)
    package = repository / "src" / "voice_intent_normalizer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (package / artifact).write_bytes(b"sourceless bytecode")

    result = subprocess.run(
        [sys.executable, str(script), "doctor"],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0


def test_bytecode_isolation_restores_interpreter_flags_and_removes_cache(
    bootstrap_module
):
    original_prefix = sys.pycache_prefix
    original_flag = sys.dont_write_bytecode
    with bootstrap_module._bytecode_isolation() as cache:
        cache_path = Path(cache)
        assert not cache_path.exists()
        assert sys.pycache_prefix == str(cache_path)
        assert sys.dont_write_bytecode is True

    assert sys.pycache_prefix == original_prefix
    assert sys.dont_write_bytecode is original_flag
    assert not cache_path.exists()


def test_bytecode_isolation_hides_temporary_directory_creation_failures(
    bootstrap_module, monkeypatch
):
    def broken_mkdtemp(**kwargs):
        raise OSError("secret temporary location")

    monkeypatch.setattr(
        bootstrap_module.tempfile,
        "mkdtemp",
        broken_mkdtemp,
    )

    with pytest.raises(RuntimeError) as exc_info:
        with bootstrap_module._bytecode_isolation():
            raise AssertionError("isolation must not yield")

    assert str(exc_info.value) == "trusted repository import isolation unavailable"
    assert "secret temporary location" not in str(exc_info.value)


def test_bytecode_isolation_hides_cache_removal_failures(
    bootstrap_module, monkeypatch, tmp_path
):
    cache = tmp_path / "private-cache"
    cache.mkdir()
    native_rmdir = os.rmdir

    def remove_then_fail(path):
        native_rmdir(path)
        raise OSError("secret temporary location")

    original_prefix = sys.pycache_prefix
    original_flag = sys.dont_write_bytecode
    monkeypatch.setattr(
        bootstrap_module.tempfile,
        "mkdtemp",
        lambda **kwargs: str(cache),
    )
    monkeypatch.setattr(bootstrap_module.os, "rmdir", remove_then_fail)

    with pytest.raises(RuntimeError) as exc_info:
        with bootstrap_module._bytecode_isolation():
            pass

    assert str(exc_info.value) == "trusted repository import isolation unavailable"
    assert "secret temporary location" not in str(exc_info.value)
    assert sys.pycache_prefix == original_prefix
    assert sys.dont_write_bytecode is original_flag
    assert not cache.exists()


def test_bytecode_isolation_restores_flags_after_body_failure(bootstrap_module):
    original_prefix = sys.pycache_prefix
    original_flag = sys.dont_write_bytecode
    with pytest.raises(ValueError, match="body failure"):
        with bootstrap_module._bytecode_isolation() as cache:
            cache_path = Path(cache)
            assert not cache_path.exists()
            raise ValueError("body failure")

    assert sys.pycache_prefix == original_prefix
    assert sys.dont_write_bytecode is original_flag
    assert not cache_path.exists()


def test_runtime_value_error_degrades_instead_of_becoming_validation_error(tmp_path):
    class BrokenLearning:
        def list_recent(self, limit):
            raise ValueError("corrupt learning-events.jsonl")

    service = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path), learning=BrokenLearning()
    )
    code, stdout, stderr = run_cli(["list", "--json"], service)

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "status": "degraded",
        "diagnostics": ["local_operation_unavailable"],
    }


def test_human_management_commands_explain_their_result(tmp_path):
    class Learning:
        def list_recent(self, limit):
            return [
                LearningEvent(
                    event_id="00000000-0000-0000-0000-000000000001",
                    timestamp="2026-07-30T00:00:00.000000Z",
                    action="confirm",
                    alias="code X",
                    canonical="Codex",
                    status=EntryStatus.CONFIRMED,
                    scope=Scope.PERSONAL,
                )
            ]

    service = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path), learning=Learning()
    )
    code, stdout, _ = run_cli(["list"], service)
    assert code == 0
    assert "code X" in stdout
    assert "Codex" in stdout

    code, stdout, _ = run_cli(["doctor"], service)
    assert code == 0
    assert "Status:" in stdout
    assert str(tmp_path) in stdout


def test_default_installer_registers_every_supported_platform(tmp_path):
    from voice_intent_normalizer import cli

    installer = cli.default_installer(
        cli.StatePaths(root=tmp_path / "voice-intent-state")
    )

    assert installer.platforms == ("codex", "openclaw", "workbuddy", "generic")


def test_default_installer_reports_openclaw_unavailable_without_its_cli(
    monkeypatch, tmp_path
):
    from voice_intent_normalizer import cli

    monkeypatch.setattr(cli, "_runtime_repository", lambda: ROOT)
    installer = cli.default_installer(
        cli.StatePaths(root=tmp_path / "voice-intent-state")
    )

    result = installer.doctor(("openclaw",))[0]

    assert result.status == "unavailable"


def test_cli_install_and_doctor_support_workbuddy(tmp_path, monkeypatch):
    from voice_intent_normalizer import cli

    monkeypatch.setattr(cli, "_runtime_repository", lambda: ROOT)
    service = SimpleNamespace(paths=cli.StatePaths(root=tmp_path / "state"))

    code, stdout, stderr = run_cli(
        [
            "install",
            "--platform",
            "workbuddy",
            "--output-dir",
            str(tmp_path / "output"),
            "--no-auto-update",
            "--json",
        ],
        service,
    )

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)[0]["status"] == "package-created"

    code, stdout, stderr = run_cli(
        ["doctor", "--platform", "workbuddy", "--json"], service
    )

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)[0]["status"] == "manual-action-required"


def test_human_scan_project_prints_a_concise_summary(tmp_path, monkeypatch):
    from voice_intent_normalizer import cli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        cli,
        "scan_project",
        lambda authority, paths: SimpleNamespace(
            entries=(object(), object()),
            truncated=True,
            files_scanned=3,
        ),
    )
    service = SimpleNamespace(paths=SimpleNamespace(root=tmp_path))

    code, stdout, stderr = run_cli(
        ["scan-project", "--project-root", str(workspace)], service
    )

    assert code == 0
    assert stderr == ""
    assert stdout == "Scanned 3 files; found 2 terms (truncated).\n"

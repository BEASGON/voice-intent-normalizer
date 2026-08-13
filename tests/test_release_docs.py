from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from voice_intent_normalizer.adapters.generic_layout import generation_source_files

ROOT = Path(__file__).resolve().parents[1]


def test_readme_follows_the_public_release_journey():
    text = (ROOT / "README.md").read_text("utf-8").casefold()
    headings = (
        "## the problem",
        "## post-submission boundary",
        "## 60-second quick start",
        "## platform compatibility",
        "## correction receipt",
        "## natural-language learning",
        "## privacy",
        "## hotword updates",
        "## troubleshooting",
        "## development",
    )
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_security_and_compatibility_document_required_safety_boundaries():
    security = (ROOT / "SECURITY.md").read_text("utf-8").casefold()
    compatibility = (
        ROOT / "references" / "platform-compatibility.md"
    ).read_text("utf-8").casefold()
    for phrase in (
        "project scan exclusions",
        "update validation",
        "hook trust",
        "permissions",
        "vulnerability report",
    ):
        assert phrase in security
    for level in ("automatic", "implicit", "manual"):
        assert level in compatibility


def test_v010_docs_do_not_claim_unconfigured_online_hotword_updates():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "SECURITY.md",
            ROOT / "adapters" / "generic" / "README.zh-CN.md",
            ROOT / "adapters" / "openclaw" / "README.zh-CN.md",
        )
    )
    assert "--auto-update" not in combined
    assert "--no-auto-update" not in combined
    assert "signature validation" not in combined


def test_release_sources_exclude_private_state_and_validate_jsonl():
    forbidden = (
        ".env",
        "token",
        "personal.jsonl",
        "projects/",
        "tests/",
        ".git/",
        ".superpowers/",
        ".pytest_cache/",
        "build/",
        "dist/",
    )
    sources = generation_source_files(ROOT)
    assert not any(
        any(marker in relative.casefold() for marker in forbidden)
        for relative in sources
    )
    for path in (ROOT / "assets" / "lexicons").rglob("*.jsonl"):
        for line_number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            assert isinstance(json.loads(line), dict), (path, line_number)


def test_workbuddy_archive_contains_no_private_or_absolute_members(tmp_path):
    from voice_intent_normalizer.adapters.base import InstallOptions
    from voice_intent_normalizer.adapters.workbuddy import WorkBuddyAdapter
    from voice_intent_normalizer.paths import StatePaths

    adapter = WorkBuddyAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    result = adapter.install(InstallOptions(output_dir=tmp_path / "output"))
    assert result.status == "package-created"
    archive = tmp_path / "output" / "voice-intent-normalizer-workbuddy.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = tuple(bundle.namelist())
    assert names == tuple(sorted(names))
    assert not any(
        name.startswith(("/", "C:", "c:"))
        or any(
            marker in name.casefold()
            for marker in (
                ".env",
                "token",
                "personal.jsonl",
                "tests/",
                ".git/",
                ".superpowers/",
                "build/",
                "dist/",
            )
        )
        for name in names
    )


def test_release_cli_smokes_clean_platform_homes_and_normalization(tmp_path):
    repository = ROOT
    script = repository / "scripts" / "voice_intent.py"
    environment = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "VOICE_INTENT_HOME": str(tmp_path / "state"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
    }
    if sys.platform == "win32":
        import os

        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    def invoke(*arguments: str) -> object:
        result = subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    normalized = invoke(
        "normalize",
        "--text",
        "帮我适配 open cloud 的技能",
        "--domain",
        "ai",
        "--json",
    )
    codex = invoke(
        "install",
        "--platform",
        "codex",
        "--json",
    )[0]
    codex_doctor = invoke("doctor", "--platform", "codex", "--json")[0]
    openclaw = invoke("doctor", "--platform", "openclaw", "--json")[0]
    workbuddy = invoke(
        "install",
        "--platform",
        "workbuddy",
        "--output-dir",
        str(tmp_path / "workbuddy-output"),
        "--json",
    )[0]

    assert normalized["corrected_text"] == "帮我适配 OpenClaw 的技能"
    assert codex["status"] == "installed"
    assert codex_doctor["status"] == "installed"
    assert openclaw["status"] == "unavailable"
    assert workbuddy["status"] == "package-created"
    assert workbuddy["capability"] == "manual"
    archive = tmp_path / "workbuddy-output" / "voice-intent-normalizer-workbuddy.zip"
    sidecar = archive.with_suffix(".zip.sha256")
    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sidecar.read_text("ascii") == f"{expected_digest}\n"


def test_built_release_archives_exclude_private_material(tmp_path):
    command = [
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--outdir",
        str(tmp_path / "dist"),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    forbidden = (
        ".env",
        "token",
        "personal.jsonl",
        "projects/",
        "tests/",
        ".git/",
        ".superpowers/",
        ".pytest_cache/",
        "build/",
        "dist/",
    )
    wheel = next((tmp_path / "dist").glob("*.whl"))
    source = next((tmp_path / "dist").glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(source, "r:gz") as archive:
        source_names = archive.getnames()
    for name in (*wheel_names, *source_names):
        assert not any(marker in name.casefold() for marker in forbidden)

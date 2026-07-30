"""Public command-line behavior for the local correction service."""

from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_intent_normalizer.learning import LearningEvent
from voice_intent_normalizer.models import (
    CorrectionDecision,
    DecisionAction,
    EntryStatus,
    Scope,
)


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
        "PYTHONPATH": str(tmp_path / "evil") + ";" + str(repository / "src"),
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

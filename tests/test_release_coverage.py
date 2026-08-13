"""Release-contract coverage for public failures and recoveries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_intent_normalizer.adapters.base import (
    AdapterResult,
    CapabilityLevel,
    InstallOptions,
    UninstallOptions,
)
from voice_intent_normalizer.adapters.codex import CodexAdapter
from voice_intent_normalizer.adapters.openclaw import OpenClawAdapter
from voice_intent_normalizer.adapters.workbuddy import WorkBuddyAdapter
from voice_intent_normalizer.cli import main
from voice_intent_normalizer.installer import Installer
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.updater import UpdateStatus, update_hotwords

ROOT = Path(__file__).resolve().parents[1]


class _OpenClawRun:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return self.replies[args]


def _openclaw_check(skills, *, returncode=0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps({"skills": skills}),
    )


def test_installer_isolates_adapter_failure_without_skipping_later_platform():
    class BrokenAdapter:
        platform = "broken"

        def detect(self):
            raise OSError("unavailable")

        def install(self, options):
            raise OSError("unavailable")

    class WorkingAdapter:
        platform = "working"

        def detect(self):
            return AdapterResult(
                self.platform, "detected", CapabilityLevel.MANUAL
            )

        def install(self, options):
            return AdapterResult(
                self.platform, "installed", CapabilityLevel.MANUAL
            )

    results = Installer(
        {"broken": BrokenAdapter(), "working": WorkingAdapter()}
    ).install(("broken", "working"), InstallOptions())

    assert [result.status for result in results] == ["degraded", "installed"]


def test_workbuddy_declines_strict_and_shared_data_deletion(tmp_path):
    adapter = WorkBuddyAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    strict = adapter.install(InstallOptions(strict=True))
    removal = adapter.uninstall(UninstallOptions(remove_shared_data=True))

    assert strict.status == "failed"
    assert removal.status == "failed"


def test_openclaw_does_not_treat_codex_skills_as_its_source(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    (codex_home / "skills").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = OpenClawAdapter(
        codex_home / "skills",
        StatePaths(root=tmp_path / "state"),
    )

    result = adapter.install(InstallOptions())

    assert result.status == "failed"


def test_codex_strict_install_and_uninstall_preserve_shared_state(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    installed = adapter.install(InstallOptions(strict=True))
    doctor = adapter.doctor()
    removed = adapter.uninstall(UninstallOptions())

    assert installed.status == "installed"
    assert doctor.capability is CapabilityLevel.AUTOMATIC
    assert removed.status == "uninstalled"


def test_cli_json_install_uses_offline_update_policy(tmp_path):
    service = type("Service", (), {"paths": StatePaths(root=tmp_path / "state")})()
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["install", "--platform", "workbuddy", "--json"],
        service=service,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert json.loads(stdout.getvalue())[0]["status"] == "package-created"
    assert stderr.getvalue() == ""


def test_update_rejects_untrusted_manifest_before_transport(tmp_path):
    class NoNetwork:
        def open_no_redirect(self, url):
            raise AssertionError("untrusted URL must not be fetched")

    result = update_hotwords(
        StatePaths(root=tmp_path / "state"),
        "https://invalid.example/manifest.json",
        NoNetwork(),
        datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    assert result.status is UpdateStatus.REJECTED
    assert result.message == "manifest source rejected"


@pytest.mark.parametrize(
    ("skills", "status"),
    [
        ([], "not-installed"),
        ([{"name": "voice-intent-normalizer", "eligible": False}], "not-installed"),
        ([{"name": "voice-intent-normalizer", "eligible": True}], "installed"),
    ],
)
def test_openclaw_doctor_reports_verified_eligibility(tmp_path, skills, status):
    command = ("openclaw", "skills", "check", "--json")
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun({command: _openclaw_check(skills)}),
    )

    result = adapter.doctor()

    assert result.status == status


def test_openclaw_doctor_degrades_for_invalid_official_result(tmp_path):
    command = ("openclaw", "skills", "check", "--json")
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun(
            {command: SimpleNamespace(returncode=0, stdout='{"skills": {}}')}
        ),
    )

    assert adapter.doctor().status == "degraded"


def test_openclaw_uninstall_without_receipt_is_manual_and_non_destructive(tmp_path):
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun({}),
    )

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "not-installed"
    assert "manual fallback" in result.messages[0]


@pytest.mark.parametrize(
    ("prepare", "status"),
    [
        (lambda home: None, "not-installed"),
        (lambda home: (home / "AGENTS.md").write_text("user rules\n"), "degraded"),
    ],
)
def test_codex_doctor_distinguishes_absent_and_incomplete_installation(
    tmp_path, monkeypatch, prepare, status
):
    home = tmp_path / "codex-home"
    home.mkdir()
    prepare(home)
    monkeypatch.setenv("CODEX_HOME", str(home))

    result = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state")).doctor()

    assert result.status == status


def test_codex_uninstall_refuses_shared_data_removal(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    result = adapter.uninstall(UninstallOptions(remove_shared_data=True))

    assert result.status == "failed"


def test_codex_strict_install_rejects_malformed_hook_document(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "hooks.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    result = adapter.install(InstallOptions(strict=True))

    assert result.status == "failed"
    assert not (home / "skills" / "voice-intent-normalizer").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--platform", "missing", "--json"],
        [
            "uninstall",
            "--platform",
            "workbuddy",
            "--output-dir",
            "out",
            "--json",
        ],
    ],
)
def test_cli_json_management_commands_return_adapter_results(tmp_path, argv):
    service = type("Service", (), {"paths": StatePaths(root=tmp_path / "state")})()
    stdout = StringIO()

    code = main(argv, service=service, stdout=stdout, stderr=StringIO())

    assert code == 0
    assert json.loads(stdout.getvalue())[0]["platform"] in {
        "missing",
        "workbuddy",
    }


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"text": 3}, TypeError),
        ({"text": "x", "domains": ("x",) * 33}, ValueError),
        ({"text": "x", "notified_pairs": {("a", 3)}}, ValueError),
    ],
)
def test_normalize_request_rejects_invalid_public_inputs(kwargs, exception):
    from voice_intent_normalizer.service import NormalizeRequest

    with pytest.raises(exception):
        NormalizeRequest(**kwargs)


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (None, "degraded"),
        ({"returncode": "zero", "stdout": ""}, "degraded"),
        ({"returncode": 1, "stdout": ""}, "degraded"),
        ({"returncode": 0, "stdout": "[]"}, "degraded"),
        (
            {
                "returncode": 0,
                "stdout": '{"skills":[{"name":"voice-intent-normalizer"},'
                '{"name":"voice-intent-normalizer","eligible":true}]}',
            },
            "degraded",
        ),
    ],
)
def test_openclaw_doctor_fails_closed_for_malformed_cli_contract(
    tmp_path, reply, status
):
    command = ("openclaw", "skills", "check", "--json")
    adapter = OpenClawAdapter(
        ROOT,
        StatePaths(root=tmp_path / "state"),
        run=_OpenClawRun({command: reply}),
    )

    assert adapter.doctor().status == status


def test_openclaw_official_uninstall_requires_exact_receipt_target(tmp_path):
    target = tmp_path / "target"
    check = ("openclaw", "skills", "check", "--json")
    help_command = ("openclaw", "skills", "--help")
    uninstall = ("openclaw", "skills", "uninstall", "voice-intent-normalizer")
    calls = _OpenClawRun(
        {
            check: _openclaw_check(
                [
                    {
                        "name": "voice-intent-normalizer",
                        "eligible": True,
                        "path": str(target),
                    }
                ]
            ),
            help_command: SimpleNamespace(returncode=0, stdout="install uninstall"),
            uninstall: SimpleNamespace(returncode=0, stdout=""),
        }
    )
    adapter = OpenClawAdapter(
        ROOT, StatePaths(root=tmp_path / "state"), run=calls
    )
    source = adapter._source()
    adapter._write_receipt(
        adapter._receipt_payload(
            source=source,
            source_digest=adapter._source_digest(source),
            workspace=None,
            target=str(target),
        )
    )
    calls.replies[check] = _openclaw_check([])

    result = adapter.uninstall(UninstallOptions())

    assert result.status == "degraded"
    assert uninstall not in calls.calls
    assert adapter._receipt_path().exists()


def test_openclaw_declines_strict_install_and_shared_data_deletion(tmp_path):
    adapter = OpenClawAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    assert adapter.install(InstallOptions(strict=True)).status == "failed"
    assert (
        adapter.uninstall(UninstallOptions(remove_shared_data=True)).status
        == "failed"
    )


def test_service_control_journey_lists_confirms_rejects_and_undoes(tmp_path):
    from voice_intent_normalizer.service import NormalizerService

    service = NormalizerService(
        StatePaths(root=tmp_path / "state"), ROOT / "assets" / "lexicons"
    )
    assert service.apply_control("ordinary text") is None
    empty = service.apply_control("查看最近学到的词")
    confirmed = service.apply_control("我说的是 WorkBuddy，不是 work body")
    rejected = service.apply_control("不要把 open cloud 改成 OpenClaw")
    listed = service.apply_control("查看最近学到的词")
    undone = service.apply_control("撤销刚才的纠正")
    delete = service.apply_control("删除你学到的这个词")

    assert empty is not None and "0" in empty.message
    assert confirmed is not None and confirmed.event is not None
    assert rejected is not None and rejected.event is not None
    assert listed is not None and "2" in listed.message
    assert undone is not None and undone.event is not None
    assert delete is not None and delete.event is None


def test_cli_human_install_prompts_for_platforms_only(tmp_path):
    class RecordingInstaller:
        def install(self, platforms, options):
            assert tuple(platforms) == ("workbuddy",)
            assert options.auto_update is False
            return (
                AdapterResult(
                    "workbuddy", "package-created", CapabilityLevel.MANUAL
                ),
            )

    stdout = StringIO()
    code = main(
        ["install"],
        service=SimpleNamespace(paths=StatePaths(root=tmp_path / "state")),
        installer=RecordingInstaller(),
        input_func=lambda _prompt: "workbuddy",
        stdout=stdout,
    )

    assert code == 0
    assert "package-created" in stdout.getvalue()


def test_cli_public_learning_journey_returns_stable_json(tmp_path):
    from voice_intent_normalizer.service import NormalizerService

    service = NormalizerService(
        StatePaths(root=tmp_path / "state"), ROOT / "assets" / "lexicons"
    )

    def invoke(*argv):
        output = StringIO()
        assert main([*argv, "--json"], service=service, stdout=output) == 0
        return json.loads(output.getvalue())

    learned = invoke(
        "learn", "--alias", "work body", "--canonical", "WorkBuddy"
    )
    rejected = invoke(
        "reject", "--alias", "open cloud", "--canonical", "OpenClaw"
    )
    listed = invoke("list", "--limit", "10")
    undone = invoke("undo")
    degraded = invoke("update")

    assert learned["event"]["status"] == "confirmed"
    assert rejected["event"]["status"] == "rejected"
    assert len(listed["events"]) == 2
    assert undone["event"]["canonical"] == "OpenClaw"
    assert degraded["status"] == "degraded"


def test_cli_validates_learning_and_management_argument_combinations(tmp_path):
    service = SimpleNamespace(paths=StatePaths(root=tmp_path / "state"))
    invalid = (
        ["list", "--limit", "0"],
        [
            "learn",
            "--alias",
            "a",
            "--canonical",
            "b",
            "--scope",
            "project",
        ],
        ["install", "--json"],
        ["doctor", "--platform", "codex", "--all-detected", "--json"],
    )
    for argv in invalid:
        errors = StringIO()
        assert main(argv, service=service, stderr=errors) == 2
        assert errors.getvalue().startswith("voice-intent:")


def test_cli_degrades_unexpected_local_operation_without_traceback(tmp_path):
    class BrokenService:
        paths = StatePaths(root=tmp_path / "state")

        def normalize(self, request):
            raise OSError("offline")

    output = StringIO()
    code = main(
        ["normalize", "--text", "hello", "--json"],
        service=BrokenService(),
        stdout=output,
    )

    assert code == 0
    assert json.loads(output.getvalue()) == {
        "status": "degraded",
        "diagnostics": ["local_operation_unavailable"],
    }


def test_openclaw_rejects_unsafe_receipts_and_source_entries(tmp_path, monkeypatch):
    adapter = OpenClawAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    receipt = adapter._receipt_path()
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"{}")
    assert adapter.doctor().status in {"unavailable", "degraded"}
    assert adapter.uninstall(UninstallOptions()).status == "failed"

    source = tmp_path / "unsafe-source"
    source.mkdir()
    alias = source / "alias"
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("platform cannot create the required file alias")
    with pytest.raises(ValueError, match="alias"):
        adapter._tree_bytes(source)


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"format":1,"scope":"global","source":"x",'
        b'"source_digest":"bad","target":null}',
        b'{"format":1,"format":1,"scope":"global",'
        b'"source":"x","source_digest":"' + b"0" * 64 + b'","target":null}',
    ],
)
def test_openclaw_receipt_parser_rejects_noncanonical_ownership(payload, tmp_path):
    adapter = OpenClawAdapter(ROOT, StatePaths(root=tmp_path / "state"))

    with pytest.raises(ValueError, match="receipt|JSON"):
        adapter._validate_receipt(payload)


def test_codex_rejects_malformed_agents_and_hook_structures(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    agents = home / "AGENTS.md"
    agents.write_text(
        "<!-- VOICE-INTENT-NORMALIZER:BEGIN -->\n", encoding="utf-8"
    )
    assert adapter.install(InstallOptions()).status == "failed"

    agents.unlink()
    hooks = home / "hooks.json"
    hooks.write_text('{"hooks":{"UserPromptSubmit":{}}}', encoding="utf-8")
    assert adapter.install(InstallOptions(strict=True)).status == "failed"


def test_codex_install_reports_runtime_failure_without_configuration_mutation(
    tmp_path, monkeypatch
):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    monkeypatch.setattr(
        adapter,
        "_runtime",
        lambda: SimpleNamespace(
            install=lambda _options: AdapterResult(
                "generic", "failed", CapabilityLevel.UNAVAILABLE
            )
        ),
    )

    result = adapter.install(InstallOptions())

    assert result.status == "failed"
    assert not (home / "AGENTS.md").exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"canonical": ""}, "canonical"),
        ({"aliases": ()}, "aliases"),
        ({"weight": True}, "weight"),
        ({"project_id": 3}, "project_id"),
        ({"source": 3}, "source"),
        ({"use_count": -1}, "use_count"),
        ({"notes": 3}, "notes"),
    ],
)
def test_public_lexicon_model_rejects_invalid_typed_fields(overrides, message):
    from voice_intent_normalizer.models import EntryStatus, LexiconEntry, Scope

    values = {
        "canonical": "Codex",
        "scope": Scope.BASE,
        "aliases": ("code X",),
        "domains": (),
        "weight": 0.9,
        "status": EntryStatus.CURATED,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        LexiconEntry(**values)


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"canonical": "Codex"},
        {
            "canonical": "Codex",
            "scope": "base",
            "aliases": ["code X"],
            "domains": [],
            "weight": 0.9,
            "status": "curated",
            "extra": True,
        },
        {
            "canonical": 3,
            "scope": "base",
            "aliases": ["code X"],
            "domains": [],
            "weight": 0.9,
            "status": "curated",
        },
        {
            "canonical": "Codex",
            "scope": 3,
            "aliases": ["code X"],
            "domains": [],
            "weight": 0.9,
            "status": "curated",
        },
        {
            "canonical": "Codex",
            "scope": "base",
            "aliases": "code X",
            "domains": [],
            "weight": 0.9,
            "status": "curated",
        },
    ],
)
def test_public_lexicon_parser_rejects_malformed_entries(raw):
    from voice_intent_normalizer.lexicon import parse_entry

    with pytest.raises(ValueError):
        parse_entry(raw)


def test_public_jsonl_bytes_reports_encoding_and_blank_line_failures():
    from voice_intent_normalizer.lexicon import load_jsonl_bytes

    with pytest.raises(TypeError, match="bytes"):
        load_jsonl_bytes("not bytes", "memory")
    with pytest.raises(ValueError, match="invalid UTF-8"):
        load_jsonl_bytes(b"{}\n\xff", "memory")
    with pytest.raises(ValueError, match="blank lines"):
        load_jsonl_bytes(b"\n", "memory")


def test_learning_store_rejects_invalid_scope_and_corrupt_journal(tmp_path):
    from voice_intent_normalizer.learning import LearningStore
    from voice_intent_normalizer.models import Scope

    store = LearningStore(tmp_path / "state")
    with pytest.raises(ValueError, match="scoped"):
        store.confirm("a", "b", Scope.BASE)
    with pytest.raises(ValueError, match="project_id"):
        store.confirm("a", "b", Scope.PERSONAL, project_id="project")

    store.root.mkdir()
    store.events_file.write_bytes(b"\n")
    with pytest.raises(ValueError, match="blank event"):
        store.list_recent()
    store.events_file.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        store.list_recent()
    store.events_file.write_text("{bad}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        store.list_recent()


def test_learning_store_empty_undo_and_list_are_stable(tmp_path):
    from voice_intent_normalizer.learning import LearningStore

    store = LearningStore(tmp_path / "state")

    assert store.undo_last() is None
    assert store.list_recent(0) == ()


def test_service_reports_update_scanner_and_layer_failures_without_stopping(
    tmp_path, monkeypatch
):
    from voice_intent_normalizer import service as service_module
    from voice_intent_normalizer.models import DecisionAction
    from voice_intent_normalizer.service import NormalizeRequest, NormalizerService

    project = tmp_path / "project"
    project.mkdir()

    def fail_update(_paths):
        raise OSError("offline")

    def fail_scan(_project, _paths):
        raise OSError("unavailable")

    normalizer = NormalizerService(
        StatePaths(root=tmp_path / "state"),
        ROOT / "assets" / "lexicons",
        hotword_updater=fail_update,
        project_scanner=fail_scan,
    )
    monkeypatch.setattr(service_module, "project_cache_is_stale", lambda *a, **k: True)

    decision = normalizer.normalize(
        NormalizeRequest(
            text="配置 open cloud",
            project_root=project,
            domains=("ai", "ai"),
        )
    )

    assert decision.action is DecisionAction.APPLY
    assert "hotword_update_failed" in decision.diagnostics
    assert "project_scan_failed" in decision.diagnostics


def test_service_falls_back_to_packaged_layers_on_loader_failure(
    tmp_path, monkeypatch
):
    from voice_intent_normalizer import service as service_module
    from voice_intent_normalizer.service import NormalizeRequest, NormalizerService

    normalizer = NormalizerService(
        StatePaths(root=tmp_path / "state"), ROOT / "assets" / "lexicons"
    )

    def fail_layers(*args, **kwargs):
        raise OSError("state unavailable")

    monkeypatch.setattr(
        service_module.LexiconSet, "load_with_diagnostics", fail_layers
    )

    decision = normalizer.normalize(
        NormalizeRequest(text="配置 open cloud", domains=("ai", "ai"))
    )

    assert decision.corrected_text == "配置 OpenClaw"
    assert "state_unavailable" in decision.diagnostics


@pytest.mark.parametrize(
    "kwargs",
    [
        {"domains": ("x" * 257,)},
        {"conversation_terms": ("x" * 1025,)},
        {"notified_pairs": frozenset({("x" * 257, "y")})},
    ],
)
def test_normalize_request_rejects_oversized_context_terms(kwargs):
    from voice_intent_normalizer.service import NormalizeRequest

    with pytest.raises(ValueError, match="oversized"):
        NormalizeRequest(text="x", **kwargs)


def test_normalize_request_bounds_iterators_before_consuming_unbounded_values():
    from voice_intent_normalizer.service import NormalizeRequest

    with pytest.raises(ValueError, match="bounds"):
        NormalizeRequest(text="x", domains=(str(index) for index in range(100)))


def test_codex_configuration_helpers_preserve_nonmanaged_content(tmp_path):
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    agents = tmp_path / "AGENTS.md"
    agents.write_text("user guidance\n", encoding="utf-8")
    assert adapter._remove_agents_block(agents) is False
    assert agents.read_text(encoding="utf-8") == "user guidance\n"

    hooks = tmp_path / "hooks.json"
    hooks.write_text('{"hooks":{"SessionEnd":[]}}', encoding="utf-8")
    assert adapter._remove_hook(hooks, tmp_path / "skill") is False
    assert adapter._has_hook(hooks) is False


def test_codex_rejects_oversized_or_nonfile_configuration(tmp_path):
    adapter = CodexAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    directory = tmp_path / "hooks.json"
    directory.mkdir()
    with pytest.raises(ValueError, match="bounded file"):
        adapter._read_hooks(directory)
    directory.rmdir()
    directory.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="bounded file"):
        adapter._read_hooks(directory)


def test_openclaw_source_and_receipt_helpers_are_idempotent(tmp_path):
    adapter = OpenClawAdapter(ROOT, StatePaths(root=tmp_path / "state"))
    first = adapter._source()
    second = adapter._source()
    assert first == second
    assert adapter._tree_bytes(first)
    assert adapter._same_path(first, first)
    assert adapter._same_path(first, tmp_path / "missing") is False

    receipt = adapter._receipt_payload(
        source=first,
        source_digest=adapter._source_digest(first),
        workspace=tmp_path / "workspace",
        target=None,
    )
    adapter._write_receipt(receipt)
    assert adapter._read_receipt() == receipt
    adapter._backup_receipt()
    assert adapter._receipt_path().with_name("openclaw.json.bak").is_file()
    adapter._remove_receipt()
    assert adapter._read_receipt() is None


@pytest.mark.parametrize(
    "payload",
    [
        object(),
        b"{\"x\":NaN}",
        b'{"format":1,"kind":"generation","identifier":"bad"}',
        "oversized",
    ],
)
def test_generic_manifest_contract_rejects_untrusted_json_shapes(payload):
    from voice_intent_normalizer.adapters.generic_contract import validate_manifest

    if payload == "oversized":
        payload = "x" * (9 * 1024 * 1024)
    with pytest.raises(ValueError):
        validate_manifest(payload)


@pytest.mark.parametrize(
    ("kind", "identifier", "version", "files"),
    [
        ("generation", "bad", "1.0", {"a": b"x"}),
        ("capsule", "other", "1.0", {"a": b"x"}),
        ("generation", f"g-{'a' * 64}-{'b' * 32}", "", {"a": b"x"}),
        (
            "generation",
            f"g-{'a' * 64}-{'b' * 32}",
            "1.0",
            {"../escape": b"x"},
        ),
        (
            "generation",
            f"g-{'a' * 64}-{'b' * 32}",
            "1.0",
            {"a": "not-bytes"},
        ),
    ],
)
def test_generic_manifest_builder_rejects_invalid_identity_and_files(
    kind, identifier, version, files
):
    from voice_intent_normalizer.adapters.generic_contract import build_manifest

    with pytest.raises(ValueError):
        build_manifest(kind, identifier, version, files)


def test_generic_status_launch_policy_allows_only_safe_phases(tmp_path):
    from dataclasses import replace

    from voice_intent_normalizer.adapters.generic_contract import (
        status_allows_active_generation_launch,
        validate_status_v5,
    )
    from voice_intent_normalizer.adapters.generic_layout import (
        prepare_versioned_artifacts,
        status_v5_payload,
    )

    skill_root = tmp_path / "skills"
    generations = tmp_path / "generations"
    skill_root.mkdir()
    generations.mkdir()
    artifacts = prepare_versioned_artifacts(ROOT, "1" * 32)
    terminal = validate_status_v5(
        status_v5_payload(
            skill_root=skill_root,
            capability="implicit",
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
        ),
        skill_root=skill_root,
        generations_root=generations,
    )
    assert status_allows_active_generation_launch(terminal)
    assert not status_allows_active_generation_launch(object())
    pending = replace(terminal, transaction_id="t-" + "0" * 32,
                      transaction_phase="activation-pending")
    assert not status_allows_active_generation_launch(pending)


def test_versioned_artifact_helpers_reject_wrong_reference_kinds():
    from voice_intent_normalizer.adapters.generic_layout import (
        generation_ref_payload,
        prepare_versioned_artifacts,
        status_v5_payload,
    )

    artifacts = prepare_versioned_artifacts(ROOT, "2" * 32)
    with pytest.raises(ValueError, match="generation"):
        generation_ref_payload(artifacts.capsule)
    with pytest.raises(ValueError, match="capsule"):
        status_v5_payload(
            skill_root=ROOT,
            capability="implicit",
            capsule=artifacts.generation,
            active=artifacts.generation,
            previous=None,
        )
    with pytest.raises(ValueError, match="incomplete"):
        status_v5_payload(
            skill_root=ROOT,
            capability="implicit",
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
            transaction_id="t-" + "0" * 32,
        )
    with pytest.raises(ValueError, match="transaction"):
        status_v5_payload(
            skill_root=ROOT,
            capability="implicit",
            capsule=artifacts.capsule,
            active=artifacts.generation,
            previous=None,
            transaction_id="bad",
            transaction_phase="activation-pending",
        )


def test_versioned_artifact_preparation_rejects_bad_nonce():
    from voice_intent_normalizer.adapters.generic_layout import (
        prepare_versioned_artifacts,
    )

    with pytest.raises(ValueError, match="nonce"):
        prepare_versioned_artifacts(ROOT, "bad")

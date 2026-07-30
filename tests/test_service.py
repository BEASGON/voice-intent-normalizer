from __future__ import annotations

import json
import os

import pytest

from voice_intent_normalizer.models import DecisionAction, Scope
from voice_intent_normalizer.paths import StatePaths


def _write_entry(path, **overrides):
    raw = {
        "canonical": "Codex",
        "scope": "base",
        "aliases": ["code X"],
        "domains": ["software-development"],
        "weight": 0.9,
        "status": "curated",
    }
    raw.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def service(tmp_path):
    from voice_intent_normalizer.service import NormalizerService

    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    builtins_root = tmp_path / "builtins"
    _write_entry(builtins_root / "base-zh.jsonl")
    return NormalizerService(paths, builtins_root)


@pytest.fixture
def service_with_failed_update(tmp_path):
    from voice_intent_normalizer.service import NormalizerService

    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    builtins_root = tmp_path / "builtins"
    _write_entry(builtins_root / "base-zh.jsonl")

    def failed_update(_paths):
        raise OSError("offline")

    return NormalizerService(paths, builtins_root, hotword_updater=failed_update)


def test_service_corrects_project_aware_term(service, project_root):
    from voice_intent_normalizer.service import NormalizeRequest

    service.learning.confirm(
        "星河公作台",
        "星河工作台",
        Scope.PROJECT,
        project_id=service.paths.for_project(project_root).project_id,
    )

    decision = service.normalize(
        NormalizeRequest(
            text="打开星河公作台的配置",
            project_root=project_root,
            domains=("software-development",),
        )
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "打开星河工作台的配置"


def test_service_continues_when_update_fails(service_with_failed_update):
    from voice_intent_normalizer.service import NormalizeRequest

    decision = service_with_failed_update.normalize(
        NormalizeRequest(
            text="使用 code X 检查项目",
            domains=("software-development",),
        )
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "使用 Codex 检查项目"


def test_service_scans_a_project_only_when_its_cache_is_missing(service, project_root):
    from voice_intent_normalizer.service import NormalizeRequest

    (project_root / "widget_engine.py").write_text(
        "class WidgetEngine:\n    pass\n", encoding="utf-8"
    )

    service.normalize(NormalizeRequest(text="检查项目", project_root=project_root))

    assert service.paths.for_project(project_root).scan_lexicon_file.is_file()


def test_service_passes_conversation_receipt_state_to_policy(service):
    from voice_intent_normalizer.service import NormalizeRequest

    decision = service.normalize(
        NormalizeRequest(
            text="使用 code X 检查项目",
            domains=("software-development",),
            conversation_terms=("Codex",),
            notified_pairs=frozenset({("code x", "Codex")}),
        )
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.notices == ()
    assert "conversation:mention" in decision.candidates[0].evidence


def test_service_routes_explicit_learning_only_through_apply_control(service):
    from voice_intent_normalizer.service import NormalizeRequest

    control_text = "我说的是 Codex，不是 code X"
    decision = service.normalize(NormalizeRequest(text=control_text))

    assert decision.action is DecisionAction.KEEP
    assert decision.diagnostics == ("explicit_control",)
    assert not service.learning.events_file.exists()

    result = service.apply_control(control_text)

    assert result is not None
    assert result.handled is True
    assert result.event is not None
    assert result.event.canonical == "Codex"
    assert result.event.scope is Scope.PERSONAL


def test_service_degrades_to_builtins_when_state_is_read_only(tmp_path):
    from voice_intent_normalizer.service import NormalizeRequest, NormalizerService

    paths = StatePaths.resolve(environ={}, home=tmp_path / "home")
    paths.root.mkdir(parents=True)
    builtins_root = tmp_path / "builtins"
    _write_entry(builtins_root / "base-zh.jsonl")
    original_mode = paths.root.stat().st_mode
    os.chmod(paths.root, 0o555)
    try:
        service = NormalizerService(paths, builtins_root)
        decision = service.normalize(
            NormalizeRequest(
                text="使用 code X 检查项目",
                domains=("software-development",),
            )
        )
    finally:
        os.chmod(paths.root, original_mode)

    assert decision.corrected_text == "使用 Codex 检查项目"
    assert "read_only_state" in decision.diagnostics


def test_service_falls_back_to_builtins_for_malformed_state(service):
    from voice_intent_normalizer.service import NormalizeRequest

    service.paths.root.mkdir(parents=True)
    service.paths.personal_file.write_text("not-json\n", encoding="utf-8")

    decision = service.normalize(
        NormalizeRequest(
            text="使用 code X 检查项目",
            domains=("software-development",),
        )
    )

    assert decision.corrected_text == "使用 Codex 检查项目"
    assert "personal_invalid" in decision.diagnostics


def test_stale_scanner_cache_refreshes_without_erasing_project_learning(
    service, project_root
):
    from voice_intent_normalizer.service import NormalizeRequest

    source = project_root / "first.py"
    source.write_text("class FirstWidget:\n    pass\n", encoding="utf-8")
    service.normalize(NormalizeRequest(text="检查", project_root=project_root))
    service.learning.confirm(
        "work body",
        "WorkBuddy",
        Scope.PROJECT,
        project_id=service.paths.for_project(project_root).project_id,
    )
    source.write_text(
        "class FirstWidget:\n    pass\nclass SecondWidget:\n    pass\n",
        encoding="utf-8",
    )

    decision = service.normalize(
        NormalizeRequest(text="work body 和 SecondWidget", project_root=project_root)
    )

    project_paths = service.paths.for_project(project_root)
    assert decision.corrected_text == "WorkBuddy 和 SecondWidget"
    assert project_paths.lexicon_file.is_file()
    assert project_paths.scan_lexicon_file.is_file()
    assert "SecondWidget" in project_paths.scan_lexicon_file.read_text(
        encoding="utf-8"
    )
    assert "WorkBuddy" in project_paths.lexicon_file.read_text(encoding="utf-8")


def test_corrupt_personal_layer_keeps_valid_project_and_hotword_layers(
    service, project_root
):
    from voice_intent_normalizer.service import NormalizeRequest

    project_id = service.paths.for_project(project_root).project_id
    service.paths.root.mkdir(parents=True)
    service.paths.personal_file.write_text("not-json\n", encoding="utf-8")
    _write_entry(
        service.paths.for_project(project_root).lexicon_file,
        canonical="ProjectTool",
        scope="project",
        aliases=["project tool"],
        domains=["software-development"],
        weight=1.0,
        status="confirmed",
        project_id=project_id,
    )
    _write_entry(
        service.paths.hotwords_file,
        canonical="HotTool",
        scope="hot",
        aliases=["hot tool"],
        domains=["software-development"],
        weight=1.0,
        status="curated",
    )

    decision = service.normalize(
        NormalizeRequest(
            text="project tool 和 hot tool",
            project_root=project_root,
            domains=("software-development",),
        )
    )

    assert decision.corrected_text == "ProjectTool 和 HotTool"
    assert "personal_invalid" in decision.diagnostics


def test_corrupt_hotword_layer_keeps_valid_project_layer(service, project_root):
    from voice_intent_normalizer.service import NormalizeRequest

    project_id = service.paths.for_project(project_root).project_id
    _write_entry(
        service.paths.for_project(project_root).lexicon_file,
        canonical="ProjectTool",
        scope="project",
        aliases=["project tool"],
        domains=[],
        weight=1.0,
        status="confirmed",
        project_id=project_id,
    )
    service.paths.hotwords_file.parent.mkdir(parents=True)
    service.paths.hotwords_file.write_text("not-json\n", encoding="utf-8")

    decision = service.normalize(
        NormalizeRequest(text="project tool", project_root=project_root)
    )

    assert decision.corrected_text == "ProjectTool"
    assert "hotword_invalid" in decision.diagnostics


def test_read_only_list_control_remains_available(service):
    service.learning.confirm("work body", "WorkBuddy", Scope.PERSONAL)
    original_mode = service.paths.root.stat().st_mode
    os.chmod(service.paths.root, 0o555)
    try:
        result = service.apply_control("查看最近学到的词。")
    finally:
        os.chmod(service.paths.root, original_mode)

    assert result is not None
    assert result.handled is True
    assert "1" in result.message


@pytest.mark.parametrize(
    "text",
    (
        "我说的是 WorkBuddy，不是 work body",
        "不要把 work body 改成 WorkBuddy",
        "撤销刚才的纠正。",
        "删除你学到的这个词。",
    ),
)
def test_read_only_write_controls_never_mutate_learning_state(service, text):
    service.paths.root.mkdir(parents=True)
    original_mode = service.paths.root.stat().st_mode
    os.chmod(service.paths.root, 0o555)
    try:
        result = service.apply_control(text)
    finally:
        os.chmod(service.paths.root, original_mode)

    assert result is not None
    assert result.handled is True
    assert "只读" in result.message
    assert not service.learning.events_file.exists()


def test_confirmed_mapping_applies_to_an_ordinary_phrase_without_context(service):
    from voice_intent_normalizer.service import NormalizeRequest

    service.learning.confirm("work body", "WorkBuddy", Scope.PERSONAL)

    decision = service.normalize(NormalizeRequest(text="打开 work body"))

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "打开 WorkBuddy"


@pytest.mark.parametrize(
    "text",
    (
        "删除 code X",
        "set code X retry to 42",
        "python code X.py",
    ),
)
def test_public_domain_evidence_never_auto_applies_to_high_impact_text(service, text):
    from voice_intent_normalizer.service import NormalizeRequest

    decision = service.normalize(
        NormalizeRequest(text=text, domains=("software-development",))
    )

    assert decision.action is not DecisionAction.APPLY
    assert decision.corrected_text == text


def test_request_rejects_unbounded_text_and_context_terms():
    from voice_intent_normalizer.service import NormalizeRequest

    with pytest.raises(ValueError, match="text exceeds"):
        NormalizeRequest(text="x" * 20_001)
    with pytest.raises(ValueError, match="text exceeds"):
        NormalizeRequest(text="😀" * 17_000)
    with pytest.raises(ValueError, match="conversation_terms"):
        NormalizeRequest(
            text="ok", conversation_terms=tuple("x" for _ in range(129))
        )


def test_package_root_exports_service_contracts():
    from voice_intent_normalizer import (
        ControlResult,
        NormalizeRequest,
        NormalizerService,
    )

    assert NormalizerService.__name__ == "NormalizerService"
    assert NormalizeRequest.__name__ == "NormalizeRequest"
    assert ControlResult.__name__ == "ControlResult"

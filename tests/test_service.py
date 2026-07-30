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

    assert service.paths.for_project(project_root).lexicon_file.is_file()


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
    assert "state_unavailable" in decision.diagnostics

from __future__ import annotations

import pytest

from voice_intent_normalizer.matching import MatchContext
from voice_intent_normalizer.models import (
    Candidate,
    DecisionAction,
    EntryStatus,
    LexiconEntry,
    Scope,
)


def _candidate(
    text: str,
    original: str,
    canonical: str,
    score: float,
    *,
    domains: tuple[str, ...] = ("ai", "agent"),
    evidence: tuple[str, ...] = (
        "alias:exact",
        "domain:overlap",
        "weight:0.18",
    ),
) -> Candidate:
    start = text.index(original)
    entry = LexiconEntry(
        canonical=canonical,
        scope=Scope.HOT,
        aliases=(original,),
        domains=domains,
        weight=0.9,
        status=EntryStatus.CURATED,
    )
    return Candidate(
        canonical=canonical,
        original=original,
        replacement_span=(start, start + len(original)),
        score=score,
        evidence=evidence,
        entry=entry,
    )


@pytest.fixture
def openclaw_candidate() -> Candidate:
    return _candidate("给龙虾安装这个 Agent 技能", "龙虾", "OpenClaw", 0.93)


@pytest.fixture
def delete_candidates() -> tuple[Candidate, Candidate]:
    text = "删除发布目录"
    return (
        _candidate(
            text,
            "发布目录",
            "release-directory",
            0.90,
            domains=("software-development",),
        ),
        _candidate(
            text,
            "发布目录",
            "publish-directory",
            0.80,
            domains=("software-development",),
        ),
    )


def test_literal_lobster_is_not_changed(openclaw_candidate: Candidate) -> None:
    from voice_intent_normalizer.policy import decide

    decision = decide(
        "今天晚上想吃龙虾",
        (
            Candidate(
                canonical=openclaw_candidate.canonical,
                original="龙虾",
                replacement_span=(6, 8),
                score=openclaw_candidate.score,
                evidence=openclaw_candidate.evidence,
                entry=openclaw_candidate.entry,
            ),
        ),
        MatchContext(domains=frozenset({"food"})),
    )

    assert decision.action is DecisionAction.KEEP
    assert decision.corrected_text == "今天晚上想吃龙虾"


def test_ai_lobster_receives_first_use_receipt(
    openclaw_candidate: Candidate,
) -> None:
    from voice_intent_normalizer.policy import decide

    decision = decide(
        "给龙虾安装这个 Agent 技能",
        (openclaw_candidate,),
        MatchContext(domains=frozenset({"ai", "agent"})),
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "给OpenClaw安装这个 Agent 技能"
    assert decision.notices == ("已按 OpenClaw 理解（原转写：龙虾）",)


def test_destructive_target_ambiguity_asks(
    delete_candidates: tuple[Candidate, Candidate],
) -> None:
    from voice_intent_normalizer.policy import decide

    decision = decide(
        "删除发布目录",
        delete_candidates,
        MatchContext(domains=frozenset({"software-development"})),
    )

    assert decision.action is DecisionAction.ASK
    assert decision.question is not None
    assert decision.corrected_text == "删除发布目录"


@pytest.mark.parametrize(
    ("text", "reason"),
    (
        ("删除 /srv/releases", "path"),
        ("git push origin main", "command"),
        ("升级到 v1.2.3", "semantic-version"),
        ("在 2026-07-29 发布", "date"),
        ("增加 15% 预算", "percentage"),
        ("付款 ¥100", "money"),
        ("更新 deploy_config", "code-identifier"),
        ("授权这个应用", "high-impact-verb"),
    ),
)
def test_detect_risk_reports_consequential_signals(text: str, reason: str) -> None:
    from voice_intent_normalizer.policy import detect_risk

    assessment = detect_risk(text)

    assert assessment.high_impact is True
    assert reason in assessment.reasons


def test_low_risk_apply_requires_confidence_and_competing_margin() -> None:
    from voice_intent_normalizer.policy import decide

    text = "配置 open cloud"
    candidates = (
        _candidate(text, "open cloud", "OpenClaw", 0.84),
        _candidate(text, "open cloud", "OpenCloud", 0.60),
    )

    decision = decide(text, candidates, MatchContext(domains=frozenset({"ai"})))

    assert decision.action is DecisionAction.KEEP


def test_apply_accepts_exact_confidence_and_margin_thresholds() -> None:
    from voice_intent_normalizer.policy import decide

    text = "配置 open cloud"
    candidates = (
        _candidate(text, "open cloud", "OpenClaw", 0.95),
        _candidate(text, "open cloud", "OpenCloud", 0.80),
    )

    decision = decide(text, candidates, MatchContext(domains=frozenset({"ai"})))

    assert decision.action is DecisionAction.APPLY


def test_replacements_apply_from_right_to_left_without_shifting_spans() -> None:
    from voice_intent_normalizer.policy import decide

    text = "先 open cloud，再 code x"
    candidates = (
        _candidate(text, "open cloud", "OpenClaw", 0.95),
        _candidate(
            text,
            "code x",
            "Codex",
            0.93,
            domains=("software-development",),
        ),
    )

    decision = decide(
        text,
        candidates,
        MatchContext(domains=frozenset({"ai", "software-development"})),
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "先 OpenClaw，再 Codex"


def test_notified_pair_suppresses_only_the_receipt(
    openclaw_candidate: Candidate,
) -> None:
    from voice_intent_normalizer.policy import decide

    decision = decide(
        "给龙虾安装这个 Agent 技能",
        (openclaw_candidate,),
        MatchContext(domains=frozenset({"ai", "agent"})),
        notified_pairs=frozenset({("龙虾", "OpenClaw")}),
    )

    assert decision.action is DecisionAction.APPLY
    assert decision.notices == ()


def test_notified_pair_never_suppresses_an_ask(
    delete_candidates: tuple[Candidate, Candidate],
) -> None:
    from voice_intent_normalizer.policy import decide

    decision = decide(
        "删除发布目录",
        delete_candidates,
        MatchContext(domains=frozenset({"software-development"})),
        notified_pairs=frozenset({("发布目录", "release-directory")}),
    )

    assert decision.action is DecisionAction.ASK

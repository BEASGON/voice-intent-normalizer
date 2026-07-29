from __future__ import annotations

from types import SimpleNamespace

import pytest

from voice_intent_normalizer.lexicon import LexiconSet
from voice_intent_normalizer.models import EntryStatus, LexiconEntry, Scope


@pytest.fixture
def ai_lexicons() -> LexiconSet:
    return LexiconSet(
        entries=(
            LexiconEntry(
                canonical="OpenClaw",
                scope=Scope.HOT,
                aliases=("Open Cloud", "龙虾"),
                domains=("ai", "agent"),
                weight=0.9,
                status=EntryStatus.CURATED,
            ),
            LexiconEntry(
                canonical="Codex",
                scope=Scope.HOT,
                aliases=("code X",),
                domains=("software-development",),
                weight=0.9,
                status=EntryStatus.CURATED,
            ),
        )
    )


@pytest.fixture
def workbuddy_lexicons() -> LexiconSet:
    return LexiconSet(
        entries=(
            LexiconEntry(
                canonical="WorkBuddy",
                scope=Scope.HOT,
                aliases=("work body",),
                domains=("ai",),
                weight=0.8,
                status=EntryStatus.CURATED,
            ),
        )
    )


def test_open_cloud_matches_openclaw(ai_lexicons):
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    context = MatchContext(domains=frozenset({"ai", "agent"}))
    candidates = generate_candidates(
        "帮我适配 open cloud 的技能",
        ai_lexicons,
        context,
    )

    best = candidates[0]
    assert best.canonical == "OpenClaw"
    assert best.original == "open cloud"
    assert best.replacement_span == (5, 15)
    assert best.score == pytest.approx(0.83)
    assert best.evidence == ("alias:normalized", "domain:overlap", "weight:0.18")


def test_work_body_matches_case_and_spacing(workbuddy_lexicons):
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    candidates = generate_candidates(
        "在 Work   Body 里面调用",
        workbuddy_lexicons,
        MatchContext(domains=frozenset({"ai"})),
    )

    assert candidates[0].canonical == "WorkBuddy"
    assert candidates[0].original == "Work   Body"


def test_missing_pypinyin_keeps_alias_matching(monkeypatch, ai_lexicons):
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    monkeypatch.setattr(
        "voice_intent_normalizer.matching._load_pypinyin", lambda: None
    )
    candidates = generate_candidates(
        "使用 code X",
        ai_lexicons,
        MatchContext(domains=frozenset({"software-development"})),
    )

    assert candidates[0].canonical == "Codex"


def test_normalize_alias_collapses_unicode_case_whitespace_and_separators():
    from voice_intent_normalizer.matching import normalize_alias

    assert normalize_alias(" Ｗork---Body_/agent ") == "work body agent"


def test_phonetic_key_is_optional(monkeypatch):
    from voice_intent_normalizer.matching import phonetic_key

    monkeypatch.setattr(
        "voice_intent_normalizer.matching._load_pypinyin", lambda: None
    )

    assert phonetic_key("龙虾") is None


def test_phonetic_key_uses_lazily_loaded_provider(monkeypatch):
    from voice_intent_normalizer.matching import phonetic_key

    provider = SimpleNamespace(lazy_pinyin=lambda value: ["Long", "Xia"])
    monkeypatch.setattr(
        "voice_intent_normalizer.matching._load_pypinyin", lambda: provider
    )

    assert phonetic_key("龙虾") == "long-xia"


def test_pinyin_fallback_generates_candidate_when_alias_text_differs(monkeypatch):
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    syllables = {"会": "hui", "话": "hua", "绘": "hui", "画": "hua"}
    provider = SimpleNamespace(
        lazy_pinyin=lambda value: [
            syllables.get(character, character) for character in value
        ]
    )
    monkeypatch.setattr(
        "voice_intent_normalizer.matching._load_pypinyin", lambda: provider
    )
    entry = LexiconEntry(
        canonical="Session",
        scope=Scope.HOT,
        aliases=("会话",),
        domains=(),
        weight=0.9,
        status=EntryStatus.CURATED,
    )

    candidates = generate_candidates(
        "绘画", LexiconSet(entries=(entry,)), MatchContext()
    )

    assert candidates[0].replacement_span == (0, 2)
    assert candidates[0].evidence == ("alias:phonetic", "weight:0.18")


def test_declared_phonetic_key_is_used_for_pinyin_fallback(monkeypatch):
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    syllables = {
        "你": "ni",
        "好": "hao",
        "绘": "hui",
        "画": "hua",
    }
    provider = SimpleNamespace(
        lazy_pinyin=lambda value: [
            syllables.get(character, character) for character in value
        ]
    )
    monkeypatch.setattr(
        "voice_intent_normalizer.matching._load_pypinyin", lambda: provider
    )
    entry = LexiconEntry(
        canonical="Conversation",
        scope=Scope.HOT,
        aliases=("你好",),
        domains=(),
        weight=0.9,
        status=EntryStatus.CURATED,
        phonetics=("hui-hua",),
    )

    candidates = generate_candidates(
        "绘画", LexiconSet(entries=(entry,)), MatchContext()
    )

    assert candidates[0].canonical == "Conversation"
    assert candidates[0].evidence == ("alias:phonetic", "weight:0.18")


def test_scoring_records_context_and_negative_alias_evidence():
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    entry = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.PROJECT,
        aliases=("open claw",),
        domains=("ai",),
        weight=1.0,
        status=EntryStatus.CONFIRMED,
        negative_aliases=("open door",),
    )
    candidates = generate_candidates(
        "open claw 和 open door",
        LexiconSet(entries=(entry,)),
        MatchContext(
            domains=frozenset({"ai"}),
            project_terms=frozenset({"OpenClaw"}),
            conversation_terms=frozenset({"openclaw"}),
        ),
    )

    assert candidates[0].score == 1.0
    assert candidates[0].evidence == (
        "alias:exact",
        "domain:overlap",
        "project:exact-term",
        "conversation:mention",
        "weight:0.20",
    )
    negative = next(
        candidate for candidate in candidates if candidate.original == "open door"
    )
    assert negative.score == 0.0
    assert negative.evidence[0] == "alias:negative"


def test_negative_alias_overrides_a_positive_normalized_alias_for_the_same_span():
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    entry = LexiconEntry(
        canonical="OpenClaw",
        scope=Scope.HOT,
        aliases=("open-cloud",),
        domains=("ai",),
        weight=1.0,
        status=EntryStatus.CURATED,
        negative_aliases=("open cloud",),
    )

    candidates = generate_candidates(
        "open cloud",
        LexiconSet(entries=(entry,)),
        MatchContext(
            domains=frozenset({"ai"}),
            project_terms=frozenset({"OpenClaw"}),
            conversation_terms=frozenset({"OpenClaw"}),
        ),
    )

    candidate = candidates[0]
    assert candidate.score == 0.0
    assert "alias:negative" in candidate.evidence
    assert "alias:normalized" not in candidate.evidence


def test_ties_follow_layer_precedence_then_canonical_name():
    from voice_intent_normalizer.matching import MatchContext, generate_candidates

    base = LexiconEntry(
        canonical="Alpha",
        scope=Scope.BASE,
        aliases=("tool",),
        domains=(),
        weight=0.0,
        status=EntryStatus.CURATED,
    )
    hot = LexiconEntry(
        canonical="Zulu",
        scope=Scope.HOT,
        aliases=("tool",),
        domains=(),
        weight=0.0,
        status=EntryStatus.CURATED,
    )

    candidates = generate_candidates(
        "tool",
        LexiconSet(entries=(base, hot)),
        MatchContext(),
    )

    assert [candidate.canonical for candidate in candidates] == ["Zulu", "Alpha"]

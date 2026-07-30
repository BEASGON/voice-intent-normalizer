"""Conservative safety policy for lexicon-backed voice corrections."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .matching import MatchContext, normalize_alias
from .models import Candidate, CorrectionDecision, DecisionAction, EntryStatus, Scope

APPLY_THRESHOLD = 0.85
ASK_THRESHOLD = 0.65
MARGIN_THRESHOLD = 0.15
_FLOAT_TOLERANCE = 1e-9

_COMMAND_PATTERN = re.compile(
    r"""
    (?:
        \b(?:git\s+(?:push|commit|reset|clean)|
        (?:rm|del|rmdir|Remove-Item|kubectl|docker)\b)
        |
        (?<![A-Za-z0-9_])
        [A-Za-z_][\w.-]*
        \s+
        (?:--?[\w-]+|[./~][\w./~-]*|[\w.-]+\.(?:py|js|mjs|cjs|sh|ps1))
        |
        \b(?:npm|pnpm|yarn|pip(?:x|3)?|poetry|uv)\s+
        (?:publish|install|uninstall|build|run|test|add|remove|update)\b
    )
    """,
    re.VERBOSE,
)

RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "path",
        re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|~?[\\/]|\.\.?[\\/])"),
    ),
    ("command", _COMMAND_PATTERN),
    ("semantic-version", re.compile(r"\bv?\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b")),
    ("date", re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")),
    ("percentage", re.compile(r"\b\d+(?:\.\d+)?\s*%")),
    (
        "money",
        re.compile(r"(?:[¥￥$€]\s*\d|\b\d+(?:\.\d+)?\s*(?:元|人民币|美元|dollars?))"),
    ),
    (
        "number",
        re.compile(
            r"(?<![A-Za-z0-9_.¥￥$€-])\d+(?:\.\d+)?"
            r"(?![A-Za-z0-9_.-]|\s*(?:%|元|人民币|美元|dollars?\b))"
        ),
    ),
    (
        "code-identifier",
        re.compile(r"\b(?:[A-Za-z]+_[A-Za-z0-9_]*|[a-z]+[A-Z][A-Za-z0-9]*)\b"),
    ),
    ("high-impact-verb", re.compile(r"删除|覆盖|发布|部署|提交|推送|授权|付款")),
)


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """High-impact text features that require extra correction evidence."""

    high_impact: bool
    reasons: tuple[str, ...]


def detect_risk(text: str) -> RiskAssessment:
    """Identify consequential content without deciding whether to change it."""
    reasons = tuple(name for name, pattern in RISK_PATTERNS if pattern.search(text))
    return RiskAssessment(high_impact=bool(reasons), reasons=reasons)


def _valid_candidates(
    text: str, candidates: Iterable[Candidate]
) -> tuple[Candidate, ...]:
    valid: list[Candidate] = []
    for candidate in candidates:
        start, end = candidate.replacement_span
        if 0 <= start < end <= len(text) and text[start:end] == candidate.original:
            if candidate.original != candidate.canonical:
                valid.append(candidate)
    return tuple(valid)


def _candidate_groups(
    candidates: tuple[Candidate, ...],
) -> tuple[tuple[Candidate, ...], ...]:
    groups: dict[tuple[int, int], list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.replacement_span, []).append(candidate)
    return tuple(tuple(group) for group in groups.values())


def _margin(group: tuple[Candidate, ...]) -> float:
    if len(group) == 1:
        return 1.0
    return group[0].score - group[1].score


def _has_context_support(candidate: Candidate, context: MatchContext) -> bool:
    domains = {normalize_alias(domain) for domain in context.domains}
    entry_domains = {normalize_alias(domain) for domain in candidate.entry.domains}
    if domains & entry_domains:
        return True
    return any(
        evidence in {"project:exact-term", "conversation:mention"}
        for evidence in candidate.evidence
    )


def _has_high_impact_support(candidate: Candidate, context: MatchContext) -> bool:
    """Require local project identity or an explicit V1 confirmation for risk."""
    if (
        candidate.entry.status is EntryStatus.CONFIRMED
        and candidate.entry.source == "explicit-learning-v1"
    ):
        return True
    return (
        candidate.entry.scope is Scope.PROJECT
        and normalize_alias(candidate.canonical)
        in {normalize_alias(term) for term in context.project_terms}
    )


def _eligible_to_apply(
    candidate: Candidate,
    margin: float,
    context: MatchContext,
    risk: RiskAssessment,
) -> bool:
    if (
        candidate.score + _FLOAT_TOLERANCE < APPLY_THRESHOLD
        or margin + _FLOAT_TOLERANCE < MARGIN_THRESHOLD
    ):
        return False
    if risk.high_impact and not _has_high_impact_support(candidate, context):
        return False
    return _has_context_support(candidate, context) or not context.domains


def _overlaps(left: Candidate, right: Candidate) -> bool:
    left_start, left_end = left.replacement_span
    right_start, right_end = right.replacement_span
    return left_start < right_end and right_start < left_end


def _select_non_overlapping(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    selected: list[Candidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-item.score, item.replacement_span, item.canonical),
    ):
        if not any(_overlaps(candidate, current) for current in selected):
            selected.append(candidate)
    return tuple(selected)


def _is_cross_language(original: str, canonical: str) -> bool:
    original_has_cjk = any("\u4e00" <= character <= "\u9fff" for character in original)
    canonical_has_cjk = any(
        "\u4e00" <= character <= "\u9fff" for character in canonical
    )
    return original_has_cjk != canonical_has_cjk


def _is_proper_noun(canonical: str) -> bool:
    return any(character.isupper() for character in canonical)


def _receipts(
    candidates: Iterable[Candidate],
    notified_pairs: frozenset[tuple[str, str]],
) -> tuple[str, ...]:
    notices: list[str] = []
    for candidate in candidates:
        pair = (normalize_alias(candidate.original), candidate.canonical)
        if pair in notified_pairs:
            continue
        if _is_proper_noun(candidate.canonical) or _is_cross_language(
            candidate.original, candidate.canonical
        ):
            notices.append(
                f"已按 {candidate.canonical} 理解（原转写：{candidate.original}）"
            )
    return tuple(notices)


def _apply_replacements(text: str, candidates: Iterable[Candidate]) -> str:
    corrected = text
    for candidate in sorted(
        candidates,
        key=lambda item: item.replacement_span,
        reverse=True,
    ):
        start, end = candidate.replacement_span
        corrected = f"{corrected[:start]}{candidate.canonical}{corrected[end:]}"
    return corrected


def _ask_decision(
    text: str,
    candidates: tuple[Candidate, ...],
    risk: RiskAssessment,
) -> CorrectionDecision:
    candidate = candidates[0]
    question = f"请确认是否将“{candidate.original}”理解为“{candidate.canonical}”？"
    return CorrectionDecision(
        action=DecisionAction.ASK,
        original_text=text,
        corrected_text=text,
        question=question,
        candidates=candidates,
        diagnostics=risk.reasons,
    )


def decide(
    text: str,
    candidates: Iterable[Candidate],
    context: MatchContext,
    notified_pairs: frozenset[tuple[str, str]] = frozenset(),
) -> CorrectionDecision:
    """Choose whether a transcript interpretation is safe to apply, ask, or keep."""
    all_candidates = tuple(candidates)
    valid_candidates = _valid_candidates(text, all_candidates)
    risk = detect_risk(text)
    groups = _candidate_groups(valid_candidates)
    leading = tuple(group[0] for group in groups)

    uncertain = tuple(
        group[0]
        for group in groups
        if group[0].score >= ASK_THRESHOLD
        and not _eligible_to_apply(group[0], _margin(group), context, risk)
    )
    if risk.high_impact and uncertain:
        return _ask_decision(text, uncertain, risk)

    selected = _select_non_overlapping(
        candidate
        for group in groups
        if _eligible_to_apply(group[0], _margin(group), context, risk)
        for candidate in (group[0],)
    )
    if selected:
        return CorrectionDecision(
            action=DecisionAction.APPLY,
            original_text=text,
            corrected_text=_apply_replacements(text, selected),
            notices=_receipts(selected, notified_pairs),
            candidates=selected,
            diagnostics=risk.reasons,
        )
    return CorrectionDecision(
        action=DecisionAction.KEEP,
        original_text=text,
        corrected_text=text,
        candidates=leading,
        diagnostics=risk.reasons,
    )

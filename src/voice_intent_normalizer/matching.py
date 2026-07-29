"""Deterministic candidate generation for lexicon-backed voice corrections."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .lexicon import LexiconSet
from .models import Candidate, LexiconEntry, Scope

_SEPARATOR_PATTERN = re.compile(r"[\s\-_/]+")
_SCOPE_PRECEDENCE = {
    Scope.PERSONAL: 0,
    Scope.PROJECT: 1,
    Scope.INDUSTRY: 2,
    Scope.HOT: 3,
    Scope.BASE: 4,
}


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Context that can increase confidence in an otherwise ambiguous match."""

    domains: frozenset[str] = frozenset()
    project_terms: frozenset[str] = frozenset()
    conversation_terms: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "domains", frozenset(self.domains))
        object.__setattr__(self, "project_terms", frozenset(self.project_terms))
        object.__setattr__(
            self, "conversation_terms", frozenset(self.conversation_terms)
        )


def normalize_alias(value: str) -> str:
    """Return a comparison key without changing the display or source value."""
    return _SEPARATOR_PATTERN.sub(
        " ", unicodedata.normalize("NFKC", value).casefold()
    ).strip()


def _load_pypinyin() -> Any | None:
    """Load the optional pinyin provider without changing the environment."""
    try:
        import pypinyin
    except ImportError:
        return None
    return pypinyin


def phonetic_key(value: str) -> str | None:
    """Return an optional pinyin key, or ``None`` when pypinyin is absent."""
    provider = _load_pypinyin()
    if provider is None:
        return None
    syllables = provider.lazy_pinyin(unicodedata.normalize("NFKC", value))
    return "-".join(syllables).casefold()


def _source_key(value: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """Normalize each source character while retaining mappings to source offsets."""
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        pieces.append(normalized)
        starts.extend((index,) * len(normalized))
        ends.extend((index + 1,) * len(normalized))
    return "".join(pieces), tuple(starts), tuple(ends)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    parts = [part for part in _SEPARATOR_PATTERN.split(normalize_alias(alias)) if part]
    expression = r"[\s\-_/]+".join(re.escape(part) for part in parts)
    if not expression:
        return re.compile(r"(?!x)x")
    if parts[0][0].isascii() and parts[0][0].isalnum():
        expression = rf"(?<![a-z0-9]){expression}"
    if parts[-1][-1].isascii() and parts[-1][-1].isalnum():
        expression = rf"{expression}(?![a-z0-9])"
    return re.compile(expression)


def _alias_matches(
    source_key: str,
    starts: tuple[int, ...],
    ends: tuple[int, ...],
    alias: str,
) -> Iterable[tuple[int, int]]:
    for match in _alias_pattern(alias).finditer(source_key):
        start, end = match.span()
        if start != end:
            yield starts[start], ends[end - 1]


def _phonetic_key(value: str, provider: Any) -> str:
    syllables = provider.lazy_pinyin(unicodedata.normalize("NFKC", value))
    return "-".join(syllables).casefold()


def _phonetic_matches(
    text: str, entry: LexiconEntry, provider: Any
) -> Iterable[tuple[int, int]]:
    """Find equal pinyin keys using lexicon aliases and declared phonetics."""
    target_keys = {
        key
        for alias in entry.aliases
        if (key := _phonetic_key(alias, provider))
    }
    target_keys.update(
        normalize_alias(phonetic).replace(" ", "-")
        for phonetic in entry.phonetics
    )
    target_keys.update(
        key
        for phonetic in entry.phonetics
        if (key := _phonetic_key(phonetic, provider))
    )
    if not target_keys:
        return

    lengths = {len(alias) for alias in entry.aliases if alias}
    for length in lengths:
        for start in range(0, len(text) - length + 1):
            end = start + length
            fragment = text[start:end]
            if _phonetic_key(fragment, provider) in target_keys:
                yield start, end


def _term_keys(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_alias(value) for value in values)


def _candidate(
    text: str,
    entry: LexiconEntry,
    span: tuple[int, int],
    match_kind: str,
    context: MatchContext,
) -> Candidate:
    evidence: list[str] = []
    score = 0.0
    if match_kind == "exact":
        score += 0.55
        evidence.append("alias:exact")
    elif match_kind == "normalized":
        score += 0.45
        evidence.append("alias:normalized")
    elif match_kind == "phonetic":
        score += 0.35
        evidence.append("alias:phonetic")
    else:
        score -= 1.0
        evidence.append("alias:negative")

    if _term_keys(entry.domains) & _term_keys(context.domains):
        score += 0.20
        evidence.append("domain:overlap")
    if normalize_alias(entry.canonical) in _term_keys(context.project_terms):
        score += 0.25
        evidence.append("project:exact-term")
    if normalize_alias(entry.canonical) in _term_keys(context.conversation_terms):
        score += 0.15
        evidence.append("conversation:mention")

    weight_score = entry.weight * 0.20
    score += weight_score
    evidence.append(f"weight:{weight_score:.2f}")
    return Candidate(
        canonical=entry.canonical,
        original=text[span[0] : span[1]],
        replacement_span=span,
        score=max(0.0, min(1.0, score)),
        evidence=tuple(evidence),
        entry=entry,
    )


def generate_candidates(
    text: str, lexicons: LexiconSet, context: MatchContext
) -> tuple[Candidate, ...]:
    """Generate ordered, explainable candidates without altering *text*."""
    source_key, starts, ends = _source_key(text)
    found: dict[tuple[LexiconEntry, tuple[int, int]], str] = {}
    match_priority = {"phonetic": 0, "normalized": 1, "exact": 2, "negative": 3}

    def record(entry: LexiconEntry, span: tuple[int, int], kind: str) -> None:
        key = (entry, span)
        existing = found.get(key)
        if existing is None or match_priority[kind] > match_priority[existing]:
            found[key] = kind

    for entry in lexicons.entries:
        for alias in entry.aliases:
            for span in _alias_matches(source_key, starts, ends, alias):
                kind = "exact" if text[span[0] : span[1]] == alias else "normalized"
                record(entry, span, kind)
        for alias in entry.negative_aliases:
            for span in _alias_matches(source_key, starts, ends, alias):
                record(entry, span, "negative")

    provider = _load_pypinyin()
    if provider is not None:
        for entry in lexicons.entries:
            for span in _phonetic_matches(text, entry, provider):
                record(entry, span, "phonetic")

    candidates = tuple(
        _candidate(text, entry, span, kind, context)
        for (entry, span), kind in found.items()
    )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                _SCOPE_PRECEDENCE[candidate.entry.scope],
                candidate.canonical,
                candidate.replacement_span,
                candidate.original,
            ),
        )
    )

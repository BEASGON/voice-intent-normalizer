"""Immutable domain models for lexicon-backed intent normalization."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from numbers import Real


class Scope(str, Enum):
    BASE = "base"
    HOT = "hot"
    INDUSTRY = "industry"
    PROJECT = "project"
    PERSONAL = "personal"


class EntryStatus(str, Enum):
    CURATED = "curated"
    CANDIDATE = "candidate"
    REPEATED = "repeated"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


def _strings(
    values: Iterable[str], field_name: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    values = tuple(values)
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{field_name} must contain only non-blank strings")
    return values


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    canonical: str
    scope: Scope
    aliases: tuple[str, ...]
    domains: tuple[str, ...]
    weight: float
    status: EntryStatus
    phonetics: tuple[str, ...] = ()
    project_id: str | None = None
    source: str | None = None
    use_count: int | None = None
    notes: str | None = None
    negative_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.canonical, str) or not self.canonical.strip():
            raise ValueError("canonical must be a non-blank string")
        if isinstance(self.weight, bool) or not isinstance(self.weight, Real):
            raise ValueError("weight must be a number")
        if not 0.0 <= float(self.weight) <= 1.0:
            raise ValueError("weight must be between 0.0 and 1.0")
        if self.project_id is not None and not isinstance(self.project_id, str):
            raise ValueError("project_id must be a string or None")
        if self.source is not None and not isinstance(self.source, str):
            raise ValueError("source must be a string or None")
        if isinstance(self.use_count, bool) or (
            self.use_count is not None
            and (not isinstance(self.use_count, int) or self.use_count < 0)
        ):
            raise ValueError("use_count must be a non-negative integer or None")
        if self.notes is not None and not isinstance(self.notes, str):
            raise ValueError("notes must be a string or None")

        object.__setattr__(self, "scope", Scope(self.scope))
        object.__setattr__(self, "status", EntryStatus(self.status))
        object.__setattr__(
            self, "aliases", _strings(self.aliases, "aliases", allow_empty=False)
        )
        object.__setattr__(self, "domains", _strings(self.domains, "domains"))
        object.__setattr__(self, "phonetics", _strings(self.phonetics, "phonetics"))
        object.__setattr__(
            self,
            "negative_aliases",
            _strings(self.negative_aliases, "negative_aliases"),
        )
        object.__setattr__(self, "weight", float(self.weight))


@dataclass(frozen=True, slots=True)
class Candidate:
    canonical: str
    original: str
    replacement_span: tuple[int, int]
    score: float
    evidence: tuple[str, ...]
    entry: LexiconEntry

    def __post_init__(self) -> None:
        object.__setattr__(self, "replacement_span", tuple(self.replacement_span))
        object.__setattr__(self, "evidence", _strings(self.evidence, "evidence"))


class DecisionAction(str, Enum):
    APPLY = "apply"
    ASK = "ask"
    KEEP = "keep"


@dataclass(frozen=True, slots=True)
class CorrectionDecision:
    action: DecisionAction
    original_text: str
    corrected_text: str
    notices: tuple[str, ...] = ()
    question: str | None = None
    candidates: tuple[Candidate, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DecisionAction(self.action))
        object.__setattr__(self, "notices", _strings(self.notices, "notices"))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(
            self, "diagnostics", _strings(self.diagnostics, "diagnostics")
        )

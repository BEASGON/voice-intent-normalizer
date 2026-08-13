"""Release corpus metrics for public voice-normalization behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from voice_intent_normalizer.models import DecisionAction
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.service import NormalizeRequest, NormalizerService

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "corpus.jsonl"


@dataclass(frozen=True)
class CorpusCase:
    id: str
    input: str
    domains: tuple[str, ...]
    project_terms: tuple[str, ...]
    expected_action: str
    expected_text: str
    risk: str


@dataclass(frozen=True)
class CorpusResult:
    case: CorpusCase
    decision: object


def _cases() -> tuple[CorpusCase, ...]:
    cases: list[CorpusCase] = []
    for line_number, line in enumerate(
        CORPUS.read_text(encoding="utf-8").splitlines(), 1
    ):
        payload = json.loads(line)
        assert set(payload) == {
            "id",
            "input",
            "domains",
            "project_terms",
            "expected_action",
            "expected_text",
            "risk",
        }, line_number
        assert isinstance(payload["id"], str) and payload["id"]
        assert isinstance(payload["input"], str)
        assert isinstance(payload["domains"], list)
        assert isinstance(payload["project_terms"], list)
        assert payload["expected_action"] in {"apply", "keep", "ask"}
        assert isinstance(payload["expected_text"], str)
        assert isinstance(payload["risk"], str) and payload["risk"]
        cases.append(
            CorpusCase(
                id=payload["id"],
                input=payload["input"],
                domains=tuple(payload["domains"]),
                project_terms=tuple(payload["project_terms"]),
                expected_action=payload["expected_action"],
                expected_text=payload["expected_text"],
                risk=payload["risk"],
            )
        )
    assert len({case.id for case in cases}) == len(cases)
    return tuple(cases)


@pytest.fixture(scope="module")
def corpus_results(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[CorpusResult, ...]:
    service = NormalizerService(
        StatePaths(root=tmp_path_factory.mktemp("corpus-state")),
        ROOT / "assets" / "lexicons",
    )
    return tuple(
        CorpusResult(
            case,
            service.normalize(
                NormalizeRequest(
                    text=case.input,
                    domains=case.domains,
                    conversation_terms=case.project_terms,
                )
            ),
        )
        for case in _cases()
    )


def test_corpus_has_required_error_and_control_coverage(corpus_results):
    applies = [
        item for item in corpus_results if item.case.expected_action == "apply"
    ]
    controls = [
        item for item in corpus_results if item.case.expected_action == "keep"
    ]
    categories = {item.case.id.split("-")[1] for item in corpus_results}
    assert len(applies) >= 100
    assert len(controls) >= 100
    assert {
        "path",
        "command",
        "number",
        "date",
        "money",
        "delete",
        "publish",
        "deploy",
    } <= categories


def test_known_error_recall_is_at_least_95_percent(corpus_results):
    known = [
        item for item in corpus_results if item.case.expected_action == "apply"
    ]
    missed = [
        item.case.id
        for item in known
        if item.decision.corrected_text != item.case.expected_text
    ]
    assert 1 - len(missed) / len(known) >= 0.95, missed


def test_correct_text_false_modification_is_below_2_percent(corpus_results):
    controls = [
        item for item in corpus_results if item.case.expected_action == "keep"
    ]
    modified = [
        item.case.id
        for item in controls
        if item.decision.action is DecisionAction.APPLY
    ]
    assert len(modified) / len(controls) < 0.02, modified


def test_all_high_impact_ambiguities_ask(corpus_results):
    risky = [item for item in corpus_results if item.case.risk == "high-ambiguity"]
    violations = [
        item.case.id for item in risky if item.decision.action is not DecisionAction.ASK
    ]
    assert risky
    assert not violations, violations

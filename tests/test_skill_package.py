"""Integration checks for the portable root Agent Skill package."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from voice_intent_normalizer.lexicon import load_jsonl
from voice_intent_normalizer.models import DecisionAction
from voice_intent_normalizer.paths import StatePaths
from voice_intent_normalizer.service import NormalizeRequest, NormalizerService


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _frontmatter_value(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}: (.+)$", text.split("---", 2)[1], re.M)
    assert match is not None
    return match.group(1).strip().strip('"')


def _local_references(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\]\((references/[^)]+\.md)\)", text))


def _service(tmp_path: Path, repo_root: Path) -> NormalizerService:
    return NormalizerService(
        StatePaths(root=tmp_path / "state"), repo_root / "assets" / "lexicons"
    )


def test_skill_frontmatter_is_portable(repo_root: Path) -> None:
    """Catch a root skill that hosts cannot discover from standard metadata."""
    text = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: voice-intent-normalizer\n")
    assert _frontmatter_value(text, "description") == (
        "Correct Chinese voice-transcription homophones, AI terms, and project "
        "names using local context and explicit user learning."
    )
    assert len(_frontmatter_value(text, "description")) <= 160


def test_skill_references_and_openai_metadata_are_loadable(repo_root: Path) -> None:
    """Catch broken progressive-disclosure links or nonportable UI metadata."""
    text = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    references = _local_references(text)
    assert set(references) == {
        "references/lexicon-schema.md",
        "references/correction-policy.md",
        "references/domain-packs.md",
    }
    assert all((repo_root / reference).is_file() for reference in references)
    metadata = (repo_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert "default_prompt: \"Use $voice-intent-normalizer" in metadata
    assert "dependencies:" not in metadata


def test_builtin_lexicons_validate_and_keep_public_data_public(repo_root: Path) -> None:
    """Catch malformed seed records or accidental inclusion of local learning state."""
    lexicon_root = repo_root / "assets" / "lexicons"
    records = [
        entry for path in lexicon_root.rglob("*.jsonl") for entry in load_jsonl(path)
    ]
    assert records
    assert {entry.canonical for entry in records} >= {"Codex", "OpenClaw", "WorkBuddy"}
    assert all(entry.scope.value in {"base", "hot", "industry"} for entry in records)
    assert all(entry.status.value == "curated" for entry in records)
    assert all(entry.source and "public" in entry.source for entry in records)


@pytest.mark.parametrize(
    ("text", "domains", "expected"),
    (
        ("请用 code X 做这个任务", ("ai",), "Codex"),
        ("帮我配置 open cloud 的智能体", ("ai",), "OpenClaw"),
        ("在 work body 里打开技能", ("ai",), "WorkBuddy"),
    ),
)
def test_builtin_lexicons_normalize_common_agent_misrecognitions(
    tmp_path: Path,
    repo_root: Path,
    text: str,
    domains: tuple[str, ...],
    expected: str,
) -> None:
    """Catch seeds that load but do not correct the supported speech errors."""
    decision = _service(tmp_path, repo_root).normalize(
        NormalizeRequest(text=text, domains=domains)
    )
    assert decision.action is DecisionAction.APPLY
    assert expected in decision.corrected_text


def test_ambiguous_lobster_never_applies_without_ai_context(
    tmp_path: Path, repo_root: Path
) -> None:
    """Catch an unsafe seed that turns an ordinary lobster reference into OpenClaw."""
    decision = _service(tmp_path, repo_root).normalize(
        NormalizeRequest(text="我想吃小龙虾")
    )
    assert decision.action in {DecisionAction.ASK, DecisionAction.KEEP}
    assert decision.corrected_text == "我想吃小龙虾"


def test_bootstrap_normalize_returns_stable_json(
    repo_root: Path, tmp_path: Path
) -> None:
    """Catch a package that documents a bootstrap command unavailable to skill hosts."""
    from voice_intent_normalizer import cli

    output = []

    class Writer:
        def write(self, value: str) -> int:
            output.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    service = _service(tmp_path, repo_root)
    assert cli.main(
        ["normalize", "--text", "配置 open cloud", "--domain", "ai", "--json"],
        service=service,
        stdout=Writer(),
        stderr=Writer(),
    ) == 0
    payload = json.loads("".join(output))
    assert payload["action"] == "apply"
    assert payload["corrected_text"] == "配置 OpenClaw"

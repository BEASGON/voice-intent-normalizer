"""Integration checks for the portable root Agent Skill package."""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path

import pytest

from voice_intent_normalizer import cli
from voice_intent_normalizer.lexicon import LexiconSet, load_jsonl
from voice_intent_normalizer.matching import normalize_alias
from voice_intent_normalizer.models import DecisionAction, Scope
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


def _markdown_matrix(text: str, heading: str) -> dict[str, str]:
    """Read a named two-column policy matrix without relying on row offsets."""
    lines = text.splitlines()
    try:
        heading_index = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"missing policy section: {heading}") from exc
    table_start = next(
        (
            index
            for index in range(heading_index + 1, len(lines) - 1)
            if lines[index].startswith("|")
            and lines[index + 1].replace(" ", "").startswith("|---")
        ),
        None,
    )
    assert table_start is not None, f"missing policy matrix: {heading}"
    headers = [cell.strip() for cell in lines[table_start].strip("|").split("|")]
    assert headers == ["Response state", "Host behavior"]
    matrix: dict[str, str] = {}
    for line in lines[table_start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 2:
            assert cells[0] not in matrix, f"duplicate policy row: {cells[0]}"
            matrix[cells[0]] = cells[1]
    return matrix


def _service(tmp_path: Path, repo_root: Path) -> NormalizerService:
    return NormalizerService(
        StatePaths(root=tmp_path / "state"), repo_root / "assets" / "lexicons"
    )


def _run_cli(service: NormalizerService, *argv: str) -> dict[str, object]:
    output = StringIO()
    assert cli.main(list(argv), service=service, stdout=output, stderr=StringIO()) == 0
    return json.loads(output.getvalue())


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
    assert all(entry.source for entry in records)


def test_builtin_lexicon_identity_aliases_and_provenance_are_unambiguous(
    tmp_path: Path, repo_root: Path
) -> None:
    """Catch conflicting identities, duplicate aliases, or undocumented public seeds."""
    lexicon_root = repo_root / "assets" / "lexicons"
    records = [
        entry for path in lexicon_root.rglob("*.jsonl") for entry in load_jsonl(path)
    ]
    assert len({entry.source for entry in records}) == len(records)
    all_aliases = [
        normalize_alias(alias) for entry in records for alias in entry.aliases
    ]
    assert len(set(all_aliases)) == len(all_aliases)
    for path in lexicon_root.rglob("*.jsonl"):
        entries = load_jsonl(path)
        identities = {
            (entry.canonical, entry.scope, entry.project_id) for entry in entries
        }
        assert len(identities) == len(entries)
        for entry in entries:
            aliases = [normalize_alias(alias) for alias in entry.aliases]
            assert len(set(aliases)) == len(aliases)

    lexicons = LexiconSet.load(
        StatePaths(root=tmp_path / "state"), lexicon_root, domains=("ai",)
    )
    for alias in ("Open Clow", "Open Cloud", "龙虾", "小龙虾"):
        matches = lexicons.by_alias(alias)
        assert matches
        assert any(entry.scope is Scope.INDUSTRY for entry in matches)
        assert len({id(entry) for entry in matches}) == len(matches)

    provenance = (repo_root / "references" / "domain-packs.md").read_text(
        encoding="utf-8"
    )
    assert all(entry.source in provenance for entry in records)


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


def test_ambiguous_lobster_applies_only_with_ai_context(
    tmp_path: Path, repo_root: Path
) -> None:
    """Catch a domain pack that cannot restore its ambiguous alias with evidence."""
    decision = _service(tmp_path, repo_root).normalize(
        NormalizeRequest(text="请配置龙虾智能体", domains=("ai",))
    )
    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "请配置OpenClaw智能体"


def test_explicit_learning_scopes_project_state_and_personal_state(
    tmp_path: Path, repo_root: Path
) -> None:
    """Catch project learning that leaks or personal learning that does not."""
    service = _service(tmp_path, repo_root)
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()
    assert _run_cli(
        service,
        "learn",
        "--alias",
        "alpha voice",
        "--canonical",
        "AlphaVoice",
        "--scope",
        "project",
        "--project-root",
        str(project_one),
        "--json",
    )["status"] == "ok"
    assert service.normalize(
        NormalizeRequest(text="alpha voice", project_root=project_one)
    ).corrected_text == "AlphaVoice"
    assert service.normalize(
        NormalizeRequest(text="alpha voice", project_root=project_two)
    ).action is DecisionAction.KEEP
    assert (
        service.normalize(NormalizeRequest(text="alpha voice")).action
        is DecisionAction.KEEP
    )

    assert _run_cli(
        service,
        "learn",
        "--alias",
        "beta voice",
        "--canonical",
        "BetaVoice",
        "--scope",
        "personal",
        "--json",
    )["status"] == "ok"
    assert service.normalize(
        NormalizeRequest(text="beta voice", project_root=project_one)
    ).corrected_text == "BetaVoice"
    assert service.normalize(
        NormalizeRequest(text="beta voice", project_root=project_two)
    ).corrected_text == "BetaVoice"


def test_nonfatal_diagnostic_keeps_a_valid_builtin_decision(
    tmp_path: Path, repo_root: Path
) -> None:
    """Catch fail-open guidance that discards valid JSON on layer warnings."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "personal.jsonl").write_text("not-json\n", encoding="utf-8")
    service = NormalizerService(
        StatePaths(root=state_root), repo_root / "assets" / "lexicons"
    )
    decision = service.normalize(NormalizeRequest(text="open clow", domains=("ai",)))
    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "OpenClaw"
    assert "personal_invalid" in decision.diagnostics


def test_skill_contract_routes_learning_and_nonfatal_diagnostics(
    repo_root: Path,
) -> None:
    """Catch host instructions that misroute explicit learning or JSON decisions."""
    text = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    assert "--scope project --project-root" in text
    assert "personal or the active project" in text
    assert "valid `apply`, `ask`, or `keep`" in text
    assert "non-fatal diagnostics" in text


def test_policy_reference_matches_skill_response_contract(repo_root: Path) -> None:
    """Catch response rules that alter or act before a valid correction decision."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    matrix = _markdown_matrix(policy, "## Response handling")
    apply = matrix["Valid `apply` action"].casefold()
    assert "corrected_text" in apply and "notices" in apply
    ask = matrix["Valid `ask` action"].casefold()
    assert "question" in ask and "wait" in ask
    assert "never execute" in ask and "choose" in ask
    keep = matrix["Valid `keep` action"].casefold()
    assert "original" in keep and "no correction receipt" in keep
    diagnostics = matrix["Valid action with non-fatal diagnostics"].casefold()
    assert "honor" in diagnostics and "notices" in diagnostics
    assert "personal_invalid" in diagnostics and "read_only_state" in diagnostics
    assert "fail open" in matrix[
        "Command failure, invalid JSON, or no valid action"
    ].casefold()
    assert "fail open" in matrix["`status=degraded` without a decision"].casefold()


def test_bootstrap_normalize_returns_stable_json(
    repo_root: Path, tmp_path: Path
) -> None:
    """Catch a package that documents a bootstrap command unavailable to skill hosts."""
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

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


_CONTRACT_START = "<!-- voice-intent-response-contract:start -->"
_CONTRACT_END = "<!-- voice-intent-response-contract:end -->"
_RESPONSE_PREFIX = (
    "## Response handling\n\n"
    "The following JSON contract is authoritative for hosts.\n\n"
)
_NEXT_SECTION = "## Safety boundaries"
_RESPONSE_HANDLING_CONTRACT = {
    "valid_apply": {"use_text": "corrected_text", "show_notices": True},
    "valid_ask": {
        "show_question": True,
        "wait": True,
        "execute_candidate": False,
    },
    "valid_keep": {
        "use_text": "original_text",
        "show_correction_receipt": False,
    },
    "valid_decision_with_nonfatal_diagnostics": {
        "honor_decision": True,
        "show_notices": True,
        "report_diagnostics": True,
        "examples": ["personal_invalid", "read_only_state"],
    },
    "command_or_response_failure": {
        "fail_open": True,
        "use_text": "original_text",
        "invent_correction": False,
    },
    "degraded_without_decision": {
        "fail_open": True,
        "use_text": "original_text",
        "report_unavailable": True,
    },
}


def _response_contract(text: str) -> dict[str, object]:
    """Read the one fenced JSON contract allowed in the response section."""
    assert text.count(_CONTRACT_START) == 1, "response contract start is not unique"
    assert text.count(_CONTRACT_END) == 1, "response contract end is not unique"
    assert text.count("## Response handling") == 1
    start = text.index(_CONTRACT_START)
    end = text.index(_CONTRACT_END)
    assert start < end
    assert text[:start].endswith(_RESPONSE_PREFIX)
    assert text[start + len(_CONTRACT_START) : end].startswith("\n```json\n")
    assert text[start + len(_CONTRACT_START) : end].endswith("\n```\n")
    block = text[start + len(_CONTRACT_START) : end]
    assert block.count("```") == 2
    payload = block.removeprefix("\n```json\n").removesuffix("\n```\n")
    assert text[end + len(_CONTRACT_END) :].startswith(f"\n{_NEXT_SECTION}\n")
    assert "|" not in text[text.index("## Response handling") : end]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AssertionError("invalid response contract JSON") from exc
    assert isinstance(parsed, dict)
    return parsed


def _assert_response_handling_contract(contract: dict[str, object]) -> None:
    """Check the stable contract with hand-authored, independent expected values."""
    assert contract == _RESPONSE_HANDLING_CONTRACT


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
    _assert_response_handling_contract(_response_contract(policy))


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            '"use_text": "corrected_text"',
            '"use_text": "original_text"',
        ),
        (
            '"execute_candidate": false',
            '"execute_candidate": true',
        ),
        (
            '"show_correction_receipt": false',
            '"show_correction_receipt": true',
        ),
        (
            '"honor_decision": true',
            '"honor_decision": false',
        ),
        (
            '"invent_correction": false',
            '"invent_correction": true',
        ),
        (
            '"report_unavailable": true',
            '"report_unavailable": false',
        ),
    ),
)
def test_response_contract_rejects_semantic_reversals(
    repo_root: Path, before: str, after: str
) -> None:
    """Catch a JSON field reversal without relying on prose keyword matching."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    mutated = policy.replace(before, after, 1)
    assert mutated != policy
    with pytest.raises(AssertionError):
        _assert_response_handling_contract(_response_contract(mutated))


def test_response_contract_rejects_marker_scope_and_extra_instructions(
    repo_root: Path,
) -> None:
    """Catch moved or repeated sentinels and any extra instruction in its section."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    moved = policy.replace(
        _RESPONSE_PREFIX,
        "## Response handling\n\n## Unrelated\n\n"
        "The following JSON contract is authoritative for hosts.\n\n",
        1,
    )
    duplicate = policy.replace(_CONTRACT_START, f"{_CONTRACT_START}\n{_CONTRACT_START}")
    conflict = policy.replace(
        _CONTRACT_START,
        "GFM without a leading pipe | hostile\\|instruction | extra\n"
        + _CONTRACT_START,
    )
    for mutated in (moved, duplicate, conflict):
        with pytest.raises(AssertionError):
            _response_contract(mutated)


def test_response_contract_rejects_key_changes_and_allows_json_formatting(
    repo_root: Path,
) -> None:
    """Catch missing/extra keys while permitting harmless JSON ordering and indent."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    extra = policy.replace("{\n", '{\n  "unexpected": true,\n', 1)
    missing = policy.replace('  "valid_apply": {\n', "", 1).replace(
        '    "use_text": "corrected_text",\n    "show_notices": true\n  },\n',
        "",
        1,
    )
    for mutated in (extra, missing):
        with pytest.raises(AssertionError):
            _assert_response_handling_contract(_response_contract(mutated))

    reordered_json = json.dumps(_RESPONSE_HANDLING_CONTRACT, indent=4, sort_keys=True)
    reformatted = re.sub(
        r"(?s)(```json\n).*?(\n```)", rf"\1{reordered_json}\2", policy, count=1
    )
    _assert_response_handling_contract(_response_contract(reformatted))


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

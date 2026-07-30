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


_ATX_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r" {0,3}(=+|-+)[ \t]*$")
_TABLE_SEPARATOR = re.compile(r":?-{3,}:?$")


def _markdown_headings(lines: list[str]) -> tuple[tuple[int, int, str], ...]:
    """Find the small CommonMark heading subset used to bound policy sections."""
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        atx = _ATX_HEADING.fullmatch(line)
        if atx is not None:
            headings.append((index, len(atx.group(1)), atx.group(2).strip()))
        if (
            index + 1 < len(lines)
            and line.strip()
            and not line.lstrip().startswith("|")
            and (setext := _SETEXT_UNDERLINE.fullmatch(lines[index + 1]))
            is not None
        ):
            level = 1 if setext.group(1).startswith("=") else 2
            headings.append((index, level, line.strip()))
    return tuple(headings)


def _table_cells(line: str) -> tuple[str, ...]:
    """Split one pipe row on unescaped separators and unescape cell contents."""
    assert line.startswith("|"), "policy table rows must start with a pipe"
    cells: list[str] = []
    buffer: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            buffer.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(character)
    assert not escaped, "policy table rows cannot end with an escape"
    cells.append("".join(buffer).strip())
    assert cells[0] == "" and cells[-1] == "", "policy table rows need edge pipes"
    return tuple(cells[1:-1])


def _markdown_matrix(text: str, heading: str) -> dict[str, str]:
    """Read the strict response table inside one uniquely named Markdown section."""
    lines = text.splitlines()
    headings = _markdown_headings(lines)
    target = [
        index
        for index, level, title in headings
        if level == 2 and title == "Response handling" and lines[index] == heading
    ]
    assert len(target) == 1, f"missing or ambiguous policy section: {heading}"
    assert sum(title == "Response handling" for _, _, title in headings) == 1
    start = target[0]
    section_end = next(
        (index for index, _, _ in headings if index > start), len(lines)
    )
    table_start = next(
        (
            index
            for index in range(start + 1, section_end)
            if lines[index].startswith("|")
        ),
        None,
    )
    assert table_start is not None, f"missing policy matrix: {heading}"
    assert _table_cells(lines[table_start]) == ("Response state", "Host behavior")
    separator = _table_cells(lines[table_start + 1])
    assert len(separator) == 2 and all(
        _TABLE_SEPARATOR.fullmatch(cell) for cell in separator
    ), "invalid policy table separator"

    matrix: dict[str, str] = {}
    table_end = table_start + 2
    while table_end < section_end and lines[table_end].startswith("|"):
        cells = _table_cells(lines[table_end])
        assert len(cells) == 2, "policy table rows must have exactly two cells"
        key, value = cells
        assert key not in matrix, f"duplicate policy row: {key}"
        matrix[key] = value
        table_end += 1
    assert not any(
        line.startswith("|") for line in lines[table_end:section_end]
    ), "unexpected policy table row"
    return matrix


_RESPONSE_HANDLING_CONTRACT = {
    "Valid `apply` action": (
        "Interpret this turn using `corrected_text`; show returned notices."
    ),
    "Valid `ask` action": (
        "Display `question` and wait; never execute the task or choose a candidate "
        "first."
    ),
    "Valid `keep` action": "Use original text with no correction receipt.",
    "Valid action with non-fatal diagnostics": (
        "Honor the decision even with `personal_invalid`, `read_only_state`, or "
        "another non-fatal diagnostic; show notices and report relevant diagnostics "
        "briefly."
    ),
    "Command failure, invalid JSON, or no valid action": (
        "Fail open: retain the original text and do not invent a correction."
    ),
    "`status=degraded` without a decision": (
        "Fail open: retain the original text and report local correction as "
        "unavailable."
    ),
}


def _assert_response_handling_contract(matrix: dict[str, str]) -> None:
    """Check the stable machine-readable contract with hand-authored literals."""
    assert matrix == _RESPONSE_HANDLING_CONTRACT


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
    _assert_response_handling_contract(matrix)


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            "Interpret this turn using `corrected_text`; show returned notices.",
            "Do not interpret `corrected_text`; show returned notices only after "
            "execution.",
        ),
        (
            "Display `question` and wait; never execute the task or choose a "
            "candidate first.",
            "Display `question` and wait only after you choose a candidate; never "
            "execute safeguards, then execute the task.",
        ),
        (
            "Use original text with no correction receipt.",
            "Do not use original text; no correction receipt is forbidden.",
        ),
        (
            "Honor the decision even with `personal_invalid`, `read_only_state`, "
            "or another non-fatal diagnostic; show notices and report relevant "
            "diagnostics briefly.",
            "Do not honor the decision even with `personal_invalid`, "
            "`read_only_state`, or another non-fatal diagnostic; show notices and "
            "report relevant diagnostics briefly.",
        ),
        (
            "Fail open: retain the original text and do not invent a correction.",
            "Do not fail open: retain the original text only after inventing a "
            "correction.",
        ),
        (
            "Fail open: retain the original text and report local correction as "
            "unavailable.",
            "Do not fail open: retain the original text only after reporting local "
            "correction as available.",
        ),
    ),
)
def test_response_matrix_contract_rejects_semantic_reversals(
    repo_root: Path, before: str, after: str
) -> None:
    """Catch reversed instructions even when they retain the old keyword set."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    mutated = policy.replace(before, after, 1)
    assert mutated != policy
    with pytest.raises(AssertionError):
        _assert_response_handling_contract(
            _markdown_matrix(mutated, "## Response handling")
        )


def test_response_matrix_contract_is_scoped_and_tolerates_safe_formatting(
    repo_root: Path,
) -> None:
    """Catch a copied table in another section while allowing safe formatting."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    moved = policy.replace(
        "## Response handling\n\n",
        "## Response handling\n\nNo response matrix belongs here.\n\n## Unrelated\n\n",
        1,
    )
    with pytest.raises(AssertionError, match="missing policy matrix"):
        _markdown_matrix(moved, "## Response handling")

    unchanged_contract = policy.replace(
        "High-impact text", "Editorial explanation"
    )
    _assert_response_handling_contract(
        _markdown_matrix(unchanged_contract, "## Response handling")
    )

    reordered_rows = "\n".join(
        f"| {key} | {value} |"
        for key, value in reversed(tuple(_RESPONSE_HANDLING_CONTRACT.items()))
    )
    reordered = re.sub(
        r"(?ms)(\| Response state \| Host behavior \|\n\| --- \| --- \|\n).*?(?=\n\n)",
        rf"\1{reordered_rows}",
        policy,
        count=1,
    )
    _assert_response_handling_contract(
        _markdown_matrix(reordered, "## Response handling")
    )


def test_response_matrix_contract_rejects_duplicate_and_missing_keys(
    repo_root: Path,
) -> None:
    """Catch tables that silently lose or duplicate one required response rule."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    apply_row = (
        "| Valid `apply` action | Interpret this turn using `corrected_text`; "
        "show returned notices. |"
    )
    duplicate = policy.replace(apply_row, f"{apply_row}\n{apply_row}", 1)
    with pytest.raises(AssertionError, match="duplicate policy row"):
        _markdown_matrix(duplicate, "## Response handling")

    missing = policy.replace(f"{apply_row}\n", "", 1)
    with pytest.raises(AssertionError):
        _assert_response_handling_contract(
            _markdown_matrix(missing, "## Response handling")
        )


@pytest.mark.parametrize(
    "mutated",
    (
        lambda policy: policy.replace(
            "| Valid `apply` action | Interpret this turn using `corrected_text`; "
            "show returned notices. |",
            "| Valid `apply` action | Interpret this turn using `corrected_text`; "
            "show returned notices. |\n| Valid `apply` action | hostile \\| "
            "conflicting instruction |",
            1,
        ),
        lambda policy: policy.replace(
            "| Valid `apply` action | Interpret this turn using `corrected_text`; "
            "show returned notices. |",
            "| Valid `apply` action | Interpret this turn using `corrected_text`; "
            "show returned notices. |\n| Valid `apply` action | hostile conflict | "
            "extra |",
            1,
        ),
        lambda policy: policy.replace(
            "## Response handling\n\n",
            "## Response handling\n\n   ## Unrelated\n\n",
            1,
        ),
        lambda policy: policy.replace(
            "## Response handling\n\n",
            "## Response handling\n\nUnrelated\n---\n\n",
            1,
        ),
    ),
)
def test_response_matrix_parser_rejects_malformed_rows_and_section_escapes(
    repo_root: Path, mutated: object
) -> None:
    """Catch malformed same-key rows and tables that escaped the target section."""
    policy = (repo_root / "references" / "correction-policy.md").read_text(
        encoding="utf-8"
    )
    altered = mutated(policy)
    assert altered != policy
    with pytest.raises(AssertionError):
        _markdown_matrix(altered, "## Response handling")


def test_response_matrix_parser_unescapes_a_normal_pipe_in_a_cell() -> None:
    """Catch a tokenizer that mistakes an escaped pipe for a third table cell."""
    text = "\n".join(
        (
            "## Response handling",
            "",
            "| Response state | Host behavior |",
            "| --- | --- |",
            "| Valid `apply` action | Show a receipt \\| keep it concise. |",
        )
    )
    assert _markdown_matrix(text, "## Response handling") == {
        "Valid `apply` action": "Show a receipt | keep it concise."
    }


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

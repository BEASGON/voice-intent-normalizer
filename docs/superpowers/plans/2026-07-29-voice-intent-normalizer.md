# Voice Intent Normalizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first Agent Skill that corrects Chinese voice-transcription intent with layered lexicons, project context, explicit learning, safe decisions, and adapters for Codex, OpenClaw, WorkBuddy, and generic Agent Skills hosts.

**Architecture:** A platform-independent Python core owns lexicon loading, candidate generation, scoring, policy, learning, project scanning, and hotword updates. A shared root `SKILL.md` invokes that core, while small adapters own only platform discovery, installation, automatic-trigger configuration, verification, and removal. All installed adapters on one machine use the same `VOICE_INTENT_HOME` state unless the user explicitly overrides it.

**Tech Stack:** Python 3.10+, standard library, optional `pypinyin>=0.55,<1`, JSON/JSONL, `pytest`, `pytest-cov`, `ruff`, Agent Skills `SKILL.md`, Codex `AGENTS.md` and `UserPromptSubmit` hooks.

## Global Constraints

- Support Python 3.10 and newer.
- Use no mandatory third-party runtime dependency; `pypinyin>=0.55,<1` is optional.
- Never install a dependency silently at runtime.
- Store lexicons as UTF-8 JSONL with one validated entry per line.
- Keep project, conversation, and personal data local; never send them during hotword updates.
- Ignore `.git`, dependency folders, build outputs, binaries, `.env`, keys, tokens, and credential files during project scans.
- Download hotword data only over HTTPS from an explicit host allowlist; accept data only, verify SHA-256, validate schema, write atomically, and retain the last valid version.
- Treat file paths, commands, code symbols, numbers, destructive operations, publishing, deployment, and permission changes as high impact.
- Correct the Agent's interpretation after message submission; never claim to edit live dictation or the original input-box text.
- Use quiet correction only for low-impact high-confidence changes, show a short receipt for important proper nouns, and ask before consequential ambiguity.
- Keep internal learning states out of normal user-facing copy.
- Preserve existing platform configuration and remove only content marked as belonging to this project.
- Use the canonical product spellings `Codex`, `OpenClaw`, and `WorkBuddy`.
- License project code under Apache-2.0 and reject copied restricted commercial dictionaries.
- When implementing `SKILL.md`, use `skill-creator` and `superpowers:writing-skills`; validate the final skill package before release.

## Planned File Map

```text
voice-intent-normalizer/
├── SKILL.md                              # Shared host instructions and invocation contract
├── agents/openai.yaml                    # Codex/ChatGPT skill metadata
├── pyproject.toml                        # Python package, optional dependency, test/lint config
├── scripts/voice_intent.py               # Zero-install bootstrap entrypoint for skill hosts
├── src/voice_intent_normalizer/
│   ├── __init__.py                       # Public version and API exports
│   ├── models.py                         # Immutable domain models and enums
│   ├── paths.py                          # Shared state and project identity paths
│   ├── lexicon.py                        # JSONL validation, loading, precedence
│   ├── matching.py                       # Text/pinyin normalization and candidates
│   ├── policy.py                         # Risk detection, decisions, correction receipts
│   ├── service.py                        # End-to-end normalization orchestration
│   ├── project_scan.py                   # Privacy-preserving project term extraction
│   ├── learning.py                       # Explicit corrections, rejection, undo, export
│   ├── updater.py                        # Secure hotword manifest/data update
│   ├── hook.py                           # Codex UserPromptSubmit JSON adapter
│   ├── installer.py                      # Multi-platform install/doctor/uninstall coordinator
│   ├── cli.py                            # User and host command-line surface
│   └── adapters/
│       ├── base.py                       # Adapter protocol and result types
│       ├── codex.py                      # Codex skill, AGENTS, and hook integration
│       ├── openclaw.py                   # OpenClaw CLI/global-skill integration
│       ├── workbuddy.py                  # WorkBuddy import archive builder
│       └── generic.py                    # Standards-only local skill package
├── adapters/
│   ├── codex/AGENTS.snippet.md           # Marked global auto-trigger guidance
│   ├── codex/hooks.template.json         # Strict-mode hook template
│   ├── openclaw/README.zh-CN.md           # Supported install scopes and verification
│   ├── workbuddy/README.zh-CN.md          # Import, enable, test, disable, uninstall
│   └── generic/README.zh-CN.md            # Host capability checklist
├── references/
│   ├── lexicon-schema.md                 # Public JSONL schema
│   ├── correction-policy.md              # Confidence, receipt, and safety rules
│   ├── domain-packs.md                    # Domain activation and contribution rules
│   └── platform-compatibility.md          # Verified features and downgrade matrix
├── assets/lexicons/
│   ├── base-zh.jsonl                     # Stable built-in entries
│   ├── hotwords-snapshot.jsonl            # Offline public fallback
│   └── domains/
│       ├── ai.jsonl
│       ├── software-development.jsonl
│       └── product-design.jsonl
├── tests/
│   ├── fixtures/corpus.jsonl              # Chinese speech-error evaluation corpus
│   ├── test_lexicon.py
│   ├── test_matching.py
│   ├── test_policy.py
│   ├── test_service.py
│   ├── test_project_scan.py
│   ├── test_learning.py
│   ├── test_updater.py
│   ├── test_hook.py
│   ├── test_installer.py
│   ├── test_adapters.py
│   ├── test_skill_package.py
│   └── test_corpus.py
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
├── CHANGELOG.md
└── LICENSE
```

---

### Task 1: Package Foundation and Lexicon Schema

**Files:**
- Create: `pyproject.toml`
- Create: `src/voice_intent_normalizer/__init__.py`
- Create: `src/voice_intent_normalizer/models.py`
- Create: `src/voice_intent_normalizer/lexicon.py`
- Create: `tests/test_lexicon.py`

**Interfaces:**
- Produces: `Scope`, `EntryStatus`, `LexiconEntry`, `Candidate`, `DecisionAction`, `CorrectionDecision`.
- Produces: `parse_entry(raw, expected_scope=None) -> LexiconEntry`.
- Produces: `load_jsonl(path, expected_scope=None) -> tuple[LexiconEntry, ...]`.
- Produces: `write_jsonl_atomic(path, entries) -> None`.

- [ ] **Step 1: Write failing schema tests**

```python
from voice_intent_normalizer.lexicon import load_jsonl, parse_entry
from voice_intent_normalizer.models import Scope


def test_parse_entry_normalizes_collections():
    entry = parse_entry({
        "canonical": "OpenClaw",
        "scope": "hot",
        "aliases": ["Open Cloud", "龙虾"],
        "domains": ["ai", "agent"],
        "weight": 0.9,
        "status": "curated",
    })
    assert entry.canonical == "OpenClaw"
    assert entry.scope is Scope.HOT
    assert entry.aliases == ("Open Cloud", "龙虾")


def test_load_jsonl_rejects_scope_mismatch(tmp_path):
    path = tmp_path / "personal.jsonl"
    path.write_text(
        '{"canonical":"Codex","scope":"hot","aliases":["code X"],'
        '"domains":["ai"],"weight":0.9,"status":"curated"}\n',
        encoding="utf-8",
    )
    try:
        load_jsonl(path, expected_scope=Scope.PERSONAL)
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("scope mismatch was accepted")
```

- [ ] **Step 2: Run the schema tests and confirm they fail**

Run: `python -m pytest tests/test_lexicon.py -v`
Expected: FAIL because `voice_intent_normalizer.lexicon` does not exist.

- [ ] **Step 3: Add package metadata and immutable models**

Configure `pyproject.toml` with `requires-python = ">=3.10"`, a `src` layout, no required dependencies, optional group `pinyin = ["pypinyin>=0.55,<1"]`, and dev dependencies for pytest, coverage, ruff, and build. Define frozen dataclasses and string enums; reject blank canonical names, empty aliases, weights outside `0.0..1.0`, and unknown scopes/status values.

```python
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
    negative_aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    canonical: str
    original: str
    replacement_span: tuple[int, int]
    score: float
    evidence: tuple[str, ...]
    entry: LexiconEntry


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
```

- [ ] **Step 4: Implement strict JSONL loading and atomic writing**

Each error must include the path and 1-based line number. Atomic writes use a sibling temporary file, `flush()`, `os.fsync()`, then `os.replace()`. Serialization uses `ensure_ascii=False`, stable keys, and exactly one trailing newline.

- [ ] **Step 5: Run tests and lint**

Run: `python -m pytest tests/test_lexicon.py -v && python -m ruff check src tests`
Expected: all tests PASS and ruff exits `0`.

- [ ] **Step 6: Commit the schema foundation**

```bash
git add pyproject.toml src/voice_intent_normalizer tests/test_lexicon.py
git commit -m "feat: add validated lexicon schema"
```

### Task 2: Shared State Paths and Layered Lexicon Loading

**Files:**
- Create: `src/voice_intent_normalizer/paths.py`
- Modify: `src/voice_intent_normalizer/lexicon.py`
- Create: `tests/test_paths.py`
- Extend: `tests/test_lexicon.py`

**Interfaces:**
- Consumes: `LexiconEntry`, `Scope`, `load_jsonl`.
- Produces: `StatePaths.resolve(environ=None, home=None) -> StatePaths`.
- Produces: `StatePaths.for_project(project_root) -> ProjectPaths`.
- Produces: `LexiconSet.load(state_paths, builtins_root, project_root=None, domains=()) -> LexiconSet`.
- Produces: `LexiconSet.entries -> tuple[LexiconEntry, ...]` in precedence order personal, project, industry, hot, base.

- [ ] **Step 1: Write failing path and precedence tests**

```python
def test_voice_intent_home_overrides_default(tmp_path):
    paths = StatePaths.resolve(
        environ={"VOICE_INTENT_HOME": str(tmp_path / "custom")},
        home=tmp_path / "home",
    )
    assert paths.root == (tmp_path / "custom").resolve()


def test_project_ids_are_stable_and_isolated(tmp_path):
    paths = StatePaths.resolve(environ={}, home=tmp_path)
    first = paths.for_project(tmp_path / "alpha")
    second = paths.for_project(tmp_path / "beta")
    assert first.project_id == paths.for_project(tmp_path / "alpha").project_id
    assert first.project_id != second.project_id


def test_personal_alias_wins_over_hot_alias(layer_fixture):
    lexicons = LexiconSet.load(**layer_fixture)
    entries = lexicons.by_alias("work body")
    assert entries[0].scope is Scope.PERSONAL
```

- [ ] **Step 2: Run tests and confirm missing interfaces**

Run: `python -m pytest tests/test_paths.py tests/test_lexicon.py -v`
Expected: FAIL on missing `StatePaths` and `LexiconSet`.

- [ ] **Step 3: Implement shared paths and stable project identity**

Use `VOICE_INTENT_HOME` when nonblank; otherwise use `~/.voice-intent-normalizer`. Compute `project_id` as the first 16 hexadecimal characters of SHA-256 over the normalized absolute project path. Expose personal, preferences, hotword, adapter-status, project lexicon, and scan-state paths without creating them during read-only resolution.

- [ ] **Step 4: Implement layered loading and duplicate handling**

Deduplicate exact `(canonical, scope, project_id)` records by keeping the last valid record inside a file. Do not merge project entries from a different `project_id`. Index normalized aliases without changing the original display value.

- [ ] **Step 5: Run focused and full tests**

Run: `python -m pytest tests/test_paths.py tests/test_lexicon.py -v`
Expected: PASS.

- [ ] **Step 6: Commit shared state loading**

```bash
git add src/voice_intent_normalizer/paths.py src/voice_intent_normalizer/lexicon.py tests
git commit -m "feat: load layered lexicons from shared state"
```

### Task 3: Chinese, English, and Pinyin Candidate Generation

**Files:**
- Create: `src/voice_intent_normalizer/matching.py`
- Create: `tests/test_matching.py`

**Interfaces:**
- Consumes: `LexiconSet`, `LexiconEntry`, `Candidate`.
- Produces: `normalize_alias(value: str) -> str`.
- Produces: `phonetic_key(value: str) -> str | None`.
- Produces: `MatchContext(domains, project_terms, conversation_terms)`.
- Produces: `generate_candidates(text, lexicons, context) -> tuple[Candidate, ...]`.

- [ ] **Step 1: Write failing matching tests**

```python
def test_open_cloud_matches_openclaw(ai_lexicons):
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


def test_work_body_matches_case_and_spacing(workbuddy_lexicons):
    candidates = generate_candidates(
        "在 Work   Body 里面调用",
        workbuddy_lexicons,
        MatchContext(domains=frozenset({"ai"})),
    )
    assert candidates[0].canonical == "WorkBuddy"


def test_missing_pypinyin_keeps_alias_matching(monkeypatch, ai_lexicons):
    monkeypatch.setattr("voice_intent_normalizer.matching._load_pypinyin", lambda: None)
    candidates = generate_candidates(
        "使用 code X",
        ai_lexicons,
        MatchContext(domains=frozenset({"software-development"})),
    )
    assert candidates[0].canonical == "Codex"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_matching.py -v`
Expected: FAIL because the matching module is absent.

- [ ] **Step 3: Implement deterministic normalization**

Case-fold Latin text, normalize Unicode with NFKC, collapse whitespace and `-_/` separators for comparison, but retain exact source spans for replacement. Load `pypinyin` lazily; when unavailable, return `None` and continue with explicit aliases and normalized text.

```python
@dataclass(frozen=True, slots=True)
class MatchContext:
    domains: frozenset[str] = frozenset()
    project_terms: frozenset[str] = frozenset()
    conversation_terms: frozenset[str] = frozenset()


def phonetic_key(value: str) -> str | None:
    provider = _load_pypinyin()
    if provider is None:
        return None
    syllables = provider.lazy_pinyin(unicodedata.normalize("NFKC", value))
    return "-".join(syllables).casefold()
```

- [ ] **Step 4: Implement explainable candidate scoring**

Score components are bounded and recorded in `Candidate.evidence`: exact confirmed alias `+0.55`, normalized alias `+0.45`, phonetic match `+0.35`, domain overlap `+0.20`, project exact term `+0.25`, conversation mention `+0.15`, entry weight scaled by `0.20`, negative alias `-1.0`. Clamp the final value to `0.0..1.0`; order by score, layer precedence, then canonical string.

- [ ] **Step 5: Run matching tests with and without optional pinyin**

Run: `python -m pytest tests/test_matching.py -v`
Expected: PASS without `pypinyin`.

Run after installing the declared optional dev extra in the test environment: `python -m pytest tests/test_matching.py -v -k pinyin`
Expected: pinyin-specific tests PASS.

- [ ] **Step 6: Commit candidate generation**

```bash
git add src/voice_intent_normalizer/matching.py tests/test_matching.py
git commit -m "feat: generate explainable voice correction candidates"
```

### Task 4: Safety Policy, Decisions, and Correction Receipts

**Files:**
- Create: `src/voice_intent_normalizer/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Consumes: ordered `Candidate` values and `MatchContext`.
- Produces: `detect_risk(text: str) -> RiskAssessment`.
- Produces: `decide(text, candidates, context, notified_pairs=frozenset()) -> CorrectionDecision`.
- Decision thresholds: apply at `>=0.85` with margin `>=0.15`; ask at `>=0.65` when consequential; otherwise keep.

- [ ] **Step 1: Write failing policy tests**

```python
def test_literal_lobster_is_not_changed(openclaw_candidate):
    decision = decide(
        "今天晚上想吃龙虾",
        (openclaw_candidate,),
        MatchContext(domains=frozenset({"food"})),
    )
    assert decision.action is DecisionAction.KEEP


def test_ai_lobster_receives_first_use_receipt(openclaw_candidate):
    decision = decide(
        "给龙虾安装这个 Agent 技能",
        (openclaw_candidate,),
        MatchContext(domains=frozenset({"ai", "agent"})),
    )
    assert decision.action is DecisionAction.APPLY
    assert decision.notices == ("已按 OpenClaw 理解（原转写：龙虾）",)


def test_destructive_target_ambiguity_asks(delete_candidates):
    decision = decide(
        "删除发布目录",
        delete_candidates,
        MatchContext(domains=frozenset({"software-development"})),
    )
    assert decision.action is DecisionAction.ASK
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_policy.py -v`
Expected: FAIL because `policy.py` is absent.

- [ ] **Step 3: Implement risk detection**

Detect paths, command-shaped text, semantic versions, dates, percentages, money, code identifiers, and Chinese high-impact verbs including `删除`, `覆盖`, `发布`, `部署`, `提交`, `推送`, `授权`, and `付款`. A risk match never blocks the user by itself; it raises the evidence required for automatic application.

```python
@dataclass(frozen=True, slots=True)
class RiskAssessment:
    high_impact: bool
    reasons: tuple[str, ...]


def detect_risk(text: str) -> RiskAssessment:
    reasons = tuple(name for name, pattern in RISK_PATTERNS if pattern.search(text))
    return RiskAssessment(high_impact=bool(reasons), reasons=reasons)
```

- [ ] **Step 4: Implement decisions and receipt suppression**

Apply replacements from right to left by span. Show a receipt for first-use proper nouns and cross-language replacements. Suppress a receipt only when `(normalized_original, canonical)` exists in `notified_pairs`. Never suppress an `ASK` decision.

- [ ] **Step 5: Run policy tests**

Run: `python -m pytest tests/test_policy.py -v`
Expected: PASS.

- [ ] **Step 6: Commit policy**

```bash
git add src/voice_intent_normalizer/policy.py tests/test_policy.py
git commit -m "feat: add conservative correction policy"
```

### Task 5: Privacy-Preserving Project Scanner

**Files:**
- Create: `src/voice_intent_normalizer/project_scan.py`
- Create: `tests/test_project_scan.py`

**Interfaces:**
- Consumes: `StatePaths`, `LexiconEntry`, atomic JSONL writer.
- Produces: `scan_project(root, state_paths, max_files=5000, max_text_bytes=2_000_000) -> ScanResult`.
- Produces: project-scoped entries with `project_id`, `source`, and no copied source paragraphs.

- [ ] **Step 1: Write failing scanner tests**

```python
def test_scanner_extracts_names_without_secrets(tmp_path, state_paths):
    (tmp_path / "README.md").write_text("# 星河工作台\n使用 WorkBuddyAdapter\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_TOKEN=secret-value\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("IgnoredSecret", encoding="utf-8")

    result = scan_project(tmp_path, state_paths)
    canonicals = {entry.canonical for entry in result.entries}
    assert "星河工作台" in canonicals
    assert "WorkBuddyAdapter" in canonicals
    assert "secret-value" not in canonicals
    assert "IgnoredSecret" not in canonicals


def test_scanner_stores_locations_not_paragraphs(tmp_path, state_paths):
    (tmp_path / "docs.md").write_text("客户产品名是云雀引擎，后面是一大段说明。", encoding="utf-8")
    result = scan_project(tmp_path, state_paths)
    entry = next(item for item in result.entries if item.canonical == "云雀引擎")
    assert entry.source == "docs.md"
    assert "一大段说明" not in entry.source
```

- [ ] **Step 2: Run scanner tests and confirm failure**

Run: `python -m pytest tests/test_project_scan.py -v`
Expected: FAIL on missing scanner.

- [ ] **Step 3: Implement bounded safe traversal**

Skip hidden credential names, configured exclusions, `.git`, `.hg`, `.svn`, `node_modules`, `vendor`, `.venv`, `dist`, `build`, `coverage`, and common binary extensions. Stop at both limits and return `truncated=True`; do not fail the user task.

- [ ] **Step 4: Implement conservative term extraction**

Extract file/directory stems, CamelCase and snake_case identifiers, Markdown headings, and quoted Chinese names of 2–20 characters. Store only canonical term, relative source path, type, and frequency. Write results to the current project's isolated JSONL cache.

- [ ] **Step 5: Run scanner tests**

Run: `python -m pytest tests/test_project_scan.py -v`
Expected: PASS.

- [ ] **Step 6: Commit project scanning**

```bash
git add src/voice_intent_normalizer/project_scan.py tests/test_project_scan.py
git commit -m "feat: build private project lexicons"
```

### Task 6: Explicit Learning, Rejection, Undo, and Export

**Files:**
- Create: `src/voice_intent_normalizer/learning.py`
- Create: `tests/test_learning.py`

**Interfaces:**
- Consumes: shared/project state paths and lexicon atomic writer.
- Produces: `parse_control(text: str) -> ControlCommand | None`.
- Produces: `LearningStore.confirm(alias, canonical, scope, project_id=None)`.
- Produces: `LearningStore.observe(alias, canonical, source) -> EntryStatus`.
- Produces: `LearningStore.reject(alias, canonical)`.
- Produces: `LearningStore.undo_last() -> LearningEvent | None`.
- Produces: `LearningStore.personal_entries() -> tuple[LexiconEntry, ...]`.
- Produces: `LearningStore.list_recent(limit=20)`, `delete(canonical)`, and `export(path)`.

```python
@dataclass(frozen=True, slots=True)
class ControlCommand:
    kind: str
    alias: str | None = None
    canonical: str | None = None


@dataclass(frozen=True, slots=True)
class LearningEvent:
    event_id: str
    timestamp: str
    action: str
    alias: str
    canonical: str
    status: EntryStatus
    scope: Scope | None = None
    project_id: str | None = None
```

- [ ] **Step 1: Write failing natural-language control tests**

```python
@pytest.mark.parametrize(
    ("text", "kind", "alias", "canonical"),
    [
        ("我说的是 OpenClaw，不是 Open Cloud。", "confirm", "Open Cloud", "OpenClaw"),
        ("以后把 Work body 理解为 WorkBuddy。", "confirm", "Work body", "WorkBuddy"),
        ("不要把龙虾改成 OpenClaw。", "reject", "龙虾", "OpenClaw"),
    ],
)
def test_parse_control(text, kind, alias, canonical):
    command = parse_control(text)
    assert command.kind == kind
    assert command.alias == alias
    assert command.canonical == canonical


def test_undo_removes_last_learning_event(tmp_path):
    store = LearningStore.for_root(tmp_path)
    store.confirm("Open Cloud", "OpenClaw", Scope.PERSONAL)
    event = store.undo_last()
    assert event.canonical == "OpenClaw"
    assert store.list_recent() == ()


def test_repetition_does_not_silently_confirm(tmp_path):
    store = LearningStore.for_root(tmp_path)
    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.CANDIDATE
    assert store.observe("扣的克斯", "Codex", "conversation") is EntryStatus.REPEATED
    assert store.personal_entries() == ()
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_learning.py -v`
Expected: FAIL because the learning module is absent.

- [ ] **Step 3: Implement explicit control parsing**

Support the six phrases listed in the approved design. Parse only explicit grammatical patterns; do not infer a permanent mapping from an ordinary sentence. Normalize surrounding Chinese/English punctuation without changing captured spelling.

- [ ] **Step 4: Implement auditable event storage**

Append an event with UUID, UTC timestamp, action, alias, canonical, scope, and project ID to `learning-events.jsonl`, then materialize personal/project lexicons atomically. `undo_last` adds a compensating event and rebuilds state; it never rewrites history invisibly.

`observe` records first and repeated sightings as `CANDIDATE` and `REPEATED`, but neither state enters the active personal lexicon. Only `confirm` creates `CONFIRMED`; `reject` creates a `REJECTED` negative mapping.

```python
def observe(self, alias: str, canonical: str, source: str) -> EntryStatus:
    previous = self._latest_observation(alias, canonical)
    status = EntryStatus.REPEATED if previous is not None else EntryStatus.CANDIDATE
    self._append_event("observe", alias, canonical, status=status, source=source)
    return status
```

- [ ] **Step 5: Run learning tests**

Run: `python -m pytest tests/test_learning.py -v`
Expected: PASS.

- [ ] **Step 6: Commit learning controls**

```bash
git add src/voice_intent_normalizer/learning.py tests/test_learning.py
git commit -m "feat: learn corrections from explicit feedback"
```

### Task 7: Secure, Independent Hotword Updates

**Files:**
- Create: `src/voice_intent_normalizer/updater.py`
- Create: `tests/test_updater.py`

**Interfaces:**
- Consumes: `StatePaths`, JSONL validation, atomic writes.
- Produces: `HotwordManifest`, `UpdateStatus`, `UpdateResult`.
- Produces: `update_hotwords(paths, manifest_url, fetcher, now) -> UpdateResult`.

```python
@dataclass(frozen=True, slots=True)
class HotwordManifest:
    schema_version: int
    version: str
    data_url: str
    sha256: str


class UpdateStatus(str, Enum):
    UPDATED = "updated"
    CURRENT = "current"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    status: UpdateStatus
    version: str | None = None
    message: str | None = None
```

- [ ] **Step 1: Write failing updater tests**

```python
def test_valid_update_replaces_hotwords(tmp_path):
    data = b'{"canonical":"OpenClaw","scope":"hot","aliases":["Open Cloud"],"domains":["ai"],"weight":0.9,"status":"curated"}\n'
    digest = hashlib.sha256(data).hexdigest()
    fetcher = FakeFetcher({
        "https://github.com/BEASGON/voice-intent-normalizer/releases/download/data/manifest.json":
            json.dumps({
                "schema_version": 1,
                "version": "2026.07.29",
                "data_url": "https://github.com/BEASGON/voice-intent-normalizer/releases/download/data/zh-ai.jsonl",
                "sha256": digest,
            }).encode(),
        "https://github.com/BEASGON/voice-intent-normalizer/releases/download/data/zh-ai.jsonl": data,
    })
    result = update_hotwords(paths_for(tmp_path), MANIFEST_URL, fetcher, NOW)
    assert result.status is UpdateStatus.UPDATED


def test_hash_failure_preserves_last_valid_file(tmp_path):
    paths = paths_for(tmp_path)
    paths.hotwords_file.parent.mkdir(parents=True)
    paths.hotwords_file.write_text("last-valid\n", encoding="utf-8")
    result = update_hotwords(paths, MANIFEST_URL, bad_hash_fetcher(), NOW)
    assert result.status is UpdateStatus.REJECTED
    assert paths.hotwords_file.read_text(encoding="utf-8") == "last-valid\n"
```

- [ ] **Step 2: Run updater tests and confirm failure**

Run: `python -m pytest tests/test_updater.py -v`
Expected: FAIL because updater types are absent.

- [ ] **Step 3: Implement manifest and transport validation**

Allow `https` only and hosts `github.com`, `objects.githubusercontent.com`, and `raw.githubusercontent.com`. Cap manifest at 256 KiB and data at 10 MiB. Require schema version `1`, nonblank version, absolute data URL, and lowercase 64-character SHA-256.

- [ ] **Step 4: Implement once-daily atomic update**

Skip when the last successful or attempted check is under 24 hours old unless `force=True`. Download into memory within the cap, verify hash, parse every JSONL line as `Scope.HOT`, write a temporary file, replace atomically, and record version/check time. Network or validation failures return a result without raising into the user's normalization flow.

- [ ] **Step 5: Run updater tests**

Run: `python -m pytest tests/test_updater.py -v`
Expected: PASS.

- [ ] **Step 6: Commit secure updates**

```bash
git add src/voice_intent_normalizer/updater.py tests/test_updater.py
git commit -m "feat: update public hotwords safely"
```

### Task 8: End-to-End Normalization Service

**Files:**
- Create: `src/voice_intent_normalizer/service.py`
- Create: `tests/test_service.py`

**Interfaces:**
- Consumes: paths, layered lexicons, matching, policy, scanner, learning, updater.
- Produces: `NormalizeRequest(text, project_root=None, domains=(), conversation_terms=(), notified_pairs=frozenset())`.
- Produces: `NormalizerService.normalize(request) -> CorrectionDecision`.
- Produces: `NormalizerService.apply_control(text, project_root=None) -> ControlResult | None`.

```python
@dataclass(frozen=True, slots=True)
class NormalizeRequest:
    text: str
    project_root: Path | None = None
    domains: tuple[str, ...] = ()
    conversation_terms: tuple[str, ...] = ()
    notified_pairs: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class ControlResult:
    handled: bool
    message: str
    event: LearningEvent | None = None
```

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_service_corrects_project_aware_term(service, project_root):
    service.learning.confirm(
        "星河公作台",
        "星河工作台",
        Scope.PROJECT,
        project_id=service.paths.for_project(project_root).project_id,
    )
    decision = service.normalize(NormalizeRequest(
        text="打开星河公作台的配置",
        project_root=project_root,
        domains=("software-development",),
    ))
    assert decision.action is DecisionAction.APPLY
    assert decision.corrected_text == "打开星河工作台的配置"


def test_service_continues_when_update_fails(service_with_failed_update):
    decision = service_with_failed_update.normalize(NormalizeRequest(
        text="使用 code X 检查项目",
        domains=("software-development",),
    ))
    assert decision.corrected_text == "使用 Codex 检查项目"
```

- [ ] **Step 2: Run service tests and confirm failure**

Run: `python -m pytest tests/test_service.py -v`
Expected: FAIL on missing service.

- [ ] **Step 3: Implement deterministic orchestration**

Order operations as: parse explicit control, resolve state, attempt nonblocking daily hotword update, refresh stale project cache, load layers, generate candidates, decide, then return. Normalization itself does not permanently learn an unconfirmed guess.

- [ ] **Step 4: Add read-only degradation**

When state is unwritable, load built-ins and readable existing state, skip update/scan persistence, and include a nonfatal diagnostic code `read_only_state`. Do not turn diagnostics into user-visible noise unless requested by `doctor`.

- [ ] **Step 5: Run service and regression tests**

Run: `python -m pytest tests/test_service.py tests/test_matching.py tests/test_policy.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the service**

```bash
git add src/voice_intent_normalizer/service.py tests/test_service.py
git commit -m "feat: orchestrate end-to-end intent correction"
```

### Task 9: CLI, Zero-Install Bootstrap, and Codex Hook Protocol

**Files:**
- Create: `src/voice_intent_normalizer/cli.py`
- Create: `src/voice_intent_normalizer/hook.py`
- Create: `scripts/voice_intent.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_hook.py`

**Interfaces:**
- Consumes: `NormalizerService`.
- Produces CLI commands: `normalize`, `learn`, `reject`, `undo`, `list`, `scan-project`, `update`, `doctor`, `install`, `uninstall`, `hook`.
- Produces: `handle_user_prompt_submit(payload, service) -> dict[str, object]`.

- [ ] **Step 1: Write failing CLI and hook tests**

```python
def test_normalize_json_output(cli_runner):
    result = cli_runner([
        "normalize", "--text", "使用 code X", "--domain", "software-development", "--json"
    ])
    payload = json.loads(result.stdout)
    assert payload["action"] == "apply"
    assert payload["corrected_text"] == "使用 Codex"


def test_user_prompt_hook_adds_context(fake_service):
    payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": "C:/work",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "给 open cloud 安装技能",
    }
    output = handle_user_prompt_submit(payload, fake_service)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "OpenClaw" in output["hookSpecificOutput"]["additionalContext"]
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_cli.py tests/test_hook.py -v`
Expected: FAIL on absent CLI/hook modules.

- [ ] **Step 3: Implement stable JSON output**

`normalize --json` emits UTF-8 JSON with keys `action`, `original_text`, `corrected_text`, `notices`, `question`, `diagnostics`, and `candidates`. Human mode prints only the corrected text, notice, or clarification question. Validation errors go to stderr and exit `2`; operational degradation exits `0` with diagnostics.

- [ ] **Step 4: Implement the Codex hook adapter**

Read one JSON object from stdin and require `hook_event_name == "UserPromptSubmit"` plus a string `prompt`. Return no additional context for `KEEP`. For `APPLY` or `ASK`, return:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Voice intent check: interpret the user's submitted text as \"给 OpenClaw 安装技能\". Do not claim the original message was edited."
  }
}
```

Never block a prompt and cap `additionalContext` at 1,000 characters.

- [ ] **Step 5: Implement the bootstrap script**

`scripts/voice_intent.py` resolves its parent repository, prepends `src` to `sys.path`, imports `voice_intent_normalizer.cli.main`, and exits with its integer result. It must run without installing the package.

- [ ] **Step 6: Run CLI and hook tests**

Run: `python -m pytest tests/test_cli.py tests/test_hook.py -v`
Expected: PASS.

- [ ] **Step 7: Commit host entrypoints**

```bash
git add src/voice_intent_normalizer/cli.py src/voice_intent_normalizer/hook.py scripts tests
git commit -m "feat: expose correction CLI and Codex hook"
```

### Task 10: Shared Agent Skill Package and Built-In Lexicons

**Files:**
- Create: `SKILL.md`
- Create: `agents/openai.yaml`
- Create: `references/lexicon-schema.md`
- Create: `references/correction-policy.md`
- Create: `references/domain-packs.md`
- Create: `assets/lexicons/base-zh.jsonl`
- Create: `assets/lexicons/hotwords-snapshot.jsonl`
- Create: `assets/lexicons/domains/ai.jsonl`
- Create: `assets/lexicons/domains/software-development.jsonl`
- Create: `assets/lexicons/domains/product-design.jsonl`
- Create: `tests/test_skill_package.py`

**Interfaces:**
- Consumes: bootstrap CLI and public data schema.
- Produces: a root Agent Skills package loadable by Codex, OpenClaw, and standards-compatible hosts.

- [ ] **Step 1: Invoke the required skill-authoring guidance**

Read and follow `skill-creator` and `superpowers:writing-skills` before editing `SKILL.md`. Record any validation command they require in the implementation notes.

- [ ] **Step 2: Write failing package tests**

```python
def test_skill_frontmatter_is_portable(repo_root):
    text = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: voice-intent-normalizer\n")
    assert "description:" in text.split("---", 2)[1]
    assert len(extract_description(text)) <= 160


def test_all_skill_references_exist(repo_root):
    for relative in extract_local_references(repo_root / "SKILL.md"):
        assert (repo_root / relative).is_file(), relative


def test_builtin_lexicons_validate(repo_root):
    for path in (repo_root / "assets" / "lexicons").rglob("*.jsonl"):
        assert load_jsonl(path)
```

- [ ] **Step 3: Run package tests and confirm failure**

Run: `python -m pytest tests/test_skill_package.py -v`
Expected: FAIL because `SKILL.md` and data assets do not exist.

- [ ] **Step 4: Write the portable SKILL.md**

The description must front-load Chinese voice transcription, homophones, professional terms, project context, and explicit correction triggers. The body instructs the Agent to run the bootstrap `normalize --json`, honor `apply/ask/keep`, display returned notices, never pretend to edit original text, and route explicit learning phrases through the CLI.

Required frontmatter:

```yaml
---
name: voice-intent-normalizer
description: Correct Chinese voice-transcription homophones, AI terms, and project names using local context and explicit user learning.
---
```

- [ ] **Step 5: Add curated seed data**

Include `Codex`, `OpenClaw`, and `WorkBuddy` with documented aliases. Mark `龙虾` and `小龙虾` as AI-domain ambiguous aliases so policy requires supporting context. Every public entry includes source notes in the contribution documentation; no private user data appears in assets.

- [ ] **Step 6: Validate the skill and tests**

Run the validator prescribed by `skill-creator`, then run: `python -m pytest tests/test_skill_package.py -v`
Expected: validator succeeds and tests PASS.

- [ ] **Step 7: Commit the shared skill**

```bash
git add SKILL.md agents references assets tests/test_skill_package.py
git commit -m "feat: add portable Chinese voice correction skill"
```

### Task 11: Adapter Protocol and Unified Installer

**Files:**
- Create: `src/voice_intent_normalizer/adapters/__init__.py`
- Create: `src/voice_intent_normalizer/adapters/base.py`
- Create: `src/voice_intent_normalizer/adapters/generic.py`
- Create: `src/voice_intent_normalizer/installer.py`
- Create: `adapters/generic/README.zh-CN.md`
- Create: `tests/test_installer.py`

**Interfaces:**
- Produces: `PlatformAdapter.detect()`, `install(options)`, `doctor()`, `uninstall(options)`.
- Produces: `CapabilityLevel = automatic | implicit | manual | unavailable`.
- Produces: `AdapterResult(platform, status, capability, messages, changed_paths)`.
- Produces: `Installer.install(platforms, options) -> tuple[AdapterResult, ...]`.

```python
@dataclass(frozen=True, slots=True)
class InstallOptions:
    strict: bool = False
    output_dir: Path | None = None
    workspace: Path | None = None
    auto_update: bool = True


@dataclass(frozen=True, slots=True)
class AdapterResult:
    platform: str
    status: str
    capability: CapabilityLevel
    messages: tuple[str, ...] = ()
    changed_paths: tuple[Path, ...] = ()
```

- [ ] **Step 1: Write failing multi-platform coordinator tests**

```python
def test_installer_reports_each_platform_independently(fake_adapters):
    results = Installer(fake_adapters).install(("codex", "openclaw", "workbuddy"), InstallOptions())
    assert [result.platform for result in results] == ["codex", "openclaw", "workbuddy"]
    assert results[0].status == "installed"
    assert results[2].capability is CapabilityLevel.MANUAL


def test_uninstall_one_platform_preserves_shared_state(tmp_path, generic_adapter):
    shared = tmp_path / ".voice-intent-normalizer" / "personal.jsonl"
    shared.parent.mkdir()
    shared.write_text('{"canonical":"OpenClaw"}\n', encoding="utf-8")
    generic_adapter.uninstall(UninstallOptions(remove_shared_data=False))
    assert shared.exists()
```

- [ ] **Step 2: Run installer tests and confirm failure**

Run: `python -m pytest tests/test_installer.py -v`
Expected: FAIL on missing adapter protocol.

- [ ] **Step 3: Implement adapter result contracts**

Every mutation returns exact changed paths and a user-facing status. `doctor` must distinguish installed files, skill discovery, automatic trigger, strict hook, shared-state access, and required manual action.

- [ ] **Step 4: Implement generic package installation**

Copy only the runtime package into a user-selected skill root, reject targets outside that root after path resolution, preserve unrelated files, and write an adapter status file under shared state. Generic capability defaults to `IMPLICIT` only when the host confirms description-based invocation; otherwise `MANUAL`.

- [ ] **Step 5: Wire CLI install, doctor, and uninstall**

Support repeated `--platform` values and `--all-detected`. Default prompts ask only for platforms and hotword auto-update. JSON mode never prompts and requires explicit flags.

- [ ] **Step 6: Run installer tests**

Run: `python -m pytest tests/test_installer.py -v`
Expected: PASS.

- [ ] **Step 7: Commit unified installation**

```bash
git add src/voice_intent_normalizer/adapters src/voice_intent_normalizer/installer.py adapters/generic tests/test_installer.py
git commit -m "feat: add unified platform installer"
```

### Task 12: Codex Adapter and Optional Strict Hook

**Files:**
- Create: `src/voice_intent_normalizer/adapters/codex.py`
- Create: `adapters/codex/AGENTS.snippet.md`
- Create: `adapters/codex/hooks.template.json`
- Create: `tests/test_adapters_codex.py`

**Interfaces:**
- Consumes: adapter protocol, root skill package, bootstrap hook command.
- Produces: Codex install under `$CODEX_HOME/skills/voice-intent-normalizer`.
- Produces: marked `AGENTS.md` block and optional merged `UserPromptSubmit` handler.

- [ ] **Step 1: Write failing Codex adapter tests**

```python
def test_codex_install_is_idempotent(tmp_path, codex_adapter):
    first = codex_adapter.install(InstallOptions(strict=False))
    second = codex_adapter.install(InstallOptions(strict=False))
    agents = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("VOICE-INTENT-NORMALIZER:BEGIN") == 1
    assert first.status == "installed"
    assert second.status == "already-installed"


def test_codex_strict_mode_merges_existing_hooks(tmp_path, codex_adapter):
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text('{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"keep-me"}]}]}}', encoding="utf-8")
    codex_adapter.install(InstallOptions(strict=True))
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert payload["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "keep-me"
    assert len(payload["hooks"]["UserPromptSubmit"]) == 1
```

- [ ] **Step 2: Run Codex tests and confirm failure**

Run: `python -m pytest tests/test_adapters_codex.py -v`
Expected: FAIL because the Codex adapter is absent.

- [ ] **Step 3: Implement standard Codex installation**

Resolve `CODEX_HOME` or default `~/.codex`, copy the shared skill atomically, and append a marker-bounded block to the existing global `AGENTS.md`. The block tells Codex to treat Chinese input as possible speech transcription, invoke the skill on anomaly signals, show important receipts, and ask for consequential ambiguity.

- [ ] **Step 4: Implement strict hook merge**

Merge this handler without removing existing events or groups:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"<absolute-skill-path>/scripts/voice_intent.py\" hook",
            "commandWindows": "python \"<absolute-skill-path>\\scripts\\voice_intent.py\" hook",
            "statusMessage": "Checking Chinese voice transcription",
            "additionalContextLimit": 1000,
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Identify the handler by a project-specific marker in its command path. `doctor` must tell users to review/trust the non-managed hook with `/hooks`; never use `--dangerously-bypass-hook-trust`.

- [ ] **Step 5: Implement exact uninstall**

Remove only the marker-bounded AGENTS block, this project's hook handler, and this project's Codex skill directory. Preserve the hooks file and event arrays when other handlers remain.

- [ ] **Step 6: Run Codex adapter tests**

Run: `python -m pytest tests/test_adapters_codex.py -v`
Expected: PASS.

- [ ] **Step 7: Commit Codex integration**

```bash
git add src/voice_intent_normalizer/adapters/codex.py adapters/codex tests/test_adapters_codex.py
git commit -m "feat: integrate Codex auto correction"
```

### Task 13: OpenClaw Adapter

**Files:**
- Create: `src/voice_intent_normalizer/adapters/openclaw.py`
- Create: `adapters/openclaw/README.zh-CN.md`
- Create: `tests/test_adapters_openclaw.py`

**Interfaces:**
- Consumes: adapter protocol and root skill package.
- Produces: global install command `openclaw skills install <source> --as voice-intent-normalizer --global`.
- Produces: verification through `openclaw skills check --json`.

- [ ] **Step 1: Write failing OpenClaw tests**

```python
def test_openclaw_global_install_uses_official_cli(fake_runner, openclaw_adapter):
    fake_runner.queue(returncode=0, stdout='{"ok":true}')
    fake_runner.queue(returncode=0, stdout='{"skills":[{"name":"voice-intent-normalizer","eligible":true}]}')
    result = openclaw_adapter.install(InstallOptions())
    assert fake_runner.calls[0][-2:] == ["voice-intent-normalizer", "--global"]
    assert result.status == "installed"
    assert result.capability is CapabilityLevel.IMPLICIT


def test_openclaw_missing_cli_reports_unavailable(fake_runner, openclaw_adapter):
    fake_runner.raise_file_not_found = True
    result = openclaw_adapter.doctor()
    assert result.capability is CapabilityLevel.UNAVAILABLE
    assert "OpenClaw CLI" in result.messages[0]
```

- [ ] **Step 2: Run OpenClaw tests and confirm failure**

Run: `python -m pytest tests/test_adapters_openclaw.py -v`
Expected: FAIL on missing adapter.

- [ ] **Step 3: Implement official CLI installation and verification**

Use argument arrays without a shell. Default to `--global`; allow an explicit workspace mode that omits it. Verify the installed skill is eligible. Do not use Codex's native skill directory as an OpenClaw root.

- [ ] **Step 4: Implement reinstall upgrade and removal**

Since local/Git OpenClaw installs are not tracked by `openclaw skills update`, upgrade by reinstalling the source only after backing up adapter status. Use the official uninstall command when exposed by the detected version; otherwise remove only the verified managed target and report the manual fallback.

- [ ] **Step 5: Document scope and session refresh**

Explain global versus workspace scope and that a new session or detected skill refresh may be required. Do not claim access to Codex or WorkBuddy session history.

- [ ] **Step 6: Run OpenClaw adapter tests**

Run: `python -m pytest tests/test_adapters_openclaw.py -v`
Expected: PASS.

- [ ] **Step 7: Commit OpenClaw integration**

```bash
git add src/voice_intent_normalizer/adapters/openclaw.py adapters/openclaw tests/test_adapters_openclaw.py
git commit -m "feat: integrate OpenClaw skills"
```

### Task 14: WorkBuddy Import Package Adapter

**Files:**
- Create: `src/voice_intent_normalizer/adapters/workbuddy.py`
- Create: `adapters/workbuddy/README.zh-CN.md`
- Create: `tests/test_adapters_workbuddy.py`

**Interfaces:**
- Consumes: root Agent Skill package.
- Produces: deterministic `dist/voice-intent-normalizer-workbuddy.zip`.
- Produces: `CapabilityLevel.MANUAL` until WorkBuddy publicly exposes a stable automated import API.

- [ ] **Step 1: Write failing WorkBuddy packaging tests**

```python
def test_workbuddy_archive_has_skill_at_package_root(tmp_path, workbuddy_adapter):
    result = workbuddy_adapter.install(InstallOptions(output_dir=tmp_path))
    archive = tmp_path / "voice-intent-normalizer-workbuddy.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "SKILL.md" in names
    assert "scripts/voice_intent.py" in names
    assert "src/voice_intent_normalizer/service.py" in names
    assert result.capability is CapabilityLevel.MANUAL


def test_workbuddy_archive_excludes_private_and_dev_files(tmp_path, workbuddy_adapter):
    workbuddy_adapter.install(InstallOptions(output_dir=tmp_path))
    with zipfile.ZipFile(tmp_path / "voice-intent-normalizer-workbuddy.zip") as bundle:
        names = set(bundle.namelist())
    assert ".git/config" not in names
    assert "tests/test_learning.py" not in names
    assert ".env" not in names
```

- [ ] **Step 2: Run WorkBuddy tests and confirm failure**

Run: `python -m pytest tests/test_adapters_workbuddy.py -v`
Expected: FAIL on missing adapter.

- [ ] **Step 3: Build deterministic safe archives**

Include runtime skill files only, sort archive names, set a fixed ZIP timestamp, use forward slashes, reject symlinks leaving the repository, and exclude VCS, tests, docs plans/specs, state, caches, secrets, and build outputs. Generate SHA-256 beside the archive.

- [ ] **Step 4: Report the manual import journey honestly**

Return status `package-created`, capability `MANUAL`, and instructions: WorkBuddy → Skills → Add Skill → Upload Skill → choose the ZIP → enable → run the provided test phrase. `doctor` accepts a user-supplied confirmation flag after the visible WorkBuddy test succeeds; it must not inspect undocumented client files.

- [ ] **Step 5: Document enable, disable, and uninstall**

Use WorkBuddy's public Skills UI terms. State that closing the skill prevents invocation and uninstalling it does not delete the shared personal lexicon unless the user separately chooses shared-data deletion.

- [ ] **Step 6: Run WorkBuddy adapter tests**

Run: `python -m pytest tests/test_adapters_workbuddy.py -v`
Expected: PASS.

- [ ] **Step 7: Commit WorkBuddy packaging**

```bash
git add src/voice_intent_normalizer/adapters/workbuddy.py adapters/workbuddy tests/test_adapters_workbuddy.py
git commit -m "feat: package skill for WorkBuddy import"
```

### Task 15: Corpus Evaluation, Documentation, and Release Verification

**Files:**
- Create: `tests/fixtures/corpus.jsonl`
- Create: `tests/test_corpus.py`
- Create: `references/platform-compatibility.md`
- Create: `README.md`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `CHANGELOG.md`
- Create: `LICENSE`
- Modify: all runtime and adapter files found by final verification

**Interfaces:**
- Consumes: complete core, skill, adapters, and approved design.
- Produces: measurable candidate recall, false-modification rate, release archives, and public installation documentation.

- [ ] **Step 1: Build the evaluation corpus before tuning**

Create UTF-8 JSONL cases with fields `id`, `input`, `domains`, `project_terms`, `expected_action`, `expected_text`, and `risk`. Include at least 100 known error cases and 100 correct-text controls. Cover Chinese homophones, English names, mixed text, project terms, explicit feedback, ambiguous ordinary meanings, paths, commands, numbers, dates, money, deletion, publishing, and deployment.

- [ ] **Step 2: Write failing acceptance-metric tests**

```python
def test_known_error_recall_is_at_least_95_percent(corpus_results):
    known = [item for item in corpus_results if item.case.expected_action == "apply"]
    recalled = [item for item in known if item.decision.corrected_text == item.case.expected_text]
    assert len(recalled) / len(known) >= 0.95


def test_correct_text_false_modification_is_below_2_percent(corpus_results):
    controls = [item for item in corpus_results if item.case.expected_action == "keep"]
    modified = [item for item in controls if item.decision.action is DecisionAction.APPLY]
    assert len(modified) / len(controls) < 0.02


def test_all_high_impact_ambiguities_ask(corpus_results):
    risky = [item for item in corpus_results if item.case.risk == "high-ambiguity"]
    assert all(item.decision.action is DecisionAction.ASK for item in risky)
```

- [ ] **Step 3: Run corpus tests and record the failing cases**

Run: `python -m pytest tests/test_corpus.py -v`
Expected before tuning: at least one metric FAIL with case IDs printed.

- [ ] **Step 4: Tune only documented weights and seed entries**

Adjust scoring constants in `matching.py`, thresholds in `policy.py`, or public aliases in curated assets. Every change must cite the failing case category in the commit message body. Do not add input-specific branches keyed to corpus sentences.

- [ ] **Step 5: Write public documentation**

README order: user problem, exact post-submission boundary, 60-second quick start, platform table, example correction receipt, natural-language learning controls, privacy, hotword updates, troubleshooting, development. The compatibility table distinguishes automatic, implicit, and manual activation. Security documentation covers project scan exclusions, update validation, hook trust, permissions, and vulnerability reporting.

- [ ] **Step 6: Run complete verification**

Run:

```bash
python -m pytest -v
python -m pytest --cov=voice_intent_normalizer --cov-report=term-missing --cov-fail-under=90
python -m ruff check src tests
python -m build
python scripts/voice_intent.py normalize --text "帮我适配 open cloud 的技能" --domain ai --json
```

Expected: all tests PASS, coverage is at least 90%, ruff exits `0`, source/wheel build succeeds, and smoke JSON contains `"corrected_text": "帮我适配 OpenClaw 的技能"`.

- [ ] **Step 7: Perform platform smoke checks**

In clean temporary homes:

```bash
python scripts/voice_intent.py install --platform codex --json
python scripts/voice_intent.py doctor --platform codex --json
python scripts/voice_intent.py install --platform openclaw --json
python scripts/voice_intent.py doctor --platform openclaw --json
python scripts/voice_intent.py install --platform workbuddy --output-dir dist --json
```

Expected: Codex and available OpenClaw installations report discovered capability; WorkBuddy reports `package-created` and `manual` with archive SHA-256. If OpenClaw is not installed in CI, its doctor result must be the tested `unavailable` state rather than a false success.

- [ ] **Step 8: Validate privacy and package contents**

Search release archives for `.env`, tokens, local absolute paths, test fixtures, personal JSONL, and project caches. Expected: none are present. Validate every bundled JSONL file and the root `SKILL.md` using the skill-authoring validator.

- [ ] **Step 9: Commit the release candidate**

```bash
git add tests references README.md CONTRIBUTING.md SECURITY.md CHANGELOG.md LICENSE src assets adapters SKILL.md agents scripts pyproject.toml
git commit -m "release: prepare voice intent normalizer v0.1.0"
```

## Plan Self-Review Checklist

- [x] Every approved design section maps to at least one task.
- [x] Core logic is independent of all platform adapters.
- [x] Personal/hot state is shared while project state remains isolated.
- [x] Codex hook JSON follows the documented `UserPromptSubmit` input/output shape and preserves existing hooks.
- [x] OpenClaw uses its official local-skill CLI and verifies eligibility.
- [x] WorkBuddy is described as manual import because no stable public automated import API is assumed.
- [x] Every mutation is idempotent or exactly removable.
- [x] High-impact ambiguity cannot be silently applied.
- [x] The original submitted message is never described as edited.
- [x] Hotword failure and missing optional pinyin cannot block the user's task.
- [x] The corpus enforces both `>=95%` known-error recall and `<2%` false modification.
- [x] All task interfaces use the same names and return types throughout the plan.

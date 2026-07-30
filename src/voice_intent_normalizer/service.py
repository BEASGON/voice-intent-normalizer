"""Deterministic, local orchestration for voice-intent normalization."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .learning import LearningEvent, LearningStore, parse_control
from .lexicon import LexiconSet, load_jsonl
from .matching import MatchContext, generate_candidates
from .models import CorrectionDecision, DecisionAction, Scope
from .paths import StatePaths, validate_state_root
from .policy import decide
from .project_scan import project_cache_is_stale, scan_project

_STATE_UNAVAILABLE = "state_unavailable"
_READ_ONLY_STATE = "read_only_state"
_EXPLICIT_CONTROL = "explicit_control"
_MAX_TEXT_CHARS = 20_000
_MAX_TEXT_BYTES = 64 * 1024
_MAX_DOMAINS = 32
_MAX_CONVERSATION_TERMS = 128
_MAX_TERM_CHARS = 256
_MAX_TERM_BYTES = 1_024
_MAX_NOTIFIED_PAIRS = 1_024


@dataclass(frozen=True, slots=True)
class NormalizeRequest:
    """One immutable normalization request with local contextual evidence."""

    text: str
    project_root: Path | None = None
    domains: tuple[str, ...] = ()
    conversation_terms: tuple[str, ...] = ()
    notified_pairs: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if (
            len(self.text) > _MAX_TEXT_CHARS
            or len(self.text.encode("utf-8")) > _MAX_TEXT_BYTES
        ):
            raise ValueError("text exceeds normalization bounds")
        if self.project_root is not None:
            object.__setattr__(self, "project_root", Path(self.project_root))
        object.__setattr__(
            self, "domains", _text_tuple(self.domains, "domains", _MAX_DOMAINS)
        )
        object.__setattr__(
            self,
            "conversation_terms",
            _text_tuple(
                self.conversation_terms,
                "conversation_terms",
                _MAX_CONVERSATION_TERMS,
            ),
        )
        pairs = frozenset(self.notified_pairs)
        if len(pairs) > _MAX_NOTIFIED_PAIRS:
            raise ValueError("notified_pairs exceeds normalization bounds")
        if any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            for pair in pairs
        ):
            raise ValueError("notified_pairs must contain string pairs")
        if any(
            len(value) > _MAX_TERM_CHARS
            or len(value.encode("utf-8")) > _MAX_TERM_BYTES
            for pair in pairs
            for value in pair
        ):
            raise ValueError("notified_pairs contains an oversized term")
        object.__setattr__(self, "notified_pairs", pairs)


@dataclass(frozen=True, slots=True)
class ControlResult:
    """The result of one explicit learning-control sentence."""

    handled: bool
    message: str
    event: LearningEvent | None = None


class NormalizerService:
    """Compose local state, matching, and conservative decision policy.

    ``hotword_updater`` is deliberately an injected callback: hosts decide how
    to provide the allowlisted updater transport, while the core never sends
    transcript, project, conversation, or learning data to it.
    """

    def __init__(
        self,
        paths: StatePaths,
        builtins_root: str | Path,
        *,
        hotword_updater: Callable[[StatePaths], Any] | None = None,
        project_scanner: Callable[[str | Path, StatePaths], Any] = scan_project,
        learning: LearningStore | None = None,
    ) -> None:
        self.paths = paths
        self.builtins_root = Path(builtins_root)
        self.hotword_updater = hotword_updater
        self.project_scanner = project_scanner
        self.learning = (
            LearningStore.for_root(paths.root) if learning is None else learning
        )

    def normalize(self, request: NormalizeRequest) -> CorrectionDecision:
        """Return a deterministic correction without silently learning it."""
        if not isinstance(request, NormalizeRequest):
            raise TypeError("request must be a NormalizeRequest")

        if parse_control(request.text) is not None:
            return CorrectionDecision(
                action=DecisionAction.KEEP,
                original_text=request.text,
                corrected_text=request.text,
                diagnostics=(_EXPLICIT_CONTROL,),
            )

        diagnostics: list[str] = []
        state_writable = self._state_is_writable()
        if not state_writable:
            diagnostics.append(_READ_ONLY_STATE)
        else:
            self._attempt_hotword_update(diagnostics)
            self._refresh_missing_project_cache(request.project_root, diagnostics)

        lexicons = self._load_layers(request, diagnostics)
        project_terms = tuple(
            entry.canonical
            for entry in lexicons.entries
            if entry.scope is Scope.PROJECT
        )
        context = MatchContext(
            domains=frozenset(request.domains),
            project_terms=frozenset(project_terms),
            conversation_terms=frozenset(request.conversation_terms),
        )
        decision = decide(
            request.text,
            generate_candidates(request.text, lexicons, context),
            context,
            notified_pairs=request.notified_pairs,
        )
        return replace(
            decision,
            diagnostics=_unique((*decision.diagnostics, *diagnostics)),
        )

    def apply_control(
        self, text: str, project_root: Path | None = None
    ) -> ControlResult | None:
        """Apply only a positively parsed V1 learning-control sentence."""
        command = parse_control(text)
        if command is None:
            return None
        try:
            if command.kind == "list_recent":
                return ControlResult(
                    True,
                    f"最近有 {len(self.learning.list_recent())} 条明确更正。",
                )
            if not self._state_is_writable():
                return ControlResult(True, "状态目录为只读，未保存明确更正。")
            if command.kind == "confirm":
                scope = Scope.PROJECT if project_root is not None else Scope.PERSONAL
                project_id = (
                    self.paths.for_project(project_root).project_id
                    if project_root is not None
                    else None
                )
                event = self.learning.confirm(
                    _required(command.alias),
                    _required(command.canonical),
                    scope,
                    project_id=project_id,
                )
                return ControlResult(True, "已记录明确更正。", event)
            if command.kind == "reject":
                event = self.learning.reject(
                    _required(command.alias), _required(command.canonical)
                )
                return ControlResult(True, "已记录不采用该更正。", event)
            if command.kind == "undo":
                event = self.learning.undo_last()
                return ControlResult(
                    True,
                    "已撤销最近的更正。" if event else "没有可撤销的更正。",
                    event,
                )
            if command.kind == "delete":
                return ControlResult(True, "请说明要删除的词。")
        except (OSError, PermissionError, RuntimeError, ValueError):
            return ControlResult(True, "未能保存明确更正；现有状态保持不变。")
        return ControlResult(False, "不支持的明确更正命令。")

    def _attempt_hotword_update(self, diagnostics: list[str]) -> None:
        if self.hotword_updater is None:
            return
        try:
            self.hotword_updater(self.paths)
        except Exception:
            diagnostics.append("hotword_update_failed")

    def _refresh_missing_project_cache(
        self, project_root: Path | None, diagnostics: list[str]
    ) -> None:
        """Refresh only scanner-owned cache, leaving V1 learning authoritative."""
        if project_root is None:
            return
        try:
            if project_cache_is_stale(project_root, self.paths):
                self.project_scanner(project_root, self.paths)
        except (OSError, PermissionError, RuntimeError, ValueError):
            diagnostics.append("project_scan_failed")

    def _load_layers(
        self, request: NormalizeRequest, diagnostics: list[str]
    ) -> LexiconSet:
        try:
            lexicons, layer_diagnostics = LexiconSet.load_with_diagnostics(
                self.paths,
                self.builtins_root,
                project_root=request.project_root,
                domains=request.domains,
            )
            diagnostics.extend(layer_diagnostics)
            return lexicons
        except (OSError, PermissionError, RuntimeError, ValueError):
            diagnostics.append(_STATE_UNAVAILABLE)
            return self._builtins_only(request.domains)

    def _builtins_only(self, domains: Sequence[str]) -> LexiconSet:
        """Load trusted packaged layers without opening mutable local state."""
        entries = []
        seen_domains: set[str] = set()
        for domain in domains:
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            path = _first_file(
                self.builtins_root / "domains" / f"{domain}.jsonl",
                self.builtins_root / "industry" / f"{domain}.jsonl",
            )
            if path is not None:
                entries.extend(load_jsonl(path, expected_scope=Scope.INDUSTRY))
        hotword_path = _first_file(
            self.builtins_root / "hotwords-snapshot.jsonl",
            self.builtins_root / "hot.jsonl",
        )
        if hotword_path is not None:
            entries.extend(load_jsonl(hotword_path, expected_scope=Scope.HOT))
        base_path = _first_file(
            self.builtins_root / "base-zh.jsonl",
            self.builtins_root / "base.jsonl",
        )
        if base_path is not None:
            entries.extend(load_jsonl(base_path, expected_scope=Scope.BASE))
        return LexiconSet(tuple(entries))

    def _state_is_writable(self) -> bool:
        """Conservatively check local persistence without creating probe files."""
        try:
            validate_state_root(self.paths.root)
            target = self.paths.root
            while not target.exists() and target != target.parent:
                target = target.parent
            info = target.stat()
            return bool(stat.S_IMODE(info.st_mode) & 0o222) and os.access(
                target, os.W_OK
            )
        except OSError:
            return False


def _first_file(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("control mapping is incomplete")
    return value


def _text_tuple(
    values: Iterable[str], name: str, maximum: int
) -> tuple[str, ...]:
    values = tuple(values)
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{name} must contain only strings")
    if len(values) > maximum:
        raise ValueError(f"{name} exceeds normalization bounds")
    if any(
        len(value) > _MAX_TERM_CHARS
        or len(value.encode("utf-8")) > _MAX_TERM_BYTES
        for value in values
    ):
        raise ValueError(f"{name} contains an oversized term")
    return values


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))

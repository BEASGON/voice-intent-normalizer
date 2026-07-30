"""Stable local command-line surface for voice-intent normalization."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from .models import Candidate, CorrectionDecision, Scope
from .paths import StatePaths, guard_project_root
from .project_scan import scan_project
from .service import NormalizeRequest, NormalizerService


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the documented command surface without performing any I/O."""
    parser = _Parser(prog="voice-intent")
    commands = parser.add_subparsers(dest="command", required=True)
    normalize = commands.add_parser("normalize")
    normalize.add_argument("--text", required=True)
    normalize.add_argument("--domain", action="append", default=[])
    normalize.add_argument("--conversation-term", action="append", default=[])
    normalize.add_argument("--project-root")
    normalize.add_argument("--json", action="store_true")

    learn = commands.add_parser("learn")
    learn.add_argument("--alias", required=True)
    learn.add_argument("--canonical", required=True)
    learn.add_argument("--scope", choices=("personal", "project"), default="personal")
    learn.add_argument("--project-root")
    learn.add_argument("--json", action="store_true")

    reject = commands.add_parser("reject")
    reject.add_argument("--alias", required=True)
    reject.add_argument("--canonical", required=True)
    reject.add_argument("--json", action="store_true")

    undo = commands.add_parser("undo")
    undo.add_argument("--json", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")
    scan = commands.add_parser("scan-project")
    scan.add_argument("--project-root", required=True)
    scan.add_argument("--json", action="store_true")
    update = commands.add_parser("update")
    update.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    for name in ("install", "uninstall"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
    commands.add_parser("hook")
    return parser


def default_service() -> NormalizerService:
    """Create the local service using packaged lexicons when they are present."""
    repository = Path(__file__).resolve().parents[2]
    return NormalizerService(StatePaths.resolve(), repository / "assets" / "lexicons")


def main(
    argv: Sequence[str] | None = None,
    *,
    service: NormalizerService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Run one command with validation errors separated from degraded results."""
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as exc:
        errors.write(f"voice-intent: {exc}\n")
        return 2
    if args.command == "hook":
        from .hook import main as hook_main

        return hook_main(stdin=stdin, stdout=output, stderr=errors, service=service)
    try:
        active_service = default_service() if service is None else service
        return _dispatch(args, active_service, output)
    except (OSError, PermissionError, RuntimeError) as exc:
        return _degraded(args, output, str(exc) or "local operation unavailable")
    except (TypeError, ValueError) as exc:
        errors.write(f"voice-intent: {exc}\n")
        return 2


def _dispatch(
    args: argparse.Namespace, service: NormalizerService, output: TextIO
) -> int:
    if args.command == "normalize":
        decision = service.normalize(
            NormalizeRequest(
                text=args.text,
                project_root=Path(args.project_root) if args.project_root else None,
                domains=tuple(args.domain),
                conversation_terms=tuple(args.conversation_term),
            )
        )
        _write_decision(output, decision, as_json=args.json)
        return 0
    if args.command == "learn":
        scope = Scope(args.scope)
        project_id = None
        if scope is Scope.PROJECT:
            if not args.project_root:
                raise ValueError("--project-root is required for project learning")
            with guard_project_root(args.project_root) as authority:
                project_id = service.paths.for_project(authority).project_id
        event = service.learning.confirm(args.alias, args.canonical, scope, project_id)
        return _write_result(output, args, {"status": "ok", "event": _event(event)})
    if args.command == "reject":
        event = service.learning.reject(args.alias, args.canonical)
        return _write_result(output, args, {"status": "ok", "event": _event(event)})
    if args.command == "undo":
        event = service.learning.undo_last()
        return _write_result(
            output,
            args,
            {"status": "ok", "event": None if event is None else _event(event)},
        )
    if args.command == "list":
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        return _write_result(
            output,
            args,
            {
                "status": "ok",
                "events": [
                    _event(event) for event in service.learning.list_recent(args.limit)
                ],
            },
        )
    if args.command == "scan-project":
        with guard_project_root(args.project_root) as authority:
            result = scan_project(authority, service.paths)
        return _write_result(
            output,
            args,
            {
                "status": "ok",
                "entries": len(result.entries),
                "truncated": result.truncated,
                "files_scanned": result.files_scanned,
            },
        )
    if args.command == "doctor":
        writable = _state_writable(service.paths.root)
        return _write_result(
            output,
            args,
            {
                "status": "ok" if writable else "degraded",
                "state_root": str(service.paths.root),
                "diagnostics": [] if writable else ["state_unavailable"],
            },
        )
    if args.command in {"update", "install", "uninstall"}:
        return _degraded(
            args, output, f"{args.command} is not configured in this build"
        )
    raise ValueError("unknown command")


def _write_decision(
    output: TextIO, decision: CorrectionDecision, *, as_json: bool
) -> None:
    if as_json:
        _write_json(output, _decision_payload(decision))
        return
    value = decision.question or decision.corrected_text
    output.write(f"{value}\n")


def _decision_payload(decision: CorrectionDecision) -> dict[str, object]:
    return {
        "action": decision.action.value,
        "original_text": decision.original_text,
        "corrected_text": decision.corrected_text,
        "notices": list(decision.notices),
        "question": decision.question,
        "diagnostics": list(decision.diagnostics),
        "candidates": [_candidate(candidate) for candidate in decision.candidates],
    }


def _candidate(candidate: Candidate) -> dict[str, object]:
    return {
        "canonical": candidate.canonical,
        "original": candidate.original,
        "replacement_span": list(candidate.replacement_span),
        "score": candidate.score,
        "evidence": list(candidate.evidence),
        "scope": candidate.entry.scope.value,
    }


def _event(event: Any) -> dict[str, object]:
    raw = asdict(event)
    raw["status"] = event.status.value
    raw["scope"] = None if event.scope is None else event.scope.value
    return raw


def _write_result(
    output: TextIO, args: argparse.Namespace, payload: dict[str, object]
) -> int:
    if args.json:
        _write_json(output, payload)
    else:
        output.write(f"{payload['status']}\n")
    return 0


def _degraded(args: argparse.Namespace, output: TextIO, diagnostic: str) -> int:
    payload = {"status": "degraded", "diagnostics": [diagnostic]}
    if getattr(args, "json", False):
        _write_json(output, payload)
    else:
        output.write(f"{diagnostic}\n")
    return 0


def _write_json(output: TextIO, payload: dict[str, object]) -> None:
    output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    output.write("\n")


def _state_writable(root: Path) -> bool:
    target = root
    while not target.exists() and target != target.parent:
        target = target.parent
    return target.is_dir() and os.access(target, os.W_OK)

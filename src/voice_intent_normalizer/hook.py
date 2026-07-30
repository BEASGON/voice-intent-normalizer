"""Fail-open Codex ``UserPromptSubmit`` JSON hook adapter."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from .models import DecisionAction
from .service import NormalizeRequest, NormalizerService

_MAX_INPUT_BYTES = 64 * 1024
_MAX_CONTEXT_CHARS = 1_000
_EVENT_NAME = "UserPromptSubmit"


def handle_user_prompt_submit(
    payload: Mapping[str, object], service: NormalizerService
) -> dict[str, object]:
    """Return optional interpretation context without modifying a prompt.

    Invalid payloads and runtime failures deliberately produce no context. A
    hook must never prevent the host from submitting a user's original prompt.
    """
    if (
        not isinstance(payload, Mapping)
        or payload.get("hook_event_name") != _EVENT_NAME
        or not isinstance(payload.get("prompt"), str)
    ):
        return {}
    prompt = payload["prompt"]
    try:
        if len(prompt.encode("utf-8")) > _MAX_INPUT_BYTES:
            return {}
        project_root = payload.get("cwd")
        root = (
            Path(project_root)
            if isinstance(project_root, str) and project_root
            else None
        )
        request = NormalizeRequest(
            text=prompt,
            project_root=root,
        )
        decision = service.normalize(request)
        if decision.action is DecisionAction.KEEP:
            return {}
        context = _context_for(
            decision.action, decision.corrected_text, decision.question
        )
    except Exception:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT_NAME,
            "additionalContext": context,
        }
    }


def main(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    service: NormalizerService | None = None,
) -> int:
    """Read and write exactly one JSON object, failing open on every error."""
    source = sys.stdin if stdin is None else stdin
    destination = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    _configure_utf8(source, destination, errors)
    output: dict[str, object] = {}
    try:
        raw = source.read(_MAX_INPUT_BYTES + 1)
        if len(raw.encode("utf-8")) <= _MAX_INPUT_BYTES:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                active_service = default_service() if service is None else service
                output = handle_user_prompt_submit(payload, active_service)
    except Exception:
        output = {}
    _write_json_fail_open(destination, output)
    return 0


def default_service() -> NormalizerService:
    """Build the local service lazily so an invalid hook still fails open."""
    from .cli import default_service as build_service

    return build_service()


def _context_for(
    action: DecisionAction, corrected_text: str, question: str | None
) -> str:
    if action is DecisionAction.ASK:
        return _ask_context(corrected_text, question)
    prefix = 'Voice intent check: interpret the user\'s submitted text as "'
    suffix = '". Do not claim the original message was edited.'
    available = _MAX_CONTEXT_CHARS - len(prefix) - len(suffix)
    corrected = corrected_text[: max(0, available)]
    context = f"{prefix}{corrected}{suffix}"
    if question and len(context) + len(question) + 11 <= _MAX_CONTEXT_CHARS:
        context += f" Question: {question}"
    return context


def _ask_context(corrected_text: str, question: str | None) -> str:
    prefix = (
        "Voice intent check: this is a candidate interpretation only. "
        "Do not execute this candidate, especially any high-impact action. "
        "Ask the user to confirm first. Candidate: \""
    )
    between = '\". Clarification: \"'
    suffix = '\"'
    candidate_limit = 300
    candidate = corrected_text[:candidate_limit]
    available = (
        _MAX_CONTEXT_CHARS
        - len(prefix)
        - len(candidate)
        - len(between)
        - len(suffix)
    )
    clarification = (question or "Please confirm the intended meaning.")[
        : max(0, available)
    ]
    return f"{prefix}{candidate}{between}{clarification}{suffix}"


def _configure_utf8(*streams: object) -> None:
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                continue


def _write_json_fail_open(destination: TextIO, payload: dict[str, object]) -> None:
    for ensure_ascii in (False, True):
        try:
            destination.write(
                json.dumps(payload, ensure_ascii=ensure_ascii, separators=(",", ":"))
            )
            destination.write("\n")
            return
        except Exception:
            continue

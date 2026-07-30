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
    if len(prompt.encode("utf-8")) > _MAX_INPUT_BYTES:
        return {}
    project_root = payload.get("cwd")
    root = (
        Path(project_root)
        if isinstance(project_root, str) and project_root
        else None
    )
    try:
        request = NormalizeRequest(
            text=prompt,
            project_root=root,
        )
        decision = service.normalize(request)
    except Exception:
        return {}
    if decision.action is DecisionAction.KEEP:
        return {}
    context = _context_for(decision.corrected_text, decision.question)
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
    del stderr  # Never echo potentially sensitive hook input to stderr.
    source = sys.stdin if stdin is None else stdin
    destination = sys.stdout if stdout is None else stdout
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
    destination.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    destination.write("\n")
    return 0


def default_service() -> NormalizerService:
    """Build the local service lazily so an invalid hook still fails open."""
    from .cli import default_service as build_service

    return build_service()


def _context_for(corrected_text: str, question: str | None) -> str:
    prefix = 'Voice intent check: interpret the user\'s submitted text as "'
    suffix = '". Do not claim the original message was edited.'
    available = _MAX_CONTEXT_CHARS - len(prefix) - len(suffix)
    corrected = corrected_text[: max(0, available)]
    context = f"{prefix}{corrected}{suffix}"
    if question and len(context) + len(question) + 11 <= _MAX_CONTEXT_CHARS:
        context += f" Question: {question}"
    return context

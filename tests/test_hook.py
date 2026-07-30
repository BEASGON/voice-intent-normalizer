"""Codex UserPromptSubmit adapter behavior."""

from __future__ import annotations

import json
from io import StringIO

from voice_intent_normalizer.models import CorrectionDecision, DecisionAction


class FakeService:
    def __init__(self, decision: CorrectionDecision) -> None:
        self.decision = decision
        self.requests = []

    def normalize(self, request):
        self.requests.append(request)
        return self.decision


def test_user_prompt_hook_adds_context_for_corrected_interpretation():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    service = FakeService(
        CorrectionDecision(
            action=DecisionAction.APPLY,
            original_text="给 open cloud 安装技能",
            corrected_text="给 OpenClaw 安装技能",
        )
    )
    output = handle_user_prompt_submit(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "C:/work",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "给 open cloud 安装技能",
        },
        service,
    )

    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "OpenClaw" in context
    assert "Do not claim the original message was edited." in context
    assert len(context) <= 1_000
    assert service.requests[0].text == "给 open cloud 安装技能"


def test_keep_does_not_add_context():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    output = handle_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "不要改这句话"},
        FakeService(
            CorrectionDecision(
                action=DecisionAction.KEEP,
                original_text="不要改这句话",
                corrected_text="不要改这句话",
            )
        ),
    )

    assert output == {}


def test_hook_fails_open_when_prompt_exceeds_service_bounds():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    output = handle_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "字" * 20_001},
        FakeService(
            CorrectionDecision(
                action=DecisionAction.APPLY,
                original_text="",
                corrected_text="",
            )
        ),
    )

    assert output == {}


def test_hook_main_fails_open_for_invalid_payload_without_echoing_prompt():
    from voice_intent_normalizer.hook import main

    stdout = StringIO()
    stderr = StringIO()
    code = main(
        stdin=StringIO('{"hook_event_name":"Other","prompt":"secret token"}'),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert json.loads(stdout.getvalue()) == {}
    assert "secret token" not in stderr.getvalue()


def test_hook_main_reads_and_writes_exactly_one_json_object(monkeypatch):
    from voice_intent_normalizer import hook

    service = FakeService(
        CorrectionDecision(
            action=DecisionAction.ASK,
            original_text="open cloud",
            corrected_text="OpenClaw",
            question="你是指 OpenClaw 吗？",
        )
    )
    monkeypatch.setattr(hook, "default_service", lambda: service)
    stdout = StringIO()

    code = hook.main(
        stdin=StringIO(
            json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "open cloud"}
            )
        ),
        stdout=stdout,
        stderr=StringIO(),
    )

    assert code == 0
    output = json.loads(stdout.getvalue())
    assert list(output) == ["hookSpecificOutput"]
    assert "OpenClaw" in output["hookSpecificOutput"]["additionalContext"]

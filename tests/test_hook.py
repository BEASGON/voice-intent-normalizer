"""Codex UserPromptSubmit adapter behavior."""

from __future__ import annotations

import json
from io import BytesIO, StringIO, TextIOWrapper

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


def test_hook_fails_open_when_prompt_cannot_be_utf8_encoded():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    output = handle_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "\ud800"},
        FakeService(
            CorrectionDecision(
                action=DecisionAction.APPLY,
                original_text="",
                corrected_text="",
            )
        ),
    )

    assert output == {}


def test_hook_fails_open_when_service_returns_an_invalid_decision():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    class InvalidService:
        def normalize(self, request):
            return object()

    assert handle_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "open cloud"},
        InvalidService(),
    ) == {}


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


def test_ask_context_requires_confirmation_without_directing_execution():
    from voice_intent_normalizer.hook import handle_user_prompt_submit

    output = handle_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "删除生产数据库"},
        FakeService(
            CorrectionDecision(
                action=DecisionAction.ASK,
                original_text="删除生产数据库",
                corrected_text="删除 production database",
                question="请确认你是否真的要执行这个高风险操作？" + "很长" * 1_000,
            )
        ),
    )

    context = output["hookSpecificOutput"]["additionalContext"]
    assert "interpret the user's submitted text as" not in context
    assert "Do not execute this candidate" in context
    assert "confirm" in context.casefold()
    assert "请确认" in context
    assert len(context) <= 1_000


class AsciiOnlyStream:
    def __init__(self) -> None:
        self.value = ""

    def write(self, value: str) -> int:
        value.encode("ascii")
        self.value += value
        return len(value)


class BrokenStream:
    def write(self, value: str) -> int:
        raise UnicodeEncodeError("ascii", value, 0, 1, "injected")


def test_hook_reconfigures_utf8_streams_and_falls_back_to_ascii_json(monkeypatch):
    from voice_intent_normalizer import hook

    service = FakeService(
        CorrectionDecision(
            action=DecisionAction.APPLY,
            original_text="使用 code X",
            corrected_text="使用 Codex",
        )
    )
    monkeypatch.setattr(hook, "default_service", lambda: service)
    bytes_stream = BytesIO()
    utf8_stream = TextIOWrapper(bytes_stream, encoding="cp1252")

    assert hook.main(
        stdin=StringIO('{"hook_event_name":"UserPromptSubmit","prompt":"使用 code X"}'),
        stdout=utf8_stream,
        stderr=StringIO(),
    ) == 0
    utf8_stream.flush()
    assert json.loads(bytes_stream.getvalue().decode("utf-8"))

    narrow_stream = AsciiOnlyStream()
    assert hook.main(
        stdin=StringIO('{"hook_event_name":"UserPromptSubmit","prompt":"使用 code X"}'),
        stdout=narrow_stream,
        stderr=StringIO(),
        service=service,
    ) == 0
    assert "\\u" in narrow_stream.value
    assert json.loads(narrow_stream.value)


def test_hook_output_write_failure_still_returns_zero():
    from voice_intent_normalizer.hook import main

    assert main(
        stdin=StringIO('{"hook_event_name":"Other","prompt":"secret token"}'),
        stdout=BrokenStream(),
        stderr=StringIO(),
    ) == 0

"""Codex host installation contracts."""

from __future__ import annotations

import json
from pathlib import Path

from voice_intent_normalizer.adapters.base import InstallOptions, UninstallOptions
from voice_intent_normalizer.paths import StatePaths


def _adapter(tmp_path: Path, monkeypatch):
    from voice_intent_normalizer.adapters.codex import CodexAdapter

    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return CodexAdapter(
        Path(__file__).parents[1],
        StatePaths(root=tmp_path / ".voice-intent-normalizer"),
    ), codex_home


def test_codex_install_is_idempotent_and_adds_only_its_agents_block(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    agents = codex_home / "AGENTS.md"
    agents.parent.mkdir()
    agents.write_text("# User guidance\n", encoding="utf-8")

    first = adapter.install(InstallOptions())
    second = adapter.install(InstallOptions())

    content = agents.read_text(encoding="utf-8")
    assert first.status == "installed"
    assert second.status == "already-installed"
    assert content.startswith("# User guidance\n")
    assert content.count("VOICE-INTENT-NORMALIZER:BEGIN") == 1
    assert (codex_home / "skills" / "voice-intent-normalizer" / "SKILL.md").is_file()


def test_codex_strict_mode_merges_existing_events_and_groups(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": "keep-me"}]}
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": "keep-this-too"}
                            ]
                        }
                    ],
                },
                "other": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )

    result = adapter.install(InstallOptions(strict=True))
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert result.status == "installed"
    assert payload["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "keep-me"
    assert (
        payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        == "keep-this-too"
    )
    managed = [
        hook
        for group in payload["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if "voice-intent-normalizer" in hook.get("command", "")
    ]
    assert len(managed) == 1
    assert managed[0]["additionalContextLimit"] == 1000
    assert payload["other"] == {"preserve": True}


def test_codex_rejects_duplicate_hook_keys_without_mutating_them(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    original = '{"hooks":{},"hooks":{}}'
    hooks_path.write_text(original, encoding="utf-8")

    result = adapter.install(InstallOptions(strict=True))

    assert result.status == "failed"
    assert hooks_path.read_text(encoding="utf-8") == original
    assert not (codex_home / "AGENTS.md").exists()
    assert not (codex_home / "skills" / "voice-intent-normalizer").exists()


def test_codex_strict_hook_write_failure_rolls_back_new_managed_files(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    original = '{"hooks":{"SessionEnd":[]}}'
    hooks_path.write_text(original, encoding="utf-8")
    from voice_intent_normalizer.adapters.codex import CodexAdapter

    native_write = CodexAdapter._write_json_atomic

    def fail_hooks(path: Path, value: object) -> None:
        if path == hooks_path:
            raise OSError("injected hook write failure")
        native_write(path, value)

    monkeypatch.setattr(CodexAdapter, "_write_json_atomic", fail_hooks)

    result = adapter.install(InstallOptions(strict=True))

    assert result.status == "failed"
    assert hooks_path.read_text(encoding="utf-8") == original
    assert not (codex_home / "AGENTS.md").exists()
    assert not (codex_home / "skills" / "voice-intent-normalizer").exists()


def test_codex_uninstall_removes_only_managed_configuration(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "keep-me"}]}
                    ]
                }
            },
        ),
        encoding="utf-8",
    )
    adapter.install(InstallOptions(strict=True))

    result = adapter.uninstall(UninstallOptions())
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert result.status == "uninstalled"
    agents = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "VOICE-INTENT-NORMALIZER:BEGIN" not in agents
    assert payload["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": "keep-me"}]}
    ]
    assert not (codex_home / "skills" / "voice-intent-normalizer").exists()


def test_codex_uninstall_preserves_a_similarly_named_hook_path(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    similar = (
        'python3 "/opt/voice-intent-normalizer-backup/scripts/'
        'voice_intent.py" hook'
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": similar}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    adapter.install(InstallOptions(strict=True))

    adapter.uninstall(UninstallOptions())

    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert payload["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": similar}]}
    ]


def test_codex_uninstall_preserves_same_name_hook_at_another_absolute_path(
    tmp_path: Path, monkeypatch
):
    adapter, codex_home = _adapter(tmp_path, monkeypatch)
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    same_name = (
        'python3 "/opt/skills/voice-intent-normalizer/scripts/'
        'voice_intent.py" hook'
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": same_name}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    adapter.install(InstallOptions(strict=True))

    adapter.uninstall(UninstallOptions())

    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert payload["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": same_name}]}
    ]


def test_codex_doctor_requires_manual_hook_trust(tmp_path: Path, monkeypatch):
    adapter, _ = _adapter(tmp_path, monkeypatch)
    adapter.install(InstallOptions(strict=True))

    result = adapter.doctor()

    assert result.capability.value == "automatic"
    assert any("/hooks" in message for message in result.messages)
    assert all("dangerously-bypass" not in message for message in result.messages)

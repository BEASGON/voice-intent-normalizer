"""Codex installation adapter with an opt-in prompt-submit hook."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..paths import StatePaths
from .base import AdapterResult, CapabilityLevel, InstallOptions, UninstallOptions
from .generic_layout import capsule_source_files

_NAME = "voice-intent-normalizer"
_BEGIN = "<!-- VOICE-INTENT-NORMALIZER:BEGIN -->"
_END = "<!-- VOICE-INTENT-NORMALIZER:END -->"
_HOOK_EVENT = "UserPromptSubmit"
_WINDOWS_REPARSE_POINT = 0x400
_MAX_HOOK_BYTES = 1024 * 1024


class CodexAdapter:
    """Install the stable skill capsule and narrow, marked Codex guidance."""

    platform = "codex"

    def __init__(self, repository: str | Path, state_paths: StatePaths) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self.state_paths = state_paths

    def detect(self) -> AdapterResult:
        root = self._codex_home()
        return AdapterResult(
            self.platform,
            "detected" if root.exists() else "not-installed",
            CapabilityLevel.IMPLICIT,
        )

    def install(self, options: InstallOptions) -> AdapterResult:
        try:
            home = self._codex_home()
            home.mkdir(parents=True, exist_ok=True)
            self._require_direct_directory(home, "Codex home")
            skill = home / "skills" / _NAME
            hooks_path = home / "hooks.json"
            if options.strict:
                self._read_hooks(hooks_path)
            agents = home / "AGENTS.md"
            rollback = (
                agents.read_bytes() if agents.exists() else None,
                skill.exists(),
                skill.parent.exists(),
            )
            skill.parent.mkdir(exist_ok=True)
            self._require_direct_directory(skill.parent, "Codex skills directory")
            changed: list[Path] = []
            if not self._ensure_skill(skill):
                changed.append(skill)
            if self._ensure_agents_block(agents):
                changed.append(agents)
            if options.strict:
                if self._ensure_hook(hooks_path, skill):
                    changed.append(hooks_path)
            return AdapterResult(
                self.platform,
                "installed" if changed else "already-installed",
                (
                    CapabilityLevel.AUTOMATIC
                    if options.strict
                    else CapabilityLevel.IMPLICIT
                ),
                self._install_messages(options.strict),
                tuple(changed),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._rollback_install(locals().get("rollback"))
            return self._failed("Codex installation was not completed")

    def doctor(self) -> AdapterResult:
        try:
            home = self._codex_home()
            skill = home / "skills" / _NAME
            agents = home / "AGENTS.md"
            if not skill.exists() and not agents.exists():
                return AdapterResult(
                    self.platform, "not-installed", CapabilityLevel.UNAVAILABLE
                )
            installed = skill.is_dir() and self._has_agents_block(agents)
            strict = self._has_hook(home / "hooks.json")
            if not installed:
                return AdapterResult(
                    self.platform,
                    "degraded",
                    CapabilityLevel.UNAVAILABLE,
                    ("Codex installation is incomplete; manual action is required",),
                )
            messages = ["skill discovery and marked AGENTS guidance: installed"]
            if strict:
                messages.append(
                    "strict hook installed; review and trust it with /hooks"
                )
            else:
                messages.append("strict hook: not installed (optional)")
            return AdapterResult(
                self.platform,
                "installed",
                CapabilityLevel.AUTOMATIC if strict else CapabilityLevel.IMPLICIT,
                tuple(messages),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AdapterResult(
                self.platform,
                "degraded",
                CapabilityLevel.UNAVAILABLE,
                ("Codex installation could not be verified",),
            )

    def uninstall(self, options: UninstallOptions) -> AdapterResult:
        if options.remove_shared_data:
            return self._failed("shared data was not removed")
        try:
            home = self._codex_home()
            changed: list[Path] = []
            agents = home / "AGENTS.md"
            if self._remove_agents_block(agents):
                changed.append(agents)
            hooks = home / "hooks.json"
            if self._remove_hook(hooks, home / "skills" / _NAME):
                changed.append(hooks)
            skill = home / "skills" / _NAME
            if skill.exists():
                self._remove_direct_tree(skill)
                changed.append(skill)
            return AdapterResult(
                self.platform,
                "uninstalled" if changed else "not-installed",
                CapabilityLevel.UNAVAILABLE,
                ("shared personal and project data were preserved",),
                tuple(changed),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._failed(
                "Codex uninstall was not completed; manual action is required"
            )

    def _codex_home(self) -> Path:
        configured = os.environ.get("CODEX_HOME", "").strip()
        return (Path(configured) if configured else Path.home() / ".codex").expanduser()

    def _ensure_skill(self, destination: Path) -> bool:
        sources = capsule_source_files(self.repository)
        if destination.exists():
            if not destination.is_dir():
                raise ValueError("Codex skill destination is not a directory")
            if self._tree_bytes(destination) != sources:
                raise ValueError("existing Codex skill is not this managed capsule")
            return True
        staging = Path(tempfile.mkdtemp(prefix=f".{_NAME}-", dir=destination.parent))
        try:
            for relative, data in sources.items():
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            if self._tree_bytes(staging) != sources:
                raise ValueError("prepared Codex skill does not match capsule")
            os.replace(staging, destination)
        except Exception:
            if staging.exists():
                self._remove_direct_tree(staging)
            raise
        return False

    def _ensure_agents_block(self, path: Path) -> bool:
        source = self._read_optional_text(path)
        count = source.count(_BEGIN) + source.count(_END)
        if count == 0:
            separator = "" if not source or source.endswith("\n") else "\n"
            self._write_text_atomic(path, f"{source}{separator}{self._agents_block()}")
            return True
        if source.count(_BEGIN) != 1 or source.count(_END) != 1:
            raise ValueError("Codex AGENTS markers are malformed")
        start = source.index(_BEGIN)
        end = source.index(_END, start) + len(_END)
        if end <= start:
            raise ValueError("Codex AGENTS markers are malformed")
        return False

    def _remove_agents_block(self, path: Path) -> bool:
        source = self._read_optional_text(path)
        if not source:
            return False
        if source.count(_BEGIN) == source.count(_END) == 0:
            return False
        if source.count(_BEGIN) != 1 or source.count(_END) != 1:
            raise ValueError("Codex AGENTS markers are malformed")
        start = source.index(_BEGIN)
        end = source.index(_END, start) + len(_END)
        if end < len(source) and source[end] == "\n":
            end += 1
        self._write_text_atomic(path, source[:start] + source[end:])
        return True

    def _has_agents_block(self, path: Path) -> bool:
        source = self._read_optional_text(path)
        return source.count(_BEGIN) == source.count(_END) == 1

    def _ensure_hook(self, path: Path, skill: Path) -> bool:
        payload = self._read_hooks(path)
        hooks = self._hooks_mapping(payload)
        groups = hooks.setdefault(_HOOK_EVENT, [])
        if not isinstance(groups, list):
            raise ValueError("Codex hook event is invalid")
        if any(self._is_managed_group(group, skill) for group in groups):
            return False
        groups.append({"hooks": [self._hook_handler(skill)]})
        self._write_json_atomic(path, payload)
        return True

    def _remove_hook(self, path: Path, skill: Path) -> bool:
        if not path.exists():
            return False
        payload = self._read_hooks(path)
        hooks = self._hooks_mapping(payload)
        groups = hooks.get(_HOOK_EVENT)
        if groups is None:
            return False
        if not isinstance(groups, list):
            raise ValueError("Codex hook event is invalid")
        changed = False
        retained_groups: list[object] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Codex hook group is invalid")
            retained = [
                hook
                for hook in group["hooks"]
                if not self._is_managed_hook(hook, skill)
            ]
            changed |= len(retained) != len(group["hooks"])
            if retained:
                retained_groups.append({**group, "hooks": retained})
        if not changed:
            return False
        if retained_groups:
            hooks[_HOOK_EVENT] = retained_groups
        else:
            del hooks[_HOOK_EVENT]
        self._write_json_atomic(path, payload)
        return True

    def _has_hook(self, path: Path) -> bool:
        if not path.exists():
            return False
        payload = self._read_hooks(path)
        groups = self._hooks_mapping(payload).get(_HOOK_EVENT, [])
        return isinstance(groups, list) and any(
            self._is_managed_group(group, self._codex_home() / "skills" / _NAME)
            for group in groups
        )

    @staticmethod
    def _hooks_mapping(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("Codex hooks document is invalid")
        hooks = payload.get("hooks")
        if hooks is None:
            hooks = {}
            payload["hooks"] = hooks
        if not isinstance(hooks, dict):
            raise ValueError("Codex hooks document is invalid")
        return hooks

    @staticmethod
    def _is_managed_hook(hook: object, skill: Path) -> bool:
        if not isinstance(hook, dict):
            return False
        expected = CodexAdapter._hook_handler(skill)
        return (
            hook.get("command") == expected["command"]
            and hook.get("commandWindows") == expected["commandWindows"]
        )

    def _is_managed_group(self, group: object, skill: Path) -> bool:
        return (
            isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(
                self._is_managed_hook(hook, skill) for hook in group["hooks"]
            )
        )

    def _rollback_install(self, rollback: object) -> None:
        if (
            not isinstance(rollback, tuple)
            or len(rollback) != 3
            or not isinstance(rollback[0], (bytes, type(None)))
            or not isinstance(rollback[1], bool)
            or not isinstance(rollback[2], bool)
        ):
            return
        home = self._codex_home()
        agents = home / "AGENTS.md"
        skill = home / "skills" / _NAME
        agents_before, skill_existed, skills_existed = rollback
        try:
            if agents_before is None:
                if agents.exists():
                    agents.unlink()
            elif agents.exists() and agents.read_bytes() != agents_before:
                self._write_bytes_atomic(agents, agents_before)
            if not skill_existed and skill.exists():
                self._remove_direct_tree(skill)
            if not skills_existed and skill.parent.exists() and not any(
                skill.parent.iterdir()
            ):
                skill.parent.rmdir()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    @staticmethod
    def _hook_handler(skill: Path) -> dict[str, object]:
        unix_path = (skill / "scripts" / "voice_intent.py").as_posix()
        windows_path = str(skill / "scripts" / "voice_intent.py").replace("/", "\\")
        return {
            "type": "command",
            "command": f'python3 "{unix_path}" hook',
            "commandWindows": f'python "{windows_path}" hook',
            "statusMessage": "Checking Chinese voice transcription",
            "additionalContextLimit": 1000,
            "timeout": 5,
        }

    @staticmethod
    def _agents_block() -> str:
        return "\n".join((
            _BEGIN,
            "Treat Chinese input as possible speech transcription.",
            "Invoke $voice-intent-normalizer when homophone, near-sound, "
            "transliteration, terminology, or semantic-anomaly signals appear.",
            "Show an important correction receipt and ask the user before a "
            "consequential ambiguity changes the task.",
            _END,
            "",
        ))

    def _read_hooks(self, path: Path) -> dict[str, object]:
        source = self._read_optional_text(path, limit=_MAX_HOOK_BYTES)
        if not source:
            return {}
        value = json.loads(source, object_pairs_hook=self._reject_duplicate_keys)
        if not isinstance(value, dict):
            raise ValueError("Codex hooks document is invalid")
        return value

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Codex hooks document has duplicate keys")
            result[key] = value
        return result

    @staticmethod
    def _read_optional_text(path: Path, *, limit: int = _MAX_HOOK_BYTES) -> str:
        if not path.exists():
            return ""
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or CodexAdapter._is_alias(info)
            or info.st_size > limit
        ):
            raise ValueError("Codex configuration is not a direct bounded file")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _write_text_atomic(path: Path, value: str) -> None:
        CodexAdapter._write_bytes_atomic(path, value.encode("utf-8"))

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        CodexAdapter._write_bytes_atomic(path, data)

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        CodexAdapter._require_direct_directory(root, "Codex skill")
        result: dict[str, bytes] = {}
        pending = [root]
        while pending:
            directory = pending.pop()
            CodexAdapter._require_direct_directory(directory, "Codex skill")
            for entry in os.scandir(directory):
                path = Path(entry.path)
                info = path.lstat()
                if CodexAdapter._is_alias(info):
                    raise ValueError("Codex skill contains an alias")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    result[path.relative_to(root).as_posix()] = path.read_bytes()
                else:
                    raise ValueError("Codex skill contains an unsafe entry")
        return result

    @staticmethod
    def _remove_direct_tree(root: Path) -> None:
        CodexAdapter._tree_bytes(root)
        shutil.rmtree(root)

    @staticmethod
    def _require_direct_directory(path: Path, label: str) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or CodexAdapter._is_alias(info):
            raise ValueError(f"{label} is not a direct directory")

    @staticmethod
    def _is_alias(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(
            attributes & _WINDOWS_REPARSE_POINT
        )

    def _install_messages(self, strict: bool) -> tuple[str, ...]:
        messages = ["Codex skill and marked AGENTS guidance installed"]
        if strict:
            messages.append("review and trust the strict hook with /hooks")
        return tuple(messages)

    def _failed(self, message: str) -> AdapterResult:
        return AdapterResult(
            self.platform, "failed", CapabilityLevel.UNAVAILABLE, (message,)
        )

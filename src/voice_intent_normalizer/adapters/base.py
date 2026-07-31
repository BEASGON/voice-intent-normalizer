"""Small, truthful contracts shared by every host adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class CapabilityLevel(str, Enum):
    """How a host can invoke the skill, without promising live dictation edits."""

    AUTOMATIC = "automatic"
    IMPLICIT = "implicit"
    MANUAL = "manual"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class InstallOptions:
    strict: bool = False
    output_dir: Path | None = None
    workspace: Path | None = None
    auto_update: bool = True
    implicit_invocation_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class UninstallOptions:
    remove_shared_data: bool = False
    output_dir: Path | None = None
    workspace: Path | None = None
    strict: bool = False


@dataclass(frozen=True, slots=True)
class AdapterResult:
    platform: str
    status: str
    capability: CapabilityLevel
    messages: tuple[str, ...] = ()
    changed_paths: tuple[Path, ...] = ()


class PlatformAdapter(Protocol):
    """A host integration whose filesystem effects are explicitly reported."""

    platform: str

    def detect(self) -> AdapterResult: ...

    def install(self, options: InstallOptions) -> AdapterResult: ...

    def doctor(self) -> AdapterResult: ...

    def uninstall(self, options: UninstallOptions) -> AdapterResult: ...

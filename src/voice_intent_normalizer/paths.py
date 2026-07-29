"""Read-only shared-state path resolution for voice intent normalization."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """All shared-state paths belonging to one normalized project root."""

    project_root: Path
    project_id: str
    root: Path

    @property
    def lexicon_file(self) -> Path:
        """Return the project lexicon location without creating it."""
        return self.root / "project.jsonl"

    @property
    def scan_state_file(self) -> Path:
        """Return the project scanner state location without creating it."""
        return self.root / "scan-state.json"


@dataclass(frozen=True, slots=True)
class StatePaths:
    """Locations for shared user state, resolved without filesystem mutation."""

    root: Path

    @classmethod
    def resolve(
        cls,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
    ) -> StatePaths:
        """Resolve state root from ``VOICE_INTENT_HOME`` or the user home directory."""
        environment = os.environ if environ is None else environ
        configured = environment.get("VOICE_INTENT_HOME", "")
        if configured and configured.strip():
            root = Path(configured).expanduser()
        else:
            base_home = Path.home() if home is None else Path(home)
            root = base_home.expanduser() / ".voice-intent-normalizer"
        return cls(root=root.resolve())

    @property
    def personal_file(self) -> Path:
        """Return the personal lexicon location without creating it."""
        return self.root / "personal.jsonl"

    @property
    def preferences_file(self) -> Path:
        """Return the preferences location without creating it."""
        return self.root / "preferences.json"

    @property
    def hotwords_file(self) -> Path:
        """Return the downloaded public-hotword lexicon without creating it."""
        return self.root / "hotwords" / "zh-ai.jsonl"

    def adapter_status_file(self, adapter: str) -> Path:
        """Return one adapter's status document location without creating it."""
        return self.root / "adapters" / f"{adapter}.json"

    def for_project(self, project_root: str | Path) -> ProjectPaths:
        """Resolve deterministic project paths without creating them."""
        normalized_root = Path(project_root).expanduser().resolve()
        project_id = hashlib.sha256(
            str(normalized_root).encode("utf-8")
        ).hexdigest()[:16]
        return ProjectPaths(
            project_root=normalized_root,
            project_id=project_id,
            root=self.root / "projects" / project_id,
        )

"""Private state layout for the versioned generic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voice_intent_normalizer.paths import StatePaths


@dataclass(frozen=True, slots=True)
class GenericLayoutPaths:
    """Resolved generic-adapter paths without filesystem mutation."""

    adapter_root: Path
    status: Path
    transaction: Path
    generations: Path
    staging: Path
    retired: Path


def generic_layout_paths(state_paths: StatePaths) -> GenericLayoutPaths:
    """Return the immutable V1 generic-adapter layout under shared state."""
    root = state_paths.generic_adapter_root()
    return GenericLayoutPaths(
        adapter_root=root,
        status=root / "status.json",
        transaction=root / "transaction.json",
        generations=root / "generations",
        staging=root / "staging",
        retired=root / "retired",
    )

"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    _prioritize_source(repository / "src")
    from voice_intent_normalizer.cli import main as cli_main

    return cli_main()


def _prioritize_source(source: Path) -> None:
    trusted = _canonical_path(source)
    sys.path[:] = [
        entry for entry in sys.path if _canonical_path(entry) != trusted
    ]
    sys.path.insert(0, str(source.resolve()))


def _canonical_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


if __name__ == "__main__":
    raise SystemExit(main())

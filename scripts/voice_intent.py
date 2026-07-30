"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    source = str(repository / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from voice_intent_normalizer.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

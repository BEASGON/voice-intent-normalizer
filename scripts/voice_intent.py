"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    source = repository / "src"
    _prioritize_source(source)
    _clear_preloaded_package()
    from voice_intent_normalizer import cli

    if not _is_below(cli.__file__, source):
        raise RuntimeError("trusted repository CLI could not be imported")

    return cli.main()


def _prioritize_source(source: Path) -> None:
    trusted = _canonical_path(source)
    sys.path[:] = [
        entry for entry in sys.path if _canonical_path(entry) != trusted
    ]
    sys.path.insert(0, str(source.resolve()))


def _canonical_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


def _clear_preloaded_package() -> None:
    for name in tuple(sys.modules):
        if name == "voice_intent_normalizer" or name.startswith(
            "voice_intent_normalizer."
        ):
            del sys.modules[name]


def _is_below(value: str | None, root: Path) -> bool:
    if value is None:
        return False
    candidate = _canonical_path(value)
    trusted = _canonical_path(root)
    return candidate == trusted or candidate.startswith(trusted + "\\")


if __name__ == "__main__":
    raise SystemExit(main())

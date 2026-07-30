"""Run the repository checkout without installing the package first."""

from __future__ import annotations

import ntpath
import posixpath
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal


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
    resolved = str(Path(value).expanduser().resolve())
    return resolved.casefold() if sys.platform == "win32" else resolved


def _clear_preloaded_package() -> None:
    for name in tuple(sys.modules):
        if name == "voice_intent_normalizer" or name.startswith(
            "voice_intent_normalizer."
        ):
            del sys.modules[name]


def _is_below(
    value: str | None,
    root: str | Path,
    *,
    flavor: Literal["posix", "windows"] | None = None,
) -> bool:
    if value is None:
        return False
    selected = flavor or ("windows" if sys.platform == "win32" else "posix")
    if selected == "windows":
        candidate = _windows_path(value)
        trusted = _windows_path(root)
    else:
        candidate = PurePosixPath(posixpath.normpath(str(value)))
        trusted = PurePosixPath(posixpath.normpath(str(root)))
    try:
        candidate.relative_to(trusted)
    except ValueError:
        return False
    return candidate.is_absolute() and trusted.is_absolute()


def _windows_path(value: str | Path) -> PureWindowsPath:
    raw = str(value).replace("/", "\\")
    if raw.casefold().startswith("\\\\?\\"):
        raw = raw[4:]
    normalized = ntpath.normpath(raw).casefold()
    return PureWindowsPath(normalized)


if __name__ == "__main__":
    raise SystemExit(main())

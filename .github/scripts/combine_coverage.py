"""Combine Coverage.py databases with portable source path keys."""

from __future__ import annotations

import sys
from pathlib import Path

from coverage import CoverageData

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PREFIX = "src/voice_intent_normalizer/"


def _portable_source(path: str) -> str:
    return path.replace("\\", "/")


def _canonical_source(path: str) -> str:
    portable = _portable_source(path)
    if portable.startswith(SOURCE_PREFIX):
        return portable
    marker = f"/{SOURCE_PREFIX}"
    marker_index = portable.rfind(marker)
    if marker_index < 0:
        raise RuntimeError(f"unexpected coverage source: {path}")
    candidate = portable[marker_index + 1 :]
    if not (REPOSITORY_ROOT / candidate).is_file():
        raise RuntimeError(f"unexpected coverage source: {path}")
    return candidate


def combine_coverage(source: Path, output: Path) -> None:
    merged: dict[str, set[int]] = {}
    inputs = sorted(
        path
        for path in source.rglob(".coverage.*")
        if path.resolve() != output.resolve()
    )
    if not inputs:
        raise RuntimeError("no coverage databases were downloaded")
    for path in inputs:
        data = CoverageData(basename=str(path))
        data.read()
        for measured in data.measured_files():
            canonical = _canonical_source(measured)
            merged.setdefault(canonical, set()).update(data.lines(measured) or ())
    if not merged:
        raise RuntimeError("coverage databases contain no measured source")
    combined = CoverageData(basename=str(output))
    combined.add_lines(merged)
    combined.write()


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: combine_coverage.py INPUT_DIR OUTPUT_FILE")
    combine_coverage(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

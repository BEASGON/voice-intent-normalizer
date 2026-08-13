"""Combine Coverage.py databases with portable source path keys."""

from __future__ import annotations

import sys
from pathlib import Path

from coverage import CoverageData


def _portable_source(path: str) -> str:
    return path.replace("\\", "/")


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
            portable = _portable_source(measured)
            if not portable.startswith("src/voice_intent_normalizer/"):
                raise RuntimeError(f"unexpected coverage source: {measured}")
            merged.setdefault(portable, set()).update(data.lines(measured) or ())
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

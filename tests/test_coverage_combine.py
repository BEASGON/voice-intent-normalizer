from __future__ import annotations

import runpy
from pathlib import Path

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[1]


def test_cross_platform_combiner_unifies_windows_and_posix_source_names(tmp_path):
    windows = CoverageData(basename=str(tmp_path / ".coverage.windows"))
    windows.add_lines({r"src\voice_intent_normalizer\sample.py": {1, 2}})
    windows.write()
    posix = CoverageData(basename=str(tmp_path / ".coverage.ubuntu"))
    posix.add_lines({"src/voice_intent_normalizer/sample.py": {2, 3}})
    posix.write()

    namespace = runpy.run_path(
        str(ROOT / ".github" / "scripts" / "combine_coverage.py")
    )
    output = tmp_path / ".coverage"
    namespace["combine_coverage"](tmp_path, output)

    combined = CoverageData(basename=str(output))
    combined.read()
    assert combined.measured_files() == {
        "src/voice_intent_normalizer/sample.py"
    }
    assert combined.lines("src/voice_intent_normalizer/sample.py") == [1, 2, 3]

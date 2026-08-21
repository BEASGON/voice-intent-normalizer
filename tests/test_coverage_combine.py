from __future__ import annotations

import runpy
from pathlib import Path

import pytest
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


def test_combiner_maps_installed_generation_source_to_repository_module(tmp_path):
    runtime_copy = CoverageData(basename=str(tmp_path / ".coverage.windows"))
    runtime_copy.add_lines(
        {
            r"D:\a\_temp\vin-coverage\state\adapters\generic\generations"
            r"\g-test\src\voice_intent_normalizer\adapters"
            r"\generic_contract.py": {7, 8}
        }
    )
    runtime_copy.write()

    namespace = runpy.run_path(
        str(ROOT / ".github" / "scripts" / "combine_coverage.py")
    )
    output = tmp_path / ".coverage"
    namespace["combine_coverage"](tmp_path, output)

    combined = CoverageData(basename=str(output))
    combined.read()
    canonical = (
        "src/voice_intent_normalizer/adapters/generic_contract.py"
    )
    assert combined.measured_files() == {canonical}
    assert set(combined.lines(canonical) or ()) == {7, 8}


def test_combiner_rejects_runtime_source_without_repository_module(tmp_path):
    runtime_copy = CoverageData(basename=str(tmp_path / ".coverage.windows"))
    runtime_copy.add_lines(
        {
            r"D:\a\_temp\state\src\voice_intent_normalizer"
            r"\foreign.py": {1}
        }
    )
    runtime_copy.write()

    namespace = runpy.run_path(
        str(ROOT / ".github" / "scripts" / "combine_coverage.py")
    )
    with pytest.raises(RuntimeError, match="unexpected coverage source"):
        namespace["combine_coverage"](tmp_path, tmp_path / ".coverage")

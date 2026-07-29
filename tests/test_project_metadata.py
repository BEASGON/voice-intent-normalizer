from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_project_declares_the_apache_license_file():
    """Catch package metadata that omits the repository's reuse license."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["license"] == "Apache-2.0"
    assert metadata["project"]["license-files"] == ["LICENSE"]


def test_development_dependencies_provide_a_python_3_10_toml_reader():
    """Catch a test environment that cannot import a TOML reader on Python 3.10."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "tomli>=2.0; python_version < '3.11'" in metadata["project"][
        "optional-dependencies"
    ]["dev"]


def test_license_file_contains_the_complete_apache_2_terms():
    """Catch a missing or truncated Apache-2.0 license artifact."""
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert license_text.startswith("Apache License\n")
    assert "Version 2.0, January 2004" in license_text
    assert "http://www.apache.org/licenses/" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert license_text.rstrip().endswith("limitations under the License.")

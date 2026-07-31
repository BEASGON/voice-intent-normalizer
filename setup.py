"""Build the exact runtime skill allowlist into the Python package."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_BUNDLE_FILES = (
    "SKILL.md",
    "LICENSE",
    "pyproject.toml",
    "agents/openai.yaml",
    "assets/lexicons/base-zh.jsonl",
    "assets/lexicons/hotwords-snapshot.jsonl",
    "assets/lexicons/domains/ai.jsonl",
    "assets/lexicons/domains/product-design.jsonl",
    "assets/lexicons/domains/software-development.jsonl",
    "references/correction-policy.md",
    "references/domain-packs.md",
    "references/lexicon-schema.md",
    "scripts/voice_intent.py",
)


class _BuildPyWithSkillBundle(_build_py):
    def run(self) -> None:
        super().run()
        repository = Path(__file__).resolve().parent
        package_root = (
            Path(self.build_lib) / "voice_intent_normalizer"
        ).resolve()
        bundle = package_root / "_skill_bundle"
        if bundle.resolve().parent != package_root:
            raise RuntimeError("refusing to clear an unsafe generated bundle path")
        if bundle.exists():
            shutil.rmtree(bundle)
        for relative in _BUNDLE_FILES:
            source = repository / relative
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        python_root = repository / "src" / "voice_intent_normalizer"
        for source in python_root.rglob("*.py"):
            relative = source.relative_to(repository)
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


setup(cmdclass={"build_py": _BuildPyWithSkillBundle})

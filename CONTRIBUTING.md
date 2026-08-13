# Contributing

## Development setup

```powershell
python -m pip install -e .[dev]
python -m pytest -v
python -m ruff check src tests
```

Keep runtime bundles exact: new runtime sources, public lexicon files, and
references must be added to every inventory enforced by packaging and generic
layout tests.

## Safety and quality

Write a failing test before behavior changes. Do not add sentence-specific
corrections: adjust only documented scoring, policy, or curated public seed
data. Preserve fail-open behavior and require explicit learning. Never place
personal state, fixtures, secrets, or local paths in a distributable archive.

## Documentation

Update user-facing docs when a platform capability, privacy boundary, command,
or release allowlist changes. Public platform claims must describe verified
capability, not inferred client internals.

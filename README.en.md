# Voice Intent Normalizer

[简体中文](README.md) · English

## The problem

Voice transcription can confuse Chinese homophones and mixed-language product,
project, and AI names. Voice Intent Normalizer gives an Agent a local,
auditable interpretation of already submitted text, while preserving the
user's intended task and safety boundaries.

## Post-submission boundary

This skill runs after a message has been submitted. It does **not** edit a live
dictation field, rewrite the original message, read conversation history, or
silently create learning records. A correction receipt describes how the Agent
will interpret the submitted text; it never claims the text itself changed.

## 60-second quick start

1. From this repository, run a local normalization:

   ```powershell
   python scripts/voice_intent.py normalize --text "帮我适配 open cloud 的技能" --domain ai --json
   ```

2. Install for a supported host, choosing exactly one platform:

   ```powershell
   python scripts/voice_intent.py install --platform codex --json
   ```

3. Run `doctor` for that platform and follow any returned instructions.

## Platform compatibility

| Platform | Activation | What installation does |
| --- | --- | --- |
| Codex | Automatic | Installs the documented prompt-submit hook. |
| OpenClaw | Implicit | Uses OpenClaw's public local-skill CLI after eligibility verification. |
| WorkBuddy | Manual | Creates a ZIP for upload through **Skills → Add Skill → Upload Skill**. |
| Generic host | Manual | Creates a local, host-readable skill capsule when supported. |

See [platform compatibility](references/platform-compatibility.md) for
verification, enable/disable, and uninstall guidance.

## Correction receipt

For an `apply` decision, a host may present a receipt such as:

```text
I will interpret “open cloud” as “OpenClaw” for this request.
```

For an `ask` decision, answer the question before the host performs the
consequential task. A `keep` decision uses the submitted text unchanged.

## Natural-language learning

Learning is explicit and local. Tell the host a mapping, then choose a
`personal` scope for a cross-project preference or `project` scope for the
active direct project:

```powershell
python scripts/voice_intent.py learn --alias "open cloud" --canonical "OpenClaw" --scope personal --json
python scripts/voice_intent.py reject --alias "open cloud" --canonical "OpenClaw" --json
python scripts/voice_intent.py undo --json
```

The tool never infers learning from ordinary conversation. If the correct scope
is unclear, it asks rather than writing a mapping.

## Privacy

Personal and project lexicons remain local state. The project scan reads only
the active direct project and honors scan exclusions. Public hotword updates do
not upload transcripts, conversation history, personal mappings, project
lexicons, or access tokens. Release archives contain only the audited runtime
allowlist.

## Hotword updates

Built-in hotwords are available offline. Version 0.1.0 does not enable network
hotword updates; update snapshots arrive only in reviewed project releases.

## Troubleshooting

- If normalization is unavailable or response JSON is invalid, keep the
  original submitted text.
- If a high-impact correction is ambiguous, answer the returned question
  instead of guessing.
- Run `python scripts/voice_intent.py doctor --platform <name> --json` after
  installation.
- For WorkBuddy, upload the generated archive through its visible Skills UI;
  no undocumented client files are inspected.

## Development

Install development dependencies, run the tests, lint, and build:

```powershell
python -m pip install -e .[dev]
python -m pytest -v
python -m ruff check src tests
python -m build
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes and
[SECURITY.md](SECURITY.md) for security reporting.

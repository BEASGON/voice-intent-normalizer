---
name: voice-intent-normalizer
description: Correct Chinese voice-transcription homophones, AI terms, and project names using local context and explicit user learning.
---

# Voice Intent Normalizer

Interpret submitted Chinese voice text more accurately; do not claim to edit a
live dictation UI or the user's original message. Use this skill for suspicious
homophones, mixed Chinese/English product names, project terms, or an explicit
correction/learning request.

## Workflow

1. From the skill repository root, run the local bootstrap command. Use a host
   available Python executable; if it is unavailable, leave the submitted text
   unchanged and say local correction is unavailable.

   ```powershell
   python scripts/voice_intent.py normalize --text "<submitted text>" --domain "<relevant domain>" --json
   ```

   Add `--conversation-term "<term>"` for a term stated in this conversation.
   Add `--project-root "<direct local project path>"` only for the active project.
   Parse one JSON response. Fail open to the original text only when the command
   fails, JSON is invalid, no valid `apply`, `ask`, or `keep` action is present,
   or it returns `status=degraded` without a decision. When a valid decision has
   non-fatal diagnostics (for example `read_only_state` or `personal_invalid`),
   honor its action and notices, then report the relevant diagnostic briefly.

2. If `action` is `apply`, use `corrected_text` as this turn's interpretation,
   then show every returned notice. Phrase it as “我将按 … 理解”, never as a
   claim that the original text changed.

3. If `action` is `ask`, show `question` and wait for the user's answer. Do
   not perform the proposed task or choose a candidate first.

4. If `action` is `keep`, work from the original submitted text. Do not state
   that a correction occurred.

5. Only learn after an explicit instruction such as “以后把 A 理解为 B” or
   “不要把 A 改成 B”. For a cross-project personal preference, use `personal`.
   For a name or repository term specific to the active project, use `project`
   with its direct local root. If the user did not specify and the meaning does
   not determine personal or the active project, ask which scope they want;
   do not write a mapping. Never infer learning from normal conversation, read
   all history, or modify live voice input.

   ```powershell
   python scripts/voice_intent.py learn --alias "A" --canonical "B" --scope personal --json
   python scripts/voice_intent.py learn --alias "A" --canonical "B" --scope project --project-root "<direct active project>" --json
   python scripts/voice_intent.py reject --alias "A" --canonical "B" --json
   python scripts/voice_intent.py undo --json
   python scripts/voice_intent.py list --json
   ```

   `reject` is a personal “do not correct A as B” rule. `undo` reverses the
   latest effective explicit learning action; `list` shows recent local actions.

## Quick reference

| Situation | Action |
| --- | --- |
| `code X` in an AI request | Normalize with `--domain ai`; honor the decision. |
| “龙虾” / “小龙虾” | Treat as ambiguous; require the returned policy decision. |
| Explicit correction | Choose personal or project scope; ask if it is unclear. |
| Invalid decision JSON or degraded-without-decision | Fail open to the original text. |

## Common mistakes

- Do not silently learn preferences from an ordinary task.
- Do not use a project path through a symlink, junction, network share, mapped
  alias, or non-direct path.
- Do not treat a likely product name as permission to execute destructive or
  publishing actions; honor an `ask` result first.

Read [the JSONL contract](references/lexicon-schema.md) when editing public
lexicons, [the safety policy](references/correction-policy.md) when explaining a
decision, and [domain packs](references/domain-packs.md) when selecting or
contributing domain data.

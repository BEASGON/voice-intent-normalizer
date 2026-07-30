# Correction policy

## Response handling

| Response state | Host behavior |
| --- | --- |
| Valid `apply` action | Interpret this turn using `corrected_text`; show returned notices. |
| Valid `ask` action | Display `question` and wait; never execute the task or choose a candidate first. |
| Valid `keep` action | Use original text with no correction receipt. |
| Valid action with non-fatal diagnostics | Honor the decision even with `personal_invalid`, `read_only_state`, or another non-fatal diagnostic; show notices and report relevant diagnostics briefly. |
| Command failure, invalid JSON, or no valid action | Fail open: retain the original text and do not invent a correction. |
| `status=degraded` without a decision | Fail open: retain the original text and report local correction as unavailable. |

High-impact text (paths, commands, code identifiers, numbers, publishing, or
permission changes) requires stronger evidence. A proper-noun receipt says how
the Agent interpreted the text, not that it modified user input.

Only explicit correction commands create local learning records. Learning is
not a transcript-history import and is never sent with public hotword updates.

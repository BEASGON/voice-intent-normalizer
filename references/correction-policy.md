# Correction policy

The engine returns stable JSON with `apply`, `ask`, or `keep`.

| Decision | Host behavior |
| --- | --- |
| `apply` | Interpret this turn using `corrected_text`; show returned notices. |
| `ask` | Display `question` and wait before acting. |
| `keep` | Use original text with no correction receipt. |

High-impact text (paths, commands, code identifiers, numbers, publishing, or
permission changes) requires stronger evidence. A proper-noun receipt says how
the Agent interpreted the text, not that it modified user input. Any CLI
failure or diagnostic degradation fails open: retain the original text.

Only explicit correction commands create local learning records. Learning is
not a transcript-history import and is never sent with public hotword updates.

# Correction policy

## Response handling

The following JSON contract is authoritative for hosts.

<!-- voice-intent-response-contract:start -->
```json
{
  "valid_apply": {
    "use_text": "corrected_text",
    "show_notices": true
  },
  "valid_ask": {
    "show_question": true,
    "wait": true,
    "execute_task": false,
    "choose_candidate": false
  },
  "valid_keep": {
    "use_text": "original_text",
    "show_correction_receipt": false
  },
  "valid_decision_with_nonfatal_diagnostics": {
    "honor_decision": true,
    "show_notices": true,
    "report_diagnostics": true,
    "examples": ["personal_invalid", "read_only_state"]
  },
  "command_or_response_failure": {
    "fail_open": true,
    "use_text": "original_text",
    "invent_correction": false
  },
  "degraded_without_decision": {
    "fail_open": true,
    "use_text": "original_text",
    "report_unavailable": true
  }
}
```
<!-- voice-intent-response-contract:end -->
## Safety boundaries

High-impact text (paths, commands, code identifiers, numbers, publishing, or
permission changes) requires stronger evidence. A proper-noun receipt says how
the Agent interpreted the text, not that it modified user input.

Only explicit correction commands create local learning records. Learning is
not a transcript-history import and is never sent with public hotword updates.

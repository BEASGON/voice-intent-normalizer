# Platform compatibility

Voice Intent Normalizer corrects an Agent's interpretation of submitted text.
It does not alter a host's live speech or text input field.

| Platform | Activation | Install and verify | Disable and uninstall |
| --- | --- | --- | --- |
| Codex | **Automatic** | Install the documented prompt-submit hook, then run `doctor`. | Remove the owned hook; shared local lexicons remain unless separately removed. |
| OpenClaw | **Implicit** | Install through the public local-skill CLI. Success requires the CLI's eligible result; run `doctor` afterward. | Prefer the official uninstall capability. If unavailable, follow the returned manual instruction; the adapter does not delete an unverified live target. |
| WorkBuddy | **Manual** | Upload the generated ZIP through **Skills → Add Skill → Upload Skill**, enable it, then run the displayed test phrase and confirm it to `doctor`. | Close or disable the uploaded skill to stop invocation; uninstalling it does not delete shared personal lexicons. |
| Generic host | **Manual** | Use the discovered supported skill root and complete the host's documented enable step. | Remove only the verified owned capsule; preserve shared state by default. |

Capability reports are deliberately conservative. An unavailable or
unsupported platform is not reported as installed. No adapter claims access to
another host's sessions, history, or private configuration.

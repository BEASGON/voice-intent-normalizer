# Domain packs and public provenance

Select a pack only when the active request or workspace supports its domain.
`ai` covers agents and AI products, `software-development` covers development
terms, and `product-design` covers product-design terms. A domain match raises
candidate confidence; it is not permission to ignore the correction policy.

Each asset record has one source key in this table. Product-name evidence comes
from the listed official or project authority page. Speech-like aliases are
hand-curated by this project; they are not claimed to be official spellings.

| Source key | Asset record | Canonical-name evidence | Alias provenance |
| --- | --- | --- | --- |
| `codex-product-name-v1` | `base-zh.jsonl:Codex` | [OpenAI Codex](https://openai.com/index/codex-now-generally-available/) | Exact public product name only. |
| `workbuddy-product-name-v1` | `base-zh.jsonl:WorkBuddy` | [WorkBuddy](https://www.workbuddy.cn/) | Exact public product name only. |
| `agent-skills-hotword-v1` | `hotwords-snapshot.jsonl:Agent Skills` | [Agent Skills](https://agentskills.io/) | Hand-curated singular transcription alias. |
| `codex-ai-aliases-v1` | `domains/ai.jsonl:Codex` | [OpenAI Codex](https://openai.com/index/codex-now-generally-available/) | Hand-curated `code X` / `code ex` aliases. |
| `openclaw-ai-aliases-v1` | `domains/ai.jsonl:OpenClaw` | [OpenClaw](https://openclaw.ai/) | Hand-curated English and Chinese homophone aliases. |
| `workbuddy-ai-aliases-v1` | `domains/ai.jsonl:WorkBuddy` | [WorkBuddy](https://www.workbuddy.cn/) | Hand-curated `Work body` alias. |
| `codex-software-aliases-v1` | `domains/software-development.jsonl:Codex` | [OpenAI Codex](https://openai.com/index/codex-now-generally-available/) | Hand-curated development-context alias. |
| `workbuddy-software-aliases-v1` | `domains/software-development.jsonl:WorkBuddy` | [WorkBuddy](https://www.workbuddy.cn/) | Hand-curated development-context alias. |
| `workbuddy-product-design-aliases-v1` | `domains/product-design.jsonl:WorkBuddy` | [WorkBuddy](https://www.workbuddy.cn/) | Hand-curated product-design-context alias. |

Contributors must verify that names/aliases are publicly usable, add one source
key and table row per record, and never include private learning or copied
commercial dictionaries.

`龙虾` and `小龙虾` are intentionally AI-only ambiguous OpenClaw aliases. Their
industry entry stays below the apply threshold without AI supporting context and
may reach the threshold in an AI/agent context. Do not reclassify ordinary food
or animal requests as OpenClaw.

`hotwords-snapshot.jsonl` is an offline fallback in the same schema. It is not
a signed remote manifest and does not pretend to update itself; runtime updates
must use the updater's configured HTTPS, checksum, and signature boundaries.

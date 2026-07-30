# Domain packs and public provenance

Select a pack only when the active request or workspace supports its domain.
`ai` covers agents and AI products, `software-development` covers development
terms, and `product-design` covers product-design terms. A domain match raises
candidate confidence; it is not permission to ignore the correction policy.

The bundled entries are hand-curated public product names and common
transcription aliases, labeled `source: public-curated-v1`. This labels source
class and curation version, not an endorsement or a remote update signature.
Contributors must verify that names/aliases are publicly usable, add a specific
source note, and never include private learning or copied commercial
dictionaries.

`龙虾` and `小龙虾` are intentionally AI-only ambiguous OpenClaw aliases. Their
low-weight entry stays unchanged without AI supporting context; it may become a
candidate in an AI/agent context. Do not reclassify ordinary food or animal
requests as OpenClaw.

`hotwords-snapshot.jsonl` is an offline fallback in the same schema. It is not
a signed remote manifest and does not pretend to update itself; runtime updates
must use the updater's configured HTTPS, checksum, and signature boundaries.

# Public lexicon JSONL contract

Use UTF-8 JSONL: one object per nonblank line. Validate every file with the
repository loader before committing. Required fields are `canonical`, `scope`,
`aliases`, `domains`, `weight`, and `status`.

```json
{"canonical":"OpenClaw","scope":"industry","aliases":["Open Cloud"],"phonetics":[],"domains":["ai"],"weight":0.95,"status":"curated","source":"public-curated-v1"}
```

`scope` is one of `base`, `hot`, `industry`, `project`, or `personal`; bundled
files may contain only `base`, `hot`, or `industry`. `status` for a public seed
is `curated`; `weight` is a number from 0 to 1. Optional fields are
`phonetics`, `project_id`, `source`, `use_count`, `notes`, and
`negative_aliases`.

Do not add personal mappings, scanned project symbols, conversation text,
credentials, or copied restricted dictionaries. Use a short `source` value for
each public record and document the source class in domain-packs.md.

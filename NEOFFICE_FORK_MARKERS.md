# Neoffice fork markers

Map of **our** divergence from upstream (`NousResearch/hermes-agent`) in this fork
(`bvisible/hermes-agent`, branch `version-15`). Everything that can carry a comment carries a
`//// Neoffice — …` marker in place; this file holds what cannot.

At the next upstream rebase, `grep -rn "////"` in the source plus this file give the complete
picture of what is ours and why.

## Files that cannot carry a comment

| File | Why it is ours |
|---|---|
| `tests/gateway/neoffice_memory_privacy_vectors.json` | The facts that pin the company-memory rule (#881): what never reaches the bucket every colleague recalls. A byte-for-byte copy of nora's `nora/utils/memory_privacy_vectors.json`, read by the tests of both copies of the rule (`gateway/neoffice_memory_policy.py` here): change both together. |
| `tests/gateway/note_request_vectors.json` | The phrases that are, and are not, a request for a note for the person asking (`gateway/nora_chat_router.py`, `_is_note_request`). A copy, byte for byte, of the vectors the nora app tests its own reading with, so the gateway pre-router and nora read the same words: change both together. |

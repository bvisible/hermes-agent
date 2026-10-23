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
| `tests/gateway/letter_request_vectors.json` | The phrases that are, and are not, a postal letter to write (`gateway/nora_chat_router.py`, `_is_letter_request`, 21.10): letters go to the support pole and its letter tool, an explicit customer payment reminder stays the dunning's. Invented names only. |
| `tests/gateway/deck_request_vectors.json` | The phrases that are, and are not, a presentation to make (`gateway/nora_chat_router.py`, `_is_deck_request`, 22.10): a presentation is the orchestrator's own turn with its presentation tool; a question about an existing presentation, how to make one, NORA presenting herself, an e-mail or a task stay what they were; what follows « : » (unless it opens with a habit) and a span of days with an hour after it or an hours word before it are the slides' content, not a habit; `recurring_decks` stay recurring jobs (23.10). Invented names only. |
| `tests/gateway/sheet_request_vectors.json` | The phrases that are, and are not, a workbook of the company's figures to make (`gateway/nora_chat_router.py`, `_is_sheet_request`, 23.10): a new file named (« un classeur », « en Excel »), figures the sheet factory reads and a making verb make it the orchestrator's own turn with its sheet tool; a table in the chat, a question about a figure, a chart, an import or an attached file, a list export, an e-mail, a task and a recurring job stay what they were; `sheet_questions` only ask whether one can be made (`_is_sheet_question`, 23.10): NORA answers and offers, nothing is made. Invented names only. |

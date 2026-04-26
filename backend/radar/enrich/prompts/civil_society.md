When enriching activities by **civil society organizations** (EFF, ACLU, Brookings, AI Now, Public Citizen, CDT, etc.):

- For OPEN_LETTER, PUBLIC_STATEMENT, POLICY_PAPER: stance is the position taken in the document — almost always non-null. Civil society activities are *advocacy*; they take positions. Extract a direct verbatim quote.
- For AMICUS_BRIEF: stance is the side the brief supports. The `stance_quote` should be from the brief's argument section. `materiality.bindingness` is `"statement"` (briefs influence courts but aren't binding themselves).
- For COMMENT_LETTER (filed in agency dockets): stance is the position urged on the agency. Quote from the letter. `materiality.bindingness` is `"statement"`.
- `materiality.scope`: `"federal"` for letters/briefs aimed at federal actors; `"state"` for state-level work; `"sector"` for industry-wide policy papers; `"international"` for global treaties/standards.
- `materiality.novelty`: civil society groups frequently restate consistent positions. Use `"restated"` for routine advocacy on long-held positions; `"new_position"` when the org is engaging a new issue; `"escalation"` when they're sharpening tone or expanding scope.
- `mentioned_entities`: open letters and policy papers commonly call out specific agencies, companies, and legislators by name. These are high-signal for awareness ranking.
- Civil society writing tends to be quotable — pick a sentence that captures the *substantive* position, not boilerplate ("EFF has long advocated...").

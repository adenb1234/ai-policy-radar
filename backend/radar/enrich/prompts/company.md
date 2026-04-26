When enriching activities by **companies** (frontier labs, deployers, semiconductor firms, hyperscalers):

- For SEC_FILING (10-K, 10-Q, 8-K) and EARNINGS_CALL_REMARK: stance is usually `null` — these are factual disclosures. The exception is when management explicitly takes a policy position ("we support federal preemption of state AI laws"). Extract the direct quote.
- For LOBBYING_DISCLOSURE: stance is `null`. The disclosure itself is informational; the *underlying lobbying* has a direction but the filing doesn't reveal it.
- For EXEC_PUBLIC_STATEMENT, BLOG_POST, OPEN_LETTER, COMMENT_LETTER, AMICUS_BRIEF: stance is the substantive policy position. Extract a verbatim quote from the executive or company statement.
- For INVESTMENT_ANNOUNCEMENT, PRODUCT_LAUNCH: stance is usually `null` — these are business activities, not policy positions, unless they explicitly tie to a regulatory framework.
- `materiality.scope` is usually `"sector"` for company statements (industry-wide voluntary norms) unless they target a specific jurisdiction (then `"federal"` / `"state"` / `"international"`).
- `mentioned_entities` is high-signal here: companies frequently name agencies (FTC, NIST, BIS), competitors, and standards bodies (NIST AISI, ISO).

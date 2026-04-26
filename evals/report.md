## Run 20260426T214550Z

## Aggregate: avg recall —, avg reasoning 8.00, avg extras —
## Delta vs prior run: — recall, — reasoning, — extras

<!-- AGGREGATE-LINE: {"extras_quality": null, "reasoning_quality": 8.0, "relevance_recall": null, "run": "20260426T214550Z"} -->

## Per-case table

| case_id | recall | reasoning | extras | notes |
| --- | --- | --- | --- | --- |
| case_frontier_lab_policy | — | 8.00 | — | ungraded — Aden has not yet provided ground truth |

## Detailed rationales

### case_frontier_lab_policy

- **relevance_recall (—)** — ungraded — Aden has not yet provided ground truth
- **reasoning_quality (8.00)** — Reasonings are generally accurate and well-grounded in enrichment fields (stance, materiality.novelty, bindingness, topics) and tied to user equities like compute roadmap and license filings; 92233d34 and 3a363a0c are strong, while 430cf2d8 and 572af1ae appropriately downgrade weakly-binding items. Minor concern: the 2026-03-09 RFI deadline cited for 3a363a0c is attributed to activity.payload.response_due but cannot be verified from the provided fields, and the 2026-05-29 TLE close on 64db8c10 likewise references payload data not shown — possible but unverifiable, so no full deduction.
- **extras_quality (—)** — ungraded — Aden has not yet provided ground truth

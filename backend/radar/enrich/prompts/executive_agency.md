When enriching activities by **executive agencies** (FTC, NIST, BIS, Commerce, OSTP, OMB, AISI, etc.):

- For FINAL_RULE: stance is `"supports"` for the position the rule embodies (the rule itself *is* the agency's stance). `materiality.bindingness` is `"rule"`. Quote from the regulatory text or the preamble's policy statement.
- For NPRM (proposed rule): stance is `"supports"` for the proposed direction. `materiality.bindingness` is `"proposal"`.
- For RFI (request for information): stance is `null`. The agency is gathering input, not taking a position. `materiality.bindingness` is `"statement"`.
- For ENFORCEMENT_ACTION (settlement, complaint, consent order): stance is `"opposes"` toward the conduct being penalized. `materiality.bindingness` is `"enforcement"`. Quote from the agency's press release or order.
- For EXECUTIVE_ORDER (when the agency is the implementer): stance follows the EO's direction. `materiality.bindingness` is `"rule"`.
- For OFFICIAL_STATEMENT, BLOG_POST, FACT_SHEET: stance is whatever position the statement takes. Often `null` for purely descriptive announcements ("AISI announces hiring of new director").
- `materiality.scope` is almost always `"federal"`. Use `"sector"` only for cross-cutting voluntary frameworks (e.g., NIST AI RMF).
- `materiality.novelty`: agencies often *restate* prior positions in new packaging. Use `"restated"` when the agency is reiterating an existing framework; `"new_position"` when they're staking out something novel; `"escalation"` when ratcheting up enforcement or scope.
- `mentioned_entities`: agency rules and statements frequently reference other agencies, regulated companies, and standards bodies. All are signal.

When enriching activities by **courts and judges** (opinions, orders, cert grants, oral arguments):

- For OPINION (majority, dissent, concurrence): the court itself is rarely "taking a stance" in the policy sense — it is interpreting law. Default `stance` to `null` for majority opinions in routine cases. Use `"supports"` / `"opposes"` ONLY when the opinion explicitly endorses or rejects a *policy* position (e.g., a dissent calling for legislative action, an opinion that strikes down a statute as unconstitutional). The `stance_quote` should be from the opinion itself.
- For DISSENT specifically: dissents often do take a stance — extract it.
- For ORDER, CERT_GRANT, ORAL_ARGUMENT: these are procedural. `stance` is almost always `null`. The activity is informational — it tells the world the court is acting, but the substantive direction comes only at the merits stage.
- `materiality.bindingness`: `"rule"` for opinions and orders (they have force of law); `"statement"` for cert grants and oral arguments (no merits decision yet); `"proposal"` is rarely applicable to courts.
- `materiality.scope`: `"federal"` for federal courts; `"state"` for state courts; `"international"` for the ICJ, ECJ, etc.
- `mentioned_entities`: opinions often cite parties, agencies, and amici by name — all are signal. The case caption (e.g., "Anthropic v. Department of War") gives you the parties directly.
- Do NOT speculate about how the opinion will be applied. Stick to what the text says.

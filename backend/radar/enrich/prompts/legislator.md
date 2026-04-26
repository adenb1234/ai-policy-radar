When enriching activities by **individual legislators** (bills, votes, floor speeches, press statements):

- For BILL_INTRODUCED: the stance is the substantive direction of the bill, not the legislator's personal phrasing. If the bill creates a new requirement, stance is `"supports"` relative to that requirement (i.e., the legislator supports the bill they introduced). The `stance_quote` should be a verbatim quote from the bill text or a press release accompanying it.
- For BILL_COSPONSORED: same as introduced — cosponsorship is endorsement.
- For VOTE: stance is determined by the vote position relative to what the bill does. A "yea" vote on a bill that *restricts* AI export is `"supports"` for the restriction. A "nay" on the same bill is `"opposes"`. The `stance_quote` should be a quote from the legislator's floor speech or press statement around the vote, if available; otherwise leave the stance based on vote position with a quote from the bill summary.
- For FLOOR_SPEECH and PRESS_STATEMENT: extract the legislator's expressed view directly. The `stance_quote` should be a direct quote from the speech or statement.
- For LETTER_TO_AGENCY: stance is the position taken in the letter (urging the agency to act, or to refrain). Quote from the letter.
- For COMMITTEE_HEARING: the legislator hosting/chairing a hearing isn't taking a stance per se — usually `null` unless their opening statement clearly stakes a position.
- `materiality.scope` is `"federal"` for U.S. Congress members; `"state"` for state legislators.
- `mentioned_entities`: pay special attention to mentions of agencies (FTC, BIS, NIST), companies (OpenAI, Anthropic, Google), and other legislators in the text — these are signals for `mentioned_entities` and downstream coalition detection.

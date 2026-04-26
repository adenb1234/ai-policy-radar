"""Per-activity-type payload schemas for `activity.payload` JSON column.

The `activity` table stores universal fields (occurred_at, source_url, title,
raw_text, etc.) on the row itself; this module models the *type-specific*
fields that live inside the `payload` JSON blob, one pydantic model per
`activity_type` listed in BUILD_PLAN.md §15.

A registry (`PAYLOAD_SCHEMAS`) and a helper (`validate_payload`) at the bottom
let callers validate / construct payload JSON dynamically by activity_type
string.

Notes on reuse:
- `AMICUS_BRIEF` appears under both company and civil_society — modeled once.
- `COMMENT_LETTER` appears under both company and civil_society — modeled once.
- `OFFICIAL_STATEMENT` appears under both executive_agency and international —
  modeled once.

Required vs Optional asymmetry (BUILD_LOG 2026-04-26 — task 2.8b):
- Research adapters (PerEntity research adapter for civil_society / international /
  state_local / party_faction) often only have a title + a short verbatim excerpt;
  type-specific fields like `case_name`, `docket_number`, `agency` are frequently
  not stated on the source page. Forcing them required caused ~80% of EFF items
  to drop at storage validation.
- Structured adapters (Federal Register, congress.gov, CourtListener, EDGAR) get
  these fields cleanly from their APIs and SHOULD populate them.
- The compromise: keep only **load-bearing identifier** fields required (one,
  rarely two, per type — `bill_number`, `case_name`, `form_type`, `bill_or_act_id`).
  Everything else is `Optional[T] = None` so research-adapter best-effort payloads
  validate while structured-adapter full payloads still carry every detail.

All models permit extra fields (`extra="allow"`) so adapters can stash
auxiliary data without losing it; required fields remain required.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _PayloadBase(BaseModel):
    """Base for every payload model — tolerates extra adapter-supplied fields."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Company payloads
# ---------------------------------------------------------------------------


class PressReleasePayload(_PayloadBase):
    distribution_channel: str | None = None  # e.g. "businesswire", "company_newsroom"
    embargo_lifted_at: date | None = None
    tags: list[str] = []


class SecFilingPayload(_PayloadBase):
    # form_type is the load-bearing identifier — what KIND of filing this is.
    form_type: Literal["10-K", "10-Q", "8-K", "DEF 14A", "S-1", "20-F", "6-K"]
    accession_number: str | None = None
    period_of_report: date | None = None
    filed_at: date | None = None
    extracted_section: str | None = None  # e.g. "Risk Factors — AI"


class EarningsCallRemarkPayload(_PayloadBase):
    fiscal_period: str | None = None  # e.g. "Q3 2025"
    speaker: str | None = None
    speaker_role: str | None = None
    transcript_anchor: str | None = None  # paragraph id / timestamp


class BlogPostPayload(_PayloadBase):
    author: str | None = None
    author_role: str | None = None
    blog_section: str | None = None  # e.g. "research", "policy"
    tags: list[str] = []


class ExecPublicStatementPayload(_PayloadBase):
    speaker: str | None = None
    speaker_role: str | None = None
    venue: str | None = None  # e.g. "Senate hearing", "Davos panel", "X post"
    medium: Literal["speech", "interview", "social_post", "op_ed", "podcast", "other"] | None = "other"


class InvestmentAnnouncementPayload(_PayloadBase):
    counterparty: str | None = None  # who got/gave the investment
    amount_usd: float | None = None
    investment_type: Literal["equity", "debt", "grant", "joint_venture", "acquisition", "other"] | None = None
    closing_date: date | None = None


class LobbyingDisclosurePayload(_PayloadBase):
    filing_period: str | None = None  # e.g. "2025-Q3"
    amount_usd: float | None = None
    issues: list[str] = []  # LDA issue codes / topic strings
    registrant: str | None = None
    bills_referenced: list[str] = []


class AmicusBriefPayload(_PayloadBase):
    """Shared by company and civil_society."""

    case_name: str | None = None
    docket_number: str | None = None
    court: str | None = None
    side_supported: Literal["petitioner", "respondent", "neither", "appellant", "appellee"] | None = None
    co_signers: list[str] = []


class CommentLetterPayload(_PayloadBase):
    """Shared by company and civil_society."""

    docket_id: str | None = None
    agency: str | None = None
    rfi_or_nprm_title: str | None = None
    co_signers: list[str] = []


# ---------------------------------------------------------------------------
# Legislator payloads
# ---------------------------------------------------------------------------


class BillIntroducedPayload(_PayloadBase):
    # bill_number is the load-bearing identifier.
    bill_number: str  # e.g. "S.123" / "H.R.4567"
    congress: int | None = None
    chamber: Literal["house", "senate"] | None = None
    cosponsor_count: int | None = None
    committees_referred: list[str] = []


class BillCosponsoredPayload(_PayloadBase):
    bill_number: str
    congress: int | None = None
    chamber: Literal["house", "senate"] | None = None
    original_sponsor: str | None = None
    cosponsored_on: date | None = None


class VotePayload(_PayloadBase):
    bill_number: str
    vote_position: Literal["yea", "nay", "present", "not_voting"] | None = None
    roll_call_id: str | None = None
    chamber: Literal["house", "senate"] | None = None
    congress: int | None = None


class FloorSpeechPayload(_PayloadBase):
    chamber: Literal["house", "senate"] | None = None
    congressional_record_url: str | None = None
    bill_referenced: str | None = None
    duration_seconds: int | None = None


class CommitteeHearingPayload(_PayloadBase):
    committee: str | None = None
    subcommittee: str | None = None
    hearing_title: str | None = None
    role: Literal["chair", "member", "witness", "guest"] | None = "member"


class PressStatementPayload(_PayloadBase):
    medium: Literal["press_release", "social_post", "interview", "newsletter", "other"] | None = "press_release"
    co_signers: list[str] = []


class LetterToAgencyPayload(_PayloadBase):
    addressee_agency: str | None = None
    addressee_official: str | None = None
    co_signers: list[str] = []
    request_type: Literal["oversight", "rulemaking_input", "investigation", "other"] | None = "other"


# ---------------------------------------------------------------------------
# Legislative body payloads
# ---------------------------------------------------------------------------


class HearingHeldPayload(_PayloadBase):
    committee: str | None = None
    subcommittee: str | None = None
    witnesses: list[str] = []
    hearing_date: date | None = None


class MarkupPayload(_PayloadBase):
    bill_number: str | None = None
    committee: str | None = None
    outcome: Literal["reported_favorably", "reported_unfavorably", "tabled", "amended", "no_action"] | None = None
    amendments_adopted: int | None = None


class ReportReleasedPayload(_PayloadBase):
    report_number: str | None = None
    committee: str | None = None
    report_type: Literal["committee_report", "investigation", "oversight", "study", "other"] | None = "committee_report"


# ---------------------------------------------------------------------------
# Judiciary payloads
# ---------------------------------------------------------------------------


class OpinionPayload(_PayloadBase):
    # case_name is the load-bearing identifier.
    case_name: str
    court: str | None = None
    docket_number: str | None = None
    opinion_type: Literal["majority", "concurrence", "dissent", "per_curiam", "plurality"] | None = None
    panel: list[str] | None = None


class OrderPayload(_PayloadBase):
    case_name: str
    court: str | None = None
    docket_number: str | None = None
    order_type: Literal[
        "injunction",
        "stay",
        "tro",
        "summary_judgment",
        "discovery",
        "scheduling",
        "other",
    ] | None = "other"


class CertGrantPayload(_PayloadBase):
    case_name: str
    docket_number: str | None = None
    question_presented: str | None = None
    granted_on: date | None = None


class OralArgumentPayload(_PayloadBase):
    case_name: str
    court: str | None = None
    docket_number: str | None = None
    argued_on: date | None = None
    audio_url: str | None = None


# ---------------------------------------------------------------------------
# Executive agency payloads
# ---------------------------------------------------------------------------


class ExecutiveOrderPayload(_PayloadBase):
    # No required fields: `eo_number` is missing for proclamations;
    # `signing_president` is inferred from publication date and may not always
    # be derivable for state/territorial executive orders going through the FR.
    eo_number: str | None = None
    signing_president: str | None = None
    signed_on: date | None = None
    revokes: list[str] = []  # EO numbers this revokes


class NprmPayload(_PayloadBase):
    # No required fields: some FR notices arrive without a docket_id stamped.
    docket_id: str | None = None
    cfr_citations: list[str] = []
    rin: str | None = None  # Regulation Identifier Number
    comment_period_close: date | None = None


class FinalRulePayload(_PayloadBase):
    docket_id: str | None = None
    cfr_citations: list[str] = []
    rin: str | None = None
    effective_date: date | None = None


class GuidancePayload(_PayloadBase):
    guidance_id: str | None = None  # internal doc id if available
    binding: bool | None = False
    audience: str | None = None  # e.g. "covered entities", "federal contractors"


class EnforcementActionPayload(_PayloadBase):
    target: str | None = None  # respondent / firm / individual
    action_type: Literal[
        "consent_order",
        "complaint",
        "settlement",
        "fine",
        "cease_and_desist",
        "investigation_open",
        "other",
    ] | None = None
    penalty_usd: float | None = None
    docket_id: str | None = None


class OfficialStatementPayload(_PayloadBase):
    """Shared by executive_agency and international."""

    issuing_official: str | None = None
    role: str | None = None
    venue: str | None = None
    medium: Literal["press_release", "speech", "interview", "social_post", "communique", "other"] | None = "press_release"


class RfiPayload(_PayloadBase):
    docket_id: str | None = None
    response_due: date | None = None
    topics_solicited: list[str] = []


# ---------------------------------------------------------------------------
# State / local payloads
# ---------------------------------------------------------------------------


class StateLegislationPayload(_PayloadBase):
    # bill_or_act_id is the load-bearing identifier (per spec); we model it as
    # `bill_number` here for backward-compat with structured adapters that
    # populate the state bill identifier under this key.
    bill_number: str  # e.g. "CA AB 2013", "NY S.1234"
    state: str | None = None
    chamber: Literal["upper", "lower", "joint"] | None = None
    legislative_stage: str | None = None
    governor_action: Literal["signed", "vetoed", "pending", "none"] | None = None


class StateExecOrderPayload(_PayloadBase):
    state: str | None = None
    eo_number: str | None = None
    governor: str | None = None
    signed_on: date | None = None


class StateAgActionPayload(_PayloadBase):
    state: str | None = None
    target: str | None = None
    action_type: Literal[
        "lawsuit",
        "investigation",
        "settlement",
        "guidance",
        "amicus",
        "letter",
        "other",
    ] | None = None
    co_filers: list[str] = []  # other state AGs joined


class LocalOrdinancePayload(_PayloadBase):
    jurisdiction: str | None = None  # e.g. "New York City", "San Francisco"
    ordinance_number: str | None = None
    legislative_stage: str | None = None
    effective_date: date | None = None


# ---------------------------------------------------------------------------
# Civil society payloads
# ---------------------------------------------------------------------------


class PolicyPaperPayload(_PayloadBase):
    authors: list[str] = []
    series: str | None = None  # e.g. "AI Policy Brief"
    paper_type: Literal["white_paper", "report", "brief", "working_paper", "other"] | None = "white_paper"
    pages: int | None = None


class OpenLetterPayload(_PayloadBase):
    addressees: list[str] = []  # who the letter is addressed to
    signatories: list[str] = []
    signatory_count: int | None = None


class PublicStatementPayload(_PayloadBase):
    issuing_official: str | None = None
    role: str | None = None
    medium: Literal["press_release", "speech", "social_post", "interview", "blog", "other"] | None = "press_release"


# ---------------------------------------------------------------------------
# International payloads
# ---------------------------------------------------------------------------


class ForeignLegislationPayload(_PayloadBase):
    # bill_or_act_id is the load-bearing identifier.
    bill_or_act_id: str
    jurisdiction: str | None = None  # e.g. "EU", "UK", "Japan"
    legislative_stage: str | None = None
    in_force_date: date | None = None


class ForeignRegulationPayload(_PayloadBase):
    jurisdiction: str | None = None
    regulation_id: str | None = None
    issuing_body: str | None = None
    in_force_date: date | None = None


class TreatyActionPayload(_PayloadBase):
    treaty_name: str | None = None
    parties: list[str] = []
    action_type: Literal["signature", "ratification", "accession", "withdrawal", "amendment", "negotiation"] | None = None
    action_date: date | None = None


class BilateralAgreementPayload(_PayloadBase):
    parties: list[str] = []  # e.g. ["US", "UK"]
    agreement_name: str | None = None
    domain: str | None = None  # e.g. "AI safety", "compute export"
    signed_on: date | None = None


# ---------------------------------------------------------------------------
# Registry + helper
# ---------------------------------------------------------------------------


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    # company
    "PRESS_RELEASE": PressReleasePayload,
    "SEC_FILING": SecFilingPayload,
    "EARNINGS_CALL_REMARK": EarningsCallRemarkPayload,
    "BLOG_POST": BlogPostPayload,
    "EXEC_PUBLIC_STATEMENT": ExecPublicStatementPayload,
    "INVESTMENT_ANNOUNCEMENT": InvestmentAnnouncementPayload,
    "LOBBYING_DISCLOSURE": LobbyingDisclosurePayload,
    "AMICUS_BRIEF": AmicusBriefPayload,  # shared with civil_society
    "COMMENT_LETTER": CommentLetterPayload,  # shared with civil_society
    # legislator
    "BILL_INTRODUCED": BillIntroducedPayload,
    "BILL_COSPONSORED": BillCosponsoredPayload,
    "VOTE": VotePayload,
    "FLOOR_SPEECH": FloorSpeechPayload,
    "COMMITTEE_HEARING": CommitteeHearingPayload,
    "PRESS_STATEMENT": PressStatementPayload,
    "LETTER_TO_AGENCY": LetterToAgencyPayload,
    # legislative_body
    "HEARING_HELD": HearingHeldPayload,
    "MARKUP": MarkupPayload,
    "REPORT_RELEASED": ReportReleasedPayload,
    # judiciary
    "OPINION": OpinionPayload,
    "ORDER": OrderPayload,
    "CERT_GRANT": CertGrantPayload,
    "ORAL_ARGUMENT": OralArgumentPayload,
    # executive_agency
    "EXECUTIVE_ORDER": ExecutiveOrderPayload,
    "NPRM": NprmPayload,
    "FINAL_RULE": FinalRulePayload,
    "GUIDANCE": GuidancePayload,
    "ENFORCEMENT_ACTION": EnforcementActionPayload,
    "OFFICIAL_STATEMENT": OfficialStatementPayload,  # shared with international
    "RFI": RfiPayload,
    # state_local
    "STATE_LEGISLATION": StateLegislationPayload,
    "STATE_EXEC_ORDER": StateExecOrderPayload,
    "STATE_AG_ACTION": StateAgActionPayload,
    "LOCAL_ORDINANCE": LocalOrdinancePayload,
    # civil_society
    "POLICY_PAPER": PolicyPaperPayload,
    "OPEN_LETTER": OpenLetterPayload,
    "PUBLIC_STATEMENT": PublicStatementPayload,
    # international
    "FOREIGN_LEGISLATION": ForeignLegislationPayload,
    "FOREIGN_REGULATION": ForeignRegulationPayload,
    "TREATY_ACTION": TreatyActionPayload,
    "BILATERAL_AGREEMENT": BilateralAgreementPayload,
}


def validate_payload(activity_type: str, payload: dict) -> BaseModel:
    """Validate a `payload` dict against the schema for `activity_type`.

    Raises:
        ValueError: if `activity_type` is not in the registry.
        pydantic.ValidationError: if `payload` fails validation.
    """
    schema = PAYLOAD_SCHEMAS.get(activity_type)
    if schema is None:
        raise ValueError(f"unknown activity_type: {activity_type}")
    return schema.model_validate(payload)


__all__ = [
    "PAYLOAD_SCHEMAS",
    "validate_payload",
    # company
    "PressReleasePayload",
    "SecFilingPayload",
    "EarningsCallRemarkPayload",
    "BlogPostPayload",
    "ExecPublicStatementPayload",
    "InvestmentAnnouncementPayload",
    "LobbyingDisclosurePayload",
    "AmicusBriefPayload",
    "CommentLetterPayload",
    # legislator
    "BillIntroducedPayload",
    "BillCosponsoredPayload",
    "VotePayload",
    "FloorSpeechPayload",
    "CommitteeHearingPayload",
    "PressStatementPayload",
    "LetterToAgencyPayload",
    # legislative_body
    "HearingHeldPayload",
    "MarkupPayload",
    "ReportReleasedPayload",
    # judiciary
    "OpinionPayload",
    "OrderPayload",
    "CertGrantPayload",
    "OralArgumentPayload",
    # executive_agency
    "ExecutiveOrderPayload",
    "NprmPayload",
    "FinalRulePayload",
    "GuidancePayload",
    "EnforcementActionPayload",
    "OfficialStatementPayload",
    "RfiPayload",
    # state_local
    "StateLegislationPayload",
    "StateExecOrderPayload",
    "StateAgActionPayload",
    "LocalOrdinancePayload",
    # civil_society
    "PolicyPaperPayload",
    "OpenLetterPayload",
    "PublicStatementPayload",
    # international
    "ForeignLegislationPayload",
    "ForeignRegulationPayload",
    "TreatyActionPayload",
    "BilateralAgreementPayload",
]

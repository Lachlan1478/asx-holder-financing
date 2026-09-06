"""Pydantic schemas for what Claude extracts from a director's interest notice and a substantial holder notice."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SignalKind = Literal["collar", "forward", "otc_option", "equity_swap", "margin_loan", "secured_loan", "security_transfer", "lender_sale",
                     "bank_relevant_interest", "company_loan_plan", "other"]
FINANCING_KINDS = {"collar", "forward", "otc_option", "equity_swap", "margin_loan", "secured_loan", "security_transfer", "lender_sale", "bank_relevant_interest"}
ChangeCategory = Literal["on_market_buy", "on_market_sell", "off_market_buy", "off_market_sell", "transfer_no_change_in_beneficial_interest",
                         "issue_or_allotment", "vesting", "exercise", "lapse_or_expiry", "dividend_reinvestment", "entitlement_or_placement",
                         "buyback", "transfer_to_nominee_or_lender", "financing_arrangement", "lender_sale", "gift_or_estate", "other"]
Vehicle = Literal["personal", "company", "trust", "superannuation", "nominee_or_custodian", "spouse_or_family", "unknown"]
Instrument = Literal["collar", "forward", "put", "call", "swap", "loan", "margin_loan", "security_deed", "stock_lending", "employee_plan",
                     "voting_or_takeover", "fund_mandate", "other"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Signal(Strict):
    kind: SignalKind
    holder: str = Field(description="Director, or holder entity, whose securities are financed, hedged or pledged.")
    counterparty: str = Field(description="Bank, lender or derivative counterparty as named; empty if not named.")
    securities: int | None = Field(description="Number of securities covered, if stated.")
    security_class: str
    date: str = Field(description="ISO date of the arrangement or change, else empty.")
    settlement: Literal["cash", "physical", "unknown", "na"]
    confidence: Literal["high", "medium", "low"]
    evidence: str = Field(description="Verbatim quote, at most two sentences.")


class Change(Strict):
    date: str = Field(description="ISO date of change.")
    direct_or_indirect: Literal["direct", "indirect", "unknown"]
    registered_holder: str = Field(description="Entity holding the securities for an indirect interest, e.g. 'XYZ Pty Ltd as trustee for the ABC Family Trust'; empty for direct.")
    security_class: str
    category: ChangeCategory
    nature_of_change: str = Field(description="Verbatim 'nature of change' text, trimmed.")
    number: int | None = Field(description="Securities acquired (positive) or disposed (negative); null for a change in the form of an interest.")
    consideration: float | None = Field(description="Total AUD value if stated or derivable.")
    price: float | None = Field(description="AUD per security if stated or derivable.")


class Holding(Strict):
    direct_or_indirect: Literal["direct", "indirect", "unknown"]
    registered_holder: str
    vehicle: Vehicle
    security_class: str
    number_before: int | None
    number_after: int | None


class Contract(Strict):
    description: str = Field(description="Verbatim detail of contract.")
    counterparty: str
    instrument: Instrument
    number_before: int | None
    number_after: int | None


class DirectorNotice(Strict):
    form: Literal["3X", "3Y", "3Z"]
    director: str
    date_of_last_notice: str = Field(description="ISO date, or empty.")
    holdings: list[Holding] = Field(description="Every class and holding vehicle after the change (Part 1), or the initial/final positions for a 3X/3Z.")
    changes: list[Change] = Field(description="One per distinct change in Part 1. Empty for a 3X or 3Z.")
    contracts: list[Contract] = Field(description="Interests in contracts (Part 2 of a 3X or 3Y); empty if N/A or nil.")
    signals: list[Signal] = Field(description="Any financing, hedging, security or lender involvement evidenced anywhere in the notice.")


class DirectorFiling(Strict):
    symbol: str
    notices: list[DirectorNotice] = Field(description="One per director; an announcement often bundles several notices.")


class RelevantInterest(Strict):
    holder: str
    nature: str
    registered_holder: str
    security_class: str
    number: int | None


class Agreement(Strict):
    parties: str
    instrument: Instrument
    description: str
    securities: int | None
    settlement: Literal["cash", "physical", "unknown", "na"]


class SubstantialHolderFiling(Strict):
    symbol: str
    form: Literal["603", "604", "605"]
    holder: str
    holder_kind: Literal["bank_or_broker", "fund_manager_or_institution", "individual_or_private_vehicle", "listed_or_corporate", "government", "other"]
    date_of_change: str = Field(description="ISO date, or empty.")
    voting_power_before: float | None = Field(description="Percent, e.g. 5.48.")
    voting_power_after: float | None
    issued_capital: int | None = Field(description="Total shares on issue if the notice states the basis for voting power.")
    relevant_interests: list[RelevantInterest]
    agreements: list[Agreement] = Field(description="Agreements annexed or described that give rise to or change a relevant interest.")
    signals: list[Signal]

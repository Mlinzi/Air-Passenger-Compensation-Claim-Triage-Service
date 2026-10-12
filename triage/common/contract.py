"""Published data contract for the claim-submission interface (F1, D1).

Contract version is part of every stored record. Changing a field is a breaking
change and bumps CONTRACT_VERSION. The JSON schema is published at GET /contract.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0.0"


class Disruption(str, Enum):
    denied_boarding = "denied_boarding"
    cancellation = "cancellation"
    delay = "delay"
    downgrading = "downgrading"


class CabinClass(str, Enum):
    economy = "economy"
    premium_economy = "premium_economy"
    business = "business"
    first = "first"


CABIN_RANK = {"economy": 0, "premium_economy": 1, "business": 2, "first": 3}


class Route(str, Enum):
    pay_as_computed = "pay_as_computed"
    request_further_evidence = "request_further_evidence"
    decline_with_cited_provision = "decline_with_cited_provision"
    escalate_to_human = "escalate_to_human"


class ClaimRequest(BaseModel):
    """A passenger claim. All flight data is synthetic."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pnr: str = Field(pattern=r"^[A-Z0-9]{6}$", description="6-character booking reference")
    passenger_name: str = Field(min_length=2, max_length=120)
    contact_email: Optional[str] = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    carrier_code: str = Field(pattern=r"^[A-Z0-9]{2}$", description="IATA airline designator")
    flight_number: str = Field(pattern=r"^[A-Z0-9]{2}\s?\d{1,4}$")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    international: bool = False
    scheduled_departure: datetime
    disruption_type: Optional[Disruption] = Field(default=None, description="Passenger's own category; re-classified by the agent")
    notified_at: Optional[datetime] = Field(default=None, description="When the passenger was told of the disruption")
    block_time_minutes: Optional[int] = Field(default=None, ge=15, le=1200)
    actual_departure: Optional[datetime] = None
    alternate_flight_offered: Optional[bool] = None
    alternate_flight_departure: Optional[datetime] = None
    alternate_flight_taken: Optional[bool] = None
    contact_details_provided: Optional[bool] = Field(default=None, description="Contact details given at booking")
    reported_on_time: Optional[bool] = None
    booked_class: Optional[CabinClass] = None
    flown_class: Optional[CabinClass] = None
    ticket_fare_inr: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    sector_distance_km: Optional[int] = Field(default=None, ge=50, le=20000)
    airline_stated_cause: Optional[str] = Field(default=None, max_length=500)
    passenger_disputes_cause: bool = False
    description: str = Field(min_length=10, max_length=4000, description="Free-text account of what happened")

    @model_validator(mode="after")
    def cross_field(self):
        errs = []
        if self.origin == self.destination:
            errs.append("destination: must differ from origin")
        if self.alternate_flight_taken and self.alternate_flight_offered is False:
            errs.append("alternate_flight_taken: cannot be true when alternate_flight_offered is false")
        if self.alternate_flight_departure and self.alternate_flight_departure <= self.scheduled_departure:
            errs.append("alternate_flight_departure: must be after scheduled_departure")
        if self.actual_departure and self.actual_departure < self.scheduled_departure:
            errs.append("actual_departure: cannot be before scheduled_departure")
        if self.notified_at and (self.notified_at - self.scheduled_departure).total_seconds() > 86400:
            errs.append("notified_at: cannot be more than 24h after scheduled_departure")
        if self.disruption_type == Disruption.downgrading:
            if not (self.booked_class and self.flown_class):
                errs.append("flown_class: booked_class and flown_class are required for downgrading")
            elif CABIN_RANK[self.flown_class.value] >= CABIN_RANK[self.booked_class.value]:
                errs.append("flown_class: must be lower than booked_class for downgrading")
        if self.disruption_type == Disruption.delay and self.actual_departure is None:
            errs.append("actual_departure: required when disruption_type is delay")
        if errs:
            raise ValueError("; ".join(errs))
        return self


class Citation(BaseModel):
    clause_id: str
    doc_id: str
    quote: str


class Entitlement(BaseModel):
    kind: str  # compensation | refund | facilities | alternate_flight | none
    description: str
    amount_inr: Optional[float] = None
    clause_id: str


class RunResult(BaseModel):
    run_id: str
    status: str
    route: Optional[Route] = None
    disruption: Optional[str] = None
    entitlements: list[Entitlement] = []
    citations: list[Citation] = []
    rationale: str = ""
    escalation_reason: Optional[str] = None
    verified: bool = False
    degraded: bool = False
    prompt_version: str = ""
    notice: str = "Recommendation only. No payment is authorised by this system; a customer relations officer decides."


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)

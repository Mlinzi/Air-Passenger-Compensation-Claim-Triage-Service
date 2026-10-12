"""Entitlement checklist (F5, F6, F7), keyed to the official numbering of DGCA CAR Section 3
Series M Part IV, Rev. 4 (25 Jan 2023, effective 15 Feb 2023).

Each branch mirrors one condition an analyst applies. A branch may only be taken if the clause it
rests on is present in the evidence the research agent retrieved; otherwise the clause is reported
as `missing` and the research agent is sent back to find it. This is what prevents the
"amounts found, conditions not read" failure described in section 3 of the brief.

Clause map (CAR):  1.4 / 1.5 extraordinary circumstances | 2.6 denied boarding (reported in time)
  2.9 block time | 3.2.2 denied-boarding compensation | 3.3.1 notice >= 24 h: alternate or refund
  3.3.2 not informed per 3.3.1: alternate flight OR compensation + refund; facilities per 3.8.1(a)
  3.3.3 no contact details -> no financial compensation | 3.3.4 extraordinary circumstances -> none
  3.4.1-3.4.4 delay facilities | 3.5.1 downgrading | 3.6.1 foreign carriers | 3.7.1 compensation
  is referred to only in 3.2.2 and 3.3.2 | 3.8.1 facilities (a) meals (b) hotel
"""
import re

from .extract import classify_cause

CAR, CH = "car_m_iv", "airline_charter"


def k(doc, clause):
    return f"{doc}:{clause}"


class Decision:
    def __init__(self, evidence: set[str]):
        self.evidence = evidence
        self.missing: list[str] = []
        self.cited: list[str] = []
        self.entitlements: list[dict] = []
        self.basis: list[str] = []
        self.route = None
        self.escalation_reason = None

    def need(self, *keys) -> bool:
        ok = True
        for key in keys:
            if key not in self.evidence:
                if key not in self.missing:
                    self.missing.append(key)
                ok = False
        return ok

    def cite(self, key, why):
        if key not in self.cited:
            self.cited.append(key)
        self.basis.append(f"{why} [{key.split(':', 1)[1]}]")

    def ent(self, kind, desc, key, amount=None):
        self.entitlements.append({"kind": kind, "description": desc, "amount_inr": amount, "clause_id": key})

    def evidence_needed(self, why_key, why, *also):
        """Route to request-further-evidence, citing the provision that makes the fact decisive."""
        if self.need(k(CH, "C6.2"), why_key, *also):
            self.cite(why_key, why)  # `why` states the rule; the missing fact is in escalation/evidence text
            self.cite(k(CH, "C6.2"), "Supporting evidence is requested before the claim is decided")
            self.route = "request_further_evidence"

    def as_dict(self):
        return {"route": self.route, "entitlements": self.entitlements, "cited": self.cited, "basis": self.basis,
                "missing": self.missing, "escalation_reason": self.escalation_reason}


def adjudicate(category: str | None, f: dict, evidence: set[str]) -> dict:
    d = Decision(evidence)
    if category is None:
        d.route, d.escalation_reason = "escalate_to_human", "claim does not fall within the four disruption categories covered by the corpus"
        return d.as_dict()
    if not f.get("carrier_in_scope", True):
        if d.need(k(CAR, "3.6.1")):
            d.cite(k(CAR, "3.6.1"), "For foreign carriers, compensation is as contained in the regulations of their country of "
                                    "origin or as in this CAR; those regulations are outside the corpus")
            d.route, d.escalation_reason = "escalate_to_human", "foreign carrier: entitlement may depend on regulations outside the corpus"
        return d.as_dict()
    {"cancellation": _cancellation, "denied_boarding": _denied, "delay": _delay, "downgrading": _downgrade}[category](d, f)
    if d.missing:
        d.route = None
    return d.as_dict()


def _ec_clause(cause: str) -> str:
    """1.5 lists ATC, meteorological conditions and security risks; 1.4 the force-majeure list and the general test."""
    return "1.5" if re.search(r"weather|fog|storm|cyclone|rain|snow|visibility|atc|air traffic|security|bird|meteor", cause.lower()) else "1.4"


def _cause_gate(d: Decision, f: dict, exempt_clause: str, kind_label: str) -> bool:
    """Returns True if the compensation path may continue."""
    cause = f.get("airline_cause")
    kind = classify_cause(cause)
    if kind is None or kind == "operational":
        return True
    if kind == "unknown" and not f.get("provenance", {}).get("airline_cause", "").startswith("field"):
        return True  # a phrase read from the account (by regex or model) is not the airline's attribution of cause
    if not d.need(k(CAR, exempt_clause), k(CAR, "1.4"), k(CAR, "1.5")):
        return False
    if f.get("cause_disputed"):
        d.cite(k(CAR, exempt_clause), f"No {kind_label} compensation is payable for extraordinary circumstances")
        d.cite(k(CAR, "1.4"), "The exemption applies only to circumstances beyond the airline's control that could not have "
                              "been avoided even if all reasonable measures had been taken; the passenger disputes this")
        d.route, d.escalation_reason = "escalate_to_human", f"cause attribution disputed ('{cause}')"
        return False
    if kind == "contested_category":
        if d.need(k(CH, "C3.2"), k(CH, "C1.1")):
            d.cite(k(CAR, "1.4"), "The CAR's extraordinary circumstances do not list technical faults")
            d.cite(k(CAR, "1.5"), "Nor are technical faults among the causes listed in para 1.5")
            d.cite(k(CH, "C3.2"), "The airline charter treats unforeseen technical faults as beyond its control")
            d.route, d.escalation_reason = "escalate_to_human", "provisions conflict on whether the stated cause is an extraordinary circumstance"
        return False
    if kind == "unknown":
        d.cite(k(CAR, "1.4"), "The stated cause must meet the general test for circumstances beyond the airline's control")
        d.route, d.escalation_reason = "escalate_to_human", f"cause '{cause}' requires judgement under the general test"
        return False
    ec = _ec_clause(cause)
    d.cite(k(CAR, ec), f"'{cause}' is a circumstance beyond the airline's control")
    d.cite(k(CAR, exempt_clause), f"No {kind_label} compensation is payable where it is caused by extraordinary circumstances")
    d.ent("none", "Compensation declined: extraordinary circumstances", k(CAR, exempt_clause))
    d.route = "decline_with_cited_provision"
    return False


def _cancellation(d: Decision, f: dict):
    if not d.need(k(CAR, "3.3.1"), k(CAR, "3.3.2"), k(CAR, "3.3.3"), k(CAR, "3.3.4")):
        return
    notice = f.get("notice_hours")
    if notice is None:
        d.evidence_needed(k(CAR, "3.3.1"), "The airline's obligations depend on how long before departure the passenger was informed of the cancellation")
        return
    if not _cause_gate(d, f, "3.3.4", "cancellation"):
        if d.route == "decline_with_cited_provision":
            d.ent("refund", "Alternate flight or refund, as acceptable to the passenger", k(CAR, "3.3.1"))
        return
    if notice >= 24:
        when = "at least two weeks" if notice >= 336 else "less than two weeks but at least 24 hours"
        d.cite(k(CAR, "3.3.1"), f"The passenger was informed {when} before departure ({notice:.0f} h): the airline must offer an "
                                "alternate flight or a refund; the compensation in para 3.3.2 applies only to passengers not so informed")
        d.ent("none", "Cancellation compensation not payable: notice given as required by para 3.3.1", k(CAR, "3.3.1"))
        d.ent("refund", "Alternate flight or refund, as acceptable to the passenger", k(CAR, "3.3.1"))
        d.route = "decline_with_cited_provision"
        return
    # informed less than 24 hours before departure: para 3.3.2
    if f.get("contact_provided") is False:
        if d.need(k(CAR, "3.8.1")):
            d.cite(k(CAR, "3.3.3"), "No financial compensation is payable to a passenger who did not provide contact information at booking")
            d.ent("none", "Compensation declined: contact details not provided at booking", k(CAR, "3.3.3"))
            d.ent("refund", "Refund, or alternate travel arrangements, as the passenger chooses", k(CAR, "3.3.3"))
            d.route = "decline_with_cited_provision"
        return
    if f.get("alternate_taken"):
        if d.need(k(CAR, "3.8.1")):
            d.cite(k(CAR, "3.3.2"), "The airline must EITHER provide an alternate flight acceptable to the passenger OR pay "
                                    "compensation in addition to a full refund")
            d.cite(k(CAR, "3.3.2"), "The passenger accepted and travelled on the alternate flight, so the compensation limb does not apply")
            d.ent("none", "Cancellation compensation not payable: passenger accepted and travelled on the alternate flight", k(CAR, "3.3.2"))
            if f.get("at_airport") or f.get("alternate_delay_hours"):
                d.cite(k(CAR, "3.8.1"), "Meals and refreshments in relation to waiting time are owed while waiting for the alternate flight")
                d.ent("facilities", "Meals and refreshments in relation to the waiting time for the alternate flight", k(CAR, "3.8.1"))
            d.route = "decline_with_cited_provision"
        return
    if f.get("alternate_taken") is None:
        d.evidence_needed(k(CAR, "3.3.2"), "The airline must either provide an alternate flight acceptable to the passenger or pay compensation in addition to a full refund")
        return
    if f.get("contact_provided") is None and not f.get("at_airport"):
        d.evidence_needed(k(CAR, "3.3.3"), "No financial compensation is payable to passengers who did not provide adequate contact information at booking")
        return
    bt = f.get("block_time_minutes")
    if bt is None:
        d.evidence_needed(k(CAR, "2.9"), "Block time is the total time from the moment an aircraft first moves for taking off until it finally comes to rest")
        return
    if not d.need(k(CAR, "2.9")):
        return
    tier, sub = (5000, "a") if bt <= 60 else (7500, "b") if bt <= 120 else (10000, "c")
    fare = f.get("fare_inr")
    amount = min(tier, fare) if fare else tier
    d.cite(k(CAR, "2.9"), "Block time runs from the moment the aircraft first moves for take-off until it finally comes to rest")
    d.cite(k(CAR, "3.3.2"), f"Informed less than 24 hours before departure and no alternate flight was taken: compensation under "
                            f"sub-para ({sub}) is INR {tier:,} or the one-way basic fare plus fuel charge, whichever is less, "
                            f"in addition to a full refund")
    d.ent("compensation", f"Cancellation compensation, 3.3.2({sub}) (block time {bt} min)", k(CAR, "3.3.2"), float(amount))
    d.ent("refund", "Full refund of the air ticket", k(CAR, "3.3.2"))
    d.route = "pay_as_computed"


def _denied(d: Decision, f: dict):
    if not d.need(k(CAR, "3.2.2"), k(CAR, "2.6")):
        return
    if f.get("reported_on_time") is False:
        d.cite(k(CAR, "2.6"), "Denied boarding means a refusal to carry a passenger who presented himself/herself for boarding within the time specified by the airline")
        d.ent("none", "Compensation declined: passenger did not report within the specified time", k(CAR, "2.6"))
        d.route = "decline_with_cited_provision"
        return
    if f.get("reported_on_time") is None:
        d.evidence_needed(k(CAR, "2.6"), "Denied boarding means a refusal to carry a passenger who presented himself/herself for boarding within the time specified by the airline")
        return
    alt_h, fare = f.get("alternate_delay_hours"), f.get("fare_inr")
    if f.get("alternate_offered") and alt_h is not None and alt_h <= 1:
        d.cite(k(CAR, "3.2.2"), "An alternate flight scheduled to depart within one hour of the original departure was arranged: "
                                "the airline is not liable for compensation")
        d.ent("none", "Compensation not payable", k(CAR, "3.2.2"))
        d.route = "decline_with_cited_provision"
        return
    if fare is None or (f.get("alternate_offered") and alt_h is None):
        d.evidence_needed(k(CAR, "3.2.2"), "Denied boarding compensation is a percentage of the booked one-way basic fare plus fuel charge, depending on when the alternate flight departs")
        return
    if f.get("alternate_offered") and f.get("alternate_taken") is not False and alt_h <= 24:
        amt, desc = min(2 * fare, 10000), "(a) 200% of booked one-way basic fare plus fuel charge, max INR 10,000 (alternate within 24 h)"
    elif f.get("alternate_offered") and f.get("alternate_taken") is not False:
        amt, desc = min(4 * fare, 20000), "(b) 400% of booked one-way basic fare plus fuel charge, max INR 20,000 (alternate after 24 h)"
    else:
        amt, desc = min(4 * fare, 20000), "(c) full refund plus 400% of booked one-way basic fare plus fuel charge, max INR 20,000"
        d.ent("refund", "Refund of full value of ticket", k(CAR, "3.2.2"))
    d.cite(k(CAR, "3.2.2"), f"Involuntary denied boarding compensation: {desc}")
    d.ent("compensation", f"Denied boarding compensation 3.2.2{desc[:3]}", k(CAR, "3.2.2"), float(amt))
    d.route = "pay_as_computed"


def _delay(d: Decision, f: dict):
    if not d.need(k(CAR, "3.4.1"), k(CAR, "3.7.1"), k(CAR, "3.4.4")):
        return
    delay, bt = f.get("delay_hours"), f.get("block_time_minutes")
    if delay is None:
        d.evidence_needed(k(CAR, "3.4.1"), "Facilities are due when the expected delay reaches 2, 3 or 4 hours depending on block time")
        return
    d.cite(k(CAR, "3.7.1"), "The compensation this CAR provides for is the compensation referred to in Para 3.2.2 (denied boarding) and Para 3.3.2 (cancellation)")
    d.ent("none", "Monetary compensation not payable for delay", k(CAR, "3.7.1"))
    kind = classify_cause(f.get("airline_cause"))
    if kind == "extraordinary" and not f.get("cause_disputed"):
        d.cite(k(CAR, "3.4.4"), "The airline is not obliged to provide facilities where the delay is caused by extraordinary circumstances")
        d.route = "decline_with_cited_provision"
        return
    threshold, band = (2, "flights having a block time of up to 2 1/2 hrs") if (bt or 0) <= 150 else \
        (3, "flights having a block time of more than 2 1/2 hrs and up to 5 hours") if bt <= 300 else (4, "other flights")
    if delay >= threshold and d.need(k(CAR, "3.8.1")):
        d.cite(k(CAR, "3.4.1"), f"The airline shall provide facilities in accordance with Para 3.8.1 (a) for a delay of {threshold} hours or more in case of {band}")
        d.ent("facilities", "Meals and refreshments in relation to the waiting time", k(CAR, "3.8.1"))
    if delay > 6 and not f.get("international") and d.need(k(CAR, "3.4.2")):
        d.cite(k(CAR, "3.4.2"), "A domestic flight delayed more than 6 hours: option of an alternate flight within 6 hours or a full refund")
        d.ent("refund", "Alternate flight within 6 hours or full refund", k(CAR, "3.4.2"))
    if delay > 24 and d.need(k(CAR, "3.4.3")):
        d.cite(k(CAR, "3.4.3"), "Total delay over 24 hours: hotel accommodation under para 3.8.1(b)")
        d.ent("facilities", "Hotel accommodation including transfers", k(CAR, "3.4.3"))
    d.route = "decline_with_cited_provision"


def _downgrade(d: Decision, f: dict):
    intl = bool(f.get("international"))
    if not d.need(k(CAR, "3.5.1"), k(CH, "C5.1")):
        return
    fare = f.get("fare_inr")
    if (intl and f.get("distance_km") is None) or fare is None:
        d.evidence_needed(k(CAR, "3.5.1"), "An involuntarily downgraded passenger is reimbursed a percentage of the cost of ticket including taxes")
        return
    # charter C5.1 (fare difference) and CAR 3.5.1 (percentage) differ; resolved only once the
    # charter's own precedence clause has been read.
    if not d.need(k(CH, "C1.1")):
        return
    if intl:
        km = f["distance_km"]
        pct, band = (30, "flights of 1500 km or less") if km <= 1500 else (50, "flights between 1500 km to 3500 km") \
            if km <= 3500 else (75, "flights more than 3500 km")
        sub = f"(b), international sector, {band}"
    else:
        pct, sub = 75, "(a), domestic sector"
    d.cite(k(CH, "C5.1"), "The charter offers only the fare difference")
    d.cite(k(CH, "C1.1"), "Where the charter is less favourable to the passenger, the CAR prevails")
    d.cite(k(CAR, "3.5.1"), f"For involuntary downgrading, {sub}: {pct}% of the cost of ticket including taxes")
    d.ent("refund", f"Reimbursement of {pct}% of ticket cost including taxes", k(CAR, "3.5.1"), round(fare * pct / 100, 2))
    d.route = "pay_as_computed"

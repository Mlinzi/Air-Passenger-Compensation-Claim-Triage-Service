"""Deterministic classification and entity extraction (F3).

Used as the intake agent's tool and as its fallback when the model is unavailable or returns
unparseable output. Structured contract fields always win over values read from free text;
free-text values only fill gaps, and each fact records its source.
"""
import re
from datetime import datetime

NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty": 40, "forty-five": 45, "ninety": 90, "half an": 0.5}
UNIT_H = {"minute": 1 / 60, "min": 1 / 60, "hour": 1, "hr": 1, "day": 24, "week": 168}
QTY = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, NUM_WORDS), key=len, reverse=True)) + r")"
UNIT = r"(minutes?|mins?|hours?|hrs?|days?|weeks?)"

INDIAN_CARRIERS = {"AI", "6E", "SG", "UK", "QP", "IX", "I5", "9I", "S5", "2T", "G8"}

CATEGORY_CUES = {
    "cancellation": [r"\bcancel+ed\b", r"\bcancel+ation\b", r"flight was scrapped"],
    "denied_boarding": [r"denied boarding", r"refused (to let me )?board", r"overbook", r"not allowed to board", r"offloaded"],
    "downgrading": [r"downgrad", r"moved (me )?(from|to) (economy|business)", r"seated in economy"],
    "delay": [r"\bdelay(ed)?\b", r"\blate\b", r"departed .* late"],
}
OUT_OF_SCOPE_CUES = [r"\bbaggage\b", r"\bluggage\b", r"\bsuitcase\b", r"\bbag (was|got) (lost|damaged)", r"\bpet\b",
                     r"\bmedical\b.*\binjur", r"\bvisa\b"]


def to_hours(qty: str, unit: str) -> float:
    q = NUM_WORDS.get(qty.lower(), None)
    q = float(qty) if q is None else q
    u = next(v for k, v in UNIT_H.items() if unit.lower().startswith(k))
    return q * u


def classify(claim: dict) -> tuple[str | None, list[str]]:
    text = (claim.get("description") or "").lower()
    notes = []
    if any(re.search(p, text) for p in OUT_OF_SCOPE_CUES) and not any(
            re.search(p, text) for cues in CATEGORY_CUES.values() for p in cues):
        return None, ["description concerns a matter outside the CAR (e.g. baggage)"]
    scores = {k: sum(bool(re.search(p, text)) for p in v) for k, v in CATEGORY_CUES.items()}
    if claim.get("booked_class") and claim.get("flown_class") and claim["booked_class"] != claim["flown_class"]:
        scores["downgrading"] += 2
    hint = claim.get("disruption_type")
    if hint:
        scores[hint] = scores.get(hint, 0) + 1.5
    # a cancelled flight is often described as "delayed then cancelled": cancellation dominates
    if scores["cancellation"] and scores["delay"]:
        scores["delay"] -= 1
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return None, ["could not determine the disruption category"]
    if hint and hint != best:
        notes.append(f"passenger labelled the claim '{hint}' but the account describes '{best}'")
    return best, notes


def extract(claim: dict) -> dict:
    """Returns normalised facts with provenance."""
    text = claim.get("description") or ""
    low = text.lower()
    sched = _dt(claim.get("scheduled_departure"))
    f: dict = {"provenance": {}}

    def put(k, v, src):
        if v is not None and f.get(k) is None:
            f[k] = v
            f["provenance"][k] = src

    # notice period
    if claim.get("notified_at") and sched:
        put("notice_hours", round((sched - _dt(claim["notified_at"])).total_seconds() / 3600, 2), "field:notified_at")
    m = re.search(QTY + r"\s*" + UNIT + r"\s+(?:before|prior to|ahead of)\s+(?:the\s+)?(?:scheduled\s+)?(?:departure|the flight|take-?off|my flight)", low)
    if m:
        put("notice_hours", round(to_hours(m.group(1), m.group(2)), 2), "text:notice")
    if re.search(r"(at the airport|at the gate|after check-?in).{0,80}(told|informed|announced|cancel)", low) or \
            re.search(r"(told|informed|cancel).{0,80}at the (airport|gate)", low):
        f["at_airport"] = True
    if f.get("at_airport") and f.get("notice_hours") is None:
        put("notice_hours", 3.0, "text:at_airport(assumed<24h)")
    if re.search(r"no (prior )?(notice|information|message|sms|email)|never (told|informed)", low):
        put("notice_hours", 0.0, "text:no_notice")

    # alternate flight
    if claim.get("alternate_flight_offered") is not None:
        put("alternate_offered", bool(claim["alternate_flight_offered"]), "field:alternate_flight_offered")
    if claim.get("alternate_flight_taken") is not None:
        put("alternate_taken", bool(claim["alternate_flight_taken"]), "field:alternate_flight_taken")
    if claim.get("alternate_flight_departure") and sched:
        put("alternate_delay_hours", round((_dt(claim["alternate_flight_departure"]) - sched).total_seconds() / 3600, 2),
            "field:alternate_flight_departure")
    alt = re.search(r"(put|rebooked|moved|transferred|placed|offered|given)\s+(me\s+)?(on\s+|onto\s+)?(an?\s+|another\s+|the\s+)?"
                    r"(alternate\s+|alternative\s+|later\s+|next\s+)?flight", low)
    if alt:
        put("alternate_offered", True, "text:alternate")
        m = re.search(r"flight\s+(?:that\s+(?:left|departed)\s+)?" + QTY + r"\s*" + UNIT + r"\s+later", low[alt.start():])
        if m:
            put("alternate_delay_hours", round(to_hours(m.group(1), m.group(2)), 2), "text:alternate_delay")
        if re.search(r"which i (took|accepted|boarded|flew)|i (took|accepted|boarded|flew on) (it|that flight|the alternate)|and i (took|flew)", low):
            put("alternate_taken", True, "text:alternate_taken")
        if re.search(r"(refused|declined|did not take|didn't take|rejected) (it|the (alternate|alternative|later) flight)", low):
            put("alternate_taken", False, "text:alternate_declined")
    if re.search(r"no (alternate|alternative) (flight )?(was )?(offered|provided|given)", low):
        put("alternate_offered", False, "text:no_alternate")
        put("alternate_taken", False, "text:no_alternate")

    # delay length
    if claim.get("actual_departure") and sched:
        put("delay_hours", round((_dt(claim["actual_departure"]) - sched).total_seconds() / 3600, 2), "field:actual_departure")
    m = re.search(r"delayed\s+(?:by\s+)?(?:about\s+|over\s+|nearly\s+)?" + QTY + r"\s*" + UNIT, low)
    if m:
        put("delay_hours", round(to_hours(m.group(1), m.group(2)), 2), "text:delay")

    for k_src, k_dst in (("block_time_minutes", "block_time_minutes"), ("contact_details_provided", "contact_provided"),
                         ("reported_on_time", "reported_on_time"), ("ticket_fare_inr", "fare_inr"),
                         ("sector_distance_km", "distance_km"), ("international", "international"),
                         ("booked_class", "booked_class"), ("flown_class", "flown_class"),
                         ("passenger_disputes_cause", "cause_disputed")):
        if claim.get(k_src) is not None:
            put(k_dst, claim[k_src], f"field:{k_src}")
    if re.search(r"(checked in|reported) (on time|in time|well before)", low):
        put("reported_on_time", True, "text:reported_on_time")
    if re.search(r"(did not|never) (give|provide) (my |a )?(phone|mobile|contact|email)", low):
        put("contact_provided", False, "text:no_contact")

    cause = claim.get("airline_stated_cause")
    m = re.search(r"(?:due to|because of|citing|blamed)\s+([a-z \-]{3,60}?)(?:[.,;]|$)", low)
    put("airline_cause", cause or (m.group(1).strip() if m else None), "field:airline_stated_cause" if cause else "text:cause")
    if re.search(r"(but|however)[^.]{0,60}(other flights|weather was (fine|clear)|not true|lie|disput)", low):
        f["cause_disputed"] = True
        f["provenance"]["cause_disputed"] = "text:dispute"

    carrier = (claim.get("carrier_code") or "").upper()
    f["carrier_code"] = carrier
    f["carrier_in_scope"] = carrier in INDIAN_CARRIERS
    return f


def classify_cause(cause: str | None) -> str | None:
    """Maps an airline-stated cause to: extraordinary | contested_category | operational | None."""
    if not cause:
        return None
    c = cause.lower()
    if re.search(r"technical|engineering|mechanical|maintenance|snag|aog", c):
        return "contested_category"  # charter C3.2 treats as beyond control; CAR 1.4 and 1.5 do not list it
    if re.search(r"weather|fog|storm|cyclone|flood|atc|air traffic|security|strike|riot|curfew|natural|volcan|"
                 r"government|airport clos|bird", c):
        return "extraordinary"
    if re.search(r"crew|operational|commercial|schedul|low load|rotation|staff|overbook|oversold", c):
        return "operational"
    return "unknown"


def _dt(v):
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))




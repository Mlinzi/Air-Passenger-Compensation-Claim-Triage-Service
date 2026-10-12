"""Tools available to the research agent (A1). Each is called by name through RunContext.tool,
which applies the timeout, counts against the loop budget and records the step."""
from .retrieval import get_index

CLAIM_FIELDS = {"scheduled_departure", "notified_at", "block_time_minutes", "actual_departure",
                "alternate_flight_offered", "alternate_flight_departure", "alternate_flight_taken",
                "contact_details_provided", "reported_on_time", "booked_class", "flown_class", "ticket_fare_inr",
                "sector_distance_km", "airline_stated_cause", "passenger_disputes_cause", "carrier_code",
                "international", "disruption_type"}


def search_corpus(query: str, strategy: str = "dense", k: int = 5) -> dict:
    """Semantic / keyword / hybrid search over the indexed corpus."""
    if strategy not in ("dense", "keyword", "hybrid"):
        strategy = "dense"
    return {"passages": get_index().search(query, strategy, k)}


def get_clause(clause_id: str, doc_id: str | None = None) -> dict:
    """Exact lookup of a clause by its identifier, e.g. '3.6.1' or 'C6.2'."""
    return {"passages": get_index().clause(clause_id, doc_id)}


def find_modifiers(clause_id: str, doc_id: str = "car_m_iv") -> dict:
    """Clauses that refer to this clause or to the paragraph containing it -- the provisions
    most likely to add a condition or exemption to it."""
    idx = get_index()
    parents = {".".join(clause_id.split(".")[:i]) for i in range(1, clause_id.count(".") + 2)}
    out = []
    for cid, c in idx.store.items():
        sibling = c["clause_id"].rsplit(".", 1)[0] == clause_id.rsplit(".", 1)[0]
        if c["doc_id"] == doc_id and c["clause_id"] != clause_id and (sibling or parents & set(c["refs"])):
            out.append(idx.passage(cid))
    return {"passages": out}


def make_get_claim_field(claim: dict):
    def get_claim_field(field: str) -> dict:
        """Returns one field of the validated claim (the request's own data)."""
        if field not in CLAIM_FIELDS:
            return {"error": f"unknown or non-readable field '{field}'", "allowed": sorted(CLAIM_FIELDS)}
        return {"field": field, "value": claim.get(field)}
    return get_claim_field

"""Evaluation metrics (L3, L6). A TruLens-equivalent "RAG triad" computed deterministically from the
run trace so results are reproducible and free. Definitions:

groundedness       Of the statements in the answer (one per cited basis line), the fraction whose
                   cited clause was retrieved in this run AND whose quote is a literal span of it.
context_relevance  With a gold set: fraction of the gold supporting clauses that appear in the
                   retrieved passages (recall of the evidence that decides the case).
                   Without gold (live runs): fraction of retrieved clauses that the answer used,
                   i.e. cited, out of all distinct retrieved clauses (precision of retrieval).
faithfulness       With gold: 1 if the route equals the expected route AND every expected
                   decisive clause is cited, else 0. Live: 1 if the verifier passed the answer.
hallucination      Fraction of answer elements (citations + stated amounts) that do not resolve
                   to retrieved text or to a computed entitlement. Escalations with no claims = 0.
"""
import re


def _norm(t):
    return re.sub(r"\s+", " ", t or "").strip().lower()


def score(result: dict, passages: list[dict], expected: dict | None = None) -> dict:
    retrieved = {f"{p['doc_id']}:{p['clause_id']}": p for p in passages}
    cits = result.get("citations", [])
    grounded = 0
    for c in cits:
        p = retrieved.get(f"{c['doc_id']}:{c['clause_id']}")
        if p and _norm(c["quote"]) in _norm(p["text"]):
            grounded += 1
    groundedness = grounded / len(cits) if cits else (1.0 if result.get("route") == "escalate_to_human" else 0.0)

    cited_keys = {f"{c['doc_id']}:{c['clause_id']}" for c in cits}
    if expected and expected.get("supporting"):
        gold = set(expected["supporting"])
        context_relevance = len(gold & set(retrieved)) / len(gold)
    else:
        context_relevance = len(cited_keys & set(retrieved)) / len(retrieved) if retrieved else 0.0

    if expected:
        route_ok = result.get("route") == expected["route"]
        decisive = set(expected.get("decisive", []))
        faithfulness = 1.0 if route_ok and decisive <= cited_keys else 0.0
    else:
        route_ok = None
        faithfulness = 1.0 if result.get("verified") else 0.0

    elements, bad = 0, 0
    for c in cits:
        elements += 1
        p = retrieved.get(f"{c['doc_id']}:{c['clause_id']}")
        if not p or _norm(c["quote"]) not in _norm(p["text"]):
            bad += 1
    amounts = {e.get("amount_inr") for e in result.get("entitlements", []) if e.get("amount_inr")}
    passage_text = " ".join(p["text"] for p in passages)
    for a in re.findall(r"(?:Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)", result.get("rationale", "")):
        elements += 1
        v = float(a.replace(",", ""))
        if v not in amounts and a not in passage_text and f"{int(v):,}" not in passage_text:
            bad += 1
    hallucination = bad / elements if elements else 0.0
    out = {"groundedness": round(groundedness, 4), "context_relevance": round(context_relevance, 4),
           "faithfulness": faithfulness, "hallucination": round(hallucination, 4)}
    if route_ok is not None:
        out["route_correct"] = route_ok
    return out

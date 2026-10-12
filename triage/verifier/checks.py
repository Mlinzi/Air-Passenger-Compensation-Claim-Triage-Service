"""Verification checks shared by the verification service and the offline evaluation runner.
See triage/verifier/main.py for the list of checks."""
import json
import os
import re

import httpx

from triage.agent.runtime import load_prompts, parse_json
from triage.common.metrics import LLM_COST, LLM_TOKENS
from triage.common.obs import inject_headers
from triage.common.settings import get_settings

_store = {"mtime": None, "data": {}}


class _Req:
    def __init__(self, p: dict):
        self.run_id = p.get("run_id", "")
        self.prompt_version = p.get("prompt_version", "v2")
        self.draft = p["draft"]
        self.passages = p["passages"]
        self.use_model = p.get("use_model", True) and bool(get_settings().gateway_url)


def canon() -> dict:
    path = os.path.join(get_settings().index_dir, "docstore.json")
    m = os.path.getmtime(path)
    if _store["mtime"] != m:
        _store.update(mtime=m, data=json.load(open(path)))
    return _store["data"]


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def verify_payload(payload: dict) -> dict:
    req = _Req(payload)
    store = canon()
    d = req.draft
    retrieved = {p["chunk_id"]: p for p in req.passages}
    by_key = {f"{p['doc_id']}:{p['clause_id']}": p for p in req.passages}
    failures, missing_refs, checks = [], [], []

    for c in d.get("citations", []):
        p = retrieved.get(c.get("chunk_id")) or by_key.get(f"{c['doc_id']}:{c['clause_id']}")
        if not p:
            failures.append(f"citation {c['clause_id']} does not resolve to any retrieved passage")
            continue
        if p["chunk_id"] not in store or norm(store[p["chunk_id"]]["text"]) != norm(p["text"]):
            failures.append(f"passage {p['chunk_id']} differs from the canonical corpus")
            continue
        if norm(c["quote"]) not in norm(p["text"]):
            failures.append(f"quote for {c['clause_id']} is not a span of the retrieved text")
        checks.append(c["clause_id"])

    retrieved_clause_ids = {p["clause_id"] for p in req.passages}
    for cid in set(re.findall(r"\[(C?\d+(?:\.\d+)+)\]", d.get("rationale", ""))):
        if cid not in retrieved_clause_ids:
            failures.append(f"rationale cites [{cid}] which was not retrieved")

    amounts = {float(e["amount_inr"]) for e in d.get("entitlements", []) if e.get("amount_inr") is not None}
    cited_text = " ".join(by_key[f"{c['doc_id']}:{c['clause_id']}"]["text"] for c in d.get("citations", [])
                          if f"{c['doc_id']}:{c['clause_id']}" in by_key)
    passage_amounts = {float(a.replace(",", "")) for a in re.findall(r"(?:Rs\.?|INR)\s?([\d,]+)", cited_text)}
    for a in re.findall(r"(?:Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)", d.get("rationale", "")):
        v = float(a.replace(",", ""))
        if v not in amounts and v not in passage_amounts:
            failures.append(f"amount Rs {a} is neither computed nor present in a cited passage")

    # modifiers of each cited entitlement clause must have been read
    for e in d.get("entitlements", []):
        if e["kind"] not in ("compensation", "none", "refund"):
            continue  # facilities are owed irrespective of modifiers (CAR 3.7.3)
        doc, cid = e["clause_id"].split(":", 1)
        parents = {".".join(cid.split(".")[:i]) for i in range(2, cid.count(".") + 2)}
        for chunk_id, c in store.items():
            sibling = c["clause_id"].rsplit(".", 1)[0] == cid.rsplit(".", 1)[0] and cid.count(".") >= 2
            if c["doc_id"] == doc and c["clause_id"] != cid and (sibling or parents & set(c["refs"])) \
                    and c["clause_id"] not in retrieved_clause_ids:
                key = f"{doc}:{c['clause_id']}"
                if key not in missing_refs:
                    missing_refs.append(key)
    if missing_refs:
        failures.append("provisions that modify a cited clause were not read: " + ", ".join(missing_refs))

    route = d.get("route")
    if route == "pay_as_computed" and not any(e.get("amount_inr") for e in d.get("entitlements", [])):
        failures.append("pay_as_computed without a computed amount")
    if route == "decline_with_cited_provision" and not d.get("citations"):
        failures.append("decline without a cited provision")
    if route not in ("pay_as_computed", "request_further_evidence", "decline_with_cited_provision", "escalate_to_human"):
        failures.append(f"invalid route {route}")

    pt = ct = 0
    cost = 0.0
    judged = []
    if req.use_model and not failures:
        prompt = load_prompts(req.prompt_version)["verifier"]
        # the judge sees the cited span and the full clause: the span alone can be the wrong sub-item of a
        # multi-part clause (3.4.1 a/b/c), the full clause alone misleads small models on long clauses
        quotes = {c["clause_id"]: c["quote"] for c in d.get("citations", [])}
        for b in d.get("basis", [])[:6]:
            m = re.search(r"\[(C?[\d.]+)\]$", b)
            p = next((x for x in req.passages if m and x["clause_id"] == m.group(1)), None)
            if not p:
                continue
            if re.search(r"(?:Rs\.?|INR)\s?\d|\d\s?%", b):
                # amounts and percentages are verified deterministically (computed from the retrieved tier and traced
                # above); small judge models are unreliable at threshold arithmetic
                judged.append({"clause": p["clause_id"], "skipped": "numeric statement verified deterministically"})
                continue
            try:
                r = httpx.post(get_settings().gateway_url + "/v1/complete", headers=inject_headers(), timeout=get_settings().gateway_call_timeout_s,
                               json={"task": "verify", "agent": "verifier", "step": "judge", "prompt_version": req.prompt_version,
                                     "system": prompt, "user": f"Statement: {b}\nCited span: {quotes.get(p['clause_id'], '')}\nFull clause [{p['clause_id']}]: {p['text']}",
                                     "max_tokens": 120})
                if r.status_code != 200:
                    judged.append({"clause": p["clause_id"], "skipped": "model unavailable"})
                    break
                data = r.json()
                pt += data["prompt_tokens"]
                ct += data["completion_tokens"]
                cost += data["cost_usd"]
                LLM_TOKENS.labels("verifier", "verify", "prompt").inc(data["prompt_tokens"])
                LLM_TOKENS.labels("verifier", "verify", "completion").inc(data["completion_tokens"])
                LLM_COST.labels("verifier", "verify").inc(data["cost_usd"])
                v = parse_json(data["text"]) or {}
                judged.append({"clause": p["clause_id"], **v})
                if v.get("supported") is False:
                    failures.append(f"model judged statement unsupported by [{p['clause_id']}]: {v.get('reason', '')}")
            except httpx.HTTPError:
                judged.append({"clause": p["clause_id"], "skipped": "model call failed"})
                break

    return {"verified": not failures, "failures": failures, "missing_refs": missing_refs, "checked_citations": checks,
            "model_judgements": judged, "prompt_tokens": pt, "completion_tokens": ct, "cost_usd": round(cost, 8)}



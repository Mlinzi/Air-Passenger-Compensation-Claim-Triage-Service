"""Agent topology (A1, A2, A3, F3-F7), built with LangGraph.

    intake --> research <--+---------------------+
                  |        | missing provisions  | verification failed
                  v        |  (coverage loop)    |  (new strategy)
              adjudicate --+                     |
                  |                              |
                draft --> verify (Verification agent, separate service) --> finalize
                  \\________________________________________________________/
                                   any limit / failure --> escalate

* Intake agent: guardrail screen, classification and entity extraction.
* Research agent: chooses tools (search_corpus, get_clause, find_modifiers, get_claim_field) one
  call at a time. The model proposes actions; a condition-seeking policy (from the versioned
  prompt artifact) supplies follow-up actions derived from what has been read.
* Adjudication: applies the entitlement checklist; a branch only fires if its clause is in the
  retrieved evidence, otherwise the missing clause goes back to research (step 2 of section 5).
* Verification agent (own service, own prompt, own corpus access): citations must resolve to
  retrieved text, amounts must be traceable, and provisions that modify a cited clause must
  have been read. Failure -> research again with a different strategy, then escalate (step 4/5).
"""
import logging
import re
import time
from typing import TypedDict

import httpx
from langgraph.graph import END, StateGraph

from triage.common.metrics import AGENT_LATENCY, AGENT_RETRIES, AGENT_TASKS, GUARDRAIL
from triage.common.obs import inject_headers, log, tracer
from triage.common.settings import get_settings
from triage.guardrails import guards

from . import extract as ex
from . import rules, tools
from .runtime import BudgetExceeded, RunContext

logger = logging.getLogger("agent.graph")
COND_MARKERS = re.compile(r"\b(either|unless|subject to|not be payable|not payable|only where|except|shall not|irrespective)\b", re.I)
STRATEGIES = ["dense", "hybrid", "keyword"]


class S(TypedDict, total=False):
    claim: dict
    category: str | None
    facts: dict
    guard: dict
    passages: dict          # chunk_id -> passage
    queue: list             # pending research actions
    done: list              # signatures of executed actions
    derived: int
    research_round: int
    need_plan: bool
    decision: dict
    draft: dict
    verify: dict
    verify_attempts: int
    strategy_idx: int
    outcome: str            # finalize | escalate
    escalation_reason: str
    result: dict
    inject: str | None


def evidence_keys(st: S) -> set[str]:
    return {f"{p['doc_id']}:{p['clause_id']}" for p in st["passages"].values()}


def build_graph(ctx: RunContext, coverage_loop: bool = True):
    s = get_settings()
    P = ctx.prompts
    plan = P["query_plan"]

    # ------------------------------------------------------------------ intake agent
    def intake(st: S) -> S:
        t0 = time.time()
        claim = st["claim"]
        report = guards.screen_input(claim)
        for kind in ("injection", "toxicity"):
            if report[kind]:
                GUARDRAIL.labels(kind).inc()
        ctx.record("intake", "guardrail", "input_screen", {"fields": ["description", "airline_stated_cause", "passenger_name"]},
                   report, 0.1)
        category, notes = ex.classify(claim)
        facts = ex.extract(claim)
        # text flagged as an injection attempt never reaches a model
        llm = None if report["injection"] else ctx.llm("intake", "intake", P["intake"],
                      guards.untrusted_block("account", claim["description"]) +
                      f"\nStructured fields: disruption_type={claim.get('disruption_type')}, "
                      f"scheduled_departure={claim.get('scheduled_departure')}")
        if isinstance(llm, dict):  # model fills gaps only; contract fields and explicit text cues win
            out_of_scope = any("outside the CAR" in n for n in notes)  # a deterministic out-of-scope finding is never overridden
            if llm.get("category") in ("denied_boarding", "cancellation", "delay", "downgrading") and category is None                     and not out_of_scope:
                category = llm["category"]
            for k in ("notice_hours", "alternate_offered", "alternate_taken", "alternate_delay_hours", "delay_hours",
                      "airline_cause", "cause_disputed", "at_airport"):
                if facts.get(k) is None and llm.get(k) is not None:
                    facts[k] = llm[k]
                    facts["provenance"][k] = "model:intake"
        facts["classification_notes"] = notes
        ctx.record("intake", "node", "classify_extract", {"disruption_type": claim.get("disruption_type")},
                   {"category": category, "facts": facts}, (time.time() - t0) * 1000)
        AGENT_LATENCY.labels("intake").observe(time.time() - t0)
        q = []
        if category:
            q.append({"action": "search_corpus", "query": plan["primary"].format(category=category.replace("_", " ")), "strategy": "dense"})
            for mq in plan.get("modifier_queries", []):
                q.append({"action": "search_corpus", "query": mq.format(category=category.replace("_", " ")), "strategy": "dense"})
        else:
            q.append({"action": "search_corpus", "query": "applicability scope of this CAR", "strategy": "dense"})
        if not facts.get("carrier_in_scope", True):
            q.append({"action": "get_clause", "clause_id": "2.1"})
        return {"category": category, "facts": facts, "guard": report, "queue": q, "passages": {}, "done": [],
                "derived": 0, "research_round": 0, "need_plan": True, "verify_attempts": 0, "strategy_idx": 0}

    def after_intake(st: S) -> str:
        if st["guard"]["injection"] or st["guard"]["toxicity"]:
            return "escalate"
        return "research"

    # ------------------------------------------------------------------ research agent
    get_field = tools.make_get_claim_field(st_claim := {})  # bound per run below

    def sig(a: dict) -> str:
        return f"{a['action']}|{a.get('query') or a.get('clause_id') or a.get('field')}|{a.get('strategy', '')}"

    def plan_with_model(st: S) -> list[dict]:
        held = sorted(evidence_keys(st))
        user = (f"Claim category: {st['category']}\nFacts: { {k: v for k, v in st['facts'].items() if k != 'provenance'} }\n"
                f"Clauses already read: {held}\n"
                f"Provisions still required: {st.get('decision', {}).get('missing', [])}\n"
                f"Return up to 3 actions as JSON: {{\"actions\": [ ... ]}} or {{\"action\": \"finish\"}}")
        out = ctx.llm("research", "research", P["researcher"], user, max_tokens=300)
        acts = []
        if isinstance(out, dict):
            cand = out.get("actions") or ([out] if out.get("action") else [])
            for a in cand[:3]:
                if not isinstance(a, dict):
                    continue
                if a.get("action") == "search_corpus" and isinstance(a.get("query"), str):
                    acts.append({"action": "search_corpus", "query": a["query"][:200], "strategy": a.get("strategy", "dense")})
                elif a.get("action") == "get_clause" and isinstance(a.get("clause_id"), str):
                    acts.append({"action": "get_clause", "clause_id": a["clause_id"][:20]})
                elif a.get("action") == "get_claim_field" and isinstance(a.get("field"), str):
                    acts.append({"action": "get_claim_field", "field": a["field"]})
        return acts

    def absorb(st: S, passages: list[dict]) -> list[dict]:
        new = [p for p in passages if p["chunk_id"] not in st["passages"]]
        for p in new:
            st["passages"][p["chunk_id"]] = p
        follow = []
        for p in new:
            if plan.get("follow_references"):
                for ref in p["refs"]:
                    if f"{p['doc_id']}:{ref}" not in evidence_keys(st):
                        follow.append({"action": "get_clause", "clause_id": ref, "doc_id": p["doc_id"]})
                if p["doc_id"] == "car_m_iv" and re.search(r"compensation|refund", p["text"], re.I) and p["clause_id"].count(".") == 2:
                    follow.append({"action": "find_modifiers", "clause_id": p["clause_id"]})
            # query derived from what was read: the condition sentence itself (step 3)
            if plan.get("follow_references") and st["derived"] < 3:
                for sent in re.split(r"(?<=[.;])\s+", p["text"]):
                    if COND_MARKERS.search(sent) and len(sent) > 40:
                        follow.append({"action": "search_corpus", "query": " ".join(sent.split()[:18]), "strategy": "hybrid"})
                        st["derived"] += 1
                        break
        return follow

    def research(st: S) -> S:
        t0 = time.time()
        st = dict(st)
        st["queue"] = list(st["queue"])
        st["passages"] = dict(st["passages"])
        try:
            if st.get("need_plan"):
                st["need_plan"] = False
                st["queue"] = plan_with_model(st) + st["queue"]
            while st["queue"]:
                a = st["queue"].pop(0)
                if sig(a) in st["done"]:
                    continue
                st["done"] = st["done"] + [sig(a)]
                if a["action"] == "search_corpus":
                    out, ok = ctx.tool("research", "search_corpus", tools.search_corpus, query=a["query"],
                                       strategy=a.get("strategy", "dense"), k=s.retrieval_k)
                elif a["action"] == "get_clause":
                    out, ok = ctx.tool("research", "get_clause", tools.get_clause, clause_id=a["clause_id"],
                                       doc_id=a.get("doc_id"))
                elif a["action"] == "find_modifiers":
                    out, ok = ctx.tool("research", "find_modifiers", tools.find_modifiers, clause_id=a["clause_id"])
                elif a["action"] == "get_claim_field":
                    out, ok = ctx.tool("research", "get_claim_field", get_field, field=a["field"])
                    break
                else:
                    continue
                if ok and out.get("passages"):
                    st["queue"].extend(absorb(st, out["passages"]))
                break  # one tool call per graph step; the edge loops back here
        except BudgetExceeded as e:
            st["outcome"], st["escalation_reason"] = "escalate", f"loop limit reached during research: {e}"
        AGENT_LATENCY.labels("research").observe(time.time() - t0)
        return st

    def after_research(st: S) -> str:
        if st.get("outcome") == "escalate":
            return "escalate"
        return "research" if st["queue"] else "adjudicate"

    # ------------------------------------------------------------------ adjudication
    def adjudicate(st: S) -> S:
        t0 = time.time()
        d = rules.adjudicate(st["category"], st["facts"], evidence_keys(st))
        ctx.record("adjudicator", "node", "apply_checklist", {"category": st["category"], "evidence": sorted(evidence_keys(st))},
                   d, (time.time() - t0) * 1000)
        upd: S = {"decision": d}
        if d["missing"]:
            st["research_round"] = st.get("research_round", 0) + 1
            if coverage_loop and st["research_round"] <= 3:
                AGENT_RETRIES.labels("research", "coverage").inc()
                strat = STRATEGIES[min(st["research_round"], 2)]
                q = []
                for key in d["missing"]:
                    doc, cid = key.split(":", 1)
                    q.append({"action": "search_corpus", "query": f"para {cid}", "strategy": "keyword"} if strat == "keyword"
                             else {"action": "get_clause", "clause_id": cid, "doc_id": doc})
                    q.append({"action": "get_clause", "clause_id": cid, "doc_id": doc})
                upd.update(queue=q, research_round=st["research_round"], need_plan=True)
            else:
                upd.update(outcome="escalate", escalation_reason="provisions required to decide were not found in the corpus: "
                           + ", ".join(d["missing"]))
        return upd

    def after_adjudicate(st: S) -> str:
        if st.get("outcome") == "escalate":
            return "escalate"
        if st["decision"]["missing"]:
            return "research"
        if st["decision"]["route"] == "escalate_to_human":
            return "draft"  # escalations still carry a cited, verified explanation
        return "draft"

    # ------------------------------------------------------------------ drafting
    def best_quote(passage_text: str, statement: str) -> str:
        sents = [x.strip() for x in re.split(r"(?<=[.;:])\s+", passage_text) if x.strip()]
        words = set(re.findall(r"[a-z0-9]+", statement.lower()))
        return max(sents, key=lambda x: len(words & set(re.findall(r"[a-z0-9]+", x.lower())))) if sents else passage_text[:200]

    def draft(st: S) -> S:
        t0 = time.time()
        d = st["decision"]
        by_key = {}
        for p in st["passages"].values():
            by_key.setdefault(f"{p['doc_id']}:{p['clause_id']}", p)
        citations = []
        for key in d["cited"]:
            p = by_key[key]
            stmt = " ".join(b for b in d["basis"] if b.endswith(f"[{p['clause_id']}]"))
            citations.append({"clause_id": p["clause_id"], "doc_id": p["doc_id"], "chunk_id": p["chunk_id"],
                              "quote": best_quote(p["text"], stmt)})
        template = ". ".join(d["basis"]) + "."
        ctx_text = "\n".join(f"[{c['clause_id']}] {by_key[c['doc_id'] + ':' + c['clause_id']]['text']}" for c in citations)
        out = ctx.llm("adjudicator", "draft", P["drafter"],
                      f"Decision route: {d['route']}\nEntitlements: {d['entitlements']}\nBasis: {d['basis']}\nPassages:\n{ctx_text}")
        rationale = out.get("rationale") if isinstance(out, dict) and isinstance(out.get("rationale"), str) and out.get("rationale").strip() else template
        if st.get("inject") == "unsupported_citation" and st.get("verify_attempts", 0) == 0:
            # demo fault injection: an unsupported statement the verifier must catch
            rationale += " Compensation of Rs 15,000 is also payable under [3.9.7]."
            citations.append({"clause_id": "3.9.7", "doc_id": "car_m_iv", "chunk_id": "car_m_iv:3.9.7:0",
                              "quote": "the airline shall pay Rs 15,000 to every passenger"})
        dr = {"route": d["route"], "entitlements": d["entitlements"], "citations": citations, "rationale": rationale,
              "basis": d["basis"], "escalation_reason": d.get("escalation_reason")}
        ctx.record("adjudicator", "node", "draft_position", {"route": d["route"]}, dr, (time.time() - t0) * 1000)
        AGENT_LATENCY.labels("adjudicator").observe(time.time() - t0)
        return {"draft": dr}

    # ------------------------------------------------------------------ verification hand-off
    def verify(st: S) -> S:
        t0 = time.time()
        payload = {"run_id": ctx.run_id, "prompt_version": ctx.prompt_version, "draft": st["draft"],
                   "passages": list(st["passages"].values()), "use_model": ctx.llm_available}
        attempts = st.get("verify_attempts", 0) + 1
        with tracer().start_as_current_span("handoff.verifier"):
            try:
                if s.verifier_url:
                    r = httpx.post(s.verifier_url + "/verify", json=payload, headers=inject_headers(),
                                   timeout=min(s.verifier_timeout_s, ctx.remaining_s()))
                    r.raise_for_status()
                    v = r.json()
                else:  # local evaluation without the service: same checks, in process
                    from triage.verifier.checks import verify_payload
                    v = verify_payload(payload)
            except Exception as e:
                v = {"verified": False, "unavailable": True, "failures": [f"verification agent unavailable: {type(e).__name__}"]}
        ctx.steps += 1
        for k in ("prompt_tokens", "completion_tokens", "cost_usd"):
            v.setdefault(k, 0)
        ctx.pt += v["prompt_tokens"]
        ctx.ct += v["completion_tokens"]
        ctx.cost += v["cost_usd"]
        ctx.record("verifier", "node", "verify", {"attempt": attempts, "citations": [c["clause_id"] for c in st["draft"]["citations"]]},
                   v, (time.time() - t0) * 1000, ok=bool(v.get("verified")), pt=v["prompt_tokens"], ct=v["completion_tokens"],
                   cost=v["cost_usd"])
        AGENT_TASKS.labels("verifier", "pass" if v.get("verified") else "fail").inc()
        upd: S = {"verify": v, "verify_attempts": attempts}
        if not v.get("verified"):
            if v.get("unavailable") or attempts > s.agent_max_verify_retries:
                upd.update(outcome="escalate", escalation_reason="answer could not be verified: " + "; ".join(v.get("failures", [])[:3]))
            else:
                AGENT_RETRIES.labels("research", "verification").inc()
                idx = st.get("strategy_idx", 0) + 1
                strat = STRATEGIES[idx % len(STRATEGIES)]
                q = [{"action": "get_clause", "clause_id": m.split(":", 1)[1], "doc_id": m.split(":", 1)[0]} for m in v.get("missing_refs", [])]
                for b in st["draft"]["basis"][:3]:
                    q.append({"action": "search_corpus", "query": re.sub(r"\[.*?\]", "", b)[:160], "strategy": strat})
                upd.update(queue=q, strategy_idx=idx, need_plan=False)
        return upd

    def after_verify(st: S) -> str:
        if st.get("outcome") == "escalate":
            return "escalate"
        return "finalize" if st["verify"].get("verified") else "research"

    # ------------------------------------------------------------------ outputs
    def finalize(st: S) -> S:
        d = st["draft"]
        res = {"route": d["route"], "disruption": st["category"], "entitlements": d["entitlements"],
               "citations": [{k: c[k] for k in ("clause_id", "doc_id", "quote")} for c in d["citations"]],
               "rationale": d["rationale"], "escalation_reason": d.get("escalation_reason"), "verified": True,
               "degraded": ctx.degraded, "facts": {k: v for k, v in st["facts"].items()}}
        if ctx.degraded:  # completed on the deterministic fallback path: never presented as a confident answer
            res["fallback_route"] = res["route"]
            res["route"] = "escalate_to_human"
            res["escalation_reason"] = "model provider unavailable; deterministic fallback recommendation attached for officer review"
        amounts = {
            e["amount_inr"]
            for e in d["entitlements"]
            if e.get("amount_inr") is not None
        }
        res, violations = guards.filter_output(res, amounts)
        ctx.record("output_guard", "guardrail", "output_filter", {"route": res["route"]}, {"violations": violations}, 0.1)
        if violations:
            GUARDRAIL.labels("output_filter").inc()
            res["route"] = "escalate_to_human"
            res["escalation_reason"] = "output filter: " + "; ".join(violations)
        AGENT_TASKS.labels("research", "success").inc()
        AGENT_TASKS.labels("adjudicator", "success").inc()
        return {"result": res, "outcome": "finalize"}

    def escalate(st: S) -> S:
        g = st.get("guard") or {}
        reason = st.get("escalation_reason")
        if g.get("injection"):
            reason = "prompt injection detected in " + ", ".join(g["injection"]) + "; claim routed to a human without model processing"
        elif g.get("toxicity"):
            reason = "abusive content detected in " + ", ".join(g["toxicity"]) + "; routed to a human"
        AGENT_TASKS.labels("research", "escalated").inc()
        res = {"route": "escalate_to_human", "disruption": st.get("category"), "entitlements": [], "citations": [],
               "rationale": "", "escalation_reason": reason or "unable to reach a verified position", "verified": False,
               "degraded": ctx.degraded, "guardrail": g, "facts": st.get("facts", {}),
               "unverified_draft_withheld": bool(st.get("draft"))}
        ctx.record("router", "node", "escalate", {}, {"reason": res["escalation_reason"]}, 0.1)
        return {"result": res, "outcome": "escalate"}

    g = StateGraph(S)
    for name, fn in [("intake", intake), ("research", research), ("adjudicate", adjudicate), ("draft", draft),
                     ("verify", verify), ("finalize", finalize), ("escalate", escalate)]:
        g.add_node(name, fn)
    g.set_entry_point("intake")
    g.add_conditional_edges("intake", after_intake, {"research": "research", "escalate": "escalate"})
    g.add_conditional_edges("research", after_research, {"research": "research", "adjudicate": "adjudicate", "escalate": "escalate"})
    g.add_conditional_edges("adjudicate", after_adjudicate, {"research": "research", "draft": "draft", "escalate": "escalate"})
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", after_verify, {"finalize": "finalize", "research": "research", "escalate": "escalate"})
    g.add_edge("finalize", END)
    g.add_edge("escalate", END)
    return g.compile(), st_claim


def run_agent(ctx: RunContext, claim: dict, inject: str | None = None, coverage_loop: bool = True) -> dict:
    graph, claim_holder = build_graph(ctx, coverage_loop)
    claim_holder.update(claim)
    with tracer().start_as_current_span("agent.run") as span:
        span.set_attribute("run_id", ctx.run_id)
        span.set_attribute("prompt.version", ctx.prompt_version)
        try:
            final = graph.invoke({"claim": claim, "inject": inject}, {"recursion_limit": 120})
            result = final["result"]
            ctx.passages = list((final.get("passages") or {}).values())
        except Exception as e:  # any unhandled failure: never a confident answer
            log(logger, "agent run failed", logging.ERROR, error=f"{type(e).__name__}: {e}")
            AGENT_TASKS.labels("research", "error").inc()
            result = {"route": "escalate_to_human", "entitlements": [], "citations": [], "rationale": "",
                      "escalation_reason": f"run did not complete ({type(e).__name__})", "verified": False,
                      "degraded": True}
    result["prompt_version"] = ctx.prompt_version
    result["steps"] = ctx.steps
    result["tool_calls"] = ctx.tool_calls
    return result



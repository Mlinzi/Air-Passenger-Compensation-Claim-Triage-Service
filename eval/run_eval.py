"""Batch evaluation over the fixed scenario set (L2, L3).

Runs the same agent graph used in production, in process, and scores each scenario with the
metrics defined in triage/evaluation/metrics.py.

    # inside the stack (uses the gateway + verifier services):
    docker compose run --rm agent-worker python -m eval.run_eval --prompt-version v2 --label v2
    # prompt comparison (L2)
    docker compose run --rm agent-worker python -m eval.run_eval --prompt-version v1 --label v1
    docker compose run --rm agent-worker python -m eval.run_eval --compare eval/results/v1.json eval/results/v2.json
    # deliberate change (L3): fixed-size chunking instead of clause chunking
    docker compose run --rm agent-worker python -m triage.indexer.pipeline --chunking fixed --index-dir /data/index_fixed
    docker compose run --rm agent-worker python -m eval.run_eval --index-dir /data/index_fixed --label fixed_chunks
"""
import argparse
import json
import os
import statistics
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
METRICS = ("groundedness", "context_relevance", "faithfulness", "hallucination")


def load(path=os.path.join(HERE, "scenarios.jsonl")):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def run(args):
    from triage.agent.graph import run_agent
    from triage.agent.runtime import RunContext
    from triage.common.contract import ClaimRequest
    from triage.common.obs import setup_logging
    from triage.common.settings import get_settings
    from triage.evaluation.metrics import score

    setup_logging("eval", "WARNING")
    if args.index_dir:
        os.environ["INDEX_DIR"] = args.index_dir
        get_settings.cache_clear()
    if args.offline:
        os.environ["GATEWAY_URL"] = ""
        os.environ["VERIFIER_URL"] = ""
        get_settings.cache_clear()
    if args.no_cache:
        os.environ["CACHE_ENABLED"] = "false"
        get_settings.cache_clear()
    rows = []
    for sc in load():
        if args.only and sc["id"] not in args.only:
            continue
        claim = ClaimRequest.model_validate(sc["claim"]).model_dump(mode="json")
        ctx = RunContext("eval_" + uuid.uuid4().hex[:10], args.prompt_version, persist=False, offline=args.offline)
        t0 = time.time()
        res = run_agent(ctx, claim, coverage_loop=not args.no_coverage_loop)
        lat = time.time() - t0
        m = score(res, ctx.passages, sc["expected"])
        rows.append({"id": sc["id"], "expected": sc["expected"]["route"], "got": res.get("route"), **m,
                     "steps": ctx.steps, "tool_calls": ctx.tool_calls, "latency_s": round(lat, 3),
                     "cost_usd": ctx.cost, "prompt_tokens": ctx.pt, "completion_tokens": ctx.ct,
                     "escalation_reason": res.get("escalation_reason")})
        flag = "ok " if m.get("route_correct") else "BAD"
        print(f"{flag} {sc['id']:<28} expected={sc['expected']['route']:<30} got={str(res.get('route')):<30} "
              f"steps={ctx.steps:<3} tools={ctx.tool_calls:<3} {lat:5.1f}s", flush=True)
    agg = {k: round(statistics.mean(r[k] for r in rows), 4) for k in METRICS}
    agg["route_accuracy"] = round(sum(bool(r["route_correct"]) for r in rows) / len(rows), 4)
    for k in ("steps", "tool_calls", "latency_s", "cost_usd"):
        agg[f"mean_{k}"] = round(statistics.mean(r[k] for r in rows), 6)
    agg["p95_latency_s"] = round(sorted(r["latency_s"] for r in rows)[int(0.95 * (len(rows) - 1))], 3)
    out = {"label": args.label, "prompt_version": args.prompt_version, "index_dir": args.index_dir,
           "coverage_loop": not args.no_coverage_loop, "offline": args.offline, "n": len(rows),
           "aggregate": agg, "rows": rows, "ts": time.time()}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", f"{args.label}.json")
    json.dump(out, open(path, "w"), indent=1)
    print("\n" + json.dumps(agg, indent=1))
    print(f"saved {path}")


def compare(a_path, b_path):
    a, b = json.load(open(a_path)), json.load(open(b_path))
    print(f"{'metric':<22}{a['label']:>14}{b['label']:>14}{'delta':>12}")
    for k in a["aggregate"]:
        va, vb = a["aggregate"][k], b["aggregate"][k]
        print(f"{k:<22}{va:>14.4f}{vb:>14.4f}{vb - va:>+12.4f}")
    diffs = [(r["id"], r["got"], s["got"]) for r, s in zip(a["rows"], b["rows"]) if r["got"] != s["got"]]
    if diffs:
        print("\nscenarios whose route changed:")
        for d in diffs:
            print(f"  {d[0]}: {d[1]} -> {d[2]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt-version", default="v2")
    ap.add_argument("--label", default=None)
    ap.add_argument("--index-dir", default=None)
    ap.add_argument("--offline", action="store_true", help="no gateway/verifier services; deterministic paths only")
    ap.add_argument("--no-coverage-loop", action="store_true", help="ablation: do not send missing provisions back to research")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
        sys.exit(0)
    a.label = a.label or a.prompt_version
    run(a)

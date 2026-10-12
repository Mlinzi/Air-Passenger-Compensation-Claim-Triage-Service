"""Idempotent corpus pipeline: read -> parse -> chunk -> embed -> index (D2, L1).

    python -m triage.indexer.pipeline                 # incremental build (only changed chunks embedded)
    python -m triage.indexer.pipeline --reembed car_m_iv   # re-embed one document only
    python -m triage.indexer.pipeline --chunking fixed --index-dir /data/index_fixed   # experiment

Chunking strategy: one chunk per numbered clause. The difficulty in this corpus is that the
conditions that remove an entitlement sit next to, or are referenced from, the clause that states
the amount. Splitting at clause boundaries keeps each condition attached to its clause, and each
chunk records (a) its section heading path and (b) every clause it cross-references ("Para 3.8.1 (a)", "Para 1.4 and Para 1.5"),
so the agent can follow references to definitions and modifiers instead of hoping a similarity
search returns them.
"""
import argparse
import glob
import hashlib
import json
import logging
import os
import re
import time

from triage.common.obs import log, setup_logging
from triage.common.settings import get_settings

logger = logging.getLogger("indexer")

CLAUSE_RE = re.compile(r"^(C?\d{1,2}(?:\.\d{1,2})*)\.?\s+(.+)$")
# "Para 3.8.1 (a)", "Para 1.4 and Para 1.5", "para 3.2.2/3.3.2/3.5.1", "para 3.4.1 to 3.4.3"
REF_RE = re.compile(r"\bparas?\s+(\d+(?:\.\d+)+(?:\s*(?:/|,|and|to|&)\s*(?:paras?\s+)?\d+(?:\.\d+)+)*)", re.I)
# running headers/footers of the official CAR PDF
PDF_NOISE = [re.compile(p, re.I) for p in (
    r"^CIVIL AVIATION REQUIREMENTS?\s*$", r"^SERIES\s+.?M.?\s+PART\s+IV\s*$", r"^SECTION 3\s*$",
    r"^6TH AUGUST, 2010\s*$", r"^\d{1,2}\s*$", r"^Rev\.\s*\d+, dated .*$")]
DOC_TITLES = {"car_m_iv": "DGCA CAR Section 3 Series M Part IV - Facilities to be provided to passengers by airlines "
                          "due to denied boarding, cancellation of flights and delays in flights",
              "airline_charter": "Airline Passenger Charter"}

# official PDFs dropped into corpus/raw are mapped to doc ids by filename prefix
RAW_DOC_IDS = {"car": "car_m_iv", "charter": "airline_charter"}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def read_sources(corpus_dir: str) -> list[dict]:
    docs = {}
    for path in sorted(glob.glob(os.path.join(corpus_dir, "seed", "*.txt"))):
        text = open(path, encoding="utf-8").read()
        meta = dict(re.findall(r"^#(\w+)\s+(.+)$", text, re.M))
        body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
        docs[meta["DOC"]] = {"doc_id": meta["DOC"], "title": meta.get("TITLE", meta["DOC"]),
                             "version": meta.get("VERSION", "unknown"), "source": path, "text": body}
    for path in sorted(glob.glob(os.path.join(corpus_dir, "raw", "*.pdf"))):
        from pypdf import PdfReader

        name = os.path.basename(path).lower()
        doc_id = next((v for k, v in RAW_DOC_IDS.items() if name.startswith(k)), None)
        if not doc_id:
            continue
        text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        if len(text.strip()) < 200:
            log(logger, "pdf has no text layer; OCR needed, keeping seed", path=path)
            continue
        raw = open(path, "rb").read()
        rev = re.search(r"Rev\.\s*(\d+), dated ([^\n]+)", text)
        version = (f"rev{rev.group(1)} ({rev.group(2).strip()}) " if rev else "") + "pdf-" + hashlib.sha256(raw).hexdigest()[:12]
        lines = [l for l in text.splitlines() if not any(n.match(l.strip()) for n in PDF_NOISE)]
        start = next((i for i, l in enumerate(lines) if CLAUSE_RE.match(l.strip())), 0)  # skip the cover block
        docs[doc_id] = {"doc_id": doc_id, "title": docs.get(doc_id, {}).get("title", DOC_TITLES.get(doc_id, doc_id)),
                        "version": version, "source": path, "text": "\n".join(lines[start:])}
    return list(docs.values())


def _key(cid: str):
    return tuple(int(x) for x in re.findall(r"\d+", cid))


def _after(cid: str, prev: str) -> bool:
    """A new clause must be an immediate successor of the previous one: its first child (3.3 -> 3.3.1),
    next sibling (3.3.1 -> 3.3.2) or the next number at an ancestor level (3.3.5 -> 3.4, 2.10 -> 3)."""
    if cid.startswith("C") != prev.startswith("C"):
        return True
    a, b = _key(prev), _key(cid)
    if b == a + (1,):
        return True
    return any(b == a[:i] + (a[i] + 1,) for i in range(len(a)))


def parse_clauses(doc: dict) -> list[dict]:
    clauses, headings = [], {}
    cur = None
    for line in doc["text"].splitlines():
        line = line.strip()
        if not line:
            continue
        m = CLAUSE_RE.match(line)
        if m and clauses and not _after(m.group(1), clauses[-1]["clause_id"]):
            m = None  # a wrapped line that happens to start with a paragraph number ("1.5 which ...")
        if m:
            cid, body = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
            depth = cid.count(".")
            if depth <= 1 and len(body) < 80 and not body.endswith((".", ":")):  # a heading, not a provision
                headings[cid] = body
            cur = {"clause_id": cid, "text": body, "depth": depth}
            clauses.append(cur)
        elif cur is not None:
            cur["text"] += " " + re.sub(r"\s+", " ", line)
    for c in clauses:
        parts = c["clause_id"].split(".")
        path = [headings[".".join(parts[:i])] for i in range(1, len(parts)) if ".".join(parts[:i]) in headings]
        c["heading_path"] = " > ".join(path)
        c["refs"] = sorted(expand_refs(c["text"]) - {c["clause_id"]})
    return clauses


def expand_refs(text: str) -> set[str]:
    refs = set()
    for m in REF_RE.finditer(text):
        span = m.group(1)
        ids = re.findall(r"\d+(?:\.\d+)+", span)
        refs.update(ids)
        if re.search(r"\bto\b", span) and len(ids) == 2:  # "3.4.1 to 3.4.3"
            pa, pb = ids[0].split("."), ids[1].split(".")
            if pa[:-1] == pb[:-1]:
                refs.update(".".join(pa[:-1] + [str(i)]) for i in range(int(pa[-1]), int(pb[-1]) + 1))
    return refs


def chunk(doc: dict, strategy: str = "clause", size: int = 500, overlap: int = 80) -> list[dict]:
    out = []
    clauses = parse_clauses(doc)
    if strategy == "clause":
        for c in clauses:
            header = f"[{doc['doc_id']} para {c['clause_id']}] {c['heading_path']}".strip()
            pieces = [c["text"]] if len(c["text"]) <= 1500 else [c["text"][i:i + 1200] for i in range(0, len(c["text"]), 1100)]
            for n, p in enumerate(pieces):
                out.append({"chunk_id": f"{doc['doc_id']}:{c['clause_id']}:{n}", "doc_id": doc["doc_id"],
                            "clause_id": c["clause_id"], "heading_path": c["heading_path"], "refs": c["refs"],
                            "text": p, "embed_text": f"{header} :: {p}", "doc_version": doc["version"]})
    else:  # fixed-size baseline used in the evaluation experiment
        flat, spans = "", []
        for c in clauses:
            spans.append((len(flat), c["clause_id"]))
            flat += f"{c['clause_id']} {c['text']} "
        for n, i in enumerate(range(0, len(flat), size - overlap)):
            piece = flat[i:i + size]
            cid = [s for s in spans if s[0] <= i][-1][1] if spans else "?"
            out.append({"chunk_id": f"{doc['doc_id']}:fx{n}", "doc_id": doc["doc_id"], "clause_id": cid,
                        "heading_path": "", "refs": sorted(expand_refs(piece)), "text": piece,
                        "embed_text": piece, "doc_version": doc["version"]})
    for c in out:
        c["content_hash"] = sha(c["embed_text"])
    return out


def build(index_dir: str, corpus_dir: str, strategy: str = "clause", reembed: list[str] | None = None) -> dict:
    from langchain_chroma import Chroma

    from triage.agent.retrieval import get_embeddings

    t0 = time.time()
    os.makedirs(index_dir, exist_ok=True)
    store_path = os.path.join(index_dir, "docstore.json")
    old = json.load(open(store_path)) if os.path.exists(store_path) else {}

    docs = read_sources(corpus_dir)
    chunks = [c for d in docs for c in chunk(d, strategy)]
    new = {c["chunk_id"]: c for c in chunks}

    reembed = set(reembed or [])
    to_upsert = [c for cid, c in new.items()
                 if cid not in old or old[cid]["content_hash"] != c["content_hash"] or c["doc_id"] in reembed]
    to_delete = [cid for cid in old if cid not in new]

    vs = Chroma(collection_name="corpus", embedding_function=get_embeddings(cached=False),
                persist_directory=os.path.join(index_dir, "chroma"))
    if to_delete:
        vs.delete(ids=to_delete)
    if to_upsert:
        ids = [c["chunk_id"] for c in to_upsert]
        try:
            vs.delete(ids=ids)
        except Exception:
            pass
        vs.add_texts(texts=[c["embed_text"] for c in to_upsert], ids=ids,
                     metadatas=[{"doc_id": c["doc_id"], "clause_id": c["clause_id"], "chunk_id": c["chunk_id"]}
                                for c in to_upsert])

    tmp = store_path + ".tmp"
    json.dump(new, open(tmp, "w"), indent=1)
    os.replace(tmp, store_path)

    elapsed = max(time.time() - t0, 1e-6)
    manifest = {
        "built_at": time.time(),
        "index_version": sha("".join(sorted(c["content_hash"] for c in chunks)))[:12],
        "chunking": strategy,
        "embedding_model": get_settings().embedding_model,
        "sources": [{"doc_id": d["doc_id"], "version": d["version"], "source": os.path.basename(d["source"])} for d in docs],
        "doc_count": len(docs),
        "chunk_count": len(chunks),
        "embedded_this_run": len(to_upsert),
        "deleted_this_run": len(to_delete),
        "elapsed_s": round(elapsed, 3),
        "chunks_per_second": round(len(to_upsert) / elapsed, 2),
    }
    json.dump(manifest, open(os.path.join(index_dir, "manifest.json"), "w"), indent=1)
    log(logger, "index built", **{k: v for k, v in manifest.items() if k != "sources"})
    return manifest


def main():
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-dir", default=s.index_dir)
    ap.add_argument("--corpus-dir", default=s.corpus_dir)
    ap.add_argument("--chunking", choices=["clause", "fixed"], default="clause")
    ap.add_argument("--reembed", nargs="*", default=None, help="doc ids to re-embed without touching the rest")
    a = ap.parse_args()
    setup_logging("indexer", s.log_level)
    print(json.dumps(build(a.index_dir, a.corpus_dir, a.chunking, a.reembed), indent=1))


if __name__ == "__main__":
    main()

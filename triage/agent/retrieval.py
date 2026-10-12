"""Retrieval layer (L1, D7): dense (Chroma + sentence-transformers via LangChain), keyword (BM25),
hybrid (reciprocal-rank fusion), and lookup by clause identifier. Query embeddings and retrieval
results are cached in Redis, keyed by index version so a rebuild invalidates them."""
import json
import os
import re
import threading

from triage.common import cache
from triage.common.metrics import CACHE
from triage.common.settings import get_settings

_lock = threading.Lock()
_state = {"mtime": None}


class CachedEmbeddings:
    """Wraps LangChain embeddings; query vectors are cached in Redis."""

    def __init__(self, inner):
        self.inner = inner

    def embed_documents(self, texts):
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        k = cache.key("emb", get_settings().embedding_model, text)
        v = cache.get_json(k)
        if v is not None:
            CACHE.labels("embedding", "hit").inc()
            return v
        CACHE.labels("embedding", "miss").inc()
        v = self.inner.embed_query(text)
        cache.set_json(k, list(v), ttl=7 * 86400)
        return v


_emb = None


def get_embeddings(cached: bool = True):
    global _emb
    if _emb is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        model = get_settings().embedding_model
        if not model:
            raise RuntimeError("EMBEDDING_MODEL is not configured (see .env.example)")
        _emb = HuggingFaceEmbeddings(model_name=model, encode_kwargs={"normalize_embeddings": True})
    return CachedEmbeddings(_emb) if cached else _emb


def tokenize(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", s.lower())


class Index:
    def __init__(self, index_dir: str):
        from langchain_chroma import Chroma
        from rank_bm25 import BM25Okapi

        self.dir = index_dir
        self.manifest = json.load(open(os.path.join(index_dir, "manifest.json")))
        self.store: dict = json.load(open(os.path.join(index_dir, "docstore.json")))
        self.ids = list(self.store)
        self.bm25 = BM25Okapi([tokenize(self.store[i]["embed_text"]) for i in self.ids])
        self.by_clause: dict[str, list[str]] = {}
        for cid, c in self.store.items():
            self.by_clause.setdefault(c["clause_id"], []).append(cid)
        self.vs = Chroma(collection_name="corpus", embedding_function=get_embeddings(),
                         persist_directory=os.path.join(index_dir, "chroma"))

    @property
    def version(self) -> str:
        return self.manifest["index_version"]

    def passage(self, chunk_id: str, score: float | None = None) -> dict:
        c = self.store[chunk_id]
        return {"chunk_id": chunk_id, "doc_id": c["doc_id"], "clause_id": c["clause_id"],
                "heading_path": c["heading_path"], "refs": c["refs"], "text": c["text"],
                "score": None if score is None else round(float(score), 4)}

    def dense(self, query: str, k: int) -> list[dict]:
        hits = self.vs.similarity_search_with_relevance_scores(query, k=k)
        return [self.passage(d.metadata["chunk_id"], s) for d, s in hits if d.metadata.get("chunk_id") in self.store]

    def keyword(self, query: str, k: int) -> list[dict]:
        scores = self.bm25.get_scores(tokenize(query))
        top = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [self.passage(self.ids[i], scores[i]) for i in top if scores[i] > 0]

    def hybrid(self, query: str, k: int) -> list[dict]:
        fused: dict[str, float] = {}
        for lst in (self.dense(query, k * 2), self.keyword(query, k * 2)):
            for rank, p in enumerate(lst):
                fused[p["chunk_id"]] = fused.get(p["chunk_id"], 0) + 1 / (60 + rank)
        top = sorted(fused, key=lambda c: -fused[c])[:k]
        return [self.passage(c, fused[c]) for c in top]

    def search(self, query: str, strategy: str = "dense", k: int = 5) -> list[dict]:
        ck = cache.key("ret", self.version, strategy, query, k)
        hit = cache.get_json(ck)
        if hit is not None:
            CACHE.labels("retrieval", "hit").inc()
            return hit
        CACHE.labels("retrieval", "miss").inc()
        res = {"dense": self.dense, "keyword": self.keyword, "hybrid": self.hybrid}[strategy](query, k)
        cache.set_json(ck, res)
        return res

    def clause(self, clause_id: str, doc_id: str | None = None) -> list[dict]:
        cid = clause_id.strip().lstrip("§").replace("para ", "")
        out = [self.passage(x) for x in self.by_clause.get(cid, [])]
        return [p for p in out if not doc_id or p["doc_id"] == doc_id]


def get_index(index_dir: str | None = None) -> Index:
    """Process-wide index, reloaded when the indexer publishes a new manifest."""
    d = index_dir or get_settings().index_dir
    path = os.path.join(d, "manifest.json")
    mtime = os.path.getmtime(path)
    with _lock:
        if _state.get("dir") != d or _state["mtime"] != mtime:
            _state.update(index=Index(d), mtime=mtime, dir=d)
        return _state["index"]

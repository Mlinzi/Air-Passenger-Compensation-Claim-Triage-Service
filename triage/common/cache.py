"""Redis-backed caches and per-client rate limiting (D7)."""
import hashlib
import json
import time

import redis

from .settings import get_settings

_r = None


def r() -> redis.Redis:
    global _r
    if _r is None:
        _r = redis.Redis.from_url(get_settings().redis_url)
    return _r


def key(prefix: str, *parts) -> str:
    h = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"{prefix}:{h}"


def get_json(k: str):
    if not get_settings().cache_enabled:
        return None
    try:
        v = r().get(k)
        return json.loads(v) if v else None
    except redis.RedisError:
        return None  # cache is an optimisation; never fail a run because of it


def set_json(k: str, v, ttl: int | None = None) -> None:
    if not get_settings().cache_enabled:
        return
    try:
        r().set(k, json.dumps(v, default=str), ex=ttl or get_settings().cache_ttl_s)
    except redis.RedisError:
        pass


def rate_limit(client_id: str) -> tuple[bool, int]:
    """Fixed one-minute window per client. Returns (allowed, remaining)."""
    limit = get_settings().rate_limit_per_min
    window = int(time.time() // 60)
    k = f"rl:{client_id}:{window}"
    pipe = r().pipeline()
    pipe.incr(k)
    pipe.expire(k, 70)
    n, _ = pipe.execute()
    return n <= limit, max(0, limit - n)

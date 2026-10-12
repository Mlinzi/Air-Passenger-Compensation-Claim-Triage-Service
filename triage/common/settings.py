"""Runtime configuration. Every environment-specific value is injected via env (D4)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: str = "triage"
    log_level: str = "INFO"

    # infrastructure endpoints (compose service names are injected, not hard-coded)
    redis_url: str = ""
    database_url: str = ""
    gateway_url: str = ""
    verifier_url: str = ""
    otel_endpoint: str = ""  # e.g. http://jaeger:4318/v1/traces

    # retrieval
    embedding_model: str = ""
    index_dir: str = "/data/index"
    corpus_dir: str = "/corpus"
    retrieval_k: int = 5

    # agent loop bounds (A1)
    agent_max_steps: int = 60
    agent_max_tool_calls: int = 45
    agent_wall_clock_s: float = 90.0
    agent_max_verify_retries: int = 2

    # model call reliability (A5) -- used by the gateway
    llm_provider: str = "stub"  # stub | openai_compat
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 3
    llm_backoff_base_s: float = 0.5
    breaker_fail_threshold: int = 5
    breaker_reset_s: float = 30.0
    llm_price_prompt_per_1k: float = 0.0
    llm_price_completion_per_1k: float = 0.0
    stub_delay_s: float = 1.5
    stub_fail: bool = False  # simulate provider outage (demo item 8)

    # worker-side timeouts
    tool_timeout_s: float = 10.0
    gateway_call_timeout_s: float = 45.0
    verifier_timeout_s: float = 20.0

    # caching / rate limiting (D7)
    cache_enabled: bool = True
    cache_ttl_s: int = 3600
    rate_limit_per_min: int = 30

    prompt_version: str = "v2"
    demo_mode: bool = False  # enables fault-injection hooks for the demo


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""
Centralized configuration for Zovark worker.
Reads from environment variables with ZOVARK_ prefix, falls back to .env file.
SecretStr prevents accidental logging of credentials.

stabilize-runtime-hygiene (2026-04-15):
  Credential fields (db_password, redis_password, llm_key) are now REQUIRED.
  Pydantic will raise ValidationError at import time if env is missing, which
  is the desired fail-fast behavior. The former `_DEFAULT_*` fallbacks and the
  tests-only try/except wrapper have been removed.
"""
import logging
import urllib.parse
from pydantic import Field, AliasChoices, SecretStr
from pydantic_settings import BaseSettings

_settings_logger = logging.getLogger("zovark.settings")


class ZovarkSettings(BaseSettings):
    # Database
    db_host: str = "pgbouncer"
    db_port: int = 5432
    db_user: str = "zovark"
    db_password: SecretStr  # REQUIRED — set via ZOVARK_DB_PASSWORD
    db_name: str = "zovark"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: SecretStr  # REQUIRED — set via ZOVARK_REDIS_PASSWORD

    # OpenAI / LLM — Ticket 8 defaults: OpenAI Chat Completions + gpt-4o-mini
    # ZOVARK_LLM_PROVIDER: "openai" or "local" (llama.cpp-compatible)
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com"
    llm_endpoint: str = "https://api.openai.com/v1/chat/completions"
    llm_fast_model: str = "gpt-4o-mini"
    llm_quality_model: str = "gpt-4o-mini"
    # Bearer token for local / air-gap inference endpoints (llama-server, etc).
    # REQUIRED — set via ZOVARK_LLM_KEY. OpenAI calls use openai_api_key first.
    llm_key: SecretStr
    openai_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "ZOVARK_OPENAI_API_KEY"),
    )

    # Execution
    execution_mode: str = "tools"
    path_d_fallback_enabled: bool = True
    mode: str = "full"  # "full" or "templates-only"

    # Governance
    default_autonomy_level: str = "observe"

    # External threat intel (attack-surface API). Default off for air-gap / silent-failure avoidance.
    threat_intel_enabled: bool = False

    # LLM context budgeting (approximate; chars/4 heuristic in llm_client)
    context_token_budget: int = 12000

    # Optional absolute path to a GBNF grammar file (overrides grammar_name -> worker/grammars/*.gbnf)
    grammar_file: str = ""

    # DPO forge — override NVIDIA default; set to local llama-server chat completions URL for air-gap
    dpo_forge_endpoint: str = ""

    # Operational
    max_investigation_timeout_seconds: int = 300
    max_concurrent_activities: int = 16   # FIX #20: was 8
    max_concurrent_workflows: int = 32    # FIX #20: new field

    # Feature flags — FIX #20: govern via settings instead of bare os.environ.get
    fast_fill: bool = False               # ZOVARK_FAST_FILL
    human_review_threshold: int = 60      # ZOVARK_HUMAN_REVIEW_THRESHOLD
    dedup_enabled: bool = True            # ZOVARK_DEDUP_ENABLED

    # Parallel tool execution (Feature C)
    parallel_tools_enabled: bool = False  # ZOVARK_PARALLEL_TOOLS_ENABLED
    max_parallel_tools: int = 4  # ZOVARK_MAX_PARALLEL_TOOLS (1-8)

    # Observability — docker-compose sets OTEL_ENABLED + OTEL_EXPORTER_OTLP_ENDPOINT (no ZOVARK_ prefix).
    # Aliases keep worker aligned with standard OTEL env vars and compose.
    otel_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("OTEL_ENABLED", "ZOVARK_OTEL_ENABLED"),
    )
    otel_endpoint: str = Field(
        default="http://zovark-signoz-collector:4318",
        validation_alias=AliasChoices(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "ZOVARK_OTEL_ENDPOINT",
        ),
    )

    # Optional data plane (SurrealDB + Redpanda + DuckDB) — docker-compose.data-plane.yml
    redpanda_enabled: bool = False
    redpanda_brokers: str = "redpanda:9092"
    redpanda_topic_investigations: str = "zovark.investigations.completed"
    surreal_enabled: bool = False
    surreal_http_url: str = "http://surrealdb:8000"
    surreal_user: str = "root"
    # Default-empty SecretStr (not required) because SurrealDB is gated on
    # `surreal_enabled=False` by default. Operators who enable the data plane
    # must set ZOVARK_SURREAL_PASSWORD; the `surreal_enabled` + empty-password
    # combination is a misconfiguration operators should catch at deploy time.
    surreal_password: SecretStr = SecretStr("")
    surreal_ns: str = "zovark"
    surreal_db: str = "core"
    duckdb_enabled: bool = False
    duckdb_path: str = "/data/duckdb/analytics.duckdb"

    model_config = {
        "env_prefix": "ZOVARK_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def database_url(self) -> str:
        # Audit 2.28: URL-encode the password so characters like @, :, /, #, ?
        # don't break the connection string parser downstream.
        pw = urllib.parse.quote(self.db_password.get_secret_value(), safe="")
        user = urllib.parse.quote(self.db_user, safe="")
        return f"postgresql://{user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def redis_url(self) -> str:
        pw = urllib.parse.quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{pw}@{self.redis_host}:{self.redis_port}/0"


# Singleton — import this everywhere.
#
# If any required env var is missing (ZOVARK_DB_PASSWORD, ZOVARK_REDIS_PASSWORD,
# ZOVARK_LLM_KEY), Pydantic raises ValidationError at module import time.
# That is the intended fail-fast behavior per stabilize-runtime-hygiene.
# Test suites that don't mount a .env must set these env vars in their
# fixture/conftest, not paper over the failure here.
settings = ZovarkSettings()

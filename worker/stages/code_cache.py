"""
Investigation Code Cache — skip LLM for repeat alert patterns.
Key = hash(tenant_id + task_type + rule_name + sorted SIEM field names).
NOT based on field values — same code works for different IPs/users.
TTL: 24 hours + jitter to avoid thundering herds on cache bulk-expiry.
Flush after prompt updates via scripts/flush_code_cache.sh.

Audit 2.10: tenant_id is a mandatory prefix on every cache signature and the
Redis key namespace. A call without tenant_id raises immediately — tenant
isolation cannot depend on call-site discipline.
Audit 2.11: TTL includes a random jitter of 0–3600 seconds per entry so the
FAST model isn't stampeded at 24-hour boundaries.
"""
import hashlib
import logging
import os
import random
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_PREFIX = "zovark:code_cache:"
CACHE_TTL = int(os.getenv("ZOVARK_CODE_CACHE_TTL", "86400"))
CACHE_TTL_JITTER = int(os.getenv("ZOVARK_CODE_CACHE_TTL_JITTER", "3600"))


def _require_tenant(tenant_id: str) -> str:
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("code_cache: tenant_id is required and must be a non-empty string")
    return tenant_id


def get_alert_signature(tenant_id: str, task_type: str, rule_name: str, siem_event: dict) -> str:
    _require_tenant(tenant_id)
    field_names = sorted(k for k in (siem_event or {}).keys() if not k.startswith("_"))
    sig = f"{tenant_id}:{task_type}:{rule_name}:{','.join(field_names)}"
    return hashlib.sha256(sig.encode()).hexdigest()[:24]


def _cache_key(tenant_id: str, signature: str) -> str:
    _require_tenant(tenant_id)
    return f"{CACHE_PREFIX}{tenant_id}:{signature}"


def get_cached_code(redis_client, tenant_id: str, signature: str) -> Optional[str]:
    try:
        key = _cache_key(tenant_id, signature)
        cached = redis_client.get(key)
        if cached:
            logger.info(f"Code cache HIT: {tenant_id}:{signature}")
            try:
                redis_client.incr("zovark:cache_hits")
            except Exception:
                pass
            return cached.decode("utf-8") if isinstance(cached, bytes) else cached
        try:
            redis_client.incr("zovark:cache_misses")
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning(f"Code cache read error: {e}")
        return None


def set_cached_code(redis_client, tenant_id: str, signature: str, code: str) -> bool:
    try:
        if not code or len(code.strip()) < 50:
            return False
        key = _cache_key(tenant_id, signature)
        ttl = CACHE_TTL + random.randint(0, max(0, CACHE_TTL_JITTER))
        redis_client.setex(key, ttl, code)
        logger.info(
            f"Code cache SET: {tenant_id}:{signature} ({len(code)} chars, TTL={ttl}s)"
        )
        return True
    except Exception as e:
        logger.warning(f"Code cache write error: {e}")
        return False

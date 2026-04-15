"""
Circuit Breaker — Auto-degrades investigation paths during alert storms.

3 levels:
  GREEN:  Normal operation. All paths available.
  YELLOW: Queue > 50. Low/info/medium severity → template-only (skip Path B/C).
  RED:    Queue > 100. Only critical severity gets Path B/C.

Recovers when queue drops below 25 (hysteresis prevents flapping).

Audit 2.14: state is persisted in Redis so all 32 worker processes observe
the same breaker state. Previously the state lived in module-level globals
with no lock and no cross-process sharing — two concurrent workers could
see divergent GREEN/RED values and one could re-trip the threshold a
second time after another had already tripped it. The in-process cache is
still kept to avoid a Redis round-trip on every `should_force_template_only`
call; TTL is 1 second.
"""
import os
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

YELLOW_THRESHOLD = int(os.getenv("ZOVARK_CB_YELLOW", "50"))
RED_THRESHOLD = int(os.getenv("ZOVARK_CB_RED", "100"))
RECOVERY_THRESHOLD = int(os.getenv("ZOVARK_CB_RECOVERY", "25"))

_STATE_KEY = "zovark:cb:state"
_CHANGED_AT_KEY = "zovark:cb:state_changed_at"
_PENDING_KEY = "zovark:cb:last_pending"

# Atomic transition script. Idempotent: rewrites the state key only when the
# new state differs from the stored state, so every call yields the same
# Redis state regardless of caller ordering.
_CB_LUA = """
local state_key = KEYS[1]
local changed_at_key = KEYS[2]
local pending_key = KEYS[3]
local new_state = ARGV[1]
local now = ARGV[2]
local pending = ARGV[3]

redis.call('SET', pending_key, pending)

local old_state = redis.call('GET', state_key)
if old_state == false then
    old_state = 'GREEN'
end
if old_state ~= new_state then
    redis.call('SET', state_key, new_state)
    redis.call('SET', changed_at_key, now)
end
return old_state
"""

# Process-local read-through cache — 1 second TTL — and lock to serialize
# in-process writers so two workflow activities in the same process don't
# race each other.
_cache_lock = threading.Lock()
_cache_state = "GREEN"
_cache_state_changed_at = time.time()
_cache_last_fetch = 0.0
_CACHE_TTL_SEC = 1.0


def _redis_client():
    """Lazy-resolve the shared redis client so importing this module doesn't
    require Redis to be up (e.g. under pytest)."""
    try:
        from stages.ingest import _redis_client as r
        if r is not None:
            return r
    except Exception:
        pass
    try:
        import redis as redislib
        from settings import settings as _s
        return redislib.from_url(_s.redis_url)
    except Exception:
        return None


def _load_from_redis() -> None:
    """Populate the process-local cache from Redis. Silent on Redis failure —
    the caller continues with whatever stale value is cached."""
    global _cache_state, _cache_state_changed_at, _cache_last_fetch
    r = _redis_client()
    if r is None:
        return
    try:
        state = r.get(_STATE_KEY)
        changed_at = r.get(_CHANGED_AT_KEY)
        if state is not None:
            if isinstance(state, bytes):
                state = state.decode("utf-8", errors="replace")
            _cache_state = state
        if changed_at is not None:
            if isinstance(changed_at, bytes):
                changed_at = changed_at.decode("utf-8", errors="replace")
            try:
                _cache_state_changed_at = float(changed_at)
            except (TypeError, ValueError):
                pass
        _cache_last_fetch = time.monotonic()
    except Exception as e:  # pragma: no cover
        logger.debug("circuit_breaker: redis read failed: %s", e)


def get_state() -> str:
    with _cache_lock:
        if time.monotonic() - _cache_last_fetch > _CACHE_TTL_SEC:
            _load_from_redis()
        return _cache_state


def update_state(pending_count: int) -> str:
    """Compute the new state from the pending count and persist it atomically
    to Redis. Returns the *new* state (after the transition)."""
    global _cache_state, _cache_state_changed_at, _cache_last_fetch

    if pending_count >= RED_THRESHOLD:
        new_state = "RED"
    elif pending_count >= YELLOW_THRESHOLD:
        new_state = "YELLOW"
    elif pending_count <= RECOVERY_THRESHOLD:
        new_state = "GREEN"
    else:
        new_state = get_state()  # within the deadband — leave it alone

    with _cache_lock:
        r = _redis_client()
        old_state = _cache_state
        if r is not None:
            try:
                result = r.eval(
                    _CB_LUA,
                    3,
                    _STATE_KEY, _CHANGED_AT_KEY, _PENDING_KEY,
                    new_state,
                    str(time.time()),
                    str(int(pending_count)),
                )
                if isinstance(result, bytes):
                    result = result.decode("utf-8", errors="replace")
                if result:
                    old_state = result
            except Exception as e:  # pragma: no cover
                logger.debug("circuit_breaker: redis eval failed: %s", e)

        if old_state != new_state:
            logger.warning(
                f"Circuit breaker: {old_state} → {new_state} (pending={pending_count})"
            )
            _cache_state_changed_at = time.time()
        _cache_state = new_state
        _cache_last_fetch = time.monotonic()
        return new_state


def should_force_template_only(severity: str, state: Optional[str] = None) -> bool:
    s = state or get_state()
    if s == "GREEN":
        return False
    elif s == "YELLOW":
        return severity.lower() in ("low", "info", "medium")
    elif s == "RED":
        return severity.lower() != "critical"
    return False


def get_status_dict() -> dict:
    return {
        "state": get_state(),
        "state_since": _cache_state_changed_at,
        "thresholds": {
            "yellow": YELLOW_THRESHOLD,
            "red": RED_THRESHOLD,
            "recovery": RECOVERY_THRESHOLD,
        },
    }

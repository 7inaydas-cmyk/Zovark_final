"""
Singleton LLM client with dual-role concurrency control.

Provides a shared httpx.AsyncClient and split semaphores for FAST and CODE roles:
  - _fast_semaphore(1): tool selection (FAST model, short requests)
  - _code_semaphore(1): assessment (CODE model, longer reasoning)

On dev tier (single container, --parallel 2): prevents GPU contention.
On customer tier (dual containers): each semaphore maps to a different GPU.

Path A investigations (no LLM) bypass this entirely.
"""
import asyncio
import os
import re
import time
import logging

import httpx

logger = logging.getLogger(__name__)

try:
    from settings import settings as _settings
    _CONTEXT_BUDGET = int(getattr(_settings, "context_token_budget", 12000))
except ImportError:
    _settings = None
    _CONTEXT_BUDGET = int(os.environ.get("ZOVARK_CONTEXT_TOKEN_BUDGET", "12000"))


def _llm_provider() -> str:
    """local = llama.cpp / Ollama-style; openai = OpenAI Chat Completions API."""
    if _settings is not None:
        return str(getattr(_settings, "llm_provider", "local") or "local").lower().strip()
    return os.environ.get("ZOVARK_LLM_PROVIDER", "local").lower().strip()


def _default_chat_endpoint() -> str:
    if _settings is not None:
        ep = getattr(_settings, "llm_endpoint", None)
        if ep:
            return str(ep)
    return os.environ.get(
        "ZOVARK_LLM_ENDPOINT", "https://api.openai.com/v1/chat/completions"
    )


def chat_endpoint_for_model(model_name: str) -> str:
    """Resolve FAST vs CODE chat URL from env/settings (same logic as stages.llm_gateway)."""
    default = _default_chat_endpoint()
    fast_m = ""
    if _settings is not None:
        fast_m = str(getattr(_settings, "llm_fast_model", "") or "").strip()
    if not fast_m:
        fast_m = os.environ.get("ZOVARK_MODEL_FAST", "").strip()
    ep_fast = os.environ.get("ZOVARK_LLM_ENDPOINT_FAST", "").strip() or default
    ep_code = os.environ.get("ZOVARK_LLM_ENDPOINT_CODE", "").strip() or default
    if model_name == fast_m:
        return ep_fast
    return ep_code


def resolve_llm_api_key(explicit: str | None = None) -> str:
    """Bearer token for chat completions. OpenAI: prefer openai_api_key / OPENAI_API_KEY, then legacy keys."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    prov = _llm_provider()
    if prov == "openai":
        if _settings is not None:
            oa = getattr(_settings, "openai_api_key", None)
            if oa is not None:
                v = oa.get_secret_value().strip()
                if v:
                    return v
        for env_k in ("OPENAI_API_KEY", "ZOVARK_OPENAI_API_KEY"):
            v = os.environ.get(env_k, "").strip()
            if v:
                return v
    if _settings is not None:
        lk = str(getattr(_settings, "llm_key", "") or "").strip()
        if lk:
            return lk
    return os.environ.get("ZOVARK_LLM_KEY", "").strip()


def sync_llm_chat(**kwargs) -> dict:
    """Run llm_request from synchronous code (no nested event loop)."""
    return asyncio.run(llm_request(**kwargs))

_client: httpx.AsyncClient | None = None
# AsyncClient must not be reused across different event loops (e.g. repeated asyncio.run in tests).
_client_loop_id: int | None = None

# Audit 2.8: semaphores MUST be bound to the running event loop, not a module-
# import-time loop. Eagerly constructing `asyncio.Semaphore(1)` at import time
# binds to whatever loop is current during import — which on Temporal worker
# startup is NOT the loop that eventually runs the activity. Python 3.10+
# raises "got Future attached to a different loop" in that case. Fix: lazy
# construction inside llm_request(), rebound whenever the running loop changes.
_fast_semaphore: asyncio.Semaphore | None = None
_code_semaphore: asyncio.Semaphore | None = None
_semaphore_loop_id: int | None = None


def _get_role_semaphore(role: str) -> asyncio.Semaphore:
    """Return the FAST/CODE semaphore bound to the current running loop."""
    global _fast_semaphore, _code_semaphore, _semaphore_loop_id
    cur_id: int | None = None
    try:
        cur_id = id(asyncio.get_running_loop())
    except RuntimeError:
        cur_id = None
    if (
        _fast_semaphore is None
        or _code_semaphore is None
        or (cur_id is not None and _semaphore_loop_id != cur_id)
    ):
        _fast_semaphore = asyncio.Semaphore(1)
        _code_semaphore = asyncio.Semaphore(1)
        _semaphore_loop_id = cur_id
    return _code_semaphore if role in ("verdict", "summary") else _fast_semaphore

# Per-role sampling configs (Agent 4)
SAMPLING_CONFIGS = {
    "param_fill":  {"temperature": 0.0, "top_p": 1.0, "top_k": 1},
    "tool_select": {"temperature": 0.1, "top_p": 0.9, "top_k": 40},
    "verdict":     {"temperature": 0.1, "top_p": 0.9, "top_k": 40},
    "summary":     {"temperature": 0.3, "top_p": 0.95, "top_k": 50},
}

# Grammar cache (Agent 3)
_GRAMMAR_DIR = os.path.join(os.path.dirname(__file__), "grammars")
_grammar_cache: dict[str, str | None] = {}


def _load_grammar(name: str) -> str | None:
    """Load a GBNF grammar file. Returns None if not found."""
    if name not in _grammar_cache:
        path = os.path.join(_GRAMMAR_DIR, f"{name}.gbnf")
        try:
            with open(path) as f:
                _grammar_cache[name] = f.read()
        except FileNotFoundError:
            _grammar_cache[name] = None
    return _grammar_cache[name]


# --- LLM output sanitizer (model-agnostic, defense in depth) ---
# Strips control tokens that may leak into grammar-constrained output.
# Covers Gemma 4 thinking blocks, tool call corruption (llama.cpp #21316),
# and turn markers. No-op on models that don't emit these tokens.
_CONTROL_TOKEN_RE = re.compile(
    r'<\|channel>.*?<channel\|>'   # Gemma 4 thinking blocks
    r'|<\|think\|>'                # thinking trigger token
    r'|\[<\|"\|>\]'               # tool call corruption (issue #21316)
    r'|<\|turn\|>'                 # turn markers that leak
    r'|<\|"\|>',                   # quote token corruption
    re.DOTALL
)


def _approx_message_tokens(messages: list) -> int:
    """Rough token estimate (chars/4) for budgeting."""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += max(1, len(c) // 4)
    return n


def _shrink_delimited_block(content: str, start_m: str, end_m: str, trim_log: list) -> tuple[str, bool]:
    """Remove one trailing line from inside a marked section. Returns (new_content, changed)."""
    if not isinstance(content, str) or start_m not in content or end_m not in content:
        return content, False
    si = content.index(start_m) + len(start_m)
    ei = content.index(end_m, si)
    inner = content[si:ei]
    lines = inner.strip("\n").split("\n")
    if len(lines) <= 1:
        return content, False
    removed = lines[-1]
    new_inner = "\n".join(lines[:-1])
    trim_log.append(
        {
            "marker_pair": f"{start_m[:24]}…",
            "removed_line_chars": len(removed),
        }
    )
    return content[:si] + "\n" + new_inner + "\n" + content[ei:], True


def _trim_tail_lines_non_system(msgs: list, trim_log: list) -> bool:
    """Drop one trailing line from the last non-system message (after all marked sections shrunk)."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "system":
            continue
        c = msgs[i].get("content")
        if not isinstance(c, str) or len(c) < 120:
            return False
        lines = c.split("\n")
        if len(lines) <= 2:
            return False
        removed = lines.pop()
        trim_log.append({"section": "user_tail_line", "removed_line_chars": len(removed)})
        msgs[i]["content"] = "\n".join(lines)
        return True
    return False


def _truncate_messages_for_budget(messages: list, budget: int) -> list:
    """Section-aware trim: never modify system/instruction messages.

    Order: shrink correlation marked block, then institutional block, then generic user tail lines.
    """
    try:
        from context_markers import (
            CORRELATION_END,
            CORRELATION_START,
            INSTITUTIONAL_END,
            INSTITUTIONAL_START,
        )
    except ImportError:
        CORRELATION_START = "<<<ZOVARK_CORRELATION_CONTEXT>>>"
        CORRELATION_END = "<<</ZOVARK_CORRELATION_CONTEXT>>>"
        INSTITUTIONAL_START = "<<<ZOVARK_INSTITUTIONAL_KNOWLEDGE>>>"
        INSTITUTIONAL_END = "<<</ZOVARK_INSTITUTIONAL_KNOWLEDGE>>>"

    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]
    before = _approx_message_tokens(msgs)
    if before <= budget:
        return msgs

    trim_log: list = []
    meta = {
        "approx_tokens_before": before,
        "budget": budget,
        "sections_trimmed": trim_log,
        "strategy": "correlation_then_institutional_then_user_tail",
    }

    safety = 0
    while _approx_message_tokens(msgs) > budget and safety < 600:
        safety += 1
        progressed = False
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                continue
            c = m.get("content", "")
            if not isinstance(c, str):
                continue
            nc, ch = _shrink_delimited_block(c, CORRELATION_START, CORRELATION_END, trim_log)
            if ch:
                msgs[i]["content"] = nc
                progressed = True
                break
        if progressed:
            continue
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                continue
            c = m.get("content", "")
            if not isinstance(c, str):
                continue
            nc, ch = _shrink_delimited_block(c, INSTITUTIONAL_START, INSTITUTIONAL_END, trim_log)
            if ch:
                msgs[i]["content"] = nc
                progressed = True
                break
        if progressed:
            continue
        if _trim_tail_lines_non_system(msgs, trim_log):
            continue
        break

    meta["approx_tokens_after"] = _approx_message_tokens(msgs)
    meta["trim_rounds"] = safety
    logger.warning(
        "LLM context over token budget — section-aware truncation applied",
        extra={"zovark_context_trim": meta},
    )
    return msgs


def _sanitize_llm_output(text: str) -> str:
    """Strip control tokens that may leak into grammar-constrained output."""
    cleaned = _CONTROL_TOKEN_RE.sub('', text).strip()
    if cleaned != text:
        logger.warning(
            "LLM output contained control tokens — sanitized",
            extra={"original_len": len(text), "cleaned_len": len(cleaned)},
        )
    return cleaned


def get_client() -> httpx.AsyncClient:
    """Get or create the singleton httpx.AsyncClient (no base_url — chat URL is absolute)."""
    global _client, _client_loop_id
    cur_id: int | None = None
    try:
        cur_id = id(asyncio.get_running_loop())
    except RuntimeError:
        pass
    if cur_id is not None:
        if _client_loop_id is not None and _client_loop_id != cur_id:
            _client = None
        _client_loop_id = cur_id
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=30.0 if _llm_provider() == "openai" else 5.0,
                read=120.0,
                write=30.0 if _llm_provider() == "openai" else 5.0,
                pool=10.0,
            ),
        )
    return _client


async def llm_request(
    model: str,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    stage: str = "unknown",
    response_format: dict | None = None,
    role: str = "tool_select",
    grammar_name: str | None = None,
    endpoint_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Make an LLM request through semaphore-controlled singleton client.

    Args:
        role: LLM role — "param_fill", "tool_select", "verdict", "summary".
              Controls which semaphore (fast vs code) and sampling config.
        grammar_name: GBNF grammar file name (without .gbnf). None = no grammar.

    Returns the raw response JSON from the /v1/chat/completions endpoint.

    endpoint_url: full URL (e.g. https://api.openai.com/v1/chat/completions).
    api_key: Bearer token; defaults to ZOVARK_LLM_KEY / settings.
    """
    provider = _llm_provider()
    is_openai = provider == "openai"
    chat_url = (endpoint_url or "").strip() or _default_chat_endpoint()
    auth_key = resolve_llm_api_key(api_key)

    # Select semaphore based on role (lazy, loop-bound — audit 2.8)
    sem = _get_role_semaphore(role)

    # Merge role-based sampling config
    sampling = SAMPLING_CONFIGS.get(role, SAMPLING_CONFIGS["tool_select"])

    async with sem:
        client = get_client()
        start = time.perf_counter()
        budget = _CONTEXT_BUDGET
        if _settings is not None:
            budget = int(getattr(_settings, "context_token_budget", budget))
        work_messages = _truncate_messages_for_budget(messages, budget)
        eff_temp = temperature if temperature != 0.1 else sampling["temperature"]
        body: dict = {
            "model": model,
            "messages": work_messages,
            "temperature": eff_temp,
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        grammar_text = None
        if not is_openai:
            body["keep_alive"] = "30m"
            if sampling.get("top_k"):
                body["top_k"] = sampling["top_k"]
            if response_format:
                body["response_format"] = response_format
            gf = os.environ.get("ZOVARK_GRAMMAR_FILE", "").strip()
            if not gf and _settings is not None:
                gf = (getattr(_settings, "grammar_file", None) or "").strip()
            if gf:
                try:
                    with open(gf, encoding="utf-8") as gfobj:
                        grammar_text = gfobj.read()
                except OSError as e:
                    logger.warning("ZOVARK_GRAMMAR_FILE unreadable: %s", e)
            elif grammar_name:
                grammar_text = _load_grammar(grammar_name)
            if grammar_text:
                body["grammar"] = grammar_text
                body.pop("response_format", None)
        else:
            # OpenAI Chat Completions: no grammar / keep_alive / top_k
            if response_format:
                body["response_format"] = response_format

        # OTEL span
        _span = None
        try:
            from tracing import get_tracer
            _span = get_tracer().start_span("llm.call")
            _span.set_attribute("llm.model", model)
            _span.set_attribute("llm.stage", stage)
            _span.set_attribute("llm.role", role)
            _span.set_attribute("llm.grammar_used", bool(grammar_text))
        except Exception:
            pass

        try:
            response = await client.post(chat_url, json=body, headers=headers)
            response.raise_for_status()
            duration = round(time.perf_counter() - start, 2)

            result = response.json()
            usage = result.get("usage", {})
            tokens_in = usage.get("prompt_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0)

            # Sanitize control tokens before they reach JSON parsing / Pydantic
            raw_content = result["choices"][0]["message"]["content"]
            sanitized = _sanitize_llm_output(raw_content)
            result["choices"][0]["message"]["content"] = sanitized

            logger.info(f"LLM {model} [{stage}/{role}] {duration}s tokens={tokens_in}/{tokens_out}")

            if _span:
                try:
                    _span.set_attribute("llm.tokens_in", tokens_in)
                    _span.set_attribute("llm.tokens_out", tokens_out)
                    _span.set_attribute("llm.e2e_ms", int(duration * 1000))
                    _span.set_attribute("llm.success", True)
                    _span.end()
                except Exception:
                    pass

            try:
                from metrics import record_llm_call
                record_llm_call(duration, True, tokens_in, tokens_out)
            except Exception:
                pass

            return result

        except httpx.TimeoutException as e:
            duration = round(time.perf_counter() - start, 2)
            logger.error(f"LLM {model} [{stage}/{role}] timed out after {duration}s")
            if _span:
                try:
                    _span.set_attribute("llm.success", False)
                    _span.set_attribute("llm.e2e_ms", int(duration * 1000))
                    _span.record_exception(e)
                    _span.end()
                except Exception:
                    pass
            try:
                from metrics import record_llm_call
                record_llm_call(duration, False, 0, 0)
            except Exception:
                pass
            raise

        except httpx.HTTPStatusError as e:
            duration = round(time.perf_counter() - start, 2)
            logger.error(f"LLM {model} [{stage}/{role}] returned {e.response.status_code}")
            if _span:
                try:
                    _span.set_attribute("llm.success", False)
                    _span.record_exception(e)
                    _span.end()
                except Exception:
                    pass
            try:
                from metrics import record_llm_call
                record_llm_call(duration, False, 0, 0)
            except Exception:
                pass
            raise


async def close_client():
    """Close the singleton client (call on shutdown)."""
    global _client, _client_loop_id
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
    _client_loop_id = None

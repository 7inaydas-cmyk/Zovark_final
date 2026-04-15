import os
import json
import time
import logging
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from temporalio import activity
import subprocess
import sys
import re

logger = logging.getLogger(__name__)

from llm_logger import log_llm_call
from prompt_registry import get_version
from model_config import get_tier_config
from validation.dry_run import DryRunValidator
from security.prompt_sanitizer import wrap_untrusted_data
from security.alert_sanitizer import sanitize_alert
from security.adversarial_review import review_code
from database.pool_manager import _pools

# Prompt v2: modular assembler with IOC extraction, retry loop, specialist personas
# (dpo/ mounted at /app/dpo via docker-compose volume)
try:
    from dpo.prompts_v2 import PromptAssembler, TECHNIQUE_IOC_MAP, should_retry, generate_retry_hints
    _rag_available = True
except ImportError:
    _rag_available = False
    TECHNIQUE_IOC_MAP = {}
    logger.warning("dpo.prompts_v2 not available — v2 prompt assembler disabled")

# Add the /app level to path so we can import sandbox.ast_prefilter
sys.path.append("/app")
from sandbox.ast_prefilter import is_safe_python_code


def _get_worker_id():
    """Get worker_id from main module (lazy import to avoid circular)."""
    try:
        from main import WORKER_ID
        return WORKER_ID
    except Exception:
        return None


def get_db_connection(tier="normal"):
    """Get a connection from the tiered pool (falls back to direct connect)."""
    pool = _pools.get(tier) or _pools.get("normal")
    if pool is not None:
        return pool.getconn()
    db_url = os.environ.get("DATABASE_URL", "postgresql://zovark:zovark_dev_2026@postgres:5432/zovark")
    return psycopg2.connect(db_url)


def _return_connection(conn, tier="normal"):
    """Return a connection to its pool (or close if no pool)."""
    pool = _pools.get(tier) or _pools.get("normal")
    if pool is not None:
        try:
            pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    else:
        try:
            conn.close()
        except Exception:
            pass


def _get_llm_key() -> str:
    """FIX #18: get LLM key from settings (SecretStr) with env var override."""
    try:
        from settings import settings as _s
        return os.environ.get("ZOVARK_LLM_KEY", _s.llm_key.get_secret_value())
    except Exception:
        return os.environ.get("ZOVARK_LLM_KEY", "")


def _sync_commit(cur):
    """Enable synchronous commit for this transaction (critical writes)."""
    cur.execute("SET LOCAL synchronous_commit = on")


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def check_semantic_dedup_activity(alert: dict) -> dict:
    """Check if a semantically similar investigation already exists."""
    try:
        from dedup.stage3_semantic import check_semantic_dedup, store_fingerprint
        conn = get_db_connection()
        try:
            match = check_semantic_dedup(alert, conn)
            return {"match": match, "action": "similar" if match else "new"}
        finally:
            _return_connection(conn)
    except Exception as e:
        print(f"Semantic dedup failed non-fatally: {e}")
        return {"match": None, "action": "new"}


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def store_fingerprint_activity(data: dict) -> dict:
    """Store investigation fingerprint after successful completion."""
    try:
        from dedup.stage3_semantic import store_fingerprint
        conn = get_db_connection()
        try:
            store_fingerprint(data["alert"], data["task_id"], conn)
            return {"stored": True}
        finally:
            _return_connection(conn)
    except Exception as e:
        print(f"Store fingerprint failed non-fatally: {e}")
        return {"stored": False}


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def check_exact_dedup_activity(alert: dict) -> dict:
    """Stage 1: Exact hash dedup via Redis."""
    try:
        import os
        if os.environ.get('DEDUP_ENABLED', 'true').lower() != 'true':
            return {"match": None, "action": "new"}
        from dedup.stage1_exact import check_exact_dedup
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://:hydra-redis-dev-2026@redis:6379/0'))  # FIX #16
        match = check_exact_dedup(alert, r)
        return {"match": match, "action": "duplicate" if match else "new"}
    except Exception as e:
        print(f"Exact dedup failed non-fatally: {e}")
        return {"match": None, "action": "new"}


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def check_correlation_activity(alert: dict) -> dict:
    """Stage 2: Rule correlation sliding window via Redis."""
    try:
        import os
        if os.environ.get('DEDUP_ENABLED', 'true').lower() != 'true':
            return {"match": None, "action": "new"}
        from dedup.stage2_correlate import check_correlation, merge_alert
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://:hydra-redis-dev-2026@redis:6379/0'))  # FIX #16
        task_id, count = check_correlation(alert, r)
        if task_id:
            merge_alert(alert, task_id, r)
            return {"match": task_id, "action": "correlated", "count": count}
        return {"match": None, "action": "new"}
    except Exception as e:
        print(f"Correlation dedup failed non-fatally: {e}")
        return {"match": None, "action": "new"}


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def register_dedup_activity(data: dict) -> dict:
    """Register alert in all dedup layers after spawning new investigation."""
    try:
        import os, redis
        alert = data["alert"]
        task_id = data["task_id"]
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://:hydra-redis-dev-2026@redis:6379/0'))  # FIX #16
        from dedup.stage1_exact import register_alert
        from dedup.stage2_correlate import register_correlation
        register_alert(alert, task_id, r)
        register_correlation(alert, task_id, r)
        return {"registered": True}
    except Exception as e:
        print(f"Register dedup failed non-fatally: {e}")
        return {"registered": False}


@activity.defn
async def fetch_task(task_id: str) -> dict:
    """Fetch task from DB with retry loop to handle API commit race condition.

    The Go API starts the Temporal workflow before the DB transaction commits.
    This retry loop waits for the row to appear (up to ~8 seconds).
    """
    import asyncio
    max_retries = 8
    delay = 1.0  # start at 1 second

    for attempt in range(max_retries):
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, tenant_id, task_type, input, status, trace_id, raw_input, dedup_hash FROM agent_tasks WHERE id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if row:
                    if attempt > 0:
                        activity.logger.info(f"Task {task_id} found on attempt {attempt + 1}")
                    row['id'] = str(row['id'])
                    row['tenant_id'] = str(row['tenant_id'])
                    out = dict(row)
                    if out.get('trace_id'):
                        out['trace_id'] = str(out['trace_id'])
                    return out
        finally:
            _return_connection(conn)

        activity.logger.warning(f"Task {task_id} not in DB yet (attempt {attempt + 1}/{max_retries}), waiting {delay:.1f}s...")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 3.0)  # 1.0, 1.5, 2.25, 3.0, 3.0, 3.0, 3.0, 3.0

    raise ValueError(f"Task {task_id} not found after {max_retries} retries (~8s)")


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def generate_code(task_data: dict) -> dict:
    from llm_client import llm_request, resolve_llm_api_key, chat_endpoint_for_model

    prompt = task_data.get("input", {}).get("prompt", "")
    task_type = task_data.get("task_type", "Log Analysis")
    log_data = task_data.get("input", {}).get("log_data", "")
    filename = task_data.get("input", {}).get("filename", "")

    # If this is a file upload investigation, use a different system prompt
    if log_data:
        base_system_prompt = (
            "You are a senior security analyst. Generate a self-contained Python script that analyzes the REAL log data provided below. "
            "The script MUST embed the provided log data in a multi-line string variable and analyze it directly. "
            "Do NOT use mock data — the real data is provided. Use ONLY the Python standard library. "
            "Do NOT use input(), subprocess, socket, requests, or any network calls. Print results as valid JSON to stdout. "
            "CRITICAL: The script runs in a read-only sandbox. Write files ONLY to /tmp/. "
            "REQUIRED JSON OUTPUT STRUCTURE: Your script MUST print perfectly valid JSON to stdout containing exactly these EXACT top-level keys: "
            "`findings` (array of objects with title and details), "
            "`statistics` (object with counts and metrics), "
            "`recommendations` (array of strings), "
            "`risk_score` (integer 0-100), "
            "`follow_up_needed` (boolean, true only if deeper analysis would be genuinely valuable), "
            "and `follow_up_prompt` (string describing what the next investigation step should do, or empty string if not needed). "
            "Examples of when follow_up is needed: log analysis finds suspicious IPs -> follow up with threat intel; "
            "code audit finds vulns -> follow up with remediation script; IOC scan finds matches -> follow up with timeline reconstruction."
        )
    else:
        base_system_prompt = (
            "You are a senior security analyst. Generate a self-contained Python script. "
            "The script MUST include realistic mock/sample data inline so it produces meaningful output "
            "when executed in an isolated sandbox with no network access. Use ONLY the Python standard library. "
            "Do NOT use input(), subprocess, socket, requests, or any network calls. Print results as valid JSON to stdout. "
            "CRITICAL RESTRICTIONS: 1. You are in a read-only container. Write files ONLY to /tmp/. 2. Do NOT try to read non-existent system logs like 'auth.log'. Hardcode mock logs inline. 3. STRICTLY FORBIDDEN: 'import requests'. Use 'urllib.request' instead. 4. STRICTLY FORBIDDEN: 'input()'. It will crash the non-interactive sandbox. "
            "REQUIRED JSON OUTPUT STRUCTURE: Your script MUST print perfectly valid JSON to stdout containing exactly these EXACT top-level keys: "
            "`findings` (array of objects with title and details), "
            "`statistics` (object with counts and metrics), "
            "`recommendations` (array of strings), "
            "`risk_score` (integer 0-100), "
            "`follow_up_needed` (boolean, true only if deeper analysis would be genuinely valuable), "
            "and `follow_up_prompt` (string describing what the next investigation step should do, or empty string if not needed). "
            "Examples of when follow_up is needed: log analysis finds suspicious IPs -> follow up with threat intel; "
            "code audit finds vulns -> follow up with remediation script; IOC scan finds matches -> follow up with timeline reconstruction."
        )

    type_specific_prompts = {
        "Log Analysis": " Focus on parsing common log formats. Mock the log data inline instead of reading from disk.",
        "Threat Hunt": " Focus on processing hardcoded IP lists against mock threat intelligence feeds. DO NOT import requests.",
        "Incident Response": " Focus on identifying timelines of breaches. DO NOT read local files like 'auth.log', mock the logs inline!",
        "Code Audit": " Focus on static analysis. DO NOT write the mock code to disk, analyze it from a multi-line string variable inline.",
        "IOC Scan": " Focus on scanning a mock filesystem or process list. Mock everything inline. Do NOT use input(). If you must write a report, write it to /tmp/",
    }

    # Also support lowercase snake_case variants just in case
    for key in list(type_specific_prompts.keys()):
        type_specific_prompts[key.lower().replace(" ", "_")] = type_specific_prompts[key]

    system_prompt = base_system_prompt + type_specific_prompts.get(task_type, type_specific_prompts.get("Log Analysis"))

    playbook_system_prompt_override = task_data.get("input", {}).get("playbook_system_prompt_override")
    if playbook_system_prompt_override:
        wrapped_override, _ = wrap_untrusted_data(playbook_system_prompt_override, "system_override")
        system_prompt += f"\n\nPLAYBOOK OVERRIDE INSTRUCTIONS:\n{wrapped_override}"

    # Build the user message
    siem_event = task_data.get("input", {}).get("siem_event")

    # Sanitize SIEM event before embedding or passing to LLM (prevents memory poisoning)
    if siem_event:
        siem_event = sanitize_alert(siem_event)

    if log_data:
        # Truncate log data if extremely large (safety net)
        if len(log_data) > 50000:
            log_data = log_data[:50000] + "\n... [truncated]"
        augmented_prompt = f"Here is the log data from file '{filename}' to analyze:\n\n{log_data}\n\nTask: {prompt}\n\nIMPORTANT: Embed this EXACT log data in your script as a multi-line string variable named LOG_DATA. Analyze it directly."
    elif siem_event:
        siem_context = json.dumps(siem_event, indent=2)
        # Wrap untrusted SIEM data with randomized delimiters (Security P0#10)
        wrapped_siem, siem_safety_instruction = wrap_untrusted_data(siem_context, "siem_alert")
        system_prompt += f"\n\n{siem_safety_instruction}"

        # Prompt v2: modular assembler with IOC patterns, specialist personas, objective recitation
        if _rag_available:
            assembler = PromptAssembler()
            technique = task_type.lower().replace(" ", "_")
            augmented_prompt = assembler.build_investigation_prompt(
                alert_json=wrapped_siem,
                skill_type=technique,
                skill_template="",  # no template in this code path
                rag_examples=[],    # pgvector retrieval comes later
            )
            augmented_prompt += f"\n\nTask: {prompt}"
        else:
            # Fallback to original prompt
            augmented_prompt = f"SIEM ALERT DATA:\n{wrapped_siem}\n\nTask: {prompt}\n\nIMPORTANT: Extract all IOCs."
    else:
        augmented_prompt = prompt + "\n\nCRITICAL CONSTRAINTS FOR THIS SCRIPT:\n1. You MUST define a multi-line string variable containing mock mock file data instead of trying to open files.\n2. You MUST define a hardcoded mock dictionary for any web request output instead of fetching it.\n3. You MUST use 'urllib.request' if you ever need networking.\n4. You MUST hardcode user choices instead of using an input function."

    # Model tiering
    tier_config = get_tier_config("generate_code")
    llm_model = tier_config["model"]
    prompt_name = "code_generation_with_logs" if log_data else "code_generation_mock"

    start_time = time.time()
    result = await llm_request(
        llm_model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": augmented_prompt},
        ],
        temperature=0.7,
        max_tokens=tier_config["max_tokens"],
        stage="legacy_generate_code",
        role="verdict",
        endpoint_url=chat_endpoint_for_model(llm_model),
        api_key=resolve_llm_api_key(None),
    )

    execution_ms = int((time.time() - start_time) * 1000)

    # Log LLM call
    usage = result.get("usage", {})
    log_llm_call(
        activity_name="generate_code",
        model_tier=tier_config["tier"],
        model_id=llm_model,
        prompt_name=prompt_name,
        prompt_version=get_version(prompt_name),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        latency_ms=execution_ms,
        temperature=0.7,
        max_tokens=tier_config["max_tokens"],
        tenant_id=task_data.get("tenant_id"),
        task_id=task_data.get("task_id"),
    )

    code = result["choices"][0]["message"]["content"].strip()
    if code.startswith("```python"):
        code = code[9:]
    if code.startswith("```"):
        code = code[3:]
    if code.endswith("```"):
        code = code[:-3]

    # Strip LLM special tokens that leak into generated code (deepseek-coder, llama)
    code = re.sub(r'<[｜|][^>]*[｜|]>', '', code)
    code = re.sub(r'<\|(?:im_start|im_end|endoftext|begin_of_sentence|end_of_sentence|fim_prefix|fim_middle|fim_suffix)\|>', '', code)

    # Post-generation code scrubber to remove catastrophic LLM hallucinations that crash the read-only, non-interactive sandbox
    code = code.replace("import requests", "import urllib.request as urllib2")
    code = code.replace("requests.get", "urllib2.urlopen")
    code = code.replace("input(", "print('Mocking input for: ' + ")

    # Safely redirect any strictly local open() calls to the writeable /tmp/ directory
    # using regex to avoid catching things like urlopen()
    code = re.sub(r'\bopen\([\'"](?!\/tmp\/)(.*?)[\'"]', r'open("/tmp/\1"', code)

    code = re.sub(r'\bopen\([\'"](?!\/tmp\/)(.*?)[\'"]', r'open("/tmp/\1"', code)

    mock_requests = """
class MockResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)
    def json(self):
        return self._json
    def raise_for_status(self): pass


class MockRequests:
    @staticmethod
    def get(*args, **kwargs): return MockResponse({"indicator": "malicious", "confidence": 99})
    @staticmethod
    def post(*args, **kwargs): return MockResponse({"status": "success"})

requests = MockRequests()
"""
    code = mock_requests + "\n" + code

    return {
        "code": code.strip(),
        "usage": result.get("usage", {}),
        "execution_ms": execution_ms
    }


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def validate_code(code: str) -> dict:
    safe, reason = is_safe_python_code(code)
    return {
        "is_safe": safe,
        "reason": reason
    }


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def preflight_validate_code(code: str) -> dict:
    """Preflight validation — runs in <100ms, no sandbox needed."""
    from validation.preflight import preflight_validate, auto_fix_code

    # Try auto-fix first
    fixed_code, fixes = auto_fix_code(code)
    if fixes:
        print(f"PREFLIGHT auto-fix: {fixes}")

    is_valid, error_or_cleaned, warnings = preflight_validate(fixed_code)

    if warnings:
        print(f"PREFLIGHT warnings: {warnings}")

    return {
        "valid": is_valid,
        "error": error_or_cleaned if not is_valid else "",
        "cleaned_code": fixed_code if is_valid else code,
        "fixes_applied": fixes,
        "warnings": warnings,
    }


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def save_investigation_pattern(data: dict) -> dict:
    """Save successful investigation pattern to memory table."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO investigation_memory
                (task_type, alert_signature, code_template, iocs_found,
                 findings_found, risk_score, success, error_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                data.get('task_type', ''),
                data.get('alert_signature', ''),
                data.get('code', '')[:10000],
                json.dumps(data.get('iocs', [])),
                json.dumps(data.get('findings', [])),
                data.get('risk_score', 0),
                data.get('success', True),
                data.get('error_type'),
            ))
        conn.commit()
        return {"saved": True}
    except Exception as e:
        print(f"Pattern save failed (non-fatal): {e}")
        return {"saved": False, "error": str(e)}
    finally:
        _return_connection(conn)


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def execute_code(code: str) -> dict:
    # Stage 1: Adversarial review — red-team LLM checks for sandbox escape attempts
    # Must run BEFORE AST prefilter (Stage 2) and Docker execution (Stage 3)
    # FAST_FILL: skip adversarial review (no LLM call)
    if os.environ.get('ZOVARK_FAST_FILL', '') == 'true':
        review_result = {"safe": True, "reason": "fast_fill_bypass", "review_ms": 0}
    else:
        review_result = review_code(code)
    if not review_result["safe"]:
        logger.warning(f"Adversarial review blocked code: {review_result['reason']}")
        return {
            "status": "failed",
            "stdout": "",
            "stderr": f"Code blocked by adversarial review: {review_result['reason']}",
            "execution_ms": review_result.get("review_ms", 0),
            "blocked_by": "adversarial_review",
        }

    # Stage 2 (AST prefilter) and Stage 3 (Docker sandbox) follow below
    seccomp_path = "/app/sandbox/seccomp_profile.json"

    cmd = [
        "docker", "run", "--rm", "-i", "--network=none", "--read-only",
        "--tmpfs", "/tmp:size=64m,noexec,nosuid", "--workdir", "/tmp",
        "--cpus=0.5", "--memory=512m", "--memory-swap=512m",
        "--pids-limit=64", "--cap-drop=ALL",
        "--user", "65534:65534",
        "--security-opt=no-new-privileges",
        "--security-opt", f"seccomp={seccomp_path}",
        "python:3.11-slim", "python"
    ]

    start_time = time.time()
    try:
        result = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=60)
        execution_ms = int((time.time() - start_time) * 1000)

        status = "completed" if result.returncode == 0 else "failed"
        return {
            "status": status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "execution_ms": execution_ms
        }
    except subprocess.TimeoutExpired:
        execution_ms = int((time.time() - start_time) * 1000)
        return {
            "status": "failed",
            "stdout": "",
            "stderr": "Execution timed out after 30 seconds",
            "execution_ms": execution_ms
        }


@activity.defn
async def update_task_status(task_update: dict) -> None:
    conn = get_db_connection()
    worker_id = _get_worker_id()

    # Human review threshold — flag low-confidence or failed investigations
    human_review_threshold = int(os.environ.get("ZOVARK_HUMAN_REVIEW_THRESHOLD", "60"))
    risk_score = 0
    code_success = task_update.get("status") == "completed"
    output = task_update.get("output", {})
    if isinstance(output, dict):
        risk_score = output.get("risk_score", 0) or 0
        if isinstance(output.get("stdout"), str):
            try:
                parsed = json.loads(output["stdout"])
                risk_score = parsed.get("risk_score", risk_score)
            except (json.JSONDecodeError, TypeError):
                pass

    needs_review = False
    review_reason = None
    if not code_success:
        needs_review = True
        review_reason = "Code execution failed"
    elif risk_score < human_review_threshold:
        needs_review = True
        review_reason = f"Risk score {risk_score} below threshold {human_review_threshold}"

    try:
        with conn.cursor() as cur:
            if task_update["status"] == "deduplicated":
                # Fast path for dedup — only update status + dedup metadata
                cur.execute("""
                    UPDATE agent_tasks
                    SET status = %s, dedup_reason = %s, existing_task_id = %s,
                        worker_id = COALESCE(%s, worker_id), completed_at = NOW()
                    WHERE id = %s
                """, (
                    task_update["status"],
                    task_update.get("dedup_reason"),
                    task_update.get("existing_task_id"),
                    worker_id,
                    task_update["task_id"]
                ))
            else:
                cur.execute("""
                    UPDATE agent_tasks
                    SET status = %s, output = %s, error_message = %s,
                        tokens_used_input = %s, tokens_used_output = %s, execution_ms = %s,
                        severity = %s, worker_id = COALESCE(%s, worker_id),
                        needs_human_review = %s, review_reason = %s,
                        completed_at = NOW()
                    WHERE id = %s
                """, (
                    task_update["status"],
                    json.dumps(task_update.get("output", {})),
                    task_update.get("error_message", None),
                    task_update.get("tokens_input", 0),
                    task_update.get("tokens_output", 0),
                    task_update.get("execution_ms", 0),
                    task_update.get("severity", None),
                    worker_id,
                    needs_review,
                    review_reason,
                    task_update["task_id"]
                ))
        conn.commit()
    finally:
        _return_connection(conn)


@activity.defn
async def log_audit(audit_data: dict) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _sync_commit(cur)
            cur.execute("""
                INSERT INTO agent_audit_log (tenant_id, action, resource_type, resource_id, details)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                audit_data["tenant_id"],
                audit_data["action"],
                audit_data["resource_type"],
                audit_data["resource_id"],
                json.dumps(audit_data.get("details", {}))
            ))
        conn.commit()
    finally:
        _return_connection(conn)


@activity.defn
async def log_audit_event(event_data: dict) -> None:
    """Insert structured audit event into audit_events table."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _sync_commit(cur)
            cur.execute("""
                INSERT INTO audit_events (tenant_id, event_type, actor_id, actor_type, resource_type, resource_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                event_data["tenant_id"],
                event_data["event_type"],
                event_data.get("actor_id"),
                event_data.get("actor_type", "worker"),
                event_data.get("resource_type"),
                event_data.get("resource_id"),
                json.dumps(event_data.get("metadata", {}))
            ))
        conn.commit()
    except Exception as e:
        print(f"log_audit_event non-fatal error: {e}")
    finally:
        _return_connection(conn)


@activity.defn
async def record_usage(usage_data: dict) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO usage_records (tenant_id, task_id, record_type, model_name, tokens_input, tokens_output, execution_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                usage_data["tenant_id"],
                usage_data["task_id"],
                usage_data["record_type"],
                usage_data["model_name"],
                usage_data.get("tokens_input", 0),
                usage_data.get("tokens_output", 0),
                usage_data.get("execution_ms", 0)
            ))
        conn.commit()
    finally:
        _return_connection(conn)


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def save_investigation_step(step_data: dict) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _sync_commit(cur)
            cur.execute("""
                INSERT INTO public.investigation_steps (task_id, step_number, step_type, summary_prompt, generated_code, output, status, tokens_used_input, tokens_used_output, execution_ms, execution_mode, parameters_used, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (task_id, step_number) DO UPDATE SET
                    generated_code = EXCLUDED.generated_code,
                    output = EXCLUDED.output,
                    status = EXCLUDED.status,
                    tokens_used_input = EXCLUDED.tokens_used_input,
                    tokens_used_output = EXCLUDED.tokens_used_output,
                    execution_ms = EXCLUDED.execution_ms,
                    execution_mode = EXCLUDED.execution_mode,
                    parameters_used = EXCLUDED.parameters_used,
                    completed_at = NOW()
            """, (
                step_data["task_id"],
                step_data["step_number"],
                step_data.get("step_type", "analysis"),
                step_data["prompt"],
                step_data.get("generated_code", ""),
                json.dumps(step_data.get("output")) if step_data.get("output") else None,
                step_data.get("status", "completed"),
                step_data.get("tokens_used_input", 0),
                step_data.get("tokens_used_output", 0),
                step_data.get("execution_ms", 0),
                step_data.get("execution_mode", "generated"),
                json.dumps(step_data.get("parameters_used", {})) if step_data.get("parameters_used") else None,
            ))
        conn.commit()
    finally:
        _return_connection(conn)


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def check_followup_needed(check_data: dict) -> dict:
    """Parse JSON output and determine if follow-up is needed. Activity for Temporal determinism."""
    stdout_str = check_data.get("stdout", "")
    previous_prompt = check_data.get("previous_prompt", "")
    try:
        parsed = json.loads(stdout_str)
        if not isinstance(parsed, dict):
            return {"needed": False, "prompt": ""}

        needed = parsed.get("follow_up_needed", False)
        follow_prompt = parsed.get("follow_up_prompt", "")

        # Guard: empty or duplicate prompt means no follow-up
        if not follow_prompt or not follow_prompt.strip():
            return {"needed": False, "prompt": ""}
        if follow_prompt.strip() == previous_prompt.strip():
            return {"needed": False, "prompt": ""}

        return {"needed": bool(needed), "prompt": follow_prompt}
    except Exception:
        return {"needed": False, "prompt": ""}


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def generate_followup_code(task_data: dict) -> dict:
    """Generate code for a follow-up step, including previous step context."""
    from llm_client import llm_request, resolve_llm_api_key, chat_endpoint_for_model

    prompt = task_data.get("prompt", "")
    previous_context = task_data.get("previous_context", "")
    task_data.get("task_type", "log_analysis")
    step_number = task_data.get("step_number", 2)

    step_type_label = "Enrichment" if step_number == 2 else "Deep Analysis"

    system_prompt = (
        f"You are a senior security analyst performing Step {step_number} ({step_type_label}) of a multi-step investigation. "
        "The previous investigation step produced findings that require deeper analysis. "
        "Generate a self-contained Python script that builds on the previous findings. "
        "The script MUST include the previous findings as inline data and perform the requested follow-up analysis. "
        "Use ONLY the Python standard library. Do NOT use input(), subprocess, socket, requests, or any network calls. "
        "CRITICAL: Read-only sandbox. Write files ONLY to /tmp/. "
        "REQUIRED JSON OUTPUT STRUCTURE: Your script MUST print perfectly valid JSON to stdout containing: "
        "`findings` (array of objects with title and details), "
        "`statistics` (object with counts and metrics), "
        "`recommendations` (array of strings), "
        "`risk_score` (integer 0-100), "
        "`follow_up_needed` (boolean), and `follow_up_prompt` (string, empty if not needed)."
    )

    playbook_system_prompt_override = task_data.get("playbook_system_prompt_override")
    if playbook_system_prompt_override:
        system_prompt += f"\n\nPLAYBOOK OVERRIDE INSTRUCTIONS:\n{playbook_system_prompt_override}"

    # Cap previous context to 2000 chars
    if len(previous_context) > 2000:
        previous_context = previous_context[:2000] + "\n... [truncated]"

    user_message = (
        f"PREVIOUS STEP RESULTS:\n{previous_context}\n\n"
        f"FOLLOW-UP TASK: {prompt}\n\n"
        "CRITICAL CONSTRAINTS: 1. Embed the previous findings as inline data. "
        "2. Do NOT use input(). 3. Do NOT import requests. 4. Write files only to /tmp/. "
        "5. Hardcode all data inline."
    )

    tier_config = get_tier_config("generate_followup_code")
    followup_model = tier_config["model"]

    start_time = time.time()
    result = await llm_request(
        followup_model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.7,
        max_tokens=tier_config["max_tokens"],
        stage="legacy_generate_followup_code",
        role="verdict",
        endpoint_url=chat_endpoint_for_model(followup_model),
        api_key=resolve_llm_api_key(None),
    )

    execution_ms = int((time.time() - start_time) * 1000)

    # Log LLM call
    usage = result.get("usage", {})
    log_llm_call(
        activity_name="generate_followup_code",
        model_tier=tier_config["tier"],
        model_id=followup_model,
        prompt_name="code_generation_followup",
        prompt_version=get_version("code_generation_followup"),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        latency_ms=execution_ms,
        temperature=0.7,
        max_tokens=tier_config["max_tokens"],
        tenant_id=task_data.get("tenant_id"),
        task_id=task_data.get("task_id"),
    )

    code = result["choices"][0]["message"]["content"].strip()
    if code.startswith("```python"):
        code = code[9:]
    if code.startswith("```"):
        code = code[3:]
    if code.endswith("```"):
        code = code[:-3]

    # Strip LLM special tokens that leak into generated code
    code = re.sub(r'<[｜|][^>]*[｜|]>', '', code)
    code = re.sub(r'<\|(?:im_start|im_end|endoftext|begin_of_sentence|end_of_sentence|fim_prefix|fim_middle|fim_suffix)\|>', '', code)

    # Post-generation code scrubber
    code = code.replace("import requests", "import urllib.request as urllib2")
    code = code.replace("requests.get", "urllib2.urlopen")
    code = code.replace("input(", "print('Mocking input for: ' + ")
    code = re.sub(r'\bopen\([\'"](?!\/tmp\/)(.*?)[\'"]', r'open("/tmp/\1"', code)

    mock_requests = """
class MockResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)
    def json(self):
        return self._json
    def raise_for_status(self): pass


class MockRequests:
    @staticmethod
    def get(*args, **kwargs): return MockResponse({"indicator": "malicious", "confidence": 99})
    @staticmethod
    def post(*args, **kwargs): return MockResponse({"status": "success"})

requests = MockRequests()
"""
    code = mock_requests + "\n" + code

    return {
        "code": code.strip(),
        "usage": result.get("usage", {}),
        "execution_ms": execution_ms
    }


@activity.defn
async def check_requires_approval(check_data: dict) -> dict:
    """Determine if a step requires human approval before execution."""
    task_type = check_data.get("task_type", "").lower().replace(" ", "_")
    risk_score = check_data.get("risk_score", 0)
    code = check_data.get("code", "")
    check_data.get("step_number", 1)

    # Rule 1: incident_response ALWAYS requires approval
    if task_type == "incident_response":
        return {
            "required": True,
            "reason": "Incident response scripts require manual approval before execution",
            "risk_level": "high"
        }

    # Rule 2: High risk score from previous step
    if risk_score >= 80:
        return {
            "required": True,
            "reason": f"Previous step produced high risk score ({risk_score}/100)",
            "risk_level": "critical"
        }

    # Rule 3: Dangerous code patterns
    dangerous_patterns = [
        ("open(", "'w'"),      # File write
        ("open(", "\"w\""),    # File write
        ('os.remove', ''),     # File deletion
        ('shutil.rmtree', ''),  # Directory deletion
        ('subprocess', ''),    # System commands
        ('os.system', ''),     # System commands
    ]

    for pattern, secondary in dangerous_patterns:
        if pattern in code and (not secondary or secondary in code):
            return {
                "required": True,
                "reason": f"Generated code contains potentially dangerous operations ({pattern})",
                "risk_level": "medium"
            }

    return {"required": False, "reason": "", "risk_level": "low"}


@activity.defn
async def create_approval_request(request_data: dict) -> str:
    """Create an approval request in the database. Returns the approval ID."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _sync_commit(cur)
            cur.execute("""
                INSERT INTO approval_requests (task_id, step_number, risk_level, action_summary, generated_code)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                request_data["task_id"],
                request_data["step_number"],
                request_data["risk_level"],
                request_data["action_summary"],
                request_data["generated_code"]
            ))
            approval_id = str(cur.fetchone()[0])
        conn.commit()
        return approval_id
    finally:
        _return_connection(conn)


@activity.defn
async def update_approval_request(update_data: dict) -> None:
    """Update an approval request with the decision."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _sync_commit(cur)
            cur.execute("""
                UPDATE approval_requests
                SET status = %s, decided_at = NOW(), decided_by = %s, decision_comment = %s
                WHERE id = %s
            """, (
                update_data["status"],
                update_data.get("decided_by"),
                update_data.get("comment", ""),
                update_data["approval_id"]
            ))
        conn.commit()
    finally:
        _return_connection(conn)


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def retrieve_skill(task_type: str, prompt: str) -> dict:
    tei_url = os.environ.get("TEI_URL", "http://embedding-server:80/embed")
    conn = get_db_connection()
    try:
        # 1. First try keyword matching (no embeddings needed)
        prompt_words = [w.lower() for w in prompt.split() if len(w) > 2]  # simple word split dropping very short words

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Match if task_type is in threat_types, OR any keyword from DB matches any word in prompt
            # Only return skills with code_template IS NOT NULL if we are matching for template mode (which we are now preferencing)
            # Actually, the requirement says "The skill must have code_template IS NOT NULL to be returned for template mode"
            # Since retrieve_skill is used generally, we'll order by has_template to prefer templates but fallback if needed,
            # or according to instructions "The skill must have code_template IS NOT NULL to be returned for template mode"
            # It's better to just require code_template IS NOT NULL for the keyword match if that's what's intended,
            # but let's just use the existing fields and prefer those with templates.
            # Actually, I will explicitly require `code_template IS NOT NULL` in the query to guarantee template mode.

            tt = task_type.lower().replace(" ", "_")

            # --- Priority 1: Exact threat_type match (task_type = ANY(threat_types)) ---
            cur.execute("""
                SELECT id, skill_name, skill_slug, investigation_methodology, detection_patterns,
                       mitre_techniques, follow_up_chain, embedding, code_template, parameters
                FROM agent_skills
                WHERE is_active = true AND code_template IS NOT NULL
                AND %s = ANY(threat_types)
                ORDER BY times_used DESC LIMIT 1
            """, (tt,))
            exact = cur.fetchone()

            # --- Priority 2: Prefix match (task_type starts with a threat_type or vice versa) ---
            if not exact:
                cur.execute("""
                    SELECT id, skill_name, skill_slug, investigation_methodology, detection_patterns,
                           mitre_techniques, follow_up_chain, embedding, code_template, parameters
                    FROM agent_skills
                    WHERE is_active = true AND code_template IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM unnest(threat_types) t
                        WHERE t LIKE %s || '%%' OR %s LIKE t || '%%'
                    )
                    ORDER BY times_used DESC LIMIT 1
                """, (tt, tt))
                exact = cur.fetchone()

            if exact:
                slug = exact.get('skill_slug', exact.get('skill_name', '?'))
                print(f"DEBUG retrieve_skill: exact threat_type match -> {slug}")
                cur.execute("UPDATE agent_skills SET times_used = times_used + 1 WHERE id = %s", (exact['id'],))
                conn.commit()
                return dict(exact)

            # --- Priority 3: Keyword phrase match (prompt must contain the keyword) ---
            prompt_lower = prompt.lower()
            cur.execute("""
                SELECT id, skill_name, skill_slug, investigation_methodology, detection_patterns,
                       mitre_techniques, follow_up_chain, embedding, code_template, parameters
                FROM agent_skills
                WHERE is_active = true AND code_template IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM unnest(keywords) k
                    WHERE %s LIKE '%%' || lower(k) || '%%'
                )
                ORDER BY times_used DESC
            """, (prompt_lower,))
            keyword_matches = cur.fetchall()

            if keyword_matches:
                slug = keyword_matches[0].get('skill_slug', keyword_matches[0].get('skill_name', '?'))
                print(f"DEBUG retrieve_skill: keyword match ({len(keyword_matches)} candidates) -> {slug}")
                best_match = keyword_matches[0]
                cur.execute("UPDATE agent_skills SET times_used = times_used + 1 WHERE id = %s", (best_match['id'],))
                conn.commit()
                return dict(best_match)

        # No keyword matches — fall through to Path B (LLM generation)
        print(f"DEBUG retrieve_skill: no match for task_type='{task_type}' -> Path B (LLM generation)")
        return None
    except Exception as e:
        print(f"Error in retrieve_skill: {e}")
        return None
    finally:
        _return_connection(conn)


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def fill_skill_parameters(data: dict) -> dict:
    import json
    import os
    import httpx
    import time

    start_time = time.time()

    skill_params = data.get("skill_params", [])
    prompt = data.get("prompt", "")
    log_data = data.get("log_data", "")
    siem_event = data.get("siem_event", {})

    # --- FAST FILL: bypass LLM for stress testing ---
    if os.environ.get('ZOVARK_FAST_FILL', '') == 'true':
        defaults = {p["name"]: p.get("default") for p in skill_params}
        filled = dict(defaults)
        if siem_event:
            field_map = {
                "log_data": siem_event.get("raw_log", ""),
                "raw_log": siem_event.get("raw_log", ""),
                "source_ip": siem_event.get("source_ip", "10.0.0.1"),
                "src_ip": siem_event.get("source_ip", "10.0.0.1"),
                "destination_ip": siem_event.get("destination_ip", "10.0.0.2"),
                "dst_ip": siem_event.get("destination_ip", "10.0.0.2"),
                "hostname": siem_event.get("hostname", "UNKNOWN-HOST"),
                "username": siem_event.get("username", "unknown_user"),
                "rule_name": siem_event.get("rule_name", ""),
                "title": siem_event.get("title", ""),
            }
            for k in filled:
                if k in field_map and field_map[k]:
                    filled[k] = field_map[k]
                elif k in siem_event:
                    filled[k] = siem_event[k]
            if "log_data" in filled and not filled["log_data"] and siem_event.get("raw_log"):
                filled["log_data"] = siem_event["raw_log"]
        return {
            "filled_parameters": filled,
            "execution_ms": int((time.time() - start_time) * 1000),
            "input_tokens": 0, "output_tokens": 0,
        }

    from llm_client import llm_request, resolve_llm_api_key, chat_endpoint_for_model

    tier_config = get_tier_config("fill_skill_parameters")
    model_name = tier_config["model"]

    # Sanitize SIEM event before embedding or passing to LLM (prevents memory poisoning)
    if siem_event:
        siem_event = sanitize_alert(siem_event)

    defaults = {p["name"]: p.get("default") for p in skill_params}

    try:
        sys_msg = (
            "You are an expert security parameter extractor. Extract parameter values for a Python detection script "
            "based strictly on the provided user prompt. Return ONLY a valid JSON object matching the requested schema. "
            "Do not include markdown blocks, explanations, or any other text. Follow the parameter types strictly."
        )

        user_msg = f"Available parameters and their types:\\n{json.dumps(skill_params, indent=2)}\\n\\nUser Prompt:\\n{prompt}\\n\\n"
        if siem_event:
            # Wrap untrusted SIEM data (Security P0#10)
            wrapped_siem_ctx, _ = wrap_untrusted_data(json.dumps(siem_event), "siem_alert")
            user_msg += f"Available SIEM Context:\\n{wrapped_siem_ctx}\\n\\n"
        user_msg += "Respond ONLY with a JSON object where keys are parameter names and values are the extracted values."

        resp_json = await llm_request(
            model_name,
            [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=tier_config["temperature"],
            max_tokens=tier_config["max_tokens"],
            stage="fill_skill_parameters",
            role="param_fill",
            response_format={"type": "json_object"},
            endpoint_url=chat_endpoint_for_model(model_name),
            api_key=resolve_llm_api_key(None),
        )
        usage = resp_json.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        fill_ms = int((time.time() - start_time) * 1000)
        log_llm_call(
            activity_name="fill_skill_parameters",
            model_tier=tier_config["tier"],
            model_id=model_name,
            prompt_name="parameter_extraction",
            prompt_version=get_version("parameter_extraction"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=fill_ms,
            temperature=tier_config["temperature"],
            max_tokens=tier_config["max_tokens"],
            tenant_id=data.get("tenant_id"),
            task_id=data.get("task_id"),
        )

        content = resp_json["choices"][0]["message"]["content"].strip()

        extracted = json.loads(content)

        for k, v in defaults.items():
            if k not in extracted or extracted[k] is None:
                extracted[k] = v

        if "log_data" in defaults and log_data:
            extracted["log_data"] = log_data

        return {
            "filled_parameters": extracted,
            "execution_ms": int((time.time() - start_time) * 1000),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }

    except Exception as e:
        print(f"Error in fill_skill_parameters: {e}")
        if "log_data" in defaults and log_data:
            defaults["log_data"] = log_data
        elif "log_data" in defaults and siem_event:
            # LLM parameter filling failed — inject SIEM event as log_data
            # so the skill template analyzes real alert data instead of mock
            raw_log = siem_event.get("raw_log", "")
            if raw_log:
                # Prepend key fields so the template can parse them
                header = (
                    f"# SIEM Alert: {siem_event.get('title', 'N/A')}\n"
                    f"# Source IP: {siem_event.get('source_ip', 'N/A')}\n"
                    f"# Dest IP: {siem_event.get('destination_ip', 'N/A')}\n"
                    f"# Hostname: {siem_event.get('hostname', 'N/A')}\n"
                    f"# Username: {siem_event.get('username', 'N/A')}\n"
                    f"# Rule: {siem_event.get('rule_name', 'N/A')}\n"
                )
                defaults["log_data"] = header + raw_log
            else:
                defaults["log_data"] = json.dumps(siem_event, indent=2)
        return {
            "filled_parameters": defaults,
            "execution_ms": int((time.time() - start_time) * 1000),
            "input_tokens": 0,
            "output_tokens": 0
        }


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def render_skill_template(data: dict) -> str:
    import json

    template = data.get("template", "")
    parameters = data.get("parameters", {})

    rendered = template
    for key, value in parameters.items():
        placeholder = f"{{{{{key}}}}}"

        if isinstance(value, str):
            # Safe replacement for multiline strings / logs
            val_str = value.replace('\\', '\\\\').replace("'''", "\\'\\'\\'")
            rendered = rendered.replace(placeholder, val_str)
        elif isinstance(value, (list, dict)):
            rendered = rendered.replace(placeholder, json.dumps(value))
        else:
            rendered = rendered.replace(placeholder, str(value))

    return rendered


@activity.defn
async def check_rate_limit_activity(data: dict) -> bool:
    """Acquire a lease for this task. Returns True if OK, False if rate limited.
    Reads max_concurrent from tenants table if not provided."""
    from rate_limiter import acquire_lease
    tenant_id = data["tenant_id"]
    task_id = data.get("task_id", "unknown")
    worker_id = _get_worker_id()
    max_concurrent = data.get("max_concurrent")
    if max_concurrent is None:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT max_concurrent FROM tenants WHERE id = %s", (tenant_id,))
            row = cur.fetchone()
            max_concurrent = row[0] if row and row[0] else 50
            cur.close()
            _return_connection(conn)
        except Exception:
            max_concurrent = 50
    return acquire_lease(tenant_id, task_id, worker_id, max_concurrent)


@activity.defn
async def decrement_active_activity(data: dict) -> None:
    """Release the lease for this task."""
    from rate_limiter import release_lease
    if isinstance(data, str):
        # Backwards compat: old callers pass tenant_id as string.
        # Inlined from the removed worker/redis_client.py::decrement_active.
        import redis
        from settings import settings
        r = redis.from_url(settings.redis_url, decode_responses=True)
        key = f"zovark:active:{data}"
        val = r.decr(key)
        if val < 0:
            r.set(key, 0)
            r.expire(key, 3600)
        return
    release_lease(data["tenant_id"], data["task_id"])


@activity.defn
async def heartbeat_lease_activity(data: dict) -> None:
    """Extend lease TTL. Called between long-running activities."""
    from rate_limiter import heartbeat_lease
    heartbeat_lease(data["tenant_id"], data["task_id"])


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def validate_generated_code(code: str) -> dict:
    """Dry-run validation gate. Runs BEFORE full sandbox execution."""
    validator = DryRunValidator(timeout=5)
    result = await validator.validate(code)

    if not result['passed']:
        import logging
        logging.getLogger(__name__).warning(f"Dry-run validation failed: {result['reason']}")

    return result


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def enrich_alert_with_memory(task_input: dict) -> dict:
    """Step 0: Check investigation memory before generating code."""
    try:
        from investigation_memory import InvestigationMemory
        raw_entities = _extract_iocs_from_input(task_input)
        if not raw_entities:
            return {'exact_matches': [], 'similar_entities': [], 'related_investigations': []}
        db_url = os.environ.get("DATABASE_URL", "postgresql://zovark:zovark_dev_2026@postgres:5432/zovark")
        memory = InvestigationMemory(db_url=db_url)
        return await memory.enrich_alert(raw_entities, task_input.get("tenant_id"))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Memory enrichment failed non-fatally: {e}")
        return {'exact_matches': [], 'similar_entities': [], 'related_investigations': []}


def _extract_iocs_from_input(task_input: dict) -> list:
    """Quick regex extraction of IOCs from alert input for memory lookup."""
    entities = []
    text = str(task_input)

    # IPs
    for ip in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text):
        if not ip.startswith(('0.', '127.', '255.')):
            entities.append({'type': 'ip', 'value': ip})

    # Domains
    for domain in re.findall(r'\b[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}\b', text):
        if '.' in domain and not domain[0].isdigit():
            entities.append({'type': 'domain', 'value': domain.lower()})

    # SHA256 hashes
    for h in re.findall(r'\b[a-fA-F0-9]{64}\b', text):
        entities.append({'type': 'file_hash', 'value': h.lower()})

    # MD5 hashes
    for h in re.findall(r'\b[a-fA-F0-9]{32}\b', text):
        entities.append({'type': 'file_hash', 'value': h.lower()})

    return entities


# FIX #21: @activity.defn removed — dead registration (not in main.py)
async def write_investigation_memory(memory_data: dict) -> None:
    try:
        from llm_client import llm_request, resolve_llm_api_key, chat_endpoint_for_model

        tei_url = os.environ.get("TEI_URL", "http://embedding-server:80/embed")
        tier_mem = get_tier_config("write_investigation_memory")
        mem_model = tier_mem["model"]

        task_id = memory_data.get("task_id")
        tenant_id = memory_data.get("tenant_id")
        skill_used_id = memory_data.get("skill_used_id")
        threat_type = memory_data.get("threat_type", "unknown")
        final_output = memory_data.get("final_output", {})

        findings = final_output.get("findings", [])[:5]
        iocs = final_output.get("key_iocs", {})
        risk_score = final_output.get("risk_score", 0)
        recommended = final_output.get("recommendations", [])

        # --- FAST FILL: skip LLM summarization for stress testing ---
        if os.environ.get('ZOVARK_FAST_FILL', '') == 'true':
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO investigation_memory
                        (task_type, alert_signature, iocs_found, findings_found, risk_score, success)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (threat_type, threat_type, json.dumps(iocs), json.dumps(findings), risk_score, True))
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                _return_connection(conn)
            return

        # Build synthesis prompt
        summary_prompt = (
            f"Synthesize this investigation into 2-3 sentences max. Threat: {threat_type}. "
            f"Findings: {json.dumps(findings)}. Risk: {risk_score}. Recommends: {json.dumps(recommended)}."
        )

        mem_result = await llm_request(
            mem_model,
            [
                {
                    "role": "system",
                    "content": "You are a succinct security analyst. Synthesize the JSON data into a short 2-3 sentence memory summary: 'Investigated [threat_type] alert. Found [N findings]. Risk score [X] because [reason]. Resolution: [recommended action].'",
                },
                {"role": "user", "content": summary_prompt},
            ],
            temperature=tier_mem["temperature"],
            max_tokens=min(512, tier_mem["max_tokens"]),
            stage="write_investigation_memory",
            role="summary",
            endpoint_url=chat_endpoint_for_model(mem_model),
            api_key=resolve_llm_api_key(None),
        )
        memory_summary = mem_result["choices"][0]["message"]["content"].strip()

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Embed
            embed_resp = await client.post(tei_url, json={"inputs": memory_summary})
            embed_resp.raise_for_status()
            embedding = embed_resp.json()[0]

        # Save to DB
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                _sync_commit(cur)
                cur.execute("""
                    INSERT INTO investigation_memory
                    (tenant_id, task_id, skill_used_id, threat_type, memory_summary, key_findings, key_iocs, risk_score, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                """, (
                    tenant_id, task_id, skill_used_id, threat_type, memory_summary,
                    json.dumps(findings), json.dumps(iocs), risk_score, embedding
                ))
            conn.commit()
        finally:
            _return_connection(conn)

    except Exception as e:
        print(f"Non-critical failure in write_investigation_memory: {e}")
        # Do not raise, this is fire-and-forget
        pass

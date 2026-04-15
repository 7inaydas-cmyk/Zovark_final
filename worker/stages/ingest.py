"""
Stage 1: INGEST — Validate, deduplicate, PII mask.
NO LLM calls. Only Redis + DB.

Self-contained: imports psycopg2, redis directly.
Does NOT import from _legacy_activities.py.
"""
import os
import re
import time
from typing import Optional
from dataclasses import asdict

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

try:
    from temporalio import activity
except ImportError:
    class _MockActivity:
        def defn(self, func):
            return func
        @property
        def logger(self):
            import logging
            return logging.getLogger("mock_activity")
    activity = _MockActivity()

from stages.trace_helpers import trace_investigation_pipeline, trace_stage_ingest_span
from stages import IngestOutput
from stages.input_sanitizer import sanitize_siem_event
from stages.smart_batcher import get_batcher

# --- Config ---
# stabilize-runtime-hygiene: centralized DB/Redis URL via settings; no fallback literals.
from settings import settings as _settings
DATABASE_URL = os.environ.get("DATABASE_URL", _settings.database_url)
REDIS_URL = os.environ.get("REDIS_URL", _settings.redis_url)
FAST_FILL = os.environ.get("ZOVARK_FAST_FILL", "false").lower() == "true"


# --- DB helper — uses ThreadedConnectionPool via pool_manager ---
# Audit 2.1: use a context manager so connections are ALWAYS returned to the
# ThreadedConnectionPool on both success and exception paths. The previous
# pattern (`_get_db()` + `conn.close()` in a bare `finally`) leaked connections
# whenever a caller forgot the `finally`, and the fallback direct-connect path
# never returned anything to the pool at all.
from contextlib import contextmanager

try:
    from database.pool_manager import pooled_connection as _pooled_connection
    _USE_POOL = True
except ImportError:
    _USE_POOL = False


@contextmanager
def _db_conn():
    """Yield a DB connection; return it to the pool (or close it) on exit."""
    if _USE_POOL:
        from database.pool_manager import _pools
        pool = _pools.get("normal") or _pools.get("critical")
        if pool is not None:
            conn = pool.getconn()
            try:
                yield conn
            finally:
                try:
                    pool.putconn(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return
    # Fallback: direct connect — no pool to return to.
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_db():
    """Legacy shim. Prefer `with _db_conn() as conn:` in new code."""
    if _USE_POOL:
        from database.pool_manager import _pools
        pool = _pools.get("normal") or _pools.get("critical")
        if pool is not None:
            return pool.getconn()
    return psycopg2.connect(DATABASE_URL)


# --- Inverted benign detection (recognize attacks, not benign) ---
ATTACK_INDICATORS = [
    "malware", "trojan", "ransomware", "exploit", "vulnerability",
    "injection", "overflow", "brute", "credential_dump", "mimikatz",
    "cobalt", "beacon", "exfiltration", "lateral", "escalation",
    "c2", "command_and_control", "phishing", "suspicious",
    "unauthorized", "anomal", "attack", "intrusion", "compromise",
    "kerberoast", "dcsync", "pass_the_hash", "pass_the_ticket",
    "golden_ticket", "lolbin", "process_injection", "dll_sideload",
    "persistence", "wmi_abuse", "credential_dumping", "rdp_tunnel",
    "dns_exfil", "powershell_obfusc", "office_macro", "webshell",
]


def _has_attack_indicators(task_type: str, rule_name: str, title: str) -> bool:
    """Check if any field contains attack-related terminology."""
    combined = f"{task_type} {rule_name} {title}".lower()
    return any(indicator in combined for indicator in ATTACK_INDICATORS)


# HIGH-CONFIDENCE attack content patterns in raw_log (Red team patch)
# These indicate real attacks regardless of what the metadata says
RAW_LOG_ATTACK_PATTERNS = [
    # --- Original 20 patterns ---
    r'(?i)mimikatz|sekurlsa|lsadump|kerberos::',
    r'(?i)certutil\s+(-urlcache|-split|-f\s+http)',
    r'(?i)bitsadmin.*transfer.*http',
    r'(?i)mshta\s+http',
    r'(?i)rundll32.*javascript',
    r'(?i)wscript.*\.(js|vbs)\b',
    r'(?i)powershell.*(-enc\b|-encodedcommand)',
    r'(?i)invoke-(mimikatz|expression|webrequest)',
    r'(?i)net\s+(user|localgroup)\s+.*(/add|/delete)',
    r'(?i)schtasks.*/create.*(/sc|/tn|/tr)',
    r'(?i)reg\s+add.*\\\\run\b',
    r'(?i)vssadmin.*delete\s+shadows',
    r'(?i)wmic.*process\s+call\s+create',
    r'(?i)psexec|paexec',
    r'(?i)impacket|secretsdump|ntlmrelayx',
    r'(?i)bloodhound|sharphound',
    r'(?i)rubeus\s+(asreproast|kerberoast|hash)',
    r'(?i)\\\\[^\\]+\\(c|admin|ipc)\$',
    r'(?i)CreateRemoteThread|NtMapViewOfSection',
    r'(?i)lsass\.exe|ntds\.dit|sam\s+dump',
    # --- Red team v2: LOLBins and additional techniques ---
    r'(?i)msiexec\s+.*(/q|/i\s+http)',
    r'(?i)installutil\s+.*(/logfile|payload)',
    r'(?i)regasm\s+(/U\s+|.*\.dll)',
    r'(?i)regsvcs\s+.*\.dll',
    r'(?i)cmstp\s+(/s|/ns)',
    r'(?i)msbuild\.?e?x?e?\s+.*\.(csproj|xml)',
    r'(?i)forfiles\s+.*(/c|/p\s+)',
    r'(?i)System\.Reflection\.Assembly.*Load',
    r'(?i)Net\.WebClient.*Download(String|File)',
    r'(?i)DownloadString\s*\(\s*["\']http',
    # Cloud / container attacks
    r'(?i)aws\s+sts\s+assume-role',
    r'(?i)gcloud\s+auth\s+print-access-token',
    r'(?i)kubectl\s+exec\s+.*(-it|--\s+/bin)',
    r'(?i)docker\s+run\s+--privileged',
    # Linux attacks
    r'(?i)curl\s+http.*\|\s*(ba)?sh',
    r'(?i)wget\s+http.*\|\s*(ba)?sh',
    r'(?i)LD_PRELOAD\s*=',
    r'(?i)crontab\s+(-e|-l|.*\*\s+\*\s+\*)',
    r'(?i)\>>\s*/etc/crontab',
    # Additional Windows techniques
    r'(?i)cobalt\s*strike|beacon\s+interval',
    r'(?i)meterpreter|reverse.?shell',
    r'(?i)cmd\.exe.*/c\s+(whoami|net\s+|dir\s+|type\s+)',
    r'(?i)dcsync|DRS(GetNC|Replicat)',
    r'(?i)golden.?ticket|krbtgt.*0x17',
    r'(?i)kerberoast|TGS.*0x17',
    r'(?i)pass.the.(hash|ticket)',
    r'(?i)\.exe.*\\\\.*\\(c|admin|ipc)\$',
    r'(?i)shadow.*copy|vssadmin',
    r'(?i)ransom|encrypt.*files|\.locked\b',
    r'(?i)union\s+select|or\s+1\s*=\s*1|sql.?inject',
    r'(?i)<script|javascript:|onerror\s*=|xss',
    r'(?i)\.\./\.\.|path.?traversal|/etc/passwd',
    r'(?i)webshell|\.php.*upload|c99|r57',
    r'(?i)office.*macro|vba.*shell|wmi.*subscription',
]


def _has_raw_log_attack_content(raw_log: str) -> bool:
    """Check if raw_log contains high-confidence attack indicators."""
    if not raw_log or len(raw_log) < 10:
        return False
    for pattern in RAW_LOG_ATTACK_PATTERNS:
        if re.search(pattern, raw_log):
            return True
    return False


def _endpoint_ip(ev: dict, key: str) -> str:
    ep = ev.get(key)
    if isinstance(ep, dict):
        v = ep.get("ip")
        return v if isinstance(v, str) else ""
    return ""


def enrich_legacy_fields_from_ocsf(ev: dict) -> dict:
    """Add ZCS-style flat keys from OCSF 1.3 shapes for downstream tools and pattern code."""
    if not isinstance(ev, dict) or ev.get("class_uid") is None:
        return ev
    out = dict(ev)
    sip = _endpoint_ip(ev, "src_endpoint")
    if sip and not out.get("source_ip"):
        out["source_ip"] = sip
    dip = _endpoint_ip(ev, "dst_endpoint")
    if dip and not out.get("destination_ip"):
        out["destination_ip"] = dip
    actor = ev.get("actor")
    if isinstance(actor, dict):
        user = actor.get("user")
        if isinstance(user, dict):
            n = user.get("name")
            if isinstance(n, str) and n and not out.get("username"):
                out["username"] = n
    fi = ev.get("finding_info")
    if isinstance(fi, dict):
        t = fi.get("title")
        if isinstance(t, str) and t and not out.get("rule_name"):
            out["rule_name"] = t
    if out.get("message") and not out.get("raw_log"):
        out["raw_log"] = str(out["message"])
    return out


# --- PII masking (simplified — regex-based, no Redis entity map) ---
PII_PATTERNS = [
    (r'AKIA[0-9A-Z]{16}', 'AWS_KEY'),
    (r'\b\d{3}-\d{2}-\d{4}\b', 'SSN'),
    (r'\b(?:sk|pk|api|key|token|secret|bearer)[_-]?[A-Za-z0-9]{20,}\b', 'API_KEY'),
]


def _mask_pii(text: str) -> tuple:
    """Simple regex PII masking. Returns (masked_text, was_masked)."""
    masked = text
    count = 0
    for pattern, label in PII_PATTERNS:
        matches = re.findall(pattern, masked)
        for i, m in enumerate(matches):
            masked = masked.replace(m, f'[{label}_{i}]', 1)
            count += 1
    return masked, count > 0


# Audit 2.2: validate tenant_id before interpolating into SET LOCAL. uuid.UUID
# raises ValueError on malformed input so we never construct SQL from untrusted
# data. Import locally so a test that stubs out uuid doesn't break the module.
import uuid as _uuid


def validate_tenant_id(tenant_id: str) -> str:
    """Validate tenant_id is a well-formed UUID. Returns the input on success,
    raises ValueError otherwise. Every SET LOCAL app.current_tenant call MUST
    go through this helper — otherwise a bug that lets user input reach
    tenant_id becomes SQL injection on every transaction."""
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string")
    _uuid.UUID(tenant_id)  # raises on invalid
    return tenant_id


def _set_tenant(cur, tenant_id: str) -> None:
    """Apply SET LOCAL app.current_tenant inside the current transaction.
    Audit 2.2: validated UUID path only."""
    validated = validate_tenant_id(tenant_id)
    # psycopg2.sql parameter binding doesn't work for SET LOCAL through
    # PgBouncer transaction pooling, so we interpolate — but only after
    # validate_tenant_id has guaranteed the string contains only [0-9a-f-].
    cur.execute(f"SET LOCAL app.current_tenant = '{validated}'")


# --- Skill retrieval (DB only, no LLM) ---
def _retrieve_skill(task_type: str, prompt: str, conn, tenant_id: str = "") -> Optional[dict]:
    """Find matching skill template. Pure DB query.

    Audit 2.4: when tenant_id is provided, the UPDATE runs inside a
    transaction scoped by SET LOCAL app.current_tenant so RLS sees the
    correct tenant. The SELECTs also filter by tenant_id as defence in depth.
    """
    tt = task_type.lower().replace(" ", "_")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Scope the transaction to the tenant if we have one.
            if tenant_id:
                try:
                    _set_tenant(cur, tenant_id)
                except ValueError as ve:
                    print(f"_retrieve_skill: invalid tenant_id ({ve}) — aborting")
                    return None

            # Priority 1: exact threat_type match (tenant-scoped)
            cur.execute("""
                SELECT id, skill_name, skill_slug, investigation_methodology,
                       detection_patterns, mitre_techniques, code_template, parameters
                FROM agent_skills
                WHERE is_active = true AND code_template IS NOT NULL
                  AND (%s = '' OR tenant_id::text = %s)
                  AND %s = ANY(threat_types)
                ORDER BY times_used DESC LIMIT 1
            """, (tenant_id, tenant_id, tt))
            row = cur.fetchone()

            # Priority 2: prefix match
            if not row:
                cur.execute("""
                    SELECT id, skill_name, skill_slug, investigation_methodology,
                           detection_patterns, mitre_techniques, code_template, parameters
                    FROM agent_skills
                    WHERE is_active = true AND code_template IS NOT NULL
                      AND (%s = '' OR tenant_id::text = %s)
                      AND EXISTS (SELECT 1 FROM unnest(threat_types) t WHERE t LIKE %s || '%%' OR %s LIKE t || '%%')
                    ORDER BY times_used DESC LIMIT 1
                """, (tenant_id, tenant_id, tt, tt))
                row = cur.fetchone()

            if row:
                # Audit 2.4: SET LOCAL is active for this transaction so the
                # UPDATE runs under the correct tenant RLS context.
                cur.execute(
                    "UPDATE agent_skills SET times_used = times_used + 1 WHERE id = %s",
                    (row['id'],),
                )
                conn.commit()
                return dict(row)
    except Exception as e:
        print(f"Skill retrieval failed (non-fatal): {e}")
    return None


# --- Fetch task from DB (not @activity.defn — legacy fetch_task is registered) ---
async def fetch_task(task_id: str) -> dict:
    """Load task from agent_tasks table. Shared by V2 workflow."""
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, tenant_id, task_type, input, status, trace_id, raw_input, dedup_hash FROM agent_tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
            row['id'] = str(row['id'])
            row['tenant_id'] = str(row['tenant_id'])
            row['trace_id'] = str(row['trace_id']) if row.get('trace_id') else ""
            return dict(row)


# --- Main entry point ---
@activity.defn
async def ingest_alert(task_data: dict) -> dict:
    """
    Stage 1: Validate, deduplicate, prepare alert for analysis.
    NO LLM calls. Only Redis + DB.

    Returns dict (serializable IngestOutput fields).
    """
    with trace_investigation_pipeline(task_data):
        with trace_stage_ingest_span(task_data):
            return await _ingest_alert_body(task_data)


async def _ingest_alert_body(task_data: dict) -> dict:
    import time as _time
    _t0 = _time.perf_counter()
    try:
        return await __ingest_alert_core(task_data)
    finally:
        try:
            from metrics import record_pipeline_stage
            record_pipeline_stage("ingest", _time.perf_counter() - _t0)
        except Exception:
            pass


async def __ingest_alert_core(task_data: dict) -> dict:
    task_id = task_data.get("task_id", "")
    tenant_id = task_data.get("tenant_id", "")
    task_type = task_data.get("task_type", "")
    siem_event = task_data.get("input", {}).get("siem_event", {})
    siem_event = sanitize_siem_event(siem_event)
    if siem_event.get("_injection_warning"):
        activity.logger.warning(f"Prompt injection patterns detected in SIEM data for task {task_id}")
    siem_event = enrich_legacy_fields_from_ocsf(siem_event)
    if isinstance(siem_event, dict) and siem_event.get("class_uid") is not None:
        activity.logger.info(f"Ingest OCSF event class_uid={siem_event.get('class_uid')}")
    prompt = task_data.get("input", {}).get("prompt", "")

    # --- Redis client (shared by smart batcher + dedup) ---
    _redis_client = None
    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL)
    except Exception as e:
        print(f"Redis connection failed (non-fatal, batcher/dedup use fallback): {e}")

    # --- Smart batching: aggregate similar alerts within time window ---
    if siem_event and _redis_client:
        try:
            batcher = get_batcher(_redis_client)
            severity = task_data.get("input", {}).get("severity", "medium")
            # Audit 2.12: tenant_id is now mandatory on every batch key.
            should_skip, aggregated = batcher.should_batch(
                tenant_id, task_type, siem_event, severity
            )

            if should_skip:
                activity.logger.info(f"Smart batcher: alert absorbed into batch for {task_type}")
                return asdict(IngestOutput(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    task_type=task_type,
                    siem_event=siem_event,
                    prompt="",
                    is_duplicate=True,
                    duplicate_of="batch",
                    dedup_reason="smart_batch",
                    trace_id=str(task_data.get("trace_id", "") or ""),
                ))

            if aggregated:
                siem_event = aggregated
                activity.logger.info(f"Smart batcher: processing aggregated batch of {aggregated.get('_batch_count', 1)} alerts")
        except Exception as e:
            print(f"Smart batching failed (non-fatal): {e}")

    result = IngestOutput(
        task_id=task_id,
        tenant_id=tenant_id,
        task_type=task_type,
        siem_event=siem_event,
        prompt=prompt,
        trace_id=str(task_data.get("trace_id", "") or ""),
    )

    # Pre-Temporal exact dedup runs in the Go API; worker does not register duplicate Redis keys.

    # --- PII masking ---
    if prompt:
        masked_prompt, was_masked = _mask_pii(prompt)
        if was_masked:
            result.pii_masked = True
            result.prompt = masked_prompt

    # --- Skill retrieval ---
    try:
        with _db_conn() as conn:
            skill = _retrieve_skill(task_type, prompt, conn, tenant_id)
            if skill:
                # Red team patch: content-based override
                # If skill routes to benign but raw_log has attack content, block benign routing
                skill_slug = skill.get("skill_slug", "")
                if skill_slug == "benign-system-event":
                    raw_log = siem_event.get("raw_log", "")
                    if _has_raw_log_attack_content(raw_log):
                        activity.logger.warning(
                            f"Classification override: benign metadata but attack content "
                            f"in raw_log for task {task_id}. Forcing investigation."
                        )
                        skill = None  # Clear benign skill — force Path C investigation

                if skill:
                    result.skill_id = str(skill.get("id", ""))
                    result.skill_template = skill.get("code_template")
                    result.skill_params = skill.get("parameters", [])
                    result.skill_methodology = skill.get("investigation_methodology", "")
    except Exception as e:
        print(f"Skill retrieval failed (non-fatal): {e}")

    return asdict(result)

"""
Stage 4: ASSESS — Generate verdict and investigation summary.
LLM calls: entity extraction (optional), FP analysis (optional).

This is one of TWO files that call the LLM (the other is Stage 2 ANALYZE).
All LLM verdict/summary calls are contained HERE.

Self-contained: imports httpx, psycopg2 directly.
Does NOT import from _legacy_activities.py or intelligence/fp_analyzer.py.
"""
import os
import re
import json
import time
from typing import List, Dict
from dataclasses import asdict

import httpx

from temporalio import activity
from stages import AssessOutput
from stages.llm_gateway import llm_call, MODEL_CODE
from stages.model_router import get_model_config
from stages.output_validator import validate_investigation_output, safe_default_output
from stages.mitre_mapping import get_mitre_techniques

FAST_FILL = os.environ.get("ZOVARK_FAST_FILL", "false").lower() == "true"


def _suppression_pattern_matched(patterns: list, text: str, tenant_id: str, rule_prefix: str) -> bool:
    """Apply suppression regexes; log audit on evaluation errors (never silent)."""
    for i, p in enumerate(patterns):
        try:
            if re.search(p, text):
                return True
        except re.error as e:
            try:
                from governance.suppression import log_suppression_eval_failure

                log_suppression_eval_failure(
                    tenant_id or None,
                    f"{rule_prefix}_{i}",
                    f"suppression eval error: {e}",
                    alert_context=text[:800],
                )
            except Exception:
                pass
    return False
ZOVARK_LLM_ENDPOINT = os.environ.get("ZOVARK_LLM_ENDPOINT", "http://zovark-inference:8080/v1/chat/completions")
# stabilize-runtime-hygiene: LLM key loading is centralized on settings.llm_key.
# No hardcoded fallback — Pydantic raises ValidationError if ZOVARK_LLM_KEY is unset.
from settings import settings as _settings
ZOVARK_LLM_KEY = _settings.llm_key.get_secret_value()
ASSESS_SUMMARY_TIMEOUT = float(os.getenv("ZOVARK_ASSESS_TIMEOUT", "45"))


# --- Verdict derivation (shared from worker/verdict.py) ---
from verdict import derive_verdict as _derive_verdict_impl, severity_from_risk as _severity_from_risk_impl


def _derive_verdict(risk_score: int, ioc_count: int, finding_count: int, execution_mode: str = "sandbox") -> str:
    return _derive_verdict_impl(risk_score, ioc_count, finding_count, execution_mode=execution_mode)


def _severity_from_risk(risk_score: int) -> str:
    return _severity_from_risk_impl(risk_score)


# --- Template summary (no LLM) ---
def _template_summary(task_type: str, findings: list, iocs: list, risk_score: int) -> str:
    """Generate a memory summary without LLM."""
    ioc_types = set()
    for ioc in iocs:
        if isinstance(ioc, dict):
            ioc_types.add(ioc.get("type", "unknown"))
    return (
        f"Investigated {task_type} alert. "
        f"Found {len(findings)} findings and {len(iocs)} IOCs ({', '.join(ioc_types) or 'none'}). "
        f"Risk score: {risk_score}."
    )


# --- LLM summary (optional, uses CODE model) ---
# PREFIX CACHING: static system prompt is cached; only investigation data is dynamic.
_SUMMARY_SYSTEM = (
    "Summarize this SOC investigation in 2-3 sentences for an L1 analyst. "
    "Lead with the verdict, mention key IOCs, and state the recommended action. "
    "Be specific — cite IPs, usernames, and MITRE techniques from the data."
)

async def _llm_summary(stdout: str, task_type: str, task_id: str = "", tenant_id: str = "") -> str:
    """Call LLM to generate a 2-3 sentence investigation summary."""
    try:
        summary_config = get_model_config(severity="low", task_type=task_type)
        summary_config.update({"model": MODEL_CODE, "temperature": 0.3, "max_tokens": 200})
        result = await llm_call(
            prompt=stdout[:2000],
            system_prompt=_SUMMARY_SYSTEM,
            model_config=summary_config,
            task_id=task_id,
            stage="assess",
            task_type=task_type,
            tenant_id=tenant_id,
            timeout=ASSESS_SUMMARY_TIMEOUT,
            role="summary",
            grammar_name=None,  # no grammar for prose summary
        )
        return result["content"]
    except Exception as e:
        print(f"LLM summary failed (non-fatal): {type(e).__name__}: {e}")
        return ""


# --- Comprehensive IOC extraction ---
def _is_valid_ioc_ip(ip: str) -> bool:
    """Check if IP is valid and not a boring internal/broadcast address."""
    try:
        parts = ip.split('.')
        if len(parts) != 4:
            return False
        nums = [int(p) for p in parts]
        if any(n < 0 or n > 255 for n in nums):
            return False
        if ip in ('0.0.0.0', '255.255.255.255', '127.0.0.1'):
            return False
        if nums[0] == 0 or nums[0] >= 224:
            return False
        return True
    except (ValueError, IndexError):
        return False


def _looks_like_uuid(h: str) -> bool:
    """Check if a 32-char hex string is likely a UUID."""
    if len(h) == 32:
        return h[12] == '4' and h[16] in '89ab'
    return False


def _snippet_around(text: str, value: str, context_chars: int = 30) -> str:
    """Return a snippet of text surrounding the first occurrence of value."""
    match_pos = text.find(value)
    if match_pos >= 0:
        start = max(0, match_pos - context_chars)
        end = min(len(text), match_pos + len(value) + context_chars)
        return text[start:end]
    return value


def _raw_text_evidence(combined_text: str, value: str) -> list:
    """Build evidence_refs for an IOC found via regex in raw text."""
    snippet = _snippet_around(combined_text, value)
    return [{"source": "raw_log", "raw_text": snippet}]


def _extract_iocs_from_signals(siem_event: dict, stdout: str, prompt: str = "") -> list:
    """Extract IOCs from SIEM data and sandbox output, with evidence_refs."""
    iocs = []
    seen = set()

    combined_text = f"{stdout} {prompt}"
    if isinstance(siem_event, dict):
        combined_text += f" {siem_event.get('raw_log', '')}"
        combined_text += f" {siem_event.get('title', '')}"
        combined_text += f" {siem_event.get('rule_name', '')}"

    # Structured SIEM fields (highest confidence)
    if isinstance(siem_event, dict):
        for field in ('source_ip', 'src_ip', 'attacker_ip', 'remote_ip'):
            ip = siem_event.get(field, '')
            if ip and _is_valid_ioc_ip(ip) and ip not in seen:
                iocs.append({
                    "type": "ipv4", "value": ip, "context": f"{field} from SIEM event",
                    "evidence_refs": [{"source": f"siem_event.{field}", "raw_text": ip, "field_path": f"siem_event.{field}"}],
                })
                seen.add(ip)
        for field in ('destination_ip', 'dst_ip', 'dest_ip', 'target_ip'):
            ip = siem_event.get(field, '')
            if ip and _is_valid_ioc_ip(ip) and ip not in seen:
                iocs.append({
                    "type": "ipv4", "value": ip, "context": f"{field} from SIEM event",
                    "evidence_refs": [{"source": f"siem_event.{field}", "raw_text": ip, "field_path": f"siem_event.{field}"}],
                })
                seen.add(ip)
        for field in ('username', 'user', 'account', 'email', 'src_user', 'target_user'):
            val = siem_event.get(field, '')
            if val and '@' in val and val not in seen:
                iocs.append({
                    "type": "email", "value": val, "context": f"{field} from SIEM event",
                    "evidence_refs": [{"source": f"siem_event.{field}", "raw_text": val, "field_path": f"siem_event.{field}"}],
                })
                seen.add(val)
            elif val and val not in seen and val not in ('root', 'admin', 'system', 'unknown', 'N/A', '-', 'attacker'):
                iocs.append({
                    "type": "username", "value": val, "context": f"{field} from SIEM event",
                    "evidence_refs": [{"source": f"siem_event.{field}", "raw_text": val, "field_path": f"siem_event.{field}"}],
                })
                seen.add(val)

    # IPs from raw text
    for ip in re.findall(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', combined_text):
        if ip not in seen and _is_valid_ioc_ip(ip):
            iocs.append({
                "type": "ipv4", "value": ip, "context": "extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, ip),
            })
            seen.add(ip)

    # URLs
    for url in re.findall(r'(https?://[^\s<>"\')\]]+)', combined_text):
        url = url.rstrip('.,;:')
        if url not in seen and len(url) > 10:
            iocs.append({
                "type": "url", "value": url[:200], "context": "extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, url),
            })
            seen.add(url)

    # Emails
    for email in re.findall(r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b', combined_text):
        if email not in seen and not email.endswith('.local'):
            iocs.append({
                "type": "email", "value": email, "context": "extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, email),
            })
            seen.add(email)

    # File hashes
    for h in re.findall(r'\b([a-fA-F0-9]{64})\b', combined_text):
        if h.lower() not in seen:
            iocs.append({
                "type": "sha256", "value": h.lower(), "context": "hash extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, h),
            })
            seen.add(h.lower())
    for h in re.findall(r'\b([a-fA-F0-9]{40})\b', combined_text):
        if h.lower() not in seen:
            iocs.append({
                "type": "sha1", "value": h.lower(), "context": "hash extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, h),
            })
            seen.add(h.lower())
    for h in re.findall(r'\b([a-fA-F0-9]{32})\b', combined_text):
        if h.lower() not in seen and not _looks_like_uuid(h):
            iocs.append({
                "type": "md5", "value": h.lower(), "context": "hash extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, h),
            })
            seen.add(h.lower())

    # Domains
    # Audit 2.16: cap combined_text before applying the domain regex.
    # The previous pattern is ReDoS-prone on adversarial inputs ("a-a-a-a-..." x N);
    # bounding the scan window to 32 KB keeps worst-case linear.
    domain_tlds = r'(?:com|net|org|io|xyz|ru|cn|tk|info|biz|top|cc|pw|ws|club|site|online|live|me|co|op)'
    _domain_scan_text = combined_text[:32768]
    for domain in re.findall(rf'\b([a-zA-Z0-9](?:[a-zA-Z0-9-]{{0,61}}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{{0,61}}[a-zA-Z0-9])?)*\.{domain_tlds})\b', _domain_scan_text):
        if domain.lower() not in seen and len(domain) > 4:
            iocs.append({
                "type": "domain", "value": domain.lower(), "context": "domain extracted from log/analysis",
                "evidence_refs": _raw_text_evidence(combined_text, domain),
            })
            seen.add(domain.lower())

    # CVE IDs
    for cve in re.findall(r'\b(CVE-\d{4}-\d{4,})\b', combined_text, re.IGNORECASE):
        if cve.upper() not in seen:
            iocs.append({
                "type": "cve", "value": cve.upper(), "context": "CVE reference",
                "evidence_refs": _raw_text_evidence(combined_text, cve),
            })
            seen.add(cve.upper())

    return iocs


# --- FP confidence (simple rules, no LLM) ---
def _fp_confidence(risk_score: int, ioc_count: int) -> float:
    """Rule-based FP confidence. Higher = more likely false positive."""
    if risk_score >= 80 and ioc_count >= 3:
        return 0.1  # Very likely real
    elif risk_score >= 60:
        return 0.3
    elif risk_score >= 40:
        return 0.5
    elif ioc_count == 0:
        return 0.8  # Likely FP
    return 0.6


# --- Validation failure logging ---
# stabilize-runtime-hygiene: centralized DB URL via settings.
DATABASE_URL = os.environ.get("DATABASE_URL", _settings.database_url)


def _log_validation_failure(task_id: str, tenant_id: str, task_type: str, error_msg: str):
    """Log validation failure to llm_audit_log (best-effort, never raises)."""
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO llm_audit_log
                       (tenant_id, task_id, stage, task_type, model_name, status, error_message, created_at)
                       VALUES (%s, %s, 'assess', %s, 'output_validator', 'validation_failed', %s, NOW())""",
                    (tenant_id, task_id, task_type, error_msg),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Never let audit logging break the pipeline
        print(f"Validation failure logging failed (non-fatal): {e}")


# --- Plain-English summary for L1 analysts ---
def _generate_plain_english(task_type: str, verdict: str, risk_score: int,
                            findings: list, iocs: list, mitre: list,
                            siem_event: dict) -> str:
    """Generate plain-English summary for L1 analysts. No LLM, deterministic."""
    lines = []
    src = siem_event.get("source_ip", "unknown source") if isinstance(siem_event, dict) else "unknown"
    user = siem_event.get("username", "") if isinstance(siem_event, dict) else ""

    # Lead with verdict
    if verdict == "true_positive":
        lines.append(f"• CONFIRMED ATTACK detected from {src}")
    elif verdict == "suspicious":
        lines.append(f"• Suspicious activity detected from {src}")
    elif verdict == "needs_review":
        lines.append(f"• Moderate-risk activity from {src} — analyst review recommended")
    elif verdict == "benign":
        lines.append(f"• Routine activity — no threat detected")
    elif verdict == "needs_analyst_review":
        lines.append(f"• Novel attack pattern from {src} — requires analyst confirmation")
    else:
        lines.append(f"• Alert from {src} — verdict: {verdict}")

    # Key finding
    if findings:
        first = findings[0]
        if isinstance(first, dict):
            lines.append(f"• {first.get('title', first.get('finding', str(first)[:150]))}")
        elif isinstance(first, str):
            lines.append(f"• {first[:150]}")

    # IOC count
    if iocs:
        ioc_types = set()
        for ioc in iocs[:10]:
            if isinstance(ioc, dict):
                ioc_types.add(ioc.get("type", "indicator"))
        lines.append(f"• {len(iocs)} indicator(s) found ({', '.join(sorted(ioc_types))})")

    # Risk
    if risk_score >= 70:
        lines.append(f"• HIGH RISK ({risk_score}/100) — immediate action recommended")
    elif risk_score >= 25:
        lines.append(f"• MEDIUM RISK ({risk_score}/100) — review recommended")
    else:
        lines.append(f"• LOW RISK ({risk_score}/100) — no action needed")

    # MITRE
    if mitre:
        techniques = [str(t) for t in mitre[:3]]
        lines.append(f"• MITRE ATT&CK: {', '.join(techniques)}")

    # User context
    if user:
        lines.append(f"• Affected user: {user}")

    return "\n".join(lines)


# --- Main entry point ---
@activity.defn
async def assess_results(data: dict) -> dict:
    """
    Stage 4: Generate verdict and investigation summary.

    FAST_FILL: template verdict based on IOC count (no LLM).
    Normal: optional LLM summary + rules-based FP analysis.

    Input: ExecuteOutput fields + task metadata
    Returns: dict (serializable AssessOutput fields)
    """
    # OTEL span — audit 2.17: guarantee span.end() runs on every exit path
    # (happy-path return AND exception). assess_results is a long function with
    # multiple early returns; wrapping the whole body in try/finally would
    # require re-indenting hundreds of lines. Instead we attach a SpanGuard to a
    # local variable whose CPython refcount hits zero on function frame exit,
    # at which point __del__ ends the span exactly once.
    class _SpanGuard:
        __slots__ = ("span", "done")

        def __init__(self, s):
            self.span = s
            self.done = False

        def finish(self):
            if self.span is not None and not self.done:
                self.done = True
                try:
                    self.span.end()
                except Exception:
                    pass

        def __del__(self):
            self.finish()

    try:
        from tracing import get_tracer
        _span = get_tracer().start_span("stage.assess")
        _span.set_attribute("zovark.task_id", data.get("task_id", ""))
        _span.set_attribute("zovark.task_type", data.get("task_type", ""))
        _span.set_attribute("zovark.execution_mode", data.get("execution_mode", ""))
    except Exception:
        _span = None
    _span_guard = _SpanGuard(_span)  # noqa: F841 — held so __del__ runs on exit

    task_id = data.get("task_id", "")
    tenant_id = data.get("tenant_id", "")
    stdout = data.get("stdout", "")
    iocs = data.get("iocs", [])
    findings = data.get("findings", [])
    risk_score = data.get("risk_score", 0)
    recommendations = data.get("recommendations", [])
    task_type = data.get("task_type", "")

    # FAIL-CLOSED: If LLM was down during analyze, force manual review
    path_taken_early = data.get("path_taken", "")
    if path_taken_early == "error_llm_down":
        activity.logger.warning(f"LLM was down for task {task_id} — forcing needs_manual_review")
        result = AssessOutput(
            verdict="needs_manual_review",
            risk_score=0,
            severity="high",
            confidence=0.0,
            false_positive_confidence=0.0,
            recommendations=["LLM service was unavailable during investigation. Manual analysis required."],
            memory_summary="LLM unavailable — investigation incomplete",
        )
        out = asdict(result)
        out["iocs"] = []
        out["findings"] = [{"title": "LLM Unavailable", "details": "Automated investigation could not complete. Manual review required."}]
        out["mitre_attack"] = []
        out["investigation_metadata"] = {"pipeline_version": "v2", "llm_available": False}
        out["plain_english_summary"] = "• LLM service was unavailable — this alert requires manual analyst investigation\n• HIGH PRIORITY — do not ignore"
        out["status"] = "pending_review"
        out["needs_human_review"] = True
        out["review_reason"] = "LLM service was unavailable during investigation"
        try:
            from governance.cmmc import evaluate_cmmc_for_techniques
            from governance.hipaa import evaluate_hipaa_for_techniques

            out["compliance_mapping"] = {
                "cmmc": evaluate_cmmc_for_techniques([]),
                "hipaa": evaluate_hipaa_for_techniques([]),
            }
        except Exception:
            out["compliance_mapping"] = {}
        return out

    # --- Schema validation of sandbox output ---
    # Validate the data coming from execute stage (findings, iocs, risk_score, recommendations)
    sandbox_output = {
        "findings": findings,
        "iocs": iocs,
        "risk_score": risk_score,
        "recommendations": recommendations,
    }
    validation_context = {
        "tools_executed": data.get("tools_executed"),
        "plan_executed": data.get("plan_executed"),
    }
    is_valid, validation_error = validate_investigation_output(sandbox_output, context=validation_context)
    if not is_valid:
        activity.logger.warning(
            f"Sandbox output validation failed for task {task_id}: {validation_error}"
        )
        # Log validation failure to llm_audit_log (best-effort)
        _log_validation_failure(task_id, tenant_id, task_type, validation_error)
        # Use safe defaults — NEVER let invalid output reach the dashboard
        defaults = safe_default_output()
        findings = defaults["findings"]
        iocs = defaults["iocs"]
        risk_score = defaults["risk_score"]
        recommendations = defaults["recommendations"]

    # --- Web attack signal boost ---
    # If raw SIEM data contains obvious attack patterns, boost risk score
    # regardless of which template ran. This handles cross-category mismatches
    # (e.g., SQLi alert categorized as data_exfil).
    siem_event = data.get("siem_event", {})
    if isinstance(siem_event, dict):
        raw_log = siem_event.get("raw_log", "")
        siem_title = siem_event.get("title", "")
        siem_rule = siem_event.get("rule_name", "")
    else:
        raw_log = siem_title = siem_rule = ""
    # Signal boost scans SIEM-provided data ONLY — not tool execution output.
    # stdout contains tool runner variable refs ($raw_log, $step2) and JSON keys
    # ("source", "evidence_refs") that trigger false positives on attack regexes.
    combined_signal = f"{raw_log} {siem_title} {siem_rule}".lower()

    attack_signals = [
        (r"(?:union\s+select|or\s+1\s*=\s*1|'\s*or\s*'|drop\s+table|;.*--|\bsleep\s*\(|benchmark\s*\(|sqli|sql.?inject)", "SQL injection"),
        (r"<script|javascript:|onerror\s*=|onload\s*=|alert\s*\(|document\.cookie|\bxss\b", "Cross-site scripting"),
        (r"\.\./|\.\.\\|%2e%2e|/etc/passwd|/etc/shadow|path.?traversal|directory.?traversal", "Path traversal"),
        (r"admin.*bypass|auth.*bypass|broken.*auth|forced.?browsing|idor|bola|unauthorized.*access", "Auth bypass"),
        (r"command.?injection|cmd.?inject|;\s*cat\s|;\s*ls\s|`.*`|\$\(.*\)|%0a|rce\b", "Command injection"),
        (r"ssrf|server.?side.?request|localhost.*redirect|127\.0\.0\.1.*access", "SSRF"),
        (r"file.?upload|unrestricted.?upload|webshell|\.php\b.*upload|\.jsp\b.*upload", "File upload attack"),
        (r"beacon.*\d+\s*s|c2.?beacon|c2.?detect|command.?and.?control|callback.*interval|beaconing", "C2 communication"),
    ]
    attack_boost = 0
    attack_types_found = []
    for pattern, attack_name in attack_signals:
        if re.search(pattern, combined_signal, re.IGNORECASE):
            attack_boost += 45
            attack_types_found.append(attack_name)
    if attack_boost > 0:
        risk_score = min(100, risk_score + attack_boost)
        if not any(attack_types_found[0].lower() in str(f).lower() for f in findings):
            findings.append({"title": f"Attack Signal: {', '.join(attack_types_found)}",
                             "details": f"SIEM data contains indicators of {', '.join(attack_types_found)}"})
    # --- Comprehensive IOC extraction from SIEM + sandbox output ---
    extracted_iocs = _extract_iocs_from_signals(siem_event, stdout)
    existing_by_value = {}
    for i in iocs:
        if isinstance(i, dict):
            existing_by_value[i.get("value", "")] = i
    for new_ioc in extracted_iocs:
        val = new_ioc["value"]
        if val in existing_by_value:
            # Enrich existing IOC with evidence_refs from extraction
            existing_ioc = existing_by_value[val]
            if "evidence_refs" not in existing_ioc:
                existing_ioc["evidence_refs"] = new_ioc.get("evidence_refs", [])
        else:
            iocs.append(new_ioc)
            existing_by_value[val] = new_ioc

    # --- IOC provenance validation (Red team patch: prevents phantom IP fabrication) ---
    # Audit 2.15: use word-boundary regex for ALL IOC types, not just substring
    # contains. Previously "192.168.1.10" was "confirmed" when raw_log contained
    # only "192.168.1.100", and "admin" was confirmed by raw_log containing
    # "administrator" — both are provenance bypasses.
    raw_log_lower = raw_log.lower() if raw_log else ""
    if raw_log:
        for ioc in iocs:
            if not isinstance(ioc, dict):
                continue
            value = str(ioc.get("value", ""))
            if not value:
                continue
            # Compose a word-boundary pattern that also works for IPs (. separator).
            escaped = re.escape(value)
            value_in_raw = bool(
                re.search(r'(?<![A-Za-z0-9_.])' + escaped + r'(?![A-Za-z0-9_.])',
                          raw_log_lower, re.IGNORECASE)
            )

            if value_in_raw:
                ioc.setdefault("confidence", "high")
            else:
                # IOC only in structured fields — downgrade
                has_struct_source = any(
                    ref.get("source", "").startswith(("siem_event.", "source_ip", "destination_ip", "title", "rule_name"))
                    for ref in ioc.get("evidence_refs", [])
                )
                if has_struct_source:
                    ioc["confidence"] = "low"
                    ioc["provenance_warning"] = (
                        f"IOC '{value}' in structured field only, not confirmed in raw_log"
                    )

    # Count only confirmed IOCs for risk/verdict decisions
    confirmed_iocs = [i for i in iocs if isinstance(i, dict) and i.get("confidence") != "low"]

    # --- Suppression phrase detection (Red team patch: adversarial risk manipulation) ---
    SUPPRESSION_PATTERNS = [
        r'(?i)scheduled\s+(penetration\s+)?test',
        r'(?i)authorized\s+(security\s+)?scan',
        r'(?i)do\s+not\s+escalate',
        r'(?i)false\s+positive\s+(confirmed|verified)',
        r'(?i)(compliance|audit)\s+(drill|exercise)',
        r'(?i)test\s+alert\s*[-—:]\s*(ignore|disregard)',
        r'(?i)simulation\s+exercise',
        r'(?i)approved\s+activity',
    ]
    has_suppression = _suppression_pattern_matched(
        SUPPRESSION_PATTERNS, combined_signal, tenant_id, "builtin_suppression"
    )
    if has_suppression and (attack_boost > 0 or risk_score >= 50):
        # Attack indicators + suppression language = adversarial manipulation
        risk_score = max(risk_score, 75)
        findings.append({
            "title": "Adversarial Risk Suppression Detected",
            "details": (
                "Alert contains both attack indicators and suppression language. "
                "Legitimate security tests are documented in change management, "
                "not embedded in alert data."
            ),
            "severity": "high",
        })

    # --- Findings synthesis: generate findings from IOCs when sandbox produced none ---
    if confirmed_iocs and not findings and risk_score >= 50:
        for ioc in confirmed_iocs[:10]:
            findings.append({
                "title": f"Detected {ioc.get('type', 'unknown')}: {ioc.get('value', '')}",
                "severity": "high" if risk_score >= 70 else "medium",
                "synthesized": True,
            })
        activity.logger.info(f"Synthesized {len(findings)} findings from confirmed IOCs")

    # Template attack risk floor: if the alert matched a known attack template
    # (Path A) but the LLM under-scored it, ensure risk >= 70.
    # Only applies when risk is in the ambiguous 36-69 range — doesn't override
    # genuinely benign verdicts (<=35) or already-high scores (>=70).
    from stages.ingest import _has_attack_indicators
    siem_rule_name = siem_event.get("rule_name", "") if isinstance(siem_event, dict) else ""
    siem_title_val = siem_event.get("title", "") if isinstance(siem_event, dict) else ""
    if _has_attack_indicators(task_type, siem_rule_name, siem_title_val):
        if 25 <= risk_score < 70:
            activity.logger.info(f"Boosting template-matched attack from risk {risk_score} to 70")
            risk_score = 70

    # Check for verdict_override from safety wrapper (crashed Path C code)
    verdict_override = data.get("verdict_override", "")
    if verdict_override == "error":
        verdict = "error"
    else:
        execution_mode = data.get("execution_mode", "sandbox")
        verdict = _derive_verdict(risk_score, len(confirmed_iocs), len(findings), execution_mode=execution_mode)
    severity = _severity_from_risk(risk_score)
    fp_conf = _fp_confidence(risk_score, len(iocs))

    # If validation failed, override verdict — but not when risk is clearly an attack
    if not is_valid:
        if risk_score >= 70:
            verdict = "true_positive"
            activity.logger.info(f"Overriding needs_manual_review → true_positive at risk {risk_score}")
        else:
            verdict = "needs_manual_review"

    # Path C learning gate: flag for analyst review to enable template promotion
    path_taken = data.get("path_taken", "")
    if path_taken == "C" and verdict == "true_positive":
        # Store original verdict for reference, but flag for review
        data["_original_verdict"] = verdict
        verdict = "needs_analyst_review"
        activity.logger.info(f"Path C learning gate: {verdict} for task {task_id} (original: true_positive)")

    # Summary
    if FAST_FILL:
        summary = _template_summary(task_type, findings, iocs, risk_score)
    else:
        summary = await _llm_summary(stdout, task_type, task_id=task_id, tenant_id=tenant_id)
        if not summary:
            summary = _template_summary(task_type, findings, iocs, risk_score)

    result = AssessOutput(
        verdict=verdict,
        risk_score=risk_score,
        severity=severity,
        confidence=1.0 - fp_conf,
        false_positive_confidence=fp_conf,
        recommendations=recommendations,
        memory_summary=summary,
    )

    out = asdict(result)
    # Include enriched iocs/findings so they override executed values in store merge
    out["iocs"] = iocs
    out["findings"] = findings
    out["mitre_attack"] = get_mitre_techniques(task_type)
    out["investigation_metadata"] = {
        "pipeline_version": "v2",
        "schema_validated": is_valid,
    }
    out["plain_english_summary"] = _generate_plain_english(
        task_type=task_type,
        verdict=verdict,
        risk_score=risk_score,
        findings=findings,
        iocs=iocs,
        mitre=out.get("mitre_attack", []),
        siem_event=siem_event,
    )

    # Emit real-time events for dashboard waterfall
    trace_id = data.get("trace_id", "")
    try:
        from events import emit_event
        # IOC discovery events
        for ioc in iocs[:10]:  # cap at 10 to avoid flooding
            ioc_val = ioc.get("value", "") if isinstance(ioc, dict) else str(ioc)
            ioc_type = ioc.get("type", "unknown") if isinstance(ioc, dict) else "unknown"
            if ioc_val:
                emit_event(task_id, tenant_id, trace_id, "ioc_discovered", {"ioc_type": ioc_type, "value": ioc_val})
        # MITRE mapping events
        for technique in out.get("mitre_attack", [])[:5]:
            tid = technique.get("technique_id", technique) if isinstance(technique, dict) else str(technique)
            tname = technique.get("name", tid) if isinstance(technique, dict) else tid
            emit_event(task_id, tenant_id, trace_id, "mitre_mapped", {"technique_id": tid, "name": tname})
        # Verdict event
        emit_event(task_id, tenant_id, trace_id, "verdict_ready", {"verdict": verdict, "risk_score": risk_score})
    except Exception:
        pass
    # Override status to "completed" when assess produced a valid verdict.
    # The execute stage may have set status="failed" from non-zero exit code,
    # but if assess derived a real verdict with risk > 0, the investigation succeeded.
    if verdict in ("true_positive", "suspicious", "benign", "needs_analyst_review") and risk_score > 0:
        out["status"] = "completed"

    # Pydantic verdict validation (graceful — never crashes the investigation)
    try:
        from schemas import VerdictOutput
        from pydantic import ValidationError
        # Map severity for validation (assess uses "informational", schema uses "info")
        sev_map = {"informational": "info", "critical": "critical", "high": "high", "medium": "medium", "low": "low", "info": "info"}
        # Only validate the core verdicts (needs_analyst_review/needs_manual_review bypass validation)
        if verdict in ("true_positive", "suspicious", "needs_review", "benign", "inconclusive", "error"):
            verdict_for_validation = {
                "verdict": verdict,
                "risk_score": risk_score,
                "severity": sev_map.get(severity, "medium"),
                "summary": out.get("plain_english_summary", "") or summary or "Investigation complete",
                "mitre_techniques": [t.get("technique_id") or t.get("id", str(t)) if isinstance(t, dict) else str(t) for t in out.get("mitre_attack", [])],
            }
            validated = VerdictOutput.model_validate(verdict_for_validation)
            # Apply cleaned MITRE techniques back (invalid IDs silently dropped)
            out["mitre_attack_validated"] = validated.mitre_techniques
    except ImportError:
        # Pydantic / schemas module not importable — optional validation.
        pass
    except Exception as e:  # noqa: BLE001 — ValidationError / value errors are logged but non-fatal
        # Audit 2.18: split broad (ImportError, Exception) so pydantic
        # ValidationError is logged distinctly and surfaced via metrics.
        activity.logger.warning(f"Verdict validation issue (non-fatal): {e}")

    technique_ids: list[str] = []
    if out.get("mitre_attack_validated"):
        technique_ids = [str(t) for t in (out["mitre_attack_validated"] or [])]
    else:
        for t in out.get("mitre_attack", []) or []:
            if isinstance(t, dict):
                tid = t.get("technique_id") or t.get("id")
                if tid:
                    technique_ids.append(str(tid))
            elif t:
                technique_ids.append(str(t))
    try:
        from governance.cmmc import evaluate_cmmc_for_techniques
        from governance.hipaa import evaluate_hipaa_for_techniques

        out["compliance_mapping"] = {
            "cmmc": evaluate_cmmc_for_techniques(technique_ids),
            "hipaa": evaluate_hipaa_for_techniques(technique_ids),
        }
    except Exception as cm_err:
        activity.logger.warning(f"compliance_mapping skipped: {cm_err}")
        out["compliance_mapping"] = {}

    # Annotate OTEL span with final verdict attributes. _SpanGuard.finish()
    # (via __del__) will end() the span on frame exit — both success and any
    # upstream exception paths.
    if _span:
        try:
            _span.set_attribute("result.verdict", verdict)
            _span.set_attribute("result.risk_score", risk_score)
            _span.set_attribute("result.ioc_count", len(iocs))
        except Exception:
            pass
    _span_guard.finish()

    return out

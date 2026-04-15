"""
Output schema validation for investigation results.

Validates that sandbox output and assess stage output conform to the
required schema before reaching the dashboard or being stored.

Used by: stages/assess.py (post-LLM validation)
"""
from typing import Tuple


# Valid verdicts that the pipeline can produce.
# Audit 2.26: include "needs_analyst_review" and "error" which assess.py
# produces on fail-closed / validator-override paths. Previously the validator
# rejected them as "unknown", silently downgrading real flagged investigations.
VALID_VERDICTS = {
    "true_positive", "false_positive", "suspicious", "needs_review",
    "benign", "needs_manual_review", "needs_analyst_review",
    "inconclusive", "error",
}


def validate_investigation_output(output: dict, context: dict = None) -> Tuple[bool, str]:
    """Validate investigation output matches required schema.

    Args:
        output: The investigation output dict to validate.
        context: Optional context from the pipeline. When tools_executed or
                 plan_executed is present (v3 tools mode), empty findings are
                 valid — they mean "tools found nothing suspicious".

    Returns:
        (is_valid, error_message) — error_message is "valid" when is_valid is True.
    """
    if not isinstance(output, dict):
        return False, f"Output is {type(output).__name__}, expected dict"

    # --- Required keys and types ---
    required = {
        "findings": list,
        "iocs": list,
        "risk_score": (int, float),
        "recommendations": list,
    }
    for key, expected_type in required.items():
        if key not in output:
            return False, f"Missing required key: {key}"
        if not isinstance(output[key], expected_type):
            return False, f"{key}: expected {expected_type}, got {type(output[key]).__name__}"

    # --- Findings must be non-empty (unless benign/low-risk or v3 tools mode) ---
    # In v3 tools mode, empty findings means "tools found nothing suspicious" — valid.
    risk = output.get("risk_score", -1)
    ctx = context or {}
    is_tools_mode = bool(ctx.get("tools_executed") or ctx.get("plan_executed"))
    if len(output.get("findings", [])) == 0 and risk > 30 and not is_tools_mode:
        return False, "findings must be non-empty for non-benign verdicts"

    # --- risk_score must be 0-100 ---
    risk = output.get("risk_score", -1)
    if not (0 <= risk <= 100):
        return False, f"risk_score must be 0-100, got {risk}"

    # --- IOCs: normalize strings to dicts, then validate ---
    import re
    normalized_iocs = []
    for i, ioc in enumerate(output.get("iocs", [])):
        if ioc is None or ioc == "":
            continue  # Skip None/empty entries silently
        if isinstance(ioc, str):
            # LLM sometimes returns IOCs as bare strings — auto-convert
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ioc):
                ioc = {"type": "ipv4", "value": ioc, "context": "auto-converted from string"}
            elif re.match(r'^https?://', ioc):
                ioc = {"type": "url", "value": ioc, "context": "auto-converted from string"}
            elif re.match(r'^[a-fA-F0-9]{32,64}$', ioc):
                ioc = {"type": "hash", "value": ioc, "context": "auto-converted from string"}
            elif '.' in ioc and re.match(r'^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$', ioc.lower()):
                ioc = {"type": "domain", "value": ioc, "context": "auto-converted from string"}
            else:
                ioc = {"type": "unknown", "value": ioc, "context": "auto-converted from string"}
        if not isinstance(ioc, dict) or "type" not in ioc or "value" not in ioc:
            return False, f"IOC[{i}] missing type or value: {ioc}"
        normalized_iocs.append(ioc)
    output["iocs"] = normalized_iocs

    # --- Verdict validation (optional field) ---
    verdict = output.get("verdict")
    if verdict and verdict not in VALID_VERDICTS:
        return False, f"Invalid verdict: {verdict}. Must be one of: {VALID_VERDICTS}"

    return True, "valid"


def safe_default_output() -> dict:
    """Return a safe default output when validation fails after retries."""
    return {
        "findings": [
            "Investigation output failed validation -- manual review required"
        ],
        "iocs": [],
        "risk_score": 50,
        "verdict": "needs_manual_review",
        "recommendations": [
            "Manually review the original alert"
        ],
    }

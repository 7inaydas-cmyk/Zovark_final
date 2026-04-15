"""
Stage 3: EXECUTE — Run investigation code in sandbox.
NO LLM calls. Security via AST prefilter + Docker isolation.

The adversarial LLM review is NOT in this stage.
Security comes from: AST analysis → Docker sandbox (read-only, no-network, seccomp).

Self-contained: uses subprocess, ast directly.
Does NOT import from _legacy_activities.py.
"""
import os
import re
import ast
import json
import time
import subprocess
from typing import List, Dict, Tuple
from dataclasses import asdict

from temporalio import activity
from stages import ExecuteOutput

import yaml
from pathlib import Path

# Load sandbox policy (YAML-driven, customer-auditable)
_POLICY_PATH = Path(__file__).parent / "sandbox_policy.yaml"
try:
    with open(_POLICY_PATH) as f:
        SANDBOX_POLICY = yaml.safe_load(f)
    _POLICY_VERSION = SANDBOX_POLICY.get("version", "unknown")
except Exception:
    SANDBOX_POLICY = None
    _POLICY_VERSION = "hardcoded-fallback"

FAST_FILL = os.environ.get("ZOVARK_FAST_FILL", "false").lower() == "true"
DOCKER_HOST = os.environ.get("DOCKER_HOST", "")  # tcp://docker-socket-proxy:2375

# --- AST Prefilter (inlined from sandbox/ast_prefilter.py concepts) ---
# Legacy YAML-driven blocklists (kept for sandbox_policy.yaml compatibility)
if SANDBOX_POLICY:
    FORBIDDEN_IMPORTS = frozenset(SANDBOX_POLICY["ast_prefilter"]["blocked_imports"])
else:
    FORBIDDEN_IMPORTS = frozenset({
        'os', 'sys', 'subprocess', 'socket', 'shutil', 'importlib',
        'pickle', 'marshal', 'ctypes', 'pty', 'signal',
    })

if SANDBOX_POLICY:
    _raw_patterns = SANDBOX_POLICY["ast_prefilter"]["blocked_patterns"]
    FORBIDDEN_PATTERNS = [rf'\b{re.escape(p.rstrip("("))}\s*\(' if p.endswith("(") else rf'\b{re.escape(p)}\b'
                          for p in _raw_patterns]
else:
    FORBIDDEN_PATTERNS = [
        r'\b__import__\s*\(',
        r'\beval\s*\(',
        r'\bexec\s*\(',
    ]

# --- Allowlist-based validation (layered on top of YAML blocklists) ---
ALLOWED_IMPORTS = {
    "json", "re", "datetime", "collections", "math", "hashlib",
    "ipaddress", "base64", "urllib.parse", "csv", "statistics",
    "string", "copy", "itertools", "functools", "typing",
}

BLOCKED_PATTERNS = [
    "import os", "import sys", "import subprocess", "import socket",
    "import urllib.request", "import http.client", "import http.server",
    "import ftplib", "import smtplib", "import xmlrpc",
    "import requests", "import aiohttp",
    "__import__", "importlib", "ctypes", "cffi",
    "import shutil", "import tempfile", "import pathlib",
    "import glob", "import fnmatch",
    "os.environ", "os.getenv", "getpass",
    "import pickle", "import shelve",
    "builtins",
]

BLOCKED_BUILTINS = {'open', 'eval', 'exec', 'compile', '__import__', 'breakpoint'}


def _strip_comments_and_strings(code: str) -> str:
    """Strip comments and string literals so blocked-string scan only sees
    actual executable tokens. Uses the ast module when possible so we don't
    false-positive on docstrings / comments mentioning `import os`."""
    try:
        tree = ast.parse(code)
        # Use ast.unparse to re-emit the AST without comments. Docstrings are
        # preserved as string literals; replace them with empty strings.
        class _DocstringStripper(ast.NodeTransformer):
            def visit_Expr(self, node):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return ast.Expr(value=ast.Constant(value=""))
                return node

        stripped = _DocstringStripper().visit(tree)
        ast.fix_missing_locations(stripped)
        return ast.unparse(stripped)
    except Exception:
        # ast.parse failing will be handled by Layer 2. Return raw code.
        return code


def _check_blocked_strings(code: str) -> Tuple[bool, str]:
    """Layer 1: Fast string-based pattern scan.

    Audit 2.33: run the scan on AST-stripped code (no comments/docstrings),
    so a legitimate comment like `# do not call os.environ here` isn't
    flagged as an injection attempt.
    """
    scan = _strip_comments_and_strings(code)
    scan_lower = scan.lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern.lower() in scan_lower:
            return False, f"Blocked pattern: {pattern}"
    return True, "OK"


def _validate_imports_allowlist(tree: ast.Module) -> Tuple[bool, str]:
    """Layer 3: Only allow imports from the explicit allowlist."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_root = alias.name.split(".")[0]
                if alias.name not in ALLOWED_IMPORTS and module_root not in ALLOWED_IMPORTS:
                    return False, f"Blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_root = node.module.split(".")[0]
                if node.module not in ALLOWED_IMPORTS and module_root not in ALLOWED_IMPORTS:
                    return False, f"Blocked from-import: {node.module}"
    return True, "OK"


def _validate_builtin_calls(tree: ast.Module) -> Tuple[bool, str]:
    """Layer 4: Block dangerous builtin function calls."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_BUILTINS:
                return False, f"Blocked builtin call: {node.func.id}"
    return True, "OK"


def _ast_check(code: str) -> Tuple[bool, str]:
    """Static AST analysis with 4-layer validation. Returns (is_safe, reason)."""
    # Layer 1: Fast blocked-string scan
    safe, reason = _check_blocked_strings(code)
    if not safe:
        return False, reason

    # Layer 2: Parse AST (catches syntax errors)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"

    # Layer 3: Allowlist-based import validation
    safe, reason = _validate_imports_allowlist(tree)
    if not safe:
        return False, reason

    # Layer 4: Blocked builtin calls
    safe, reason = _validate_builtin_calls(tree)
    if not safe:
        return False, reason

    # Legacy YAML-driven blocklist checks (kept for backward compatibility)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split('.')[0]
                if mod in FORBIDDEN_IMPORTS:
                    return False, f"Forbidden import: {mod}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split('.')[0]
                if mod in FORBIDDEN_IMPORTS:
                    return False, f"Forbidden import: {node.module}"

    for pattern in FORBIDDEN_PATTERNS:
        m = re.search(pattern, code)
        if m:
            return False, f"Forbidden pattern: {m.group()}"

    return True, ""


# --- Safety wrapper for LLM-generated code ---
_SAFETY_WRAPPER = '''import json as _json

_error = None
_output = None
try:
{indented_code}
except Exception as _e:
    _error = str(_e)
    print(_json.dumps({{"findings": [{{"title": "Investigation code error", "details": _error}}], "iocs": [], "risk_score": 0, "verdict_override": "error", "recommendations": ["Investigation code failed. Logged for engineering review. No analyst action required."]}}))
'''


def _wrap_code_safely(code: str) -> str:
    """Wrap LLM-generated code in try/except to guarantee JSON output."""
    # Only wrap if the code doesn't already have a top-level try/except
    stripped = code.strip()
    if stripped.startswith('try:') or '\ntry:\n' in stripped[:200]:
        return code
    # Indent the original code by 4 spaces for the try block
    indented = '\n'.join('    ' + line for line in code.split('\n'))
    return _SAFETY_WRAPPER.replace('{indented_code}', indented)


# --- Stdout parser ---
def _parse_stdout(stdout: str) -> Dict:
    """Parse investigation JSON from stdout.

    Audit 2.32: bound the greedy regex search to stdout[:65536]. The previous
    `.*` with `re.DOTALL` over multi-MB outputs is quadratic in the worst case.
    64 KB is enough for every well-formed verdict payload the pipeline emits.
    """
    if not stdout or not stdout.strip():
        return {}
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        # Try to find JSON object within the first 64 KB of stdout.
        window = stdout[:65536]
        match = re.search(r'\{.*\}', window, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


# --- Docker sandbox execution ---
def _run_in_sandbox(code: str, timeout: int = None) -> Dict:
    """Execute code in Docker sandbox. No LLM calls. Policy: v{_POLICY_VERSION}."""
    if timeout is None:
        timeout = SANDBOX_POLICY["process"]["max_execution_seconds"] if SANDBOX_POLICY else 120
    memory_mb = SANDBOX_POLICY["process"]["max_memory_mb"] if SANDBOX_POLICY else 512
    seccomp_path = "/app/sandbox/seccomp_profile.json"

    cmd = [
        "docker", "run", "--rm", "-i", "--network=none", "--read-only",
        "--tmpfs", "/tmp:size=64m,noexec,nosuid", "--workdir", "/tmp",
        "--cpus=0.5", f"--memory={memory_mb}m", f"--memory-swap={memory_mb}m",
        "--pids-limit=64", "--cap-drop=ALL",
        "--user", "65534:65534",
        "--security-opt=no-new-privileges",
        "--security-opt", f"seccomp={seccomp_path}",
        "python:3.11-slim", "python",
    ]

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd, input=code, capture_output=True, text=True, timeout=timeout
        )
        execution_ms = int((time.time() - start_time) * 1000)
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "execution_ms": execution_ms,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": f"Execution timed out after {timeout}s",
            "exit_code": -1,
            "execution_ms": int((time.time() - start_time) * 1000),
        }


def _run_fast_fill(code: str) -> Dict:
    """Execute code directly via subprocess (no Docker). For stress tests only."""
    start_time = time.time()
    try:
        result = subprocess.run(
            ["python", "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "execution_ms": int((time.time() - start_time) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": "Fast-fill execution timed out",
            "exit_code": -1,
            "execution_ms": int((time.time() - start_time) * 1000),
        }


EXECUTION_MODE = os.environ.get("ZOVARK_EXECUTION_MODE", "tools")  # "tools" (v3) or "sandbox" (v2)


def _load_correlation_context(tenant_id: str, siem_event: dict) -> dict:
    from stages.context_loader import load_correlation_context
    return load_correlation_context(tenant_id, siem_event)


def _load_institutional_knowledge(tenant_id: str, siem_event: dict) -> dict:
    from stages.context_loader import load_institutional_knowledge
    return load_institutional_knowledge(tenant_id, siem_event)


def _execute_v3_tools(data: dict) -> dict:
    """V3 tool-calling execution: run tool plan in-process (no Docker sandbox)."""
    from tools.runner import execute_plan

    plan = data.get("plan", [])
    siem_event = data.get("siem_event", {})
    tenant_id = data.get("tenant_id", "")

    # Load correlation context and institutional knowledge from DB
    history_context = _load_correlation_context(tenant_id, siem_event) if tenant_id else {"investigations": []}
    institutional_knowledge = _load_institutional_knowledge(tenant_id, siem_event) if tenant_id else {}

    if not plan:
        raise ValueError("No tool plan provided")

    task_id_val = data.get("task_id", "")
    trace_id_val = data.get("trace_id", "")

    start_time = time.time()
    result = execute_plan(
        plan, siem_event,
        history_context=history_context,
        institutional_knowledge=institutional_knowledge,
        task_id=task_id_val, tenant_id=tenant_id, trace_id=trace_id_val,
    )
    execution_ms = int((time.time() - start_time) * 1000)

    if not isinstance(result, dict) or "risk_score" not in result:
        raise ValueError(f"Tool runner returned invalid result: {type(result)}")

    return asdict(ExecuteOutput(
        stdout=json.dumps(result, default=str),
        stderr="\n".join(result.get("errors", [])),
        exit_code=0 if not result.get("errors") else 1,
        status="completed",
        iocs=result.get("iocs", []),
        findings=[{"title": f, "severity": "high"} if isinstance(f, str) else f for f in result.get("findings", [])],
        risk_score=result.get("risk_score", 0),
        recommendations=[],
        execution_ms=execution_ms,
        execution_mode="tools",
        path_d_fallback=False,
        path_d_reason="",
    ))


def _execute_v2_sandbox(data: dict) -> dict:
    """V2 sandbox execution: AST prefilter + Docker sandbox."""
    code = data.get("code", "")

    if not code:
        raise ValueError("No code provided for v2 sandbox")

    # FIX #6: FAST_FILL bypasses Docker isolation — only permit in explicit sandbox mode
    execution_mode = data.get("execution_mode", os.environ.get("ZOVARK_EXECUTION_MODE", "tools"))
    if FAST_FILL and execution_mode != "sandbox":
        raise ValueError(
            "FAST_FILL=true is only permitted when execution_mode=sandbox. "
            "Set ZOVARK_EXECUTION_MODE=sandbox or disable ZOVARK_FAST_FILL."
        )

    # AST prefilter
    is_safe, reason = _ast_check(code)
    if not is_safe:
        raise ValueError(f"AST prefilter blocked: {reason}")

    # Safety wrapper for LLM-generated code (Path C)
    code_source = data.get("source", "")
    if code_source == "llm":
        code = _wrap_code_safely(code)

    activity.logger.info(f"Sandbox policy: {_POLICY_VERSION}, DOCKER_HOST={DOCKER_HOST or 'local-socket'}")

    if FAST_FILL:
        raw = _run_fast_fill(code)
    else:
        raw = _run_in_sandbox(code)

    parsed = _parse_stdout(raw.get("stdout", ""))

    return asdict(ExecuteOutput(
        stdout=raw.get("stdout", ""),
        stderr=raw.get("stderr", ""),
        exit_code=raw.get("exit_code", -1),
        status=raw.get("status", "failed"),
        iocs=parsed.get("iocs", []),
        findings=parsed.get("findings", []),
        risk_score=parsed.get("risk_score", 0),
        recommendations=parsed.get("recommendations", []),
        execution_ms=raw.get("execution_ms", 0),
        execution_mode="sandbox",
        path_d_fallback=False,
        path_d_reason="",
    ))


# --- Main entry point ---
@activity.defn
async def execute_investigation(data: dict) -> dict:
    """
    Stage 3: Execute investigation code (v2 sandbox) or tool plan (v3 in-process).

    Paths:
      tools   → v3 in-process tool runner (default)
      sandbox → v2 Docker sandbox (global feature flag)
      Path D  → per-investigation fallback from v3 to v2 on failure

    Input: {"code": str, "task_type": str} (v2) or {"plan": list, "siem_event": dict} (v3)
    Returns: dict (serializable ExecuteOutput fields)
    """
    execution_mode = data.get("execution_mode", EXECUTION_MODE)

    # Global v2 sandbox mode
    if execution_mode == "sandbox":
        try:
            return _execute_v2_sandbox(data)
        except Exception as e:
            return asdict(ExecuteOutput(
                stderr=str(e)[:500], exit_code=1, status="failed",
                execution_mode="sandbox",
            ))

    # V3 tool-calling mode with Path D fallback
    if data.get("plan"):
        try:
            return _execute_v3_tools(data)
        except Exception as e:
            activity.logger.warning(
                f"v3 tool runner failed, falling back to v2 sandbox (Path D): {e}"
            )
            # Record Path D event in OTEL trace
            try:
                from tracing import get_tracer
                _pd_span = get_tracer().start_span("path_d.fallback")
                _pd_span.set_attribute("path_d.reason", str(e)[:200])
                _pd_span.set_attribute("path_d.task_type", data.get("task_type", ""))
                _pd_span.record_exception(e)
                _pd_span.end()
            except Exception:
                pass
            # Path D: fall back to v2 sandbox for THIS investigation
            try:
                v2_result = _execute_v2_sandbox(data)
                # Tag as Path D fallback
                v2_result["execution_mode"] = "sandbox_fallback"
                v2_result["path_d_fallback"] = True
                v2_result["path_d_reason"] = str(e)[:200]
                return v2_result
            except Exception as e2:
                activity.logger.error(f"Path D v2 fallback also failed: {e2}")
                return asdict(ExecuteOutput(
                    stderr=f"v3: {str(e)[:200]}; v2: {str(e2)[:200]}",
                    exit_code=1,
                    status="failed",
                    findings=[{"title": "Investigation failed", "severity": "critical",
                               "description": f"Both v3 tool runner and v2 sandbox failed"}],
                    risk_score=0,
                    execution_mode="failed",
                    path_d_fallback=True,
                    path_d_reason=f"v3: {str(e)[:100]}; v2: {str(e2)[:100]}",
                ))

    # Fallthrough: v2 sandbox for code-based execution (existing workflows)
    try:
        return _execute_v2_sandbox(data)
    except Exception as e:
        return asdict(ExecuteOutput(
            stderr=str(e)[:500], exit_code=1, status="failed",
            execution_mode="sandbox",
        ))

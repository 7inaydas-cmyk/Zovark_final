"""
Tool runner — executes investigation plans against SIEM events.
Supports variable resolution, conditional branching, timeouts, and error isolation.
Optional: dependency-aware parallel execution (ZOVARK_PARALLEL_TOOLS_ENABLED).
"""
import re
import json
import time
import logging
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError

from tools.catalog import TOOL_CATALOG

_logger = logging.getLogger(__name__)


# --- Condition evaluator (no eval!) ---
_COMPARISON_RE = re.compile(
    r'^\$(\w+(?:\.\w+)?)\s*(>=|<=|!=|>|<|==)\s*(.+)$'
)
_BOOL_RE = re.compile(r'^\$(\w+(?:\.\w+)?)$')


def _resolve_ref(ref: str, step_results: dict, siem_event: dict, raw_log: str,
                 history_context: dict, institutional_knowledge: dict) -> Any:
    """Resolve a variable reference like $raw_log, $siem_event.field, $stepN, $stepN.field, $stepN.count."""
    if not isinstance(ref, str) or not ref.startswith("$"):
        return ref

    var = ref[1:]  # strip leading $

    if var == "raw_log":
        return raw_log
    if var == "siem_event":
        return siem_event
    if var == "correlation_context":
        return history_context
    if var == "institutional_knowledge":
        return institutional_knowledge

    # $siem_event.field
    if var.startswith("siem_event."):
        field = var[len("siem_event."):]
        return siem_event.get(field, "")

    # $stepN or $stepN.field or $stepN.count
    step_match = re.match(r'^step(\d+)(?:\.(\w+))?$', var)
    if step_match:
        step_idx = int(step_match.group(1))
        field = step_match.group(2)
        step_val = step_results.get(step_idx)
        if step_val is None:
            return 0 if field == "count" else None

        if field is None:
            return step_val
        if field == "count":
            if isinstance(step_val, (list, dict)):
                return len(step_val)
            return step_val if isinstance(step_val, (int, float)) else 0
        if isinstance(step_val, dict):
            return step_val.get(field)
        if isinstance(step_val, list):
            return len(step_val) if field == "length" else step_val
        return step_val

    return ref  # unresolved, return as-is


def _resolve_args(args: dict, step_results: dict, siem_event: dict, raw_log: str,
                  history_context: dict, institutional_knowledge: dict) -> dict:
    """Resolve all variable references in a tool's arguments."""
    resolved = {}
    for key, val in args.items():
        if isinstance(val, str) and val.startswith("$"):
            resolved[key] = _resolve_ref(val, step_results, siem_event, raw_log,
                                         history_context, institutional_knowledge)
        elif isinstance(val, list):
            resolved[key] = [
                _resolve_ref(v, step_results, siem_event, raw_log,
                             history_context, institutional_knowledge) if isinstance(v, str) and v.startswith("$") else v
                for v in val
            ]
        else:
            resolved[key] = val
    return resolved


def _evaluate_condition(condition: str, step_results: dict, siem_event: dict,
                        raw_log: str, history_context: dict, institutional_knowledge: dict) -> bool:
    """Evaluate a condition string without eval(). Supports numeric comparisons and boolean checks."""
    condition = condition.strip()

    # Comparison: $stepN > 100, $stepN.count >= 5, etc.
    comp_match = _COMPARISON_RE.match(condition)
    if comp_match:
        var_ref = "$" + comp_match.group(1)
        op = comp_match.group(2)
        rhs_str = comp_match.group(3).strip().strip('"').strip("'")

        lhs = _resolve_ref(var_ref, step_results, siem_event, raw_log,
                           history_context, institutional_knowledge)

        # Parse RHS
        if rhs_str == "null" or rhs_str == "None":
            rhs = None
        elif rhs_str == "true":
            rhs = True
        elif rhs_str == "false":
            rhs = False
        else:
            try:
                rhs = float(rhs_str)
                if rhs == int(rhs):
                    rhs = int(rhs)
            except ValueError:
                rhs = rhs_str

        # Coerce LHS for numeric comparison
        if isinstance(rhs, (int, float)) and not isinstance(lhs, (int, float)):
            try:
                lhs = float(lhs) if lhs is not None else 0
            except (ValueError, TypeError):
                lhs = 0

        try:
            if op == ">":
                return lhs > rhs
            elif op == ">=":
                return lhs >= rhs
            elif op == "<":
                return lhs < rhs
            elif op == "<=":
                return lhs <= rhs
            elif op == "==":
                return lhs == rhs
            elif op == "!=":
                return lhs != rhs
        except TypeError:
            return False

    # Boolean check: $stepN.escalation_recommended
    bool_match = _BOOL_RE.match(condition)
    if bool_match:
        var_ref = "$" + bool_match.group(1)
        val = _resolve_ref(var_ref, step_results, siem_event, raw_log,
                           history_context, institutional_knowledge)
        return bool(val)

    return False


# --- Dependency graph for parallel execution ---
_STEP_REF_PATTERN = re.compile(r'\$step(\d+)')


def _build_dependency_graph(steps: list[dict]) -> dict[int, set[int]]:
    """Parse $stepN references to build dependency DAG.
    Returns {step_index: set_of_dependency_indices} (0-indexed)."""
    deps = {}
    for i, step in enumerate(steps):
        step_str = json.dumps(step)
        step_deps = set()
        for match in _STEP_REF_PATTERN.finditer(step_str):
            ref = int(match.group(1)) - 1  # $stepN is 1-indexed, convert to 0-indexed
            if 0 <= ref < i:
                step_deps.add(ref)
        deps[i] = step_deps
    return deps


def _topological_batches(deps: dict[int, set[int]]) -> list[list[int]]:
    """Convert DAG into ordered batches of parallelizable steps."""
    remaining = set(deps.keys())
    completed = set()
    batches = []

    while remaining:
        ready = {s for s in remaining if deps[s].issubset(completed)}
        if not ready:
            # Circular dep safety net — fall back to sequential for remaining
            batches.append(sorted(remaining))
            break
        batches.append(sorted(ready))
        completed.update(ready)
        remaining -= ready

    return batches


def _run_single_step(i: int, step: dict, step_results: dict,
                     siem_event: dict, raw_log: str,
                     history_context: dict, institutional_knowledge: dict,
                     per_tool_timeout: float,
                     task_id: str, tenant_id: str, trace_id: str,
                     parallel: bool = False) -> dict:
    """Execute a single plan step. Returns {result, tool_name, error, findings, iocs, risk}."""
    # Determine actual step (handle conditional branching)
    actual_step = step
    if "condition" in step:
        cond_result = _evaluate_condition(
            step["condition"], step_results, siem_event, raw_log,
            history_context, institutional_knowledge
        )
        actual_step = step.get("if_true", {}) if cond_result else step.get("if_false", {})
        if not actual_step or "tool" not in actual_step:
            return {"result": None, "tool_name": "", "skipped": True}

    tool_name = actual_step.get("tool", "")
    tool_args = actual_step.get("args", {})

    tool_entry = TOOL_CATALOG.get(tool_name)
    if not tool_entry:
        return {"result": None, "tool_name": tool_name, "error": f"unknown tool '{tool_name}'"}

    resolved_args = _resolve_args(
        tool_args, step_results, siem_event, raw_log,
        history_context, institutional_knowledge
    )

    if task_id:
        try:
            from events import emit_event
            emit_event(task_id, tenant_id, trace_id, "tool_started", {"tool": tool_name, "step": i})
        except Exception:
            pass

    _span = None
    try:
        try:
            from tracing import get_tracer
            _span = get_tracer().start_span(f"tool.{tool_name}")
            _span.set_attribute("tool.name", tool_name)
            _span.set_attribute("tool.step", i)
            _span.set_attribute("tool.parallel", parallel)
        except Exception:
            pass

        # Audit 2.5: run the tool in a dedicated single-worker ThreadPoolExecutor
        # so `future.result(timeout=...)` can actually interrupt a runaway tool.
        # The previous implementation ran the tool synchronously and only
        # measured wall-clock after the fact — a tool that entered `while True:`
        # would block its activity worker until Temporal killed the whole
        # activity. With the executor path, an infinite-loop tool is reported
        # as a step timeout and the next step still runs.
        tool_start = time.monotonic()
        timeout_exceeded = False
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{tool_name}") as _pool:
                _fut = _pool.submit(tool_entry["function"], **resolved_args)
                try:
                    result = _fut.result(timeout=per_tool_timeout)
                except FuturesTimeoutError:
                    timeout_exceeded = True
                    # Cancel best-effort; synchronous work is unkillable at the
                    # Python level, but the worker thread will drain on pool
                    # shutdown. Returning here lets the next step run.
                    _fut.cancel()
                    result = None
        finally:
            tool_elapsed = time.monotonic() - tool_start

        if _span:
            try:
                _span.set_attribute("tool.duration_ms", int(tool_elapsed * 1000))
                _span.set_attribute("tool.success", True)
                if isinstance(result, dict) and "risk_score" in result:
                    _span.set_attribute("tool.risk_score", result["risk_score"])
                if isinstance(result, list):
                    _span.set_attribute("tool.result_count", len(result))
                _span.end()
            except Exception:
                pass

        if task_id:
            try:
                from events import emit_event, tool_summary
                emit_event(task_id, tenant_id, trace_id, "tool_completed", {
                    "tool": tool_name, "step": i,
                    "duration_ms": int(tool_elapsed * 1000),
                    "summary": tool_summary(tool_name, result, int(tool_elapsed * 1000)),
                })
            except Exception:
                pass

        try:
            from metrics import record_tool_execution
            record_tool_execution(tool_name, tool_elapsed, bool(timeout_exceeded))
        except Exception:
            pass

        # Collect aggregates
        findings, iocs, risk = [], [], None
        if isinstance(result, dict):
            findings = result.get("findings", [])
            iocs = result.get("iocs", [])
            risk = result.get("risk_score")
        elif isinstance(result, int) and tool_entry["category"] == "scoring":
            risk = result
        elif isinstance(result, list) and tool_entry["category"] == "extraction":
            iocs = result

        return {
            "result": result, "tool_name": tool_name,
            "findings": findings, "iocs": iocs, "risk": risk,
            "error": f"{tool_name} took {tool_elapsed:.1f}s (limit {per_tool_timeout}s)" if timeout_exceeded else None,
        }

    except Exception as e:
        if _span:
            try:
                _span.set_attribute("tool.success", False)
                _span.record_exception(e)
                _span.end()
            except Exception:
                pass
        try:
            from metrics import record_tool_execution
            record_tool_execution(tool_name, 0.0, True)
        except Exception:
            pass
        return {"result": None, "tool_name": tool_name, "error": f"{tool_name} error: {str(e)[:200]}"}


def execute_plan(plan: list, siem_event: dict,
                 history_context: dict = None, institutional_knowledge: dict = None,
                 total_timeout: float = 30.0, per_tool_timeout: float = 5.0,
                 task_id: str = "", tenant_id: str = "", trace_id: str = "") -> dict:
    """Execute an investigation plan — list of tool steps — against a SIEM event.

    Supports sequential (default) or parallel execution via ZOVARK_PARALLEL_TOOLS_ENABLED.
    Parallel mode uses dependency-aware batching: independent steps run concurrently,
    steps with $stepN references wait for their dependencies.

    Returns:
        dict with: findings, iocs, risk_score, verdict, tools_executed, tool_results, errors
    """
    if history_context is None:
        history_context = {}
    if institutional_knowledge is None:
        institutional_knowledge = {}

    # Check parallel execution flag
    try:
        from settings import settings as _s
        parallel_enabled = _s.parallel_tools_enabled
        max_parallel = _s.max_parallel_tools
    except (ImportError, Exception):
        parallel_enabled = False
        max_parallel = 4

    raw_log = siem_event.get("raw_log", "")
    step_results = {}
    all_findings = []
    all_iocs = []
    risk_scores = []
    tool_names_executed = []
    errors = []
    start_time = time.monotonic()

    # Conditional steps have $stepN refs in their condition string, so the dependency
    # graph correctly places them after their dependencies. Safe to parallelize.
    if parallel_enabled and len(plan) > 1:
        # --- Parallel execution: batch by dependency ---
        deps = _build_dependency_graph(plan)
        batches = _topological_batches(deps)
        _logger.info(f"Parallel execution: {len(plan)} steps in {len(batches)} batches")

        for batch_idx, batch in enumerate(batches):
            if time.monotonic() - start_time > total_timeout:
                errors.append(f"Total timeout ({total_timeout}s) exceeded at batch {batch_idx}")
                break

            if len(batch) == 1:
                # Single step — run directly (no thread overhead)
                i = batch[0]
                out = _run_single_step(
                    i, plan[i], step_results, siem_event, raw_log,
                    history_context, institutional_knowledge, per_tool_timeout,
                    task_id, tenant_id, trace_id, parallel=False,
                )
                if out.get("skipped"):
                    step_results[i + 1] = None
                    continue
                step_results[i + 1] = out["result"]
                if out.get("tool_name"):
                    tool_names_executed.append(out["tool_name"])
                all_findings.extend(out.get("findings", []))
                all_iocs.extend(out.get("iocs", []))
                if out.get("risk") is not None:
                    risk_scores.append(out["risk"])
                if out.get("error"):
                    errors.append(f"Step {i}: {out['error']}")
            else:
                # Multiple independent steps — run in parallel
                with ThreadPoolExecutor(max_workers=min(len(batch), max_parallel)) as pool:
                    futures = {}
                    for i in batch:
                        future = pool.submit(
                            _run_single_step,
                            i, plan[i], step_results, siem_event, raw_log,
                            history_context, institutional_knowledge, per_tool_timeout,
                            task_id, tenant_id, trace_id, True,
                        )
                        futures[future] = i

                    for future in as_completed(futures, timeout=per_tool_timeout * 2):
                        i = futures[future]
                        try:
                            out = future.result()
                        except Exception as e:
                            errors.append(f"Step {i}: thread error: {str(e)[:200]}")
                            step_results[i + 1] = None
                            continue

                        if out.get("skipped"):
                            step_results[i + 1] = None
                            continue
                        step_results[i + 1] = out["result"]
                        if out.get("tool_name"):
                            tool_names_executed.append(out["tool_name"])
                        all_findings.extend(out.get("findings", []))
                        all_iocs.extend(out.get("iocs", []))
                        if out.get("risk") is not None:
                            risk_scores.append(out["risk"])
                        if out.get("error"):
                            errors.append(f"Step {i}: {out['error']}")

    else:
        # --- Sequential execution (original path) ---
        for i, step in enumerate(plan):
            if time.monotonic() - start_time > total_timeout:
                errors.append(f"Total timeout ({total_timeout}s) exceeded at step {i}")
                break

            out = _run_single_step(
                i, step, step_results, siem_event, raw_log,
                history_context, institutional_knowledge, per_tool_timeout,
                task_id, tenant_id, trace_id, parallel=False,
            )
            if out.get("skipped"):
                step_results[i + 1] = None
                continue
            step_results[i + 1] = out["result"]
            if out.get("tool_name"):
                tool_names_executed.append(out["tool_name"])
            all_findings.extend(out.get("findings", []))
            all_iocs.extend(out.get("iocs", []))
            if out.get("risk") is not None:
                risk_scores.append(out["risk"])
            if out.get("error"):
                errors.append(f"Step {i}: {out['error']}")
            if out["result"] is None and not out.get("error"):
                step_results[i + 1] = None

    # Aggregate risk score — use max from scoring/detection tools
    risk_score = max(risk_scores) if risk_scores else 0

    # Apply risk floor rules based on tool outputs
    highest_floor = 0
    for step_idx, result in step_results.items():
        tool_name = tool_names_executed[step_idx - 1] if step_idx <= len(tool_names_executed) else ""
        tool_entry = TOOL_CATALOG.get(tool_name)

        # count_pattern thresholds
        if tool_name == "count_pattern" and isinstance(result, int):
            if result >= 200:
                highest_floor = max(highest_floor, 75)
            elif result >= 50:
                highest_floor = max(highest_floor, 60)

        # score_brute_force floor
        if tool_name == "score_brute_force" and isinstance(result, int):
            if result > 0:
                highest_floor = max(highest_floor, 50)

        # detection tool true_positive verdict floor
        if tool_entry and tool_entry.get("category") == "detection" and isinstance(result, dict):
            if result.get("verdict") == "true_positive":
                highest_floor = max(highest_floor, 55)

    risk_score = max(risk_score, highest_floor)

    # Deduplicate IOCs by value
    seen_iocs = set()
    unique_iocs = []
    for ioc in all_iocs:
        val = ioc.get("value", "") if isinstance(ioc, dict) else str(ioc)
        if val and val not in seen_iocs:
            seen_iocs.add(val)
            unique_iocs.append(ioc)

    # Derive verdict
    verdict = _derive_verdict(risk_score, len(unique_iocs), len(all_findings))

    return {
        "findings": all_findings,
        "iocs": unique_iocs,
        "risk_score": risk_score,
        "verdict": verdict,
        "tools_executed": len(tool_names_executed),
        "tool_names": tool_names_executed,
        "tool_results": {k: _safe_serialize(v) for k, v in step_results.items()},
        "errors": errors,
    }


def _derive_verdict(risk_score: int, ioc_count: int, finding_count: int) -> str:
    from verdict import derive_verdict
    return derive_verdict(risk_score, ioc_count, finding_count, execution_mode="tools")


def _safe_serialize(val):
    """Make a value JSON-serializable for storage."""
    if isinstance(val, (str, int, float, bool, type(None))):
        return val
    if isinstance(val, (list, tuple)):
        return [_safe_serialize(v) for v in val[:50]]  # cap at 50 items
    if isinstance(val, dict):
        return {str(k): _safe_serialize(v) for k, v in list(val.items())[:50]}
    return str(val)[:500]

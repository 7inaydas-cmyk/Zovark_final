"""Model evaluation — fixed reference investigations and optional pair (quant) stability."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

ZOVARK_LLM_ENDPOINT = os.environ.get(
    "ZOVARK_LLM_ENDPOINT",
    "http://zovark-inference:8080/v1/chat/completions",
)
# stabilize-runtime-hygiene: centralized LLM key loading.
# Pydantic raises ValidationError if ZOVARK_LLM_KEY is unset.
from settings import settings as _settings
ZOVARK_LLM_KEY = _settings.llm_key.get_secret_value()

_REFERENCE_LIMIT = 50


def _plans_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tools" / "investigation_plans.json"


def load_reference_cases(limit: int = _REFERENCE_LIMIT) -> List[Dict[str, Any]]:
    """Build exactly ``limit`` reference cases from investigation_plans.json (cycles keys if needed)."""
    path = _plans_path()
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as f:
        plans = json.load(f)
    keys = [(k, v) for k, v in plans.items() if isinstance(v, dict)]
    if not keys:
        return []
    cases: List[Dict[str, Any]] = []
    idx = 0
    while len(cases) < limit:
        plan_key, spec = keys[idx % len(keys)]
        desc = str(spec.get("description", plan_key))
        r = (idx % 200) + 1
        cases.append(
            {
                "id": f"ref_{plan_key}_{idx}",
                "task_type": plan_key,
                "expect_attack": True,
                "siem_event": {
                    "title": plan_key.replace("_", " ").title(),
                    "rule_name": plan_key,
                    "task_type": plan_key,
                    "source_ip": f"10.0.1.{r}",
                    "raw_log": f"{desc} | test reference event | malicious lateral exfil c2 phishing | 10.0.1.{r}",
                },
            }
        )
        idx += 1
    return cases


def _parse_verdict_json(content: str) -> Tuple[str, int]:
    """Extract verdict + risk from model output."""
    text = (content or "").strip()
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            v = str(obj.get("verdict", "")).lower().strip()
            r = int(obj.get("risk_score", 0))
            return v, max(0, min(100, r))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    vlow = text.lower()
    for token in (
        "needs_manual_review",
        "true_positive",
        "false_positive",
        "suspicious",
        "benign",
    ):
        if token in vlow:
            return token, 50
    return "unknown", 0


def _call_assess(case: Dict[str, Any], model: str) -> Tuple[str, int, int, float]:
    """Return verdict, risk_score, total_tokens, latency_s."""
    siem = case["siem_event"]
    user = (
        "You are a SOC assistant. Respond with JSON only (no markdown):\n"
        '{"verdict":"true_positive|false_positive|suspicious|benign|needs_manual_review",'
        '"risk_score": <integer 0-100>}\n'
        f"SIEM event: {json.dumps(siem, ensure_ascii=False)}"
    )
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Output compact JSON only. No prose.",
            },
            {"role": "user", "content": user},
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    start = time.time()
    resp = httpx.post(
        ZOVARK_LLM_ENDPOINT,
        headers={
            "Authorization": f"Bearer {ZOVARK_LLM_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    latency = time.time() - start
    content = data["choices"][0]["message"]["content"]
    tokens = data.get("usage", {}).get("total_tokens", 0)
    v, r = _parse_verdict_json(content)
    return v, r, tokens, latency


def evaluate_model(model_name: str = "fast") -> dict:
    """Run reference investigations on one model; score vs attack/benign expectation."""
    cases = load_reference_cases()
    if not cases:
        return {
            "model": model_name,
            "benchmark_count": 0,
            "average_score": 0.0,
            "total_tokens": 0,
            "total_latency_ms": 0,
            "results": [],
            "error": "no_reference_cases",
        }

    results = []
    total_score = 0.0
    total_tokens = 0
    total_latency = 0.0

    for case in cases:
        try:
            verdict, risk, tok, lat = _call_assess(case, model_name)
        except Exception as e:
            results.append(
                {
                    "id": case["id"],
                    "task_type": case["task_type"],
                    "score": 0.0,
                    "error": str(e)[:300],
                }
            )
            continue

        if case.get("expect_attack"):
            ok = verdict in ("true_positive", "suspicious", "needs_manual_review") or risk >= 40
            score = 1.0 if ok else 0.0
        else:
            ok = verdict in ("benign", "false_positive") and risk <= 35
            score = 1.0 if ok else 0.3

        total_score += score
        total_tokens += tok
        total_latency += lat
        results.append(
            {
                "id": case["id"],
                "task_type": case["task_type"],
                "score": round(score, 3),
                "verdict": verdict,
                "risk_score": risk,
                "tokens": tok,
                "latency_ms": round(lat * 1000),
            }
        )

    n = len(cases)
    return {
        "model": model_name,
        "benchmark_count": n,
        "average_score": round(total_score / n, 3) if n else 0.0,
        "total_tokens": total_tokens,
        "total_latency_ms": round(total_latency * 1000),
        "results": results,
    }


def evaluate_model_pair(
    baseline_model: str,
    candidate_model: str,
    limit: int = _REFERENCE_LIMIT,
) -> dict:
    """Compare verdicts between two model IDs (e.g. GGUF F16 vs quantized). Fails on any flip."""
    cases = load_reference_cases(limit)
    flips: List[dict] = []
    errors: List[dict] = []

    for case in cases:
        try:
            vb, _, _, _ = _call_assess(case, baseline_model)
            vc, _, _, _ = _call_assess(case, candidate_model)
        except Exception as e:
            errors.append({"id": case["id"], "error": str(e)[:200]})
            continue

        if vb != vc:
            flips.append(
                {
                    "id": case["id"],
                    "task_type": case["task_type"],
                    "baseline_verdict": vb,
                    "candidate_verdict": vc,
                }
            )

    n = len(cases)
    compared_ok = n - len(errors)
    passed = (
        len(flips) == 0
        and len(errors) == 0
        and n == limit
        and compared_ok == limit
    )
    return {
        "baseline_model": baseline_model,
        "candidate_model": candidate_model,
        "reference_case_count": n,
        "cases_compared": compared_ok,
        "errors": errors,
        "verdict_flips": flips,
        "passed": passed,
    }

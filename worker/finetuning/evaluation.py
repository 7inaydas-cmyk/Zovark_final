"""Fine-tuning evaluation metrics activity (Issue #35).

Extended evaluation: BLEU score, accuracy comparison against labeled ground truth,
and regression detection (new model vs baseline).
"""

import os
import math
import psycopg2
from psycopg2.extras import RealDictCursor
from temporalio import activity

from finetuning.evaluator import evaluate_model


def _get_db():
    # stabilize-runtime-hygiene: centralized DB URL via settings; no fallback literal.
    from settings import settings as _settings
    db_url = os.environ.get("DATABASE_URL", _settings.database_url)
    return psycopg2.connect(db_url)


def compute_bleu(reference, hypothesis, max_n=4):
    """Compute a simplified BLEU score (no smoothing, unigram to n-gram).

    Args:
        reference: Reference text string
        hypothesis: Hypothesis text string
        max_n: Maximum n-gram order (default 4)

    Returns:
        float: BLEU score between 0.0 and 1.0
    """
    if not reference or not hypothesis:
        return 0.0

    ref_tokens = reference.lower().split()
    hyp_tokens = hypothesis.lower().split()

    if not ref_tokens or not hyp_tokens:
        return 0.0

    # Brevity penalty
    bp = min(1.0, len(hyp_tokens) / len(ref_tokens)) if ref_tokens else 0.0

    precisions = []
    for n in range(1, max_n + 1):
        ref_ngrams = _get_ngrams(ref_tokens, n)
        hyp_ngrams = _get_ngrams(hyp_tokens, n)

        if not hyp_ngrams:
            precisions.append(0.0)
            continue

        # Count matches
        matches = 0
        ref_ngram_counts = {}
        for ng in ref_ngrams:
            ref_ngram_counts[ng] = ref_ngram_counts.get(ng, 0) + 1

        for ng in hyp_ngrams:
            if ref_ngram_counts.get(ng, 0) > 0:
                matches += 1
                ref_ngram_counts[ng] -= 1

        precision = matches / len(hyp_ngrams)
        precisions.append(precision)

    if not precisions or all(p == 0 for p in precisions):
        return 0.0

    # Geometric mean of precisions (with smoothing for zero)
    log_avg = 0.0
    weight = 1.0 / len(precisions)
    for p in precisions:
        if p == 0:
            p = 1e-10  # smoothing
        log_avg += weight * math.log(p)

    bleu = bp * math.exp(log_avg)
    return round(min(bleu, 1.0), 4)


def _get_ngrams(tokens, n):
    """Extract n-grams from token list."""
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


@activity.defn
async def compute_eval_metrics(data: dict) -> dict:
    """Compute comprehensive evaluation metrics for model quality.

    Input: {
        model: str (model to evaluate),
        baseline_model: str (model to compare against),
        tenant_id: optional
    }
    Returns: {
        model: str,
        bleu_scores: {avg, per_type: {task_type: score}},
        accuracy: {overall, per_type: {task_type: {correct, total, rate}}},
        regression: {detected: bool, degraded_types: [...], improved_types: [...]},
        benchmark_results: {...}
    }
    """
    model = data.get("model", "fast")
    baseline_model = data.get("baseline_model", "fast")
    tenant_id = data.get("tenant_id")

    # 1. Run benchmark evaluation on target model
    eval_results = evaluate_model(model_name=model)

    # 2. Compute BLEU scores against labeled ground truth
    bleu_scores = _compute_bleu_scores(tenant_id)

    # 3. Compute accuracy from investigation feedback
    accuracy = _compute_accuracy(tenant_id)

    # 4. Regression detection (compare with baseline if different)
    regression = {"detected": False, "degraded_types": [], "improved_types": []}
    if model != baseline_model:
        baseline_results = evaluate_model(model_name=baseline_model)
        regression = _detect_regression(eval_results, baseline_results)

    return {
        "model": model,
        "bleu_scores": bleu_scores,
        "accuracy": accuracy,
        "regression": regression,
        "benchmark_results": {
            "average_score": eval_results.get("average_score", 0),
            "benchmark_count": eval_results.get("benchmark_count", 0),
            "total_tokens": eval_results.get("total_tokens", 0),
        },
    }


def _compute_bleu_scores(tenant_id=None):
    """Compute BLEU scores by comparing investigation outputs to labeled examples."""
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get investigations with feedback (as reference quality signals)
            query = """
                SELECT i.summary, i.verdict, i.task_type,
                       f.notes as reference_notes
                FROM investigations i
                JOIN investigation_feedback f ON f.investigation_id = i.id
                WHERE f.verdict_correct = true
                  AND i.summary IS NOT NULL
                  AND f.notes IS NOT NULL
            """
            params = []
            if tenant_id:
                query += " AND i.tenant_id = %s"
                params.append(tenant_id)

            query += " LIMIT 200"
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            return {"avg": 0.0, "per_type": {}, "sample_count": 0}

        type_scores = {}
        all_scores = []

        for row in rows:
            summary = row.get("summary", "")
            reference = row.get("reference_notes", "")
            task_type = row.get("task_type", "unknown")

            score = compute_bleu(reference, summary)
            all_scores.append(score)

            if task_type not in type_scores:
                type_scores[task_type] = []
            type_scores[task_type].append(score)

        avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0

        per_type = {}
        for tt, scores in type_scores.items():
            per_type[tt] = round(sum(scores) / len(scores), 4) if scores else 0.0

        return {
            "avg": round(avg_score, 4),
            "per_type": per_type,
            "sample_count": len(all_scores),
        }

    finally:
        conn.close()


def _compute_accuracy(tenant_id=None):
    """Compute verdict accuracy from investigation feedback."""
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT
                    i.task_type,
                    f.verdict_correct,
                    f.false_positive,
                    f.missed_threat
                FROM investigation_feedback f
                JOIN investigations i ON i.id = f.investigation_id
                WHERE f.verdict_correct IS NOT NULL
            """
            params = []
            if tenant_id:
                query += " AND f.tenant_id = %s"
                params.append(tenant_id)

            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            return {"overall": 0.0, "per_type": {}, "total": 0}

        total = len(rows)
        correct = sum(1 for r in rows if r.get("verdict_correct"))
        overall = correct / total if total > 0 else 0.0

        type_stats = {}
        for row in rows:
            tt = row.get("task_type", "unknown")
            if tt not in type_stats:
                type_stats[tt] = {"correct": 0, "total": 0}
            type_stats[tt]["total"] += 1
            if row.get("verdict_correct"):
                type_stats[tt]["correct"] += 1

        per_type = {}
        for tt, stats in type_stats.items():
            per_type[tt] = {
                "correct": stats["correct"],
                "total": stats["total"],
                "rate": round(stats["correct"] / stats["total"], 3) if stats["total"] > 0 else 0.0,
            }

        return {
            "overall": round(overall, 3),
            "per_type": per_type,
            "total": total,
        }

    finally:
        conn.close()


def _detect_regression(new_results, baseline_results):
    """Detect performance regression between new and baseline model evaluations."""
    new_per_type = {}
    for r in new_results.get("results", []):
        new_per_type[r["task_type"]] = r.get("score", 0)

    baseline_per_type = {}
    for r in baseline_results.get("results", []):
        baseline_per_type[r["task_type"]] = r.get("score", 0)

    degraded = []
    improved = []
    threshold = 0.05  # 5% threshold for significance

    for task_type in set(new_per_type.keys()) | set(baseline_per_type.keys()):
        new_score = new_per_type.get(task_type, 0)
        baseline_score = baseline_per_type.get(task_type, 0)
        diff = new_score - baseline_score

        if diff < -threshold:
            degraded.append({
                "task_type": task_type,
                "new_score": round(new_score, 3),
                "baseline_score": round(baseline_score, 3),
                "diff": round(diff, 3),
            })
        elif diff > threshold:
            improved.append({
                "task_type": task_type,
                "new_score": round(new_score, 3),
                "baseline_score": round(baseline_score, 3),
                "diff": round(diff, 3),
            })

    return {
        "detected": len(degraded) > 0,
        "degraded_types": degraded,
        "improved_types": improved,
        "overall_diff": round(
            new_results.get("average_score", 0) - baseline_results.get("average_score", 0),
            3
        ),
    }

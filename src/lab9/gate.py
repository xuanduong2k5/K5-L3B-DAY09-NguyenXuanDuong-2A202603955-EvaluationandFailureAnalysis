"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems = []
    metrics = pr.get("metrics") or {}
    metric_ids = [metric["id"] for metric in spec.get("metrics", [])]
    for metric_id in metric_ids:
        if metric_id not in metrics:
            problems.append(f"missing aggregate metric {metric_id}")
        elif metrics[metric_id] is None:
            problems.append(f"missing aggregate value for {metric_id}")
    for metric_id, value in metrics.items():
        if isinstance(value, (int, float)) and not math.isfinite(value):
            problems.append(f"non-finite aggregate metric {metric_id}")
    if pr.get("evaluator_failures"):
        problems.append("evaluator_failures is non-empty")

    rows = pr.get("rows") or []
    manifest = pr.get("manifest") or {}
    seeds = manifest.get("seeds") or []
    expected_count = manifest.get("items")
    if not isinstance(expected_count, int) or len(rows) != expected_count * len(seeds):
        problems.append("row count does not match manifest items x seeds")

    def row_map(report: dict) -> dict[tuple[str, int], dict]:
        result = {}
        for row in report.get("rows") or []:
            key = (row.get("item_id"), row.get("seed"))
            if key in result:
                problems.append(f"duplicate row for {key}")
            result[key] = row
        return result

    pr_rows = row_map(pr)
    base_rows = row_map(base)
    row_metric_ids = {
        metric["id"] for metric in spec.get("metrics", [])
        if metric.get("grader") not in {"lab8.metrics.summarize", "lab9.stats.pass_hat_k"}
    }
    if set(pr_rows) != set(base_rows):
        problems.append("item/seed rows differ from baseline")
    for key, row in pr_rows.items():
        for metric_id in row_metric_ids:
            if metric_id not in row or row[metric_id] is None:
                problems.append(f"{key}: missing grader value for {metric_id}")
            elif isinstance(row[metric_id], (int, float)) and not math.isfinite(row[metric_id]):
                problems.append(f"{key}: non-finite grader value for {metric_id}")
        base_row = base_rows.get(key)
        if base_row is not None and set(row.get("slices") or []) != set(base_row.get("slices") or []):
            problems.append(f"{key}: slice tags differ from baseline")

    if base_spec and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        current_metrics = {metric["id"]: metric for metric in spec.get("metrics", [])}
        current_slices = {
            (metric.get("name"), metric.get("metric")): metric
            for metric in spec.get("slices", [])
        }

        def check_ratchet(old: dict, new: dict, label: str) -> None:
            old_threshold = old.get("threshold", old)
            new_threshold = new.get("threshold", new)
            for bound in ("min", "max"):
                if bound in old_threshold and (
                    bound not in new_threshold
                    or (bound == "min" and new_threshold[bound] < old_threshold[bound])
                    or (bound == "max" and new_threshold[bound] > old_threshold[bound])
                ):
                    problems.append(f"{label}: loosened or removed {bound} threshold")
            if old_threshold.get("no_significant_drop") and not new_threshold.get("no_significant_drop"):
                problems.append(f"{label}: removed no_significant_drop")
            if old.get("blocking") and not new.get("blocking"):
                problems.append(f"{label}: blocking changed to non-blocking")

        for old in base_spec.get("metrics", []):
            current = current_metrics.get(old["id"])
            if current is None:
                problems.append(f"ratchet: removed metric {old['id']}")
            else:
                check_ratchet(old, current, f"metric {old['id']}")
        for key, old in {
            (metric.get("name"), metric.get("metric")): metric
            for metric in base_spec.get("slices", [])
        }.items():
            current = current_slices.get(key)
            if current is None:
                problems.append(f"ratchet: removed slice {key[0]}/{key[1]}")
            else:
                check_ratchet(old, current, f"slice {key[0]}/{key[1]}")
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bootstrap_spec = spec.get("bootstrap", {})
    for item in spec.get("slices", []):
        name, metric_id = item["name"], item["metric"]
        current = per_item(pr, metric_id, slice_name=name)
        baseline = per_item(base, metric_id, slice_name=name)
        keys = sorted(set(current) & set(baseline))
        value = sum(current.values()) / len(current) if current else None
        reasons = []
        if value is None:
            reasons.append("no rows")
        elif "min" in item and value < item["min"]:
            reasons.append(f"{value:.3f} < min {item['min']}")
        note = ""
        if item.get("no_significant_drop"):
            if not keys:
                reasons.append("no paired items")
            else:
                delta, lo, hi = stats.paired_bootstrap(
                    [current[key] for key in keys],
                    [baseline[key] for key in keys],
                    n_resamples=bootstrap_spec.get("n_resamples", 1000),
                    rng=bootstrap_spec.get("rng", 2027),
                    confidence=bootstrap_spec.get("confidence", 0.95),
                )
                note = f"Δ {delta:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    reasons.append("significant drop")
        verdict = "BLOCK" if reasons and item.get("blocking") else ("warn" if reasons else "ok")
        label = f"slice:{name}/{metric_id}"
        if reasons and item.get("blocking"):
            blocked.append(label)
        lines.append(
            f"  {label:<34} {value!s:<8} {note:<28} {verdict}"
            + (f" ({'; '.join(reasons)})" if reasons else "")
        )
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

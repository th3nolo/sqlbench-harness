#!/usr/bin/env python3
"""Build compact reports for external SQL benchmark runs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from benchmark_registry import ROOT


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_run(value: str) -> Path:
    path = Path(value)
    candidates = [path]
    if not path.is_absolute():
        candidates += [ROOT / path, ROOT / "results" / "benchmarks" / path]
        candidates += list((ROOT / "results" / "benchmarks").glob(f"*/*{path.name}*"))
    for candidate in candidates:
        if (candidate / "run_metadata.json").exists():
            return candidate.resolve()
    raise SystemExit(f"could not resolve benchmark run: {value}")


def money(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):.6f}"


def pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}%"


def row_for_run(path: Path) -> dict[str, Any]:
    metadata = load_json(path / "run_metadata.json")
    summary = load_json(path / "summary_execution.json")
    audit = load_json(path / "fairness_audit.json")
    usage = metadata.get("usage") or {}
    exact = summary.get("exact_matches")
    cost = metadata.get("estimated_cost_usd")
    return {
        "benchmark": metadata.get("benchmark"),
        "display_name": metadata.get("display_name"),
        "mode": metadata.get("mode"),
        "dialect": metadata.get("dialect"),
        "track": metadata.get("track"),
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "run": path.name,
        "run_dir": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "total_items": metadata.get("total_items"),
        "total_evaluated": summary.get("total_evaluated"),
        "skipped": summary.get("skipped"),
        "exact_matches": exact,
        "execution_accuracy_pct": summary.get("execution_accuracy_pct"),
        "prediction_error_count": summary.get("prediction_error_count"),
        "estimated_cost_usd": cost,
        "cost_per_exact_match_usd": (float(cost) / exact) if cost is not None and exact else None,
        "total_tokens": usage.get("total_tokens"),
        "fairness_classification": audit.get("classification", "not-audited"),
        "score_eligible": audit.get("score_eligible"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--output-stem", default=None)
    args = parser.parse_args()

    rows = [row_for_run(resolve_run(run)) for run in args.runs]
    rows.sort(
        key=lambda row: (
            str(row.get("benchmark") or ""),
            str(row.get("track") or ""),
            row.get("execution_accuracy_pct") if row.get("execution_accuracy_pct") is not None else -1,
        ),
        reverse=True,
    )
    output_stem = Path(args.output_stem) if args.output_stem else ROOT / "results" / f"multi_benchmark_report_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not output_stem.is_absolute():
        output_stem = ROOT / output_stem
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows}
    out_json = output_stem.with_suffix(".json")
    out_md = output_stem.with_suffix(".md")
    out_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Multi-Benchmark SQL Results",
        "",
        "Rows are grouped by benchmark and track. Do not compare plain SQL and agentic DBT rows as one leaderboard.",
        "",
    ]
    groups: list[tuple[str, str]] = []
    for row in rows:
        key = (row.get("benchmark") or "unknown", row.get("track") or "unknown")
        if key not in groups:
            groups.append(key)
    for benchmark, track in groups:
        lines += [
            f"## {benchmark} - {track}",
            "",
            "| Model | Provider | Exact | Exec acc | Cost | Cost / exact | Tokens | Pred errors | Fairness | Run |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
        for row in [item for item in rows if (item.get("benchmark") or "unknown", item.get("track") or "unknown") == (benchmark, track)]:
            exact = "n/a"
            if row.get("exact_matches") is not None:
                exact = f"{row['exact_matches']}/{row.get('total_evaluated') or 0}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row.get("model") or "unknown"),
                        str(row.get("provider") or "unknown"),
                        exact,
                        pct(row.get("execution_accuracy_pct")),
                        money(row.get("estimated_cost_usd")),
                        money(row.get("cost_per_exact_match_usd")),
                        str(row.get("total_tokens") if row.get("total_tokens") is not None else "n/a"),
                        str(row.get("prediction_error_count") if row.get("prediction_error_count") is not None else "n/a"),
                        str(row.get("fairness_classification") or "not-audited"),
                        f"`{row['run']}`",
                    ]
                )
                + " |"
            )
        lines.append("")
    lines += [
        "Notes:",
        "- `n/a` execution accuracy usually means the benchmark is agentic, gold SQL is unavailable locally, or local DB setup is incomplete.",
        "- Score-ineligible or diagnostic rows should be separated before publishing leaderboard claims.",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


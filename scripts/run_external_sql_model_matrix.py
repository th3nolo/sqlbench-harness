#!/usr/bin/env python3
"""Run an external SQL benchmark matrix and report results."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def latest_run_dir(benchmark: str, model: str, track: str) -> Path | None:
    safe_model = "".join(ch if ch.isalnum() or ch in "_.-" else "__" for ch in model).strip("_")
    candidates = sorted(
        (RESULTS / "benchmarks" / benchmark).glob(f"openrouter__{safe_model}-{benchmark}-{track}-log-*"),
        key=lambda path: path.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def load_models(config_path: Path) -> list[str]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return [model for model in payload.get("models", []) if isinstance(model, str) and "/" in model]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/external_sql_models_20260703.json")
    parser.add_argument("--benchmarks", nargs="+", default=["bird-mini-dev", "kaggledbqa"])
    parser.add_argument("--track", default="schema-plan", choices=["raw", "schema-plan"])
    parser.add_argument("--limit", type=int, default=None, help="cases per benchmark/model for a bounded evaluation")
    parser.add_argument("--full", action="store_true", help="run each benchmark's full local split")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--output-stem", default=None)
    args = parser.parse_args()
    if args.full and args.limit is not None:
        parser.error("--full and --limit are mutually exclusive")
    if not args.full and args.limit is None:
        parser.error("choose an evaluation scope: pass --limit N for a bounded evaluation or --full for the full-split evaluation")

    case_limit = None if args.full else args.limit

    models = load_models(ROOT / args.config)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_stem = args.output_stem or f"results/external_sql_matrix_{args.track}_{stamp}"
    matrix: dict[str, Any] = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": args.config,
        "benchmarks": args.benchmarks,
        "models": models,
        "track": args.track,
        "limit": case_limit,
        "scope": "full" if args.full else "bounded",
        "runs": [],
    }

    exit_code = 0
    run_dirs: list[Path] = []
    for benchmark in args.benchmarks:
        for model in models:
            run_cmd = [
                str(ROOT / "scripts" / "bb"),
                "benchmark",
                "run",
                "--name",
                benchmark,
                "--model",
                model,
                "--provider",
                "openrouter",
                "--track",
                args.track,
                "--workers",
                str(args.workers),
                "--max-tokens",
                str(args.max_tokens),
            ]
            if case_limit is not None:
                run_cmd += ["--limit", str(case_limit)]
            result = run(run_cmd)
            print(result.stdout, end="")
            run_dir = latest_run_dir(benchmark, model, args.track)
            item: dict[str, Any] = {
                "benchmark": benchmark,
                "model": model,
                "run_exit_code": result.returncode,
                "run_dir": str(run_dir.relative_to(ROOT)) if run_dir else None,
            }
            if result.returncode != 0:
                item["run_error_excerpt"] = result.stdout[-4000:]
                exit_code = 1
            if run_dir:
                run_dirs.append(run_dir)
                eval_result = run(
                    [
                        str(ROOT / "scripts" / "bb"),
                        "benchmark",
                        "eval",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
                print(eval_result.stdout, end="")
                item["eval_exit_code"] = eval_result.returncode
                if eval_result.returncode != 0:
                    item["eval_error_excerpt"] = eval_result.stdout[-4000:]
                    exit_code = 1
            matrix["runs"].append(item)

    if run_dirs:
        report_cmd = [
            str(ROOT / "scripts" / "bb"),
            "benchmark",
            "report",
            "--runs",
            *[str(path) for path in run_dirs],
            "--output-stem",
            output_stem,
        ]
        report_result = run(report_cmd)
        print(report_result.stdout, end="")
        matrix["report_exit_code"] = report_result.returncode
        matrix["report_stem"] = output_stem
        if report_result.returncode != 0:
            matrix["report_error_excerpt"] = report_result.stdout[-4000:]
            exit_code = 1

    matrix_path = ROOT / f"{output_stem}.matrix.json"
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {matrix_path.relative_to(ROOT)}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

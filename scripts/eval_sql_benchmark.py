#!/usr/bin/env python3
"""Evaluate registered SQL benchmark runs against local SQLite databases."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from benchmark_registry import ROOT, canonical_name, load_cases


SQL_PERMISSION_DENIED = "dangerous SQL refused"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().strip(";").lower())


RESULT_COMPARISON_VERSION = "sqlite-result-v2"

# SQLite lexical forms, not a SQL grammar. Quoted tokens and comments must be
# consumed whole so their contents cannot affect parenthesis depth or keywords.
SQL_TOKENS = re.compile(
    r"--[^\n]*(?:\n|$)|/\*[\s\S]*?(?:\*/|$)"
    r"|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
    r"|[\w$]+|[^\s]",
    re.UNICODE,
)


def has_outer_order_by(sql: str) -> bool:
    """Detect outer SQLite ordering, excluding CTEs, subqueries and windows.

    Used only for successfully executed gold SQL; SQLite validates the syntax.
    This deliberately does not infer ordering from the prediction or question.
    """
    depth = 0
    previous = None
    for match in SQL_TOKENS.finditer(sql):
        token = match.group()
        if token.startswith(("--", "/*")):
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and previous == "ORDER" and token.upper() == "BY":
            return True
        previous = token.upper() if depth == 0 else None
    return False


def normalize_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    """Retain SQLite values and row order; comparison is a separate operation."""
    return [list(row) for row in rows]


def compare_rows(gold: list[list[Any]], predicted: list[list[Any]], *, ordered: bool) -> bool:
    """Compare sequences or duplicate-preserving bags, with positional columns.

    SQLite-returned None, str and bytes remain distinct. INTEGER and REAL compare
    by exact numeric value (1 == 1.0), without rounding or string coercion.
    """
    if ordered:
        return gold == predicted
    return Counter(map(tuple, gold)) == Counter(map(tuple, predicted))


def execute_sqlite(db_path: Path, sql: str, *, instruction_limit: int = 500_000) -> tuple[list[list[Any]] | None, str | None]:
    if not sql.strip():
        return None, "empty SQL"
    conn = None
    permission_denied = False
    try:
        # URI escaping keeps filename characters such as '?' and '#' literal.
        # mode=ro also refuses missing databases instead of creating them.
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")

        def authorize(action: int, arg1: str | None, arg2: str | None,
                      database: str | None, source: str | None) -> int:
            nonlocal permission_denied
            # Fail closed for writes, ATTACH, PRAGMA, transactions, and any
            # future operation. SQL text matching is not a permission boundary.
            if action == sqlite3.SQLITE_FUNCTION:
                if (arg2 or "").lower() != "load_extension":
                    return sqlite3.SQLITE_OK
            elif action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            permission_denied = True
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorize)
        remaining = {"count": instruction_limit}

        def progress_handler() -> int:
            remaining["count"] -= 1
            return 1 if remaining["count"] <= 0 else 0

        conn.set_progress_handler(progress_handler, 100)
        cursor = conn.execute(sql)
        if cursor.description is None:
            return None, SQL_PERMISSION_DENIED
        rows = cursor.fetchall()
        return normalize_rows(rows), None
    except Exception as exc:
        if permission_denied:
            return None, SQL_PERMISSION_DENIED
        return None, str(exc)
    finally:
        if conn is not None:
            conn.close()


def load_run_cases(run_dir: Path, benchmark: str, split: str) -> list[dict[str, Any]]:
    metadata = read_json(run_dir / "run_metadata.json")
    case_ids = set(metadata.get("case_ids") or [])
    cases = load_cases(benchmark, split=split)
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
    return cases


def audit_run(run_dir: Path, cases: list[dict[str, Any]], run_metadata: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not run_metadata.get("prompt_set_sha256"):
        findings.append({"severity": "medium", "code": "missing_prompt_set_hash", "message": "run metadata lacks prompt_set_sha256"})
    if run_metadata.get("provider") == "prompt-only":
        findings.append({"severity": "low", "code": "prompt_only", "message": "prompt-only run has no model outputs"})
    if run_metadata.get("provider") == "droid" and run_metadata.get("track") != "bounded-agent" and "schema-plan" not in str(run_metadata.get("track")):
        findings.append({"severity": "low", "code": "agent_track_unclear", "message": "Droid run should be reported as bounded-agent track"})

    cases_by_id = {case["id"]: case for case in cases}
    for prompt_path in sorted(run_dir.glob("*/prompt.txt")):
        case = cases_by_id.get(prompt_path.parent.name)
        if not case:
            continue
        gold_sql = normalize_sql(case.get("gold_sql", ""))
        if len(gold_sql) < 40:
            continue
        prompt_norm = normalize_sql(prompt_path.read_text(encoding="utf-8", errors="ignore"))
        if gold_sql and gold_sql in prompt_norm:
            findings.append(
                {
                    "severity": "critical",
                    "code": "gold_sql_in_prompt",
                    "message": "Prompt contains exact gold SQL.",
                    "case_id": case["id"],
                    "file": str(prompt_path.relative_to(ROOT)),
                }
            )

    has_blocking = any(item["severity"] in {"critical", "high"} for item in findings)
    classification = "diagnostic_contaminated" if has_blocking else "fair"
    if run_metadata.get("track") in {"schema-plan", "raw+retrieval-context", "bounded-agent+schema-plan"} and not has_blocking:
        classification = "fair_with_tools"
    if run_metadata.get("mode") == "agentic_dbt" and not has_blocking:
        classification = "agentic_fair"
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "classification": classification,
        "score_eligible": not has_blocking and run_metadata.get("provider") != "prompt-only",
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--benchmark")
    parser.add_argument("--split", default="test")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    run_metadata = read_json(run_dir / "run_metadata.json")
    benchmark = canonical_name(args.benchmark or run_metadata["benchmark"])
    cases = load_run_cases(run_dir, benchmark, args.split)

    details = []
    for case in cases:
        case_dir = run_dir / case["id"]
        pred_path = case_dir / "predicted.sql"
        pred_sql = pred_path.read_text(encoding="utf-8", errors="ignore") if pred_path.exists() else ""
        gold_sql = case.get("gold_sql", "")
        db_raw = case.get("db_path") or ""
        db_path = Path(db_raw) if db_raw else None
        row: dict[str, Any] = {
            "id": case["id"],
            "db_id": case.get("db_id"),
            "mode": case.get("mode"),
            "dialect": case.get("dialect"),
            "has_prediction": bool(pred_sql.strip()),
            "dangerous_sql": False,
            "skipped": False,
            "skip_reason": None,
            "exact_match": False,
            "gold_error": None,
            "pred_error": None,
            "result_comparison": None,
        }
        if case.get("mode") != "sql":
            row["skipped"] = True
            row["skip_reason"] = "agentic benchmark; use benchmark-native evaluator"
        elif db_path is None or not db_path.exists():
            row["skipped"] = True
            row["skip_reason"] = "local sqlite database not found"
        elif not gold_sql.strip():
            row["skipped"] = True
            row["skip_reason"] = "gold SQL unavailable"
        else:
            gold_rows, gold_error = execute_sqlite(db_path, gold_sql)
            pred_rows, pred_error = execute_sqlite(db_path, pred_sql)
            row["gold_error"] = gold_error
            row["pred_error"] = pred_error
            row["dangerous_sql"] = pred_error == SQL_PERMISSION_DENIED
            row["gold_row_count"] = len(gold_rows or [])
            row["pred_row_count"] = len(pred_rows or [])
            if gold_error is None:
                ordered = has_outer_order_by(gold_sql)
                row["result_comparison"] = "ordered" if ordered else "unordered_multiset"
                row["exact_match"] = bool(
                    pred_error is None and compare_rows(gold_rows, pred_rows, ordered=ordered)
                )
        details.append(row)

    evaluated = [row for row in details if not row["skipped"] and not row.get("gold_error")]
    exact = sum(1 for row in evaluated if row["exact_match"])
    pred_errors = sum(1 for row in evaluated if row.get("pred_error"))
    skipped = sum(1 for row in details if row["skipped"])
    parseable_or_executable = sum(1 for row in evaluated if not row.get("pred_error"))
    summary = {
        "result_comparison_version": RESULT_COMPARISON_VERSION,
        "metric_scope": "harness_local_sqlite_not_benchmark_native",
        "benchmark": benchmark,
        "run_dir": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total_cases": len(details),
        "total_evaluated": len(evaluated),
        "skipped": skipped,
        "exact_matches": exact,
        "execution_accuracy_pct": (exact / len(evaluated) * 100) if evaluated else None,
        "prediction_error_count": pred_errors,
        "executable_rate": (parseable_or_executable / len(evaluated)) if evaluated else None,
        "details": details,
    }
    audit = audit_run(run_dir, cases, run_metadata)
    (run_dir / "summary_execution.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "fairness_audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "details"}, indent=2, sort_keys=True))
    print(f"Wrote {run_dir / 'summary_execution.json'}")
    print(f"Wrote {run_dir / 'fairness_audit.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

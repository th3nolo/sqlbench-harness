#!/usr/bin/env python3
"""Non-gold SQL verifier for BeaverBench repair gates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pragma: no cover - supports running outside the venv
    sqlglot = None
    exp = None

from schema_plan import build_schema_plan, normalize


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "beaver"


AGG_FUNC_NAMES = {
    "avg": {"AVG", "MEAN"},
    "sum": {"SUM"},
    "count": {"COUNT"},
    "variance": {"VARIANCE", "VAR_POP", "VAR_SAMP"},
    "min": {"MIN"},
    "max": {"MAX"},
}


def is_select_like(sql: str) -> bool:
    stripped = sql.strip().lower()
    return stripped.startswith("select") or stripped.startswith("with")


def regex_tables(sql: str, corpus_tables: dict[str, dict[str, Any]]) -> list[str]:
    upper = sql.upper()
    return sorted(name for name in corpus_tables if re.search(rf"\b{re.escape(name.upper())}\b", upper))


def parse_sql(sql: str):
    if sqlglot is None:
        return None, "sqlglot is not installed"
    try:
        return sqlglot.parse_one(sql, read="mysql"), None
    except Exception as exc:
        return None, str(exc)


def extract_features(sql: str, corpus_tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tree, parse_error = parse_sql(sql)
    features: dict[str, Any] = {
        "parse_error": parse_error,
        "tables": regex_tables(sql, corpus_tables),
        "columns": [],
        "functions": [],
        "join_equalities": [],
        "select_count": len(re.findall(r"\bSELECT\b", sql, re.I)),
        "has_cte": bool(re.search(r"^\s*WITH\b", sql.strip(), re.I)),
        "has_group_by": bool(re.search(r"\bGROUP\s+BY\b", sql, re.I)),
        "has_order_by": bool(re.search(r"\bORDER\s+BY\b", sql, re.I)),
        "has_join": bool(re.search(r"\bJOIN\b", sql, re.I)),
        "has_window": bool(re.search(r"\bOVER\s*\(", sql, re.I)),
    }
    if tree is None or exp is None:
        features["functions"] = sorted(set(re.findall(r"\b([A-Z_]+)\s*\(", sql.upper())))
        return features

    features["tables"] = sorted({table.name for table in tree.find_all(exp.Table)})
    features["columns"] = sorted({col.name for col in tree.find_all(exp.Column)})
    features["functions"] = sorted(
        {
            (func.sql_name() if hasattr(func, "sql_name") else func.key).upper()
            for func in tree.find_all(exp.Func)
        }
    )
    features["has_group_by"] = tree.find(exp.Group) is not None
    features["has_order_by"] = tree.find(exp.Order) is not None
    features["has_join"] = any(True for _ in tree.find_all(exp.Join))
    features["has_window"] = any(True for _ in tree.find_all(exp.Window))
    features["has_cte"] = features["has_cte"] or any(True for _ in tree.find_all(exp.CTE))
    features["select_count"] = max(features["select_count"], sum(1 for _ in tree.find_all(exp.Select)))
    alias_to_table = {}
    for table in tree.find_all(exp.Table):
        alias_to_table[table.alias_or_name.upper()] = table.name.upper()
        alias_to_table[table.name.upper()] = table.name.upper()
    equalities = set()
    for eq in tree.find_all(exp.EQ):
        left = eq.left
        right = eq.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_table = alias_to_table.get((left.table or "").upper())
        right_table = alias_to_table.get((right.table or "").upper())
        if not left_table or not right_table:
            continue
        left_ref = f"{left_table}.{left.name.upper()}"
        right_ref = f"{right_table}.{right.name.upper()}"
        equalities.add(tuple(sorted((left_ref, right_ref))))
    features["join_equalities"] = [
        {"left": left, "right": right}
        for left, right in sorted(equalities)
    ]
    return features


def high_evidence_tables(plan: dict[str, Any], max_tables: int = 8) -> list[dict[str, Any]]:
    candidates = plan.get("table_candidates") or []
    if not candidates:
        return []
    top_score = float(candidates[0].get("score") or 0)
    threshold = max(5.0, top_score * 0.35)
    scored = [
        item
        for item in candidates[: max_tables + 4]
        if float(item.get("score") or 0) >= threshold and item.get("matched_columns")
    ]
    direct = [
        item
        for item in scored
        if any("direct" in evidence or "plural" in evidence for evidence in item.get("evidence") or [])
    ]
    selected = direct or scored[:1]
    selected.sort(
        key=lambda item: (
            0
            if any("direct" in evidence or "plural" in evidence for evidence in item.get("evidence") or [])
            else 1,
            -float(item.get("score") or 0),
            item["table"],
        )
    )
    return selected[:max_tables]


def table_family_satisfied(expected: str, used_tables: set[str]) -> bool:
    if expected in used_tables:
        return True
    expected_upper = expected.upper()
    for used in used_tables:
        used_upper = used.upper()
        if used_upper.startswith(expected_upper + "_"):
            return True
        if expected_upper.endswith("_SUMMARY") and expected_upper.removesuffix("_SUMMARY") == used_upper:
            return True
    return False


def slot_table_satisfied(expected: str, used_tables: set[str]) -> bool:
    return table_family_satisfied(expected, used_tables)


def operation_present(operation: str, features: dict[str, Any], sql: str) -> bool:
    funcs = set(features.get("functions") or [])
    upper = sql.upper()
    if operation == "range":
        return "MAX" in funcs and "MIN" in funcs
    if operation in AGG_FUNC_NAMES:
        return bool(funcs & AGG_FUNC_NAMES[operation])
    if operation == "dense_rank":
        return "DENSE_RANK" in funcs or "DENSE_RANK" in upper
    if operation == "rank":
        return "RANK" in funcs or "RANK" in upper
    if operation == "group_by":
        return bool(features.get("has_group_by"))
    if operation == "order_by":
        return bool(features.get("has_order_by"))
    return True


def join_present(join: dict[str, Any], sql: str) -> bool:
    upper = sql.upper()
    left_table, left_col = join["left"].split(".", 1)
    right_table, right_col = join["right"].split(".", 1)
    return (
        left_table.upper() in upper
        and right_table.upper() in upper
        and left_col.upper() in upper
        and right_col.upper() in upper
    )


def required_join_present(left: str, right: str, sql: str, features: dict[str, Any]) -> bool:
    required = tuple(sorted((left.upper(), right.upper())))
    equalities = {
        tuple(sorted((item["left"].upper(), item["right"].upper())))
        for item in features.get("join_equalities") or []
    }
    if required in equalities:
        return True
    if features.get("join_equalities"):
        return False
    upper = sql.upper()
    left_table, left_col = left.split(".", 1)
    right_table, right_col = right.split(".", 1)
    return (
        left_table.upper() in upper
        and right_table.upper() in upper
        and left_col.upper() in upper
        and right_col.upper() in upper
    )


def join_on_clauses(sql: str, table_name: str) -> list[str]:
    pattern = re.compile(
        rf"\bJOIN\s+{re.escape(table_name)}\b(?:\s+\w+)?\s+ON\s+"
        r"(.*?)(?=\bJOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bLIMIT\b|\)\s*,|\)\s*SELECT\b|$)",
        re.I | re.S,
    )
    return [match.group(1) for match in pattern.finditer(sql)]


def verify_sql(
    *,
    sql: str,
    question: str,
    corpus_tables: dict[str, dict[str, Any]],
    plan: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan is None:
        plan = build_schema_plan(instance_id="adhoc", question=question, corpus_tables=corpus_tables)

    features = extract_features(sql, corpus_tables)
    findings: list[dict[str, Any]] = []
    score = 100

    if not sql.strip():
        findings.append({"severity": "high", "code": "empty_sql", "message": "Model returned empty SQL."})
        score -= 80
    elif not is_select_like(sql):
        findings.append({"severity": "high", "code": "not_select", "message": "SQL is not a SELECT/WITH query."})
        score -= 70

    if features.get("parse_error"):
        findings.append(
            {
                "severity": "medium",
                "code": "parse_error",
                "message": f"SQL parser could not fully parse the query: {features['parse_error']}",
            }
        )
        score -= 20

    used_tables = set(features.get("tables") or [])
    used_columns = {col.upper() for col in features.get("columns") or []}
    high_tables = high_evidence_tables(plan)
    missing_tables = [
        item for item in high_tables if not table_family_satisfied(item["table"], used_tables)
    ]
    # Penalize only the strongest misses. The planner is a heuristic and should
    # not force every plausible table into every answer.
    for item in missing_tables[:2]:
        matched_cols = ", ".join(col["column"] for col in (item.get("matched_columns") or [])[:4])
        findings.append(
            {
                "severity": "medium",
                "code": "missing_high_evidence_table",
                "table": item["table"],
                "message": f"High-evidence table {item['table']} is not used; matched columns: {matched_cols}",
            }
        )
        score -= 10

    for op in plan.get("required_operations") or []:
        operation = op.get("operation")
        if operation and not operation_present(operation, features, sql):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_required_operation",
                    "operation": operation,
                    "message": f"Question appears to require {operation}, but the SQL does not show it.",
                }
            )
            score -= 12

    joins = plan.get("join_candidates") or []
    if len(used_tables) >= 2 and joins:
        relevant_joins = [
            join
            for join in joins[:8]
            if join["left"].split(".", 1)[0] in used_tables and join["right"].split(".", 1)[0] in used_tables
        ]
        if relevant_joins and not any(join_present(join, sql) for join in relevant_joins[:3]):
            findings.append(
                {
                    "severity": "medium",
                    "code": "weak_join_evidence",
                    "message": "SQL uses multiple candidate tables but does not show the strongest inferred join columns.",
                    "candidate_joins": relevant_joins[:3],
                }
            )
            score -= 10

    for slot in plan.get("required_slot_map") or []:
        if slot.get("confidence") not in {"high", "medium"}:
            continue
        role = slot.get("role")
        table = slot.get("table")
        column = slot.get("column")
        recommended = slot.get("recommended")
        if not table or not column or not recommended:
            continue
        if not slot_table_satisfied(table, used_tables):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_required_slot_table",
                    "slot": slot.get("slot"),
                    "recommended": recommended,
                    "message": f"Required {role} slot should use {recommended}, but table {table} is not present.",
                }
            )
            score -= 10
            continue
        if column.upper() not in used_columns:
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_required_slot_column",
                    "slot": slot.get("slot"),
                    "recommended": recommended,
                    "message": f"Required {role} slot should use column {recommended}, but that column is not present in the SQL.",
                }
            )
            score -= 10

    for join in plan.get("required_join_map") or []:
        if join.get("confidence") not in {"high", "medium"}:
            continue
        left = join.get("left")
        right = join.get("right")
        if not left or not right:
            continue
        if not required_join_present(left, right, sql, features):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_required_join",
                    "left": left,
                    "right": right,
                    "role": join.get("role"),
                    "message": f"Required join {left} = {right} is not present; {join.get('reason', '')}",
                }
            )
            score -= 10

    blueprint = plan.get("query_blueprint") or {}
    required_features = blueprint.get("required_features") or {}
    pattern = blueprint.get("pattern")
    if pattern == "multi_context_cte":
        if required_features.get("cte_or_subquery") and not (
            features.get("has_cte") or int(features.get("select_count") or 0) >= 2
        ):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_query_blueprint_structure",
                    "pattern": pattern,
                    "message": "Query blueprint requires separate CTE/subquery contexts, but the SQL is a single flat SELECT.",
                }
            )
            score -= 14
        if required_features.get("grouped_count_context") and not (
            "COUNT" in set(features.get("functions") or []) and features.get("has_group_by")
        ):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_blueprint_grouped_count_context",
                    "pattern": pattern,
                    "message": "Query blueprint requires a grouped count-by-department context over offered-summary rows.",
                }
            )
            score -= 12
        detail_table = required_features.get("preserve_detail_table")
        if detail_table and not table_family_satisfied(detail_table, used_tables):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_blueprint_detail_table",
                    "pattern": pattern,
                    "table": detail_table,
                    "message": f"Query blueprint requires preserving detail rows from {detail_table}.",
                }
            )
            score -= 12
        for detail_col in required_features.get("preserve_detail_columns") or []:
            if detail_col.upper() not in used_columns:
                findings.append(
                    {
                        "severity": "medium",
                        "code": "missing_blueprint_detail_column",
                        "pattern": pattern,
                        "column": detail_col,
                        "message": f"Query blueprint requires preserving offered-detail column {detail_col}.",
                    }
                )
                score -= 8
        budget_window_pattern = re.compile(
            r"AVG\s*\(\s*[^)]*DEPT_BUDGET_CODE[^)]*\)\s*OVER\s*\([^)]*(SCHOOL_NAME|SCHOOL_CODE)",
            re.I | re.S,
        )
        if required_features.get("window_context") and not budget_window_pattern.search(sql):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_blueprint_window_context",
                    "pattern": pattern,
                    "message": "Query blueprint requires AVG(DEPT_BUDGET_CODE) as a window within school context, not a separate plain aggregate.",
                }
            )
            score -= 10
        if required_features.get("subject_code_context_school_only"):
            for clause in join_on_clauses(sql, "SIS_SUBJECT_CODE"):
                upper_clause = clause.upper()
                if "SCHOOL_CODE" in upper_clause and "DEPARTMENT_CODE" in upper_clause:
                    findings.append(
                        {
                            "severity": "medium",
                            "code": "overconstrained_subject_code_context",
                            "pattern": pattern,
                            "message": (
                                "Subject-code school context should not combine SCHOOL_CODE and DEPARTMENT_CODE "
                                "in the same SIS_SUBJECT_CODE join; it narrows school-level subject-code context."
                            ),
                        }
                    )
                    score -= 12
                    break
    elif pattern == "lookup_display_aggregate":
        lookup_join = required_features.get("lookup_join")
        if lookup_join and " = " in lookup_join:
            left, right = lookup_join.split(" = ", 1)
            if not required_join_present(left, right, sql, features):
                findings.append(
                    {
                        "severity": "medium",
                        "code": "missing_blueprint_lookup_join",
                        "pattern": pattern,
                        "message": f"Query blueprint requires lookup-display join {lookup_join}.",
                    }
                )
                score -= 10

    q_norm = normalize(question)
    if "for each" in q_norm and not features.get("has_group_by") and any(
        op.get("operation") in {"avg", "sum", "count", "variance", "range"} for op in plan.get("required_operations") or []
    ):
        findings.append(
            {
                "severity": "medium",
                "code": "grain_without_group_by",
                "message": "Question asks for per-entity aggregate grain, but SQL has no GROUP BY.",
            }
        )
        score -= 12

    if preview and preview.get("row_count") == 0:
        findings.append(
            {
                "severity": "low",
                "code": "empty_result",
                "message": "SQL executed but returned zero rows.",
            }
        )
        score -= 8

    score = max(0, min(100, score))
    should_repair = any(item["severity"] in {"high", "medium"} for item in findings) and score <= 90
    return {
        "score": score,
        "should_repair": should_repair,
        "features": features,
        "findings": findings,
        "planner": plan.get("planner"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--sql-file", required=True)
    args = parser.parse_args()

    data_dir = VENDOR / "data" / args.dataset
    questions = {item["id"]: item for item in json.loads((data_dir / "dev_sampled.json").read_text())}
    corpus_tables = json.loads((data_dir / "dev_tables.json").read_text())
    plan_path = data_dir / "retrieval" / "schema_plan.json"
    plans = json.loads(plan_path.read_text()) if plan_path.exists() else {}
    question = questions[args.question_id]["question"]
    sql = Path(args.sql_file).read_text(encoding="utf-8")
    result = verify_sql(
        sql=sql,
        question=question,
        corpus_tables=corpus_tables,
        plan=plans.get(args.question_id),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

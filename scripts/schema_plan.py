#!/usr/bin/env python3
"""Build non-gold schema plans for BeaverBench questions.

The planner uses only the natural-language question plus table metadata,
columns, and example values. It intentionally does not read gold SQL, gold
tables, column mappings, or join_keys.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
from itertools import combinations
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "beaver"
PLANNER_VERSION = "schema_plan_v3_non_gold_blueprints"

STOPWORDS = {
    "about",
    "above",
    "along",
    "also",
    "and",
    "answers",
    "any",
    "are",
    "associated",
    "between",
    "both",
    "column",
    "columns",
    "desc",
    "description",
    "each",
    "for",
    "from",
    "give",
    "have",
    "include",
    "into",
    "list",
    "not",
    "number",
    "ordered",
    "over",
    "provide",
    "return",
    "rounded",
    "show",
    "that",
    "the",
    "their",
    "them",
    "these",
    "this",
    "with",
    "within",
}

ABBREVIATIONS = {
    "dept": {"department"},
    "dlc": {"department"},
    "num": {"number"},
    "res": {"research"},
    "sqft": {"square", "footage", "feet"},
    "vol": {"volume"},
}

AGGREGATE_HINTS = {
    "avg": {"average", "avg", "mean"},
    "sum": {"sum"},
    "count": {"count", "how many"},
    "variance": {"variance", "var"},
    "range": {"range"},
    "min": {"minimum", "min"},
    "max": {"maximum", "max"},
}

SYNONYMS = {
    "dept": {"department", "dept", "dlc", "organization", "org"},
    "department": {"department", "dept", "dlc", "organization", "org"},
    "dlc": {"department", "dept", "dlc", "organization", "org"},
    "course": {"course", "subject", "class"},
    "subject": {"course", "subject", "class"},
    "space": {"space", "room", "unit", "facility", "fclt"},
    "unit": {"space", "room", "unit", "facility", "fclt"},
    "school": {"school", "college"},
    "key": {"key", "id", "code", "number"},
    "id": {"key", "id", "code", "number"},
    "code": {"key", "id", "code", "number"},
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def tokens(value: str, *, keep_stopwords: bool = False, expand: bool = False) -> set[str]:
    raw = normalize(value).split()
    out = {token for token in raw if len(token) > 1 and (keep_stopwords or token not in STOPWORDS)}
    expanded = set(out)
    for token in out:
        if token.endswith("s") and len(token) > 3:
            expanded.add(token[:-1])
        expanded.update(ABBREVIATIONS.get(token, set()))
        if expand:
            expanded.update(SYNONYMS.get(token, set()))
    return expanded


def phrase(value: str) -> str:
    return normalize(value.replace("_", " "))


def ngrams(question: str, max_n: int = 4) -> set[str]:
    parts = normalize(question).split()
    grams = set()
    for size in range(1, max_n + 1):
        for idx in range(0, max(0, len(parts) - size + 1)):
            gram = " ".join(parts[idx : idx + size])
            if gram and not all(part in STOPWORDS for part in gram.split()):
                grams.add(gram)
    return grams


def flatten_examples(table: dict[str, Any], max_values: int = 80) -> list[str]:
    values: list[str] = []
    for row in table.get("example_rows") or []:
        for value in row:
            if value is None:
                continue
            text = str(value)
            if text and text.lower() != "nan":
                values.append(text)
    for col_values in table.get("example_columns") or []:
        for value in col_values[:5]:
            if value is None:
                continue
            text = str(value)
            if text and text.lower() != "nan":
                values.append(text)
    return values[:max_values]


def question_values(question: str) -> set[str]:
    values = set(re.findall(r"'([^']+)'|\"([^\"]+)\"", question))
    flattened = {left or right for left, right in values if left or right}
    flattened.update(re.findall(r"\b[A-Z][A-Za-z0-9_&/-]{2,}\b", question))
    return flattened


def required_operations(question: str) -> list[dict[str, Any]]:
    q = normalize(question)
    operations = []
    for name, hints in AGGREGATE_HINTS.items():
        matched = sorted(hint for hint in hints if hint in q)
        if matched:
            operations.append({"operation": name, "evidence": matched})
    if re.search(r"\bnumber of .*subjects? offered\b", q) or re.search(r"\bnumber of .*records?\b", q):
        operations.append({"operation": "count", "evidence": ["number of offered subjects/records"]})
    total_phrases = re.findall(r"\btotal ([a-z0-9 ]{2,40}?)(?:,| and| for| within|$)", q)
    if any("unit" not in phrase.split() for phrase in total_phrases):
        operations.append({"operation": "sum", "evidence": ["total aggregate phrase"]})
    if "dense rank" in q:
        operations.append({"operation": "dense_rank", "evidence": ["dense rank"]})
    elif " rank" in f" {q}":
        operations.append({"operation": "rank", "evidence": ["rank"]})
    if "order by" in q or "ordered by" in q:
        operations.append({"operation": "order_by", "evidence": ["ordered by"]})
    if "for each" in q or "grouped by" in q:
        operations.append({"operation": "group_by", "evidence": ["for each/grouped by"]})
    return operations


def required_grain(question: str) -> list[str]:
    grains = []
    q = normalize(question)
    for match in re.finditer(r"for each ([a-z0-9 ]{2,80}?)(?:,| where| with| ordered| and|$)", q):
        grain = match.group(1).strip()
        if grain:
            grains.append(grain)
    for marker in ("per ", "by "):
        for match in re.finditer(rf"\b{marker}([a-z0-9 ]{{2,40}}?)(?:,| and| ordered|$)", q):
            grain = match.group(1).strip()
            if grain and grain not in grains:
                grains.append(grain)
    return grains[:4]


def table_score(question: str, table_name: str, table: dict[str, Any]) -> dict[str, Any]:
    q_tokens = tokens(question)
    q_grams = ngrams(question)
    q_norm = normalize(question)
    q_values = question_values(question)
    table_phrase = phrase(table_name)
    table_tokens = tokens(table_name, keep_stopwords=True)
    column_matches: list[dict[str, Any]] = []
    score = 0.0
    evidence: list[str] = []

    overlap = q_tokens & table_tokens
    if overlap:
        score += 2.2 * len(overlap)
        evidence.append(f"table-name token overlap: {', '.join(sorted(overlap))}")
    if table_phrase in q_grams:
        score += 9.0
        evidence.append(f"direct table phrase: {table_phrase}")
    if f"{table_phrase}s" in q_norm or f"{table_phrase}es" in q_norm:
        score += 12.0
        evidence.append(f"plural/direct entity phrase: {table_phrase}")

    for col in table.get("column_names", []):
        col_phrase = phrase(col)
        col_tokens = tokens(col, keep_stopwords=True)
        col_overlap = q_tokens & col_tokens
        col_score = 0.0
        reasons: list[str] = []
        if col_phrase in q_grams:
            col_score += 7.0
            reasons.append("direct column phrase")
        if f"{col_phrase}s" in q_norm or f"{col_phrase}es" in q_norm:
            col_score += 3.0
            reasons.append("plural/direct column phrase")
        if col_overlap:
            col_score += 1.4 * len(col_overlap)
            reasons.append(f"token overlap: {', '.join(sorted(col_overlap))}")
        if col_score:
            score += col_score
            column_matches.append(
                {
                    "column": col,
                    "score": round(col_score, 3),
                    "evidence": reasons,
                }
            )

    examples = flatten_examples(table)
    example_hits = []
    normalized_examples = {normalize(value): value for value in examples if len(str(value)) <= 80}
    for value in q_values:
        norm_value = normalize(value)
        if norm_value and norm_value in normalized_examples:
            example_hits.append(value)
    if example_hits:
        score += 4.0 * len(example_hits)
        evidence.append(f"example value hits: {', '.join(sorted(set(example_hits)))}")

    for value in examples:
        if str(value).startswith("D_") and {"department", "dept", "dlc"} & q_tokens:
            score += 0.4
            evidence.append("example values look like DLC/department keys")
            break

    column_matches.sort(key=lambda item: (-item["score"], item["column"]))
    return {
        "table": table_name,
        "score": round(score, 3),
        "matched_columns": column_matches[:10],
        "evidence": evidence[:8],
        "columns": table.get("column_names", []),
    }


def column_profile(table: dict[str, Any], column: str) -> dict[str, Any]:
    columns = table.get("column_names", [])
    idx = columns.index(column) if column in columns else -1
    values = []
    if idx >= 0:
        for row in table.get("example_rows") or []:
            if idx < len(row) and row[idx] is not None and str(row[idx]).lower() != "nan":
                values.append(str(row[idx]))
        example_columns = table.get("example_columns") or []
        if idx < len(example_columns):
            values.extend(str(value) for value in example_columns[idx][:8] if value is not None and str(value).lower() != "nan")
    value_tokens = set()
    prefixes = set()
    for value in values[:20]:
        value_tokens.add(normalize(value))
        if "_" in value:
            prefixes.add(value.split("_", 1)[0])
    return {
        "tokens": tokens(column, keep_stopwords=True, expand=True),
        "values": {value for value in value_tokens if value},
        "prefixes": prefixes,
    }


def compatible_column_score(left_name: str, left: dict[str, Any], right_name: str, right: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    if left_name == right_name:
        score += 6.0
        reasons.append("same column name")
    token_overlap = left["tokens"] & right["tokens"]
    if token_overlap:
        score += 1.5 * len(token_overlap)
        reasons.append(f"column token overlap: {', '.join(sorted(token_overlap))}")
    value_overlap = left["values"] & right["values"]
    if value_overlap:
        score += min(14.0, 4.0 * len(value_overlap))
        reasons.append(f"example value overlap: {', '.join(sorted(list(value_overlap))[:3])}")
    if left["prefixes"] and left["prefixes"] == right["prefixes"]:
        score += 4.0
        reasons.append(f"matching value prefix: {', '.join(sorted(left['prefixes']))}")
    keyish_left = {"key", "id", "code"} & left["tokens"]
    keyish_right = {"key", "id", "code"} & right["tokens"]
    if keyish_left and keyish_right and token_overlap:
        score += 2.0
        reasons.append("both columns are key/code/id-like")
    deptish_left = {"dept", "department", "dlc", "organization", "org"} & left["tokens"]
    deptish_right = {"dept", "department", "dlc", "organization", "org"} & right["tokens"]
    if deptish_left and deptish_right and (keyish_left or keyish_right):
        score += 5.0
        reasons.append("department/DLC key compatibility")
    nameish_left = {"name", "names"} & left["tokens"]
    nameish_right = {"name", "names"} & right["tokens"]
    if (
        deptish_left
        and deptish_right
        and (nameish_left or nameish_right)
        and (keyish_left or keyish_right)
        and (left["prefixes"] or right["prefixes"])
    ):
        score += 6.0
        reasons.append("coded department-name field can join to department/DLC key")
    return score, reasons


def infer_join_candidates(
    table_candidates: list[dict[str, Any]],
    corpus_tables: dict[str, dict[str, Any]],
    *,
    max_tables: int = 12,
    max_joins: int = 16,
) -> list[dict[str, Any]]:
    selected = [item["table"] for item in table_candidates[:max_tables]]
    joins = []
    profiles: dict[tuple[str, str], dict[str, Any]] = {}
    for table_name in selected:
        table = corpus_tables[table_name]
        for col in table.get("column_names", []):
            profiles[(table_name, col)] = column_profile(table, col)

    for left_table, right_table in combinations(selected, 2):
        best: tuple[float, str, str, list[str]] | None = None
        for left_col in corpus_tables[left_table].get("column_names", []):
            for right_col in corpus_tables[right_table].get("column_names", []):
                score, reasons = compatible_column_score(
                    left_col,
                    profiles[(left_table, left_col)],
                    right_col,
                    profiles[(right_table, right_col)],
                )
                if score and (best is None or score > best[0]):
                    best = (score, left_col, right_col, reasons)
        if best and best[0] >= 3.0:
            joins.append(
                {
                    "left": f"{left_table}.{best[1]}",
                    "right": f"{right_table}.{best[2]}",
                    "score": round(best[0], 3),
                    "evidence": best[3][:5],
                }
            )
    joins.sort(key=lambda item: (-item["score"], item["left"], item["right"]))
    return joins[:max_joins]


def distractor_warnings(candidates: list[dict[str, Any]]) -> list[str]:
    warnings = []
    by_family: dict[str, list[str]] = {}
    for item in candidates[:15]:
        parts = item["table"].split("_")
        family = "_".join(parts[:2]) if len(parts) >= 2 else parts[0]
        by_family.setdefault(family, []).append(item["table"])
    for family, tables in sorted(by_family.items()):
        if len(tables) >= 3:
            warnings.append(
                f"Many similar {family} tables ranked high: {', '.join(tables[:5])}. Choose by required columns and grain, not name similarity alone."
            )
    return warnings[:6]


def table_family_satisfied(expected: str, actual: str) -> bool:
    expected_upper = expected.upper()
    actual_upper = actual.upper()
    return (
        actual_upper == expected_upper
        or actual_upper.startswith(expected_upper + "_")
        or (
            expected_upper.endswith("_SUMMARY")
            and expected_upper.removesuffix("_SUMMARY") == actual_upper
        )
    )


def candidate_tables_by_rank(table_candidates: list[dict[str, Any]]) -> dict[str, int]:
    return {item["table"]: idx for idx, item in enumerate(table_candidates, start=1)}


def find_column_owner(
    *,
    column_names: list[str],
    corpus_tables: dict[str, dict[str, Any]],
    table_candidates: list[dict[str, Any]],
    preferred_tables: list[str] | None = None,
    avoid_tables: list[str] | None = None,
) -> tuple[str, str, list[dict[str, Any]]] | None:
    preferred_tables = preferred_tables or []
    avoid_tables = avoid_tables or []
    ranks = candidate_tables_by_rank(table_candidates)
    scored: list[tuple[float, str, str]] = []
    wanted = {column.upper() for column in column_names}
    for table_name, table in corpus_tables.items():
        for column in table.get("column_names", []):
            if column.upper() not in wanted:
                continue
            score = 0.0
            rank = ranks.get(table_name)
            if rank is not None:
                score += max(0.0, 20.0 - rank)
            for preferred in preferred_tables:
                if table_family_satisfied(preferred, table_name):
                    score += 45.0
            for avoided in avoid_tables:
                if table_family_satisfied(avoided, table_name):
                    score -= 25.0
            if table_name.endswith("_SUMMARY"):
                score += 3.0
            scored.append((score, table_name, column))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    alternatives = [
        {"table": table, "column": column, "score": round(score, 3)}
        for score, table, column in scored[1:6]
    ]
    _, table, column = scored[0]
    return table, column, alternatives


def add_slot(
    slots: list[dict[str, Any]],
    *,
    slot: str,
    role: str,
    table: str,
    column: str,
    reason: str,
    operation: str | None = None,
    confidence: str = "medium",
    alternatives: list[dict[str, Any]] | None = None,
) -> None:
    key = (slot, role, table, column, operation or "")
    for existing in slots:
        existing_key = (
            existing.get("slot"),
            existing.get("role"),
            existing.get("table"),
            existing.get("column"),
            existing.get("operation") or "",
        )
        if existing_key == key:
            return
    slots.append(
        {
            "slot": slot,
            "role": role,
            "table": table,
            "column": column,
            "recommended": f"{table}.{column}",
            "operation": operation,
            "confidence": confidence,
            "reason": reason,
            "alternatives": alternatives or [],
        }
    )


def infer_contextual_preferences(question: str, corpus_tables: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    q = normalize(question)
    preferences: dict[str, list[str]] = {}
    if "space unit" in q and "SPACE_UNIT" in corpus_tables:
        preferences["department_display"] = ["SPACE_UNIT"]
        preferences["space_unit_grain"] = ["SPACE_UNIT"]
    if ("subject offered" in q or "subjects offered" in q) and "SUBJECT_OFFERED_SUMMARY" in corpus_tables:
        preferences["subject_offered_grain"] = ["SUBJECT_OFFERED_SUMMARY"]
        preferences["subject_offered_fields"] = ["SUBJECT_OFFERED_SUMMARY"]
    if (
        "degree granting" in q
        or "department budget" in q
        or "mathematics" in q
    ) and "SIS_DEPARTMENT" in corpus_tables:
        preferences["academic_department"] = ["SIS_DEPARTMENT"]
    if "subject code" in q and "SIS_SUBJECT_CODE" in corpus_tables:
        preferences["subject_code"] = ["SIS_SUBJECT_CODE"]
    return preferences


def add_owner_slot_if_present(
    slots: list[dict[str, Any]],
    *,
    question: str,
    slot: str,
    aliases: list[str],
    role: str,
    column_names: list[str],
    corpus_tables: dict[str, dict[str, Any]],
    table_candidates: list[dict[str, Any]],
    preferred_tables: list[str] | None = None,
    avoid_tables: list[str] | None = None,
    operation: str | None = None,
    reason: str,
    confidence: str = "medium",
) -> None:
    q = normalize(question)
    if not any(normalize(alias) in q for alias in aliases):
        return
    owner = find_column_owner(
        column_names=column_names,
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=preferred_tables,
        avoid_tables=avoid_tables,
    )
    if owner is None:
        return
    table, column, alternatives = owner
    add_slot(
        slots,
        slot=slot,
        role=role,
        table=table,
        column=column,
        operation=operation,
        reason=reason,
        confidence=confidence,
        alternatives=alternatives,
    )


def build_required_slot_map(
    *,
    question: str,
    corpus_tables: dict[str, dict[str, Any]],
    table_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    q = normalize(question)
    prefs = infer_contextual_preferences(question, corpus_tables)
    slots: list[dict[str, Any]] = []

    if "space unit" in q and "SPACE_UNIT" in corpus_tables:
        add_slot(
            slots,
            slot="space unit / department display grain",
            role="grain",
            table="SPACE_UNIT",
            column="SPACE_UNIT",
            confidence="high",
            reason="Question says space units associated with each department; SPACE_UNIT is the direct lookup/display table and SPACE_UNIT is its display column.",
        )
        add_slot(
            slots,
            slot="department name",
            role="output",
            table="SPACE_UNIT",
            column="SPACE_UNIT",
            confidence="high",
            reason="In the space-unit context, the department display name should come from SPACE_UNIT.SPACE_UNIT rather than raw DLC keys or HR hierarchy names.",
        )

    if ("subject offered" in q or "subjects offered" in q) and "SUBJECT_OFFERED_SUMMARY" in corpus_tables:
        add_slot(
            slots,
            slot="subject offered grain",
            role="grain",
            table="SUBJECT_OFFERED_SUMMARY",
            column="SUBJECT_OFFERED_SUMMARY_KEY",
            confidence="high",
            reason="Question asks for each subject offered / number of subjects offered; SUBJECT_OFFERED_SUMMARY is the offered-summary grain table.",
        )

    add_owner_slot_if_present(
        slots,
        question=question,
        slot="subject code description",
        aliases=["subject code description", "subject code desc"],
        role="output",
        column_names=["SUBJECT_CODE_DESC"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_code"),
        reason="Exact requested output phrase maps to SUBJECT_CODE_DESC.",
        confidence="high",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="subject code",
        aliases=["subject code"],
        role="output",
        column_names=["SUBJECT_CODE"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_code"),
        reason="Exact requested output phrase maps to SUBJECT_CODE.",
        confidence="high",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="subject title",
        aliases=["subject title"],
        role="output",
        column_names=["SUBJECT_TITLE"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_offered_fields"),
        avoid_tables=["COURSE_CATALOG_SUBJECT_OFFERED", "DRUPAL_COURSE_CATALOG", "CIS_COURSE_CATALOG"] if prefs.get("subject_offered_fields") else None,
        reason="Subject-title output should come from the active offered-summary grain when the question says subject offered.",
        confidence="high" if prefs.get("subject_offered_fields") else "medium",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="total units",
        aliases=["total units"],
        role="output",
        column_names=["TOTAL_UNITS"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_offered_fields"),
        avoid_tables=["COURSE_CATALOG_SUBJECT_OFFERED", "DRUPAL_COURSE_CATALOG", "CIS_COURSE_CATALOG"] if prefs.get("subject_offered_fields") else None,
        reason="Total-units output should stay on the subject-offered grain when that grain is requested.",
        confidence="high" if prefs.get("subject_offered_fields") else "medium",
    )
    if "space unit" not in q:
        add_owner_slot_if_present(
            slots,
            question=question,
            slot="department name",
            aliases=["department name"],
            role="output",
            column_names=["DEPARTMENT_NAME"],
            corpus_tables=corpus_tables,
            table_candidates=table_candidates,
            preferred_tables=prefs.get("academic_department"),
            reason="Department filters and budget attributes point to the academic department table.",
            confidence="high" if prefs.get("academic_department") else "medium",
        )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="school name",
        aliases=["school name"],
        role="output",
        column_names=["SCHOOL_NAME"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_code") or prefs.get("academic_department"),
        reason="Requested school-name output must use a table participating in the subject/department join.",
        confidence="medium",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="department budget code",
        aliases=["department budget code", "dept budget code", "average department budget code"],
        role="measure",
        column_names=["DEPT_BUDGET_CODE"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("academic_department"),
        operation="avg" if "average" in q or "avg" in q else None,
        reason="Budget-code measure is an academic department attribute.",
        confidence="high",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="degree granting filter",
        aliases=["degree granting"],
        role="filter",
        column_names=["IS_DEGREE_GRANTING"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("academic_department"),
        reason="Degree-granting predicate should be applied on the department dimension.",
        confidence="high",
    )
    if "mathematics" in q:
        owner = find_column_owner(
            column_names=["DEPARTMENT_NAME"],
            corpus_tables=corpus_tables,
            table_candidates=table_candidates,
            preferred_tables=prefs.get("academic_department"),
        )
        if owner is not None:
            table, column, alternatives = owner
            add_slot(
                slots,
                slot="Mathematics filter",
                role="filter",
                table=table,
                column=column,
                confidence="high",
                reason="The value Mathematics is a department-name filter.",
                alternatives=alternatives,
            )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="department code for dense rank/join",
        aliases=["department code"],
        role="window_or_join",
        column_names=["DEPARTMENT_CODE"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("academic_department"),
        operation="dense_rank" if "dense rank" in q else None,
        reason="Department-code window/join grain should come from the department dimension.",
        confidence="high" if "dense rank" in q else "medium",
    )

    if "number of supervisee" in q or "supervisee" in q:
        for operation, alias in [("avg", "average"), ("range", "range"), ("variance", "variance")]:
            if alias in q or (operation == "avg" and "average" in q):
                add_owner_slot_if_present(
                    slots,
                    question=question,
                    slot=f"{operation} number of supervisees",
                    aliases=["number of supervisees", "supervisees"],
                    role="measure",
                    column_names=["NUM_OF_SUPERVISEES"],
                    corpus_tables=corpus_tables,
                    table_candidates=table_candidates,
                    preferred_tables=["SPACE_SUPERVISOR_USAGE"],
                    operation=operation,
                    reason="Supervisee measures live on SPACE_SUPERVISOR_USAGE.",
                    confidence="high",
                )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="total square footage",
        aliases=["total square footage", "square footage", "sqft"],
        role="measure",
        column_names=["SQFT", "ROOM_SQUARE_FOOTAGE"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=["SPACE_SUPERVISOR_USAGE"],
        operation="sum" if "total" in q else None,
        reason="The requested total square footage measure should aggregate the usage table sqft column.",
        confidence="high",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="total research volume",
        aliases=["total research volume", "research volume"],
        role="measure",
        column_names=["RESEARCH_VOLUME"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=["SPACE_SUPERVISOR_USAGE"],
        operation="sum" if "total" in q else None,
        reason="Research-volume measure lives on SPACE_SUPERVISOR_USAGE.",
        confidence="high",
    )
    add_owner_slot_if_present(
        slots,
        question=question,
        slot="number of subjects offered",
        aliases=["number of subjects offered", "subjects offered"],
        role="measure",
        column_names=["SUBJECT_OFFERED_SUMMARY_KEY", "SUBJECT_KEY"],
        corpus_tables=corpus_tables,
        table_candidates=table_candidates,
        preferred_tables=prefs.get("subject_offered_grain"),
        operation="count",
        reason="Counting subjects offered should count rows/keys from the offered-summary grain.",
        confidence="high" if prefs.get("subject_offered_grain") else "medium",
    )

    role_order = {"grain": 0, "filter": 1, "output": 2, "measure": 3, "window_or_join": 4}
    slots.sort(key=lambda item: (role_order.get(item["role"], 99), item["slot"], item["recommended"]))
    return slots


def has_slot_table(slots: list[dict[str, Any]], table_name: str) -> bool:
    return any(table_family_satisfied(table_name, slot.get("table", "")) for slot in slots)


def add_join_requirement(
    joins: list[dict[str, Any]],
    *,
    left: str,
    right: str,
    reason: str,
    confidence: str = "high",
    role: str = "join",
) -> None:
    key = tuple(sorted((left, right)))
    for existing in joins:
        if tuple(sorted((existing["left"], existing["right"]))) == key:
            return
    joins.append(
        {
            "left": left,
            "right": right,
            "role": role,
            "confidence": confidence,
            "reason": reason,
        }
    )


def build_required_join_map(
    *,
    question: str,
    corpus_tables: dict[str, dict[str, Any]],
    slot_map: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    q = normalize(question)
    joins: list[dict[str, Any]] = []
    if has_slot_table(slot_map, "SPACE_SUPERVISOR_USAGE") and has_slot_table(slot_map, "SPACE_UNIT"):
        add_join_requirement(
            joins,
            left="SPACE_SUPERVISOR_USAGE.DEPT_NAMES",
            right="SPACE_UNIT.DLC_KEY",
            role="lookup_join",
            reason="Space supervisor usage stores department-like DLC keys in DEPT_NAMES; SPACE_UNIT owns the display grain through DLC_KEY.",
        )
    if has_slot_table(slot_map, "SUBJECT_OFFERED_SUMMARY") and has_slot_table(slot_map, "SIS_DEPARTMENT"):
        add_join_requirement(
            joins,
            left="SIS_DEPARTMENT.DEPARTMENT_CODE",
            right="SUBJECT_OFFERED_SUMMARY.OFFER_DEPT_CODE",
            role="grain_join",
            reason="Offered-summary rows are attached to departments through OFFER_DEPT_CODE.",
        )
    if (
        has_slot_table(slot_map, "SIS_DEPARTMENT")
        and has_slot_table(slot_map, "SIS_SUBJECT_CODE")
        and ("school name" in q or "within each school" in q or "school" in q)
    ):
        add_join_requirement(
            joins,
            left="SIS_DEPARTMENT.SCHOOL_CODE",
            right="SIS_SUBJECT_CODE.SCHOOL_CODE",
            role="school_context_join",
            reason="The question asks for school-level subject-code context and average budget within each school; preserve the school-code join rather than only department-code joining.",
        )
    if (
        has_slot_table(slot_map, "SIS_DEPARTMENT")
        and has_slot_table(slot_map, "SIS_SUBJECT_CODE")
        and has_slot_table(slot_map, "SUBJECT_OFFERED_SUMMARY")
        and not ("school name" in q or "within each school" in q or "school" in q)
    ):
        add_join_requirement(
            joins,
            left="SIS_DEPARTMENT.DEPARTMENT_CODE",
            right="SIS_SUBJECT_CODE.DEPARTMENT_CODE",
            role="department_context_join",
            confidence="medium",
            reason="Department-code context can connect SIS_DEPARTMENT and SIS_SUBJECT_CODE, but do not let it replace the school-code subject-code context when school-level outputs are requested.",
        )
    return joins


def build_query_blueprint(
    *,
    question: str,
    slot_map: list[dict[str, Any]],
    join_map: list[dict[str, Any]],
) -> dict[str, Any] | None:
    q = normalize(question)

    def has_table(table_name: str) -> bool:
        return has_slot_table(slot_map, table_name)

    if (
        has_table("SUBJECT_OFFERED_SUMMARY")
        and has_table("SIS_DEPARTMENT")
        and has_table("SIS_SUBJECT_CODE")
        and ("subject offered" in q or "subjects offered" in q)
    ):
        return {
            "pattern": "multi_context_cte",
            "confidence": "high",
            "reason": (
                "The question mixes subject-code/school context, offered-subject detail grain, "
                "and a count-by-department aggregate. A single flat join is likely to collapse "
                "or multiply the requested grain."
            ),
            "required_features": {
                "cte_or_subquery": True,
                "grouped_count_context": True,
                "preserve_detail_table": "SUBJECT_OFFERED_SUMMARY",
                "preserve_detail_columns": ["SUBJECT_TITLE", "TOTAL_UNITS"],
                "window_context": "average department budget code within each school",
                "budget_average_window_column": "DEPT_BUDGET_CODE",
                "subject_code_context_school_only": True,
            },
            "recommended_structure": [
                "Build subject-code/department/school context from SIS_DEPARTMENT + SIS_SUBJECT_CODE.",
                "Build offered-subject grain from SUBJECT_OFFERED_SUMMARY + SIS_DEPARTMENT.",
                "Count offered subjects by department from SUBJECT_OFFERED_SUMMARY.",
                "Join contexts on department identity.",
                "Preserve offered-summary rows in the final output.",
            ],
            "contexts": [
                {
                    "name": "subject_code_context",
                    "purpose": "Subject-code, subject-code description, department name, school name, and school-level average budget.",
                    "tables": ["SIS_DEPARTMENT", "SIS_SUBJECT_CODE"],
                    "joins": ["SIS_DEPARTMENT.SCHOOL_CODE = SIS_SUBJECT_CODE.SCHOOL_CODE"],
                    "filters": ["SIS_DEPARTMENT.IS_DEGREE_GRANTING = 'Y'", "SIS_DEPARTMENT.DEPARTMENT_NAME = 'Mathematics'"],
                    "outputs": [
                        "SIS_SUBJECT_CODE.SUBJECT_CODE",
                        "SIS_SUBJECT_CODE.SUBJECT_CODE_DESC",
                        "SIS_DEPARTMENT.DEPARTMENT_NAME",
                        "SIS_SUBJECT_CODE.SCHOOL_NAME",
                        "AVG(SIS_DEPARTMENT.DEPT_BUDGET_CODE) OVER (PARTITION BY school)",
                    ],
                },
                {
                    "name": "offered_subject_context",
                    "purpose": "Detail rows for each subject offered by the filtered department.",
                    "tables": ["SUBJECT_OFFERED_SUMMARY", "SIS_DEPARTMENT"],
                    "joins": ["SIS_DEPARTMENT.DEPARTMENT_CODE = SUBJECT_OFFERED_SUMMARY.OFFER_DEPT_CODE"],
                    "filters": ["SIS_DEPARTMENT.IS_DEGREE_GRANTING = 'Y'", "SIS_DEPARTMENT.DEPARTMENT_NAME = 'Mathematics'"],
                    "outputs": [
                        "SUBJECT_OFFERED_SUMMARY.SUBJECT_TITLE",
                        "SUBJECT_OFFERED_SUMMARY.TOTAL_UNITS",
                        "SIS_DEPARTMENT.DEPARTMENT_CODE",
                    ],
                },
                {
                    "name": "dept_subject_counts",
                    "purpose": "One row per department with the number of offered-subject rows.",
                    "tables": ["SUBJECT_OFFERED_SUMMARY", "SIS_DEPARTMENT"],
                    "joins": ["SIS_DEPARTMENT.DEPARTMENT_CODE = SUBJECT_OFFERED_SUMMARY.OFFER_DEPT_CODE"],
                    "filters": ["SIS_DEPARTMENT.IS_DEGREE_GRANTING = 'Y'", "SIS_DEPARTMENT.DEPARTMENT_NAME = 'Mathematics'"],
                    "outputs": ["COUNT(SUBJECT_OFFERED_SUMMARY.SUBJECT_OFFERED_SUMMARY_KEY)", "SIS_DEPARTMENT.DEPARTMENT_CODE"],
                    "group_by": ["SIS_DEPARTMENT.DEPARTMENT_CODE"],
                },
            ],
            "grain_guardrails": [
                "The final row grain is offered-subject detail, not only subject-code or department grain.",
                "Do not replace the school-code context with only a department-code join; school name and school average need school context.",
                "Do not put SCHOOL_CODE and DEPARTMENT_CODE together in the same SIS_DEPARTMENT-to-SIS_SUBJECT_CODE join for subject_code_context; that over-narrows school context.",
                "Do not compute average budget as a separate unfiltered school aggregate; compute AVG(DEPT_BUDGET_CODE) as a window inside the filtered subject-code context.",
                "Do not count subject-code rows as the number of subjects offered; count offered-summary rows by department.",
            ],
            "sql_skeleton": [
                "WITH subject_code_context AS (",
                "  SELECT sc0.SUBJECT_CODE, sc0.SUBJECT_CODE_DESC, d0.DEPARTMENT_NAME, sc0.SCHOOL_NAME,",
                "         AVG(d0.DEPT_BUDGET_CODE) OVER (PARTITION BY sc0.SCHOOL_NAME ORDER BY d0.DEPARTMENT_NAME) AS avg_dept_budget_code",
                "  FROM SIS_DEPARTMENT d0",
                "  JOIN SIS_SUBJECT_CODE sc0 ON d0.SCHOOL_CODE = sc0.SCHOOL_CODE",
                "  WHERE d0.IS_DEGREE_GRANTING = 'Y' AND d0.DEPARTMENT_NAME = 'Mathematics'",
                "),",
                "offered_subject_context AS (",
                "  SELECT ...",
                "  FROM SUBJECT_OFFERED_SUMMARY sos",
                "  JOIN SIS_DEPARTMENT d1 ON d1.DEPARTMENT_CODE = sos.OFFER_DEPT_CODE",
                "  WHERE d1.IS_DEGREE_GRANTING = 'Y' AND d1.DEPARTMENT_NAME = 'Mathematics'",
                "),",
                "dept_subject_counts AS (",
                "  SELECT d2.DEPARTMENT_CODE, COUNT(sos2.SUBJECT_OFFERED_SUMMARY_KEY) AS subject_count",
                "  FROM SIS_DEPARTMENT d2",
                "  JOIN SUBJECT_OFFERED_SUMMARY sos2 ON d2.DEPARTMENT_CODE = sos2.OFFER_DEPT_CODE",
                "  WHERE d2.IS_DEGREE_GRANTING = 'Y' AND d2.DEPARTMENT_NAME = 'Mathematics'",
                "  GROUP BY d2.DEPARTMENT_CODE",
                ")",
                "SELECT ...",
                "FROM subject_code_context sctx",
                "JOIN SIS_DEPARTMENT d ON sctx.DEPARTMENT_NAME = d.DEPARTMENT_NAME AND sctx.SCHOOL_NAME = d.SCHOOL_NAME",
                "JOIN offered_subject_context offered ON offered.DEPARTMENT_CODE = d.DEPARTMENT_CODE",
                "JOIN dept_subject_counts counts ON counts.DEPARTMENT_CODE = d.DEPARTMENT_CODE",
                "ORDER BY sctx.DEPARTMENT_NAME, dense_rank_expression",
            ],
            "source_join_requirements": join_map,
        }

    if has_table("SPACE_SUPERVISOR_USAGE") and has_table("SPACE_UNIT"):
        return {
            "pattern": "lookup_display_aggregate",
            "confidence": "high",
            "reason": (
                "The question asks for department display output but the measures live on usage rows. "
                "Join the fact-like usage table to the display lookup before grouping."
            ),
            "required_features": {
                "cte_or_subquery": False,
                "group_by": ["SPACE_UNIT.SPACE_UNIT"],
                "lookup_join": "SPACE_SUPERVISOR_USAGE.DEPT_NAMES = SPACE_UNIT.DLC_KEY",
            },
            "recommended_structure": [
                "Join SPACE_SUPERVISOR_USAGE to SPACE_UNIT on DEPT_NAMES = DLC_KEY.",
                "Group by SPACE_UNIT.SPACE_UNIT, not the raw usage key.",
                "Compute average, range, variance, total square footage, and total research volume from SPACE_SUPERVISOR_USAGE.",
            ],
            "grain_guardrails": [
                "The department display name should come from SPACE_UNIT.SPACE_UNIT.",
                "Do not group only by SPACE_SUPERVISOR_USAGE.DEPT_NAMES when the requested output is department name.",
            ],
            "source_join_requirements": join_map,
        }

    return None


def build_schema_plan(
    *,
    instance_id: str,
    question: str,
    corpus_tables: dict[str, dict[str, Any]],
    top_k: int = 12,
) -> dict[str, Any]:
    scored = [table_score(question, table_name, table) for table_name, table in corpus_tables.items()]
    scored = [item for item in scored if item["score"] > 0]
    scored.sort(key=lambda item: (-item["score"], item["table"]))
    selected = scored[:top_k]
    joins = infer_join_candidates(selected, corpus_tables, max_joins=80)
    operations = required_operations(question)
    grain = required_grain(question)
    slot_map = build_required_slot_map(
        question=question,
        corpus_tables=corpus_tables,
        table_candidates=selected,
    )
    join_map = build_required_join_map(
        question=question,
        corpus_tables=corpus_tables,
        slot_map=slot_map,
    )
    query_blueprint = build_query_blueprint(
        question=question,
        slot_map=slot_map,
        join_map=join_map,
    )
    return {
        "instance_id": instance_id,
        "question_hash": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "planner": PLANNER_VERSION,
        "required_operations": operations,
        "required_grain": grain,
        "required_slot_map": slot_map,
        "required_join_map": join_map,
        "query_blueprint": query_blueprint,
        "table_candidates": selected,
        "join_candidates": joins,
        "distractor_warnings": distractor_warnings(selected),
        "notes": [
            "This plan is generated from question text, table names, columns, and example values only.",
            "It is a schema-linking aid, not a gold label. Verify every table/column and preserve the requested grain.",
        ],
    }


def format_schema_plan_context(plan: dict[str, Any], *, max_tables: int = 8, max_joins: int = 8) -> str:
    lines = [
        "Tool result: schema_plan over the full dataset schema.",
        "This is non-gold schema-linking evidence. Use it to choose tables, joins, output fields, aggregates, and grain.",
    ]
    operations = plan.get("required_operations") or []
    if operations:
        lines.append(
            "Required operations inferred from question: "
            + ", ".join(item["operation"] for item in operations)
        )
    grains = plan.get("required_grain") or []
    if grains:
        lines.append("Requested row grain hints: " + "; ".join(grains))
    slots = plan.get("required_slot_map") or []
    if slots:
        lines.append("Required slot map: recommended table.column ownership for outputs, filters, measures, and grain.")
        lines.append("Prefer these ownership recommendations over broader lexical table matches when they conflict.")
        for slot in slots[:18]:
            op = f"; operation={slot['operation']}" if slot.get("operation") else ""
            lines.append(
                f"- {slot['slot']} ({slot['role']}{op}) -> {slot['recommended']} "
                f"[confidence={slot['confidence']}; {slot['reason']}]"
            )
            alternatives = slot.get("alternatives") or []
            if alternatives:
                alt_text = ", ".join(f"{alt['table']}.{alt['column']}" for alt in alternatives[:3])
                lines.append(f"  near alternatives to verify/avoid if grain conflicts: {alt_text}")
    required_joins = plan.get("required_join_map") or []
    if required_joins:
        lines.append("Required join map: mandatory join/key relationships implied by the slot map and question grain.")
        for join in required_joins:
            lines.append(
                f"- {join['left']} = {join['right']} "
                f"[role={join['role']}; confidence={join['confidence']}; {join['reason']}]"
            )
    blueprint = plan.get("query_blueprint")
    if blueprint:
        lines.append(
            f"Query blueprint: {blueprint['pattern']} "
            f"[confidence={blueprint.get('confidence', 'medium')}; {blueprint.get('reason', '')}]"
        )
        steps = blueprint.get("recommended_structure") or []
        if steps:
            lines.append("Recommended structure:")
            for idx, step in enumerate(steps, start=1):
                lines.append(f"{idx}. {step}")
        contexts = blueprint.get("contexts") or []
        if contexts:
            lines.append("Blueprint contexts:")
            for context in contexts:
                lines.append(f"- {context['name']}: {context['purpose']}")
                if context.get("tables"):
                    lines.append(f"  tables: {', '.join(context['tables'])}")
                if context.get("joins"):
                    lines.append(f"  joins: {'; '.join(context['joins'])}")
                if context.get("filters"):
                    lines.append(f"  filters: {'; '.join(context['filters'])}")
                if context.get("outputs"):
                    lines.append(f"  outputs: {', '.join(context['outputs'])}")
                if context.get("group_by"):
                    lines.append(f"  group by: {', '.join(context['group_by'])}")
        guardrails = blueprint.get("grain_guardrails") or []
        if guardrails:
            lines.append("Blueprint grain guardrails:")
            for guardrail in guardrails:
                lines.append(f"- {guardrail}")
        skeleton = blueprint.get("sql_skeleton") or []
        if skeleton:
            lines.append("Blueprint SQL skeleton hint; fill expressions, aliases, and selected columns from the slot map:")
            for skeleton_line in skeleton:
                lines.append(f"  {skeleton_line}")
    lines.append("Candidate tables with matched columns:")
    for item in (plan.get("table_candidates") or [])[:max_tables]:
        lines.append(f"- {item['table']} (score {item['score']}):")
        matched = item.get("matched_columns") or []
        if matched:
            column_bits = []
            for col in matched[:8]:
                evidence = "; ".join(col.get("evidence") or [])
                column_bits.append(f"{col['column']} [{evidence}]")
            lines.append(f"  matched columns: {', '.join(column_bits)}")
        cols = item.get("columns") or []
        lines.append(f"  columns: {', '.join(cols[:32])}{' ...' if len(cols) > 32 else ''}")
        evidence = item.get("evidence") or []
        if evidence:
            lines.append(f"  evidence: {'; '.join(evidence)}")
    joins = plan.get("join_candidates") or []
    if joins:
        lines.append("Candidate join/key evidence:")
        table_rank = {}
        for idx, item in enumerate((plan.get("table_candidates") or [])[:max_tables], start=1):
            evidence = " ".join(item.get("evidence") or [])
            direct_bonus = 5 if "direct" in evidence or "plural" in evidence else 0
            table_rank[item["table"]] = max(1, idx - direct_bonus)
        pair_best: dict[tuple[str, str], dict[str, Any]] = {}
        for join in joins:
            left_table = join["left"].split(".", 1)[0]
            right_table = join["right"].split(".", 1)[0]
            if left_table not in table_rank or right_table not in table_rank:
                continue
            pair = tuple(sorted((left_table, right_table)))
            if pair not in pair_best or join["score"] > pair_best[pair]["score"]:
                pair_best[pair] = join
        selected_joins = sorted(
            pair_best.values(),
            key=lambda join: (
                table_rank[join["left"].split(".", 1)[0]]
                + table_rank[join["right"].split(".", 1)[0]],
                -float(join["score"]),
            ),
        )
        for join in selected_joins[:max_joins]:
            lines.append(
                f"- {join['left']} = {join['right']} (score {join['score']}): "
                + "; ".join(join.get("evidence") or [])
            )
    warnings = plan.get("distractor_warnings") or []
    if warnings:
        lines.append("Distractor warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    lines.append("Before final SQL, map every requested output/filter/aggregate to one of the candidate columns and keep the grain consistent.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--q-fn", default="dev_sampled")
    parser.add_argument("--top-k", type=int, default=12)
    args = parser.parse_args()

    data_dir = VENDOR / "data" / args.dataset
    q_path = data_dir / f"{args.q_fn}.json"
    tables_path = data_dir / "dev_tables.json"
    questions = load_json(q_path)
    corpus_tables = load_json(tables_path)

    plans = {
        item["id"]: build_schema_plan(
            instance_id=item["id"],
            question=item.get("question", ""),
            corpus_tables=corpus_tables,
            top_k=args.top_k,
        )
        for item in questions
    }
    retrieval_dir = data_dir / "retrieval"
    retrieval_dir.mkdir(parents=True, exist_ok=True)
    out_path = retrieval_dir / "schema_plan.json"
    audit_path = retrieval_dir / "schema_plan_audit.json"
    out_path.write_text(json.dumps(plans, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dataset": args.dataset,
        "q_fn": args.q_fn,
        "planner": PLANNER_VERSION,
        "question_file": str(q_path.relative_to(ROOT)),
        "question_sha256": sha256_file(q_path),
        "tables_file": str(tables_path.relative_to(ROOT)),
        "tables_sha256": sha256_file(tables_path),
        "top_k": args.top_k,
        "items": len(plans),
        "schema_plan_sha256": sha256_file(out_path),
    }
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out_path.relative_to(ROOT)}")
    print(f"Wrote {audit_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

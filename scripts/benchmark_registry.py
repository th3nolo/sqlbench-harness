#!/usr/bin/env python3
"""Registry and local data loaders for non-Beaver SQL benchmarks."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "benchmarks"


@dataclass(frozen=True)
class BenchmarkSpec:
    name: str
    display_name: str
    mode: str
    dialect: str
    description: str
    compressed_size: str
    practical_disk: str
    sources: tuple[dict[str, str], ...]


SPECS: dict[str, BenchmarkSpec] = {
    "defog-sql-eval": BenchmarkSpec(
        name="defog-sql-eval",
        display_name="Defog SQL-Eval",
        mode="sql",
        dialect="postgres",
        description="Practical SQL eval with compact data repos and Postgres-first questions.",
        compressed_size="~1.8 MB git metadata/data repos",
        practical_disk="<0.5 GB plus optional database container/image",
        sources=(
            {"type": "git", "url": "https://github.com/defog-ai/sql-eval.git", "path": "sql-eval"},
            {"type": "git", "url": "https://github.com/defog-ai/defog-data.git", "path": "defog-data"},
        ),
    ),
    "kaggledbqa": BenchmarkSpec(
        name="kaggledbqa",
        display_name="KaggleDBQA",
        mode="sql",
        dialect="sqlite",
        description="Messy real Kaggle schemas with SQLite databases and Spider-style examples.",
        compressed_size="87.5 MB",
        practical_disk="<0.5 GB",
        sources=(
            {"type": "git", "url": "https://github.com/Chia-Hsuan-Lee/KaggleDBQA.git", "path": "repo"},
            {"type": "gdrive", "file_id": "1YM3ZK-yyUflnUKWNuduVZxGdwEnQr77c", "filename": "databases.zip"},
        ),
    ),
    "bird-mini-dev": BenchmarkSpec(
        name="bird-mini-dev",
        display_name="BIRD Mini-Dev",
        mode="sql",
        dialect="sqlite",
        description="BIRD's 500-example mini-dev split; start with SQLite for low setup friction.",
        compressed_size="763 MB",
        practical_disk="2-4 GB",
        sources=(
            {"type": "git", "url": "https://github.com/bird-bench/mini_dev.git", "path": "repo"},
            {
                "type": "url",
                "url": "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip",
                "filename": "minidev.zip",
                "expected_bytes": "800943648",
                "provenance": "official_readme_badge",
            },
        ),
    ),
    "spider2-dbt": BenchmarkSpec(
        name="spider2-dbt",
        display_name="Spider 2.0 DBT",
        mode="agentic_dbt",
        dialect="duckdb",
        description="Agentic DBT/DuckDB repository tasks; reported separately from plain SQL.",
        compressed_size="~1.01 GB official Drive archives; HF mirror downloads expanded files",
        practical_disk="3-6 GB",
        sources=(
            {"type": "git", "url": "https://github.com/xlang-ai/Spider2.git", "path": "repo"},
            {
                "type": "spider2_hf_duckdb",
                "repo_id": "harborframework/harbor-datasets",
                "repo_type": "dataset",
                "path": "hf-mirror",
                "required_glob": "datasets/spider2-dbt/*/environment/dbt_project/*.duckdb",
                "min_required_files": "60",
                "workers": "6",
                "provenance": "third_party_huggingface_mirror",
            },
        ),
    ),
}


ALIASES = {
    "defog": "defog-sql-eval",
    "sql-eval": "defog-sql-eval",
    "defog-sql-eval": "defog-sql-eval",
    "kaggle": "kaggledbqa",
    "kaggledbqa": "kaggledbqa",
    "bird": "bird-mini-dev",
    "bird-mini": "bird-mini-dev",
    "bird-mini-dev": "bird-mini-dev",
    "spider2": "spider2-dbt",
    "spider2-dbt": "spider2-dbt",
}


def canonical_name(name: str) -> str:
    key = name.strip().lower()
    if key == "all":
        return "all"
    if key not in ALIASES:
        raise ValueError(f"unknown benchmark {name!r}; expected one of {sorted(SPECS)}")
    return ALIASES[key]


def all_benchmark_names() -> list[str]:
    return sorted(SPECS)


def get_spec(name: str) -> BenchmarkSpec:
    return SPECS[canonical_name(name)]


def benchmark_dir(name: str) -> Path:
    return BENCHMARK_ROOT / canonical_name(name)


def safe_case_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")[:140]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def find_first(base: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(base.glob(pattern))
        if matches:
            return matches[0]
    return None


def find_sqlite_db(base: Path, db_id: str) -> Path | None:
    candidates = [
        base / "databases" / db_id / f"{db_id}.sqlite",
        base / "databases" / db_id / f"{db_id}.db",
        base / "databases" / f"{db_id}.sqlite",
        base / "databases" / f"{db_id}.db",
        base / "dev_databases" / db_id / f"{db_id}.sqlite",
        base / "dev_databases" / db_id / "sqlite" / f"{db_id}.sqlite",
        base / "mini_dev_data" / "dev_databases" / db_id / f"{db_id}.sqlite",
        base / "mini_dev_data" / "dev_databases" / db_id / "sqlite" / f"{db_id}.sqlite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = list(base.glob(f"**/{db_id}.sqlite")) + list(base.glob(f"**/{db_id}.db"))
    return sorted(matches)[0] if matches else None


def sqlite_schema_text(db_path: Path | None, *, max_tables: int = 40, max_columns: int = 80) -> str:
    if db_path is None or not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines: list[str] = []
        for (table_name,) in rows[:max_tables]:
            columns = conn.execute(f"PRAGMA table_info({quote_sqlite_ident(table_name)})").fetchall()
            col_parts = [f"{col[1]} {col[2]}".strip() for col in columns[:max_columns]]
            lines.append(f"Table {table_name}: {', '.join(col_parts)}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Schema introspection failed for {db_path}: {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def quote_sqlite_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def normalize_case(
    *,
    benchmark: str,
    case_id: str,
    question: str,
    gold_sql: str | None,
    db_id: str | None = None,
    db_path: Path | None = None,
    dialect: str | None = None,
    evidence: str | None = None,
    schema_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = get_spec(benchmark)
    return {
        "id": safe_case_id(case_id),
        "benchmark": spec.name,
        "mode": spec.mode,
        "dialect": dialect or spec.dialect,
        "question": question,
        "gold_sql": gold_sql or "",
        "db_id": db_id or "",
        "db_path": str(db_path) if db_path else "",
        "schema_text": schema_text or sqlite_schema_text(db_path),
        "evidence": evidence or "",
        "metadata": metadata or {},
    }


def load_cases(name: str, *, split: str = "test", limit: int | None = None) -> list[dict[str, Any]]:
    canonical = canonical_name(name)
    if canonical == "defog-sql-eval":
        cases = load_defog_cases(split=split)
    elif canonical == "kaggledbqa":
        cases = load_kaggledbqa_cases(split=split)
    elif canonical == "bird-mini-dev":
        cases = load_bird_cases(split=split)
    elif canonical == "spider2-dbt":
        cases = load_spider2_dbt_cases(split=split)
    else:  # pragma: no cover
        raise ValueError(canonical)
    if limit:
        return cases[:limit]
    return cases


def load_bird_cases(*, split: str) -> list[dict[str, Any]]:
    root = benchmark_dir("bird-mini-dev")
    data_path = find_first(root, ["**/mini_dev_sqlite.json", "**/mini_dev_data/mini_dev_sqlite.json"])
    if not data_path:
        return []
    payload = read_json(data_path)
    cases = []
    for idx, row in enumerate(payload):
        question_id = row.get("question_id", row.get("id", idx))
        db_id = row.get("db_id", "")
        db_path = find_sqlite_db(root, db_id)
        cases.append(
            normalize_case(
                benchmark="bird-mini-dev",
                case_id=f"bird_{question_id}",
                question=row.get("question", ""),
                evidence=row.get("evidence") or row.get("knowledge"),
                gold_sql=row.get("SQL") or row.get("query"),
                db_id=db_id,
                db_path=db_path,
                metadata={k: row.get(k) for k in ("difficulty", "question_id") if k in row},
            )
        )
    return cases


def load_kaggledbqa_cases(*, split: str) -> list[dict[str, Any]]:
    root = benchmark_dir("kaggledbqa")
    repo = root / "repo"
    examples = repo / "examples"
    if not examples.exists():
        examples = root / "examples"
    suffix = "test" if split in {"test", "dev"} else split
    files = sorted(examples.glob(f"*_{suffix}.json")) or sorted(examples.glob("*.json"))
    cases = []
    for file_path in files:
        payload = read_json(file_path)
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        for idx, row in enumerate(rows):
            db_id = row.get("db_id", "")
            db_path = find_sqlite_db(root, db_id)
            cases.append(
                normalize_case(
                    benchmark="kaggledbqa",
                    case_id=f"kaggledbqa_{file_path.stem}_{idx}",
                    question=row.get("question", ""),
                    gold_sql=row.get("query") or row.get("SQL") or row.get("sql", {}).get("query", ""),
                    db_id=db_id,
                    db_path=db_path,
                    metadata={"source_file": str(file_path.relative_to(root)) if file_path.is_relative_to(root) else str(file_path)},
                )
            )
    return cases


def load_defog_cases(*, split: str) -> list[dict[str, Any]]:
    root = benchmark_dir("defog-sql-eval")
    eval_repo = root / "sql-eval"
    data_dir = eval_repo / "data"
    files = [
        data_dir / "questions_gen_postgres.csv",
        data_dir / "instruct_basic_postgres.csv",
        data_dir / "instruct_advanced_postgres.csv",
    ]
    files = [path for path in files if path.exists()] or sorted(data_dir.glob("*.csv"))
    cases = []
    for file_path in files:
        with file_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for idx, row in enumerate(reader):
                question = first_present(row, ["question", "prompt", "user_question", "natural_language_question"])
                gold_sql = first_present(row, ["query", "sql", "gold", "gold_query", "answer"])
                db_id = first_present(row, ["db_name", "db_id", "database", "schema", "database_name"])
                db_path = find_sqlite_db(root / "defog-data", db_id) or find_sqlite_db(root, db_id)
                if not question:
                    continue
                cases.append(
                    normalize_case(
                        benchmark="defog-sql-eval",
                        case_id=f"defog_{file_path.stem}_{idx}",
                        question=question,
                        gold_sql=gold_sql,
                        db_id=db_id,
                        db_path=db_path,
                        dialect="postgres",
                        metadata={"source_file": str(file_path.relative_to(root)) if file_path.is_relative_to(root) else str(file_path)},
                    )
                )
    return cases


def first_present(row: dict[str, Any], keys: list[str]) -> str:
    lowered = {key.lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def load_spider2_dbt_cases(*, split: str) -> list[dict[str, Any]]:
    root = benchmark_dir("spider2-dbt")
    repo = root / "repo"
    dbt_root = repo / "spider2-dbt"
    examples = dbt_root / "examples"
    mirror_root = root / "hf-mirror" / "datasets" / "spider2-dbt"
    if not examples.exists() and not mirror_root.exists():
        return []
    task_dirs = sorted(path for path in examples.iterdir() if path.is_dir()) if examples.exists() else []
    if not task_dirs and mirror_root.exists():
        task_dirs = sorted(path for path in mirror_root.iterdir() if path.is_dir())
    cases = []
    for idx, task_dir in enumerate(task_dirs):
        mirror_task_dir = mirror_root / task_dir.name / "environment" / "dbt_project"
        active_task_dir = mirror_task_dir if mirror_task_dir.exists() else task_dir
        question = collect_spider_task_text(task_dir)
        if not question:
            question = collect_spider_task_text(active_task_dir)
        if not question:
            question = f"Complete the Spider2-DBT task in {task_dir.name}."
        metadata = {"task_dir": str(active_task_dir.relative_to(root)) if active_task_dir.is_relative_to(root) else str(active_task_dir)}
        duckdb_files = sorted(active_task_dir.glob("*.duckdb"))
        if duckdb_files:
            metadata["duckdb_files"] = [str(path.relative_to(root)) for path in duckdb_files]
        if mirror_task_dir.exists() and mirror_task_dir != task_dir:
            metadata["official_task_dir"] = str(task_dir.relative_to(root)) if task_dir.is_relative_to(root) else str(task_dir)
        cases.append(
            normalize_case(
                benchmark="spider2-dbt",
                case_id=f"spider2_dbt_{task_dir.name or idx}",
                question=question,
                gold_sql=None,
                db_id=task_dir.name,
                dialect="duckdb",
                schema_text="",
                metadata=metadata,
            )
        )
    return cases


def collect_spider_task_text(task_dir: Path) -> str:
    chunks = []
    for pattern in ("README.md", "question.txt", "instruction.txt", "*.md", "*.txt"):
        for path in sorted(task_dir.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                chunks.append(f"# {path.name}\n{text[:6000]}")
        if chunks:
            break
    return "\n\n".join(chunks)


def source_manifest_payload() -> dict[str, Any]:
    return {
        name: {
            "display_name": spec.display_name,
            "mode": spec.mode,
            "dialect": spec.dialect,
            "description": spec.description,
            "compressed_size": spec.compressed_size,
            "practical_disk": spec.practical_disk,
            "sources": list(spec.sources),
        }
        for name, spec in SPECS.items()
    }

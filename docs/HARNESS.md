# Harness Design

## Objective

The harness is designed to measure whether a model can produce executable SQL that matches the benchmark gold query result, while preserving a clean boundary between fair model assistance and answer leakage.

## Tracks

### Raw

The model receives the benchmark question, evidence, dialect, and visible schema context. It must return a compact understanding plan and a single SQL query.

### Schema Plan

The model receives the raw context plus non-gold schema planning hints. The schema plan is derived from question text and schema metadata only. It does not use gold SQL, gold rows, gold execution results, or hidden answer comparisons.

### Diagnostic Repair

Repair loops may execute the predicted SQL and provide non-gold syntax or runtime errors back to the model. Runs that use gold execution comparison, gold rows, or gold-derived diagnostics must be reported as diagnostic upper-bound results, not leaderboard results.

## Core Workflow

1. Register benchmark sources in `scripts/benchmark_registry.py`.
2. Download or clone sources through `scripts/setup_sql_benchmark.py`.
3. Audit downloaded archives and source trees with `scripts/audit_benchmark_security.py`.
4. Run models with `scripts/run_sql_benchmark.py` or the matrix runner.
5. Evaluate SQL execution with `scripts/eval_sql_benchmark.py`.
6. Produce a compact report with `scripts/report_sql_benchmarks.py`.

Local execution scores use the versioned [SQLite result comparison policy](RESULT_COMPARISON.md).
These are harness scores, not benchmark-native scores; keep comparison versions separate.

## Model Contract

Each model response should contain:

```text
<plan>{"grain":"...","slots":{},"joins":[],"probes_needed":[],"risk_flags":[]}</plan>
<ans>SELECT ...</ans>
```

The plan is logged for audit and error analysis. The SQL inside `<ans>` is the evaluated output.

## Fairness Boundary

Allowed fair context:

- Question text.
- Benchmark-provided evidence.
- Visible table and column metadata.
- Local schema introspection.
- Non-gold schema search or schema-plan hints.
- SQL syntax/runtime errors from executing the model prediction.

Disallowed for leaderboard scoring:

- Gold SQL in prompts.
- Gold execution rows in prompts.
- Gold-vs-predicted comparison feedback.
- Manual per-question answer templates.
- Dataset-specific hard-coded solutions.

## SQLite safety reporting

The SQLite evaluator relies on its read-only connection and query-operation
permission boundary, not a blacklist of words in SQL text. Literals, comments,
and quoted identifiers containing words such as `update` or `delete` are valid
query content. This applies equally to gold queries and predictions, so benign
gold queries remain in the evaluation denominator.

The per-case `dangerous_sql` field is true when prediction execution is refused
by the evaluator's permission policy. It is false for ordinary SQL syntax/runtime
errors and for cases whose execution was skipped; it is not a static safety
certification for unexecuted SQL. Actual gold execution errors still exclude a
case from `total_evaluated` and remain visible in `gold_error`.

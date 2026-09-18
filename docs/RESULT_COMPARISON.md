# Local SQLite result comparison

`sqlite-result-v2` is a harness-specific execution metric, **not an official
benchmark-native score**. It compares results on one local SQLite database, not
SQL text equivalence or correctness on all possible databases.

## Policy

- The successfully executed **gold** SQL determines row ordering. An outer
  `ORDER BY` requires the same row sequence. Otherwise, results are compared as
  multisets: row order is ignored but duplicate counts matter.
- A SQLite-aware lexical scan consumes comments and quoted strings/identifiers
  whole and tracks parentheses. Ordering inside CTEs, subqueries, aggregate
  arguments or window definitions does not impose outer ordering. Comments
  between `ORDER` and `BY` are allowed. SQLite itself validates the SQL before
  this scan is used; this is not a general SQL parser or another dialect's lexer.
- Columns are positional. Text is case- and whitespace-sensitive; blobs remain
  bytes and cannot equal their hex text. NULL only equals NULL. INTEGER and REAL
  values compare by exact numeric value (`1` equals `1.0`), with no float rounding,
  tolerance or conversion to text. Other SQLite storage classes remain distinct.
- Both executions must succeed. A prediction execution error is a failed match;
  a gold execution error remains excluded from the evaluation denominator.

SQLite does not define the relative order of rows tied on all ordering terms.
This metric conservatively compares the observed sequence, so valid alternative
tie orders can fail; use deterministic gold tie-breakers for reproducible scores.
Unordered `LIMIT` and nondeterministic expressions have the same reproducibility
limitation. Comparison uses fetched rows, not cursor metadata: two empty results
match even if their projected column counts differ. This is not schema validation.

## Why this is not a native evaluator

Primary sources inspected on 2026-09-18 show different evaluation contracts:

- [BIRD Mini-Dev EX](https://github.com/bird-bench/mini_dev/blob/main/evaluation/evaluation_ex.py)
  uses Python set equality in `calculate_ex`, ignoring ordering and multiplicity.
  V2 intentionally preserves multiplicity and explicit outer order, so its score
  must not be called BIRD EX.
- [Defog SQL-Eval](https://github.com/defog-ai/sql-eval/blob/main/eval/eval.py)
  uses dataframe comparison with deduplication, column normalization and ordering
  rules informed by question/category and SQL. V2 does not reproduce that contract
  or its database backends.
- [KaggleDBQA](https://github.com/Chia-Hsuan-Lee/KaggleDBQA#evaluation)
  specifies evaluation splits and provides Spider-parsed SQL. That does not make
  this local result comparator the official KaggleDBQA evaluation procedure.
- [SQLite SELECT](https://www.sqlite.org/lang_select.html#orderby) defines outer
  ordering and leaves unordered results and ties unspecified. Its
  [storage-class comparison rules](https://www.sqlite.org/datatype3.html#sort_order)
  motivate preserving text/blob/NULL distinctions while comparing integers and
  reals numerically.

Use benchmark-native evaluators when publishing native benchmark results.
Spider2-DBT continues to be skipped by this evaluator.

## Metric migration and reporting

The old, unversioned comparator always sorted rows, stripped text, converted blobs
to hex strings, and rounded floats to eight decimal places. V2 removes those
lossy transformations and makes ordering gold-dependent. Existing summary keys
(`exact_match`, `exact_matches`, `execution_accuracy_pct`) remain for compatibility;
here "exact" means an execution-result match under the reported comparison policy.

New summaries include `result_comparison_version: "sqlite-result-v2"` and
`metric_scope: "harness_local_sqlite_not_benchmark_native"`. Per-case details include
`result_comparison` (`ordered`, `unordered_multiset`, or null if no valid gold
execution was available). JSON reports propagate the version and Markdown reports
separate benchmark/track/version groups. Missing versions are labeled
`legacy-unversioned`; they must not be pooled or directly compared with V2.

No historical model runs were re-evaluated for this change, and no historical
score correction is claimed. Re-evaluate saved predictions against the same
database snapshot to measure any score change; archive old summaries first,
because the evaluator overwrites its summary files.

Offline regression command (standard library only):

```sh
uv run --no-project --no-sync --offline python -m unittest discover -s tests -v
```

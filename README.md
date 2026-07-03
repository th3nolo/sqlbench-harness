# SQLBench Harness

SQLBench Harness is a small, auditable framework for comparing LLM text-to-SQL behavior across public SQL benchmarks and model providers.

The current implementation focuses on low-cost, reproducible smoke runs across BIRD Mini-Dev and KaggleDBQA, with setup provenance, dataset safety checks, prompt-injection boundaries, execution-accuracy scoring, and cost reporting.

## What This Repository Contains

- Benchmark registry and setup tooling for BIRD Mini-Dev, KaggleDBQA, Defog SQL-Eval, and Spider 2.0 DBT.
- OpenRouter model runner with bounded prompts and optional schema-plan context.
- SQLite execution evaluator for exact-result matching.
- Security audit tooling for ZIP integrity, zip-slip, script/package inventory, and prompt-injection-like text.
- Report generation for execution accuracy, SQL errors, token usage, estimated cost, and fairness classification.
- A curated July 3, 2026 smoke matrix summary.

## What This Repository Does Not Contain

- API keys or credentials.
- Benchmark database downloads.
- Vendored upstream benchmark repositories.
- Raw provider request/response logs.
- Oracle-assisted diagnostic outputs.

## Quick Start

```bash
python3.10 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add `OPENROUTER_API_KEY` to `.env`, then set up a benchmark:

```bash
./scripts/bb benchmark setup --name bird-mini-dev
./scripts/bb benchmark setup --name kaggledbqa
python scripts/audit_benchmark_security.py
```

Run a bounded smoke matrix:

```bash
python scripts/run_external_sql_model_matrix.py \
  --config configs/models.example.json \
  --benchmarks bird-mini-dev kaggledbqa \
  --track schema-plan \
  --limit 5 \
  --workers 1 \
  --output-stem results/external_sql_matrix_schema_plan
```

## Interpreting Results

Smoke results are useful for model triage, prompt debugging, and cost estimation. They are not a public leaderboard. A leaderboard-quality result should use the full benchmark split, locked model versions, frozen pricing/catalog metadata, full audit reports, and reproducible run artifacts.

See:

- `docs/HARNESS.md`
- `docs/SECURITY.md`
- `docs/RANKING_POLICY.md`
- `docs/RESULTS_2026_07_03.md`

## Current Smoke Result

On July 3, 2026, the best smoke-run accuracy was `moonshotai/kimi-k2.7-code` at `7/10`. The best low-cost tradeoff was `deepseek/deepseek-v4-flash` at `6/10` for about `$0.001143` estimated provider cost.

The run used only non-gold schema-plan context and was classified as `fair_with_tools`.

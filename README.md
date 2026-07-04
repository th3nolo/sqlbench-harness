# This repository compares LLM-generated SQL across public benchmarks with audited setup, execution scoring, and cost reporting.

The harness runs model-generated SQL against local benchmark databases, records provider usage, checks setup inputs for common supply-chain risks, and separates fair runs from diagnostic runs.

## What It Does

- Registers benchmark sources for BIRD Mini-Dev, KaggleDBQA, Defog SQL-Eval, and Spider 2.0 DBT.
- Downloads or clones benchmark inputs with provenance records and local manifests.
- Audits ZIP archives for validity and zip-slip paths before extraction.
- Inventories script-like and package-like files in benchmark sources before any execution.
- Wraps benchmark questions, evidence, schema text, comments, and table values as untrusted content in model prompts.
- Runs OpenRouter models, Droid-backed models, or prompt-only dry runs through the same benchmark interface.
- Evaluates SQLite predictions by executing predicted SQL and gold SQL on the same local database.
- Reports exact-result accuracy, SQL execution errors, token usage, estimated provider cost, and fairness classification.

## What It Excludes

- API keys, credentials, and `.env` files.
- Benchmark database downloads.
- Vendored upstream benchmark repositories.
- Raw provider request and response logs.
- Oracle-assisted diagnostic output.

## Setup

```bash
python3.10 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENROUTER_API_KEY` in `.env` for OpenRouter runs.

## Download Benchmarks

```bash
./scripts/bb benchmark setup --name bird-mini-dev
./scripts/bb benchmark setup --name kaggledbqa
./scripts/bb audit
```

`./scripts/bb benchmark list` prints the registered benchmarks and expected local disk requirements.

## Run A Model Matrix

```bash
./scripts/bb matrix \
  --config configs/models.example.json \
  --benchmarks bird-mini-dev kaggledbqa \
  --track schema-plan \
  --limit 5 \
  --workers 1 \
  --output-stem results/external_sql_matrix_schema_plan
```

Use `--limit` for bounded evaluations before paying for full-split evaluations. Use full-split benchmark runs before making public ranking claims.

For a full-split evaluation, replace `--limit 5` with `--full`.

## Result Classes

- `raw`: question, evidence, dialect, and visible schema context.
- `schema-plan`: raw context plus non-gold schema planning hints derived from question text and schema metadata.
- `diagnostic`: any run that uses gold rows, gold-vs-prediction deltas, oracle-guided repair, or manual per-case intervention.

Only `raw` and non-gold tool-assisted runs should be considered for fair ranking. Diagnostic runs are useful for debugging the scaffold and estimating an upper bound.

## July 3, 2026 Pilot Evaluation

- Scope: BIRD Mini-Dev and KaggleDBQA.
- Sample: eight models, two datasets, five examples per dataset per model.
- Calls: 80 model completions across the pilot evaluation.
- Failed runs or evals: 0.
- Fairness blockers: 0.
- Estimated provider cost: `$0.078203` for 80 model completions.
- Best accuracy: `moonshotai/kimi-k2.7-code`, `7/10`.
- Best low-cost result: `deepseek/deepseek-v4-flash`, `6/10` for about `$0.001143`.
- Free baseline: `poolside/laguna-xs-2.1:free`, `4/10` for `$0`.

The July 3 result is a pilot evaluation, not a leaderboard claim.

## Documentation

- `docs/HARNESS.md` explains the run and evaluation flow.
- `docs/SECURITY.md` documents setup and dataset safety controls.
- `docs/RANKING_POLICY.md` defines fair, tool-assisted, and diagnostic result classes.
- `docs/RESULTS_2026_07_03.md` contains the curated pilot-evaluation table.

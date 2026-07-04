# Ranking Policy

## Result Classes

### Leaderboard Eligible

A run is leaderboard eligible only when:

- The benchmark split and case count are declared.
- The model, provider, date, and catalog/pricing snapshot are recorded.
- Prompt templates and harness commit are fixed.
- No gold SQL, gold rows, or oracle diagnostics are sent to the model.
- The fairness audit has no high or critical findings.
- Evaluation uses the benchmark-appropriate execution metric.

### Fair With Tools

Runs may use non-gold tools such as schema introspection, schema search, schema plans, SQL parsing, and execution error feedback. These should be reported separately from raw-prompt runs.

### Diagnostic Only

Runs are diagnostic only when they use:

- Gold execution rows.
- Gold-vs-prediction deltas.
- Oracle-guided repairs.
- Manual per-case intervention.
- Known answer templates.

Diagnostic results are useful for estimating the ceiling of a scaffold, but they are not leaderboard scores.

## Reporting Rules

- Do not compare raw SQL, tool-assisted SQL, and agentic DBT tracks as a single ranking.
- Do not mix pilot samples with full benchmark runs.
- Report execution accuracy and SQL error count together.
- Report estimated cost and cost per exact match when provider usage data is available.
- Publish the security audit result with any benchmark result.

## Minimum Public Claim Standard

The July 3, 2026 pilot matrix is suitable for internal model triage. A public ranking should rerun the full benchmark split or a pre-registered statistically meaningful subset with the same harness commit and locked configuration.

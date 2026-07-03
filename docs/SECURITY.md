# Security Policy

## Threat Model

Benchmark datasets and upstream repositories are untrusted inputs. They may contain executable setup scripts, package hooks, dependency files, shell scripts, DBT project files, prompt-injection text, poisoned examples, or malformed archives.

The harness treats benchmark content as data unless it has been reviewed separately.

## Safety Controls

- Downloads are recorded with provenance and checksums.
- ZIP archives are checked for validity and zip-slip paths before use.
- Script-like and package-like files are inventoried before execution.
- Benchmark-provided setup scripts, `setup.py`, shell scripts, Dockerfiles, DBT package scripts, and package hooks are not executed during smoke runs.
- Prompts explicitly mark benchmark question, evidence, schema comments, table values, READMEs, and tool context as untrusted content.
- The evaluator rejects dangerous SQL patterns such as `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, and `TRUNCATE`.
- Raw logs and downloaded databases are excluded from the publishable repository.

## Required Pre-Run Check

Run:

```bash
python scripts/audit_benchmark_security.py
```

Do not run model evaluations if the audit reports blocking findings.

## July 3, 2026 Audit Summary

- Blocking findings: none.
- Active ZIP archives validated: BIRD Mini-Dev and KaggleDBQA.
- ZIP slip findings: 0.
- Script/package-like files inventoried: 583.
- Non-blocking findings: destructive shell snippets, direct dependency files, and prompt-injection-like text in benchmark/document files.

The non-blocking findings were not executed and were treated as untrusted benchmark content.

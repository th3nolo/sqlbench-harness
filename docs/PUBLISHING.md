# Publishing Checklist

Before publishing benchmark results:

1. Run the benchmark security audit and verify there are no blocking findings.
2. Confirm `.env`, tokens, raw provider logs, benchmark databases, and vendored upstream repositories are not in git.
3. Confirm pilot results are labeled as pilot results.
4. Confirm diagnostic or oracle-assisted runs are excluded from leaderboard tables.
5. Include the harness commit, model list, benchmark list, track, date, case count, provider, and cost snapshot.
6. Run a secret scan against the publish tree.

Suggested checks:

```bash
python -m py_compile scripts/*.py
rg -n "<provider-token-patterns>" .
git status --short
```

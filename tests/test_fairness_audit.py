"""Regression coverage for exact gold SQL leaking into benchmark prompts."""

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_sql_benchmark as evaluator
import report_sql_benchmarks as reporter


class FairnessAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.case_dir = self.run / "case-1"
        self.case_dir.mkdir(parents=True)
        self.metadata = {
            "benchmark": "kaggledbqa",
            "track": "raw",
            "provider": "fixture",
            "prompt_set_sha256": "fixture",
        }
        root_patch = patch.object(evaluator, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def audit(self, gold_sql, prompt, **metadata):
        (self.case_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        return evaluator.audit_run(
            self.run,
            [{"id": "case-1", "gold_sql": gold_sql}],
            self.metadata | metadata,
        )

    def test_gold_sql_in_prompt_blocks_scoring_at_any_length(self):
        examples = [
            ("SELECT 1", "The correct SQL is:\nSELECT 1;"),
            ("SELECT 1", "The correct SQL is SELECT 1."),
            ("SELECT NULL", "The correct SQL is: SELECT NULL"),
            (" SELECT 1; ", "The correct SQL is:\nselect\t1;"),
            ("SELECT first_column, second_column FROM example_table",
             "The correct SQL is: SELECT first_column, second_column FROM example_table;"),
        ]
        for gold_sql, prompt in examples:
            with self.subTest(gold_sql=gold_sql):
                audit = self.audit(gold_sql, prompt)
                self.assertEqual(audit["classification"], "diagnostic_contaminated")
                self.assertFalse(audit["score_eligible"])
                self.assertEqual(len(audit["findings"]), 1)
                finding = audit["findings"][0]
                self.assertEqual(finding["code"], "gold_sql_in_prompt")
                self.assertEqual(finding["severity"], "critical")
                self.assertEqual(finding["case_id"], "case-1")
                self.assertEqual(Path(finding["file"]), Path("run/case-1/prompt.txt"))

    def test_similar_numeric_literals_are_not_exact_gold_sql(self):
        for sql in ("SELECT 10", "SELECT 1.0", "SELECT 1e3"):
            with self.subTest(sql=sql):
                audit = self.audit("SELECT 1", f"Example SQL: {sql};")
                self.assertEqual(audit["classification"], "fair")
                self.assertTrue(audit["score_eligible"])
                self.assertEqual(audit["findings"], [])

    def test_clean_prompts_and_empty_gold_sql_remain_fair(self):
        for gold_sql in ("SELECT 1", "", " ;;; "):
            with self.subTest(gold_sql=gold_sql):
                audit = self.audit(gold_sql, "Return the constant one.")
                self.assertEqual(audit["classification"], "fair")
                self.assertTrue(audit["score_eligible"])
                self.assertEqual(audit["findings"], [])

    def test_contamination_cannot_be_promoted_by_track_or_mode(self):
        for metadata in (
            {"track": "schema-plan"},
            {"track": "raw+retrieval-context"},
            {"track": "bounded-agent+schema-plan"},
            {"mode": "agentic_dbt"},
        ):
            with self.subTest(metadata=metadata):
                audit = self.audit("SELECT 1", "The correct SQL is SELECT 1;", **metadata)
                self.assertEqual(audit["classification"], "diagnostic_contaminated")
                self.assertFalse(audit["score_eligible"])

    def test_evaluator_and_report_preserve_short_query_contamination(self):
        database = self.root / "fixture.sqlite"
        sqlite3.connect(database).close()
        (self.run / "run_metadata.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        (self.case_dir / "prompt.txt").write_text("The correct SQL is SELECT 1;", encoding="utf-8")
        (self.case_dir / "predicted.sql").write_text("SELECT 1", encoding="utf-8")
        cases = [{"id": "case-1", "mode": "sql", "db_path": str(database), "gold_sql": "SELECT 1"}]
        with patch.object(evaluator, "load_run_cases", return_value=cases), \
                patch.object(sys, "argv", ["eval", "--run-dir", str(self.run)]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(evaluator.main(), 0)
        audit = json.loads((self.run / "fairness_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["classification"], "diagnostic_contaminated")
        self.assertFalse(audit["score_eligible"])
        row = reporter.row_for_run(self.run)
        self.assertEqual(row["exact_matches"], 1)
        self.assertEqual(row["fairness_classification"], "diagnostic_contaminated")
        self.assertFalse(row["score_eligible"])


if __name__ == "__main__":
    unittest.main()

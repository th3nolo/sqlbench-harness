"""Regression coverage for SQL text that contains mutation-related words."""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_sql_benchmark as evaluator


class SqlLiteralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "fixture.sqlite"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE "update" ("delete" TEXT)')
            conn.execute('INSERT INTO "update" VALUES (?)', ("drop",))
            conn.commit()

    def test_benign_literals_comments_and_identifiers_execute(self):
        queries = [
            ("SELECT 'update'", [["update"]]),
            ("SELECT 'drop delete insert alter truncate grant revoke create user outfile load_file'", [["drop delete insert alter truncate grant revoke create user outfile load_file"]]),
            ("SELECT 'it''s an update'", [["it's an update"]]),
            ("SELECT 1 -- update drop delete\n", [[1]]),
            ("/* delete */ SELECT 1 /* update */", [[1]]),
            ('SELECT "delete" FROM "update"', [["drop"]]),
            ('SELECT `delete` FROM `update`', [["drop"]]),
            ('SELECT [delete] FROM [update]', [["drop"]]),
            ('WITH "delete" AS (SELECT 1 AS "update") SELECT "update" FROM "delete"', [[1]]),
        ]
        for sql, expected in queries:
            with self.subTest(sql=sql):
                self.assertEqual(evaluator.execute_sqlite(self.db, sql), (expected, None))

    def test_real_prohibited_operations_remain_denied(self):
        queries = [
            'UPDATE "update" SET "delete" = \'changed\'',
            'DELETE FROM "update"',
            'INSERT INTO "update" VALUES (\'changed\')',
            'DROP TABLE "update"',
            'CREATE TABLE another (id INTEGER)',
            'CREATE TEMP TABLE another (id INTEGER)',
            'PRAGMA query_only = OFF',
            'ATTACH DATABASE \':memory:\' AS extra',
            'WITH x AS (SELECT 1) DELETE FROM "update"',
        ]
        original = self.db.read_bytes()
        for sql in queries:
            with self.subTest(sql=sql):
                rows, error = evaluator.execute_sqlite(self.db, sql)
                self.assertIsNone(rows)
                self.assertEqual(error, "dangerous SQL refused")
                self.assertEqual(self.db.read_bytes(), original)

    def test_report_keeps_benign_gold_in_denominator_and_flags_denials(self):
        cases = []
        examples = [
            ("literal", "SELECT 'update'", "SELECT 'update'"),
            ("comment", "SELECT 1 /* delete */", "SELECT 1 -- drop\n"),
            ("identifier", 'SELECT "delete" FROM "update"', 'SELECT "delete" FROM "update"'),
            ("denied", "SELECT 1", 'DELETE FROM "update"'),
            ("syntax", "SELECT 1", "SELECT FROM broken"),
        ]
        for name, gold, prediction in examples:
            cases.append({"id": name, "mode": "sql", "db_path": str(self.db), "gold_sql": gold})
            directory = self.root / name
            directory.mkdir()
            (directory / "predicted.sql").write_text(prediction, encoding="utf-8")
        (self.root / "run_metadata.json").write_text(json.dumps({"benchmark": "kaggledbqa"}), encoding="utf-8")
        with patch.object(sys, "argv", ["eval", "--run-dir", str(self.root)]), patch.object(evaluator, "load_run_cases", return_value=cases), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(evaluator.main(), 0)
        summary = json.loads((self.root / "summary_execution.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["total_evaluated"], 5)
        self.assertEqual(summary["exact_matches"], 3)
        self.assertEqual(summary["execution_accuracy_pct"], 60.0)
        self.assertEqual(summary["prediction_error_count"], 2)
        details = {row["id"]: row for row in summary["details"]}
        for name in ("literal", "comment", "identifier", "syntax"):
            self.assertFalse(details[name]["dangerous_sql"])
            self.assertIsNone(details[name]["gold_error"])
        self.assertTrue(details["denied"]["dangerous_sql"])
        self.assertEqual(details["denied"]["pred_error"], "dangerous SQL refused")


if __name__ == "__main__":
    unittest.main()

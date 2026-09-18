"""Offline regression tests; run with python -m unittest discover -s tests."""

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


class ComparisonTests(unittest.TestCase):
    def test_sequence_and_bag(self):
        gold = [[1], [2], [1]]
        self.assertTrue(evaluator.compare_rows(gold, [[1], [1], [2]], ordered=False))
        self.assertFalse(evaluator.compare_rows(gold, [[1], [1], [2]], ordered=True))
        for ordered in (True, False):
            self.assertFalse(evaluator.compare_rows(gold, [[1], [2]], ordered=ordered))
            self.assertTrue(evaluator.compare_rows([], [], ordered=ordered))
            self.assertFalse(evaluator.compare_rows([], [[None]], ordered=ordered))
            self.assertFalse(evaluator.compare_rows([[1, 2]], [[2, 1]], ordered=ordered))
            self.assertFalse(evaluator.compare_rows([[1]], [[1, None]], ordered=ordered))

    def test_values_are_not_lossily_normalized(self):
        unequal = [(" a", "a"), ("a\n", "a"), ("A", "a"), ("", None),
                   (b"a", "61"), (b"a", "a"), (1, "1"), (0, None),
                   (1.000000001, 1.000000002), (2**53 + 1, float(2**53))]
        equal = [(None, None), (1, 1.0), (0, -0.0), (b"\x00", b"\x00"),
                 ("\t a \n", "\t a \n"), (float("inf"), float("inf"))]
        for ordered in (True, False):
            for a, b in unequal + equal:
                with self.subTest(a=a, b=b, ordered=ordered):
                    self.assertEqual(evaluator.compare_rows([[a]], [[b]], ordered=ordered), (a, b) in equal)
        self.assertTrue(evaluator.compare_rows([[None], [b"a"], [1], ["1"]],
                                               [["1"], [1.0], [None], [b"a"]], ordered=False))

    def test_outer_order_detection_on_executable_sql(self):
        queries = {
            "SELECT 1 AS n ORDER BY n": True,
            "SELECT 1 AS n OrDeR /* ( ORDER BY */\n BY n": True,
            "SELECT 1 AS n ORDER -- comment\n BY n": True,
            "SELECT 1 AS n UNION ALL SELECT 2 ORDER BY n DESC LIMIT 1": True,
            "WITH t AS (SELECT 1 AS n ORDER BY n) SELECT n FROM t ORDER BY n": True,
            "WITH t AS (SELECT 1 AS n ORDER BY n) SELECT n FROM t": False,
            "SELECT n FROM (SELECT 1 AS n ORDER BY n)": False,
            "SELECT row_number() OVER (ORDER BY 1)": False,
            "SELECT row_number() OVER w WINDOW w AS (ORDER BY 1)": False,
            "SELECT 'ORDER BY (', 'it''s ORDER BY'": False,
            'SELECT 1 AS "ORDER BY", 2 AS "a""ORDER BY"': False,
            "SELECT 1 AS [ORDER BY], 2 AS `ORDER BY`": False,
            "SELECT 1 /* ORDER BY ) */ -- ORDER BY (": False,
            "SELECT 'x' ORDER BY 1 /* trailing ( */": True,
            "SELECT 1 AS order_by": False,
        }
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            for sql, expected in queries.items():
                with self.subTest(sql=sql):
                    conn.execute(sql).fetchall()  # Validate fixture syntax, not only token output.
                    self.assertEqual(evaluator.has_outer_order_by(sql), expected)

    def test_sqlite_execution_preserves_values_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.sqlite"
            sqlite3.connect(db).close()
            sql = "SELECT ' a ' AS text, x'61', NULL, 1.000000001 UNION ALL SELECT 'b', x'62', 2, 1.000000002 ORDER BY text DESC"
            rows, error = evaluator.execute_sqlite(db, sql)
            self.assertIsNone(error)
            self.assertEqual(rows, [["b", b"b", 2, 1.000000002], [" a ", b"a", None, 1.000000001]])


class EvaluationReportTests(unittest.TestCase):
    def test_evaluation_and_report_versioning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "fixture.sqlite"
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.executescript("CREATE TABLE t(n); INSERT INTO t VALUES (1), (2), (2);")
            run = root / "new"
            run.mkdir()
            metadata = {"benchmark": "kaggledbqa", "track": "raw", "provider": "fixture", "prompt_set_sha256": "fixture"}
            (run / "run_metadata.json").write_text(json.dumps(metadata))
            queries = [
                ("ordered", "SELECT n FROM t ORDER BY n", "SELECT n FROM t ORDER BY n DESC", False),
                ("unordered", "SELECT n FROM t", "SELECT n FROM t ORDER BY n DESC", True),
                ("duplicates", "SELECT n FROM t", "SELECT DISTINCT n FROM t", False),
                ("whitespace", "SELECT ' a '", "SELECT 'a'", False),
                ("blob", "SELECT x'61'", "SELECT '61'", False),
                ("numeric", "SELECT 1", "SELECT 1.0", True),
                ("precision", "SELECT 1.000000001", "SELECT 1.000000002", False),
                ("null", "SELECT NULL", "SELECT NULL", True),
                ("error", "SELECT 1", "SELECT missing", False),
                ("empty", "SELECT n FROM t WHERE 0", "SELECT n FROM t WHERE 0", True),
                ("gold_error", "SELECT missing", "SELECT 1", False),
            ]
            cases = []
            for case_id, gold, pred, expected in queries:
                case_dir = run / case_id
                case_dir.mkdir()
                (case_dir / "predicted.sql").write_text(pred)
                cases.append({"id": case_id, "mode": "sql", "dialect": "sqlite", "db_path": str(db), "gold_sql": gold})
            with patch.object(evaluator, "load_run_cases", return_value=cases), patch.object(sys, "argv", ["eval", "--run-dir", str(run)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(evaluator.main(), 0)
            summary = json.loads((run / "summary_execution.json").read_text())
            self.assertEqual(summary["result_comparison_version"], "sqlite-result-v2")
            self.assertEqual(summary["metric_scope"], "harness_local_sqlite_not_benchmark_native")
            self.assertEqual(summary["total_evaluated"], 10)
            self.assertEqual(summary["exact_matches"], 4)
            self.assertEqual(summary["prediction_error_count"], 1)
            self.assertEqual([r["exact_match"] for r in summary["details"]], [q[3] for q in queries])
            self.assertEqual(summary["details"][0]["result_comparison"], "ordered")
            self.assertEqual(summary["details"][1]["result_comparison"], "unordered_multiset")
            self.assertIsNone(summary["details"][-1]["result_comparison"])
            legacy = root / "old"
            legacy.mkdir()
            (legacy / "run_metadata.json").write_text(json.dumps(metadata))
            (legacy / "summary_execution.json").write_text('{"exact_matches": 1}')
            out = root / "report"
            with patch.object(sys, "argv", ["report", "--runs", str(run), str(legacy), "--output-stem", str(out)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reporter.main(), 0)
            report = out.with_suffix(".md").read_text()
            self.assertIn("## kaggledbqa - raw - sqlite-result-v2", report)
            self.assertIn("## kaggledbqa - raw - legacy-unversioned", report)
            rows = json.loads(out.with_suffix(".json").read_text())["rows"]
            self.assertEqual({r["result_comparison_version"] for r in rows}, {"sqlite-result-v2", "legacy-unversioned"})


if __name__ == "__main__":
    unittest.main()

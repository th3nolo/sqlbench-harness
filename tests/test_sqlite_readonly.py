"""SQLite permission regressions; run with unittest discovery."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_sql_benchmark as evaluator


class ReadOnlySQLiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "sample # & ü.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("CREATE TABLE items (id INTEGER, value TEXT);"
                               "INSERT INTO items VALUES (1, 'one'), (2, 'two');"
                               "CREATE VIEW item_view AS SELECT * FROM items;")
        conn.close()
        self.original = self.db.read_bytes()

    def assert_unchanged(self):
        self.assertEqual(self.db.read_bytes(), self.original)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT * FROM items").fetchall(), [(1, 'one'), (2, 'two')])
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall(),
                             [('item_view',), ('items',)])
        conn.close()

    def test_queries(self):
        queries = [
            ("SELECT * FROM item_view", [[1, 'one'], [2, 'two']]),
            ("WITH x AS (SELECT id FROM items) SELECT SUM(id) FROM x", [[3]]),
            ("WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<5) SELECT SUM(n) FROM x", [[15]]),
            ("SELECT a.id, COUNT(*) FROM items a JOIN items b ON a.id=b.id GROUP BY a.id", [[1, 1], [2, 1]]),
            ("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM items", [[1, 1], [2, 2]]),
        ]
        for sql, expected in queries:
            with self.subTest(sql=sql):
                self.assertEqual(evaluator.execute_sqlite(self.db, sql), (expected, None))
        self.assert_unchanged()

    def test_mutations_denied_without_keyword_filter(self):
        target = self.root / 'attached.sqlite'
        queries = [
            'CREATE TABLE new_table (x)', 'CREATE TEMP TABLE temp_table (x)',
            'CREATE INDEX new_index ON items(id)', 'CREATE VIEW new_view AS SELECT * FROM items',
            'CREATE TRIGGER new_trigger AFTER INSERT ON items BEGIN DELETE FROM items; END',
            'INSERT INTO items VALUES (3, "three")', 'UPDATE items SET id=99',
            'DELETE FROM items', 'DROP TABLE items', 'ALTER TABLE items ADD COLUMN extra',
            'REPLACE INTO items VALUES (3, "three")',
            'WITH x AS (SELECT 1) DELETE FROM items',
            f"ATTACH DATABASE '{target.as_posix()}' AS other", 'DETACH DATABASE main',
            f"VACUUM INTO '{target.as_posix()}'", 'VACUUM', 'ANALYZE', 'REINDEX',
            'PRAGMA query_only=OFF', 'PRAGMA writable_schema=ON', 'PRAGMA user_version=7',
            'PRAGMA journal_mode=WAL', 'BEGIN', 'SELECT load_extension("absent")',
            'SELECT 1; CREATE TABLE new_table (x)',
        ]
        # Prove the SQLite boundary rather than the legacy raw-text filter.
        # The separate literal-handling change may remove that filter entirely.
        with patch.object(evaluator, 'DANGEROUS_SQL', create=True) as legacy_filter:
            legacy_filter.search.return_value = None
            for sql in queries:
                with self.subTest(sql=sql):
                    rows, error = evaluator.execute_sqlite(self.db, sql)
                    self.assertIsNone(rows)
                    self.assertTrue(error)
                    if sql != 'SELECT 1; CREATE TABLE new_table (x)':
                        self.assertEqual(error, 'dangerous SQL refused')
                    self.assert_unchanged()
                    self.assertFalse(target.exists())

    def test_missing_database_is_not_created(self):
        missing = self.root / 'missing.sqlite'
        rows, error = evaluator.execute_sqlite(missing, 'SELECT 1')
        self.assertIsNone(rows)
        self.assertTrue(error)
        self.assertFalse(missing.exists())

    def test_instruction_limit_and_connection_cleanup(self):
        connections = []
        connect = sqlite3.connect

        def track(*args, **kwargs):
            conn = connect(*args, **kwargs)
            connections.append(conn)
            return conn

        with patch.object(evaluator.sqlite3, 'connect', side_effect=track):
            self.assertEqual(evaluator.execute_sqlite(self.db, 'SELECT 1'), ([[1]], None))
            self.assertTrue(evaluator.execute_sqlite(self.db, 'SELECT invalid')[1])
            self.assertTrue(evaluator.execute_sqlite(self.db, 'CREATE TABLE denied (x)')[1])
            rows, error = evaluator.execute_sqlite(
                self.db,
                'WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT SUM(n) FROM x',
                instruction_limit=10,
            )
            self.assertIsNone(rows)
            self.assertIn('interrupted', error)
        self.assertEqual(len(connections), 4)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
        self.assert_unchanged()


if __name__ == '__main__':
    unittest.main()


# -*- coding: utf-8 -*-
"""迁移器：幂等、checksum、备份与失败回滚。"""

import glob
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import db as wiki_db
from kb.wiki.config import WikiConfig
from kb.wiki.db import (
    MigrationChecksumError,
    connect,
    db_status,
    discover_migrations,
    run_migrations,
)


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = WikiConfig(
            enabled=True, vault_path=os.path.join(self._tmp.name, "vault")
        )

    def _new_conn(self, name="wiki.sqlite3"):
        path = os.path.join(self._tmp.name, name)
        conn = connect(path, self.config)
        self.addCleanup(conn.close)
        return conn, path

    def _backups(self, db_path):
        return glob.glob(db_path + ".bak-*")

    def test_empty_dir_has_no_pending(self):
        empty_dir = os.path.join(self._tmp.name, "empty_migrations")
        os.makedirs(empty_dir)
        conn, path = self._new_conn("empty.sqlite3")
        self.assertEqual(run_migrations(conn, migrations_dir=empty_dir), [])
        self.assertEqual(db_status(conn), [])
        self.assertEqual(self._backups(path), [])

    def test_discover_migrations_sorted_with_checksums(self):
        migrations = discover_migrations()
        self.assertEqual(
            [migration.name for migration in migrations],
            [
                "0001_core.sql",
                "0002_entities_relations.sql",
                "0003_claims_evidence.sql",
                "0004_jobs_journal.sql",
            ],
        )
        self.assertEqual(
            [migration.version for migration in migrations],
            ["0001", "0002", "0003", "0004"],
        )
        for migration in migrations:
            with open(migration.path, "rb") as handle:
                expected = hashlib.sha256(handle.read()).hexdigest()
            self.assertEqual(migration.checksum, expected)
            self.assertTrue(migration.sql.strip())

    def test_multiple_statements_on_one_line(self):
        inline_dir = os.path.join(self._tmp.name, "inline_migrations")
        os.makedirs(inline_dir)
        with open(os.path.join(inline_dir, "0001_inline.sql"), "w", encoding="utf-8") as handle:
            handle.write(
                "CREATE TABLE inline_a (id TEXT PRIMARY KEY); "
                "CREATE TABLE inline_b (id TEXT PRIMARY KEY);\n"
            )
        conn, _ = self._new_conn("inline.sqlite3")
        self.assertEqual(
            run_migrations(conn, migrations_dir=inline_dir), ["0001"]
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue({"inline_a", "inline_b"} <= tables)

    def test_empty_database_migrates_and_backs_up(self):
        conn, path = self._new_conn()
        applied = run_migrations(conn)
        self.assertEqual(applied, ["0001", "0002", "0003", "0004"])
        self.assertEqual(db_status(conn), applied)
        self.assertEqual(len(self._backups(path)), 1)

        rows = conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        discovered = {migration.version: migration for migration in discover_migrations()}
        for row in rows:
            migration = discovered[row["version"]]
            self.assertEqual(row["name"], migration.name)
            self.assertEqual(row["checksum"], migration.checksum)
            self.assertRegex(row["applied_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(len(tables), 16)

    def test_repeat_migrations_are_idempotent(self):
        conn, path = self._new_conn()
        self.assertEqual(len(run_migrations(conn)), 4)
        backups_after_first = self._backups(path)
        self.assertEqual(len(backups_after_first), 1)

        self.assertEqual(run_migrations(conn), [])
        self.assertEqual(db_status(conn), ["0001", "0002", "0003", "0004"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 4
        )
        self.assertEqual(self._backups(path), backups_after_first)

    def test_checksum_mismatch_rejected(self):
        migrations_copy = os.path.join(self._tmp.name, "migrations_copy")
        shutil.copytree(wiki_db.MIGRATIONS_DIR, migrations_copy)
        conn, path = self._new_conn("tamper.sqlite3")
        self.assertEqual(
            len(run_migrations(conn, migrations_dir=migrations_copy)), 4
        )
        backups_after_first = self._backups(path)

        target = os.path.join(migrations_copy, "0001_core.sql")
        with open(target, "ab") as handle:
            handle.write(b"\n-- tampered\n")

        with self.assertRaises(MigrationChecksumError) as ctx:
            run_migrations(conn, migrations_dir=migrations_copy)
        self.assertEqual(ctx.exception.version, "0001")
        self.assertEqual(ctx.exception.name, "0001_core.sql")
        self.assertNotEqual(ctx.exception.expected, ctx.exception.actual)
        self.assertEqual(db_status(conn), ["0001", "0002", "0003", "0004"])
        # checksum 校验先于备份，不应产生新备份
        self.assertEqual(self._backups(path), backups_after_first)

    def test_pending_failure_does_not_write_record_or_partial_schema(self):
        bad_dir = os.path.join(self._tmp.name, "bad_migrations")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "0005_bad.sql"), "w", encoding="utf-8") as handle:
            handle.write(
                "CREATE TABLE should_rollback (id TEXT PRIMARY KEY);\n"
                "INSERT INTO missing_table (id) VALUES ('x');\n"
            )

        conn, path = self._new_conn("broken.sqlite3")
        with self.assertRaises(sqlite3.DatabaseError):
            run_migrations(conn, migrations_dir=bad_dir)

        self.assertEqual(db_status(conn), [])
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
            0,
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertNotIn("should_rollback", tables)
        self.assertEqual(len(self._backups(path)), 1)
        self.assertFalse(conn.in_transaction)

        # 连接仍可用：换回默认迁移目录可继续完成迁移。
        self.assertEqual(
            run_migrations(conn), ["0001", "0002", "0003", "0004"]
        )

    def test_memory_database_skips_backup(self):
        conn = connect(":memory:", self.config)
        self.addCleanup(conn.close)
        self.assertEqual(
            run_migrations(conn), ["0001", "0002", "0003", "0004"]
        )
        self.assertEqual(db_status(conn), ["0001", "0002", "0003", "0004"])
        self.assertEqual(
            glob.glob(os.path.join(self._tmp.name, "*.bak-*")), []
        )


if __name__ == "__main__":
    unittest.main()

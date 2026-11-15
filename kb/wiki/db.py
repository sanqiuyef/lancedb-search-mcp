# -*- coding: utf-8 -*-
"""SQLite 连接、事务与迁移器。

连接级 PRAGMA：``foreign_keys=ON``、``busy_timeout``；
库级 PRAGMA：``journal_mode=WAL``、``synchronous=NORMAL``。
迁移器按文件名顺序执行 ``migrations/*.sql``，记录 sha256 checksum，
每个迁移独立事务，应用前用 SQLite backup API 生成 ``<db>.bak-<utcstamp>``。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from . import constants as C
from .config import WikiConfig

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_VERSION_RE = re.compile(r"^(\d+)")


class WikiDBError(RuntimeError):
    """Wiki SQLite 层错误。"""


class MigrationError(WikiDBError):
    """迁移发现/执行失败。"""


class MigrationChecksumError(MigrationError):
    """已执行迁移文件的 checksum 与记录不一致。"""

    def __init__(self, version: str, name: str, expected: str, actual: str) -> None:
        self.version = version
        self.name = name
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"迁移 {version} ({name}) checksum 不一致："
            f"已记录 {expected}，当前文件 {actual}"
        )


@dataclass(frozen=True)
class MigrationFile:
    """一个待执行/已执行的迁移文件。"""

    version: str
    name: str
    path: str
    checksum: str
    sql: str


def _is_memory_path(path: str | None) -> bool:
    if not path:
        return True
    return path == ":memory:" or path.startswith("file::memory:") or "mode=memory" in path


def _utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def connect(
    db_path: str | os.PathLike[str] | None = None,
    config: WikiConfig | None = None,
) -> sqlite3.Connection:
    """打开 Wiki SQLite 连接并设置连接级/库级 PRAGMA。

    ``db_path`` 为文件路径时自动创建父目录；``:memory:`` 不落盘。
    ``config`` 仅用于读取 ``db_busy_timeout_ms``，缺省 5000ms。
    """
    path = ":memory:" if db_path is None else os.fspath(db_path)
    if not _is_memory_path(path):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    busy_timeout_ms = C.DEFAULT_DB_BUSY_TIMEOUT_MS
    if config is not None:
        busy_timeout_ms = int(getattr(config, "db_busy_timeout_ms", busy_timeout_ms))
    busy_timeout_ms = max(0, busy_timeout_ms)

    conn = sqlite3.connect(
        path,
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    # 内存库会返回 memory，不会报错。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务上下文：正常提交，任意异常回滚。"""
    if conn.in_transaction:
        raise WikiDBError("连接已处于事务中，不支持嵌套 transaction()")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {C.SCHEMA_MIGRATIONS_TABLE} (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_migrations(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    _ensure_schema_migrations(conn)
    rows = conn.execute(
        f"SELECT version, name, checksum, applied_at "
        f"FROM {C.SCHEMA_MIGRATIONS_TABLE} ORDER BY version"
    ).fetchall()
    return {str(row[0]): row for row in rows}


def discover_migrations(
    migrations_dir: str | os.PathLike[str] | None = None,
) -> list[MigrationFile]:
    """按文件名排序发现 ``*.sql``，计算每个文件的 sha256。"""
    directory = os.fspath(migrations_dir) if migrations_dir is not None else MIGRATIONS_DIR
    if not os.path.isdir(directory):
        raise MigrationError(f"迁移目录不存在: {directory}")

    migrations: list[MigrationFile] = []
    seen_versions: dict[str, str] = {}
    for filename in sorted(os.listdir(directory)):
        if not filename.lower().endswith(".sql"):
            continue
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            raw = handle.read()
        checksum = hashlib.sha256(raw).hexdigest()
        try:
            sql = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"迁移文件不是 UTF-8: {path}") from exc
        match = _VERSION_RE.match(filename)
        version = match.group(1) if match else os.path.splitext(filename)[0]
        if version in seen_versions:
            raise MigrationError(
                f"重复的迁移版本 {version}: {seen_versions[version]} 与 {filename}"
            )
        seen_versions[version] = filename
        migrations.append(
            MigrationFile(
                version=version,
                name=filename,
                path=path,
                checksum=checksum,
                sql=sql,
            )
        )
    return migrations


def _split_sql_statements(script: str) -> list[str]:
    """把 SQL 脚本拆成单条语句，避免 executescript 的隐式 COMMIT。"""
    statements: list[str] = []
    buffer: list[str] = []
    for char in script:
        buffer.append(char)
        if char == ";" and sqlite3.complete_statement("".join(buffer)):
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _database_file(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or "")
    return ""


def _backup_database(conn: sqlite3.Connection, db_file: str) -> str:
    backup_path = f"{db_file}.bak-{_utc_stamp()}"
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return backup_path


def _apply_migration(conn: sqlite3.Connection, migration: MigrationFile) -> None:
    statements = _split_sql_statements(migration.sql)
    with transaction(conn):
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            f"INSERT INTO {C.SCHEMA_MIGRATIONS_TABLE} "
            f"(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.checksum, _utc_now()),
        )


def run_migrations(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str] | None = None,
    migrations_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """执行尚未应用的迁移，返回本次新应用的版本列表。

    - 已执行版本的 checksum 与当前文件不一致时抛 ``MigrationChecksumError``；
    - 应用前备份到 ``<db>.bak-<utcstamp>``（内存库跳过）；
    - 每个迁移单独事务，失败不会写入 ``schema_migrations``。
    """
    if conn.in_transaction:
        raise WikiDBError("run_migrations 需要空闲连接（当前连接处于事务中）")

    _ensure_schema_migrations(conn)
    applied = _applied_migrations(conn)
    discovered = discover_migrations(migrations_dir)

    for migration in discovered:
        row = applied.get(migration.version)
        if row is None:
            continue
        recorded_checksum = str(row[2])
        if recorded_checksum != migration.checksum:
            raise MigrationChecksumError(
                migration.version,
                migration.name,
                recorded_checksum,
                migration.checksum,
            )

    pending = [
        migration
        for migration in discovered
        if migration.version not in applied
    ]
    if not pending:
        return []

    actual_db_file = _database_file(conn)
    if actual_db_file and db_path is not None:
        requested = os.fspath(db_path)
        if not _is_memory_path(requested):
            actual_db_file = os.path.abspath(requested)
    if actual_db_file and not _is_memory_path(actual_db_file):
        try:
            _backup_database(conn, actual_db_file)
        except (OSError, sqlite3.Error) as exc:
            raise MigrationError(f"迁移前备份失败: {actual_db_file}: {exc}") from exc

    applied_now: list[str] = []
    for migration in pending:
        _apply_migration(conn, migration)
        applied_now.append(migration.version)
    return applied_now


def db_status(conn: sqlite3.Connection) -> list[str]:
    """返回已应用迁移版本列表（按版本排序）；表不存在时返回空列表。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (C.SCHEMA_MIGRATIONS_TABLE,),
    ).fetchone()
    if row is None:
        return []
    return [
        str(record[0])
        for record in conn.execute(
            f"SELECT version FROM {C.SCHEMA_MIGRATIONS_TABLE} ORDER BY version"
        )
    ]


__all__ = [
    "MIGRATIONS_DIR",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationFile",
    "WikiDBError",
    "connect",
    "db_status",
    "discover_migrations",
    "run_migrations",
    "transaction",
]

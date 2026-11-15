# -*- coding: utf-8 -*-
"""``write_journal`` 扫描与崩溃恢复建议。

恢复原则：
- ``scan_journal`` 与 ``recover(apply=False)`` 完全只读，不修改 DB/文件；
- 恢复过程**永不删除**文件、journal 行、page_revisions 行或任何业务数据；
- ``recover(apply=True)`` 只把“已确证收敛”的记录标为 ``completed``：文件存在、
  文件 sha256 与 ``journal.revision_id`` 对应 page_revision 的 ``content_sha256``
  一致，且 ``pages.current_revision_id`` 指向该修订；
- 其余状态只给出下一步动作（补登记、重建索引、人工核对），不自动写库。

动作取值：
- ``complete``：可安全标 completed；
- ``mark_failed``：prepared 且目标文件不存在，建议人工确认后置 failed（脚本不自动写）；
- ``needs_registration``：文件已写但 DB 缺 page_revision，需要人工/任务补登记；
- ``update_index``：DB 已提交，需要更新页面索引；
- ``rebuild_index``：索引待建，需要重建页面索引；
- ``report_only``：failed 记录，只报告错误；
- ``manual_review``：状态不一致，需要人工核对。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import nullcontext
from typing import Any

from .config import WikiConfig, WikiPathError, ensure_within
from .db import transaction
from .models import WriteJournalRecord
from .repositories import jobs as jobs_repo
from .repositories import pages as pages_repo

OPEN_JOURNAL_STATES = (
    "prepared",
    "file_written",
    "db_committed",
    "index_pending",
    "failed",
)

ACTION_COMPLETE = "complete"
ACTION_MARK_FAILED = "mark_failed"
ACTION_NEEDS_REGISTRATION = "needs_registration"
ACTION_UPDATE_INDEX = "update_index"
ACTION_REBUILD_INDEX = "rebuild_index"
ACTION_REPORT_ONLY = "report_only"
ACTION_MANUAL_REVIEW = "manual_review"


class RecoveryError(RuntimeError):
    """恢复扫描/执行失败。"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _journal_from_row(row: sqlite3.Row) -> WriteJournalRecord:
    return WriteJournalRecord.model_validate(dict(row))


def scan_journal(
    conn: sqlite3.Connection,
) -> dict[str, list[WriteJournalRecord]]:
    """按状态分类返回所有未完成（非 completed）的 journal 记录。

    返回值始终包含五个键（prepared/file_written/db_committed/index_pending/
    failed），没有记录时为对应空列表；记录按 created_at、journal_id 升序。
    """
    if not _table_exists(conn, "write_journal"):
        raise RecoveryError("write_journal 表不存在；请先执行 Wiki 迁移")
    rows = conn.execute(
        "SELECT * FROM write_journal WHERE state <> 'completed' "
        "ORDER BY created_at ASC, journal_id ASC"
    ).fetchall()
    result: dict[str, list[WriteJournalRecord]] = {
        state: [] for state in OPEN_JOURNAL_STATES
    }
    for row in rows:
        record = _journal_from_row(row)
        result[str(record.state)].append(record)
    return result


def _action(
    journal: WriteJournalRecord, action: str, detail: str
) -> dict[str, Any]:
    return {
        "journal_id": journal.journal_id,
        "state": str(journal.state),
        "action": action,
        "detail": detail,
    }


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _target_path(
    config: WikiConfig, journal: WriteJournalRecord
) -> tuple[str | None, str]:
    """把 journal.target_path 解析为 vault 内的绝对路径。"""
    raw = (journal.target_path or "").strip()
    if not raw:
        return None, "journal 缺少 target_path"
    normalized = raw.replace("\\", "/")
    if os.path.isabs(normalized):
        candidate = normalized
    else:
        parts = normalized.split("/")
        if any(part == ".." for part in parts):
            return None, f"target_path 包含 '..'，已拒绝: {raw}"
        candidate = os.path.join(config.vault_path, *parts)
    try:
        absolute = ensure_within(config.vault_path, candidate)
        absolute = ensure_within(config.page_root, absolute)
    except WikiPathError as exc:
        return None, f"target_path 不在页面根目录内: {exc}"
    return absolute, ""


def _convergence_problem(
    conn: sqlite3.Connection, path: str, journal: WriteJournalRecord
) -> tuple[str, str] | None:
    """返回 (问题类型, 说明)；None 表示文件与 DB 已收敛。"""
    if not journal.revision_id:
        return "missing_db", "journal 缺少 revision_id，无法核对 DB 记录"
    revision = pages_repo.get_page_revision(conn, journal.revision_id)
    if revision is None:
        return "missing_db", (
            f"page_revisions 中不存在 revision_id={journal.revision_id}，"
            "需要先登记页面修订"
        )

    page_id = journal.page_id or revision.page_id
    page = pages_repo.get_page(conn, page_id)
    if page is None:
        return "manual_review", (
            f"pages 中不存在 page_id={page_id}，文件与 DB 记录不一致"
        )
    if revision.page_id != page.page_id:
        return "manual_review", (
            f"revision {revision.revision_id} 属于页面 {revision.page_id}，"
            f"而非 journal 的 {page.page_id}"
        )
    if page.current_revision_id != revision.revision_id:
        return "manual_review", (
            f"页面当前修订为 {page.current_revision_id!r}，"
            f"journal 修订 {revision.revision_id} 已不是当前修订"
        )

    try:
        disk_sha256 = _sha256_file(path)
    except OSError as exc:
        return "manual_review", f"无法读取目标文件: {exc}"
    if disk_sha256 != revision.content_sha256:
        return "manual_review", (
            "文件 sha256 与 page_revision 不一致："
            f"disk={disk_sha256}, db={revision.content_sha256}"
        )
    return None


def _recover_prepared(
    conn: sqlite3.Connection, config: WikiConfig, journal: WriteJournalRecord
) -> dict[str, Any]:
    path, error = _target_path(config, journal)
    if path is None:
        return _action(journal, ACTION_MANUAL_REVIEW, error)
    if not os.path.isfile(path):
        return _action(
            journal,
            ACTION_MARK_FAILED,
            f"目标文件不存在，建议人工确认后置 failed: {journal.target_path}",
        )

    problem = _convergence_problem(conn, path, journal)
    if problem is None:
        return _action(
            journal,
            ACTION_COMPLETE,
            "文件存在且与 DB 当前修订一致，可安全标 completed",
        )
    kind, message = problem
    if kind == "missing_db":
        return _action(journal, ACTION_NEEDS_REGISTRATION, message)
    return _action(journal, ACTION_MANUAL_REVIEW, message)


def _recover_file_written(
    conn: sqlite3.Connection, config: WikiConfig, journal: WriteJournalRecord
) -> dict[str, Any]:
    path, error = _target_path(config, journal)
    if path is None:
        return _action(journal, ACTION_MANUAL_REVIEW, error)
    if not os.path.isfile(path):
        return _action(
            journal,
            ACTION_MANUAL_REVIEW,
            f"文件已写记录存在但目标文件缺失，需人工核对: {journal.target_path}",
        )

    problem = _convergence_problem(conn, path, journal)
    if problem is None:
        return _action(
            journal,
            ACTION_COMPLETE,
            "文件 sha256 与 page_revision 一致，可安全标 completed",
        )
    kind, message = problem
    if kind == "missing_db":
        return _action(journal, ACTION_NEEDS_REGISTRATION, message)
    return _action(journal, ACTION_MANUAL_REVIEW, message)


def _recover_db_committed(journal: WriteJournalRecord) -> dict[str, Any]:
    detail = (
        f"DB 已提交（page_id={journal.page_id or '-'}，"
        f"revision_id={journal.revision_id or '-'}），需要更新页面索引"
    )
    return _action(journal, ACTION_UPDATE_INDEX, detail)


def _recover_index_pending(journal: WriteJournalRecord) -> dict[str, Any]:
    detail = (
        f"页面索引待建（page_id={journal.page_id or '-'}），"
        "需要重建页面索引后再标 completed"
    )
    return _action(journal, ACTION_REBUILD_INDEX, detail)


def _recover_failed(journal: WriteJournalRecord) -> dict[str, Any]:
    message = journal.error_message.strip() or "无错误信息"
    return _action(
        journal,
        ACTION_REPORT_ONLY,
        f"写入曾失败，仅报告不自动处理: {message}",
    )


def recover(
    conn: sqlite3.Connection,
    config: WikiConfig,
    apply: bool = False,
) -> list[dict[str, Any]]:
    """扫描未完成 journal 并返回恢复动作列表。

    ``apply=False``（默认）完全只读；``apply=True`` 仅在事务中把
    ``action == "complete"`` 的记录状态更新为 ``completed``，其他记录保持不变。
    返回值每项固定为 ``{journal_id, state, action, detail}``，``state`` 是扫描时
    的原状态。
    """
    if not isinstance(config, WikiConfig):
        raise TypeError("config 必须是 WikiConfig")

    actions: list[dict[str, Any]] = []
    scanned = scan_journal(conn)
    for state in OPEN_JOURNAL_STATES:
        for journal in scanned[state]:
            if state == "prepared":
                action = _recover_prepared(conn, config, journal)
            elif state == "file_written":
                action = _recover_file_written(conn, config, journal)
            elif state == "db_committed":
                action = _recover_db_committed(journal)
            elif state == "index_pending":
                action = _recover_index_pending(journal)
            else:
                action = _recover_failed(journal)
            actions.append(action)

    completable = [item for item in actions if item["action"] == ACTION_COMPLETE]
    if apply and completable:
        context = nullcontext() if conn.in_transaction else transaction(conn)
        with context:
            for item in completable:
                jobs_repo.update_journal_state(
                    conn,
                    item["journal_id"],
                    "completed",
                    detail={
                        "recovered_by": "kb.wiki.recovery.recover",
                        "previous_state": item["state"],
                    },
                )
                item["detail"] = f"{item['detail']}（apply=True 已标 completed）"

    return actions


__all__ = [
    "ACTION_COMPLETE",
    "ACTION_MANUAL_REVIEW",
    "ACTION_MARK_FAILED",
    "ACTION_NEEDS_REGISTRATION",
    "ACTION_REBUILD_INDEX",
    "ACTION_REPORT_ONLY",
    "ACTION_UPDATE_INDEX",
    "OPEN_JOURNAL_STATES",
    "RecoveryError",
    "recover",
    "scan_journal",
]

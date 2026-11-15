# -*- coding: utf-8 -*-
"""Wiki 提案服务：draft/diff 落盘、审核队列与 apply/reject。

提案文件写在 ``<wiki_db_dir>/proposals/<proposal_id>/draft.md`` 与 ``diff.md``，
绝不写入 vault 页面目录；只有 ``apply_proposal(approve=True)`` 通过 PageStore
更新页面。

状态机：pending → rejected / stale / cancelled / approved → applied；
apply 失败时 approved 会恢复为 pending（error_message 记录原因，可重试），
不会永久停在 approved；``cancel_proposal`` 只允许取消 pending 提案。
状态迁移全部用 CAS（WHERE status=?）并检查 rowcount，终态不可被普通更新覆盖。

apply 是人工审核通过，允许把页面发布为 published；该语义与
``WIKI_AUTO_PUBLISH``（自动发布开关）无关。
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from . import constants as C
from .config import WikiConfig, ensure_within
from .db import transaction
from .ids import new_ulid
from .models import PageRecord, PageRevisionRecord, ProposalRecord
from .page_store import (
    GitCommitError,
    PageFileMissingError,
    PageNotFoundError,
    PageStore,
    PageValidationError,
    RevisionConflictError,
    atomic_write_text,
)
from .repositories import pages as pages_repo
from .repositories import proposals as proposals_repo

PROPOSAL_STATES = C.PROPOSAL_STATUSES
PERSISTED_PROPOSAL_STATES = C.PROPOSAL_STATUSES


class ProposalError(RuntimeError):
    """提案服务基类错误。"""


class ProposalNotFoundError(ProposalError, LookupError):
    """提案不存在。"""


@dataclass(frozen=True, slots=True)
class ProposalDetail:
    record: ProposalRecord
    payload: dict[str, Any]
    draft: str
    diff: str

    def __getattr__(self, name: str) -> Any:
        """未定义属性代理到 ProposalRecord（如 proposal_id/status/page_id）。"""
        return getattr(self.record, name)


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    ok: bool
    status: str
    message: str
    proposal: ProposalRecord
    page: PageRecord | None = None
    revision: PageRevisionRecord | None = None


def _payload(record: ProposalRecord) -> dict[str, Any]:
    try:
        data = json.loads(record.payload_json) if record.payload_json else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ProposalService:
    """提案创建、查询与审核入口。"""

    def __init__(
        self,
        config: WikiConfig,
        conn: sqlite3.Connection,
        page_store: PageStore | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.page_store = page_store or PageStore(config, conn)
        self.proposals_root = os.path.join(
            os.path.dirname(config.db_path), "proposals"
        )

    # ── 路径 ──
    def _proposal_dir(self, proposal_id: str) -> str:
        return ensure_within(
            self.proposals_root, os.path.join(self.proposals_root, proposal_id)
        )

    def _artifact_paths(self, proposal_id: str) -> tuple[str, str]:
        directory = self._proposal_dir(proposal_id)
        return (
            os.path.join(directory, "draft.md"),
            os.path.join(directory, "diff.md"),
        )

    # ── 创建/读取 ──
    def create_proposal(
        self,
        proposal_type: str,
        target_page_id: str,
        proposed_content: str,
        rationale: str,
        expected_revision_id: str,
        evidence_refs: list[Any] | tuple[Any, ...] | None,
        created_by: str,
    ) -> ProposalRecord:
        if not isinstance(proposal_type, str) or not proposal_type.strip():
            raise ValueError("proposal_type 必须是非空字符串")
        if not isinstance(target_page_id, str) or not target_page_id.strip():
            raise ValueError("target_page_id 必须是非空字符串")
        if not isinstance(proposed_content, str):
            raise ValueError("proposed_content 必须是字符串")
        if not isinstance(rationale, str):
            raise ValueError("rationale 必须是字符串")
        if not isinstance(expected_revision_id, str):
            raise ValueError("expected_revision_id 必须是字符串")
        if not isinstance(created_by, str) or not created_by.strip():
            raise ValueError("created_by 必须是非空字符串")

        page_view = self.page_store.read_page(target_page_id)
        if page_view is None:
            raise ProposalError(f"目标页面不存在: {target_page_id}")

        evidence = list(evidence_refs or [])
        try:
            json.dumps(evidence, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence_refs 必须可 JSON 序列化") from exc

        payload = {
            "proposed_content": proposed_content,
            "evidence_refs": evidence,
        }
        fromfile = f"a/{page_view.page.relative_path}"
        tofile = f"b/{page_view.page.relative_path}"
        diff_text = "".join(
            difflib.unified_diff(
                page_view.content.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        if diff_text and not diff_text.endswith("\n"):
            diff_text += "\n"

        proposal_id = new_ulid()
        draft_path, diff_path = self._artifact_paths(proposal_id)
        directory = self._proposal_dir(proposal_id)
        if os.path.exists(directory):
            raise ProposalError(f"提案目录已存在: {directory}")

        now = proposals_repo.utc_now()
        record = ProposalRecord(
            proposal_id=proposal_id,
            proposal_type=proposal_type.strip(),
            status="pending",
            page_id=target_page_id,
            expected_revision_id=expected_revision_id,
            result_revision_id="",
            payload_json=_dump_payload(payload),
            rationale=rationale,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            reviewed_by="",
            reviewed_at=None,
            applied_at=None,
            error_message="",
        )
        try:
            os.makedirs(directory, exist_ok=False)
            atomic_write_text(draft_path, proposed_content)
            atomic_write_text(diff_path, diff_text)
            # 调用方（如 submit_extraction）已在事务中时复用，保证提案记录与
            # 同批业务写入原子提交。
            context = (
                nullcontext() if self.conn.in_transaction else transaction(self.conn)
            )
            with context:
                proposals_repo.insert_proposal(self.conn, record)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def get_proposal(self, proposal_id: str) -> ProposalDetail | None:
        record = proposals_repo.get_proposal(self.conn, proposal_id)
        if record is None:
            return None
        draft_path, diff_path = self._artifact_paths(proposal_id)
        draft = ""
        diff = ""
        try:
            with open(draft_path, "r", encoding="utf-8") as handle:
                draft = handle.read()
        except OSError:
            draft = ""
        try:
            with open(diff_path, "r", encoding="utf-8") as handle:
                diff = handle.read()
        except OSError:
            diff = ""
        return ProposalDetail(
            record=record,
            payload=_payload(record),
            draft=draft,
            diff=diff,
        )

    def review_queue(self, status: str = "pending") -> list[ProposalRecord]:
        if status not in PERSISTED_PROPOSAL_STATES:
            raise ValueError(
                f"未知 proposal status: {status!r}；"
                f"可持久化状态: {', '.join(PERSISTED_PROPOSAL_STATES)}"
            )
        return proposals_repo.list_proposals(self.conn, status=status)

    # ── 审核 ──
    def _current_decision(
        self, proposal_id: str, message: str
    ) -> ProposalDecision:
        """CAS 失败后读取最新记录并返回当前状态（并发下只有一方成功）。"""
        current = proposals_repo.get_proposal(self.conn, proposal_id)
        if current is None:  # pragma: no cover - 行不会在审核期间被删
            raise ProposalNotFoundError(f"提案不存在: {proposal_id}")
        return ProposalDecision(
            ok=False,
            status=str(current.status),
            message=f"{message}（当前状态: {current.status}）",
            proposal=current,
        )

    def _restore_pending(
        self,
        record: ProposalRecord,
        error: BaseException | str,
        *,
        expected_revision_id: str | None = None,
    ) -> ProposalDecision:
        """apply 失败后把 approved 提案恢复为 pending（可重试、审核队列可见）。

        状态机没有 failed；pending + error_message 是唯一可重试的持久状态，
        终态 applied/rejected/stale/cancelled 不会从这里写入。
        """
        message = f"应用失败，提案已恢复 pending 可重试: {error}"
        fields: dict[str, Any] = {
            "status": "pending",
            "error_message": message,
        }
        if expected_revision_id:
            fields["expected_revision_id"] = expected_revision_id
        try:
            with transaction(self.conn):
                changed = proposals_repo.update_proposal(
                    self.conn,
                    record.proposal_id,
                    expected_status="approved",
                    **fields,
                )
        except BaseException:
            changed = 0
        current = proposals_repo.get_proposal(self.conn, record.proposal_id)
        if current is None:  # pragma: no cover - 行不会在审核期间被删
            raise ProposalNotFoundError(
                f"提案不存在: {record.proposal_id}"
            )
        if changed != 1 and str(current.status) == "applied":
            # 恢复 CAS 失败但更新实际已提交（如 COMMIT 后异常）。
            return ProposalDecision(
                ok=True,
                status="applied",
                message="提案已应用并提交",
                proposal=current,
            )
        if changed != 1 and str(current.status) == "approved":
            message = (
                "应用失败且无法恢复 pending（数据库不可写），需要人工处理: "
                f"{error}"
            )
        return ProposalDecision(
            ok=False,
            status=str(current.status),
            message=message,
            proposal=current,
        )

    def cancel_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "agent",
        reason: str = "",
    ) -> ProposalDecision:
        """取消 pending 提案；不修改页面，也不删除 draft/diff。"""
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 必须是非空字符串")
        if not isinstance(reason, str):
            raise ValueError("reason 必须是字符串")
        record = proposals_repo.get_proposal(self.conn, proposal_id)
        if record is None:
            raise ProposalNotFoundError(f"提案不存在: {proposal_id}")
        if record.status != "pending":
            return ProposalDecision(
                ok=False,
                status=str(record.status),
                message=f"提案状态为 {record.status}，只有 pending 可取消",
                proposal=record,
            )

        payload = _payload(record)
        if reason:
            payload["cancel_reason"] = reason
        reviewed_at = proposals_repo.utc_now()
        with transaction(self.conn):
            changed = proposals_repo.update_proposal(
                self.conn,
                proposal_id,
                expected_status="pending",
                status="cancelled",
                reviewed_by=actor,
                reviewed_at=reviewed_at,
                payload_json=_dump_payload(payload),
                error_message="",
            )
        if changed != 1:
            return self._current_decision(
                proposal_id, "取消失败：提案状态已被并发修改"
            )
        updated = proposals_repo.get_proposal(self.conn, proposal_id)
        assert updated is not None
        return ProposalDecision(
            ok=True,
            status="cancelled",
            message="提案已取消，页面未修改",
            proposal=updated,
        )

    def apply_proposal(
        self,
        proposal_id: str,
        approve: bool,
        review_note: str = "",
        actor: str = "agent",
    ) -> ProposalDecision:
        record = proposals_repo.get_proposal(self.conn, proposal_id)
        if record is None:
            raise ProposalNotFoundError(f"提案不存在: {proposal_id}")
        if not isinstance(review_note, str):
            raise ValueError("review_note 必须是字符串")

        if record.status != "pending":
            return ProposalDecision(
                ok=False,
                status=str(record.status),
                message=f"提案状态为 {record.status}，只有 pending 可审核",
                proposal=record,
            )

        payload = _payload(record)
        if review_note:
            payload["review_note"] = review_note
        payload_json = _dump_payload(payload)
        reviewed_at = proposals_repo.utc_now()

        if not approve:
            with transaction(self.conn):
                changed = proposals_repo.update_proposal(
                    self.conn,
                    proposal_id,
                    expected_status="pending",
                    status="rejected",
                    reviewed_by=actor,
                    reviewed_at=reviewed_at,
                    payload_json=payload_json,
                )
            if changed != 1:
                return self._current_decision(
                    proposal_id, "拒绝失败：提案状态已被并发修改"
                )
            updated = proposals_repo.get_proposal(self.conn, proposal_id)
            assert updated is not None
            return ProposalDecision(
                ok=True,
                status="rejected",
                message="提案已拒绝，页面未修改",
                proposal=updated,
            )

        proposed_content = payload.get("proposed_content")
        if not isinstance(proposed_content, str):
            return self._mark_stale(
                record,
                "提案 payload 缺少 proposed_content",
                actor,
                reviewed_at,
                payload_json,
                expected_status="pending",
            )

        try:
            page_view = (
                self.page_store.read_page(record.page_id)
                if record.page_id
                else None
            )
        except (PageNotFoundError, PageFileMissingError, PageValidationError) as exc:
            return self._mark_stale(
                record,
                f"目标页面不可用: {exc}",
                actor,
                reviewed_at,
                payload_json,
                expected_status="pending",
            )
        if page_view is None:
            return self._mark_stale(
                record,
                "目标页面不存在",
                actor,
                reviewed_at,
                payload_json,
                expected_status="pending",
            )

        current_revision_id = page_view.page.current_revision_id or ""
        if current_revision_id != (record.expected_revision_id or ""):
            return self._mark_stale(
                record,
                "expected_revision_id 已过期",
                actor,
                reviewed_at,
                payload_json,
                expected_status="pending",
            )

        # 人工审核通过：CAS pending → approved。并发取消/审核只有一方成功。
        with transaction(self.conn):
            approved = proposals_repo.update_proposal(
                self.conn,
                proposal_id,
                expected_status="pending",
                status="approved",
                reviewed_by=actor,
                reviewed_at=reviewed_at,
                payload_json=payload_json,
            )
        if approved != 1:
            return self._current_decision(
                proposal_id, "审核失败：提案状态已被并发修改"
            )

        try:
            result = self.page_store.update_page(
                record.page_id,
                proposed_content,
                record.expected_revision_id,
                actor=actor,
                commit=True,
                source_proposal_id=proposal_id,
                change_reason=record.rationale,
                # apply 是人工批准，允许发布；与 WIKI_AUTO_PUBLISH 无关。
                target_status="published",
            )
        except GitCommitError as exc:
            # 文件与 DB 已提交、仅 git 失败：把 expected 前移到当前修订，
            # 恢复 pending 后可再次 apply。
            current_page = pages_repo.get_page(self.conn, record.page_id)
            return self._restore_pending(
                record,
                exc,
                expected_revision_id=(
                    current_page.current_revision_id
                    if current_page is not None
                    else None
                ),
            )
        except (
            RevisionConflictError,
            PageFileMissingError,
            PageNotFoundError,
        ) as exc:
            return self._restore_pending(record, exc)
        except PageValidationError as exc:
            self._restore_pending(record, exc)
            raise
        except BaseException as exc:
            self._restore_pending(record, exc)
            raise

        if result.status != "updated":
            return self._mark_stale(
                record,
                result.message or "页面在审核期间发生外部修改",
                actor,
                reviewed_at,
                payload_json,
                expected_status="approved",
            )

        applied_at = proposals_repo.utc_now()
        try:
            with transaction(self.conn):
                changed = proposals_repo.update_proposal(
                    self.conn,
                    proposal_id,
                    expected_status="approved",
                    status="applied",
                    result_revision_id=(
                        result.revision.revision_id if result.revision else ""
                    ),
                    applied_at=applied_at,
                    error_message="",
                )
        except BaseException as exc:
            current = proposals_repo.get_proposal(self.conn, proposal_id)
            if current is not None and str(current.status) == "applied":
                return ProposalDecision(
                    ok=True,
                    status="applied",
                    message="提案已应用并提交",
                    proposal=current,
                    page=result.page,
                    revision=result.revision,
                )
            self._restore_pending(
                record,
                exc,
                expected_revision_id=(
                    result.revision.revision_id if result.revision else None
                ),
            )
            raise
        if changed != 1:
            current = proposals_repo.get_proposal(self.conn, proposal_id)
            if current is not None and str(current.status) == "applied":
                return ProposalDecision(
                    ok=True,
                    status="applied",
                    message="提案已应用并提交",
                    proposal=current,
                    page=result.page,
                    revision=result.revision,
                )
            return self._restore_pending(
                record,
                "提案状态在写入后发生变化",
                expected_revision_id=(
                    result.revision.revision_id if result.revision else None
                ),
            )
        updated = proposals_repo.get_proposal(self.conn, proposal_id)
        assert updated is not None
        return ProposalDecision(
            ok=True,
            status="applied",
            message="提案已应用并提交",
            proposal=updated,
            page=result.page,
            revision=result.revision,
        )

    def _mark_stale(
        self,
        record: ProposalRecord,
        message: str,
        actor: str,
        reviewed_at: str,
        payload_json: str,
        *,
        expected_status: str,
    ) -> ProposalDecision:
        with transaction(self.conn):
            changed = proposals_repo.update_proposal(
                self.conn,
                record.proposal_id,
                expected_status=expected_status,
                status="stale",
                reviewed_by=actor,
                reviewed_at=reviewed_at,
                error_message=message,
                payload_json=payload_json,
            )
        if changed != 1:
            return self._current_decision(
                record.proposal_id, "标记 stale 失败：提案状态已被并发修改"
            )
        updated = proposals_repo.get_proposal(self.conn, record.proposal_id)
        assert updated is not None
        return ProposalDecision(
            ok=False,
            status="stale",
            message=message,
            proposal=updated,
        )


__all__ = [
    "PERSISTED_PROPOSAL_STATES",
    "PROPOSAL_STATES",
    "ProposalDecision",
    "ProposalDetail",
    "ProposalError",
    "ProposalNotFoundError",
    "ProposalService",
]

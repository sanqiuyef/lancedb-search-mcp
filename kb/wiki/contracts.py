# -*- coding: utf-8 -*-
"""Wiki service 的 Pydantic 请求/响应模型。

``submit_extraction`` 采用两步摄入契约：Agent 先分析资料，再把结构化抽取
结果一次提交给服务端。服务端不内置 LLM、不调用外部 API，只做校验与落库。

契约要点：
- ``analysis`` 必填，记录本轮抽取依据的分析文本（便于审计）；
- ``claim_type`` 接受 factual/definition/result（落库为 fact）与
  hypothesis/interpretation（落库为 hypothesis）；
- ``chunk_texts`` 可选，按 chunk_id 提供原文，用于离线校验引文；
  未提供时服务端按 chunk_refs 回查 LanceDB；
- 所有模型 ``extra="forbid"``，避免 Agent 拼错字段后被静默忽略。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# claim_type 的对外取值 → SQLite claims.claim_type 落库值。
CLAIM_TYPE_DB_MAP = {
    "fact": "fact",
    "factual": "fact",
    "definition": "fact",
    "result": "fact",
    "hypothesis": "hypothesis",
    "interpretation": "hypothesis",
}
FACTUAL_CLAIM_TYPES = frozenset({"fact", "factual", "definition", "result"})


class ContractModel(BaseModel):
    """契约模型基类：禁止未知字段。"""

    model_config = ConfigDict(extra="forbid")


class EvidenceInput(ContractModel):
    """一条候选引文：chunk_id + 原文摘句。"""

    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    section_path: str = ""
    evidence_role: str = "supports"

    @field_validator("chunk_id", "quote")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空字符串")
        return cleaned

    @field_validator("evidence_role")
    @classmethod
    def _strip_role(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("evidence_role 不能为空")
        return cleaned


class ExtractionEntity(ContractModel):
    """候选实体；confidence 低于配置阈值时只返回候选、不落库。"""

    name: str = Field(min_length=1)
    entity_type: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name 不能为空")
        return cleaned

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"aliases[{index}] 必须是非空字符串")
            text = item.strip()
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class ExtractionClaim(ContractModel):
    """候选断言；事实型断言必须有可校验的引文。"""

    claim_text: str = Field(min_length=1)
    claim_type: str = "fact"
    evidence: list[EvidenceInput] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("claim_text")
    @classmethod
    def _strip_claim_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("claim_text 不能为空")
        return cleaned

    @field_validator("claim_type")
    @classmethod
    def _known_claim_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CLAIM_TYPE_DB_MAP:
            allowed = ", ".join(sorted(CLAIM_TYPE_DB_MAP))
            raise ValueError(f"未知 claim_type: {value!r}（允许: {allowed}）")
        return normalized

    @property
    def is_factual(self) -> bool:
        """factual/definition/result（含 fact）视为事实型，必须有证据。"""
        return self.claim_type in FACTUAL_CLAIM_TYPES

    @property
    def db_claim_type(self) -> str:
        return CLAIM_TYPE_DB_MAP[self.claim_type]


class ExtractionRelation(ContractModel):
    """候选关系；谓词白名单在 service 层校验，以便按条拒绝而不是整包失败。"""

    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("subject", "predicate", "object")
    @classmethod
    def _strip_relation_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("关系字段不能为空")
        return cleaned


class PageSuggestion(ContractModel):
    """页面建议；只用于生成 proposal，服务端不会直接创建/发布页面。"""

    action: Literal["create", "update"]
    page_id: str = ""
    title: str = ""
    slug: str = ""
    page_type: str = ""
    content_md: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def _validate_action_fields(self) -> "PageSuggestion":
        if self.action == "update" and not self.page_id.strip():
            raise ValueError("action=update 时必须提供 page_id")
        if self.content_md and not isinstance(self.content_md, str):
            raise ValueError("content_md 必须是字符串")
        if self.action == "create" and not (self.title.strip() or self.slug.strip()):
            raise ValueError("action=create 时至少提供 title 或 slug")
        return self


class ExtractionPayload(ContractModel):
    """``wiki_submit_extraction`` 的完整输入。"""

    analysis: str = Field(min_length=1)
    source_document_id: str = ""
    source_chunk_ids: list[str] = Field(default_factory=list)
    entities: list[ExtractionEntity] = Field(default_factory=list)
    claims: list[ExtractionClaim] = Field(default_factory=list)
    relations: list[ExtractionRelation] = Field(default_factory=list)
    page_suggestion: PageSuggestion | None = None
    chunk_texts: dict[str, str] = Field(default_factory=dict)

    @field_validator("analysis")
    @classmethod
    def _strip_analysis(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("analysis 不能为空（两步摄入要求先分析再提交）")
        return cleaned

    @field_validator("chunk_texts")
    @classmethod
    def _validate_chunk_texts(cls, value: dict[str, str]) -> dict[str, str]:
        for chunk_id, text in value.items():
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise ValueError("chunk_texts 的 key 必须是非空 chunk_id")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"chunk_texts[{chunk_id!r}] 必须是非空字符串")
        return value


# =============================================================
# 响应模型
# =============================================================


class WikiErrorResponse(BaseModel):
    """统一错误响应（MCP 层与 service 的业务失败共用）。"""

    ok: Literal[False] = False
    error: str
    code: str = ""


class ExtractionCreated(BaseModel):
    entities: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)


class ExtractionRejection(BaseModel):
    kind: Literal["entity", "claim", "relation", "page_suggestion"]
    index: int
    reason: str
    error: str = ""
    name: str = ""
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    item: dict[str, Any] | None = None


class ExtractionResult(BaseModel):
    ok: bool = True
    created: ExtractionCreated = Field(default_factory=ExtractionCreated)
    rejected: list[ExtractionRejection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None


class ActionResult(BaseModel):
    """通用写操作结果；具体字段由 service 追加。"""

    ok: bool = True
    status: str = ""
    message: str = ""
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ActionResult",
    "CLAIM_TYPE_DB_MAP",
    "ContractModel",
    "EvidenceInput",
    "ExtractionClaim",
    "ExtractionCreated",
    "ExtractionEntity",
    "ExtractionPayload",
    "ExtractionRejection",
    "ExtractionRelation",
    "ExtractionResult",
    "FACTUAL_CLAIM_TYPES",
    "PageSuggestion",
    "WikiErrorResponse",
]

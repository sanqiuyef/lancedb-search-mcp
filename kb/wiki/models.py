# -*- coding: utf-8 -*-
"""Wiki SQLite 表的 Pydantic v2 记录模型。

每个业务表对应一个 ``*Record``，字段与迁移 SQL 的列一一对应。
StrEnum 取值与 ``constants.py`` / SQL CHECK 保持一致。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import new_ulid


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    ARCHIVED = "archived"
    ERROR = "error"
    LEGACY_UNVERIFIED = "legacy_unverified"


class DocumentRevisionStatus(StrEnum):
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    LEGACY_UNVERIFIED = "legacy_unverified"


class PageType(StrEnum):
    CONCEPT = "concept"
    ENTITY = "entity"
    TOPIC = "topic"


class PageStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EntityStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class RelationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class AliasType(StrEnum):
    ALIAS = "alias"
    REDIRECT = "redirect"
    ABBREVIATION = "abbreviation"
    TRANSLATION = "translation"
    SYNONYM = "synonym"


class ClaimType(StrEnum):
    FACT = "fact"
    HYPOTHESIS = "hypothesis"


class ClaimStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISPUTED = "disputed"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    STALE = "stale"
    APPROVED = "approved"
    APPLIED = "applied"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JournalState(StrEnum):
    PREPARED = "prepared"
    FILE_WRITTEN = "file_written"
    DB_COMMITTED = "db_committed"
    INDEX_PENDING = "index_pending"
    COMPLETED = "completed"
    FAILED = "failed"


class WikiRecord(BaseModel):
    """记录模型基类：禁止未知字段，避免与 SQL schema 漂移。"""

    model_config = ConfigDict(extra="forbid")


class DocumentRecord(WikiRecord):
    document_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    canonical_uri: str = Field(min_length=1)
    display_name: str = ""
    category: str = ""
    status: DocumentStatus = DocumentStatus.ACTIVE
    current_revision_id: str | None = None
    created_at: str
    updated_at: str


class DocumentRevisionRecord(WikiRecord):
    revision_id: str = Field(default_factory=new_ulid, min_length=1)
    document_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    raw_sha256: str = Field(min_length=1)
    parsed_sha256: str = Field(min_length=1)
    processing_fingerprint: str = Field(min_length=1)
    parser_name: str = ""
    parser_version: str = ""
    parser_config_sha256: str = ""
    source_path: str = ""
    parsed_markdown_path: str = ""
    chunk_count: int = Field(default=0, ge=0)
    status: DocumentRevisionStatus = DocumentRevisionStatus.PROCESSING
    error_message: str = ""
    created_at: str


class PageRecord(WikiRecord):
    page_id: str = Field(default_factory=new_ulid, min_length=1)
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    page_type: PageType
    status: PageStatus = PageStatus.DRAFT
    current_revision_id: str | None = None
    relative_path: str = Field(min_length=1)
    created_by: str = ""
    created_at: str
    updated_at: str


class PageRevisionRecord(WikiRecord):
    revision_id: str = Field(default_factory=new_ulid, min_length=1)
    page_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    relative_path: str = Field(min_length=1)
    content_sha256: str = Field(min_length=1)
    git_commit: str = ""
    summary: str = ""
    change_reason: str = ""
    source_proposal_id: str | None = None
    created_by: str = ""
    created_at: str


class EntityRecord(WikiRecord):
    entity_id: str = Field(default_factory=new_ulid, min_length=1)
    canonical_name: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    entity_type: str = ""
    description: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: EntityStatus = EntityStatus.PROPOSED
    created_by: str = ""
    created_at: str
    updated_at: str


class EntityAliasRecord(WikiRecord):
    entity_id: str = Field(min_length=1)
    alias: str = Field(min_length=1)
    normalized_alias: str = Field(min_length=1)
    alias_type: AliasType = AliasType.ALIAS
    created_at: str


class PageEntityRecord(WikiRecord):
    page_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    role: str = "mention"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: str


class PageLinkRecord(WikiRecord):
    source_page_id: str = Field(min_length=1)
    target_slug: str = Field(min_length=1)
    target_page_id: str | None = None
    display_text: str = ""
    created_at: str


class RelationRecord(WikiRecord):
    relation_id: str = Field(default_factory=new_ulid, min_length=1)
    subject_entity_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_entity_id: str = Field(min_length=1)
    status: RelationStatus = RelationStatus.PROPOSED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_page_id: str | None = None
    evidence: str = ""
    created_by: str = ""
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _subject_not_object(self) -> "RelationRecord":
        if self.subject_entity_id == self.object_entity_id:
            raise ValueError("subject_entity_id 与 object_entity_id 不能相同")
        return self


class ChunkRefRecord(WikiRecord):
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_revision_id: str = Field(min_length=1)
    legacy_doc_id: str = ""
    chunk_index: int = Field(ge=0)
    source_value: str = ""
    section_path: str = ""
    page_number: int | None = Field(default=None, ge=1)
    text_sha256: str = Field(min_length=1)
    char_start: int = Field(default=0, ge=0)
    char_end: int = Field(default=0, ge=0)
    locator_json: str = "{}"
    created_at: str

    @model_validator(mode="after")
    def _char_range(self) -> "ChunkRefRecord":
        if self.char_end < self.char_start:
            raise ValueError("char_end 不能小于 char_start")
        return self


class ClaimRecord(WikiRecord):
    claim_id: str = Field(default_factory=new_ulid, min_length=1)
    document_id: str | None = None
    page_id: str | None = None
    subject_entity_id: str | None = None
    predicate: str = ""
    object_entity_id: str | None = None
    object_text: str = ""
    claim_text: str = Field(min_length=1)
    claim_type: ClaimType = ClaimType.HYPOTHESIS
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ClaimStatus = ClaimStatus.PROPOSED
    created_at: str
    updated_at: str


class ClaimEvidenceRecord(WikiRecord):
    evidence_id: str = Field(default_factory=new_ulid, min_length=1)
    claim_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    char_start: int = Field(default=0, ge=0)
    char_end: int = Field(default=0, ge=0)
    text_sha256: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: str

    @model_validator(mode="after")
    def _char_range(self) -> "ClaimEvidenceRecord":
        if self.char_end < self.char_start:
            raise ValueError("char_end 不能小于 char_start")
        return self


class ProposalRecord(WikiRecord):
    proposal_id: str = Field(default_factory=new_ulid, min_length=1)
    proposal_type: str = Field(default="update_page", min_length=1)
    status: ProposalStatus = ProposalStatus.PENDING
    page_id: str | None = None
    expected_revision_id: str = ""
    result_revision_id: str = ""
    payload_json: str = "{}"
    rationale: str = ""
    created_by: str = ""
    created_at: str
    updated_at: str
    reviewed_by: str = ""
    reviewed_at: str | None = None
    applied_at: str | None = None
    error_message: str = ""


class WikiJobRecord(WikiRecord):
    job_id: str = Field(default_factory=new_ulid, min_length=1)
    job_type: str = Field(min_length=1)
    status: JobStatus = JobStatus.PENDING
    payload_json: str = "{}"
    result_json: str = "{}"
    error_message: str = ""
    attempts: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class WriteJournalRecord(WikiRecord):
    journal_id: str = Field(default_factory=new_ulid, min_length=1)
    job_id: str | None = None
    proposal_id: str | None = None
    page_id: str | None = None
    target_path: str = ""
    revision_id: str = ""
    state: JournalState = JournalState.PREPARED
    detail_json: str = "{}"
    error_message: str = ""
    created_at: str
    updated_at: str


__all__ = [
    "AliasType",
    "ChunkRefRecord",
    "ClaimEvidenceRecord",
    "ClaimRecord",
    "ClaimStatus",
    "ClaimType",
    "DocumentRecord",
    "DocumentRevisionRecord",
    "DocumentRevisionStatus",
    "DocumentStatus",
    "EntityAliasRecord",
    "EntityRecord",
    "EntityStatus",
    "JobStatus",
    "JournalState",
    "PageEntityRecord",
    "PageLinkRecord",
    "PageRecord",
    "PageRevisionRecord",
    "PageStatus",
    "PageType",
    "ProposalRecord",
    "ProposalStatus",
    "RelationRecord",
    "RelationStatus",
    "WikiJobRecord",
    "WikiRecord",
    "WriteJournalRecord",
]

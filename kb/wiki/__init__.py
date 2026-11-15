# -*- coding: utf-8 -*-
"""Wiki V1 元数据底座。

Phase 1 对外接口：
- ``WikiConfig.from_env()`` / ``ensure_within()``：路径与开关配置；
- ``connect()`` / ``transaction()`` / ``run_migrations()`` / ``db_status()``：SQLite；
- ``document_id()`` / ``chunk_id()`` / ``new_ulid()`` / 文本指纹：稳定身份；
- ``parse_frontmatter()`` / ``render_frontmatter()`` / ``validate_page_frontmatter()``；
- ``models`` 中的 Pydantic 记录模型与 StrEnum。
"""

from __future__ import annotations

from .config import (
    WikiConfig,
    WikiConfigError,
    WikiDisabledError,
    WikiPathError,
    ensure_within,
)
from .db import (
    MIGRATIONS_DIR,
    MigrationChecksumError,
    MigrationError,
    MigrationFile,
    WikiDBError,
    connect,
    db_status,
    discover_migrations,
    run_migrations,
    transaction,
)
from .ids import (
    chunk_id,
    content_sha256,
    document_id,
    new_ulid,
    normalize_text_sha256,
)
from .markdown import (
    FrontmatterError,
    FrontmatterValidationError,
    parse_frontmatter,
    render_frontmatter,
    split_wiki_links,
    validate_page_frontmatter,
)
from .models import (
    AliasType,
    ChunkRefRecord,
    ClaimEvidenceRecord,
    ClaimRecord,
    ClaimStatus,
    ClaimType,
    DocumentRecord,
    DocumentRevisionRecord,
    DocumentRevisionStatus,
    DocumentStatus,
    EntityAliasRecord,
    EntityRecord,
    EntityStatus,
    JobStatus,
    JournalState,
    PageEntityRecord,
    PageLinkRecord,
    PageRecord,
    PageRevisionRecord,
    PageStatus,
    PageType,
    ProposalRecord,
    ProposalStatus,
    RelationRecord,
    RelationStatus,
    WikiJobRecord,
    WikiRecord,
    WriteJournalRecord,
)

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
    "FrontmatterError",
    "FrontmatterValidationError",
    "JobStatus",
    "JournalState",
    "MIGRATIONS_DIR",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationFile",
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
    "WikiConfig",
    "WikiConfigError",
    "WikiDBError",
    "WikiDisabledError",
    "WikiJobRecord",
    "WikiPathError",
    "WikiRecord",
    "WriteJournalRecord",
    "chunk_id",
    "connect",
    "content_sha256",
    "db_status",
    "discover_migrations",
    "document_id",
    "ensure_within",
    "new_ulid",
    "normalize_text_sha256",
    "parse_frontmatter",
    "render_frontmatter",
    "run_migrations",
    "split_wiki_links",
    "transaction",
    "validate_page_frontmatter",
]

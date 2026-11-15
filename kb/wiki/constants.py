# -*- coding: utf-8 -*-
"""Wiki 元数据底座常量。

本模块只放纯常量，不导入其他 wiki 子模块，避免循环依赖。
SQL 迁移中的 CHECK 取值、``models.py`` 的 StrEnum 与本文件的元组必须一致，
``tests/test_wiki_db.py`` 会做一致性校验。
"""

from __future__ import annotations

# ── 默认配置 ──
DEFAULT_VAULT_PATH = r"D:\cherry-workplace\knowledge-hub\vault"
DEFAULT_PAGE_ROOT = "wiki-维基"
DEFAULT_PAGE_INDEX_TABLE = "wiki_pages"
DEFAULT_DB_SUBDIR = ".wiki"
DEFAULT_DB_FILENAME = "wiki.sqlite3"

DEFAULT_AUTO_PUBLISH = False
DEFAULT_REQUIRE_EVIDENCE = True
DEFAULT_MIN_ENTITY_CONFIDENCE = 0.90
DEFAULT_MIN_RELATION_CONFIDENCE = 0.90
DEFAULT_MAX_AUTO_PAGE_UPDATES = 0
DEFAULT_DB_BUSY_TIMEOUT_MS = 5000
DEFAULT_GIT_COMMIT = True

# ── 页面子目录映射（vault 契约：只允许三类，不新建顶层目录） ──
PAGE_TYPE_SUBDIRS = {
    "concept": "concepts-概念",
    "entity": "entities-实体",
    "topic": "topics-主题",
}

# ── 业务表 ──
BUSINESS_TABLES = (
    "documents",
    "document_revisions",
    "pages",
    "page_revisions",
    "entities",
    "entity_aliases",
    "page_entities",
    "page_links",
    "relations",
    "chunk_refs",
    "claims",
    "claim_evidence",
    "proposals",
    "wiki_jobs",
    "write_journal",
)

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
TABLE_NAMES = BUSINESS_TABLES + (SCHEMA_MIGRATIONS_TABLE,)
EXPECTED_BUSINESS_TABLE_COUNT = len(BUSINESS_TABLES)  # 15

# ── SQL CHECK 取值（与 models.py 的 StrEnum 保持一致） ──
DOCUMENT_STATUSES = ("active", "missing", "archived", "error", "legacy_unverified")
DOCUMENT_REVISION_STATUSES = (
    "processing",
    "indexed",
    "failed",
    "superseded",
    "legacy_unverified",
)
PAGE_TYPES = tuple(PAGE_TYPE_SUBDIRS)  # ("concept", "entity", "topic")
PAGE_STATUSES = ("draft", "review", "published", "archived")
ENTITY_STATUSES = ("active", "merged", "deprecated")
RELATION_STATUSES = ("proposed", "verified", "rejected", "deprecated")
ALIAS_TYPES = ("alias", "redirect", "abbreviation", "translation", "synonym")
CLAIM_TYPES = ("fact", "hypothesis")
CLAIM_STATUSES = ("proposed", "accepted", "rejected", "disputed")
PROPOSAL_STATUSES = ("pending", "rejected", "stale", "approved", "applied", "cancelled")
JOB_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
JOURNAL_STATES = (
    "prepared",
    "file_written",
    "db_committed",
    "index_pending",
    "completed",
    "failed",
)

# ── frontmatter 契约 ──
FRONTMATTER_DELIMITER = "---"
VAULT_REQUIRED_FRONTMATTER_FIELDS = ("title", "tags", "created", "type", "summary")
WIKI_REQUIRED_FRONTMATTER_FIELDS = (
    "page_id",
    "slug",
    "page_type",
    "status",
    "aliases",
    "revision_id",
    "updated",
)
WIKI_PAGE_TYPE = "permanent"

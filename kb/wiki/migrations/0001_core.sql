-- Wiki V1 0001: 文档身份、文档修订、页面与页面修订。
-- 时间列统一存 UTC ISO8601 文本；默认值由 SQLite 生成。

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    canonical_uri TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'missing', 'archived', 'error', 'legacy_unverified')),
    current_revision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_type, canonical_uri)
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents (category);
CREATE INDEX IF NOT EXISTS idx_documents_current_revision ON documents (current_revision_id);

CREATE TABLE IF NOT EXISTS document_revisions (
    revision_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents (document_id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    raw_sha256 TEXT NOT NULL,
    parsed_sha256 TEXT NOT NULL,
    processing_fingerprint TEXT NOT NULL,
    parser_name TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    parser_config_sha256 TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    parsed_markdown_path TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    status TEXT NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'indexed', 'failed', 'superseded', 'legacy_unverified')),
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, revision_no),
    UNIQUE (document_id, processing_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_document_revisions_document
    ON document_revisions (document_id);
CREATE INDEX IF NOT EXISTS idx_document_revisions_status
    ON document_revisions (status);

CREATE TABLE IF NOT EXISTS pages (
    page_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    title TEXT NOT NULL,
    page_type TEXT NOT NULL
        CHECK (page_type IN ('concept', 'entity', 'topic')),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'review', 'published', 'archived')),
    current_revision_id TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pages_page_type ON pages (page_type);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages (status);
CREATE INDEX IF NOT EXISTS idx_pages_current_revision ON pages (current_revision_id);

CREATE TABLE IF NOT EXISTS page_revisions (
    revision_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL
        REFERENCES pages (page_id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    relative_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    git_commit TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    change_reason TEXT NOT NULL DEFAULT '',
    source_proposal_id TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (page_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_page_revisions_page ON page_revisions (page_id);
CREATE INDEX IF NOT EXISTS idx_page_revisions_proposal
    ON page_revisions (source_proposal_id);
CREATE INDEX IF NOT EXISTS idx_page_revisions_content_sha
    ON page_revisions (content_sha256);

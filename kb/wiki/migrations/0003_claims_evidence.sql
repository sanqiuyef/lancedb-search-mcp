-- Wiki V1 0003: 旧切片定位桥、Claim 与证据。
-- chunk_refs UNIQUE(document_revision_id, chunk_index, text_sha256)：
-- 真实库存在同一 (source, chunk_index) 下文本不同的切片，不能只按 index 唯一。

CREATE TABLE IF NOT EXISTS chunk_refs (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES documents (document_id) ON DELETE CASCADE,
    document_revision_id TEXT NOT NULL
        REFERENCES document_revisions (revision_id) ON DELETE CASCADE,
    legacy_doc_id TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    source_value TEXT NOT NULL DEFAULT '',
    section_path TEXT NOT NULL DEFAULT '',
    page_number INTEGER CHECK (page_number IS NULL OR page_number >= 1),
    text_sha256 TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0 CHECK (char_start >= 0),
    char_end INTEGER NOT NULL DEFAULT 0 CHECK (char_end >= 0),
    locator_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_revision_id, chunk_index, text_sha256),
    CHECK (char_end >= char_start)
);

CREATE INDEX IF NOT EXISTS idx_chunk_refs_document ON chunk_refs (document_id);
CREATE INDEX IF NOT EXISTS idx_chunk_refs_revision
    ON chunk_refs (document_revision_id);
CREATE INDEX IF NOT EXISTS idx_chunk_refs_legacy_doc
    ON chunk_refs (legacy_doc_id);
CREATE INDEX IF NOT EXISTS idx_chunk_refs_text_sha ON chunk_refs (text_sha256);
CREATE INDEX IF NOT EXISTS idx_chunk_refs_source ON chunk_refs (source_value);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    document_id TEXT
        REFERENCES documents (document_id) ON DELETE SET NULL,
    page_id TEXT
        REFERENCES pages (page_id) ON DELETE SET NULL,
    subject_entity_id TEXT
        REFERENCES entities (entity_id) ON DELETE SET NULL,
    predicate TEXT NOT NULL DEFAULT '',
    object_entity_id TEXT
        REFERENCES entities (entity_id) ON DELETE SET NULL,
    object_text TEXT NOT NULL DEFAULT '',
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL DEFAULT 'hypothesis'
        CHECK (claim_type IN ('fact', 'hypothesis')),
    confidence REAL NOT NULL DEFAULT 0.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'disputed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_claims_document ON claims (document_id);
CREATE INDEX IF NOT EXISTS idx_claims_page ON claims (page_id);
CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_claims_object ON claims (object_entity_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims (status);

CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL
        REFERENCES claims (claim_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL
        REFERENCES chunk_refs (chunk_id) ON DELETE CASCADE,
    quote TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0 CHECK (char_start >= 0),
    char_end INTEGER NOT NULL DEFAULT 0 CHECK (char_end >= 0),
    text_sha256 TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (char_end >= char_start)
);

CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence (claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_chunk ON claim_evidence (chunk_id);

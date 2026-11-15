-- Wiki V1 0004: 提案、后台任务与跨文件/DB/索引写入日志。

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL DEFAULT 'update_page',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'rejected', 'stale', 'approved', 'applied', 'cancelled')),
    page_id TEXT
        REFERENCES pages (page_id) ON DELETE SET NULL,
    expected_revision_id TEXT NOT NULL DEFAULT '',
    result_revision_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    rationale TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    applied_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status);
CREATE INDEX IF NOT EXISTS idx_proposals_page ON proposals (page_id);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals (created_at);

CREATE TABLE IF NOT EXISTS wiki_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_wiki_jobs_status ON wiki_jobs (status);
CREATE INDEX IF NOT EXISTS idx_wiki_jobs_type ON wiki_jobs (job_type);
CREATE INDEX IF NOT EXISTS idx_wiki_jobs_created ON wiki_jobs (created_at);

CREATE TABLE IF NOT EXISTS write_journal (
    journal_id TEXT PRIMARY KEY,
    job_id TEXT
        REFERENCES wiki_jobs (job_id) ON DELETE SET NULL,
    proposal_id TEXT
        REFERENCES proposals (proposal_id) ON DELETE SET NULL,
    page_id TEXT
        REFERENCES pages (page_id) ON DELETE SET NULL,
    target_path TEXT NOT NULL DEFAULT '',
    revision_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'file_written', 'db_committed', 'index_pending', 'completed', 'failed')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_write_journal_state ON write_journal (state);
CREATE INDEX IF NOT EXISTS idx_write_journal_page ON write_journal (page_id);
CREATE INDEX IF NOT EXISTS idx_write_journal_job ON write_journal (job_id);
CREATE INDEX IF NOT EXISTS idx_write_journal_proposal
    ON write_journal (proposal_id);

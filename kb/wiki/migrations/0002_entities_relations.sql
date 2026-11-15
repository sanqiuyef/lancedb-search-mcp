-- Wiki V1 0002: 实体、别名、页面-实体关联、页面双链与关系。

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL COLLATE NOCASE,
    entity_type TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'archived')),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_entities_normalized_name
    ON entities (normalized_name);
CREATE INDEX IF NOT EXISTS idx_entities_entity_type ON entities (entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities (status);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL
        REFERENCES entities (entity_id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL COLLATE NOCASE,
    alias_type TEXT NOT NULL DEFAULT 'alias'
        CHECK (alias_type IN ('alias', 'redirect', 'abbreviation', 'translation', 'synonym')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (entity_id, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized
    ON entity_aliases (normalized_alias);

CREATE TABLE IF NOT EXISTS page_entities (
    page_id TEXT NOT NULL
        REFERENCES pages (page_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL
        REFERENCES entities (entity_id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'mention',
    confidence REAL NOT NULL DEFAULT 0.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (page_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_page_entities_entity ON page_entities (entity_id);

CREATE TABLE IF NOT EXISTS page_links (
    source_page_id TEXT NOT NULL
        REFERENCES pages (page_id) ON DELETE CASCADE,
    target_slug TEXT NOT NULL COLLATE NOCASE,
    target_page_id TEXT
        REFERENCES pages (page_id) ON DELETE SET NULL,
    display_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (source_page_id, target_slug)
);

CREATE INDEX IF NOT EXISTS idx_page_links_target_page
    ON page_links (target_page_id);
CREATE INDEX IF NOT EXISTS idx_page_links_target_slug
    ON page_links (target_slug);

CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL
        REFERENCES entities (entity_id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL
        REFERENCES entities (entity_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'archived')),
    confidence REAL NOT NULL DEFAULT 0.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_page_id TEXT
        REFERENCES pages (page_id) ON DELETE SET NULL,
    evidence TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (subject_entity_id <> object_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations (object_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_predicate ON relations (predicate);
CREATE INDEX IF NOT EXISTS idx_relations_status ON relations (status);

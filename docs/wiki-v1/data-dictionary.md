# Wiki V1 数据字典

> 对应 `kb/wiki/migrations/0001..0004` 与 `kb/wiki/models.py`。
> 共 15 张业务表 + 1 张迁移记录表；SQL CHECK 取值与
> `kb/wiki/constants.py`、Pydantic StrEnum 保持一致。

## 通用约定

- 所有时间列为 `TEXT`，存 UTC ISO8601 文本，默认值由
  `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` 生成（毫秒 + `Z`）。
- 主键与业务 ID 为 ULID 文本（26 字符），schema 只约束非空，不校验格式。
- 布尔值不落库；JSON 结构统一存 `TEXT`（如 `payload_json/detail_json`）。
- 连接级 `PRAGMA foreign_keys = ON`；库级 `journal_mode = WAL`、
  `synchronous = NORMAL`。
- `schema_migrations` 记录已执行迁移的版本、文件名、文件 sha256 与应用时间；
  它不属于 15 张业务表。

## 表关系

```
documents 1 ─── N document_revisions
    │                   │
    │                   └── N chunk_refs ─── N claim_evidence
    │                                            │
    └── N claims ────────────────────────────────┘
pages 1 ─── N page_revisions
  │ 1 ─── N page_links（source）
  │ 1 ─── N page_entities N ─── 1 entities
  │ 1 ─── N proposals
  └── N write_journal（page_id 可空）

entities 1 ─── N entity_aliases
entities 1 ─── N relations（subject / object 两条外键）
pages 1 ─── N relations（source_page_id，可空）

proposals 1 ─── N write_journal
wiki_jobs 1 ─── N write_journal
```

逻辑指针（未声明外键）：`documents.current_revision_id`、
`pages.current_revision_id`、`page_revisions.source_proposal_id`。
这些列由应用层保证一致性，`validator.py` 负责报告不一致。

## 1. documents

文档身份表，按 `(source_type, canonical_uri)` 唯一。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `document_id` | TEXT | PK | 稳定文档 ID |
| `source_type` | TEXT | NOT NULL | 来源类型，如 `file` / `web` |
| `canonical_uri` | TEXT | NOT NULL | 规范化来源 URI/路径 |
| `display_name` | TEXT | NOT NULL DEFAULT `''` | 展示名 |
| `category` | TEXT | NOT NULL DEFAULT `''` | 分类 |
| `status` | TEXT | NOT NULL DEFAULT `'active'`，CHECK | 见状态值 |
| `current_revision_id` | TEXT | 可空，无 FK | 当前文档修订（逻辑指针） |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

唯一约束：`UNIQUE(source_type, canonical_uri)`。
索引：`status`、`category`、`current_revision_id`。

状态值：`active / missing / archived / error / legacy_unverified`。

## 2. document_revisions

文档每次解析/切片的修订记录。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `revision_id` | TEXT | PK | 修订 ID |
| `document_id` | TEXT | NOT NULL，FK → `documents.document_id` ON DELETE CASCADE | 所属文档 |
| `revision_no` | INTEGER | NOT NULL，CHECK `>= 1` | 文档内递增修订号 |
| `raw_sha256` | TEXT | NOT NULL | 原始文件/网页内容哈希 |
| `parsed_sha256` | TEXT | NOT NULL | 解析后 Markdown 哈希 |
| `processing_fingerprint` | TEXT | NOT NULL | 解析配置/版本指纹 |
| `parser_name` | TEXT | NOT NULL DEFAULT `''` | 解析器名称 |
| `parser_version` | TEXT | NOT NULL DEFAULT `''` | 解析器版本 |
| `parser_config_sha256` | TEXT | NOT NULL DEFAULT `''` | 解析配置哈希 |
| `source_path` | TEXT | NOT NULL DEFAULT `''` | 原始文件路径 |
| `parsed_markdown_path` | TEXT | NOT NULL DEFAULT `''` | 解析产物路径 |
| `chunk_count` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 切片数 |
| `status` | TEXT | NOT NULL DEFAULT `'processing'`，CHECK | 见状态值 |
| `error_message` | TEXT | NOT NULL DEFAULT `''` | 失败原因 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

唯一约束：`UNIQUE(document_id, revision_no)`、
`UNIQUE(document_id, processing_fingerprint)`。
索引：`document_id`、`status`。

状态值：`processing / indexed / failed / superseded / legacy_unverified`。

## 3. pages

Wiki 页面身份与当前状态。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `page_id` | TEXT | PK | 稳定页面 ID |
| `slug` | TEXT | NOT NULL，`COLLATE NOCASE`，UNIQUE | 页面 slug（大小写不敏感唯一） |
| `title` | TEXT | NOT NULL | 页面标题 |
| `page_type` | TEXT | NOT NULL，CHECK | `concept / entity / topic` |
| `status` | TEXT | NOT NULL DEFAULT `'draft'`，CHECK | 见状态值 |
| `current_revision_id` | TEXT | 可空，无 FK | 当前页面修订（逻辑指针） |
| `relative_path` | TEXT | NOT NULL，UNIQUE | 相对 vault 的路径，使用 `/` |
| `created_by` | TEXT | NOT NULL DEFAULT `''` | 创建者标识 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

索引：`page_type`、`status`、`current_revision_id`。

状态值：`draft / review / published / archived`。

## 4. page_revisions

页面文件修订元数据；正文以 vault 文件 + Git 为准。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `revision_id` | TEXT | PK | 修订 ID |
| `page_id` | TEXT | NOT NULL，FK → `pages.page_id` ON DELETE CASCADE | 所属页面 |
| `revision_no` | INTEGER | NOT NULL，CHECK `>= 1` | 页面内递增修订号 |
| `relative_path` | TEXT | NOT NULL | 该修订对应文件相对路径 |
| `content_sha256` | TEXT | NOT NULL | 文件内容 sha256 |
| `git_commit` | TEXT | NOT NULL DEFAULT `''` | 关联 Git 提交（可空串） |
| `summary` | TEXT | NOT NULL DEFAULT `''` | 修订摘要 |
| `change_reason` | TEXT | NOT NULL DEFAULT `''` | 变更原因 |
| `source_proposal_id` | TEXT | 可空，无 FK | 来源提案（逻辑指针） |
| `created_by` | TEXT | NOT NULL DEFAULT `''` | 写入者标识 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

唯一约束：`UNIQUE(page_id, revision_no)`。
索引：`page_id`、`source_proposal_id`、`content_sha256`。

## 5. entities

实体（概念、方法、数据集、机构等）。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `entity_id` | TEXT | PK | 实体 ID |
| `canonical_name` | TEXT | NOT NULL | 规范名称 |
| `normalized_name` | TEXT | NOT NULL，`COLLATE NOCASE` | 归一化名称（查询用） |
| `entity_type` | TEXT | NOT NULL DEFAULT `''` | 实体类型（自由文本） |
| `description` | TEXT | NOT NULL DEFAULT `''` | 描述 |
| `confidence` | REAL | NOT NULL DEFAULT 0.0，CHECK 0..1 | 置信度 |
| `status` | TEXT | NOT NULL DEFAULT `'proposed'`，CHECK | 见状态值 |
| `created_by` | TEXT | NOT NULL DEFAULT `''` | 创建者 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

索引：`normalized_name`、`entity_type`、`status`。

状态值：`proposed / accepted / rejected / archived`。
显式合并时 source 实体保留并置 `archived`，不删除行。

## 6. entity_aliases

实体别名；`alias_type=redirect` 时 `alias` 存目标 `entity_id`（应用层约定）。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `entity_id` | TEXT | NOT NULL，FK → `entities.entity_id` ON DELETE CASCADE | 所属实体 |
| `alias` | TEXT | NOT NULL | 别名文本；redirect 行为目标实体 ID |
| `normalized_alias` | TEXT | NOT NULL，`COLLATE NOCASE` | 归一化别名 |
| `alias_type` | TEXT | NOT NULL DEFAULT `'alias'`，CHECK | 见状态值 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

主键：`(entity_id, normalized_alias)`；索引：`normalized_alias`。

别名类型：`alias / redirect / abbreviation / translation / synonym`。

## 7. page_entities

页面与实体的多对多关联。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `page_id` | TEXT | NOT NULL，FK → `pages.page_id` ON DELETE CASCADE | 页面 |
| `entity_id` | TEXT | NOT NULL，FK → `entities.entity_id` ON DELETE CASCADE | 实体 |
| `role` | TEXT | NOT NULL DEFAULT `'mention'` | 页面中的角色 |
| `confidence` | REAL | NOT NULL DEFAULT 0.0，CHECK 0..1 | 置信度 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

主键：`(page_id, entity_id)`；索引：`entity_id`。

## 8. page_links

页面双链（`[[target]]` / `[[target|alias]]`），由 `link_index` 重建。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `source_page_id` | TEXT | NOT NULL，FK → `pages.page_id` ON DELETE CASCADE | 来源页面 |
| `target_slug` | TEXT | NOT NULL，`COLLATE NOCASE` | 链接目标原始 slug/title |
| `target_page_id` | TEXT | 可空，FK → `pages.page_id` ON DELETE SET NULL | 解析后的目标；NULL = 断链 |
| `display_text` | TEXT | NOT NULL DEFAULT `''` | 显示文本 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

主键：`(source_page_id, target_slug)`；索引：`target_page_id`、`target_slug`。

## 9. relations

实体间有向关系。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `relation_id` | TEXT | PK | 关系 ID |
| `subject_entity_id` | TEXT | NOT NULL，FK → `entities.entity_id` ON DELETE CASCADE | 主语实体 |
| `predicate` | TEXT | NOT NULL | 谓词（应用层白名单，见下） |
| `object_entity_id` | TEXT | NOT NULL，FK → `entities.entity_id` ON DELETE CASCADE | 宾语实体 |
| `status` | TEXT | NOT NULL DEFAULT `'proposed'`，CHECK | 见状态值 |
| `confidence` | REAL | NOT NULL DEFAULT 0.0，CHECK 0..1 | 置信度 |
| `source_page_id` | TEXT | 可空，FK → `pages.page_id` ON DELETE SET NULL | 来源页面 |
| `evidence` | TEXT | NOT NULL DEFAULT `''` | 证据文本 |
| `created_by` | TEXT | NOT NULL DEFAULT `''` | 创建者 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

表级 CHECK：`subject_entity_id <> object_entity_id`（禁自环）。
索引：`subject_entity_id`、`object_entity_id`、`predicate`、`status`。

状态值：`proposed / accepted / rejected / archived`。
谓词白名单（`repositories/relations.py`）：`contains_component / uses /
based_on / improves / part_of / predicts / evaluated_on / compared_with /
supports / contradicts / related_to`。schema 不约束谓词，`validator.py` 检查。

## 10. chunk_refs

旧 LanceDB 切片在 Wiki 中的定位引用（不存正文）。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `chunk_id` | TEXT | PK | 切片 ID |
| `document_id` | TEXT | NOT NULL，FK → `documents.document_id` ON DELETE CASCADE | 文档 |
| `document_revision_id` | TEXT | NOT NULL，FK → `document_revisions.revision_id` ON DELETE CASCADE | 文档修订 |
| `legacy_doc_id` | TEXT | NOT NULL DEFAULT `''` | 旧 LanceDB `doc_id` |
| `chunk_index` | INTEGER | NOT NULL，CHECK `>= 0` | 文档内切片序号 |
| `source_value` | TEXT | NOT NULL DEFAULT `''` | 旧 source 字段 |
| `section_path` | TEXT | NOT NULL DEFAULT `''` | 章节路径 |
| `page_number` | INTEGER | 可空，CHECK `NULL` 或 `>= 1` | PDF 页码 |
| `text_sha256` | TEXT | NOT NULL | 归一化文本哈希 |
| `char_start` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 起始字符 |
| `char_end` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 结束字符（右开） |
| `locator_json` | TEXT | NOT NULL DEFAULT `'{}'` | 扩展定位 JSON |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

唯一约束：`UNIQUE(document_revision_id, chunk_index, text_sha256)`。
表级 CHECK：`char_end >= char_start`。
索引：`document_id`、`document_revision_id`、`legacy_doc_id`、
`text_sha256`、`source_value`。

## 11. claims

可引用或待验证的陈述。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `claim_id` | TEXT | PK | Claim ID |
| `document_id` | TEXT | 可空，FK → `documents.document_id` ON DELETE SET NULL | 来源文档 |
| `page_id` | TEXT | 可空，FK → `pages.page_id` ON DELETE SET NULL | 关联页面 |
| `subject_entity_id` | TEXT | 可空，FK → `entities.entity_id` ON DELETE SET NULL | 主语实体 |
| `predicate` | TEXT | NOT NULL DEFAULT `''` | 谓词 |
| `object_entity_id` | TEXT | 可空，FK → `entities.entity_id` ON DELETE SET NULL | 宾语实体 |
| `object_text` | TEXT | NOT NULL DEFAULT `''` | 宾语文本 |
| `claim_text` | TEXT | NOT NULL | Claim 正文 |
| `claim_type` | TEXT | NOT NULL DEFAULT `'hypothesis'`，CHECK | `fact / hypothesis` |
| `confidence` | REAL | NOT NULL DEFAULT 0.0，CHECK 0..1 | 置信度 |
| `status` | TEXT | NOT NULL DEFAULT `'proposed'`，CHECK | 见状态值 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

索引：`document_id`、`page_id`、`subject_entity_id`、`object_entity_id`、
`status`。

状态值：`proposed / accepted / rejected / disputed`。

## 12. claim_evidence

Claim 的引文证据；写入前必须通过 `evidence.verify_quote()` 定位校验。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `evidence_id` | TEXT | PK | 证据 ID |
| `claim_id` | TEXT | NOT NULL，FK → `claims.claim_id` ON DELETE CASCADE | 所属 Claim |
| `chunk_id` | TEXT | NOT NULL，FK → `chunk_refs.chunk_id` ON DELETE CASCADE | 来源切片 |
| `quote` | TEXT | NOT NULL | 引文 |
| `char_start` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 引文起始 |
| `char_end` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 引文结束（右开） |
| `text_sha256` | TEXT | NOT NULL DEFAULT `''` | `quote_sha256(quote)` 指纹 |
| `confidence` | REAL | NOT NULL DEFAULT 0.0，CHECK 0..1 | 置信度 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |

表级 CHECK：`char_end >= char_start`。
索引：`claim_id`、`chunk_id`。

注意：当前表没有 `role` / `verification` 列。`add_claim_evidence(role=...)`
的 role 只作为调用方语义返回，不落库；质量报告的 Quote Validity 在当前
schema 下采用 `text_sha256` + 位置字段的结构校验，若未来新增
`verification` 列则优先按该列统计，并在报告中标注口径。

## 13. proposals

页面更新提案与审核状态。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `proposal_id` | TEXT | PK | 提案 ID |
| `proposal_type` | TEXT | NOT NULL DEFAULT `'update_page'` | 提案类型 |
| `status` | TEXT | NOT NULL DEFAULT `'pending'`，CHECK | 见状态值 |
| `page_id` | TEXT | 可空，FK → `pages.page_id` ON DELETE SET NULL | 目标页面 |
| `expected_revision_id` | TEXT | NOT NULL DEFAULT `''` | 审核时的期望修订 |
| `result_revision_id` | TEXT | NOT NULL DEFAULT `''` | 应用后生成的修订 |
| `payload_json` | TEXT | NOT NULL DEFAULT `'{}'` | 提案内容/证据引用 JSON |
| `rationale` | TEXT | NOT NULL DEFAULT `''` | 理由 |
| `created_by` | TEXT | NOT NULL DEFAULT `''` | 创建者 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |
| `reviewed_by` | TEXT | NOT NULL DEFAULT `''` | 审核者 |
| `reviewed_at` | TEXT | 可空 | 审核时间 |
| `applied_at` | TEXT | 可空 | 应用时间 |
| `error_message` | TEXT | NOT NULL DEFAULT `''` | 失败/过期原因 |

索引：`status`、`page_id`、`created_at`。

状态值：`pending / rejected / stale / approved / applied`。
服务层常量另有 `cancelled`，但当前 schema CHECK 不允许持久化该值。

## 14. wiki_jobs

后台任务队列与结果。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `job_id` | TEXT | PK | 任务 ID |
| `job_type` | TEXT | NOT NULL | 任务类型 |
| `status` | TEXT | NOT NULL DEFAULT `'pending'`，CHECK | 见状态值 |
| `payload_json` | TEXT | NOT NULL DEFAULT `'{}'` | 输入 JSON |
| `result_json` | TEXT | NOT NULL DEFAULT `'{}'` | 结果 JSON |
| `error_message` | TEXT | NOT NULL DEFAULT `''` | 错误信息 |
| `attempts` | INTEGER | NOT NULL DEFAULT 0，CHECK `>= 0` | 尝试次数 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |
| `started_at` | TEXT | 可空 | 开始时间 |
| `finished_at` | TEXT | 可空 | 结束时间 |

索引：`status`、`job_type`、`created_at`。

状态值：`pending / running / succeeded / failed / cancelled`。
状态迁移：`pending → running|failed|cancelled`；
`running → succeeded|failed|cancelled`；终态不可再迁移。

## 15. write_journal

跨文件/DB/索引写入的崩溃恢复日志。

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `journal_id` | TEXT | PK | 日志 ID |
| `job_id` | TEXT | 可空，FK → `wiki_jobs.job_id` ON DELETE SET NULL | 关联任务 |
| `proposal_id` | TEXT | 可空，FK → `proposals.proposal_id` ON DELETE SET NULL | 关联提案 |
| `page_id` | TEXT | 可空，FK → `pages.page_id` ON DELETE SET NULL | 关联页面 |
| `target_path` | TEXT | NOT NULL DEFAULT `''` | 目标文件相对路径 |
| `revision_id` | TEXT | NOT NULL DEFAULT `''` | 目标页面修订 |
| `state` | TEXT | NOT NULL DEFAULT `'prepared'`，CHECK | 见状态值 |
| `detail_json` | TEXT | NOT NULL DEFAULT `'{}'` | 附加信息 JSON |
| `error_message` | TEXT | NOT NULL DEFAULT `''` | 错误信息 |
| `created_at` | TEXT | NOT NULL，默认 UTC | 创建时间 |
| `updated_at` | TEXT | NOT NULL，默认 UTC | 更新时间 |

索引：`state`、`page_id`、`job_id`、`proposal_id`。

状态值：`prepared / file_written / db_committed / index_pending / completed /
failed`。`completed` 之外的记录由 `kb.wiki.recovery.scan_journal()` 分类，
`recover(apply=True)` 只把已收敛记录标 `completed`，永不删除文件或行。

## 状态值汇总

| 域 | 允许值 |
|---|---|
| document.status | `active`, `missing`, `archived`, `error`, `legacy_unverified` |
| document_revision.status | `processing`, `indexed`, `failed`, `superseded`, `legacy_unverified` |
| pages.page_type | `concept`, `entity`, `topic` |
| pages.status | `draft`, `review`, `published`, `archived` |
| entities.status | `proposed`, `accepted`, `rejected`, `archived` |
| entity_aliases.alias_type | `alias`, `redirect`, `abbreviation`, `translation`, `synonym` |
| relations.status | `proposed`, `accepted`, `rejected`, `archived` |
| claims.claim_type | `fact`, `hypothesis` |
| claims.status | `proposed`, `accepted`, `rejected`, `disputed` |
| proposals.status | `pending`, `rejected`, `stale`, `approved`, `applied` |
| wiki_jobs.status | `pending`, `running`, `succeeded`, `failed`, `cancelled` |
| write_journal.state | `prepared`, `file_written`, `db_committed`, `index_pending`, `completed`, `failed` |

## 外键删除行为

| 父表 | 子表字段 | ON DELETE |
|---|---|---|
| documents | document_revisions.document_id | CASCADE |
| documents | chunk_refs.document_id | CASCADE |
| documents | claims.document_id | SET NULL |
| document_revisions | chunk_refs.document_revision_id | CASCADE |
| pages | page_revisions.page_id | CASCADE |
| pages | page_entities.page_id | CASCADE |
| pages | page_links.source_page_id | CASCADE |
| pages | page_links.target_page_id | SET NULL |
| pages | relations.source_page_id | SET NULL |
| pages | claims.page_id | SET NULL |
| pages | proposals.page_id | SET NULL |
| pages | write_journal.page_id | SET NULL |
| entities | entity_aliases.entity_id | CASCADE |
| entities | page_entities.entity_id | CASCADE |
| entities | relations.subject_entity_id | CASCADE |
| entities | relations.object_entity_id | CASCADE |
| entities | claims.subject_entity_id / object_entity_id | SET NULL |
| claims | claim_evidence.claim_id | CASCADE |
| chunk_refs | claim_evidence.chunk_id | CASCADE |
| proposals | write_journal.proposal_id | SET NULL |
| wiki_jobs | write_journal.job_id | SET NULL |

## 派生索引

`WIKI_PAGE_INDEX_TABLE`（默认 `wiki_pages`）不属于上述 15 张业务表，是检索用
派生索引。其真实 schema 由 Phase 4 定义；本阶段质量报告只做表存在性检查：
表缺失时输出“索引未建立”，表存在时按 `page_id` 或 `slug` 列比较 published
页面缺失情况。

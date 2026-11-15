# Wiki V1 架构决策记录（ADR）

> 状态：Phase 7 归档。记录 Wiki V1 已落地或已确定的关键决策，便于后续阶段
> （页面索引、检索接入、UI）沿用同一套边界。

## 决策总览

| 编号 | 决策 | 状态 |
|---|---|---|
| ADR-001 | Git 作为页面版本的权威来源 | 已实施 |
| ADR-002 | 页面元数据适配 vault frontmatter 契约 | 已实施 |
| ADR-003 | 15 张业务表在 Phase 1 一次性建全 | 已实施 |
| ADR-004 | 不内置 LLM，只做证据与提案 | 已实施 |
| ADR-005 | `wiki_pages` 是可重建的派生索引 | 已确定（Phase 4 实施） |
| ADR-006 | `WIKI_ENABLED` 默认关闭 | 已实施 |

---

## ADR-001：Git 作为页面版本的权威来源

**背景**

Wiki 页面以 Markdown 文件存放在 Obsidian vault 中。SQLite 需要记录页面身份、
当前修订和写审计，但不必复制完整正文；vault 本身已经是用户的工作区，通常也是
版本仓库。

**决策**

- 页面正文与历史以 vault 中的 Markdown 文件 + Git 提交为准；
- `pages` 只保存页面身份、slug、路径、状态和 `current_revision_id`；
- `page_revisions` 保存 `content_sha256`、`git_commit`、变更原因与提案引用，
  用于把某次写入关联到具体文件内容与提交；
- PageStore 的写入顺序为：同目录临时文件 → fsync → 重读校验 sha256 →
  `os.replace` → SQLite 事务 → 可选 `git add/commit`；
- Git 提交不是写入成功的前提：`WIKI_GIT_COMMIT=0` 或 vault 不是 Git 仓库时跳过；
  Git 失败时只把 `write_journal` 置 `failed`，不回滚已写入的文件与 DB 记录。

**理由**

- 避免在 SQLite 中重复存储正文，减少双写漂移；
- 用户可直接用 Obsidian/Git 查看、对比、回滚页面，不需要专用工具；
- `content_sha256` 与磁盘文件哈希可检测手工编辑；PageStore 在更新前发现磁盘
  与当前修订不一致时返回 `needs_review`，不覆盖用户改动。

**后果**

- 完整的页面历史依赖 vault 的 Git 仓库；未初始化 Git 时只有当前文件与
  `page_revisions` 元数据，没有提交级历史；
- 恢复流程不能凭空重建 Git 对象，只能以备份的文件/仓库为准；
- 页面索引、全文检索等派生数据必须可重建，不能反过来成为版本权威。

---

## ADR-002：页面元数据适配 vault frontmatter 契约

**背景**

vault 已有自己的 Markdown 规范（见 vault 根 `AGENTS.md` 与 `SCHEMA.md`），
要求 `title/tags/created/type/summary` 五个字段。Wiki 页面还需要页面身份与
修订信息，但不能破坏 Obsidian 的阅读与检索体验。

**决策**

- 页面 frontmatter 在 vault 五字段基础上追加：
  `page_id/slug/page_type/status/aliases/revision_id/updated`；
- `type` 固定为 `permanent`；`created` 为 `YYYY-MM-DD`；`updated` 为 UTC
  ISO8601 文本；`page_type` 只允许 `concept/entity/topic`；`status` 只允许
  `draft/review/published/archived`；
- 页面位置固定为
  `<vault>/wiki-维基/{concepts-概念|entities-实体|topics-主题}/<slug>.md`；
- `validate_page_frontmatter()` 是唯一入口，更新已有页面时禁止修改
  `page_id/slug/page_type`；
- `rebuild_wiki_metadata.py` 对缺 `page_id` 的文件只报告，不自动改写用户文件；
  只有 frontmatter 完整合法的孤儿文件才允许登记 DB。

**理由**

- 与现有 vault 规范兼容，页面仍是普通 Obsidian 笔记；
- 严格的枚举与路径校验可以阻止跨目录写入、路径穿越和 frontmatter 漂移；
- “只报告、不自动改用户文件”避免脚本批量污染 vault。

**后果**

- 手工创建的页面需要补齐 wiki 字段后才能进入 DB；
- 中文目录名（`wiki-维基`、`concepts-概念` 等）是 vault 契约的一部分，
  代码中不做重命名；
- 页面 slug 与文件名保持一致是登记时的硬约束，不一致的文件只报告。

---

## ADR-003：15 张业务表在 Phase 1 一次性建全

**背景**

Wiki V1 涉及文档登记、页面、实体、关系、Claim、证据、提案、任务与写入日志。
如果按功能阶段逐张加表，早期迁移会在多个阶段反复变更，测试与迁移 checksum 也
难以稳定。

**决策**

- `kb/wiki/migrations/0001..0004` 一次性定义 15 张业务表：
  `documents/document_revisions/pages/page_revisions/entities/entity_aliases/
  page_entities/page_links/relations/chunk_refs/claims/claim_evidence/
  proposals/wiki_jobs/write_journal`；
- 另有基础设施表 `schema_migrations`（版本、文件名、sha256、应用时间）；
- 迁移按文件名排序、每个迁移独立事务、应用前用 SQLite backup API 生成
  `<db>.bak-<utcstamp>`；
- 迁移是前向的：已执行的 SQL 文件不允许修改，否则 checksum 校验失败；
- 新需求通过新增迁移实现，不提供自动 down migration。

**理由**

- 多阶段共享同一套 schema，减少跨分支合并冲突；
- checksum + 自动备份使“哪个版本改了什么”可审计；
- 前向迁移比自动回滚更安全：回滚通过备份恢复完成，避免半套 schema。

**后果**

- 部分表在早期阶段没有写入方，属于预留但已建模；
- 任何 schema 修正都必须新增迁移，并同步 `models.py`、`constants.py` 与测试；
- 迁移前备份文件会累积，需要运维侧定期清理。

---

## ADR-004：不内置 LLM，只做证据与提案

**背景**

Wiki 的长期目标是沉淀可引用的知识页面。若写入路径中直接调用生成模型，内容
质量、引用准确性与成本都难以复核。

**决策**

- Wiki 核心不调用任何生成式模型；
- 证据链只接收能在切片原文中定位的引文：`evidence.verify_quote()` 做归一化
  子串匹配，`add_claim_evidence()` 校验通过才落库；
- 页面更新走提案：`ProposalService.create_proposal()` 生成 draft/diff，
  审核通过后由 PageStore 写入；`pending/approved/applied` 与
  `rejected/stale` 状态可审计；
- 质量报告只统计可复核指标；关系正确率明确标注“需人工抽样”，不由脚本编造。

**理由**

- 引用可回溯到 `chunk_refs` 与源文档，降低幻觉进入页面的概率；
- 生成、审核与写入解耦，调用方可以替换模型而不改变存储契约；
- 成本与延迟可控，测试可完全离线。

**后果**

- 页面内容质量取决于调用方/审核者；核心只提供约束与审计；
- 需要额外的提案队列与人工审核流程；
- 证据内容级复核需要回查 LanceDB，SQLite 本身不保存切片正文。

---

## ADR-005：`wiki_pages` 是可重建的派生索引

**背景**

页面检索需要 LanceDB/向量索引（`WIKI_PAGE_INDEX_TABLE`，默认 `wiki_pages`）。
派生索引可能因迁移、恢复、索引升级而缺失或损坏，不能与页面源数据同级。

**决策**

- 页面源数据 = vault Markdown + SQLite `pages/page_revisions` 元数据；
- `wiki_pages` 只作为检索加速的派生索引，允许删除后重建；
- 写入日志用 `index_pending` 表示“文件与 DB 已落、索引待建”；
- 质量报告用表存在性检查 `wiki_pages`：表缺失时明确输出“索引未建立”，
  不把缺失误报为 0；
- 页面索引重建脚本归 Phase 4（`scripts/rebuild_wiki_page_index.py`），
  本阶段不修改、不复制其实现。

**理由**

- 索引损坏不应导致页面内容丢失；删除派生索引是安全的恢复手段；
- 恢复/迁移后可以按需重建，避免把索引状态当成业务真相；
- 质量报告能同时覆盖“未建索引”和“已建但缺页”两种情况。

**后果**

- 恢复数据库或迁移后需要重建页面索引；
- `WIKI_PAGE_INDEX_TABLE` 必须与 Phase 4 索引表名一致；
- 本阶段只能报告索引缺失，不能代替 Phase 4 执行重建。

---

## ADR-006：`WIKI_ENABLED` 默认关闭

**背景**

Wiki 会写入用户 vault 与本地 SQLite，并可能触发 Git 提交。默认开启会让升级
后的服务在用户不知情时改变文件。

**决策**

- `WIKI_ENABLED` 默认 `0`；`WikiConfig.from_env()` 未设置时为 `False`；
- 服务层调用需要启用的接口时，`require_enabled()` 抛出 `WikiDisabledError`，
  提示设置 `WIKI_ENABLED=1` 并重启；
- 运维脚本（备份、校验、质量报告、元数据重建）不强制要求启用，可显式用
  `--db/--vault` 指向目标库与 vault。

**理由**

- 写入型功能必须显式启用，避免误改用户文件；
- 只读运维能力不依赖开关，便于在禁用状态下体检与备份；
- 配置集中在环境变量，便于 MCP 客户端与本地脚本共用。

**后果**

- 首次使用需要显式配置 `WIKI_ENABLED=1` 并重启服务；
- 禁用状态下不能调用页面创建/更新等写入接口；
- 环境变量变更不会热加载，需要重启进程。

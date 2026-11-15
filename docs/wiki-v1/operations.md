# Wiki V1 运维手册

> 适用范围：Phase 7 交付的备份、校验、质量报告、元数据重建与迁移回滚。
> 所有命令使用 `D:\anaconda3\python.exe`，避免调用其他 Python 环境。

## 1. 运行前提

- Python：`D:\anaconda3\python.exe`（当前 3.13.9）。
- 依赖：`pydantic`、`PyYAML`（可选但当前环境已装）；运维脚本本身不依赖
  LanceDB 或 embedding 模型。
- 工作目录：仓库根目录，或给脚本传绝对路径。
- 数据库：已执行 Wiki 迁移的 SQLite 文件，默认
  `<vault>/.wiki/wiki.sqlite3`。
- 所有脚本都支持 `--db`、`--vault` 显式指定目标，不依赖 `cd`。

## 2. 配置与启用/停用

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WIKI_ENABLED` | `0` | 写入功能总开关；`1/true/yes/on` 启用 |
| `WIKI_VAULT_PATH` | `D:\cherry-workplace\knowledge-hub\vault` | vault 绝对路径 |
| `WIKI_PAGE_ROOT` | `wiki-维基` | 页面根目录（相对 vault 或绝对路径） |
| `WIKI_DB_PATH` | `<vault>/.wiki/wiki.sqlite3` | Wiki SQLite 路径 |
| `WIKI_PAGE_INDEX_TABLE` | `wiki_pages` | 派生页面索引表名 |
| `WIKI_AUTO_PUBLISH` | `0` | 是否自动发布 |
| `WIKI_REQUIRE_EVIDENCE` | `1` | fact claim 缺证据是否按 error 级处理 |
| `WIKI_MIN_ENTITY_CONFIDENCE` | `0.90` | 实体置信度阈值 |
| `WIKI_MIN_RELATION_CONFIDENCE` | `0.90` | 关系置信度阈值 |
| `WIKI_MAX_AUTO_PAGE_UPDATES` | `0` | 单次自动更新页面上限 |
| `WIKI_DB_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout |
| `WIKI_GIT_COMMIT` | `1` | 页面写入后是否在 vault 中 git commit |

**启用**：设置 `WIKI_ENABLED=1`（以及需要覆盖的路径），重启服务进程。环境变量
不会热加载；`require_enabled()` 在禁用时抛出 `WikiDisabledError`。

**停用**：设置 `WIKI_ENABLED=0` 并重启。停用后页面创建/更新等写入接口不可用，
但备份、校验、质量报告、元数据重建脚本仍可显式指定 `--db/--vault` 运行。

## 3. vault 工作区要求

- 必须是 Obsidian vault（建议有 `.obsidian/`），根目录存在
  `AGENTS.md`/`SCHEMA.md` 时遵循其笔记规范。
- 页面固定放在：
  ```
  <vault>/wiki-维基/
    concepts-概念/
    entities-实体/
    topics-主题/
  ```
  三类子目录之外不新建页面目录；页面文件名为 `<slug>.md`。
- 建议 vault 是 Git 仓库：页面历史以 Git 为准；不初始化 Git 时
  `WIKI_GIT_COMMIT=1` 会跳过提交，`page_revisions.git_commit` 为空串。
- `.wiki/`（SQLite、WAL、备份、proposals）不应提交到 vault 的 Git 仓库；
  在 vault 的 `.gitignore` 中排除。
- 不要用符号链接/junction 把页面目录指向 vault 外；`ensure_within()` 会拒绝
  跨盘、`..` 段与符号链接逃逸。
- 同一 vault 只允许一个写入进程；SQLite 用 WAL + busy_timeout 容忍短暂并发
  读，但页面文件写入依赖进程内顺序与 `write_journal`。

## 4. 备份

### 4.1 命令

```bash
# 默认：<db_dir>/backups/wiki-<utcstamp>.sqlite3
D:/anaconda3/python.exe -X utf8 scripts/backup_wiki.py \
  --db "D:/path/to/vault/.wiki/wiki.sqlite3" \
  --vault "D:/path/to/vault"

# 指定输出文件或目录（目录不存在时按目录处理需要以 / 或 \ 结尾）
D:/anaconda3/python.exe -X utf8 scripts/backup_wiki.py \
  --db "D:/path/to/vault/.wiki/wiki.sqlite3" \
  --vault "D:/path/to/vault" \
  --out "D:/backups/wiki-20261115.sqlite3"

# 输出完整 manifest JSON
D:/anaconda3/python.exe -X utf8 scripts/backup_wiki.py ... --json
```

### 4.2 产物

- 备份库：用 SQLite backup API 生成的一致性快照，完成后执行
  `PRAGMA integrity_check`，结果必须为 `ok`。
- 提案归档：若 `<db_dir>/proposals` 存在，生成
  `proposals-<utcstamp>.tar.gz`（归档内顶层目录为 `proposals/`）。
- `manifest.json`（与备份库同目录）字段：
  - `created_at`：UTC 时间；
  - `source_db`：源库绝对路径、大小、sha256；
  - `backup`：备份文件路径、大小、sha256、`integrity_check`、
    `schema_versions`；
  - `proposals`：归档路径、大小、sha256、来源目录、文件数（目录不存在时为
    `null`）；
  - `vault`：vault 路径、是否 Git 仓库、HEAD、分支、status 摘要
    （dirty、变更/未跟踪/修改/新增/删除/重命名/冲突数量、前 50 行样本）；
  - `manifest_path`：本次 manifest 的绝对路径。
- 同一输出目录重复备份会覆盖 `manifest.json`；历史备份以各自的
  `wiki-<utcstamp>.sqlite3` / `proposals-<utcstamp>.tar.gz` 文件为准。
- 脚本**不执行 git push**，不做远程复制；需要异地备份时由外部工具接管。

### 4.3 失败处理

- 源库不存在或不是文件：退出码 1，stderr 输出“源库不存在/不是文件”，不创建
  任何输出。
- 输出文件已存在：拒绝覆盖（退出码 1）；换一个 `--out` 或先归档旧文件。
- 备份或 integrity_check 失败：清理本次已创建的文件并报错。

## 5. 恢复

1. 停止所有写入进程（MCP 服务、脚本、Obsidian 同步任务）。
2. 恢复 SQLite：
   ```bash
   cp "D:/backups/wiki-20261115.sqlite3" "D:/path/to/vault/.wiki/wiki.sqlite3"
   rm -f "D:/path/to/vault/.wiki/wiki.sqlite3-wal" \
         "D:/path/to/vault/.wiki/wiki.sqlite3-shm"
   ```
   恢复后不要保留旧库的 `-wal`/`-shm`，否则可能把旧事务重放到新库。
3. 恢复提案目录（若归档存在）：
   ```bash
   tar -xzf "D:/backups/proposals-<utcstamp>.tar.gz" \
     -C "D:/path/to/vault/.wiki"
   ```
   归档解压后为 `.wiki/proposals/`；如已有同名目录，先移到一边再解压。
4. 校验：
   ```bash
   D:/anaconda3/python.exe -X utf8 scripts/validate_wiki.py \
     --db "D:/path/to/vault/.wiki/wiki.sqlite3" \
     --vault "D:/path/to/vault" --strict
   D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_metadata.py \
     --db "D:/path/to/vault/.wiki/wiki.sqlite3" \
     --vault "D:/path/to/vault"          # dry-run，先看差异
   ```
5. 重建页面索引（见 §9），再恢复服务。

说明：页面正文在 vault 文件中，不在 SQLite 备份里，因此**恢复数据库不会恢复
页面文件**；页面文件/历史由 vault 自身的备份或 Git 仓库恢复。

## 6. 崩溃恢复（write_journal）

`kb.wiki.recovery.scan_journal(conn)` 返回非 `completed` 的 journal，按
`prepared/file_written/db_committed/index_pending/failed` 分类。每个动作固定为
`{journal_id, state, action, detail}`。

| state | 恢复动作 | 说明 |
|---|---|---|
| `prepared` | `complete` / `mark_failed` / `needs_registration` / `manual_review` | 文件存在且与 DB 当前修订一致 → 可标 completed；文件不存在 → 建议人工置 failed；DB 缺修订 → 需登记 |
| `file_written` | `complete` / `needs_registration` / `manual_review` | 重算文件 sha256 并与 `revision_id` 对应 page_revision 比对 |
| `db_committed` | `update_index` | DB 已提交，需更新页面索引 |
| `index_pending` | `rebuild_index` | 需重建页面索引 |
| `failed` | `report_only` | 只报告 `error_message`，不自动处理 |

执行恢复建议：

```python
from kb.wiki.config import WikiConfig
from kb.wiki.db import connect
from kb.wiki.recovery import recover, scan_journal

config = WikiConfig.from_env()
conn = connect(config.db_path, config)
print(scan_journal(conn))
actions = recover(conn, config, apply=False)   # 先只读看建议
# 人工确认后：
# recover(conn, config, apply=True)            # 只把确定收敛的记录标 completed
```

安全边界：

- `recover()` **不删除**任何文件、journal 行、page_revisions 行或其他业务数据；
- `apply=True` 只把“文件 sha256 + DB 当前修订一致”的记录标 `completed`；
- `prepared` 且文件缺失不会自动写 `failed`，只报告 `mark_failed` 建议；
- `file_written` 缺 DB 记录时可用
  `rebuild_wiki_metadata.py --apply` 按 vault frontmatter 登记后再运行
  `recover(apply=True)`（要求 frontmatter 合法且 slug/路径一致）；
- `db_committed`/`index_pending` 需要 Phase 4 页面索引重建完成后，由运维确认
  再更新 journal 状态（可使用
  `kb.wiki.repositories.jobs.update_journal_state(conn, id, "completed")`）。

## 7. 校验与质量报告

### 7.1 一致性校验

```bash
D:/anaconda3/python.exe -X utf8 scripts/validate_wiki.py \
  --db "<db>" --vault "<vault>"            # Markdown（默认）
D:/anaconda3/python.exe -X utf8 scripts/validate_wiki.py \
  --db "<db>" --vault "<vault>" --json     # JSON
D:/anaconda3/python.exe -X utf8 scripts/validate_wiki.py \
  --db "<db>" --vault "<vault>" --strict   # 有 error 时退出码 1
```

- 只读打开数据库（`mode=ro`），不执行迁移、不修复、不写 vault；
- 覆盖 frontmatter、当前修订归属、磁盘 hash、断链、fact claim 缺证据、
  关系自环/非法谓词、来源缺失、页面文件缺失与孤儿页面文件；
- 同时输出未完成 journal 分类；
- 退出码：`0` 正常；`1` strict 且存在 error；`2` 运行环境错误
  （库缺失、未迁移、读取失败）。

### 7.2 质量报告

```bash
# stdout（Markdown）
D:/anaconda3/python.exe -X utf8 scripts/wiki_quality_report.py \
  --db "<db>" --vault "<vault>"

# 写入文件
D:/anaconda3/python.exe -X utf8 scripts/wiki_quality_report.py \
  --db "<db>" --vault "<vault>" \
  --out docs/wiki-v1/quality-report.md

# JSON
D:/anaconda3/python.exe -X utf8 scripts/wiki_quality_report.py \
  --db "<db>" --vault "<vault>" --json
```

指标口径：

- **Evidence Coverage**：`claim_type` 属于
  `fact/factual/definition/result` 且状态非 rejected 的 claim 中，至少有一条
  `claim_evidence` 的比例。当前 schema 只有 fact/hypothesis，
  definition/result 尚未作为独立类型落库，后续扩展 claim_type 后自动纳入。
- **Quote Validity**：若 `claim_evidence` 表含 `verification` 列，按
  `verification='verified'` 统计通过率；当前 schema 无该列，且切片原文不在
  SQLite，则按 `quote_sha256(quote)` 与 `text_sha256` 比对、
  `char_end >= char_start` 做结构校验。内容级复核需
  `evidence.revalidate_evidence()` 回查 LanceDB，报告会明确标注口径与限制。
- **Broken Link Rate**：`page_links.target_page_id IS NULL` / 总链接；无链接时
  比率为 `null`。
- **Orphan Pages**：published 页面中无入链且无出链的数量。
- **Duplicate Page Candidates**：标题/别名归一后重复、摘要相似度达到阈值
  （默认 0.82，可 `--similarity-threshold` 调整）的候选；只报告，不合并。
- **Unindexed Published Pages**：published 页面在 `WIKI_PAGE_INDEX_TABLE`
  中缺失；索引表不存在时输出“索引未建立”。
- **Unsourced Relations**：proposed/accepted 关系既无 `evidence` 也无
  `source_page_id`。
- **Unresolved Aliases**：redirect 目标实体不存在、别名所属实体
  archived/rejected、同一 `normalized_alias` 被多个 active 实体共享。

关系正确率需人工抽样，脚本不计算、不编造。

## 8. 重建元数据

从 vault 页面 frontmatter 补齐 SQLite 的 `pages/page_revisions`。

```bash
# dry-run（默认，只报告）
D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_metadata.py \
  --db "<db>" --vault "<vault>"
D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_metadata.py \
  --db "<db>" --vault "<vault>" --json

# 写入缺失记录
D:/anaconda3/python.exe -X utf8 scripts/rebuild_wiki_metadata.py \
  --db "<db>" --vault "<vault>" --apply
```

行为：

- 只扫描 `wiki-维基/{concepts-概念,entities-实体,topics-主题}/*.md`；
- 缺 `page_id` 的文件只报告，**不改写用户文件**；
- frontmatter 非法、slug 与文件名不一致、page_type 与目录不一致：只报告；
- DB 不存在且 frontmatter 合法：`--apply` 插入 `pages` + `page_revisions`，
  `content_sha256` 取文件原始字节哈希，`revision_no=1`；
- DB 已有记录：报告 `existing`；slug/路径/revision 冲突：报告冲突，不覆盖；
- DB 有记录但文件缺失：报告 `orphan_page_records`，不删除；
- `--apply` 幂等：第二次执行不会重复登记；
- `--apply`/`--dry-run` 互斥；
- 本脚本只补 `pages/page_revisions`，不重建 `page_links`、不写页面索引；
  登记后如需完整链接与检索数据，再运行页面索引/链接重建流程。

## 9. 重建页面索引

页面索引由 `kb/wiki/page_index.py` 实现，重建入口是
`scripts/rebuild_wiki_page_index.py`。

流程：

1. 确认 `WIKI_PAGE_INDEX_TABLE` 与索引表名一致（默认 `wiki_pages`）；
2. 运行 `python -X utf8 scripts/rebuild_wiki_page_index.py`（默认增量；`--dry-run` 预览，`--drop` 显式重建）；
3. 运行质量报告确认 `unindexed_published_pages.status == "ok"` 且缺失数为 0；
4. 对 `index_pending` journal 人工确认后标 `completed`。

索引表尚未建立时，质量报告会明确输出“索引未建立”，不应把该状态当成 0 缺失。

## 10. 迁移与回滚

- 迁移文件位于 `kb/wiki/migrations/`，按文件名排序执行；每个迁移单独事务，
  失败不会写 `schema_migrations`。
- 已执行迁移的 sha256 记录在 `schema_migrations`；修改已执行 SQL 文件会导致
  `MigrationChecksumError`。**不要编辑已应用的迁移文件**，需要变更时新增迁移。
- `run_migrations()` 在应用待执行迁移前，用 SQLite backup API 生成
  `<db>.bak-<utcstamp>`（内存库跳过）；这些文件是回滚依据，保留策略由运维决定。
- 回滚方式：停止写入 → 用 `<db>.bak-<utcstamp>` 或正式备份覆盖当前库 →
  删除 `-wal`/`-shm` → 运行 `validate_wiki.py`。
- 不提供自动 down migration；不要手工删除 `schema_migrations` 行来“重放”迁移。
- 运维脚本不会自动执行迁移；遇到“数据库未执行 Wiki 迁移”错误时先显式迁移。

## 11. 日常检查清单

- [ ] `validate_wiki.py --strict` 退出码 0；
- [ ] 质量报告无新增断链/孤儿页；Evidence Coverage 与 Quote Validity 变化可解释；
- [ ] `journal.total == 0`，或每个未完成 journal 都有明确处理动作；
- [ ] 备份 manifest 的 `integrity_check == "ok"`，备份文件 sha256 与 manifest 一致；
- [ ] vault Git 工作区状态符合预期，`.wiki/` 未被提交；
- [ ] 页面索引状态为已建立，published 页面无未索引项。

# Wiki V1 测试报告

## 结论

| 项目 | 结果 |
|---|---|
| 基线（Phase 0 之前） | 40 项 |
| 当前全量 discover | **495 项，全部通过** |
| 失败/错误/跳过 | 0 / 0 / 0 |
| 运行环境 | Windows 10.0.19045，Python 3.13.9（`D:\anaconda3\python.exe`） |
| 全量耗时 | 约 36 秒 |

## 测试演进

| 阶段 | 累计测试数 | 主要范围 |
|---|---:|---|
| 基线 | 40 | 原有检索/入库/网页/监听/图像等基础能力 |
| Phase 0 | 218 | watcher、ingest 身份/幂等、URL 规范化、图片去重 |
| Phase 1 | 134（分支） | 配置、稳定 ID、frontmatter、SQLite 迁移 |
| Phase 2 | 278（分支） | 文档登记、切片引用、证据链、旧库迁移 |
| Phase 3 | 299（分支） | 页面存储、实体/关系、页面双链、提案、validator |
| schema + Phase 4 + 7 | 434 | 状态对齐、页面索引/检索、恢复/备份/质量指标 |
| 导航层 | 453 | index/log/purpose/overview、统一提交 |
| Phase 5 | 485 | service 门面 + 14 个 MCP 工具 + 抽取契约 |
| Phase 6 | 494 | Wiki 阅读与提案审核 UI |
| 发布修复 | 495 | 正文型提案审核通过后自动发布并入索引 |

新增测试全部使用 `tempfile.TemporaryDirectory()`，不访问真实 vault、真实 Wiki SQLite 或 LanceDB。

## Phase 8 端到端（隔离环境）

临时 vault + 真实 `my_docs` 的 300 条切片：

| 步骤 | 结果 |
|---|---|
| 迁移 | 3 个文档、300 条 chunk_refs |
| 抽取 | 1 个实体、1 条 factual claim，引文校验通过，0 拒绝 |
| 提案 → 审核 | pending → applied |
| 发布 | 页面 status=published，Git 提交 `wiki: update ... (r2)` |
| 导航 | index/log/purpose/overview 均生成，Git 提交 `wiki: refresh navigation` |
| 页面索引 | 真实 bge-m3 索引成功，`index_error=""` |
| 检索 | `wiki_search(mode=page)` 命中 1 条 |
| 校验 | 0 error / 0 warning |

## 运行命令

```bash
# 全量（仓库根目录）
D:/anaconda3/python.exe -X utf8 -m unittest discover -s tests -t .

# Phase 7 目标测试
D:/anaconda3/python.exe -X utf8 -m unittest \
  tests.test_wiki_recovery tests.test_wiki_ops -v
```

## Phase 7 覆盖明细

### `tests/test_wiki_recovery.py`（8 项）

| 测试 | 验证点 |
|---|---|
| `test_scan_classifies_open_states_and_excludes_completed` | 五种非 completed 状态各归一类；completed 不进入扫描结果 |
| `test_prepared_missing_file_reports_mark_failed_without_write` | prepared + 文件不存在 → `mark_failed` 建议；apply=False/True 都不写状态 |
| `test_prepared_existing_file_and_db_converge_completes` | prepared + 文件与 DB 当前修订一致 → apply=False 只读，apply=True 标 completed |
| `test_matching_hash_completes_only_with_apply` | file_written 哈希一致：只读不写；apply 后 completed；文件数与 journal 行数不变 |
| `test_hash_mismatch_requires_manual_review` | 文件被手工修改 → `manual_review`，apply 不改状态 |
| `test_missing_db_record_needs_registration` | 缺 page_revision → `needs_registration`，不自动登记 |
| `test_db_committed_index_pending_failed_actions` | 三种状态分别返回 `update_index` / `rebuild_index` / `report_only`；apply 不改变 |
| `test_apply_completes_only_convergent_and_never_deletes` | 多状态混合：apply 只完成可收敛记录；文件、journal、page_revisions 数量不变 |

### `tests/test_wiki_ops.py`（16 项）

| 测试 | 验证点 |
|---|---|
| `test_backup_creates_db_proposals_and_manifest` | backup API 产物可打开、数据完整；manifest 大小/sha256/integrity；proposals tar 内容；非 Git vault 标记 |
| `test_default_output_directory` | 默认 `<db_dir>/backups/wiki-*.sqlite3` 与 manifest 生成 |
| `test_missing_source_returns_error_without_output` | 源库缺失 → 退出码 1、stderr 清晰、不生成输出 |
| `test_clean_state_json` | validate CLI 正常 JSON、计数为 0 |
| `test_warning_only_passes_strict` | 只有 warning 时 `--strict` 返回 0 |
| `test_error_fails_strict` | error 级问题 `--strict` 返回 1，问题码正确 |
| `test_failed_journal_is_reported` | validate JSON 含 failed journal 及错误信息 |
| `test_missing_database_returns_two` | validate 运行环境错误退出码 2 |
| `test_markdown_output_contains_sections` | 默认 Markdown 含报告、问题、journal 段落 |
| `test_metric_values_and_scope_notes` | 构造 claim/link/orphan/重复标题别名/摘要相似/关系/别名，断言各指标数字；断言口径与“关系正确率需人工抽样” |
| `test_quote_validity_uses_verification_column_when_present` | 增加 verification 列后优先按 `verification='verified'` 统计并通过率 |
| `test_index_table_presence_is_checked` | 无 `wiki_pages` → “索引未建立”；建表后按 page_id 计算缺失数 |
| `test_cli_markdown_out_and_json` | `--out` 写 Markdown 文件；`--json` 输出结构化指标 |
| `test_dry_run_apply_idempotent_and_never_edits_files` | 合法/缺 page_id/非法 frontmatter/DB 孤儿；dry-run 不写；apply 登记 hash 正确；用户文件字节不变；二次 apply 幂等 |
| `test_cli_json_dry_run_and_apply` | rebuild CLI dry-run/apply JSON 与 DB 写入 |
| `test_missing_database_returns_two` | rebuild 运行环境错误退出码 2 |

## 覆盖范围

- **恢复**：`write_journal` 五类未完成状态 + completed 排除；prepared /
  file_written 的文件 hash 与 page_revision 比对；pages 当前修订校验；
  apply 的只写 completed 约束；不删除任何文件或行。
- **备份**：SQLite backup API、integrity_check、sha256、proposals tar.gz、
  manifest、默认输出路径、源库缺失错误。
- **校验 CLI**：validator + scan_journal 组合、Markdown/JSON 输出、
  `--strict` 退出码、运行环境错误退出码。
- **质量报告**：Evidence Coverage、Quote Validity（结构口径）、Broken Link
  Rate、Orphan Pages、Duplicate Page Candidates、Unindexed Published Pages、
  Unsourced Relations、Unresolved Aliases；索引表存在/缺失两种路径。
- **元数据重建**：合法页面登记、缺 page_id 只报告、非法 frontmatter、
  DB 孤儿、文件不被改写、apply 幂等、CLI dry-run/apply。
- **既有回归**：配置、迁移、frontmatter、文档登记、证据链、实体/关系、
  页面存储、提案、链接、validator 全部保持通过。

## 未覆盖与已知限制

- journal 状态由测试直接构造，未做真实进程 kill/断电注入；恢复逻辑的
  “收敛判定”被覆盖，但操作系统级 fsync/断电行为不在单测范围。
- 按任务要求不访问真实 vault/LanceDB；Quote Validity 未做内容级回查，
  只验证 `quote_sha256(quote)` 与位置字段的结构口径。
- 页面索引已合并并有临时 LanceDB 测试；Phase 8 用真实 bge-m3 跑通一次，
  但未对大规模页面库做性能与索引重建压测。
- 备份未覆盖大库、在线高并发写入、磁盘满、权限拒绝、跨盘/网络盘与远程复制；
  不自动 push 是设计约束，不属于未实现功能。
- 元数据重建未覆盖符号链接/junction、超长路径、非 UTF-8 文件名等平台边界；
  frontmatter YAML 复杂嵌套由既有 `markdown.py` 测试覆盖，本阶段只覆盖
  wiki 字段校验。
- 恢复不处理 Git 对象损坏/缺失；页面历史回滚依赖 vault Git 仓库或文件备份。
- 质量报告的摘要相似度比较为 O(n²)，大页面库需要观察耗时；候选列表截断为
  前 50 项，计数仍为全量。

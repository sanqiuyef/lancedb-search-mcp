# Wiki V1 真实库迁移报告

- 执行日期：2026-11-15
- 源库：`D:\cherry-workplace\knowledge-hub\lancedb`（LanceDB，只读）
- 目标库：`D:\cherry-workplace\knowledge-hub\wiki\wiki.sqlite3`（SQLite，新建）
- 迁移脚本：`scripts/migrate_wiki_v1.py`
- 原则：只登记，不改原表；不重算向量；不删除任何 LanceDB 行。

## 迁移前审计

| 项目 | 数值 |
|---|---:|
| `my_docs` 行数 | 26,583 |
| `image_chunks` 行数 | 4,186 |
| 去重 source | 218（全部为纯 basename，无路径分隔符） |
| `(source, chunk_index)` 重复组 | 3,654 组 / 21,147 行 |
| 重复 `doc_id` | 0 |
| 空 `source` / 空 `doc_id` | 0 / 0 |
| `ingested_at` 范围 | 2026-10-03T16:56:06Z → 2026-11-05T05:07:18Z |

重复 `(source, chunk_index)` 的历史原因是同一文件被多次入库、分块发生变化而 source 与 chunk_index 未变；`doc_id` 含文本内容，因此不重复。迁移把每一行都保留为独立证据，不合并、不删除。

## dry-run（只读）

命令：

```bash
python -X utf8 scripts/migrate_wiki_v1.py --json
```

结果：

| 项目 | 数值 |
|---|---:|
| 检查行数 | 26,583 |
| 跳过行数 | 0 |
| 将创建 documents | 218 |
| 将创建 document_revisions | 218 |
| 将创建 chunk_refs | 26,583 |
| 相同 `(document, chunk_index, text_sha256)` | 0 |
| canonical 冲突 | 0 |
| LanceDB 行数变化 | 无（26,583 / 4,186 前后一致） |

## apply（已执行）

命令：

```bash
WIKI_DB_PATH="D:\cherry-workplace\knowledge-hub\wiki\wiki.sqlite3" \
  python -X utf8 scripts/migrate_wiki_v1.py --apply --json
```

结果：

| 表 | 行数 |
|---|---:|
| `schema_migrations` | 4（0001–0004） |
| `documents` | 218 |
| `document_revisions` | 218（全部 `legacy_unverified`） |
| `chunk_refs` | 26,583 |
| `pages` / `claims` / `entities` / `relations` / `proposals` | 0 |

- SQLite 文件大小：约 24 MB。
- `my_docs` / `image_chunks` 行数迁移前后不变。
- 未对 LanceDB 执行 add/delete/update/create_index/optimize/drop。
- 重复执行 idempotent：dry-run 的 `documents_existing` / `revisions_existing` 会随已登记数量增加，不会重复插入。

## 后续修正（2026-11-15 当天）

### source 改为相对路径

- 用 `scripts/migrate_sources_to_relative.py` 原地更新 `source` 列，不重算向量。
- 216/218 个 source 在 vault 中唯一匹配并改写为相对路径；2 个无文件保持 basename：
  - `Sharifian 等 - 2023 - LISFLOOD-FP 8.1 ... .pdf`
  - `SoilGrids 2.0: producing soil information for the globe with quantified spatial uncertainty.md`
- `my_docs` 行数 26,583 不变；`text_idx` / `vector_idx` 重建后仍覆盖 26,583 行。
- 备份：`D:\cherry-workplace\knowledge-hub\backups\my_docs.lance-20261115-src-rewrite`。

### FTS 分词器改为 ngram

- lancedb 0.39 + jieba 对查询「不确定性」会触发 Rust `memory allocation ... failed` 并退出进程；临时 2 行表可复现。
- 默认改为 `FTS_BASE_TOKENIZER=ngram`（2–3 gram），真实 `text_idx` 已重建；「不确定性 / 洪涝 / CNN-LSTM / flood」均可正常检索。
- 如需恢复 jieba：`FTS_BASE_TOKENIZER=jieba/default`，但该查询风险仍在。

### Wiki DB 重建与种子页

Wiki SQLite 已按新的路径级 source 重建：

| 表 | 行数 |
|---|---:|
| `documents` / `document_revisions` | 218 / 218 |
| `chunk_refs` | 26,583 |
| `pages` | 3（published） |
| `claims` / `claim_evidence` | 6 / 6 |
| `entities` / `page_entities` | 3 / 3 |
| `page_links` | 6 |
| `proposals` | 3（全部 applied） |

3 个证据种子页（真实切片逐条摘录，未做推断）：

- `urban-flood-evidence` — 城市洪涝（1 条证据）
- `cnn-lstm-evidence` — CNN-LSTM（2 条证据）
- `flood-uncertainty-evidence` — 洪水建模不确定性（3 条证据）

质量报告：Evidence Coverage 100%（6/6）、Quote Validity 100%（6/6 结构校验）、断链 0、未索引 published 页面 0、校验 0 error / 0 warning。

## 已知限制

- 2 个未匹配 source 仍是 basename，watcher 会跳过其自动更新/删除，需人工确认。
- 3,654 组重复位置的行全部保留为不同文本切片的证据；`chunk_refs` 的唯一键是 `(document_revision_id, chunk_index, text_sha256)`，不使用 `(revision, index)`。
- 种子页只做证据摘录，不是完整综述；Claim/Entity/Relation 的进一步抽取由 Agent 侧按 `wiki_submit_extraction` 契约写入。
- vault 导航文件使用 `ai-` 前缀；用户自己的 `INDEX.md` 已从 git 历史恢复，后续生成不会覆盖无生成标记的同名文件。

## 回滚

- 目标 SQLite 是本次新建，可直接删除 `D:\cherry-workplace\knowledge-hub\wiki\wiki.sqlite3` 回到迁移前状态（原始 LanceDB 始终未被修改）。
- 如需保留审计，可先执行 `scripts/backup_wiki.py` 生成快照。

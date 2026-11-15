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

## 已知限制

- 218 个 source 都是 basename，历史文件在不同目录下同名时无法区分；统一登记为 `legacy_unverified`，不伪造原始路径、raw hash 或解析器版本。
- 3,654 组重复位置的行全部保留为不同文本切片的证据；`chunk_refs` 的唯一键是 `(document_revision_id, chunk_index, text_sha256)`，不使用 `(revision, index)`。
- 迁移没有重建页面、没有生成 Claim/Entity/Relation；这些由 Agent 侧抽取后通过 `wiki_submit_extraction` 写入。
- 真实 vault 的 `wiki-维基/` 目前没有页面；`index.md` / `log.md` / `purpose.md` / `overview.md` 会在第一次发布后生成。

## 回滚

- 目标 SQLite 是本次新建，可直接删除 `D:\cherry-workplace\knowledge-hub\wiki\wiki.sqlite3` 回到迁移前状态（原始 LanceDB 始终未被修改）。
- 如需保留审计，可先执行 `scripts/backup_wiki.py` 生成快照。

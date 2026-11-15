# LanceDB Search MCP

面向 AI 工作流的**本地优先**文档知识库检索服务（MCP Server）。内核基于
[lancedb](https://github.com/lancedb/lancedb) 官方 SDK：LanceModel schema、官方 embedding
注册表、原生 BM25 全文检索（默认 ngram 中文分词，可切 jieba）、官方 hybrid 混合检索与 Reranker 接口。

- **嵌入/精排全本地**：嵌入（bge-m3）与精排（bge-reranker-v2-m3）都在本机 GPU 跑，用时挂载、
  闲置自动卸载显存，不调用云端推理 API；仅扫描件 OCR 可选启用云 MinerU 回退
  （`MINERU_API_KEY`，默认关闭，显式配置后该回退会把文件发往云端）；
- **公式级 PDF 解析**：PDF 入库优先走本地 [MinerU](https://github.com/opendatalab/MinerU)
  pipeline，把论文里的公式转成 **LaTeX** 后再入库 —— 而不是纯文本抽取器那种
  `(cid:2)`、字母打散、上下标丢失的坏结果（见「PDF 解析」一节）；
- **混合检索 + 精排**：向量 + BM25 融合，CrossEncoder 精排；`ask_knowledge` 检索并打包
  编号证据块，作答由调用方模型完成；
- **15 个 MCP 工具（启用 Wiki 层后 29 个）**：检索 / 证据打包 / 增删改查 / 网页抓取入库 /
  目录监听自动重索引 / 图像检索；Wiki 层另提供 14 个页面、证据与提案工具；
- **零插件检索面板**：可选的本地面板（`ui_server.py`，127.0.0.1:8002），供人直接查询。

## 主要能力

- **检索模式**：`search_knowledge` 默认 `search_mode="vector"`；显式选 `hybrid` 时走向量 +
  BM25 融合，且只有 `use_reranker=True` 才加载本地 CrossEncoder 精排（失败回退 RRF）；
  `vector` 与 `text` 分支不做精排；
- **全本地模型**：BAAI/bge-m3 嵌入 + bge-reranker-v2-m3 精排，模型缓存目录由 `HF_HOME`
  指定；**用时挂载、闲置自动卸载显存**（`LOCAL_MODEL_IDLE_UNLOAD`，默认 300s，0=常驻，
  对齐 Ollama keep-alive）；
- **原生 FTS**：Lance 原生 BM25 倒排索引，默认 ngram 2–3 分词（避免 lancedb 0.39 的 jieba 中文查询 panic）；
- **证据打包（非生成式问答）**：`ask_knowledge` 检索并打包 `[n]` 编号证据块（来源 + 全文
  chunk），不调用任何生成模型；作答与引用标注由 MCP 宿主模型基于证据完成；
- **PDF → Markdown（含 LaTeX 公式）**：所有 PDF 先经本地 MinerU 转成 vault 内
  Markdown 再入库；转换失败即报错，不再回退 PDF 文本层（避免公式被压平）；
- **网页抓取 + 目录监听**：网页正文先写成 vault 剪藏再入库；监听 vault 目录自动重索引。

## 来源规则（2026-11-15）

**vault 是唯一向量化来源**：`my_docs.source` 只能是 vault 相对路径，不再直接索引
vault 外文件或 PDF 文本层。

- **所有二进制先转 Markdown 入 vault**：PDF 用本地 MinerU；`.docx/.pptx/.xlsx`
  先用 docling，失败回退 markitdown；MinerU 3.4 的本地 API 健康检查会绕过
  `127.0.0.1/localhost/::1` 代理（代码已自动注入 `NO_PROXY`）；
- **落点固定**：paper 类 → `08-literature-文献/papers-论文全文/`；其他转换产物 →
  `raw-素材/converted-转换/`；网页剪藏 → `00-inbox-收件箱/from-web-网页/`；
- `add_documents(scan_dir)` 只接受 vault 内目录；`add_documents(reindex_all=True)`
  只接受 vault 根（且扫描结果非空），否则拒绝清空知识库；
- **外部文件用 `add_single_document`**：自动转换/复制进 vault 后再入库；`scan_dir`
  参数仅为兼容保留，不参与 source 计算；
- 历史遗留的 basename source 用 `scripts/relink_sources_to_vault.py` 重指向
  （默认 dry-run，`--apply` 才写库）。

## 快速开始

```powershell
# 1) 安装运行时依赖（Python 3.10+；GPU 可选，CPU 亦能跑，只是慢）
python -m pip install -r requirements-runtime.txt
python -m pip install torch sentence-transformers        # 本地嵌入/精排所需

# 2) 配置 MCP（复制示例并按本机修改路径）
copy .mcp.example.json .mcp.json

# 3) 启动服务（stdio MCP，通常由 MCP 客户端拉起）
python -X utf8 server.py

# 4) 入库（在 MCP 客户端里调用工具）
#    add_documents(scan_dir="<vault>")             目录批量入库（只接受 vault 内目录）
#    add_single_document(filepath="...")           单文件入库（外部文件自动转换进 vault）
#    search_knowledge(query="...", search_mode="hybrid")
```

首次检索会自动下载嵌入模型（约 2GB，可用 `HF_ENDPOINT=https://hf-mirror.com` 走镜像）。
`LANCEDB_DB_PATH` 不设时默认建在 `D:\cherry-workplace\knowledge-hub\lancedb`
（作者机器布局），**建议显式设置成你自己的路径**。

### 安装 PDF 转换环境（PDF 入库必需）

2026-11-15 起 PDF 入库不再回退文本层：未安装 MinerU 时 PDF 转换会直接失败，请先
安装本地 MinerU，或先把 PDF 转成 Markdown 放入 vault 再入库。

```powershell
# MinerU 本地 pipeline 需要独立 venv（其 transformers 版本要求与主环境常冲突）
python -m venv --system-site-packages .mineru-venv
.\.mineru-venv\Scripts\python -m pip install "mineru[pipeline]==3.4.4" "transformers>=4.57,<5"
mineru-models-download -s modelscope -m pipeline      # 首次下载模型（约 2.5GB）
```

之后 PDF 入库会自动调用它（转换产物按 路径+大小+mtime 缓存在 `<库目录>/_pdf_md_cache/`）：
文本层含乱码/数学字母 → 自动切 OCR 模式，中文论文同样自动识别。
可用 `LANCEDB_MINERU=0` 关闭（关闭后 PDF 入库会失败，只适合纯 Markdown 库）、
`LANCEDB_MINERU_BIN` 指定可执行文件路径。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LANCEDB_DB_PATH` | `D:\cherry-workplace\knowledge-hub\lancedb` | LanceDB 库目录（**建议显式设置**） |
| `LOCAL_EMBED_MODEL` / `LOCAL_EMBED_DIM` | `BAAI/bge-m3` / `1024` | 嵌入模型与维度 |
| `LOCAL_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 精排模型 |
| `LOCAL_MODEL_DEVICE` | `auto` | `cuda` / `cpu` / `auto` |
| `LOCAL_MODEL_IDLE_UNLOAD` | `300` | 闲置卸载显存秒数，0=常驻 |
| `HF_HOME` / `HF_HUB_CACHE` | 系统默认 | 模型缓存目录 |
| `LANCE_LANGUAGE_MODEL_HOME` | `D:\lance\language_models` | 仅 `FTS_BASE_TOKENIZER=jieba/default` 时需要 |
| `FTS_BASE_TOKENIZER` | `ngram` | 全文分词器；可显式设为 `jieba/default` |
| `FTS_NGRAM_MIN` / `FTS_NGRAM_MAX` | `2` / `3` | ngram 分词长度范围 |
| `LANCEDB_MINERU` | `1` | 是否启用本地 MinerU 转换（0 关闭） |
| `LANCEDB_MINERU_BIN` / `_CACHE` / `_STAGING` / `_TIMEOUT` | 见 `kb/config.py` | MinerU 可执行文件 / 缓存目录 / 短路径暂存 / 超时 |
| `LANCEDB_OCR` | `0` | 是否启用扫描件 OCR 回退链 |
| `TESSERACT_CMD` / `LANCEDB_OCR_LANG` | 见 `kb/config.py` | Tesseract 路径与语言包 |
| `MINERU_API_KEY` | 空 | 云 MinerU API（可选，仅扫描件回退链用） |

## MCP 工具（15 个；启用 Wiki 后 29 个）

`WIKI_ENABLED=1` 时额外注册 14 个 Wiki 工具（见「Wiki 知识层」）；默认关闭，原 15 个
工具与启动方式不变。

| 类别 | 工具 |
|---|---|
| 检索与证据打包 | search_knowledge、ask_knowledge、search_similar |
| 知识库管理 | get_knowledge_status、list_documents、get_document、add_documents、add_single_document、update_document、delete_documents |
| 自建能力 | ingest_url、start_watcher、stop_watcher |
| 图像检索（2026-10-06） | search_images（以文搜图）、ingest_images（图片向量入库） |

`search_knowledge` 默认 `search_mode="vector"`，可选 `text` / `hybrid`；Reranker 只在
`hybrid` 分支且 `use_reranker=True` 时生效。`ask_knowledge` 只做检索与证据打包，返回
`[n] 来源 + 全文 chunk` 的编号文本，不调用生成模型。

`add_documents` / `add_single_document` 均支持 `export_markdown_dir` 参数：入库的同时把
PDF 转换出的 Markdown + 图片导出到指定目录留档（标准布局 `<名>.md` + `<名>_images/`）。

### 删除与更新语义（Phase 0 修复后）

- `delete_documents`：先按完整 source 精确匹配（`normcase`，Windows 下不区分大小写）；
  仅当未命中且模式含 `* ? [` 时按 `fnmatch` 通配（如 `*.pdf`、`docs/*`），不再做子串匹配；
  匹配集合覆盖全部来源时会被拒绝。`source_pattern` 留空或 `delete_documents("*")`
  都必须显式 `confirm_all=True` 才会清空。
- `update_document`：按完整文件路径唯一确定已有 source；同名 basename 匹配到多个 source
  且路径后缀无法唯一判定时返回错误并列出候选，不执行任何写入或删除。
- `add_single_document`：source 一律取 vault 相对路径；vault 外文件先转换/复制进
  vault 再入库，`scan_dir` 仅兼容保留。若库中已有等价 source，会按新内容幂等替换。

### 网页入库

- `ingest_url` 的顺序：抓取 → `kb.vault_source.save_web_markdown` 写
  `00-inbox-收件箱/from-web-网页/<safe-title>-<sha1(canonical)[:8]>.md` →
  `add_single_document(md_path)` 分块入库；
- source 是该剪藏的 vault 相对路径，不再产生 `网页-<host>-<hash>` 形式的 source；
- 同一 canonical URL 重复抓取复用同一快照文件，切片按内容幂等替换；抓取失败不写
  vault、不改库，快照/入库失败会明确返回错误；
- canonical 规则不变：scheme/host 小写、去默认端口、去 fragment、清
  `utm_*` / `fbclid` / `gclid`，**不折叠尾斜杠**，query 参数不排序。

### 目录监听

- 已适配 watchdog 6.0 事件 API（`is_directory`），支持同时监听多个目录；父子目录
  重叠会被拒绝，需先停止已有监听。
- 新增/修改走尾沿防抖：同一路径静默 5 秒后只重索引一次；删除事件批量延迟 1.5 秒
  清理；移动事件同时处理旧来源删除与新位置重索引（目标移出监听根时只清理旧来源）。
- 不传 `watch_dir` 时监听当前工作目录，返回文本会明确提示这一点。

## 图像向量检索（2026-10-06 新增）

以 `jinaai/jina-clip-v2`（图文对齐双塔，1024 维，CC-BY-NC-4.0 仅限个人学术用途）
为图像嵌入模型，与文本链路（bge-m3）**完全独立**：独立表 `image_chunks`、独立
嵌入函数、共享闲置卸载机制。中文查询直接可用（以文搜图）；`caption` 当前恒为空
字符串（字段预留，尚未接入描述生成）。

- **模型本地化（重要）**：`D:\huggingface\local-models\jina-clip-v2\` 与
  `local-models\jina-embeddings-v3\` 为**自包含目录**（权重 + 内联实现代码 +
  auto_map 改写为本地引用）。原因：transformers v5 的动态模块离线解析对
  trust_remote_code 多文件模型不稳定，且 jina 两级模型各自引用远程实现仓库。
  重建方法：从 HF 缓存快照 copytree → 拷入实现仓库 *.py → 剥掉 auto_map 的
  `仓库前缀--` → v2 的 `text_config.hf_model_name_or_path` 指向 v3 本地目录 →
  `text_config.hf_model_config_kwargs.use_flash_attn=false`（本机未装 flash-attn）。
  另给本地 modeling_clip.py 打了两处 isinstance→类名校验补丁（v5 动态模块按文件
  内容分目录缓存，config 与 model 类来自不同模块实例，isinstance 必然失败）。
- **依赖兼容**：Anaconda 需 `timm`（EVA02 视觉塔）；`kb/image_search.py` 运行时
  给 `transformers.models.clip.modeling_clip` 垫回 v5 移除的 `clip_loss`（推理不调用，
  仅为通过顶层 import）。
- 入库：`ingest_images(scan_dir)`，默认扫描 vault 文献图片目录，按 mtime 增量；
  mtime 变化的图片或同一路径存在多行时会替换旧行（先写新行，再按旧 `_rowid` 删除）；
  历史遗留的重复行可通过 `ingest_images(rebuild=True)` 清空重建，或再次入库收敛。
- 检索：`search_images(query, limit, source_filter)`，cosine 距离转相似度输出。

## Wiki 知识层（默认关闭）

在原始切片检索之上增加一层可审阅的 Wiki：SQLite 保存文档/修订/切片引用/断言与证据/
实体/关系/提案/写入日志，页面正文写在 Obsidian vault 的 Markdown 文件，Git 作为版本库。
服务端不内置 LLM、不调用外部 API；抽取与写作由宿主 Agent 完成后提交。

### 启用与配置

设置 `WIKI_ENABLED=1` 并重启服务后追加 14 个 MCP 工具；默认 `0` 时原 15 个工具与启动
方式完全不变。环境变量不会热加载。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WIKI_ENABLED` | `0` | Wiki 总开关（`1/true/yes/on` 启用） |
| `WIKI_VAULT_PATH` | `D:\cherry-workplace\knowledge-hub\vault` | Obsidian vault 绝对路径 |
| `WIKI_DB_PATH` | `<vault>/.wiki/wiki.sqlite3` | Wiki SQLite 路径 |
| `WIKI_PAGE_ROOT` | `wiki-维基` | 页面根目录（相对 vault） |
| `WIKI_PAGE_INDEX_TABLE` | `wiki_pages` | 页面向量索引表名 |
| `WIKI_AUTO_PUBLISH` | `0` | 是否自动发布页面 |
| `WIKI_REQUIRE_EVIDENCE` | `1` | fact claim 缺证据是否按 error 处理 |
| `WIKI_MIN_ENTITY_CONFIDENCE` | `0.90` | 实体置信度阈值 |
| `WIKI_MIN_RELATION_CONFIDENCE` | `0.90` | 关系置信度阈值 |
| `WIKI_DB_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout |
| `WIKI_GIT_COMMIT` | `1` | 页面写入后是否在 vault 中 git commit |

完整配置与运维流程见 `docs/wiki-v1/operations.md`。

### MCP 工具（14 个）

| 工具 | 类型 | 说明 |
|---|---|---|
| `wiki_status` | 只读 | 配置（脱敏）、DB 版本、表计数、页面/提案/任务统计、索引状态与未收敛 journal |
| `wiki_search` | 只读 | 统一检索：page / evidence / hybrid / related |
| `wiki_get_page` | 只读 | 页面全文、当前修订、出链/反链与证据摘要 |
| `wiki_list_pages` | 只读 | 分页列出页面（默认 `published`） |
| `wiki_get_related` | 只读 | 页面或实体的 1-2 跳链接与关系邻域 |
| `wiki_get_evidence` | 只读 | 按 claim_id 查看证据链，或按 chunk_id 查看证据包 |
| `wiki_search_evidence` | 只读 | 直接检索原始切片证据（`score_source=rag`） |
| `wiki_create_page` | 写入 | 创建 `draft` 页面，不发布 |
| `wiki_propose_update` | 写入 | 为已有页面创建 `update_page` 提案（写 draft/diff） |
| `wiki_submit_extraction` | 写入 | 提交 Agent 抽取的实体/断言/关系/页面建议，校验后落库 |
| `wiki_get_proposal` | 只读 | 提案全文、diff、payload 与目标页当前修订 |
| `wiki_review_queue` | 只读 | 按状态列出提案审核队列（默认 `pending`） |
| `wiki_apply_proposal` | 写入 | `approve` 写入页面并刷新导航/索引；`reject` 不改页面 |
| `wiki_validate` | 只读 | 一致性校验（pages/links/claims/documents/entities/relations/journal） |

页面写入默认只产生 Draft 或 Proposal；`wiki_submit_extraction` 只落库实体/断言/关系，
不直接发布页面；只有 `wiki_apply_proposal(decision="approve")` 才会发布或更新页面。

### 页面布局与 frontmatter

```
<vault>/wiki-维基/
├── INDEX.md（用户自己的总索引）
├── 使用说明.md（人读的目录与用法说明）
├── ai-index.md / ai-log.md / ai-purpose.md / ai-overview.md
├── concepts-概念/
├── entities-实体/
└── topics-主题/
```

- `ai-index.md`、`ai-overview.md` 是生成物，重建时覆盖；`ai-purpose.md` 只在不存在时生成模板，
  之后由人维护；`ai-log.md` 只追加。`ai-` 前缀用于避开用户自己的 `INDEX.md` / `log.md`。
- 页面 frontmatter = vault 五字段 `title/tags/created/type/summary` +
  `page_id/slug/page_type/status/aliases/revision_id/updated`，可选 `sources`；
  `type` 固定 `permanent`，`page_type` 为 `concept/entity/topic`，
  `status` 为 `draft/review/published/archived`。

### 证据链

`claim → claim_evidence → chunk_refs → document_revisions → documents`。引文写入前先做
空白归一化，必须能在切片原文中定位（`evidence.verify_quote()`）；切片正文不存 SQLite，
默认按 `chunk_refs` 的 `source_value/chunk_index/legacy_doc_id` 回查 LanceDB。

### 工具链

| 脚本 | 说明 |
|---|---|
| `scripts/migrate_wiki_v1.py` | 把 `my_docs` 既有切片登记进 Wiki SQLite（`--apply` 写入，`--limit` 限量） |
| `scripts/audit_wiki_environment.py` | 只读审计运行环境、LanceDB 表与 source 形态 |
| `scripts/validate_wiki.py` | 一致性校验 CLI（`--strict` / `--json`，只读） |
| `scripts/backup_wiki.py` | SQLite + proposals 备份，生成 manifest |
| `scripts/wiki_quality_report.py` | 质量报告：证据覆盖、断链、孤儿页、重复候选等 |
| `scripts/rebuild_wiki_metadata.py` | 从 vault frontmatter 补齐 `pages/page_revisions` |
| `scripts/rebuild_wiki_page_index.py` | 重建/增量更新 `wiki_pages` 页面索引 |
| `scripts/relink_sources_to_vault.py` | 旧 basename source 重指向 vault 论文 Markdown（默认 dry-run，`--apply` 同步 LanceDB/Wiki） |

迁移状态：2026-11-15 已对真实库执行（218 documents / 218 revisions / 26,583 chunk_refs）；`--apply` 才写 SQLite，重复执行幂等。迁移报告见 `docs/wiki-v1/migration-report.md`。

### 许可边界

本仓库代码 MIT。Wiki 设计参考 Andrej Karpathy 的 LLM Wiki 方法论与
[nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)（GPL-3.0），仅借鉴公开设计，
未复制或改写其代码；模型权重遵循各自许可。设计记录见
`docs/wiki-v1/llm-wiki-inspired-design.md`。

## 自建模块清单（待后期单独优化）

以下模块为自建实现，文件头部有 `[自建模块 · 待后期单独优化]` 标记：

| 模块 | 说明 | 优化方向 |
|---|---|---|
| `kb/web.py` | 网页抓取 → vault 剪藏 Markdown → 幂等入库 | 反爬重试、正文智能抽取、与 web-search-server 抓取链复用 |
| `kb/watcher.py` | watchdog 6.0 目录监听（多目录、删除/移动同步、5 秒尾沿防抖） | 整目录移入/移出的递归迁移兜底、事件队列 |
| `mineru_pdf_splitter.py` | MinerU 大 PDF 预切分 | 按需使用，独立工具 |

## 架构

```
server.py                MCP 薄入口（15 工具；启用 Wiki 后 29 个）
kb/                      知识库核心包
  config.py              环境变量 + 单库路径解析（无分区）
  embeddings.py          本地 embedding（bge-m3，官方注册表 + LRU 缓存）
  image_search.py        ★图像向量检索（jina-clip-v2 自包含加载 + image_chunks 表）
  model_lifecycle.py     本地模型生命周期：用时挂载、闲置自动卸载（gc + empty_cache）
  schema.py              LanceModel schema、建表、FTS/向量索引维护、库目录 README
  pdf_convert.py         ★PDF→Markdown（本地 MinerU pipeline；公式→LaTeX，缓存+档位判定）
  vault_source.py        ★vault 唯一来源层（文本复制/PDF/Office 转换/网页剪藏，source 取 vault 相对路径）
  ingest.py              来源准备、分块（800/100）、增删改查（只接受 vault 来源）
  search.py              官方 hybrid + 本地 CrossEncoder 精排（失败回退 RRF）
  ask.py                 检索证据打包（[n] 编号证据块，不调用生成模型）
  web.py / watcher.py    [自建模块] 网页剪藏入库 / 目录监听（watchdog 6.0）
  wiki/                  Wiki V1 知识层：SQLite 元数据/证据/提案 + vault 页面 + Git
                         （config/db/ids/markdown/models 基础设施；document_registry/
                         evidence/entity_resolver/page_store/link_index/proposals/
                         navigation/page_index/search/service/validator）
scripts/                 rebuild_from_sources.py（批量重建）+ mineru_to_markdown.py（批量 PDF→md）
                         + pdf_to_markdown.py（docling 备选）+ 冒烟脚本 + mineru_pdf_splitter.py
                         + Wiki 工具链（migrate/audit/validate/backup/quality/rebuild）
ui_server.py / ui/       检索面板（零插件版，HTTP 8002，复用 kb.search）+ run_ui.cmd 启动器
```

### PDF 解析（2026-10-04 起：本地 MinerU 管线）

PDF 入库优先调用本地 MinerU（`D:\cherry-workplace\mcp_servers\lancedb-search\.mineru-venv`，
mineru 3.4.4 + transformers 4.57；Anaconda 的 transformers v5 与其不兼容）把 PDF 转成
**含 LaTeX 公式的 Markdown**，落到 vault 后走标题感知分块。转换失败直接报错，不再回退
PDF 文本层；`kb.ingest.extract_content` 仍保留旧的文本层/OCR 回退链供直接调用与兼容，
但 `add_documents` / `add_single_document` 等入库入口不再使用它。转换产物按
`路径+大小+mtime` 缓存在 `<库>/_pdf_md_cache/`。

- 档位自动判定：文本层含 `U+FFFD`/私用区/数学字母（U+1D400-1D7FF）/`(cid:` → 强制 OCR；
  正文 CJK≥15% → 中文+OCR；其余 → 英文+auto（详见 `kb/pdf_convert.py` docstring）。
- 背景：pdfminer 主通路会把公式毁成 `(cid:N)`/字母打散（116 篇文献中 1313 块受损、MSE 等
  检索 0 命中），2026-10-03 全量用 MinerU 重转（116/116，0 失败，9256 处 LaTeX 公式）。
- 独立跑批量入库脚本前建议保持脚本内置的 `LOCAL_MODEL_IDLE_UNLOAD=0` 与 `TQDM_DISABLE=1`
  （消除闲置卸载看门狗/tqdm 监视线程，规避 Python 3.13+torch 的原生线程状态崩溃）。

单库扁平模式（2026-10-02 起移除分区机制）：一个 LanceDB 库一个池子，无 project 列；
类别（category）按源目录自动标注（支持目录名包含匹配），可作过滤条件。库路径优先级：
`LANCEDB_DB_PATH 环境变量 > 默认 knowledge-hub/lancedb`（全局知识库根目录内的向量库子文件夹，根目录规划与 Obsidian 协作）。

## 入库与批量重建

- **一条命令走完（推荐）**：`python -X utf8 scripts/pdf_pipeline.py <PDF目录>
  [--md-dir <vault内md目录>] [--workers 3] [--resume] [--no-ingest] [--dry-run]`
  —— 阶段 1 PDF → Markdown（MinerU 并行转换、带缓存、可断点），md 必须落在 vault 内
  （默认 `08-literature-文献/papers-论文全文`）；阶段 2 委托
  `rebuild_from_sources.py`，按 vault 相对路径入库。MinerU 不可用时直接报错退出；
- **按需入库**：MCP 工具 `add_documents`（只扫 vault 内目录）/ `add_single_document`
  （单文件，外部文件自动转换进 vault，PDF 走 MinerU → LaTeX）；两者都支持
  `export_markdown_dir` 参数，把转换出的 Markdown + 图片额外导出留档（不改变 source）；
- **只转换不入库**：`python -X utf8 scripts/mineru_to_markdown.py <PDF目录> <md目录>
  --workers 3`（每篇产出 `<名>.md` + `<名>_images/`，可断点续跑）；
  备选引擎 docling 见 `scripts/pdf_to_markdown.py`；
- **重建/更新**：`python -X utf8 scripts/rebuild_from_sources.py <vault内目录>`
  （`--dry-run`、`--limit-files`，checkpoint 断点续传，自动按 `doc_id` 去重）。
  只接受 vault 内目录；二进制先转 vault Markdown 再入库；
- **历史 source 重指向**：`python -X utf8 scripts/relink_sources_to_vault.py
  --db "<lancedb>" --vault "<vault>"`（默认 dry-run；`--apply` 写 LanceDB 与 Wiki
  SQLite，`--mark-missing` 标记无匹配文档）；
- **实测规模参考**（作者使用场景）：116 篇论文（2205 页）→ 18k 切片，
  3 并行转换约 1.5 小时（RTX 4060），重建入库约 12 分钟。

## 检索面板（零插件版，人用）

`ui_server.py` + `ui/index.html`：只绑 `127.0.0.1:8002` 的本地检索面板，检索内核复用
`kb.search.search_structured`（混合检索 + 本地精排），供 Obsidian（核心 Web Viewer）或
任意浏览器内嵌使用，不依赖 MCP 与任何 Obsidian 插件。
Wiki 不提供 UI：页面、证据与审核队列都在 vault 的 Markdown 和 MCP 工具里，由 Agent 直接读写。

```powershell
run_ui.cmd                     # 启动器（内部设好 HF/env 后拉起 ui_server.py）
# 浏览器打开 http://127.0.0.1:8002/
```

## 安全约定

仓库不提交：API Key、`.mcp.json`、LanceDB 数据、日志、虚拟环境。
首次使用复制 `.mcp.example.json` → `.mcp.json` 并按本机修改。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

当前全量基线 592 项（2026-11-15 实测 `unittest discover`）。离线测试（假 embedding、
mock HTTP）覆盖配置、分块、schema/检索端到端、reranker 回退、证据打包、网页
canonical/幂等、目录监听（防抖/删除/多目录）、图像入库计划与替换，以及 Wiki 层
（配置/ID/frontmatter/迁移、文档登记、证据链、实体关系、页面存储、双链、提案、
检索、服务门面、MCP 注册、页面索引、恢复与运维脚本）。

## License

代码 [MIT](LICENSE) © 2026 sanqiuyef。

Wiki 层的设计参考 Andrej Karpathy 的 LLM Wiki 方法论与 `nashsu/llm_wiki`（GPL-3.0），
仅借鉴设计，未复制其代码（见 `docs/wiki-v1/llm-wiki-inspired-design.md`）。

模型权重遵循各自许可，与代码许可相互独立：

- `jinaai/jina-clip-v2`：CC-BY-NC-4.0，仅限个人学术用途；
- `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`：仓库内未声明其许可，使用前请核对
  各自模型卡。
- MinerU 及其模型、Tesseract 语言包等第三方组件同样遵循各自许可。

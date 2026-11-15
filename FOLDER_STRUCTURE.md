# 目录结构

```
lancedb-search/
├── server.py                       MCP 薄入口（15 个内置工具；WIKI_ENABLED=1 时追加 14 个 Wiki 工具，共 29 个）
├── kb/                             知识库核心包
│   ├── config.py                   环境变量与单库路径解析（无分区）
│   ├── embeddings.py               本地 embedding（bge-m3，官方注册表 + LRU 缓存）
│   ├── image_search.py             ★图像向量检索（jina-clip-v2 自包含加载 + image_chunks 表，2026-10-06）
│   ├── model_lifecycle.py          本地模型生命周期：用时挂载、闲置自动卸载显存
│   ├── schema.py                   LanceModel schema、建表、FTS/向量索引、库目录 README
│   ├── ingest.py                   文档解析（MinerU→文本层→OCR 回退链）、分块、增删改查
│   ├── pdf_convert.py              ★PDF→Markdown 核心（本地 MinerU pipeline，缓存+档位判定；与 scripts 批量脚本共用）
│   ├── vault_source.py             ★vault 唯一来源层：文本复制、PDF/Office 转换、网页剪藏；source 取 vault 相对路径
│   ├── search.py                   检索内核：vector/text/hybrid + 本地 CrossEncoder 精排（回退 RRF）
│   ├── ask.py                      检索证据打包（[n] 编号证据块，不调用生成模型）
│   ├── web.py                      [自建模块 · 待优化] 网页抓取 → vault 剪藏 Markdown → 幂等入库
│   ├── watcher.py                  [自建模块 · 待优化] watchdog 6.0 目录监听（多目录、删除/移动同步、防抖）
│   └── wiki/                       Wiki V1 知识层（SQLite 元数据 + vault 页面 + Git）
│       ├── __init__.py             包导出：配置、迁移、ID、frontmatter 等公共接口
│       ├── config.py               WIKI_* 配置、绝对路径解析与 vault 边界校验
│       ├── constants.py            默认路径、页面类型、表名、状态值与 frontmatter 契约
│       ├── contracts.py            submit_extraction 的 Pydantic 请求/响应模型
│       ├── db.py                   SQLite 连接、事务、PRAGMA 与迁移器（checksum + 备份）
│       ├── document_registry.py    文档身份、修订幂等、切片引用登记与旧源迁移
│       ├── entity_resolver.py      实体规范化、候选召回、显式合并与页面关联
│       ├── evidence.py             引文校验、Claim 证据链与 LanceDB 原文回查
│       ├── ids.py                  document/chunk 稳定 ID、ULID 与文本指纹
│       ├── link_index.py           [[wikilink]] 出链解析、反链与断链报告
│       ├── markdown.py             frontmatter 安全解析/校验 + wikilink 纯工具
│       ├── models.py               Pydantic v2 记录模型与 StrEnum
│       ├── navigation.py           index/log/purpose/overview 的生成与刷新
│       ├── page_index.py           wiki_pages LanceDB 页面索引（写入/删除/重建）
│       ├── page_store.py           页面原子写、frontmatter 适配、SQLite 元数据与 git 提交
│       ├── proposals.py            draft/diff 落盘、审核队列与 apply/reject
│       ├── recovery.py             write_journal 扫描与崩溃恢复建议（只读优先）
│       ├── search.py               页面 hybrid、证据直通、图谱邻域统一检索
│       ├── service.py              service 门面：只读查询、草稿/提案/审核、两步抽取
│       ├── validator.py            一致性校验（只报告，不修复）
│       ├── migrations/             0001_core ~ 0004_jobs_journal（15 张业务表）
│       └── repositories/           仓储层（薄 SQL 读写）
│           ├── claims.py           claims / claim_evidence
│           ├── documents.py        documents / document_revisions / chunk_refs
│           ├── entities.py         entities / entity_aliases / page_entities
│           ├── jobs.py             wiki_jobs / write_journal
│           ├── pages.py            pages / page_revisions
│           ├── proposals.py        proposals
│           └── relations.py        relations / 谓词白名单 / 邻域 BFS
│
├── scripts/
│   ├── rebuild_from_sources.py     批量重建脚本（vault 内目录 + 二进制转换 + checkpoint 断点续传）
│   ├── smoke_local_backend.py      本地双模型真实加载冒烟（GPU 推理验证）
│   ├── smoke_idle_unload.py        闲置自动卸载冒烟（显存释放 + 重载验证）
│   ├── test_jina_clip_smoke.py     图像嵌入冒烟（kb.image_search 生产路径 + 中文查询×样图矩阵）
│   ├── test_image_search.py        入库后真实图像检索验证（中文查询 top5）
│   ├── search_knowledge_cli.py     search_knowledge 命令行兜底（独立子进程查文本库）
│   ├── search_images_cli.py        search_images 命令行兜底（独立子进程查图像库）
│   ├── mineru_pdf_splitter.py      [自建 OCR 链路] MinerU 大 PDF 预切分工具
│   ├── pdf_pipeline.py             ★ 标准管道：PDF → Markdown → 向量库（一条命令，可断点）
│   ├── mineru_to_markdown.py       PDF→Markdown 批量转换（只转换不入库）
│   ├── pdf_to_markdown.py          PDF→Markdown 备选转换器（docling 公式增强 + CUDA）
│   ├── audit_wiki_environment.py   Wiki 只读审计：运行环境、LanceDB 表与 source 形态
│   ├── migrate_wiki_v1.py          Wiki V1 迁移入口（默认 dry-run，--apply 写 SQLite）
│   ├── migrate_sources_to_relative.py  my_docs 的 basename source → vault 相对路径（不重算向量）
│   ├── relink_sources_to_vault.py  ★旧 basename source 按论文同名 md 重指向（默认 dry-run，--apply 同步 Wiki）
│   ├── validate_wiki.py            Wiki 一致性校验 CLI（--strict/--json，只读）
│   ├── backup_wiki.py              Wiki SQLite + proposals 备份与 manifest
│   ├── wiki_quality_report.py      Wiki 质量报告（证据覆盖/断链/孤儿页/重复候选等）
│   ├── rebuild_wiki_metadata.py    从 vault frontmatter 补齐 pages/page_revisions（默认 dry-run）
│   └── rebuild_wiki_page_index.py  重建/增量更新 wiki_pages 页面索引（--drop 全量）
├── docs/
│   └── wiki-v1/
│       ├── architecture-decisions.md  Wiki V1 ADR（Git 权威、frontmatter、15 张表等）
│       ├── data-dictionary.md         15 张业务表、状态值、外键与派生索引
│       ├── llm-wiki-inspired-design.md 与 llm_wiki 的设计借鉴及许可边界
│       ├── agent-extraction-contract.md 两步摄入契约与 payload/校验规则
│       ├── repository-audit.md        只读环境与库表审计结果
│       ├── migration-report.md        真实库迁移 dry-run/apply 结果与回滚说明
│       ├── quality-report.md          真实 Wiki 库质量指标基线（当前 0 页面）
│       ├── operations.md              备份/恢复/校验/质量/重建/迁移运维手册
│       └── test-report.md             Wiki V1 测试报告（495 项基线）
├── .mineru-venv/                   独立 venv：mineru 3.4.4 + transformers 4.57（Anaconda 已是 v5，不能共用）
├── tests/                          unittest 离线测试套件
│   ├── kb_test_support.py          公共设施：假 embedding、临时库、注册表
│   ├── test_kb_config.py           库路径优先级
│   ├── test_kb_ingest.py           分块、类别推断、文本抽取、来源身份与幂等替换
│   ├── test_kb_schema_search.py    schema/检索端到端（FTS/hybrid/过滤/回退）
│   ├── test_kb_model_lifecycle.py  闲置卸载逻辑 + 本地嵌入懒加载（假模型）
│   ├── test_kb_ask.py              证据检索打包（mock）
│   ├── test_kb_web.py              网页 canonical、vault 剪藏路径与幂等入库（mock HTTP）
│   ├── test_kb_watcher.py          watchdog 事件、尾沿防抖、删除/移动、多目录
│   ├── test_kb_image_search.py     图像入库计划、旧行替换、过滤转义
│   ├── test_relink_sources.py      旧 basename source 重指向：唯一匹配、行数不变、幂等与 missing
│   ├── test_wiki_config.py         WikiConfig/env 覆盖/vault 路径边界
│   ├── test_wiki_ids.py            稳定 ID、ULID、文本指纹
│   ├── test_wiki_markdown.py       frontmatter 安全解析/校验、wikilink
│   ├── test_wiki_migrations.py     迁移幂等、checksum、备份与回滚
│   ├── test_wiki_db.py             SQLite 事务/约束与 Pydantic 模型一致性
│   ├── test_wiki_document_registry.py 文档身份、修订幂等、切片引用与旧源登记
│   ├── test_wiki_entities.py       实体规范化/显式合并、关系白名单与邻域
│   ├── test_wiki_evidence.py       引文校验、证据落库与 LanceDB 原文回查
│   ├── test_wiki_links.py          出链/反链/断链、slug/title/alias 解析
│   ├── test_wiki_mcp.py            WIKI_ENABLED 开关、14 个工具注册与参数解析
│   ├── test_wiki_migrate.py        迁移脚本 dry-run 只读、apply 幂等与 limit
│   ├── test_wiki_navigation.py     index/log/purpose/overview 生成与刷新
│   ├── test_wiki_ops.py            备份 manifest、validate CLI、质量报告与元数据重建
│   ├── test_wiki_page_index.py     页面索引写入、发布过滤、幂等与 CLI dry-run
│   ├── test_wiki_page_store.py     原子写、乐观锁、外部编辑与 git 提交
│   ├── test_wiki_proposals.py      draft/diff、审核队列、approve/reject 与 stale
│   ├── test_wiki_recovery.py       journal 状态、只读扫描与 apply 收敛
│   ├── test_wiki_search.py         页面 hybrid、过滤、证据直通与图谱邻域
│   ├── test_wiki_service.py        service 门面、草稿/提案/审核、两步抽取与校验
│   └── test_wiki_validator.py      逐类问题 code/level 与只读行为
│
├── .mcp.json                       本机 MCP 配置（gitignore，示例为 .mcp.example.json）
├── .gitattributes                  统一换行策略与二进制文件属性
├── requirements-runtime.txt        运行时依赖（本地模型另需 torch/sentence-transformers）
├── README.md                       项目说明
└── FOLDER_STRUCTURE.md             本文件
```

## 数据目录（不在仓库内）

LanceDB 库、Obsidian vault 与 Wiki SQLite 都在仓库外，仓库只保留代码与文档。

- `D:\cherry-workplace\knowledge-hub\lancedb` — 当前知识库（数字以
  `docs/wiki-v1/repository-audit.md` 的只读审计为准）：
  - `my_docs`：26,583 行（218 个 source；`text_idx(text)` + `vector_idx(vector)`）
  - `image_chunks`：4,186 行（jina-clip-v2，1024 维）
  - `wiki_pages`：启用 Wiki 层并完成索引重建后才出现的派生页面索引（与 `my_docs` 同库、独立表）
  - `web_figure_chunks`：95 行，2026-10-29 遗留实验表；当前代码不引用，
    **勿删勿迁移**，后续单独评估
- `D:\cherry-workplace\knowledge-hub\vault` — Obsidian vault（**不在仓库内**，唯一向量化来源）：
  - `00-inbox-收件箱/from-web-网页/`：`ingest_url` 生成的网页剪藏 Markdown
  - `08-literature-文献/papers-论文全文/`：论文类 PDF/Office 转换产物（MinerU / docling → markitdown）
  - `raw-素材/converted-转换/`：其他二进制转换产物
  - `wiki-维基/`：Wiki 页面正文与 `ai-index.md`、`ai-log.md`、`ai-purpose.md`、`ai-overview.md`
  - `.wiki/wiki.sqlite3`：Wiki 元数据（默认 `WIKI_DB_PATH`），同级有 `proposals/`、`backups/`
- `D:\huggingface\hub` — bge-m3 / bge-reranker-v2-m3 / jina 模型缓存（HF_HOME）
- `D:\huggingface\local-models\` — jina-clip-v2 与 jina-embeddings-v3 **自包含目录**
  （权重+内联实现代码+auto_map 本地化，transformers v5 离线兼容，重建步骤见 README 图像检索节）
- `D:\lance\language_models\jieba` — jieba 分词词典（仅在 `FTS_BASE_TOKENIZER=jieba/default` 时需要；默认使用 ngram）

## 已删除的历史功能（2026-10 整改期间）

- 桌面知识浏览器（PySide6）与文档语义图谱：实用性不足，2026-10-02 删除
- GraphRAG 内容图谱（content_graph.py + 3 个 MCP 工具）：随浏览器一并删除
- Chunk 资产/generation 机制、旧 web 查看器、多库切换 shim：官方 SDK 重写时删除

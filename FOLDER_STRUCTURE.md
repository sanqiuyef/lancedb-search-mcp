# 目录结构

```
lancedb-search/
├── server.py                       MCP 薄入口（15 个工具定义）
├── kb/                             知识库核心包
│   ├── config.py                   环境变量与单库路径解析（无分区）
│   ├── embeddings.py               本地 embedding（bge-m3，官方注册表 + LRU 缓存）
│   ├── image_search.py             ★图像向量检索（jina-clip-v2 自包含加载 + image_chunks 表，2026-10-06）
│   ├── model_lifecycle.py          本地模型生命周期：用时挂载、闲置自动卸载显存
│   ├── schema.py                   LanceModel schema、建表、FTS/向量索引、库目录 README
│   ├── ingest.py                   文档解析（MinerU→文本层→OCR 回退链）、分块、增删改查
│   ├── pdf_convert.py              ★PDF→Markdown 核心（本地 MinerU pipeline，缓存+档位判定；与 scripts 批量脚本共用）
│   ├── search.py                   检索内核：vector/text/hybrid + 本地 CrossEncoder 精排（回退 RRF）
│   ├── ask.py                      检索证据打包（[n] 编号证据块，不调用生成模型）
│   ├── web.py                      [自建模块 · 待优化] 网页抓取入库（canonical source + 幂等替换）
│   ├── watcher.py                  [自建模块 · 待优化] watchdog 6.0 目录监听（多目录、删除/移动同步、防抖）
│   └── wiki/                       Wiki V1 元数据底座（Phase 1）
│       ├── config.py               WIKI_* 配置与 vault 路径边界校验
│       ├── constants.py            默认路径、页面类型、表名等常量
│       ├── db.py                   SQLite 连接、事务与迁移器（checksum + 备份）
│       ├── ids.py                  document/chunk ID、ULID、内容指纹
│       ├── markdown.py             frontmatter 安全解析/校验 + wikilink
│       ├── models.py               Pydantic v2 记录模型与 StrEnum
│       ├── migrations/             0001_core ~ 0004_jobs_journal SQL 迁移
│       └── repositories/           [Phase 2 规划中] 仓储层（当前仅占位 __init__.py）
│
├── scripts/
│   ├── rebuild_from_sources.py     批量重建脚本（攒批 + checkpoint 断点续传）
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
│   ├── audit_wiki_environment.py   Wiki V1 只读环境/库表审计（生成 docs/wiki-v1/repository-audit.md）
│   └── migrate_wiki_v1.py          [Phase 2 规划中] Wiki V1 迁移入口（尚未加入仓库）
├── docs/
│   └── wiki-v1/
│       └── repository-audit.md     Wiki V1 审计报告（运行环境、表行数、source 形态、vault）
├── .mineru-venv/                   独立 venv：mineru 3.4.4 + transformers 4.57（Anaconda 已是 v5，不能共用）
├── tests/                          unittest 离线测试套件
│   ├── kb_test_support.py          公共设施：假 embedding、临时库、注册表
│   ├── test_kb_config.py           库路径优先级
│   ├── test_kb_ingest.py           分块、类别推断、文本抽取、来源身份与幂等替换
│   ├── test_kb_schema_search.py    schema/检索端到端（FTS/hybrid/过滤/回退）
│   ├── test_kb_model_lifecycle.py  闲置卸载逻辑 + 本地嵌入懒加载（假模型）
│   ├── test_kb_ask.py              证据检索打包（mock）
│   ├── test_kb_web.py              网页 canonical/幂等与正文解析（mock HTTP）
│   ├── test_kb_watcher.py          watchdog 事件、尾沿防抖、删除/移动、多目录
│   ├── test_kb_image_search.py     图像入库计划、旧行替换、过滤转义
│   ├── test_wiki_config.py         WikiConfig/env 覆盖/vault 路径边界
│   ├── test_wiki_ids.py            稳定 ID、ULID、文本指纹
│   ├── test_wiki_markdown.py       frontmatter 安全解析/校验、wikilink
│   ├── test_wiki_migrations.py     迁移幂等、checksum、备份与回滚
│   └── test_wiki_db.py             SQLite 事务/约束与 Pydantic 模型一致性
│
├── .mcp.json                       本机 MCP 配置（gitignore，示例为 .mcp.example.json）
├── .gitattributes                  统一换行策略与二进制文件属性
├── requirements-runtime.txt        运行时依赖（本地模型另需 torch/sentence-transformers）
├── README.md                       项目说明
└── FOLDER_STRUCTURE.md             本文件
```

## 数据目录（不在仓库内）

- `D:\cherry-workplace\knowledge-hub\lancedb` — 当前知识库（数字以
  `docs/wiki-v1/repository-audit.md` 的只读审计为准）：
  - `my_docs`：26,583 行（218 个 source；`text_idx(text)` + `vector_idx(vector)`）
  - `image_chunks`：4,186 行（jina-clip-v2，1024 维）
  - `web_figure_chunks`：95 行，2026-10-29 遗留实验表；当前代码不引用，
    **勿删勿迁移**，后续单独评估
- `D:\huggingface\hub` — bge-m3 / bge-reranker-v2-m3 / jina 模型缓存（HF_HOME）
- `D:\huggingface\local-models\` — jina-clip-v2 与 jina-embeddings-v3 **自包含目录**
  （权重+内联实现代码+auto_map 本地化，transformers v5 离线兼容，重建步骤见 README 图像检索节）
- `D:\lance\language_models\jieba` — jieba 分词词典（LANCE_LANGUAGE_MODEL_HOME）

## 已删除的历史功能（2026-10 整改期间）

- 桌面知识浏览器（PySide6）与文档语义图谱：实用性不足，2026-10-02 删除
- GraphRAG 内容图谱（content_graph.py + 3 个 MCP 工具）：随浏览器一并删除
- Chunk 资产/generation 机制、旧 web 查看器、多库切换 shim：官方 SDK 重写时删除

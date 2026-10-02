# 目录结构

```
lancedb-search/
├── server.py                       MCP 薄入口（17 个工具定义 + 桌面兼容检索入口）
├── kb_config.py                    环境变量、单库路径解析、kb-config.json 分区注册表
├── kb_embeddings.py                SiliconFlow/本地 embedding（官方注册表 + LRU 缓存）
├── kb_schema.py                    LanceModel schema、建表、FTS/向量索引、库目录 README
├── kb_ingest.py                    文档解析（OCR 链）、分块、增删改查
├── kb_search.py                    官方 hybrid 检索 + SiliconFlowReranker（回退 RRF）
├── kb_ask.py                       RAG 问答（SiliconFlow chat + 编号引用）
│
├── kb_web.py                       [自建模块 · 待优化] 网页抓取入库
├── kb_watcher.py                   [自建模块 · 待优化] watchdog 目录监听
├── content_graph.py                [自建模块 · 待优化] GraphRAG 内容图谱（SQLite 侧车）
├── knowledge_browser_core.py       [自建模块 · 待优化] 桌面浏览器支撑层
├── knowledge_graph.py              [自建模块 · 待优化] 文档级语义图谱构建
├── knowledge_graph_desktop.py      [自建模块 · 待优化] PySide6 桌面知识浏览器
├── mineru_pdf_splitter.py          [自建 OCR 链路] MinerU 大 PDF 预切分工具
│
├── scripts/
│   └── rebuild_from_sources.py     新库重建迁移脚本（checkpoint 断点续传）
├── tests/                          unittest 离线测试套件（44 个）
│   ├── kb_test_support.py          公共设施：假 embedding、临时库、注册表
│   ├── test_kb_config.py           分区规范化、库路径优先级
│   ├── test_kb_ingest.py           分块、类别推断、文本抽取
│   ├── test_kb_schema_search.py    schema/检索端到端（FTS/hybrid/过滤/回退）
│   ├── test_kb_ask.py              问答（mock HTTP）
│   ├── test_kb_web.py              网页解析（mock HTTP）
│   └── test_content_graph.py       图谱抽取与检索
│
├── kb-config.json                  本机分区注册表（gitignore，示例为 kb-config.example.json）
├── .mcp.json                       本机 MCP 配置（gitignore，示例为 .mcp.example.json）
├── content_graph.sqlite3           内容图谱侧车（gitignore，运行时生成）
├── knowledge_graph_relations.sqlite3  手动关系侧车（gitignore）
├── rebuild_state.json              迁移 checkpoint（运行时生成）
├── requirements-runtime.txt        MCP 运行时依赖
├── requirements-browser-build.txt  桌面端打包依赖
├── build_knowledge_browser.ps1     PyInstaller 打包脚本
├── build/ dist/                    打包工件（gitignore）
├── README.md                       项目说明
└── FOLDER_STRUCTURE.md             本文件
```

## 数据目录（不在仓库内）

- 知识库当前**完全为空**（2026-10-02 用户决定清空全部向量数据）；首次入库时自动重建 `knowledge_v2`
- `D:\cherry-workplace\旧库文本备份-20261002.zip` — 旧库 chunk 纯文本备份（非向量，不需要可删）
- `D:\lance\language_models\jieba` — jieba 分词词典（LANCE_LANGUAGE_MODEL_HOME）
- `D:\huggingface` — HF 模型缓存（HF_HOME）

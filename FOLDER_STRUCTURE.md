# 目录结构

```
lancedb-search/
├── server.py                       MCP 薄入口（14 个工具定义）
├── kb_config.py                    环境变量、单库路径解析、kb-config.json 分区注册表
├── kb_embeddings.py                SiliconFlow/本地 embedding（官方注册表 + LRU 缓存）
├── kb_schema.py                    LanceModel schema、建表、FTS/向量索引、库目录 README
├── kb_ingest.py                    文档解析（OCR 链）、分块、增删改查
├── kb_search.py                    官方 hybrid 检索 + SiliconFlowReranker（回退 RRF）
├── kb_ask.py                       RAG 问答（chat + 编号引用）
│
├── kb_web.py                       [自建模块 · 待优化] 网页抓取入库
├── kb_watcher.py                   [自建模块 · 待优化] watchdog 目录监听
├── mineru_pdf_splitter.py          [自建 OCR 链路] MinerU 大 PDF 预切分工具
│
├── scripts/
│   └── rebuild_from_sources.py     批量重建脚本（攒批 + checkpoint 断点续传）
├── tests/                          unittest 离线测试套件
│   ├── kb_test_support.py          公共设施：假 embedding、临时库、注册表
│   ├── test_kb_config.py           分区规范化、库路径优先级
│   ├── test_kb_ingest.py           分块、类别推断、文本抽取
│   ├── test_kb_schema_search.py    schema/检索端到端（FTS/hybrid/过滤/回退）
│   ├── test_kb_ask.py              问答（mock HTTP）
│   └── test_kb_web.py              网页解析（mock HTTP）
│
├── kb-config.json                  本机分区注册表（gitignore，示例为 kb-config.example.json）
├── .mcp.json                       本机 MCP 配置（gitignore，示例为 .mcp.example.json）
├── requirements-runtime.txt        运行时依赖（本地模型另需 torch/sentence-transformers）
├── README.md                       项目说明
└── FOLDER_STRUCTURE.md             本文件
```

## 数据目录（不在仓库内）

- 知识库当前**完全为空**（2026-10-02 用户决定清空全部向量数据）；首次入库时自动重建 `knowledge_v2`
- `D:\huggingface\hub` — bge-m3 / bge-reranker-v2-m3 模型缓存（HF_HOME）
- `D:\lance\language_models\jieba` — jieba 分词词典（LANCE_LANGUAGE_MODEL_HOME）

## 已删除的历史功能（2026-10 整改期间）

- 桌面知识浏览器（PySide6）与文档语义图谱：实用性不足，2026-10-02 删除
- GraphRAG 内容图谱（content_graph.py + 3 个 MCP 工具）：随浏览器一并删除
- Chunk 资产/generation 机制、旧 web 查看器、多库切换 shim：官方 SDK 重写时删除

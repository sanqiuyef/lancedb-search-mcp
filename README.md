# LanceDB Search MCP

面向 AI 工作流的本地文档知识库检索服务。内核完全基于 **lancedb 0.39 官方 SDK**：
LanceModel schema、官方 embedding 注册表、原生 BM25 全文检索（jieba 中文分词）、
官方 hybrid 混合检索与 Reranker 接口。

> 2026-10 全面整改：旧版手写 RRF/混合检索/embedding 调度全部替换为官方 API。
> 工具面从 26 个精简为 14 个。知识库数据按用户决定**全部清空**
> （旧库与新库均已删除，从零开始按需入库）。
> 桌面浏览器与知识图谱功能已于 2026-10-02 删除（实用性不足）。

## 主要能力

- **官方 hybrid 检索**：`query_type="hybrid"` 向量 + BM25 融合，RRF 或 SiliconFlow 精排；
- **本地模型**（当前后端）：BAAI/bge-m3 嵌入 + bge-reranker-v2-m3 精排，RTX 4060 实测通过，
  零 API 费用；**用时挂载、闲置自动卸载显存**（`LOCAL_MODEL_IDLE_UNLOAD`，默认 300s，
  0=常驻，对齐 Ollama keep-alive）；可经 `EMBEDDING_BACKEND=api` 切回 SiliconFlow Qwen3 组合；
- **原生 FTS**：Lance 原生 BM25 倒排索引，jieba 分词（词典在 `LANCE_LANGUAGE_MODEL_HOME`）；
- **RAG 问答**：ask_knowledge 带编号引用作答；
- **OCR 链路**：PDF 文本层 → MinerU API → 本地 Tesseract 回退；
- **网页抓取 + 目录监听**自动重索引（自建）。

## MCP 工具（14 个）

| 类别 | 工具 |
|---|---|
| 检索问答 | search_knowledge、ask_knowledge、search_similar、get_document |
| 知识库管理 | get_knowledge_status、list_documents、list_knowledge_bases、add_documents、add_single_document、update_document、delete_documents |
| 自建能力 | ingest_url、start_watcher、stop_watcher |

已移除：switch_knowledge_base（legacy shim）、资产/generation 机制 7 工具、
generate_index、extract_to_note、内容图谱 3 工具（build/get/search_content_graph）。

## 自建模块清单（待后期单独优化）

以下模块为自建实现，文件头部有 `[自建模块 · 待后期单独优化]` 标记：

| 模块 | 说明 | 优化方向 |
|---|---|---|
| `kb_web.py` | 网页抓取入库 | 反爬重试、正文智能抽取、与 web-search-server 抓取链复用 |
| `kb_watcher.py` | watchdog 目录监听 | 多目录注册、删除事件同步、事件队列 |
| `mineru_pdf_splitter.py` | MinerU 大 PDF 预切分 | 按需使用，独立工具 |

## 架构（整改后）

```
server.py                MCP 薄入口（14 工具）
kb/                      知识库核心包
  config.py              环境变量 + kb-config.json 分区注册表（热重载）
  embeddings.py          SiliconFlow/本地 embedding（官方注册表 + LRU 缓存）
  model_lifecycle.py     本地模型生命周期：用时挂载、闲置自动卸载（gc + empty_cache）
  schema.py              LanceModel schema、建表、FTS/向量索引维护、库目录 README
  ingest.py              解析（含 OCR）、分块（800/100）、增删改查
  search.py              官方 hybrid + SiliconFlow/本地 CrossEncoder 精排（失败回退 RRF）
  ask.py                 RAG 问答
  web.py / watcher.py    [自建模块] 网页入库 / 目录监听
scripts/                 rebuild_from_sources.py（批量重建）+ 冒烟脚本 + mineru_pdf_splitter.py（OCR 工具）
```

单库分区模式：所有项目分区共存于同一个 LanceDB 库，`project` 列过滤，
`kb-config.json` 维护 分区名 → 源文件目录 映射。库路径优先级：
`LANCEDB_DB_PATH 环境变量 > kb-config.json 的 db_path > 默认 knowledge_v2`。

## 数据状态

- **知识库当前完全为空**（2026-10-02 用户决定清空全部向量数据：旧库与 knowledge_v2 均已删除）；
- 按需入库：MCP 工具 `add_documents`（扫目录）/ `add_single_document`（单文件）；
- 批量重建：`D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py`
  （按 kb-config 分区源目录重建，支持 `--dry-run`、`--project`、`--limit-files`，checkpoint 断点续传）。

## 安全约定

仓库不提交：API Key、`.mcp.json`、`kb-config.json`（本机路径）、LanceDB 数据、
日志、虚拟环境。首次使用复制
`.mcp.example.json` → `.mcp.json`、`kb-config.example.json` → `kb-config.json` 并按本机修改。

## 源码运行

```powershell
python -m pip install -r requirements-runtime.txt   # 本地后端另需 torch/sentence-transformers（已装于 Anaconda）
python -X utf8 server.py                            # MCP 服务
```

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

离线测试（假 embedding、mock HTTP），覆盖配置、分块、schema/检索端到端、
reranker 回退、问答、网页解析。

# LanceDB Search MCP

面向 AI 工作流的**全本地**文档知识库检索服务（MCP Server）。内核基于
[lancedb](https://github.com/lancedb/lancedb) 官方 SDK：LanceModel schema、官方 embedding
注册表、原生 BM25 全文检索（jieba 中文分词）、官方 hybrid 混合检索与 Reranker 接口。

- **零云端依赖**：嵌入（bge-m3）与精排（bge-reranker-v2-m3）都在本机 GPU 跑，用时挂载、
  闲置自动卸载显存，不调用任何外部 API、数据不出本机；
- **公式级 PDF 解析**：PDF 入库优先走本地 [MinerU](https://github.com/opendatalab/MinerU)
  pipeline，把论文里的公式转成 **LaTeX** 后再入库 —— 而不是纯文本抽取器那种
  `(cid:2)`、字母打散、上下标丢失的坏结果（见「PDF 解析」一节）；
- **混合检索 + 精排**：向量 + BM25 融合，CrossEncoder 精排，带编号引用的 RAG 问答；
- **13 个 MCP 工具**：检索 / 问答 / 增删改查 / 网页抓取入库 / 目录监听自动重索引；
- **零插件检索面板**：可选的本地面板（`ui_server.py`，127.0.0.1:8002），供人直接查询。

## 主要能力

- **官方 hybrid 检索**：`query_type="hybrid"` 向量 + BM25 融合，本地 CrossEncoder 精排（失败回退 RRF）；
- **全本地模型**：BAAI/bge-m3 嵌入 + bge-reranker-v2-m3 精排，模型缓存目录由 `HF_HOME`
  指定；**用时挂载、闲置自动卸载显存**（`LOCAL_MODEL_IDLE_UNLOAD`，默认 300s，0=常驻，
  对齐 Ollama keep-alive）；
- **原生 FTS**：Lance 原生 BM25 倒排索引，jieba 分词（词典在 `LANCE_LANGUAGE_MODEL_HOME`）；
- **RAG 问答**：ask_knowledge 带编号引用作答；
- **PDF → Markdown（含 LaTeX 公式）**：本地 MinerU pipeline 优先，失败回退
  PyMuPDF/pdfminer 文本层，扫描件再回退 OCR 链（云 MinerU API → 本地 Tesseract）；
- **网页抓取 + 目录监听**自动重索引（自建）。

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
#    add_documents(scan_dir="D:\your\docs")        目录批量入库
#    add_single_document(filepath="...")           单文件入库
#    search_knowledge(query="...", search_mode="hybrid")
```

首次检索会自动下载嵌入模型（约 2GB，可用 `HF_ENDPOINT=https://hf-mirror.com` 走镜像）。
`LANCEDB_DB_PATH` 不设时默认建在 `D:\cherry-workplace\knowledge-hub\lancedb`
（作者机器布局），**建议显式设置成你自己的路径**。

### 启用 PDF 公式解析（可选增强）

不做这一步也能用：PDF 会走 PyMuPDF/pdfminer 文本层抽取（公式会被压平）。

```powershell
# MinerU 本地 pipeline 需要独立 venv（其 transformers 版本要求与主环境常冲突）
python -m venv --system-site-packages .mineru-venv
.\.mineru-venv\Scripts\python -m pip install "mineru[pipeline]==3.4.4" "transformers>=4.57,<5"
mineru-models-download -s modelscope -m pipeline      # 首次下载模型（约 2.5GB）
```

之后 PDF 入库会自动调用它（转换产物按 路径+大小+mtime 缓存在 `<库目录>/_pdf_md_cache/`）：
文本层含乱码/数学字母 → 自动切 OCR 模式，中文论文同样自动识别。
可用 `LANCEDB_MINERU=0` 关闭、`LANCEDB_MINERU_BIN` 指定可执行文件路径。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LANCEDB_DB_PATH` | `D:\cherry-workplace\knowledge-hub\lancedb` | LanceDB 库目录（**建议显式设置**） |
| `LOCAL_EMBED_MODEL` / `LOCAL_EMBED_DIM` | `BAAI/bge-m3` / `1024` | 嵌入模型与维度 |
| `LOCAL_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 精排模型 |
| `LOCAL_MODEL_DEVICE` | `auto` | `cuda` / `cpu` / `auto` |
| `LOCAL_MODEL_IDLE_UNLOAD` | `300` | 闲置卸载显存秒数，0=常驻 |
| `HF_HOME` / `HF_HUB_CACHE` | 系统默认 | 模型缓存目录 |
| `LANCE_LANGUAGE_MODEL_HOME` | `D:\lance\language_models` | jieba 分词词典目录（FTS 用） |
| `LANCEDB_MINERU` | `1` | 是否启用本地 MinerU 转换（0 关闭） |
| `LANCEDB_MINERU_BIN` / `_CACHE` / `_STAGING` / `_TIMEOUT` | 见 `kb/config.py` | MinerU 可执行文件 / 缓存目录 / 短路径暂存 / 超时 |
| `LANCEDB_OCR` | `0` | 是否启用扫描件 OCR 回退链 |
| `TESSERACT_CMD` / `LANCEDB_OCR_LANG` | 见 `kb/config.py` | Tesseract 路径与语言包 |
| `MINERU_API_KEY` | 空 | 云 MinerU API（可选，仅扫描件回退链用） |

## MCP 工具（13 个）

| 类别 | 工具 |
|---|---|
| 检索问答 | search_knowledge、ask_knowledge、search_similar、get_document |
| 知识库管理 | get_knowledge_status、list_documents、add_documents、add_single_document、update_document、delete_documents |
| 自建能力 | ingest_url、start_watcher、stop_watcher |

## 自建模块清单（待后期单独优化）

以下模块为自建实现，文件头部有 `[自建模块 · 待后期单独优化]` 标记：

| 模块 | 说明 | 优化方向 |
|---|---|---|
| `kb_web.py` | 网页抓取入库 | 反爬重试、正文智能抽取、与 web-search-server 抓取链复用 |
| `kb_watcher.py` | watchdog 目录监听 | 多目录注册、删除事件同步、事件队列 |
| `mineru_pdf_splitter.py` | MinerU 大 PDF 预切分 | 按需使用，独立工具 |

## 架构

```
server.py                MCP 薄入口（13 工具）
kb/                      知识库核心包
  config.py              环境变量 + 单库路径解析（无分区）
  embeddings.py          本地 embedding（bge-m3，官方注册表 + LRU 缓存）
  model_lifecycle.py     本地模型生命周期：用时挂载、闲置自动卸载（gc + empty_cache）
  schema.py              LanceModel schema、建表、FTS/向量索引维护、库目录 README
  pdf_convert.py         ★PDF→Markdown（本地 MinerU pipeline；公式→LaTeX，缓存+档位判定）
  ingest.py              解析（MinerU→文本层→OCR 回退链）、分块（800/100）、增删改查
  search.py              官方 hybrid + 本地 CrossEncoder 精排（失败回退 RRF）
  ask.py                 RAG 问答
  web.py / watcher.py    [自建模块] 网页入库 / 目录监听
scripts/                 rebuild_from_sources.py（批量重建）+ mineru_to_markdown.py（批量 PDF→md）
                         + pdf_to_markdown.py（docling 备选）+ 冒烟脚本 + mineru_pdf_splitter.py
ui_server.py / ui/       检索面板（零插件版，HTTP 8002，复用 kb.search）+ run_ui.cmd 启动器
```

### PDF 解析（2026-10-04 起：本地 MinerU 管线）

PDF 入库优先调用本地 MinerU（`D:\cherry-workplace\mcp_servers\lancedb-search\.mineru-venv`，
mineru 3.4.4 + transformers 4.57；Anaconda 的 transformers v5 与其不兼容）把 PDF 转成
**含 LaTeX 公式的 Markdown**，走标题感知分块；失败回退 PyMuPDF/pdfminer 文本层，扫描件再回退
OCR 链（云 MinerU → Tesseract）。转换产物按 `路径+大小+mtime` 缓存在 `<库>/_pdf_md_cache/`。

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

- **按需入库**：MCP 工具 `add_documents`（扫目录）/ `add_single_document`（单文件，
  PDF 自动走 MinerU → LaTeX）；
- **批量重建**：`python -X utf8 scripts/rebuild_from_sources.py <源目录>`
  （支持 `--dry-run`、`--limit-files`，checkpoint 断点续传，自动按 `doc_id` 去重）；
- **批量 PDF → Markdown**：`python -X utf8 scripts/mineru_to_markdown.py <PDF目录> <输出目录>
  --workers 3`（每篇产出 `<名>.md` + `<名>_images/`，可断点续跑）；
  备选引擎 docling 见 `scripts/pdf_to_markdown.py`；
- **实测规模参考**（作者使用场景）：116 篇论文（2205 页）→ 18k 切片，
  3 并行转换约 1.5 小时（RTX 4060），重建入库约 12 分钟。

## 检索面板（零插件版，人用）

`ui_server.py` + `ui/index.html`：只绑 `127.0.0.1:8002` 的本地检索面板，检索内核复用
`kb.search.search_structured`（混合检索 + 本地精排），供 Obsidian（核心 Web Viewer）或
任意浏览器内嵌使用，不依赖 MCP 与任何 Obsidian 插件。

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

离线测试（假 embedding、mock HTTP），覆盖配置、分块、schema/检索端到端、
reranker 回退、问答、网页解析。

## License

[MIT](LICENSE) © 2026 sanqiuyef

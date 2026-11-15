# LanceDB Search MCP

面向 AI 工作流的**本地优先**文档知识库检索服务（MCP Server）。内核基于
[lancedb](https://github.com/lancedb/lancedb) 官方 SDK：LanceModel schema、官方 embedding
注册表、原生 BM25 全文检索（jieba 中文分词）、官方 hybrid 混合检索与 Reranker 接口。

- **嵌入/精排全本地**：嵌入（bge-m3）与精排（bge-reranker-v2-m3）都在本机 GPU 跑，用时挂载、
  闲置自动卸载显存，不调用云端推理 API；仅扫描件 OCR 可选启用云 MinerU 回退
  （`MINERU_API_KEY`，默认关闭，显式配置后该回退会把文件发往云端）；
- **公式级 PDF 解析**：PDF 入库优先走本地 [MinerU](https://github.com/opendatalab/MinerU)
  pipeline，把论文里的公式转成 **LaTeX** 后再入库 —— 而不是纯文本抽取器那种
  `(cid:2)`、字母打散、上下标丢失的坏结果（见「PDF 解析」一节）；
- **混合检索 + 精排**：向量 + BM25 融合，CrossEncoder 精排；`ask_knowledge` 检索并打包
  编号证据块，作答由调用方模型完成；
- **15 个 MCP 工具**：检索 / 证据打包 / 增删改查 / 网页抓取入库 / 目录监听自动重索引 /
  图像检索；
- **零插件检索面板**：可选的本地面板（`ui_server.py`，127.0.0.1:8002），供人直接查询。

## 主要能力

- **检索模式**：`search_knowledge` 默认 `search_mode="vector"`；显式选 `hybrid` 时走向量 +
  BM25 融合，且只有 `use_reranker=True` 才加载本地 CrossEncoder 精排（失败回退 RRF）；
  `vector` 与 `text` 分支不做精排；
- **全本地模型**：BAAI/bge-m3 嵌入 + bge-reranker-v2-m3 精排，模型缓存目录由 `HF_HOME`
  指定；**用时挂载、闲置自动卸载显存**（`LOCAL_MODEL_IDLE_UNLOAD`，默认 300s，0=常驻，
  对齐 Ollama keep-alive）；
- **原生 FTS**：Lance 原生 BM25 倒排索引，jieba 分词（词典在 `LANCE_LANGUAGE_MODEL_HOME`）；
- **证据打包（非生成式问答）**：`ask_knowledge` 检索并打包 `[n]` 编号证据块（来源 + 全文
  chunk），不调用任何生成模型；作答与引用标注由 MCP 宿主模型基于证据完成；
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

## MCP 工具（15 个）

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
- `add_single_document`：不传 `scan_dir` 时 source 退化为文件名；若库中已有同名 source
  会报错拒绝覆盖，请传 `scan_dir`（按相对路径入库）或改用 `update_document`。

### 网页入库

- source 形如 `网页-<host>-<sha1(canonical)[:12]>`；canonical 只做 scheme/host 小写、
  去默认端口、去 fragment、清 `utm_*` / `fbclid` / `gclid`，**不折叠尾斜杠**，query
  参数不排序。
- 同一 canonical URL 重复抓取时先补写新块，成功后再删除不在新 `doc_id` 集合中的旧块；
  内容未变的块不重复写入。

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

## 自建模块清单（待后期单独优化）

以下模块为自建实现，文件头部有 `[自建模块 · 待后期单独优化]` 标记：

| 模块 | 说明 | 优化方向 |
|---|---|---|
| `kb/web.py` | 网页抓取入库（canonical source + 幂等替换） | 反爬重试、正文智能抽取、与 web-search-server 抓取链复用 |
| `kb/watcher.py` | watchdog 6.0 目录监听（多目录、删除/移动同步、5 秒尾沿防抖） | 整目录移入/移出的递归迁移兜底、事件队列 |
| `mineru_pdf_splitter.py` | MinerU 大 PDF 预切分 | 按需使用，独立工具 |

## 架构

```
server.py                MCP 薄入口（15 工具）
kb/                      知识库核心包
  config.py              环境变量 + 单库路径解析（无分区）
  embeddings.py          本地 embedding（bge-m3，官方注册表 + LRU 缓存）
  image_search.py        ★图像向量检索（jina-clip-v2 自包含加载 + image_chunks 表）
  model_lifecycle.py     本地模型生命周期：用时挂载、闲置自动卸载（gc + empty_cache）
  schema.py              LanceModel schema、建表、FTS/向量索引维护、库目录 README
  pdf_convert.py         ★PDF→Markdown（本地 MinerU pipeline；公式→LaTeX，缓存+档位判定）
  ingest.py              解析（MinerU→文本层→OCR 回退链）、分块（800/100）、增删改查
  search.py              官方 hybrid + 本地 CrossEncoder 精排（失败回退 RRF）
  ask.py                 检索证据打包（[n] 编号证据块，不调用生成模型）
  web.py / watcher.py    [自建模块] 网页入库 / 目录监听（watchdog 6.0）
  wiki/                  Wiki V1 元数据底座（Phase 1：配置、SQLite 迁移、稳定 ID、
                         frontmatter；repositories 为 Phase 2 规划中）
scripts/                 rebuild_from_sources.py（批量重建）+ mineru_to_markdown.py（批量 PDF→md）
                         + pdf_to_markdown.py（docling 备选）+ 冒烟脚本 + mineru_pdf_splitter.py
                         + audit_wiki_environment.py（只读环境/库表审计）
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

- **一条命令走完（推荐）**：`python -X utf8 scripts/pdf_pipeline.py <PDF目录>
  [--md-dir <md输出>] [--workers 3] [--resume] [--no-ingest] [--dry-run]`
  —— 阶段 1 PDF → Markdown（MinerU 并行转换、带缓存、可断点），阶段 2 Markdown → 向量库
  （标题分块 + 预计算向量 + 按 doc_id 去重 + 索引维护）。MinerU 不可用时直接报错退出，
  不做静默降级（避免把压平公式入进库）；
- **按需入库**：MCP 工具 `add_documents`（扫目录）/ `add_single_document`（单文件，
  PDF 自动走 MinerU → LaTeX）；两者都支持 `export_markdown_dir` 参数，把转换出的
  Markdown + 图片同步导出留档（可再用于 Obsidian 等阅读端）；
- **只转换不入库**：`python -X utf8 scripts/mineru_to_markdown.py <PDF目录> <md目录>
  --workers 3`（每篇产出 `<名>.md` + `<名>_images/`，可断点续跑）；
  备选引擎 docling 见 `scripts/pdf_to_markdown.py`；
- **重建/更新**：`python -X utf8 scripts/rebuild_from_sources.py <源目录>`
  （`--dry-run`、`--limit-files`，checkpoint 断点续传，自动按 `doc_id` 去重）；
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

离线测试（假 embedding、mock HTTP），覆盖配置、分块、schema/检索端到端、reranker 回退、
证据打包、网页 canonical/幂等、目录监听（防抖/删除/多目录）、图像入库计划与替换、
Wiki 元数据（配置/ID/frontmatter/SQLite 迁移）。

## License

代码 [MIT](LICENSE) © 2026 sanqiuyef。

模型权重遵循各自许可，与代码许可相互独立：

- `jinaai/jina-clip-v2`：CC-BY-NC-4.0，仅限个人学术用途；
- `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`：仓库内未声明其许可，使用前请核对
  各自模型卡。
- MinerU 及其模型、Tesseract 语言包等第三方组件同样遵循各自许可。

# LanceDB 知识浏览器

## 定位

这个程序不是一个独立笔记软件，而是现有 LanceDB 向量知识库的可视化读取层。AI 仍然通过 `search_knowledge` MCP 搜索文件块；人通过本程序查看数据库内容、搜索排名原因、原件状态和人工关系。

浏览器不改 LanceDB 表、向量、Embedding 或源文件。派生数据分别写入独立的
`knowledge_graph_relations.sqlite3`（人工关系）和 `content_graph.sqlite3`（内容图谱）。

## 使用

1. 源码运行：`python -X utf8 knowledge_graph_desktop.py`。
2. 左侧选择知识库，用文件名、类别和扩展名过滤。
3. 中间的“全文”按 `chunk_index` 拼接；“Chunk”页可上下切换、复制和定位。
4. 顶部搜索可选 Vector/Text/Hybrid 和 Reranker。右侧保留向量排名、距离、FTS 排名、RRF、类别提升、重排和最终排名。
5. Reranker 超时或失败时，界面和 MCP 都会显示“已回退”，结果按召回顺序继续可用。
6. “人工关系”支持 `related` / `supports` / `contradicts` / `depends_on` / `supersedes`，可选绑定当前 Chunk 作为证据。
7. “文档关系图（旧）”只在打开标签时读取向量，保留用于查看文件平均向量关系和回滚。
8. “内容图谱（实体/观点）”针对当前文件按需构建 Document、Chunk、Entity、Method、Claim、Topic 和 Dataset 节点；点击关系可查看证据并跳回具体 Chunk。

## 内容图谱

内容图谱使用内容 Hash 作为文档身份，来源路径单独记录。同一内容移动位置后会复用已有抽取；同一路径内容变化时会原子切换到新文档。切换 Embedding 模型不会删除实体、观点、证据关系或人工关系。

默认使用不联网的 `heuristic-v2` 抽取器，适合零成本预览结构，但结果会标记为“待模型或人工确认”。正式抽取可连接任意 OpenAI-compatible Chat Completions 接口：

```powershell
$env:CONTENT_GRAPH_EXTRACTOR = "openai-compatible"
$env:CONTENT_GRAPH_LLM_URL = "https://example.com/v1/chat/completions"
$env:CONTENT_GRAPH_LLM_MODEL = "your-model"
$env:CONTENT_GRAPH_LLM_API_KEY = "从环境变量提供，不写入仓库"
$env:CONTENT_GRAPH_LLM_TIMEOUT = "120"
$env:CONTENT_GRAPH_BATCH_CHARS = "16000"
```

只有主动点击“构建/增量更新当前文件”或调用构建 MCP 工具时才会调用模型。读取、搜索和打开图谱不会产生模型费用。

新增 MCP 工具：

- `build_content_graph`：构建或增量更新一个文档；
- `get_content_graph`：读取文档内容图谱与证据；
- `search_content_graph`：搜索实体、方法、观点、主题和数据集；
- `get_content_graph_stats`：查看派生图谱规模。

性能策略：后台任务由窗口持有，避免 Qt 任务对象提前释放；快速切换文档时取消尚未开始的旧 Chunk 任务；全文、索引健康和知识图谱均按需加载。窗口版运行异常会写入程序目录下的 `knowledge_browser.log`。

`kb-config.json` 的 `source_roots` 用于将 LanceDB 中的相对来源路径映射到原文件。原件缺失或同名歧义都不影响阅读数据库文本。

## MinerU 超过 200 页的 PDF

`mineru_pdf_splitter.py` 先在本地拆 PDF，不调用 MinerU API：

```powershell
python -X utf8 mineru_pdf_splitter.py "D:\docs\large.pdf" "D:\docs\large-mineru-parts"
```

例如 401 页会生成 3 段：1–200、201–400、401。程序还会检查每段是否超过默认 200 MB；超过时继续二分，直到同时满足页数和文件大小限制。同时生成 `*.mineru-split-manifest.json`，记录源文件 SHA-256、每段页码、大小、`page_offset` 和处理状态。后续接 MinerU 精准解析 API 时，应按清单上传、失败重试，再用 `page_offset` 还原全文页码。

拆分解决的是 API 页数上限，不会自动解决跨分段的表格/章节语义。后续合并时应对分段边界做告警，并避免图片资产重名。MinerU Token 只允许从 `MINERU_API_TOKEN` 环境变量读取，不写入代码、配置或日志。

## 验证与打包

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 knowledge_graph_desktop.py --smoke-test `
  --relations "$env:TEMP\kb-browser-smoke.sqlite3" `
  --content-graph "$env:TEMP\content-graph-smoke.sqlite3"
.\build_knowledge_browser.ps1 -VenvPath ".\.build-venv-clean" -SkipInstall
```

新产物为 `dist\KnowledgeBrowser\KnowledgeBrowser.exe`；旧 `dist\KnowledgeGraph\KnowledgeGraph.exe` 保留用于回滚。只有新 EXE smoke test 退出码为 0 后，才应调整默认启动脚本。

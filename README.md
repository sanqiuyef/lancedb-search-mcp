# LanceDB Search MCP

面向 AI 工作流的本地 LanceDB 文档检索、搜索解释与知识可视化工具。项目同时提供 MCP 服务和原生 PySide6“LanceDB 知识浏览器”。

## 主要能力

- Vector、Text、Hybrid 检索，以及可追踪的 RRF、类别提升和 Reranker 流水线；
- 按来源精确读取文件全文与 Chunk，不依赖模糊文件名匹配；
- 原生桌面端浏览知识库、原件状态、搜索解释和索引健康；
- 文档级语义关系图，以及带 Chunk 证据的实体、方法、观点和主题图谱；
- 独立 SQLite 侧车库存储人工关系与内容图谱，不修改 LanceDB 向量和源文件；
- MinerU 超过 200 页 PDF 的本地拆分与页码偏移清单。

详细使用说明见 [KNOWLEDGE_BROWSER.md](KNOWLEDGE_BROWSER.md)。

## 安全约定

仓库不会提交以下本机数据：

- API Key、Token、`.mcp.json` 和 `.env`；
- `kb-config.json` 中的本机路径；
- LanceDB 数据、SQLite 侧车库、日志、虚拟环境和打包产物。

首次使用时复制示例配置：

```powershell
Copy-Item .mcp.example.json .mcp.json
Copy-Item kb-config.example.json kb-config.json
```

然后按本机环境修改路径，并通过环境变量或本地 MCP 配置提供密钥。

## 源码运行

建议在独立 Python 环境中安装依赖：

```powershell
python -m pip install -r requirements-browser-build.txt
python -X utf8 knowledge_graph_desktop.py
```

启动 MCP 服务：

```powershell
python -X utf8 server.py
```

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

## 打包

```powershell
.\build_knowledge_browser.ps1 -VenvPath ".\.build-venv-clean" -SkipInstall
```

构建脚本会生成 `dist\KnowledgeBrowser\KnowledgeBrowser.exe`，并自动运行不调用 Embedding、Reranker 或外部 API 的 smoke test。

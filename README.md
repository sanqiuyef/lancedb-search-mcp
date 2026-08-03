# LanceDB Search MCP

面向 AI 工作流的本地 LanceDB 文档检索、搜索解释与知识可视化工具。项目同时提供 MCP 服务和原生 PySide6“LanceDB 知识浏览器”。

## 主要能力

- Vector、Text、Hybrid 检索，以及可追踪的 RRF、类别提升和 Reranker 流水线；
- 按来源精确读取文件全文与 Chunk，不依赖模糊文件名匹配；
- 原生桌面端浏览知识库、原件状态、搜索解释和索引健康；
- 文档级语义关系图，以及带 Chunk 证据的实体、方法、观点和主题图谱；
- 独立 SQLite 侧车库存储人工关系与内容图谱，不修改 LanceDB 向量和源文件；
- MinerU 超过 200 页 PDF 的本地拆分与页码偏移清单。
- Chunk 文本资产与可切换 Embedding generation：换 Embedding 不再需要源 PDF/Word，Reranker 可独立替换。

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

## 可恢复知识资产与 Embedding 迁移

`my_docs` 仍是兼容旧 MCP 与浏览器的活动检索表；入库时会同步保存独立的
Chunk 文本资产。向量只是从这些资产派生出来的 generation。

1. 先用 `verify_knowledge_assets` 检查 Chunk 资产完整性。
2. 修改 Embedding 配置后调用 `rebuild_knowledge`。它从持久化 Chunk 重建新
   generation，成功校验前不会删除活动索引。
3. 用 `list_embedding_generations` 查看新旧模型，必要时调用
   `switch_embedding_generation` 回滚。若库在旧 generation 之后新增了文本，
   系统会拒绝切换，以免遗漏新文档；应先对该模型重新构建。
4. 定期用 `export_knowledge_assets` 导出 ZIP；源文件遗失或数据库重建时，先用
   `restore_knowledge_assets` 导入，再调用 `rebuild_knowledge`。

资产包包含完整 Chunk 原文、来源、类别、顺序与哈希，不包含向量或原始二进制
文件。因此它可以跨 Embedding/Reranker 和向量模型维度迁移，但不能还原 PDF 的
版式、图片或附件。

## 打包

```powershell
.\build_knowledge_browser.ps1 -VenvPath ".\.build-venv-clean" -SkipInstall
```

构建脚本会生成 `dist\KnowledgeBrowser\KnowledgeBrowser.exe`，并自动运行不调用 Embedding、Reranker 或外部 API 的 smoke test。

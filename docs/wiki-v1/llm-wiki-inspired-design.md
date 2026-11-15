# 借鉴 llm_wiki 的设计（不含代码）

> 目的：记录本仓库 Wiki V1 哪些设计参考了外部项目，哪些是自有实现，以及许可边界。
> 参考对象：
> - Andrej Karpathy, *LLM Wiki*（方法论 gist，公开设计范式）。
> - `nashsu/llm_wiki`（Tauri 桌面应用，**GPL-3.0**）：<https://github.com/nashsu/llm_wiki>。
>
> 本文件只记录设计层面的借鉴。本仓库没有复制、翻译或改写 llm_wiki 的源代码；两边的实现语言、存储与运行方式不同。llm_wiki 的应用代码是 GPL-3.0，本仓库是 MIT；如需在其基础上修改分发，必须遵守 GPL-3.0，不能把其代码并入本仓库。

## 采纳的设计

| llm_wiki 中的做法 | 我们的落地 | 说明 |
|---|---|---|
| 三层结构：原始资料 / Wiki / Schema | `my_docs`（原始切片，只读）；vault `wiki-维基/`（页面）；vault `AGENTS.md` + `SCHEMA.md` + `wiki-维基/ai-purpose.md`（规则与意图） | 原始资料不因 Wiki 改写 |
| `index.md` 作为导航入口 | `wiki-维基/ai-index.md`，发布页面后重建，按页面类型列出链接 | 纯生成物，可重建；`ai-` 前缀避免覆盖用户的 `INDEX.md` |
| `log.md` 作为时序操作日志 | `wiki-维基/ai-log.md`，每次创建/提案/发布/拒绝追加一行 | 追加写，不回溯修改 |
| `purpose.md` 定义知识库目标与范围 | `wiki-维基/ai-purpose.md`，首次生成模板，之后由人编辑 | AI 不覆盖人的修改 |
| `overview.md` 全局概要 | `wiki-维基/ai-overview.md`，发布后按页面/证据统计重建 | 只放统计与入口，不替代页面 |
| `[[wikilink]]` + YAML frontmatter | 已有 `page_links` 与 vault frontmatter；页面级新增 `sources: []` | 页面级来源与引文级证据并存 |
| 两步摄入：先分析、再生成 | Agent 侧契约：`wiki_submit_extraction` 接收 analysis 与候选对象，再另行生成页面 | 服务端不内置 LLM，不产生 API 费用 |
| SHA256 增量缓存 | `document_revisions.processing_fingerprint` + `chunk_refs` | 同一指纹重复登记幂等 |
| 持久化摄入队列 | `wiki_jobs`（queued/running/succeeded/failed/needs_review） | 失败不影响原始 RAG 入库 |
| 审核队列 | `proposals` + `write_journal` + Validator | 默认只能 Draft/Proposal，发布需显式批准 |
| 页面类型（source/entity/concept/synthesis/overview） | 保留 vault 现有 `concept/entity/topic` 三类；来源摘要与综述暂用 `topic` + tags 表达 | 不擅自改 vault 的目录地图 |
| 知识图谱（四信号 + Louvain） | 已有 `page_links` + `relations`，V1 只做 1-2 跳邻域查询；图分析与社区检测留待 V2 | 不引入图数据库 |

## 明确不借用

- 不复制 llm_wiki 的 Tauri/Rust/React 代码、提示词文本、图谱算法实现或 UI 组件。
- 不引入其项目目录约定（`raw/sources/`、`wiki/`、`schema.md`）作为本仓库的强制结构；我们使用现有 vault。
- 不在服务端内置 LLM 调用；抽取由宿主 Agent 完成。
- 不把其 GPL-3.0 代码或衍生作品并入本 MIT 仓库。

## 我们的差异点

1. **引文级证据**：`claims → claim_evidence → chunk_refs → document_revisions → documents`，可定位到具体切片与原文摘句；llm_wiki 是页面级 `sources: []`。
2. **本地检索栈**：bge-m3 + BM25（默认 ngram 2–3，可切 jieba）+ MinerU + 图像检索，离线运行、零 token 成本。
3. **Obsidian 作为正文权威**：页面在 vault 内，Git 即版本库；SQLite 只存身份、关系、证据与提案。
4. **可关闭**：`WIKI_ENABLED=0` 时原 15 个 MCP 工具与启动路径完全不变。

## 许可与引用

- 方法论：Karpathy gist，作为设计范式引用。
- 产品参考：`nashsu/llm_wiki`，GPL-3.0；本项目仅借鉴公开可见的产品设计，不复制代码。
- 本仓库代码：MIT；模型权重遵循各自许可（jina-clip-v2 为 CC-BY-NC-4.0，bge 系列许可另行确认）。

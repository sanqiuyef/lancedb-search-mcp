# Agent 两步摄入契约（wiki_submit_extraction）

> 适用范围：Agent/宿主模型把原始资料消化为 Wiki 实体、断言、关系与页面建议时使用。
> 服务端（`kb/wiki/service.py`）不内置 LLM、不调用外部 API；它只校验、落库并返回
> 结构化结果。页面仍遵循「默认 Draft / Proposal，显式审核后才发布」。
>
> 相关工具：`wiki_search_evidence`、`wiki_get_evidence`、`wiki_submit_extraction`、
> `wiki_create_page`、`wiki_propose_update`、`wiki_review_queue`、`wiki_apply_proposal`。

## 1. 两步流程

### 第一步：分析（Agent 侧）

1. 用 `wiki_search_evidence`（或既有 `search_knowledge`/`ask_knowledge`）检索原始切片；
2. 阅读证据，必要时用 `wiki_get_evidence` 按 `chunk_id` 查看 `evidence_bundle`；
3. 产出一段 `analysis`（本轮结论与依据），以及实体/断言/关系候选；
4. 不要让模型「凭记忆」补事实：无法定位到 chunk 的内容应标为 hypothesis。

### 第二步：提交（服务端校验）

调用 `wiki_submit_extraction`，参数 `payload_json` 是下文的 JSON 字符串。
服务端会：

- 校验 payload 结构（Pydantic，未知字段直接拒绝整包）；
- 按规范化名称/别名复用实体（不自动合并相似实体）；
- 对事实型断言要求至少一条可校验引文；
- 校验关系谓词白名单与端点实体；
- 在一个 SQLite 事务中写入实体/断言/关系，异常整体回滚；
- 只根据 `page_suggestion` 生成 proposal，不直接创建/发布页面。

## 2. 请求 JSON 示例

```json
{
  "analysis": "资料指出城市洪涝受降雨与排水能力共同影响；SWMM 常用于降雨径流模拟。",
  "source_document_id": "7f1c9e...64位doc_id...",
  "source_chunk_ids": ["a3f0...chunk_id..."],
  "entities": [
    {
      "name": "Urban Flood",
      "entity_type": "concept",
      "aliases": ["城市洪涝", "内涝"],
      "description": "城市区域因降雨超过排水能力形成的积水现象。",
      "evidence_ids": ["a3f0...chunk_id..."],
      "confidence": 0.95
    },
    {
      "name": "SWMM",
      "entity_type": "tool",
      "aliases": ["Storm Water Management Model"],
      "description": "暴雨管理模型。",
      "evidence_ids": ["a3f0...chunk_id..."],
      "confidence": 0.93
    }
  ],
  "claims": [
    {
      "claim_text": "城市洪涝受降雨与排水能力共同影响。",
      "claim_type": "factual",
      "evidence": [
        {
          "chunk_id": "a3f0...chunk_id...",
          "quote": "城市洪涝受降雨与排水能力共同影响",
          "page_number": 3,
          "section_path": "2.1 影响因素",
          "evidence_role": "supports"
        }
      ],
      "confidence": 0.9
    },
    {
      "claim_text": "SWMM 可能适合该城市的洪涝情景模拟。",
      "claim_type": "hypothesis",
      "evidence": [],
      "confidence": 0.6
    }
  ],
  "relations": [
    {
      "subject": "Urban Flood",
      "predicate": "uses",
      "object": "SWMM",
      "evidence_ids": ["a3f0...chunk_id..."],
      "confidence": 0.9
    }
  ],
  "page_suggestion": {
    "action": "update",
    "page_id": "01J...页面ID...",
    "content_md": "# 城市洪涝\n\n城市洪涝受降雨与排水能力共同影响。\n",
    "rationale": "补充影响因素与 SWMM 的关系"
  },
  "chunk_texts": {
    "a3f0...chunk_id...": "城市洪涝受降雨与排水能力共同影响。SWMM 常用于模拟。"
  }
}
```

说明：

- `chunk_texts` 可选。提供时服务端直接用这里的原文校验引文（离线、可复现）；
  未提供时按 `chunk_refs.legacy_doc_id/source_value/chunk_index` 回查 LanceDB。
- `source_document_id` 可选；一旦提供必须是已登记的 `documents.document_id`，
  否则整包失败（`source_document_not_found`），不产生部分写入。
- `source_chunk_ids` 只做提示与存在性告警，不决定写入范围。

## 3. 字段与校验规则

### 3.1 `entities[]`

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 规范名；NFKC + casefold + 去标点空白后查重 |
| `entity_type` | 否 | 自由字符串（如 concept/tool/person） |
| `aliases` | 否 | 别名列表；按规范化别名查重 |
| `description` | 否 | 实体描述 |
| `evidence_ids` | 否 | chunk_id 或 evidence_id；不存在只告警 |
| `confidence` | 否 | 0..1，默认 0 |

规则：

- `confidence < WIKI_MIN_ENTITY_CONFIDENCE`（默认 0.90）：**不写入**，返回候选
  （`reason=below_confidence_threshold`，`candidates[]` 为已有相似实体）；
- `confidence >=` 阈值：按规范化 canonical/alias 精确复用已有实体；没有才创建；
- **不自动合并**：名称相似但不完全相等的实体保持独立，需要人工调用显式合并；
- 同一批里重复出现的同一实体只创建一次，第二条返回 `created=false`；
- 别名与其他实体冲突（需要显式 merge）会让整包失败并回滚，避免静默丢失。

### 3.2 `claims[]`

| 字段 | 必填 | 说明 |
|---|---|---|
| `claim_text` | 是 | 断言正文 |
| `claim_type` | 否 | 默认 `fact`；见下表 |
| `evidence[]` | 事实型必填 | `{chunk_id, quote, page_number?, section_path?, evidence_role?}` |
| `confidence` | 否 | 0..1，默认 0 |

`claim_type` 映射：

| 对外取值 | 是否事实型 | 落库 `claims.claim_type` |
|---|---|---|
| `fact` / `factual` / `definition` / `result` | 是 | `fact` |
| `hypothesis` / `interpretation` | 否 | `hypothesis` |

规则：

- 事实型断言没有 `evidence` → 拒绝该条（`reason=missing_evidence`）；
- `chunk_id` 不存在 → 拒绝该条（`reason=not_found`）；
- `quote` 归一化空白后必须能在 chunk 原文中找到；找不到 → `reason=not_found`；
- hypothesis/interpretation 可以无证据；
- 通过的断言以 `status=proposed` 落库，引文写入 `claim_evidence`，
  并尽量补齐 chunk 的 `page_number/section_path`。

### 3.3 `relations[]`

| 字段 | 必填 | 说明 |
|---|---|---|
| `subject` | 是 | 实体 `entity_id` 或名称/别名 |
| `predicate` | 是 | 必须来自谓词白名单 |
| `object` | 是 | 实体 `entity_id` 或名称/别名 |
| `evidence_ids` | 否 | 写入 `relations.evidence`（JSON 数组） |
| `confidence` | 否 | 0..1，默认 0 |

谓词白名单（与 `kb/wiki/repositories/relations.py` 同步）：

```text
contains_component, uses, based_on, improves, part_of, predicts,
evaluated_on, compared_with, supports, contradicts, related_to
```

规则：

- 谓词不在白名单 → 拒绝该条（`invalid_predicate`，附带允许值列表）；
- `subject`/`object` 必须解析到**已存在且 active** 的实体（名称或 id）；
  解析失败 → `unresolved_entity`；命中多个同名实体同样视为未解析；
- 关系端点不能是同一实体 → `self_loop`；
- `confidence < WIKI_MIN_RELATION_CONFIDENCE`（默认 0.90）→ 只返回候选、不写入；
- 默认 `status=proposed`，不会自动 `verified`；
- 相同 `(subject, predicate, object)` 已存在时复用已有关系，`created=false`。

### 3.4 `page_suggestion`

| 字段 | 说明 |
|---|---|
| `action` | `create` 或 `update` |
| `page_id` | `action=update` 必填 |
| `title` / `slug` | `action=create` 至少提供一个（仅用于提示） |
| `page_type` | concept / entity / topic（仅用于提示） |
| `content_md` | 有值时用于生成 proposal |
| `rationale` | 提案理由 |

规则：

- 服务端**不会**因 `page_suggestion` 直接创建或发布页面；
- `action=update` 且提供 `content_md`：生成 `update_page` proposal
  （`expected_revision_id` 取目标页当前 revision）；
- `action=create`：只返回 warning，提示先调用 `wiki_create_page` 生成 draft，
  再用 `wiki_propose_update` 提交内容；
- 没有 `content_md`：不生成 proposal，返回 warning。

## 4. 响应结构

成功时返回：

```json
{
  "ok": true,
  "created": {
    "entities": [
      {
        "entity_id": "01J...",
        "canonical_name": "Urban Flood",
        "normalized_name": "urbanflood",
        "entity_type": "concept",
        "description": "……",
        "confidence": 0.95,
        "status": "active",
        "aliases": ["城市洪涝", "内涝"],
        "created": true,
        "matched_by": "none"
      }
    ],
    "claims": [
      {
        "claim_id": "01J...",
        "claim_text": "城市洪涝受降雨与排水能力共同影响。",
        "claim_type": "fact",
        "source_claim_type": "factual",
        "confidence": 0.9,
        "status": "proposed",
        "document_id": "7f1c9e...",
        "evidence_ids": ["01J..."],
        "evidence_count": 1
      }
    ],
    "relations": [
      {
        "relation_id": "01J...",
        "subject_entity_id": "01J...",
        "subject_name": "Urban Flood",
        "predicate": "uses",
        "object_entity_id": "01J...",
        "object_name": "SWMM",
        "confidence": 0.9,
        "status": "proposed",
        "evidence_ids": ["a3f0..."],
        "created": true
      }
    ]
  },
  "rejected": [
    {
      "kind": "claim",
      "index": 0,
      "reason": "not_found",
      "error": "引文未在 chunk 原文中找到: '……'",
      "name": "断言开头……",
      "candidates": [],
      "item": {"claim_text": "……", "claim_type": "factual"}
    }
  ],
  "warnings": [
    "source_chunk_ids 中的 chunk 不存在: xxx"
  ],
  "proposal": {
    "proposal_id": "01J...",
    "status": "pending",
    "page_id": "01J...",
    "expected_revision_id": "01J...",
    "diff": "--- a/wiki-维基/...\n+++ b/wiki-维基/...\n"
  }
}
```

`rejected[].reason` 取值：

| reason | 含义 |
|---|---|
| `invalid_name` | 名称规范化后为空 |
| `below_confidence_threshold` | 低于实体/关系阈值，只返回候选 |
| `missing_evidence` | 事实型断言没有证据 |
| `not_found` | chunk 不存在、取不到原文，或引文不匹配 |
| `invalid_predicate` | 关系谓词不在白名单 |
| `unresolved_entity` | 关系端点无法解析到 active 实体 |
| `self_loop` | 关系 subject 与 object 是同一实体 |

## 5. 页面流转（两步摄入的页面侧）

```text
wiki_create_page                → vault 页面 status=draft（不发布）
wiki_propose_update             → proposal pending（draft.md + diff.md）
wiki_review_queue               → 人工/Agent 审核
wiki_apply_proposal(approve)    → 写页面 + git commit + 重建 index/log/overview
                                   + 更新页面索引（失败记 index_pending）
wiki_apply_proposal(reject)     → 页面不变
```

发布方式：

- 若提案内容是**完整文档**（以 `---` frontmatter 开头），可在 frontmatter 中把
  `status` 改为 `published`；审核通过后页面即发布。
- 若提案内容只是正文，页面沿用当前 status（draft 仍是 draft，published 仍 published）。

`wiki_create_page` 的参数是逗号分隔字符串（`aliases`/`tags`/`sources`），不是数组；
MCP 层会拆分为列表。

## 6. 事务与错误语义

- 实体/断言/关系与 `page_suggestion` 的 proposal 记录共用一个
  `BEGIN IMMEDIATE` 事务：
  - 单条语义问题进入 `rejected`，其余对象继续；
  - 意外异常（别名冲突、DB 约束错误等）抛 `WikiServiceError`，整个事务回滚，
    不会留下部分实体/断言/关系/提案记录；
- proposal 的 `draft.md`/`diff.md` 是文件写入，不受 SQLite 回滚保护；创建失败时
  服务端会清理本次提案目录，并降级为 `warnings`，不回滚已校验的抽取结果；
- payload 结构错误（缺 `analysis`、未知字段、非法 `claim_type`）直接抛
  `WikiServiceError(code=invalid_payload)`；
- MCP 层捕获所有异常，返回 `{"ok": false, "error": "..."}`，不抛到协议层。

## 7. 安全边界

- 服务端不读取真实 vault 之外的路径；`WikiConfig` 会校验页面根目录在 vault 内；
- 不把 `chunk_texts` 或原文写入页面以外的位置；提案文件写在
  `<db_dir>/proposals/<proposal_id>/`，不写入页面目录；
- 测试与离线验证使用临时 vault、临时 SQLite、临时 LanceDB；不要指向真实知识库；
- 不内置 LLM、不产生外部 API 费用；所有判断由 Agent 提供、服务端只做结构校验。

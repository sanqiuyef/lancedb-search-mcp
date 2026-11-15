# Wiki 质量报告

- 生成时间: 2026-11-15T15:38:02.283Z
- 数据库: `D:\cherry-workplace\knowledge-hub\wiki\wiki.sqlite3`
- vault: `D:\cherry-workplace\knowledge-hub\vault`
- 页面索引表: `wiki_pages`

## 指标摘要

| 指标 | 数值 | 口径/说明 |
|---|---|---|
| Evidence Coverage | 100.0% (6/6) | factual/definition/result 类 claim 中有 supporting evidence 的比例 |
| Quote Validity | 100.0% (6/6) | 结构校验（quote_sha256 与位置字段），非内容级复核 |
| Broken Link Rate | 0.0% (0/6) | target_page_id 为空的链接 / 总链接 |
| Orphan Pages | 0/3 | published 页面中无入链且无出链 |
| Duplicate Page Candidates | 0 组 / 0 对 | 标题别名重复 + 摘要高相似 |
| Unindexed Published Pages | 0 | published 页面在页面索引中的缺失数 |
| Unsourced Relations | 0 | 无 evidence 且无 source_page_id 的 proposed/verified 关系 |
| Unresolved Aliases | 0 | redirect 目标缺失 / inactive 实体别名 / active 实体别名冲突 |

## 明细

### Evidence Coverage

- 适用 claim（claim_type ∈ fact, factual, definition, result，状态属于 proposed, accepted, disputed）: 6
- 已有证据: 6
- 覆盖率: 100.0%

### Quote Validity

- 证据总数: 6
- 结构校验通过: 6
- 通过率: 100.0%
- 校验方法: `structural_fingerprint`（claim_evidence 无 verification 列，切片原文不在 SQLite；内容级校验需回查 LanceDB）

### Broken Link Rate

- 断链: 0 / 总链接 6
- 断链率: 0.0%

### Orphan Pages

- published 页面: 3
- 孤儿页面: 0

### Duplicate Page Candidates

- 标题/别名归一后重复组: 0
- 摘要相似对（阈值 0.82）: 0
- 只报告候选，不自动合并。

### Unindexed Published Pages

- 状态: `ok`
- 缺失页面: 0

### Unsourced Relations

- 数量: 0

### Unresolved Aliases

- 数量: 0
- 分类: dangling_redirect=0, inactive_owner=0, ambiguous_active_alias=0

## 口径与限制

- Evidence Coverage 口径：claim_type 属于 fact/factual/definition/result 且状态非 rejected 的 claim；当前 schema 仅持久化 fact/hypothesis，definition/result 需未来扩展 claim_type 后生效。
- Quote Validity 口径：claim_evidence 若含 verification 列，按 verification='verified' 统计；当前 schema 无该列，则按 quote_sha256(quote) 与 text_sha256 比对及位置字段做结构校验，内容级校验需通过 evidence.revalidate_evidence 回查 LanceDB。
- Broken Link Rate = target_page_id IS NULL 的链接数 / 总链接数；无链接时比率为 null。
- Duplicate Page Candidates 只报告候选，不合并页面。
- Unindexed Published Pages 依赖 WIKI_PAGE_INDEX_TABLE；表缺失时输出“索引未建立”。
- 关系正确率需人工抽样，本脚本不计算、不编造。

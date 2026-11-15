# Wiki V1 仓库与环境审计

> 由 `scripts/audit_wiki_environment.py` 只读生成。

## 运行环境

- Python: `3.13.9` (`D:\anaconda3\python.exe`)
- lancedb: `0.39.0`
- pyarrow: `21.0.0`
- pydantic: `2.12.5`
- mcp: `1.29.0`
- sentence-transformers: `6.0.1`
- torch: `2.6.0+cu124`
- watchdog: `6.0.0`

## LanceDB

- 路径: `D:\cherry-workplace\knowledge-hub\lancedb`
- 存在: True
- 体积: 693.5 MB

| 表 | 行数 | 索引 |
|---|---|---|
| `image_chunks` | 4186 | 无 |
| `my_docs` | 26583 | text_idx(text), vector_idx(vector) |
| `web_figure_chunks` | 95 | 无 |
| `wiki_pages` | 3 | wiki_page_text_idx(text) |

## `my_docs` source 形态

- 总行数: 26583
- 去重 source: 218
- 含路径分隔符的 source: 218 (100.00%)
- 纯 basename source: 0
- `网页-` source: 0，单 source 最大行数: 0
- 空 source 行: 0，空 doc_id 行: 0
- 重复 `(source, chunk_index)`: 3654 组 / 21147 行
- 重复 `doc_id`: 0 组 / 0 行
- `ingested_at` 范围: 2026-10-03T16:56:06Z → 2026-11-05T05:07:18Z
- 同一 source 多 category: 0 个

### 行数最多的 source（前 20）

| source | chunks |
|---|---|
| `08-literature-文献\papers-论文全文\Open problems in uncertainty quantification for flood modelling A systematic review.md` | 384 |
| `08-literature-文献\papers-论文全文\基于多主体建模的城市内涝脆弱性时空动态计算与多情景差异研究.md` | 376 |
| `08-literature-文献\papers-论文全文\Distribution-Free Predictive Inference for Regression.md` | 349 |
| `08-literature-文献\papers-论文全文\Estimating uncertainty in flood model outputs using machine learning informed by Monte Carlo analysi.md` | 319 |
| `08-literature-文献\papers-论文全文\The Shuttle Radar Topography Mission.md` | 313 |
| `08-literature-文献\papers-论文全文\Physics-informed neural control for mitigating flood effects A case study based on a simulated hydr.md` | 295 |
| `08-literature-文献\papers-论文全文\Improving precipitation nowcasting via multiphysical parameter fusion in radar echo extrapolation.md` | 279 |
| `08-literature-文献\papers-论文全文\Advancing Flood Detection and Mapping A Review of Earth Observation Services, 3D Data Integration,.md` | 274 |
| `08-literature-文献\papers-论文全文\A novel CNN-LSTM-Attention model to forecast flood susceptibility under global climate scenarios.md` | 270 |
| `08-literature-文献\papers-论文全文\Integrative statistical analysis of compound flood dynamics under climate variability in China’s coa.md` | 266 |
| `08-literature-文献\papers-论文全文\Physics-constrained deep learning for high-dimensional surrogate modeling and uncertainty quantifica.md` | 263 |
| `08-literature-文献\papers-论文全文\Accelerating Urban Flood Inundation Simulation Under Spatio‐Temporally Varying Rainstorms Using Conv.md` | 259 |
| `08-literature-文献\papers-论文全文\Physics-informed deep learning for hillslope subsurface flow prediction integrating soil moisture d.md` | 258 |
| `08-literature-文献\papers-论文全文\Fusion of data-driven models with a knowledge-guided loss function for flood forecasting.md` | 257 |
| `08-literature-文献\papers-论文全文\Improving River Routing Using a Differentiable Muskingum‐Cunge Model and Physics‐Informed Machine Le.md` | 257 |
| `08-literature-文献\papers-论文全文\Poggio 等 - 2021 - SoilGrids 2.0 producing soil information for the globe with quantified spatial uncertainty.md` | 251 |
| `08-literature-文献\papers-论文全文\基于遥感的中国格网暴雨强度公式推求及研究.md` | 250 |
| `08-literature-文献\papers-论文全文\Urban Flood Modeling and Forecasting with Deep Neural Operator and Transfer Learning.md` | 240 |
| `08-literature-文献\papers-论文全文\Physics-informed neural networks A deep learning framework for solving forward and inverse problems.md` | 229 |
| `08-literature-文献\papers-论文全文\Uncertainty in estimating the relative change of design floods under climate change a stylized expe.md` | 228 |

## Wiki SQLite

- 路径: `D:\cherry-workplace\knowledge-hub\vault\.wiki\wiki.sqlite3`
- 存在: False

## Vault

```json
{
  "vault_path": "D:\\cherry-workplace\\knowledge-hub\\vault",
  "vault_exists": true,
  "obsidian_marker": true,
  "is_git_repo": true,
  "wiki_root": "D:\\cherry-workplace\\knowledge-hub\\vault\\wiki-维基",
  "wiki_root_exists": true,
  "wiki_subdirs": [
    "concepts-概念",
    "entities-实体",
    "topics-主题"
  ],
  "existing_md_count": 9
}
```

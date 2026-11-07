# Tasks: T-014 长文件拆分

> 执行约束：每阶段 = 实现 → 全量 pytest 门禁 → 独立 code review → 修复 → 单次本地提交。
> 测试命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`

## P0 · 基线

- **状态**: done
- **动作**: 记录拆分前全量测试结果与 wc -l 快照。
- **证据**: 2026-11-07 `pytest tests/ -q` → **262 passed, 4 skipped, 8 warnings in 66.45s**（.venv，PYTHONPATH=src:.）。基线全绿。

## T-101 · deeper_research_tool.py 拆分（2167 行）

- **前置**: P0 基线
- **涉及**: `search/tool/deeper_research_tool.py`（30 行类骨架）+ 新增 `search/tool/deeper_research/{wiring,thinking_pipeline,thinking_iterate,thinking_synthesize,search_entry,tool_factories,streaming,streaming_loop,streaming_finalize}.py`
- **动作**: 按 design.md T-101 定稿表执行——9 个 Mixin 模块；`thinking()`/`thinking_stream()` 两个 800+ 行单方法在原 `步骤1-8` 注释接缝处做阶段分解，阶段体逐行原样搬移（脚本断言校验 18 段全部一致），仅新增 def/return/`ctx` 传递行。
- **验收**: pytest 全绿（262 passed/4 skipped 与基线一致）；ruff F821/F811 clean；wc -l 骨架 30、新模块最大 405（全部 ≤500）；全链 import 冒烟通过（wiring 因本地 Neo4j 凭据不可用未跑通，属原有导入期副作用，import 列表与原文件一致）。
- **状态**: done

## T-102 · deep_research_tool.py 拆分（1320 行）

- **前置**: T-101
- **涉及**: `search/tool/deep_research_tool.py`（108 行类骨架，search/close 留守）+ 新增 `search/tool/deep_research/{__init__,wiring,retrieval_funcs,thinking_pipeline,streaming,tool_factories}.py`
- **验收**: pytest 全绿；ruff F821/F811 clean；25 个方法块 verbatim 断言；独立评审 PASS。
- **状态**: done

## T-103 · hybrid_tool.py 拆分（661 行）

- **前置**: T-102
- **涉及**: `search/tool/hybrid_tool.py`（169 行骨架）+ 新增 `search/tool/hybrid_retrieval.py`（372）与 `hybrid_keywords.py`（149）
- **验收**: pytest 全绿；ruff clean；12 个方法块 verbatim；独立评审 PASS。
- **状态**: done

## T-104 · reasoning/ 三文件拆分（chain_of_exploration 808 / thinking 761 / evidence 710）

- **前置**: T-103
- **涉及**: `search/tool/reasoning/{chain_of_exploration,thinking,evidence}.py`（骨架 32/40/92 行）+ 新增 12 个同级 Mixin 模块（chain_strategy/scoring/info/explorer、thinking_hypotheses/branching/state/text、evidence_record/contradiction/citations/summary）
- **特别约束**: `thinking_text.py` 评审修订为 ThinkingTextMixin（原定模式 B），调用方零改动。
- **验收**: pytest 全绿；ruff F821/F811 clean；56 个方法块 verbatim（AST 复核）；独立评审 PASS。
- **状态**: done

## T-105 · agents 层拆分（base 792 / deep_research_agent 640 / reflector 730 / worker_coordinator 703）

- **前置**: T-104
- **涉及**: `agents/{base,deep_research_agent}.py`、`agents/multi_agent/executor/{reflector,worker_coordinator}.py` + 各自 Mixin 模块
- **状态**: pending

## T-106 · base_reporter.py 拆分（1163 行）

- **前置**: T-105
- **涉及**: `agents/multi_agent/reporter/base_reporter.py`（115 行骨架）+ 新增 `report_schemas.py`（schema 迁出+re-export）与 5 个 Mixin（report_pipeline/card_sections/mapreduce_sections/report_formatting/report_cache）
- **验收**: pytest 全绿；ruff clean；36 块 verbatim；独立评审 PASS（含死代码副本修复复核）。
- **状态**: done

## T-107 · incremental_graph_builder.py 拆分（964 行）

- **前置**: T-106
- **涉及**: `integrations/build/incremental_graph_builder.py` + 新增 `integrations/build/incremental_{file_changes,merge,io,stats}.py`
- **状态**: pending

## T-108 · harness 主链拆分（workflow 731 / runtime 718）

- **前置**: T-107（主链最后拆）
- **涉及**: `harness/workflow.py`（27 行骨架：docstring + Protocol + 两 driver re-export）+ 新增 `harness/{report_fallbacks,drivers_plan_execute,drivers_deep_research}.py`；`harness/runtime.py`（174 行骨架）+ 新增 `harness/{runtime_execution,runtime_persistence}.py`
- **特别约束**: `_execute_run_inner`（383 行）整块搬移禁止内部再拆 ✓；`_evidence_only_report` 全仓无外部引用，迁入 report_fallbacks.py 且不加兼容 re-export（design 已同步）。
- **验收**: pytest 全绿；ruff clean；verbatim 断言；bootstrap/run_service/测试导入链实测可用；独立评审 PASS（含 `from __future__ import annotations` 补回 4 文件）。
- **状态**: done

## 收尾

- **动作**: 全仓 `wc -l` 复核（无 ≥600 非 facade 文件）；acceptance.md 汇总；规范文档与代码一致性检查。
- **结果（2026-11-07）**: src/ 现存最大文件 561 行（`incremental_update.py`，属 500–599 backlog 段）；14 个原长文件全部降为 27–177 行类骨架；每个骨架与新模块均 ≤500/≤200 预算。src 总行数 45,793 → 46,553（+760，为文件头溯源注释、模块 docstring 与 re-export 的结构性开销 ≈1.7%）。
- **状态**: done

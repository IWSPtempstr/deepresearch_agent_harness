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
- **涉及**: `search/tool/deep_research_tool.py` + 新增 `search/tool/deep_research/{wiring,retrieval_funcs,thinking_pipeline,streaming,tool_factories}.py`
- **验收**: 同 T-101 模式；pytest 全绿 + 行数门禁。
- **状态**: pending

## T-103 · hybrid_tool.py 拆分（661 行）

- **前置**: T-102
- **涉及**: `search/tool/hybrid_tool.py` + 新增 `search/tool/hybrid_retrieval.py`
- **状态**: pending

## T-104 · reasoning/ 三文件拆分（chain_of_exploration 808 / thinking 761 / evidence 710）

- **前置**: T-103
- **涉及**: `search/tool/reasoning/{chain_of_exploration,thinking,evidence}.py` + 新增 `chain_strategy/chain_scoring/chain_info/chain_explorer`、`thinking_hypotheses/thinking_branching/thinking_state/thinking_text`、`evidence_record/evidence_contradiction/evidence_citations/evidence_summary`
- **特别约束**: `thinking_text.py` 用模式 B（模块函数 + re-import），`deeper_research/branching.py` 等调用方零改动。
- **状态**: pending

## T-105 · agents 层拆分（base 792 / deep_research_agent 640 / reflector 730 / worker_coordinator 703）

- **前置**: T-104
- **涉及**: `agents/{base,deep_research_agent}.py`、`agents/multi_agent/executor/{reflector,worker_coordinator}.py` + 各自 Mixin 模块
- **状态**: pending

## T-106 · base_reporter.py 拆分（1163 行）

- **前置**: T-105
- **涉及**: `agents/multi_agent/reporter/base_reporter.py` + 新增 `reporter/{report_pipeline,card_sections,mapreduce_sections,report_formatting}.py`
- **约束**: pydantic schema（ReporterConfig/SectionContent/ReportResult）留在原文件。
- **状态**: pending

## T-107 · incremental_graph_builder.py 拆分（964 行）

- **前置**: T-106
- **涉及**: `integrations/build/incremental_graph_builder.py` + 新增 `integrations/build/incremental_{file_changes,merge,io,stats}.py`
- **状态**: pending

## T-108 · harness 主链拆分（workflow 731 / runtime 718）

- **前置**: T-107（主链最后拆）
- **涉及**: `harness/workflow.py` + 新增 `harness/{report_fallbacks,drivers_plan_execute,drivers_deep_research}.py`；`harness/runtime.py` + 新增 `harness/{runtime_execution,runtime_persistence}.py`
- **特别约束**: `_execute_run_inner`（383 行）整块搬移禁止内部再拆；`workflow.py` re-export 两个 driver；`_evidence_only_report` 模式 B re-import。
- **验收**: pytest 全绿（重点 tests/harness/）+ `from deepresearch_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver` 冒烟。
- **状态**: pending

## 收尾

- **动作**: 全仓 `wc -l` 复核（无 ≥600 非 facade 文件）；acceptance.md 汇总；规范文档与代码一致性检查。
- **状态**: pending

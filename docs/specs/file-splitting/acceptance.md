# Acceptance: T-014 长文件拆分

## P0 · 基线（2026-11-07）

- 命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
- 结果：**262 passed, 4 skipped, 8 warnings in 66.45s** — 全绿，作为后续每阶段的行为不变基准。
- 行数快照（拆分前，≥600 行）：deeper_research_tool 2167 / deep_research_tool 1320 / base_reporter 1163 / incremental_graph_builder 964 / chain_of_exploration 808 / agents/base 792 / thinking 761 / workflow 731 / reflector 730 / runtime 718 / evidence 710 / worker_coordinator 703 / hybrid_tool 661 / deep_research_agent 640。

## 决策记录

- 2026-11-06 用户决策：保留 GraphRAG 全链路；对长文件拆分（替代删除式瘦身）。
- 2026-11-06 AskUserQuestion：拆分阈值 600 行；import 兼容采用保留 facade。
- 2026-11-07 design 定稿：Mixin 拆分为默认模式（类骨架 ≤200 行），无状态纯函数用模式 B（模块函数 + re-import）。
- 2026-11-07 用户确认执行方式：按规范文档设定 goal，每完成一个阶段就提交工作区并做 code review。

## T-101 · deeper_research_tool.py 拆分（2026-11-07）

- 改动：`deeper_research_tool.py` 2167 → 30 行类骨架；新增 `deeper_research/` 下 9 个 Mixin 模块（125/266/405/234/102/148/371/402/231 行，全部 ≤500）。
- 逐行校验：`/tmp/verify_split.py` 对 18 个映射段断言原文一致 → ALL VERBATIM ✓（评审方独立复核 18/18，仅 2 处空行规范化）。
- ruff：`--select F821,F811,E722` → All checks passed（曾抓出并修复 1 个真实问题：原 2127 行使用循环变量 `iteration`，现经 ctx 传递）。
- pytest：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q` → **262 passed, 4 skipped, 8 warnings in 55.31s**，与 P0 基线完全一致。
- import 冒烟：8/9 Mixin + 骨架通过；`wiring` 全链被本地 Neo4j 凭据阻断（`config/neo4jdb.py` 导入期连接副作用，main 上同样触发，与拆分无关——import 列表与原文件一致）。
- 独立评审（general-purpose 评审代理）：**verdict PASS**，四项不变量（逐行一致/类路径 MRO 不变/行为不变/imports 完备）全部成立。
- 已知偏差（均不可达或无语义）：max_iterations≤0 时 `ctx["iteration"]` 会 NameError（原版走优雅 return；MAX_SEARCH_LIMIT=5 且 workflow.py 钳制 max(1,...)，分支不可达）。

## T-102 · deep_research_tool.py 拆分（2026-11-07）

- 改动：`deep_research_tool.py` 1320 → 108 行类骨架（search/close 留守）；新增 `deep_research/` 子包 5 个 Mixin（85/299/359/449/70 行）+ `__init__.py`。
- 逐行校验：25 个方法块（含 454 行 @staticmethod）全部 verbatim；评审方独立复核 25/25 字节级一致。
- **过程发现并修复**：首版把 `BaseSearchTool` 放在 MRO 首位，abc 抽象检查沿 MRO 先命中抽象定义，类被判 abstract，`tests/retrieval/test_retrieval_phase2.py` 3 个用例失败 → 修复为 Mixin 在前、基类殿后，写入 design.md 模式 A 硬规则。
- 门禁：pytest **262 passed, 4 skipped（55s，与基线一致）**；ruff F821,F811,E722 clean；py_compile 通过；5 模块 runtime import 成功。
- 独立评审：**verdict PASS**；跨模块耦合核查——DeeperResearchTool 经 `self.deep_research.*` 的 6 处方法调用拆分后 MRO 全部可解析。
- 非阻塞备忘：骨架保留原 typing 全量导入（4 个惰性未使用名，与原文件风格一致，不清理）。

## T-103 · hybrid_tool.py 拆分（2026-11-07）

- 改动：`hybrid_tool.py` 661 → 169 行骨架；新增同包 `hybrid_retrieval.py`（372）/`hybrid_keywords.py`（149）——评审修订：单模块会超 500 预算，故拆两个 Mixin，design.md 已同步。
- 门禁：pytest **262 passed, 4 skipped（55s）**；ruff F821,F811,E722 clean；12 个方法块 verbatim；`HybridSearchTool.__abstractmethods__` 为空（MRO：两 Mixin 在前、BaseSearchTool 殿后）。
- 独立评审：**verdict PASS**；耦合核查 8 处 importer 零改动，外部 `self.hybrid_tool.extract_keywords/.close()` 经 MRO 可解析。
- 非阻塞备忘：新模块残留原文件机械复制的未用 import（F401，无运行时影响，留后续清理轮）。

## T-104 · reasoning/ 三文件拆分（2026-11-07）

- 改动：chain_of_exploration 808→32、thinking 761→40、evidence 711→92（骨架）；新增 12 个同级 Mixin 模块（60–323 行，全部 ≤500）。
- 逐行校验：56 个方法块 verbatim（评审方 AST 逐方法复核全覆盖，唯一差异为 EOF 换行）。
- 门禁：pytest **262 passed, 4 skipped（56s）**；ruff F821,F811 全目录 clean（E722 裸 except 系原代码既有风格，非本次引入，不在门禁）。
- 独立评审：**verdict PASS**；耦合核查——branching/enhancer/streaming_loop 等对 `thinking_engine.*`/`evidence_tracker.*`/`chain_explorer.explore` 的调用点拆分后 MRO 全部可解析。
- 评审修订（已同步 design.md）：thinking_text 由模式 B 模块函数改为 ThinkingTextMixin，调用方零改动。

## T-105 · agents 层四文件拆分（2026-11-07）

- 改动：base 792→154、deep_research_agent 640→177、reflector 730→124、worker_coordinator 703→134（骨架）；新增 12 个 Mixin 模块（50–394 行，全部 ≤500）。
- **过程发现并修复（三项，均已写入 design.md §2 硬规则）**：
  1. 模块 docstring 导致 import 解析为零 → 解析器跳过 docstring；
  2. `_LOGGER = logging.getLogger(...)` 模块级赋值未随模块携带 → prologue 机制（含其依赖的 import logging）；
  3. 携带 @abstractmethod 的普通 Mixin 不被 ABCMeta 汇总，BaseAgent 抽象方法 4→2 → 该类 Mixin 继承 ABC，恢复 4。
- 门禁：pytest **262 passed, 4 skipped（55s）**；ruff F821,F811 clean（71 方法块 verbatim）；`BaseAgent.__abstractmethods__` 恰为原 4 个；ReflectionExecutor 具象化；WorkerCoordinator 保持普通类。
- 独立评审：**verdict PASS**；execute_task 内 7 处跨 Mixin self.* 调用逐一 MRO 解析通过；四组"only-in-orig 语句"均为空（无丢失）。
- 评审修订（已同步 design.md）：execute_task 拆入 reflector_execute.py（留骨架将超 200 预算）；骨架保留原模块 docstring。
- **评审发现的仓库既有问题（非本次引入，待用户决定）**：`reporter/evidence_cards.py:380` 存在遗留 F821（`json` 未导入，commit 5b84f30 引入），运行到该分支会 NameError——属潜在线上 bug，建议 T-014 完成后单独小提交修复。

## T-106 · base_reporter.py 拆分（2026-11-07）

- 改动：`base_reporter.py` 1163 → 115 行骨架（仅 __init__ 留守）；新增 `report_schemas.py`（74，三个 pydantic 类整体迁出 + 骨架 re-export 保旧路径）与 5 个 Mixin（report_pipeline 273 / card_sections 230 / mapreduce_sections 215 / report_formatting 177 / report_cache 200）。
- 结构特例（均已验证）：模块级 schema 抽到叶子模块避免 Mixin↔骨架循环 import；模块级函数 `_extract_tracker_from_state` 随唯一调用方迁入 card_sections.py。
- **过程发现并修复（第四条拆分库规则）**：`parse_methods` 对类最后一个方法默认取到 EOF，把文件尾部的模块级函数吞进方法块、随 report_cache 重复携带 → 类体止于首个非缩进行（评审代理的全文件行归属核算抓到，T-101~T-105 的原文尾部均为类方法、不受影响）。
- 门禁：pytest **262 passed, 4 skipped（56s）**；ruff F821,F811 clean；36 个块 verbatim；MRO 实测 5 Mixin 归属正确；4 处外部 import 站点（workflow/orchestrator/factory/reporter__init__）零改动可用。
- 独立评审：**verdict PASS**（死代码副本问题在提交前修复并复核）。

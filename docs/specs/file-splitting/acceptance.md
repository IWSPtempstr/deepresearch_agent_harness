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

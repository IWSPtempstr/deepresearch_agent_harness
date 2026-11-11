# T-401 盘点记录：基线、调用图与存量数据

- 日期：2026-11-11；执行基线 `main@dd42137`。
- 本文档记录实施前事实清单与实施 gate，全部由只读检查与 dry-run 脚本得出。

## 1. 测试基线

- 命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
- 结果：**376 passed, 4 skipped**（61.5s），与 acceptance.md 记录的既有数量一致，仅作为回归基线，不充当 T-017 新测试证据。
- Lint：`ruff check scripts/inventory_coreact_migration.py` 通过；`git diff --check` 无输出。

## 2. 存量数据盘点（本地 `data/app.db`，SQLite 只读打开）

工具：`scripts/inventory_coreact_migration.py`（默认 dry-run；`file:...?mode=ro` 打开，
不写库内容——SQLite 可能生成空的 `-wal`/`-shm` sidecar；不打印消息正文/秘钥）。

| 项 | 值 |
|---|---|
| runs 总数 | 163（全部 `workflow_mode='deep_research'`） |
| 终态 | completed=159 |
| 未终态 | interrupted=4（无任何 checkpoint 行） |
| checkpoints | 954 行，`schema_version` 全部 =1 |
| 编排标记分布 | deep_research=954；coreact_v1/staged_v1/staged_legacy=0（本地库从未跑过 plan_execute_report） |

标记分类与快照实现对齐：`payload["workflow_mode"]` 为权威；plan_execute_report 的引擎
标记读 `workflow_state["orchestration"]`（缺标记=旧 staged 实现，与
`drivers_plan_execute._restore` 同规则）。标记桶总和 = checkpoint 总数（本地 954/954 对账通过）。

**旧 Run 处理清单与实施 gate：**

- 4 个 `interrupted` Run 均无 checkpoint（无执行现场）→ 处置=`manual_review`；
  禁止自动续跑/取消/清空，保持原状。T-407 的"可映射快照 dry-run 转换"不涉及它们。
- 159 个 completed 的 `deep_research` 历史报告必须继续可读：历史读取 DTO 与
  `runs.workflow_mode` CHECK 约束（`persistence/models.py:92`）在 T-407/T-408 均不收窄。
- 本地库无 `plan_execute_report` Run、无 coreact checkpoint，故 checkpoint 版本适配
  （旧 LoopState → 新 ExecutionState）只需按"缺标记=legacy staged"处理；上线环境
  （PostgreSQL）切换前须用只读账号执行同款 SQL（或将库镜像为 SQLite 后重跑本脚本）
  出具该环境专属清单。

## 3. 入口与调用图（静态扫描 + 人工核对）

### 3.1 生产入口（现状）

| 入口 | 文件 | 当前分派 |
|---|---|---|
| HTTP API（进程内调度） | `backend/app/services/run_service.py:186-219` `_execute.workflow_factory` | `WorkflowMode.DEEP_RESEARCH` → `DeepResearchAgent`+`DeepResearchDriver`；否则 → `MultiAgentFactory.create_default_bundle`+`PlanExecuteReportDriver` |
| Worker | 同一 `_execute`（`execute_now` → `_execute`） | 同上 |
| CLI/本地 stage-3 | `harness/bootstrap.py:32-77` `run_persistent_query` | 默认 `WorkflowMode.DEEP_RESEARCH`；`plan_execute_report` 走 bundle |
| Skill eval | `run_service.run_evaluation_case:270-310` | 请求参数 `workflow_mode`（evaluator 只在 `workflow_factory is None` 时挂载） |
| 前端 | `frontend/src/types/api.ts`、`ChatPage.tsx` | 传递 `workflow_mode`（默认 deep_research） |
| 编排实现选择 | `config/settings_sections.py:414` `RESEARCH_ORCHESTRATION`（coreact_v1/staged_v1，默认 coreact_v1）→ `chat_service.py:144-146` 写入 `config_snapshot["orchestration"]`；`drivers_plan_execute.py:39,70` 按其分派 | 旧 Run 无该标记 → 按 `staged_v1` 处理 |

### 3.2 旧引擎模块引用清单（脚本扫描 + 全量人工核对）

脚本输出为"直接文本引用"启发式清单（registry/字符串动态调用需另行复核）；
下表经全量静态核对补全：

| 候选模块 | 生产引用方 | 测试/CLI 引用 |
|---|---|---|
| `harness/drivers_deep_research.py` | `harness/workflow.py:10`（re-export，实际使用方为 bootstrap/run_service）；`search/tool/*/wiring.py` 注释提及 | `tests/harness/test_harness_phase3.py:327,344`（经 workflow.py） |
| `agents/deep_research_agent.py`（含 nodes/asks/analysis 三个 Mixin） | `bootstrap.py:9`、`run_service.py:11`；`agents/fusion_agent.py:7`（经 `legacy_facade` 间接引用 orchestrator） | `tests/smoke/test_imports.py:2`、`test_harness_phase3.py:359,362`、`tests/retrieval/test_retrieval_phase2.py:293,295`；CLI `search_without_stream.py:13,17` |
| `agents/fusion_agent.py`（FusionGraphRAGAgent） | 无生产入口引用 | **根 CLI `search_without_stream.py:14,29` 导入并实例化（`AGENT_NAMES=("deep_research","fusion")`，:21）**——删除前须同步移除该 CLI 选项；`integration/legacy_facade.py:13,17` 仅服务它 |
| `agents/multi_agent/orchestrator.py`（MultiAgentOrchestrator/Result/Metrics） | `multi_agent_factory.py:11`、`integration/legacy_facade.py:17`（legacy_facade 仅被 fusion_agent 消费，见下）、`drivers_plan_execute.py:9` | `tests/smoke/test_imports.py:3` |
| `agents/multi_agent/integration/multi_agent_factory.py` | `bootstrap.py:10`、`run_service.py:12`、`integration/__init__.py` | `tests/harness/test_role_injection.py:3` |
| `planner/base_planner.py` | `orchestrator.py:16`、`multi_agent_factory.py:15`、`drivers_plan_execute.py:16`（仅 PlannerResult） | `test_coreact_driver.py`、`test_coreact_e2e.py`、`test_coreact_rubric_coverage.py` 等 |
| `planner/task_decomposer.py` / `plan_reviewer.py` | `planner/__init__.py`、`base_planner.py:30,34` | `tests/retrieval/test_retrieval_phase2.py:19`、`tests/smoke/test_planner_structured_output.py:4,5` |
| `planner/clarifier.py` | `planner/__init__.py`、`base_planner.py:26` | `test_coreact_e2e.py:26` 等（目标保留复用） |
| `executor/reflector*.py`（ReflectionExecutor + 4 Mixin） | `executor/__init__.py:12`、`worker_coordinator.py:23`（唯一生产注册点）；`AnswerValidationTool` 另被 `search/tool_registry.py:33` 注册为 `answer_validator` | 无直接测试引用 |
| `search/tool/deep_research/`、`deeper_research/` | `deep_research_tool.py:9-13`、`deeper_research_tool.py:3-11`（工具 Mixin）；工具类被 `deep_research_agent.py:5,144`、`retrieval_executor.py:215,218`（lazy）、`tool_registry.py:25-26`（registry 键 `deep_research`/`deeper_research`） | `tests/retrieval/test_retrieval_phase2.py:18` |
| `harness/drivers_plan_execute.py` | `workflow.py:11` re-export → bootstrap/run_service | 6 个 coreact 测试文件直接 import（加 `tests/evolution/test_builtin_learning_loop.py` 经 workflow.py re-export 共 7 个） |
| `harness/report_fallbacks.py` `_evidence_only_report` | `drivers_deep_research.py:12` 与 `report_pipeline` 共享 | —（共享能力，保留） |
| `harness/verifiers/`（deterministic 等） | `harness/contracts.py:111` | 3 个测试引用（最终 Contract 门，保留） |

### 3.3 入口选择字符串与配置

- `backend/app/schemas/api.py:37`：API 默认 `workflow_mode=deep_research`（旧路径可被选中的根因）。
- `backend/app/api/v1/health.py:71`：capabilities 同时广告两种 workflow。
- `frontend/src/types/api.ts:2`、`ChatPage.tsx:64,133,354`：前端仍提供双工作流选择。
- `RESEARCH_ORCHESTRATION`（`settings_sections.py:414`，默认 coreact_v1）：仅
  `chat_service.py:146` 在 `plan_execute_report` 时写入快照；消费点为
  `drivers_plan_execute.py:39,70`、`report_pipeline.py:175`、`coreact_support.py:81`。
- 脚本/评测：`scripts/evaluation_gate.py:49` 与 `scripts/evaluate_recovery_e2e.py:271`
  默认 `deep_research`；`evolution/evaluator.py:50` 默认 `plan_execute_report`。

### 3.4 保留能力（不得随删除迁移）

- GraphRAG 构建/索引/检索管线（`graph/`、`pipelines/ingestion`、`community/`、`build_knowledge_graph.py`）。
- web/scholar Provider（`retrieval/`、`search/`）与路由 `retrieval/router.py`。
- Evidence Ledger、ArtifactStore、Checkpoint/Lease/StateMachine、Reporter 卡片与
  MapReduce（`reporter/evidence_cards.py`、`mapreduce_sections.py`）、最终确定性 Contract。
- `planner/clarifier.py` 的澄清能力（T-406 BriefBuilder 复用）。
- `search/tool_registry.py` 的 `answer_validator` 注册（共享安全检查，非 reflection 专属）。

## 4. 关键实施 gate（引用 T-401 结论）

1. **G-ENTRY**：T-407 切换前，本清单中 3.1 全部入口必须改为唯一 coreact 工厂；
   `WorkflowMode.DEEP_RESEARCH` 分支、API 默认值与前端选择器一并归一化。
2. **G-DATA**：切换只影响新写入；历史读取（含 4 个 manual_review Run）保持原状；
   不自动取消/重启。若上线库出现带 coreact/staged checkpoint 的活动 Run，
   需先重跑盘点并另批处置清单。
3. **G-DELETE**：T-408 物理删除前必须复核 3.2 引用清单并重跑脚本（含 registry/
   字符串入口扫描——`tool_registry.py` 的 `deep_research`/`deeper_research`/`answer_validator`
   键与 prompt 文本提及均需逐项处理），仅专属且无调用方的文件可删；共享能力先抽取。
4. **G-CHECK**：数据库 `ck_runs_workflow_mode` CHECK 暂留；收窄属后续独立迁移。
5. **G-DEADCODE**：`agents/fusion_agent.py` 与 `integration/legacy_facade.py` 无生产入口
   调用方，仅根 CLI `search_without_stream.py` 的 `fusion` 选项在用；T-408 删除时须
   连同该 CLI 选项一并处理（脚本非生产入口，但其可用性是删除前置条件）。

## 5. 脚本与证据文件

- 盘点脚本：`scripts/inventory_coreact_migration.py`（dry-run，`--output` 写 JSON 清单，
  `--skip-references` 跳过静态扫描；引用扫描覆盖 src/backend/scripts/evals/tests/根目录
  散落脚本，排除 `.venv`/`vendor`/`.zcode-site` 镜像）。
- 示例输出：本地库盘点结果见本文 §2；静态引用扫描结果见 §3.2。
- 后续各阶段结束时须重跑脚本确认存量数据边界未被破坏（runs/checkpoints 计数不变）。
- **边界口径**：本仓库测试套件存在既有副作用——`tests/observability/test_observability.py`
  调用 `create_app()` 未指定 database_url，会在本地 `data/app.db` 产生新的
  deep_research/completed Run 行（fake driver，非旧引擎真实执行）。因此
  "计数不变"按以下口径核对，而非全表计数：
  1. `plan_execute_report` 行数保持 0；
  2. 既有 4 个 interrupted/manual_review Run 原状未动；
  3. checkpoint 编排标记分布不含 coreact_v1/staged 新增。
  （2026-11-11 T-402 收尾核对：plan_execute_report=0，interrupted=4 原状，checkpoint
  标记无 coreact 新增——增量行均为上述既有测试副作用。）
- 独立 code review（2026-11-11，两轮）结论：首轮 8 项脚本/文档发现全部修复并经复审
  逐项验证（标记启发式与快照实现对齐、非 dict state_json 崩溃防护、SQLite-only 表述、
  扫描范围补 tests/与根目录、标记桶对账 954/954、文档 stale 参数、WAL sidecar 说明、
  扫描自匹配排除）；复审另指出 4 项文档准确性问题（PostgreSQL 重跑指引、fusion_agent
  调用方表述、测试文件计数、tool_registry 行号），均已按复审意见修正。

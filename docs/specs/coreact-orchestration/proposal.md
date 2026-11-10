# Proposal: T-015 plan_execute_report 编排替换为 Co-ReAct 式步级验证控制

## 1. 背景与问题

前期调研（LLM Compass multi-agent 页面及其引用的 Anthropic 工程博客、Co-ReAct arXiv:2605.23590 及其 Apache-2.0 仓库、MiroThinker-H1 arXiv:2603.15726、Marco DeepResearch arXiv:2603.28376、QUEST arXiv:2605.24218）加上对本仓库的三路只读侦察，得出三条候选路线：

- A. 增强现有 F2 staged 编排（阶段后验证）；
- B. 新增第三 workflow 模式跑 Co-ReAct 步级循环；
- C. 在既有 `workflow_mode=plan_execute_report` 外壳内**替换**其编排为步级验证控制。

**用户决策（2026-11-10）：采用 C，只实现不做对比实验，定位为简历/作品集项目。**

当前 F2 的问题（代码证据）：

- 编排是"一次性规划 → 整批执行 → 阶段后验证"（`drivers_plan_execute.py` 的 `plan/execute/report`），验证结果无法影响中间检索动作；
- 持久化粒度是整个执行批次（`runtime_persistence.py:25-74` 在 `execute()` 返回后才写 ToolCall/Evidence/Checkpoint），崩溃即丢失整批进度、重复整批外部调用；
- 恢复规则把 `EXECUTING` checkpoint 视为"批次已完成"直接进 `REPORTING`（`runtime.py` `_safe_stage_after`），无法表达"执行进行到一半"。

## 2. 场景与用户

- 角色：项目维护者（简历/面试展示）。
- 场景：向面试官展示"把论文机制（步级 rubric 注入、per-criterion verdict、单次定向重试、全局证据链验证）落进一个带持久化/恢复/预算的生产级 Harness"。
- 目标流程：新 Run 走 `coreact_v1` 步级循环；存量 Run 按版本键继续走 legacy staged 编排直到排干。

## 3. 目标

- [ ] 新建的 `plan_execute_report` Run 默认以 `coreact_v1` 编排执行：初始候选计划 → 步级决策（观察驱动）→ 单动作执行 → 任务级 rubric 验证（确定性优先）→ 失败至多一次定向重试 → 每动作持久提交；报告后全局 `rubric_coverage` 检查进入现有 Completion Contract 通道。
- [ ] 断点恢复粒度从"整批"提升到"单个已提交动作之后"：崩溃后从下一个动作继续，已提交的 plan/task/toolcall/evidence 行无重复。
- [ ] 外部面完全兼容：`workflow_mode` 枚举、API schema、前端、DB CHECK 约束零改动；`config_snapshot["orchestration"]` 版本键分派，缺省视为 `staged_v1`（legacy）。
- [ ] F1 `deep_research`、fake drivers、skill 学习循环、既有恢复语义（legacy checkpoint → REPORTING）全部不变。
- [ ] 全量测试基线绿 → 每阶段保持绿。

## 4. 非目标

- 不做：rubric on/off 对比实验、质量提升百分比声明、A/B 开关。
- 不并行执行（v1 串行；现有 parallel 分支缺 stop_predicate，串行语义最清晰）。
- 不改：状态机枚举/转移、DB schema、API、前端、F1、`MultiAgentOrchestrator.run()`。
- 不引入：外部 agent runtime（MiroFlow/AutoGen/MetaGPT/FrontierAgent）、训练框架、向量库等新依赖。
- 不承诺：外部工具调用 exactly-once（崩溃窗口内可能重复 ≤1 个动作——严格优于原"整批丢失"）；不做 pre-invoke intent 行（记为已知限制，理由见 design §7）。
- 不改 `llm_gateway` 的 F2 failover 概念（命名冲突，代码/文档一律用 plan_execute_report/coreact 术语）。

## 5. 范围与影响面

| 层 | 文件 | 变更 |
|---|---|---|
| 核心模型 | `agents/multi_agent/core/rubric.py`（新） | TaskRubric/RubricCriterion/RubricVerdict + 确定性 rubric 生成 |
| 循环状态 | `harness/coreact_state.py`（新） | CoreActLoopState/PendingAction（版本化，存于 `workflow_state["coreact"]`） |
| 步级验证 | `harness/coreact_verifier.py`（新） | 确定性检查 + 可选预算门控的语义检查，结构化 verdict |
| 步级决策 | `harness/coreact_controller.py`（新） | 观察驱动选择下一动作 / ready_to_report，畸形决策有界兜底 |
| 驱动 | `harness/drivers_plan_execute.py` | 版本分派 + 步循环 + 重试 + 报告启发式适配；legacy 路径保留 |
| 协调器 | `agents/multi_agent/executor/worker_coordinator.py` | 新增公共 `execute_action()` 单动作适配器 |
| 运行时 | `harness/runtime.py` · `runtime_execution.py` | `_safe_stage_after`/`_load_context` 版本感知恢复；`step_commit` 回调注入（duck-typed） |
| 契约 | `harness/contracts.py` · `verifiers/deterministic.py` | 新增 `rubric_coverage` 检查 kind，纳入 locally_repairable |
| 配置 | `config/settings.py` · `backend/app/services/chat_service.py` | `RESEARCH_ORCHESTRATION`（默认 coreact_v1）+ 创建 Run 时戳记 |
| 文档 | `docs/RUN-FLOWS.md` | F2 节改写为真实新行为 |

- API/协议：无变化。数据库/迁移：无（检查 kind 存字符串列，Literal 扩展是代码级变更）。
- 测试：新增 `tests/harness/test_coreact_*.py`；既有 `test_harness_phase3.py`/`test_takeover.py`/`test_lease_heartbeat.py`/`test_worker_drain.py` 必须保持绿。

## 6. 约束与不变量

- 确定性安全门权威性不变：`SourcePolicy` 工具/来源白名单、Completion Contract、BudgetManager（`max_task_retries`/`max_replans`/token/tool 上限）、租约与心跳、幂等重放语义。coreact 的任何决策不得绕过。
- verifier 不得产生与确定性规则矛盾的 pass；LLM 输出畸形/超时 → 降级为纯确定性判定，永不因模型失败而 Fail 整个 Run。
- 每个动作至多一次定向重试；重试结果**替换**失败结果（Co-ReAct 语义），计数走现有 `consume_retry()`。
- rubric/verdict/LoopState 全部持久化进 `workflow_state`，快照恢复后行为一致；Evidence Ledger 仍是唯一证据事实源，rubric/verdict 只是元数据。
- 报告/验证预留 token（`REPORT_RESERVED_TOKENS`/`VERIFICATION_RESERVED_TOKENS`）不可被步级验证侵占；预算不足 → 走现有 `reserved_budget_fallback`。
- 每阶段独立 code review 通过后本地提交；不使用 Docker。

## 7. 风险与开放问题

| 问题 | 影响 | 处理方式 |
|---|---|---|
| continuation marker 误判导致 legacy Run 被新恢复规则截断 | 存量 Run 行为改变 | marker 必须同时满足"engine==coreact_v1 且 done==false"；legacy checkpoint（无 marker）保持旧映射；专项测试锁定两条路径 |
| LLM 决策死循环/畸形输出 | Run 卡死或浪费预算 | 决策上限（`COREACT_MAX_DECISIONS`）、连续无进展计数、畸形 JSON 最多重试 2 次、确定性兜底 ready_to_report |
| compact/reserved 报告启发式在增量计划下退化 | 报告质量 | 单动作即 compact 的条件保持不变；reserved 报告章节来源改为动作账本去重；专项测试 |
| 步间 pause/cancel 与租约接管竞态 | 双写 | 沿用现有协作式检查（heartbeat + 步间检查 + RunLeaseLost 静默让位）；租约 fencing 记为后续硬化项 |
| 步级验证 token 侵蚀报告预算 | 报告降级 | verify 语义检查仅在剩余 > 报告+验证预留+verify 配额时启用；否则纯确定性 |

无待用户决策的开放问题；C 的范围取舍已由用户确认。

## 8. 成功标准

- [ ] Given scripted-controller 测试，When 断言"动作1的观察改变动作2的选择 / 早停不绕过终验 / 失败后恰好一次重试 / 畸形决策有界终止"，Then 全部通过。
- [ ] Given 步循环中注入崩溃，When 恢复，Then 从下一个动作继续且 plan/task/toolcall/evidence 行无重复；Given legacy EXECUTING checkpoint，Then 仍推进 REPORTING。
- [ ] Given 新 Run，When 检查 `config_snapshot["orchestration"]`，Then 等于 `coreact_v1`；Given 缺该键的快照，Then 走 legacy 路径。
- [ ] 验收命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q` 与 P0 基线一致（新增测试外全绿）；每阶段 acceptance.md 记录命令、结果摘要、review 结论。

## 9. 决策状态

- 状态：`proposed`（待用户批准 design 后转 approved；本文件随首个提交入库）
- 用户确认：采用 C、只实现不实验、简历定位（2026-11-10）
- 日期：2026-11-10

# Design: T-017 单一 coreact 路径与职责收敛

- 状态：`draft`；关联 proposal.md；日期：2026-11-11。
- 仅为目标设计，尚未实现；无实验性优越性结论。

## 1. 现状与真实接入点

| 当前路径 | 现有文件/接口 | 目标变化 |
|---|---|---|
| HTTP/worker 工厂 | `backend/app/services/run_service.py` 的 workflow_factory；`harness/bootstrap.py` 工厂 | 一个 coreact 工厂，两处共用，无 else 落入旧引擎 |
| 入口选择 | `harness/contracts.py` WorkflowMode；`backend/app/schemas/api.py`；`frontend/src/types/api.ts`、`pages/ChatPage.tsx`；health capabilities | 新写入唯一模式，旧请求标识在边界归一化，历史 DTO 可读 |
| 规划 | `planner/base_planner.py` generate_plan / _simple_plan / _ensure_reflection_task，clarifier/task_decomposer/plan_reviewer | 保留可选澄清与简单请求捷径；BriefBuilder 输出研究契约，去掉 reflection task 和完整 DAG 预规划 |
| 控制循环 | `harness/coreact_loop.py` / `coreact_controller.py` | Controller 只选动作/批次/准备报告；移出执行/重试/状态合并 |
| 执行 | `executor/worker_coordinator.py` execute_action、`coordinator_tasks.py` _execute_isolated_task/_merge_isolated_state、`coordinator_modes.py` | 提取统一 ActionRunner；复用隔离合并思想，不让 driver 调私有方法 |
| 报告 | `harness/report_pipeline.py`、`reporter/base_reporter.py` | 适配 ResearchBrief 与已接受动作结果，保留 Cards/MapReduce/预算兜底 |
| 验证 | `harness/coreact_verifier.py`、`harness/contracts.py`、`verifiers/deterministic.py`、`recovery.py` | 局部反馈、可选语义判定与最终硬门分开；失败分流可解释 |
| 状态/恢复 | `coreact_state.py`、`driver_adapters.py`、`runtime.py`、`runtime_step_hooks.py`、`runtime_persistence.py` | 显式多动作状态与版本适配，保留外层 RunStatus 生命周期 |
| 模型 | `models/model_tier.py`、`integration/multi_agent_factory.py` | 冻结 brief/controller/reporter/verifier 模型身份，角色名称变更兼容旧映射 |

## 2. 目标组件边界

```text
API / Worker / CLI → CoreActFactory → CoreActDriver
                                ├─ BriefBuilder（可复用 Clarifier）
                                ├─ ResearchController
                                ├─ ActionRunner
                                │    ├─ SourcePolicy + Budget admission
                                │    ├─ action intent / lease checks
                                │    ├─ isolated RetrievalExecutor + Provider
                                │    ├─ coordinator-owned stable merge
                                │    └─ LocalVerifier + bounded retry
                                └─ ReportService（现有 Reporter）
Harness：状态机 / Lease / Checkpoint / Ledger / Artifact / final Contract
```

不再设置必经 Reflector Agent：局部验证输出反馈，Controller 决定研究方向，ReportService 负责修稿，Harness 决定最终退出。可选 semantic verifier 是依赖接口，不是新增自由交谈 agent。

## 3. 数据与接口（拟议，实施时逐项冻结）

- `ResearchBrief`：brief_id/version/hash、goal、questions、output_requirements、allowed_sources、required_sections、acceptance_criteria、budget_policy。冻结 source_mode，模型不得扩大来源。需求修订须显式版本化。
- `ActionSpec`：action_id、task_id、target_question_ids、query、tool、depends_on、rubric_id、attempt、budget_allocation。可由现有 TaskNode 适配，不强制同步改所有存储模型。
- `ActionRubric`：objective、expected_output、evidence_requirements、required/optional criteria、verifier_kind/version。生成受确定性边界约束，不以中文滑窗词作为事实核验。
- `ActionResult`：action_id、attempt_id、status、records、evidence_refs、delta、verdict、usage、observation_ref。
- `ObservationSummary`：带 evidence_ids 的 findings、contradictions、unanswered_questions、suggested_followups；摘要长度和条数上限可配置。第一版可确定性抽取摘要；启用模型摘要时记录版本、成本并禁止生成账本外 ID。
- `EvidenceAssessment`：claim_id/evidence_ids、状态 available/supported/disputed/unresolved、check_kind、verifier/version、reason、next_action。supported 表示某项检查支持指定 claim，不等于独立事实认证。
- `ExecutionState`：schema_version、implementation_version、next_operation、inflight_actions、committed_action_ids、retry_counts、active_result_refs、observations、assessments。保留旧 LoopState 的只读升级适配，但不启动旧引擎。

接口约定：Controller.decide(brief, observations, assessments, remaining_budget) → action(s)/ready；ActionRunner.run_batch(actions, frozen_context) → ActionResult[]；Verifier.verify_action → verdict；Reporter.generate(brief, accepted_evidence) → candidate_report。用户/内部输出契约详见同目录 `output-contract.md`，其中 Markdown 报告、ResearchRunResult、ActionResult、ObservationSummary 的字段和状态语义为单一权威。

## 4. Rubric 与观察输入

- Controller 可见研究级契约、动作目标、摘要与未解决问题，不直接接收无限原始输出。
- 有模型参与的 worker/摘要组件通过内部 messages/context 接收动作 rubric；Provider.search 仅接 query/top_k/filters 等公开参数。
- 纯检索 Worker 不伪称 LLM 自治 agent：动作契约通过查询、过滤、后处理与验收落实。
- 外部网页/论文/工具文本作为不可信数据单独包裹；摘要只能引用当前 Run 的真实 evidence_ids，禁止将外部文字提升为系统指令。
- 结构检查先行；语义 verifier 可选且受预算/超时门控，返回 pass/fail/unknown。不可用或非法输出标 unknown；若检查被配置为 required，unknown 不得按 pass，预算不足只能降级为明确受限报告或诚实失败。

## 5. 执行、并发与重试所有权

1. Controller 提议有界小批次；ActionRunner 验证依赖就绪、source/tool 白名单和 query 去重。
2. 并发上限取配置、Run.max_concurrency、Provider/Gateway 限额和可用预算的交集。为 in-flight 动作预留 tool/token/retry 额度，不能等执行结束后才决定是否超发。
3. 每动作在冻结的上下文副本上执行，返回 delta；共享 ledger/事实不被各线程直接覆盖。协调者按 action sequence 稳定合并，以来源字段维持每动作 provenance；全量 dict.update 不得覆盖其他任务局部结果。
4. 每个 rubric/action 最多一次定向修正；传输重试与研究重试分开计数。保留所有 attempt 的记录、调用与成本；active_result_refs 指向最新有效尝试，禁止删掉失败历史。
5. 最终修稿由 ReportService 执行；证据缺口反馈给 Controller 补检索，不重新跑旧 Planner/Reflector 流水线。
6. 暂停/取消停止新派发，当前动作按既有可协作语义收尾；取消优先。lease 失效停止合并/提交，不能让后台线程回调继续写入。check-then-write 不是原子 fencing，不声称消除 TOCTOU。

## 6. Intent、提交与恢复

- 派发前保存**全部批内动作**、rubric、attempt、next_operation 与 intent，不能只保存主动作。
- 合并后对每个动作记录 observation/result/usage 的提交状态，使用稳定 action_id/attempt_id/tool_call_id。数据库行幂等不等于外部调用 exactly-once。
- 未执行/结果未知的 intent 以相同动作身份恢复；已提交动作不得重做；prepared 外部结果未知必须按工具是否只读/支持幂等明确处理，禁止承诺恰好一次。
- 新状态显式区分 execution ongoing 与 complete，不能只依据 RunStatus.EXECUTING 推断下一阶段。
- P0 盘点旧 Run；checkpoint 转换工具默认 dry-run、输出原 hash/目标版本/映射结果，不原地破坏原 checkpoint。不可映射的旧 Run 进入人工处理清单，不清空、不默默换引擎。
- 灰度切换只针对唯一 coreact 新版本；迁移验证期间可以保留纯数据适配器，不能以兼容为名长期运行第二引擎。回滚前先停止新派发、导出备份并确认版本读取能力，不把新快照交给旧引擎。

## 7. 删除/合并清单与门禁

| 候选模块 | 处理 | 删除前必须确认 |
|---|---|---|
| `drivers_deep_research.py`、`deep_research_agent.py` 及其专属 Mixin | 移除独立生产入口；仅专属且无调用方时物理删除 | F1 API/CLI/工厂、旧快照、search tool 嵌套调用与 import |
| `search/tool/deep_research/`、`deeper_research/` 与原工具壳 | 优先去除 coreact 的嵌套研究调用；按复用图决定删除或抽取能力 | GraphRAG CoE/检索策略不得随之删除；不能把深搜工具与独立 workflow 混为一谈 |
| `planner/plan_reviewer.py`、`task_decomposer.py`、BasePlanner 旧流程 | 新路径替换为 BriefBuilder；无 CLI/评测调用后删除旧实现 | 澄清、简单问答、研究问题覆盖仍有实现 |
| `executor/reflector*.py`、`validation_tool.py` 的旧流程专属部分 | LocalVerifier/Controller 接管后删 reflection task 生成、注册与专属 retry | validation_tool 的其它调用方；不删除共享安全检查 |
| `MultiAgentOrchestrator.run`、OrchestratorResult/Metrics、旧 staged 分派 | 显式组件依赖替代整流程外壳 | standalone CLI、工厂、评测/Skill 调用；必要时薄 facade，不保留运行时 |
| 两套 parallel/merge/retry 与机械拆分 Mixin | 统一 ActionRunner，按职责迁移后删除重复逻辑 | K=1/K=2 真实 executor 隔离、预算/恢复/取消测试 |
| `RESEARCH_ORCHESTRATION=staged_v1` 选择、frontend 双工作流选择 | 移除配置分支与选择 UI；旧参数只在边界归一化 | 文档、env.example、API/OpenAPI、health capabilities、模型戳记 |

默认只保留一种 coreact 生产实现；最终物理删除清单须由静态扫描、调用探针、迁移盘点和独立 review 共同确认，不按表格盲删。

## 8. 数据库/API与模型兼容

- 初期不收窄历史表的 workflow_mode CHECK；新建请求归一化为 plan_execute_report，历史读取 DTO 兼容旧值。若未来重命名为 coreact，另立迁移，不在此顺手做。
- model_snapshot.role_models 冻结实际 brief/controller/reporter/verifier 身份；原 planner key 可在适配层映射 brief，不现算覆盖历史模型选择；无语义 verifier 时显式标 disabled，避免审计伪归因。
- API/前端只展示唯一研究入口；历史页保留原流程标识与新引擎版本说明。事件 schema 变更应向后兼容，工具进度与报告查看不能回归。

## 9. 验证与已知取舍

正确性测试优先，无需性能/质量比较实验。覆盖：真实工厂入口、隔离/稳定合并、非空观察影响下一动作、rubric 不外发、required semantic unknown、批次 intent 崩溃/重试恢复、lease/pause/cancel、报告修复与最终合同、旧已完成 Run 可读、旧活动快照转换或安全拒绝。沿用本项目 pytest/前端构建与 CI。

不采用一次性删旧数据库/无版本替换、全量工具输出塞入 prompt、额外自由对话 Reflector、移植外部 Agent Runtime、纯行数 Mixin 分割。保留现有报告上下文压缩与 GraphRAG，避免破坏已独立验证的能力。

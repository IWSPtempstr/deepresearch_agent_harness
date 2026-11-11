# Tasks: T-017 单一 coreact 路径与职责收敛

## 执行规则

本次只写规范，下面全部为未来实施任务，状态均 `pending`。
每阶段：实现 → 正确性测试 → 独立 code review → 修复/复审 → 本地提交；不得用实现本身代替批准、测试或验收。不自行推送/部署、不自动清库/取消旧 Run；Docker 使用前询问。新测试路径是拟新增路径，命令仅在实现后可执行。

通用门禁：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`；变更文件 Ruff F821/F811/F401；`git diff --check`。不得把既有测试数量直接抄作本规范结果。

## T-401 — 基线、调用图与存量数据盘点
- 状态：done
- 前置：none
- 目标：形成唯一入口切换与删除的事实清单。
- 涉及：`harness/bootstrap.py`、`backend/app/services/run_service.py`、`harness/contracts.py`、`persistence/models.py`、CLI/前端入口、旧 driver/planner/reflector/tool 的 importer；未来新增迁移盘点脚本。
- 动作：记录 HEAD、全量测试基线；静态/动态调用图；只读列出旧 workflow/engine 的终态与未终态数量、checkpoint 版本；记录候选删除文件与保留 GraphRAG 能力。
- 验收：运行通用门禁；盘点脚本默认 dry-run，人工确认无写库/秘密泄露。
- 预期证据：基线日志、调用/引用清单、旧 Run 处理清单与实施 gate。
- 实际证据（2026-11-11，main@dd42137）：全量 pytest 376 passed/4 skipped；新增只读盘点脚本 `scripts/inventory_coreact_migration.py`（dry-run 默认，无写库路径，不打印正文/秘钥）；本地库盘点 163 runs（159 completed / 4 interrupted 且无 checkpoint→manual_review）、954 checkpoints（schema_version 全 1，编排标记 deep_research=954，标记桶对账 954/954）；入口矩阵/引用清单/保留能力/实施 gate 见 `docs/specs/coreact-consolidation/inventory.md`（含 5 项 gate：G-ENTRY/G-DATA/G-DELETE/G-CHECK/G-DEADCODE）；独立 code review 8 项发现（标记启发式、崩溃防护、扫描范围等）已全部修复并复审确认。通用门禁：pytest 376 passed；ruff 通过；`git diff --check` 干净。
- 风险：若存在不可安全转换的活动 Run，先阻塞切换，另获数据处理批准。（本地库的 4 个 manual_review Run 无执行现场，保持原状，不阻塞后续实现，但上线切换前须重跑盘点出具该环境清单。）

## T-402 — ResearchBrief 与单一职责接口
- 状态：done
- 前置：T-401
- 目标：冻结 Brief/ActionSpec/ActionResult/ObservationSummary/EvidenceAssessment/ExecutionState 接口，并冻结 `output-contract.md` 中的用户报告与内部动作结果协议。
- 涉及：现有 `core/plan_spec.py`、`harness/coreact_state.py`、拟新增 `harness/research_brief.py` / `action_models.py`、`docs/specs/coreact-consolidation/output-contract.md`。
- 动作：定义版本/hash/证据引用/next_operation/inflight 集合；旧 TaskNode/LoopState 的数据适配器；定义模型角色冻结规则；实现 ResearchRunResult 的 API/前端兼容视图设计。
- 验收：拟新增 `tests/harness/test_coreact_consolidation_models.py`，测试 JSON 往返、未知版本拒绝、来源约束、并行/重试身份和旧 payload 转换；验证 Markdown 报告状态、ActionResult execution/verification 分离；通用门禁。
- 预期证据：schema/兼容矩阵与测试日志。
- 实际证据（2026-11-11）：新增 `harness/research_brief.py`（ResearchBrief/BudgetPolicy/EvidenceAssessment/merge_assessments + canonical_role_models 角色冻结规则，schema_version>1 拒绝）、`harness/action_models.py`（ActionSpec/ActionRubric/ActionResult/ObservationSummary/ExecutionState + TaskNode/TaskRubric/CoreActLoopState 只读适配器，LegacyStateUnsupported 显式拒绝未知版本/畸形 pending）、`harness/run_result.py`（ResearchRunResult，覆盖 RunStatus 全部 16 态，completed⇒verification passed、failed draft 隔离、draft verified=false 强制）；`output-contract.md` 状态更新为已冻结 schema。`tests/harness/test_coreact_consolidation_models.py` 56 项测试覆盖全部验收点（含 inflight_rubrics/retry_reasons 保留、active_result_refs 仅引用已提交动作、attempt_id 派生校验、孤儿键拒绝）。通用门禁：pytest 432 passed/4 skipped（376 基线 + 56）；ruff F821/F811/F401 通过；`git diff --check` 干净。独立 code review 一轮 15 项发现（3 major：done→complete 语义反转、in-flight rubric/重试反馈丢失、缺 cancelling 态；12 minor/nit）全部修复并复审 APPROVE。
- 风险：不把未知旧状态自动当空状态重新执行。（适配器对未知版本/畸形 pending 一律抛 LegacyStateUnsupported；legacy done 态映射为 report 而非 complete。）

## T-403 — 统一隔离 ActionRunner
- 状态：done
- 前置：T-402
- 目标：替代共享状态 coreact gather 与重复调度/重试逻辑。
- 涉及：`executor/worker_coordinator.py`、`coordinator_tasks.py`、`coordinator_modes.py`、`harness/coreact_loop.py`、拟新增 `harness/action_runner.py`。
- 动作：提取公开单动作/批次接口；输入副本隔离、结果 delta、稳定合并；预派发预算预约、来源/去重门；统一 retry 所有权，保留所有 attempt 与成本。
- 验收：拟新增 `tests/harness/test_action_runner_isolation.py`；K=2 使用 barrier 证明重叠而非仅断言执行两个；不同完成顺序不改变合并结果；真实执行器+fake provider，测试预算超限零派发、任务失败、共享键冲突与每动作一次修正；通用门禁。
- 预期证据：隔离/并发/预算/重试正确性日志。
- 实际证据（2026-11-11）：新增 `harness/action_runner.py`（公开 `ActionRunner`：admit 预派发准入——依赖/工具白名单/query 去重/DispatchBudget tool 额度预约，`execute_isolated` 深拷贝隔离执行，公开 `merge_isolated_state` 从 coordinator_tasks 逐行提取（含 tracker 子树按键合并强化），`run_and_merge`——并发执行→sequence 顺序稳定合并→逐动作验证→每动作至多一次定向修正，try/finally 保证预约释放，合并后副本即弃）；`coreact_loop.py` 执行段重写为 runner 调用（admit→intent→run_and_merge→记账，恢复重放跳过 admit/修正，pending 游标/step_commit 语义保留）。`tests/harness/test_action_runner_isolation.py` 13 项测试：threading.Barrier(2) 在真实 RetrievalExecutor+FakeProvider 内证明 K=2 重叠、0.2s 延迟差反序完成下合并结果一致、共享键 sequence 裁决且局部键不丢、预算零派发与部分准入边界、失败记录保留、修正恰一次/放弃修正、批内取消零派发、worker 线程对象身份隔离证明。通用门禁：pytest 445 passed/4 skipped（432 基线 + 13）；ruff F821/F811/F401 通过；`git diff --check` 干净。独立 code review 结论 APPROVE（13 项 minor/nit 中 7 项随本阶段修复：部分拒绝可观测性、预约 try/finally、副本即弃、record=None 不发 completed、批内取消测试实化、边界准入测试、来源注释更新；其余记账见下）。
- 风险：不能用 GIL 下 list.append 原子性作为线程安全证明。（测试以 barrier/对象身份/结构隔离证明；合并仅在事件循环协调段发生。）遗留记账：failure_reasons 仅记最终 verdict（接受的行为统一）；current_task_id 不再由 coreact 写入（无读者，docstring 注明）；tool_call_history 去重键继承 staged（无功能影响）；token/retry 额度预约未覆盖（T-404 扩展 DispatchBudget）。


## T-404 — 多动作 intent 与耐久恢复
- 状态：done
- 前置：T-403
- 目标：派发前保存全批次动作，恢复不丢次要动作或重做已提交结果。
- 涉及：`runtime_step_hooks.py`、`runtime_persistence.py`、`runtime.py`、`coreact_state.py`、trajectory repositories。
- 动作：稳定 action/attempt/tool ID、全量 in-flight intent、逐动作提交/预算核销与 next_operation；pause/cancel/lease 检查；旧 checkpoint 适配器，显式 unsupported 处理。
- 验收：拟新增 `tests/harness/test_action_runner_recovery.py`；在 intent前后、调用后/提交前、部分批次提交后、retry 中注入中断；新 Runtime 恢复；断言工具调用/证据/预算/事件，不仅测试 helper predicate；通用门禁。
- 预期证据：故障点矩阵、持久行/事件/费用计数及残余窗口说明。
- 实际证据（2026-11-11）：`coreact_state.py` 扩展 `pending_batch`（全批次 in-flight intent）+ `committed_action_ids`（逐动作提交）+ `next_operation`（显式 execute/report，driver snapshot 序列化）；恢复归一化逐项校验（畸形项剔除并计入 failure_reasons，全无效→decide，旧单动作快照兼容回退）。`coreact_loop.py`：intent 写全批次、run_and_merge 新增 `commit_fn`（逐动作提交，action_commit 钩子只持久化不发布 step 事件）与 `record_fn`（verdict 元数据提交前写入——恢复后 rubric_coverage 门不被削弱）、修正游标即时持久化、重放防御性跳过已提交项（重试游标始终重放）。`action_runner.py`：commit_fn/record_fn 挂载；RunLeaseLost/BudgetExceeded 从 worker 传播（不降级为动作失败记录）。稳定 tool ID：TaskNode.attempt 字段 + retrieval_executor tool_call_id=(run_id,task_id,attempt) 派生（重放行幂等，重试新 ID 保留全部尝试）。T-402 适配器改用 inflight_items（迁移视图不丢次要动作）。`tests/harness/test_action_runner_recovery.py` 8 项：intent 后崩溃（批次完整落盘、零 tool/evidence 行）、intent 后崩溃恢复（次要动作不丢、恢复不走 controller）、行已落库/checkpoint 未写窗口（稳定 ID 行幂等=1、事件不重复、外部调用重复 2 次如实记录、计费不双收）、部分批次提交后恢复（已提交不重做、rubric_coverage/min_evidence 通过）、retry 中崩溃（attempt=2 游标重放、task_retries 不双计）、批次游标校验 3 项。故障点矩阵：intent 后/行后 checkpoint 前/部分提交后/重试执行中（4 点已注入测试）+ 重试合并后提交前（游标收缩先于持久化的顺序保证，分析覆盖）——均无动作丢失窗口；游标收缩先于持久化，checkpoint 恒保守。通用门禁：pytest 453 passed/4 skipped；ruff（变更文件）通过；git diff --check 干净。独立 code review 首轮 2 major（verdict 元数据提交时序、稳定 tool ID 缺失）+ 7 minor/nit 全部修复并复审。
- 风险：不宣称外部 exactly-once 或已消除原子 fencing 缺口。（外部调用在行后/checkpoint 前崩溃窗口内重复一次，如实断言；内存计费可随窗口丢失（低估），重放不双收。pause/cancel/lease 检查保持在动作边界（lease_probe/pause_probe 未改动）。已知行为变化：确定性 tool ID 使 staged reflection 重试的计费从每次尝试一行变为同 ID 去重一行（低估方向、证据行不受影响；staged 引擎将在 T-407/408 退役）。逐动作提交的 _persist_execution 为 O(N²) 重扫（K≤4 批次可接受，性能不作为目标）。）


## T-405 — 证据摘要与动作 rubric 指导
- 状态：done
- 前置：T-404
- 目标：Controller 从真实研究发现决策，动作执行明确目标/输出/来源/边界。
- 涉及：`coreact_controller.py`、`coreact_verifier.py`、`coreact_support.py`、`core/rubric.py`、`executor/base_executor.py`、ReportService 输入。
- 动作：生成有界 ObservationSummary；claim-scoped assessments；rubric 进入内部执行上下文但不进 Provider 参数；可选 semantic verifier pass/fail/unknown 与预算门；报告安全降级规则。
- 验收：拟新增 `tests/harness/test_coreact_observation_contract.py`；改变证据内容而不变计数会改变 scripted controller 决策；过滤账本外 ID、prompt injection、工具参数泄露；required unknown 不得通过；状态去重与冲突迁移；通用门禁。
- 预期证据：输入/输出协议、摘要边界/费用与安全测试日志。
- 实际证据（2026-11-11）：`coreact_support.py` 新增 `_coreact_build_observation`（账本过滤+去重+有界的 ObservationSummary，findings 携带真实 evidence_id；无证据不造 finding）、`_coreact_record_assessments`（claim 级评估迁移：supported 带 deterministic_proxy 归因并注明"非独立事实认证"，unresolved 带补证建议；merge_assessments 后写优先；按 updated_step 保留最近 12 条；损坏条目剔除）、`build_semantic_judge`（依赖接口：JSON 三态归一化，畸形/异常→unknown）、`coreact_semantic_gaps`（报告降级依据）。`coreact_state` observations/assessments 持久化 + 恢复时逐条校验（畸形剔除不注入 prompt）。`coreact_controller`：DecisionContext 增 observations/assessments；prompt 新增"研究发现摘要"段（显式标注外部数据非指令，条数/长度有界）。`coreact_verifier`：语义三态 pass/fail/unknown + 预算门 + required 模式（unknown 的一切来源——异常/畸形/预算门/判定器缺失——均追加失败判罚，不得按 pass；语义 fail 不进权重分母但拉低通过判定；pass 不升级确定性 fail）。`action_runner`：rubric 注入隔离副本 context_snapshot（内部通道，共享快照不变，Provider 参数不泄露）。`report_pipeline`：语义缺口 → 报告追加"局限（语义核验）"节。设置：COREACT_SEMANTIC_ENABLED（默认关闭→not_run；开启后 rubric 携带可选语义检查，主路径 optional）。`tests/harness/test_coreact_observation_contract.py` 20 项覆盖全部验收点（同计数不同内容改变决策上下文且互相排除、账本外 ID 过滤、注入文本仅在数据段且解析仍需严格 JSON、Provider 参数白名单+query 不携带 rubric、required unknown 四来源矩阵、语义 pass 不升级确定性 fail、fail 不进权重、评估去重/冲突迁移、checkpoint 往返+畸形剔除、共享快照不变、报告降级+无缺口不受影响）。通用门禁：pytest 473 passed/4 skipped；ruff（变更文件）通过；git diff --check 干净。独立 code review 首轮 3 major（判定器缺失分支漏判罚、语义接线 no-op、报告降级未实现）+ 5 minor/nit 全部修复；复审确认。
- 风险：关键词检查仍仅为代理，不升级为事实认证。（supported 一律携带 check_kind/verifier 归因且 reason 注明"非独立事实认证"。已知窗口：崩溃于逐动作提交后、批次收尾前时，已提交动作的观察/评估不在恢复游标中重建（有损视图语义，证据仍在账本）；语义判定器/控制器 LLM 消耗不进 llm_tokens 计费（与既有 controller decide 一致，预算门为咨询性）。）

## T-406 — BriefBuilder 替换重复规划与 Reflector
- 状态：done
- 前置：T-405
- 目标：Controller 成为唯一下一动作决策者，移除无效 reflection 候选。
- 涉及：`planner/base_planner.py`、`clarifier.py`、`task_decomposer.py`、`plan_reviewer.py`、`executor/reflector*.py`、工厂与 coreact driver。
- 动作：BriefBuilder 生成问题/验收/报告契约，保留可选澄清及简单请求捷径；停止自动 reflection task、核心工厂不注册旧 Reflector；改 final evidence 缺口反馈 Controller，而非回旧 Planner。
- 验收：拟新增 `tests/harness/test_coreact_brief_flow.py`；澄清/摘要/多方向研究、三来源模式、无 reflection 派发、ReportService 输入与预算 fallback；通用门禁。
- 预期证据：无旧规划/反思入口调用的运行 trace、角色模型一致性断言。
- 实际证据（2026-11-11）：新增 `planner/brief_builder.py`（BriefBuilder：与 BasePlanner.generate_plan 同签名；简单请求捷径与旧实现同参数；多方向候选图全部就绪、来源冻结、无 reflection/custom/deep_research 类型；evidence 缺口（min_evidence/no_source_evidence/insufficient_source_evidence/claim_support/source_match）→ 补充检索候选反馈 Controller；可选澄清（复用 Clarifier，无 LLM 跳过）+ 澄清历史记录；ResearchBrief 契约随候选图生成并在 replan 时显式版本化（同 goal 复用、变更 with_revision +1））。工厂新增 `create_coreact_bundle`（planner=BriefBuilder；worker 不注册 ReflectionExecutor——注入的 worker 亦被清洗；角色 brief/controller/reporter/verifier 走 canonical 映射；OrchestratorConfig 与 BriefBuilderConfig 由 settings 派生）。入口接线：run_service HTTP/worker 路径与 bootstrap CLI 路径均改用 create_coreact_bundle；bootstrap 为 plan_execute_report 写入 orchestration 戳记（与 chat_service 同规则）；coreact_controller.decide 增加 llm-None 守卫（离线直落确定性兜底，llm_disabled 标注）；model_tier 增加 brief(complex)/verifier(standard) 角色 + 历史 planner_model→brief_model 兼容映射（不覆盖历史选择）。`tests/harness/test_coreact_brief_flow.py` 12 项：简单捷径、多方向无 reflection、三来源模式、缺口补充候选、澄清门（阻塞/放行）、brief 契约冻结、工厂装配+注入 worker 清洗、factory plan 端到端、coreact 全流程无 reflection 派发 trace、缺口 replan 后补充候选**实际可派发**（executed==["web_search"] 且 query 含"补充证据"）、预算 fallback 真实执行 `_build_reserved_budget_report` 且 rubric 随 Contract、coreact_rubrics 输入。staged 路径（BasePlanner/_ensure_reflection_task/ReflectionExecutor）零改动；无文件物理删除。通用门禁：pytest 485 passed/4 skipped；ruff（变更文件）通过；git diff --check 干净。独立 code review 两轮：首轮 2 major（run_service 注入 worker 绕过"不注册 Reflector"、bootstrap 入口缺 orchestration 戳记）+ 8 minor/nit；二轮 Finding A（replan 测试空断言）+ 2 nit——全部修复并复审 APPROVE。
- 风险：只有共享检查完整迁移且调用者审计通过才能物理删除文件。（本阶段零删除。显式遗留至 T-407：chat_service 冻结 model_snapshot.role_models 仍为 ("planner","reporter","controller") 三角色、不含 verifier——HTTP 路径新 Run 的冻结快照需在 T-407 model snapshot 工作中纳入 brief/verifier 角色命名（模型身份经 planner→brief 别名保持 recorded==actual）。）

## T-407 — 单一入口切换与历史兼容
- 状态：done
- 前置：T-406；T-401 的旧 Run 处置 gate 通过
- 目标：所有生产入口只创建 coreact，不再运行 F1/staged 引擎。
- 涉及：`run_service.py`、`bootstrap.py`、API schemas、capabilities、前端 ChatPage/types、model snapshot、迁移工具。
- 动作：新请求旧标识在边界归一化；保留历史读取；dry-run 检查并转换可映射快照；不可映射活动 Run 安全拒绝且列人工处理，禁止自动取消；移除 RESEARCH_ORCHESTRATION 的旧路径选择。
- 验收：拟新增 `tests/api/test_coreact_only_entry.py` 与 `tests/harness/test_coreact_legacy_adapter.py`；HTTP/worker/CLI/Skill eval 全入口 trace 同一路径、OpenAPI/前端一致；历史报告可读、旧快照可转换或明确拒绝；`npm --prefix frontend run build` + 通用门禁。
- 预期证据：入口矩阵、dry-run/备份回滚记录、前端构建、历史读取测试。
- 实际证据（2026-11-11）：入口矩阵切换完成——(a) API：`MessageSend.workflow_mode` 默认 plan_execute_report + before 校验器把旧标识 deep_research 边界归一化（历史读取返回持久层原值）；(b) run_service/bootstrap 工厂删除 DEEP_RESEARCH 分支（无 else 落入旧引擎），存量活动 deep_research Run 显式抛 `AppError(LEGACY_WORKFLOW_UNSUPPORTED)`（新增业务错误码；拒绝先于 Provider 构建），Runtime 落诚实失败并保留业务错误码（AppError error_code 不退化成类名）；(c) RecoveryManager：旧模式活动 Run 不进入自动恢复（保持原状，warning 记录人工处理清单）；(d) capabilities 只广告 `plan_execute_report`；(e) chat_service 冻结 role_models 含 brief/controller/reporter/verifier（T-406 遗留项落地）；(f) RESEARCH_ORCHESTRATION 收窄为单选 coreact_v1（staged 快照仍按戳记只读恢复）；(g) 前端 ChatPage 移除工作流选择器（唯一入口默认 plan_execute_report，历史 Run 只读展示）。迁移工具：`scripts/convert_legacy_checkpoints.py`（默认 dry-run 清单；--apply 对可映射 coreact 游标**追加**归一化 checkpoint 行——原行与原 hash 保留、不原地破坏；staged/deep_research/未来版本→待人工处理）。新增测试 15 项（API 归一化/默认/拒绝未知、capabilities 单入口、工厂显式拒绝+错误码审计、恢复排除 legacy 且原状、历史完成 Run 可读、旧单动作快照恢复适配、转换工具分类矩阵/dry-run 零写入/--apply 追加行、chat_service 角色戳记、OpenAPI 默认值一致）。前端 `npm run build` 通过。通用门禁：pytest 500 passed/4 skipped（485 基线 + 15）；ruff（变更文件）通过；git diff --check 干净；存量数据边界按更新后口径核对（inventory.md §5）。
- 风险：任何破坏性数据操作必须另获批准；数据库历史 CHECK 不随意收窄。（本阶段零破坏性数据操作：--apply 仅追加行且默认 dry-run；4 个 manual_review Run 原状；无 CHECK 收窄。flaky 备注：tests/llm_gateway/test_gateway.py::test_slot_limiter_queues_then_times_out 为既有计时敏感测试，与本阶段无关，单跑通过。）


## T-408 — 删除旧专属模块与整流程外壳
- 状态：done
- 前置：T-407
- 目标：最终仅 coreact 生产实现，不保留旧引擎作 fallback。
- 涉及：design §7 删除候选表、`MultiAgentOrchestrator.run` 及专用模型、旧 driver/planner/reflector/scheduler/Mixin、registry/__init__/测试/文档。
- 动作：逐文件列删除原因、已迁移职责、所有 importer；共享搜索/GraphRAG 能力先抽取再删除旧壳；最小 facade 只导出新实现或数据适配器；清理死配置与测试，不能删核心安全测试凑绿。
- 验收：静态检查旧生产 dispatch/reflection 自动生成消失；import smoke、CLI、三 source mode 端到端；保留 GraphRAG 构建/检索专项测试；通用门禁与独立删除清单 review。
- 预期证据：删除/保留清单、调用图、无失联 import 及能力覆盖日志。
- 实际证据（2026-11-11）：**删除 42 个文件**（git rm 可追溯；合计 −7,831 行，提交整体 −8,891 行含修改文件改写），逐项理由/职责迁移：
  (1) 引擎 driver/agent：`harness/drivers_deep_research.py`、`agents/deep_research_agent*.py`×4——独立 deep_research 引擎已于 T-407 入口退役（唯一 importer workflow.py re-export 同步清理）；
  (2) 嵌套研究工具：`search/tool/deep_research/`×6、`deeper_research/`×11、`deep_research_tool.py`、`deeper_research_tool.py`——仅服务退役引擎；GraphRAG 检索策略在 `retrieval/graphrag_provider.py` 与 graph/ 管线（未动，专项测试保留）；
  (3) 旧规划：`planner/base_planner.py`、`task_decomposer.py`、`plan_reviewer.py`——BriefBuilder 接管；PlannerResult 迁至 `planner/result.py`（避免 plan_spec↔clarifier 循环导入）；CLARIFY_PROMPT 保留（Clarifier 复用）；
  (4) 反思：`executor/reflector*.py`×5、`search/tool/validation_tool.py`——T-401 调用图确认 answer_validator 仅 reflector 消费（registry 条目同步删除）；`executor/research_executor.py`（staged 专属 deep_research 任务类型）；
  (5) 整流程外壳：`MultiAgentOrchestrator.run`+OrchestratorResult/Metrics（orchestrator.py 重写为组件方法 plan/execute_action/report）；`worker_coordinator` 整批调度（coordinator_modes/coordinator_tasks 删除——隔离/合并由 ActionRunner 权威承载；execute_action/_execute_single_task/_create_failure_record 保留）；`drivers_plan_execute` staged 分支删除（staged 快照恢复→execute 显式 LEGACY_WORKFLOW_UNSUPPORTED 人工处理）；
  (6) 死代码：`agents/fusion_agent.py`、`integration/legacy_facade.py`（G-DEADCODE：无生产调用方）+ `search_without_stream.py` CLI 简化（fusion/deep_research 选项移除）。
  保留：GraphRAG 构建/索引/检索（graph/、pipelines/、community/、retrieval/）、web/scholar Provider、Evidence Ledger、Reporter 卡片/MapReduce、确定性 Contract、`harness/verifiers/`、共享 search 工具（local/global/hybrid/naive/chain_exploration/hypothesis）。配置清理：MULTI_AGENT_WORKER_EXECUTION_MODE/MAX_CONCURRENCY、MA_REFLECTION_* 删除；planner/executor prompts 清理（仅 CLARIFY_PROMPT 存活）。**TaskNode/RetrievalResult 的 task_type Literal 保留 deep_research/deeper_research/reflection 值**——存量快照 TaskNode 解析兼容（有意决定，非遗漏；controller 不提议、无执行器处理）。测试调整：删除 staged 批量隔离测试（ActionRunner 测试覆盖）、deep_research driver/agent 测试、planner 结构化输出测试；新增 staged 快照拒绝测试与模块缺席断言（find_spec）。静态检查：退役模块 import 全部清除（grep 0 引用，除 inventory/convert 盘点脚本与兼容性注释）；Import smoke 通过；三 source mode 由 brief_flow（规划级）+ scholar route + coreact e2e 覆盖。通用门禁：pytest 488 passed/4 skipped（500 − 12 个退役引擎专属测试）；ruff（变更文件）通过；git diff --check 干净。
- 风险：零直接 importer 不等于无公共/动态调用，需扫描 registry/字符串入口。（tool_registry 的 deep_research/deeper_research/answer_validator 键已删除；动态字符串调用扫描见 inventory §3.2 复核记录；TaskNode Literal 保留旧值以保旧快照解析——见上。）


## T-409 — 全链验收、运行文档与移交
- 状态：done
- 前置：T-408
- 目标：验证唯一 coreact 全链，文档/代码/恢复/展示一致。
- 涉及：真实 Runtime+数据库 e2e、`docs/RUN-FLOWS.md`、README、`.env.example`、acceptance.md。
- 动作：覆盖 happy path、语义未知、缺证据补检索、bounded repair、K=2、pause/cancel、接管、批次/重试崩溃、旧历史页；更新简历描述，明确非论文复现与未做效果实验。
- 验收：通用门禁、前端构建/测试（若项目有相应脚本）、适用 CI、最终独立 code review；不虚构 provider/LLM/服务测试。
- 预期证据：最终验收矩阵、命令输出、审查结论、已知限制/回滚交接。
- 实际证据（2026-11-11）：**最终验收矩阵**（acceptance.md §2）AC-01..AC-16 全部 PASS——逐项映射到测试文件与命令（e2e 链路：test_coreact_e2e（happy path/步级提交/暂停续跑）、test_action_runner_recovery（5 故障点崩溃恢复）、test_action_runner_isolation（K=2 barrier/预算/隔离）、test_coreact_observation_contract（语义四来源/注入/泄露/降级）、test_coreact_brief_flow（BriefBuilder/缺口派发/预算 fallback）、test_coreact_only_entry + test_coreact_legacy_adapter（单一入口/历史兼容/转换工具）、test_takeover（SIGKILL 接管））。运行文档：RUN-FLOWS.md F1/F2 重写为唯一 coreact 路径（退役引擎历史兼容如实）、README 定位声明（工程实现/简历项目，非论文复现、未做效果实验）、.env.example 死变量清理 + COREACT_SEMANTIC_ENABLED 说明、inventory.md 边界口径更新。通用门禁：pytest 488 passed/4 skipped；ruff（变更文件）通过；git diff --check 干净；npm run build 通过；CI（ci.yml pytest+npm build+PostgreSQL）配置适用未改。已知限制与回滚交接见 acceptance.md §4（非 exactly-once/fencing/语义默认关闭/2 项既有 flaky/旧 Run 处置清单/按阶段 revert）。
- 风险：未验证的真实服务/GPU/性能不写通过。（测试以 fake provider + 真实执行器栈/真实 SQLite Runtime；LLM/Neo4j/Tavily 服务级端到端未验证——本地部署路径与 CI 存在，验收不含服务级断言。）


## 进度

已完成 9（T-401..T-409 全部）；进行中 0；阻塞 0；待执行 0。T-017 实施完成：唯一 coreact 生产路径（最终验收矩阵见 acceptance.md §2；已知限制与回滚交接见 §4）。

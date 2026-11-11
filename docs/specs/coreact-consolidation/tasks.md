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
- 实际证据（2026-11-11）：`coreact_state.py` 扩展 `pending_batch`（全批次 in-flight intent）+ `committed_action_ids`（逐动作提交）+ `next_operation`（显式 execute/report，driver snapshot 序列化）；恢复归一化逐项校验（畸形项剔除并计入 failure_reasons，全无效→decide，旧单动作快照兼容回退）。`coreact_loop.py`：intent 写全批次、run_and_merge 新增 `commit_fn`（逐动作提交，action_commit 钩子只持久化不发布 step 事件）与 `record_fn`（verdict 元数据提交前写入——恢复后 rubric_coverage 门不被削弱）、修正游标即时持久化、重放防御性跳过已提交项（重试游标始终重放）。`action_runner.py`：commit_fn/record_fn 挂载；RunLeaseLost/BudgetExceeded 从 worker 传播（不降级为动作失败记录）。稳定 tool ID：TaskNode.attempt 字段 + retrieval_executor tool_call_id=(run_id,task_id,attempt) 派生（重放行幂等，重试新 ID 保留全部尝试）。T-402 适配器改用 inflight_items（迁移视图不丢次要动作）。`tests/harness/test_action_runner_recovery.py` 8 项：intent 后崩溃（批次完整落盘、零 tool/evidence 行）、intent 后崩溃恢复（次要动作不丢、恢复不走 controller）、行已落库/checkpoint 未写窗口（稳定 ID 行幂等=1、事件不重复、外部调用重复 2 次如实记录、计费不双收）、部分批次提交后恢复（已提交不重做、rubric_coverage/min_evidence 通过）、retry 中崩溃（attempt=2 游标重放、task_retries 不双计）、批次游标校验 3 项。故障点矩阵：intent 后/行后 checkpoint 前/部分提交后/重试执行中/重试合并后提交前——均无动作丢失窗口；游标收缩先于持久化，checkpoint 恒保守。通用门禁：pytest 453 passed/4 skipped；ruff（变更文件）通过；git diff --check 干净。独立 code review 首轮 2 major（verdict 元数据提交时序、稳定 tool ID 缺失）+ 7 minor/nit 全部修复并复审。
- 风险：不宣称外部 exactly-once 或已消除原子 fencing 缺口。（外部调用在行后/checkpoint 前崩溃窗口内重复一次，如实断言；内存计费可随窗口丢失（低估），重放不双收。pause/cancel/lease 检查保持在动作边界（lease_probe/pause_probe 未改动）。已知行为变化：确定性 tool ID 使 staged reflection 重试的计费从每次尝试一行变为同 ID 去重一行（低估方向、证据行不受影响；staged 引擎将在 T-407/408 退役）。逐动作提交的 _persist_execution 为 O(N²) 重扫（K≤4 批次可接受，性能不作为目标）。）


## T-405 — 证据摘要与动作 rubric 指导
- 状态：pending
- 前置：T-404
- 目标：Controller 从真实研究发现决策，动作执行明确目标/输出/来源/边界。
- 涉及：`coreact_controller.py`、`coreact_verifier.py`、`coreact_support.py`、`core/rubric.py`、`executor/base_executor.py`、ReportService 输入。
- 动作：生成有界 ObservationSummary；claim-scoped assessments；rubric 进入内部执行上下文但不进 Provider 参数；可选 semantic verifier pass/fail/unknown 与预算门；报告安全降级规则。
- 验收：拟新增 `tests/harness/test_coreact_observation_contract.py`；改变证据内容而不变计数会改变 scripted controller 决策；过滤账本外 ID、prompt injection、工具参数泄露；required unknown 不得通过；状态去重与冲突迁移；通用门禁。
- 预期证据：输入/输出协议、摘要边界/费用与安全测试日志。
- 实际证据：未执行。
- 风险：关键词检查仍仅为代理，不升级为事实认证。

## T-406 — BriefBuilder 替换重复规划与 Reflector
- 状态：pending
- 前置：T-405
- 目标：Controller 成为唯一下一动作决策者，移除无效 reflection 候选。
- 涉及：`planner/base_planner.py`、`clarifier.py`、`task_decomposer.py`、`plan_reviewer.py`、`executor/reflector*.py`、工厂与 coreact driver。
- 动作：BriefBuilder 生成问题/验收/报告契约，保留可选澄清及简单请求捷径；停止自动 reflection task、核心工厂不注册旧 Reflector；改 final evidence 缺口反馈 Controller，而非回旧 Planner。
- 验收：拟新增 `tests/harness/test_coreact_brief_flow.py`；澄清/摘要/多方向研究、三来源模式、无 reflection 派发、ReportService 输入与预算 fallback；通用门禁。
- 预期证据：无旧规划/反思入口调用的运行 trace、角色模型一致性断言。
- 实际证据：未执行。
- 风险：只有共享检查完整迁移且调用者审计通过才能物理删除文件。

## T-407 — 单一入口切换与历史兼容
- 状态：pending
- 前置：T-406；T-401 的旧 Run 处置 gate 通过
- 目标：所有生产入口只创建 coreact，不再运行 F1/staged 引擎。
- 涉及：`run_service.py`、`bootstrap.py`、API schemas、capabilities、前端 ChatPage/types、model snapshot、迁移工具。
- 动作：新请求旧标识在边界归一化；保留历史读取；dry-run 检查并转换可映射快照；不可映射活动 Run 安全拒绝且列人工处理，禁止自动取消；移除 RESEARCH_ORCHESTRATION 的旧路径选择。
- 验收：拟新增 `tests/api/test_coreact_only_entry.py` 与 `tests/harness/test_coreact_legacy_adapter.py`；HTTP/worker/CLI/Skill eval 全入口 trace 同一路径、OpenAPI/前端一致；历史报告可读、旧快照可转换或明确拒绝；`npm --prefix frontend run build` + 通用门禁。
- 预期证据：入口矩阵、dry-run/备份回滚记录、前端构建、历史读取测试。
- 实际证据：未执行。
- 风险：任何破坏性数据操作必须另获批准；数据库历史 CHECK 不随意收窄。

## T-408 — 删除旧专属模块与整流程外壳
- 状态：pending
- 前置：T-407
- 目标：最终仅 coreact 生产实现，不保留旧引擎作 fallback。
- 涉及：design §7 删除候选表、`MultiAgentOrchestrator.run` 及专用模型、旧 driver/planner/reflector/scheduler/Mixin、registry/__init__/测试/文档。
- 动作：逐文件列删除原因、已迁移职责、所有 importer；共享搜索/GraphRAG 能力先抽取再删除旧壳；最小 facade 只导出新实现或数据适配器；清理死配置与测试，不能删核心安全测试凑绿。
- 验收：静态检查旧生产 dispatch/reflection 自动生成消失；import smoke、CLI、三 source mode 端到端；保留 GraphRAG 构建/检索专项测试；通用门禁与独立删除清单 review。
- 预期证据：删除/保留清单、调用图、无失联 import 及能力覆盖日志。
- 实际证据：未执行。
- 风险：零直接 importer 不等于无公共/动态调用，需扫描 registry/字符串入口。

## T-409 — 全链验收、运行文档与移交
- 状态：pending
- 前置：T-408
- 目标：验证唯一 coreact 全链，文档/代码/恢复/展示一致。
- 涉及：真实 Runtime+数据库 e2e、`docs/RUN-FLOWS.md`、README、`.env.example`、acceptance.md。
- 动作：覆盖 happy path、语义未知、缺证据补检索、bounded repair、K=2、pause/cancel、接管、批次/重试崩溃、旧历史页；更新简历描述，明确非论文复现与未做效果实验。
- 验收：通用门禁、前端构建/测试（若项目有相应脚本）、适用 CI、最终独立 code review；不虚构 provider/LLM/服务测试。
- 预期证据：最终验收矩阵、命令输出、审查结论、已知限制/回滚交接。
- 实际证据：未执行。
- 风险：未验证的真实服务/GPU/性能不写通过。

## 进度

已完成 3（T-401..T-403）；进行中 0；阻塞 0；待执行 6。下一步：T-404（多动作 intent 与耐久恢复）。每阶段收尾须重跑 T-401 盘点脚本确认存量数据边界（口径见 inventory.md §5）。

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
- 状态：pending
- 前置：T-401
- 目标：冻结 Brief/ActionSpec/ActionResult/ObservationSummary/EvidenceAssessment/ExecutionState 接口，并冻结 `output-contract.md` 中的用户报告与内部动作结果协议。
- 涉及：现有 `core/plan_spec.py`、`harness/coreact_state.py`、拟新增 `harness/research_brief.py` / `action_models.py`、`docs/specs/coreact-consolidation/output-contract.md`。
- 动作：定义版本/hash/证据引用/next_operation/inflight 集合；旧 TaskNode/LoopState 的数据适配器；定义模型角色冻结规则；实现 ResearchRunResult 的 API/前端兼容视图设计。
- 验收：拟新增 `tests/harness/test_coreact_consolidation_models.py`，测试 JSON 往返、未知版本拒绝、来源约束、并行/重试身份和旧 payload 转换；验证 Markdown 报告状态、ActionResult execution/verification 分离；通用门禁。
- 预期证据：schema/兼容矩阵与测试日志。
- 实际证据：未执行。
- 风险：不把未知旧状态自动当空状态重新执行。

## T-403 — 统一隔离 ActionRunner
- 状态：pending
- 前置：T-402
- 目标：替代共享状态 coreact gather 与重复调度/重试逻辑。
- 涉及：`executor/worker_coordinator.py`、`coordinator_tasks.py`、`coordinator_modes.py`、`harness/coreact_loop.py`、拟新增 `harness/action_runner.py`。
- 动作：提取公开单动作/批次接口；输入副本隔离、结果 delta、稳定合并；预派发预算预约、来源/去重门；统一 retry 所有权，保留所有 attempt 与成本。
- 验收：拟新增 `tests/harness/test_action_runner_isolation.py`；K=2 使用 barrier 证明重叠而非仅断言执行两个；不同完成顺序不改变合并结果；真实执行器+fake provider，测试预算超限零派发、任务失败、共享键冲突与每动作一次修正；通用门禁。
- 预期证据：隔离/并发/预算/重试正确性日志。
- 实际证据：未执行。
- 风险：不能用 GIL 下 list.append 原子性作为线程安全证明。

## T-404 — 多动作 intent 与耐久恢复
- 状态：pending
- 前置：T-403
- 目标：派发前保存全批次动作，恢复不丢次要动作或重做已提交结果。
- 涉及：`runtime_step_hooks.py`、`runtime_persistence.py`、`runtime.py`、`coreact_state.py`、trajectory repositories。
- 动作：稳定 action/attempt/tool ID、全量 in-flight intent、逐动作提交/预算核销与 next_operation；pause/cancel/lease 检查；旧 checkpoint 适配器，显式 unsupported 处理。
- 验收：拟新增 `tests/harness/test_action_runner_recovery.py`；在 intent前后、调用后/提交前、部分批次提交后、retry 中注入中断；新 Runtime 恢复；断言工具调用/证据/预算/事件，不仅测试 helper predicate；通用门禁。
- 预期证据：故障点矩阵、持久行/事件/费用计数及残余窗口说明。
- 实际证据：未执行。
- 风险：不宣称外部 exactly-once 或已消除原子 fencing 缺口。

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

已完成 1（T-401）；进行中 0；阻塞 0；待执行 8。下一步：T-402（ResearchBrief 与单一职责接口）。每阶段收尾须重跑 T-401 盘点脚本确认存量数据边界（runs/checkpoints 计数不变）。

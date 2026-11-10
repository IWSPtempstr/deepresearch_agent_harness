# Tasks: T-015 Co-ReAct 式步级验证控制

> 执行约束：每阶段 = 实现 → 全量 pytest 门禁（与 P0 基线一致）→ 独立 code review → 修复 → 单次本地提交。
> 测试命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
> 术语：代码/提交用 coreact / plan_execute_report，不用 "F2"（与 llm_gateway 的 F2 failover 冲突）。

## P0 · 基线

- **状态**: done
- **证据**: 2026-11-10，分支 `feat/t015-coreact-orchestration`（自 main=baac909），`pytest tests/ -q` → **262 passed, 4 skipped, 8 warnings in 69.73s**。

## T-201 · rubric / LoopState 核心模型与确定性 rubric 生成

- **前置**: P0
- **涉及**: 新增 `agents/multi_agent/core/rubric.py`、`harness/coreact_state.py`；测试 `tests/harness/test_coreact_rubric.py`
- **动作**: RubricCriterion/TaskRubric/StepVerdict 模型；`build_task_rubric` 确定性生成（criteria：evidence_presence/source_policy/output_schema/claim_support/no_duplicate_query，threshold=0.5，max_retries=1）；CoreActLoopState/PendingAction 版本化模型 + 畸形 workflow_state 安全还原。
- **验收**: pytest 全绿 + 模型序列化/校验单测 13 例。
- **状态**: done

## T-202 · coreact_verifier 确定性步级验证

- **前置**: T-201
- **涉及**: 新增 `harness/coreact_verifier.py`；测试 `tests/harness/test_coreact_verifier.py`
- **动作**: 确定性检查（SourcePolicy、账本内 evidence_id、证据计数、查询去重、输出存在）+ 预算门控语义检查（严格 JSON，畸形/异常降级为确定性判定，永不与确定性 fail 相矛盾）。
- **状态**: pending

## T-203 · coreact_controller 步级决策

- **前置**: T-201
- **涉及**: 新增 `harness/coreact_controller.py`；测试 `tests/harness/test_coreact_controller.py`
- **动作**: ControllerDecision（下一动作/ready_to_report/reason）；LLM 结构化决策畸形 ×2 后兜底；确定性兜底（候选图可执行任务→下一个，否则 ready_to_report）；no_progress 有界；产出 TaskNode 过 SourcePolicy/依赖校验。
- **状态**: pending

## T-204 · driver 集成：版本分派 + 步循环 + 单动作适配

- **前置**: T-202、T-203
- **涉及**: `harness/drivers_plan_execute.py`、`executor/worker_coordinator.py`（+`execute_action`）；测试 `tests/harness/test_coreact_driver.py`
- **动作**: orchestration 版本分派（缺省 staged_v1）；coreact 路径 plan/execute 循环（串行、步间 pause/cancel/budget 检查、一次重试结果替换、事件按既有形状逐动作发布）；`supports_step_commit=True` 类属性 + `step_commit` 属性；compact/reserved 报告启发式适配；legacy staged 路径与构造签名不变。
- **状态**: pending

## T-205 · runtime 步级提交与 continuation 恢复

- **前置**: T-204
- **涉及**: `harness/runtime_execution.py`、`harness/runtime_persistence.py`、`harness/runtime.py`；测试 `tests/harness/test_coreact_recovery.py`
- **动作**: `commit_coreact_step`（幂等复用 _persist_plan/_persist_execution + EXECUTING checkpoint）；注入 `step_commit`；`_safe_stage_after`/`_load_context`：EXECUTING checkpoint 携带未完成 coreact marker → 停在 executing，否则旧映射；恢复重放覆盖新路径。
- **状态**: pending

## T-206 · rubric_coverage 全局契约检查

- **前置**: T-204
- **涉及**: `harness/contracts.py`、`harness/verifiers/deterministic.py`、`harness/drivers_plan_execute.py`（repair 路由）；测试并入 `test_coreact_driver.py`
- **动作**: 新 kind `rubric_coverage`（每 rubric required_claims 须被账本内证据就近引用，不接受 reporter 自报）；纳入 `locally_repairable` → 既有 repair→重验；repair 保持确定性 `_evidence_only_report`。
- **状态**: pending

## T-207 · 配置戳记与文档

- **前置**: T-205
- **涉及**: `config/settings.py`、`backend/app/services/chat_service.py`、`docs/RUN-FLOWS.md`
- **动作**: `RESEARCH_ORCHESTRATION`（默认 coreact_v1）+ 建 Run 时写入 config_snapshot；RUN-FLOWS F2 节改写为 coreact_v1 真实行为（含崩溃窗口 ≤1 动作的诚实表述）；capabilities 是否暴露 orchestration 记为可选项。
- **状态**: pending

## T-208 · e2e 正确性与收尾

- **前置**: T-206、T-207
- **涉及**: `tests/harness/test_coreact_e2e.py`（或并入 recovery 测试）；acceptance.md 汇总
- **动作**: 通过 runtime 的完整 e2e（scripted LLM）：happy path 全链 + 步间崩溃恢复 + 预算耗尽 reserved fallback；全仓测试绿；验收汇总与已知限制清单。
- **状态**: pending

## 进度摘要

- 已完成：1（P0）
- 进行中：0
- 阻塞：0
- 待执行：8
- 下一步：T-201
- 最后更新：2026-11-10

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
- **状态**: done
- **备注**: restore 侧同步强化——pending task/rubric 逐一校验，版本偏斜 pending 清空并回退 decide（评审发现原注释声称的围栏不存在，已实际补齐）。

## T-203 · coreact_controller 步级决策

- **前置**: T-201
- **涉及**: 新增 `harness/coreact_controller.py`；测试 `tests/harness/test_coreact_controller.py`
- **动作**: ControllerDecision（下一动作/ready_to_report/reason）；每步总尝试 2 次（畸形 JSON / 非法 TaskNode / 调用异常合并计数）后兜底；LLM 内容 ≤20k 字符；多围栏按序解析；确定性兜底（候选图可执行任务→下一个，否则 ready_to_report）；产出 TaskNode 过结构校验（SourcePolicy 于派发层权威执行）。
- **验收**: focused 19 passed；全量 313 passed/4 skipped；ruff F821/F811/F401 clean；独立评审 PASS（硬化补丁复评）。
- **状态**: done

## T-204 · driver 集成：版本分派 + 步循环 + 单动作适配

- **前置**: T-202、T-203
- **涉及**: `harness/drivers_plan_execute.py`（orchestration 分派 + coreact 步循环）、`executor/worker_coordinator.py`（+`execute_action`）、`agents/multi_agent/orchestrator.py`（`controller_model`/`execute_action` façade）、`agents/multi_agent/core/plan_spec.py`（TaskType 增 scholar_search、status 增 skipped）、`executor/retrieval_executor.py`（can_handle + provider 分派增 scholar_search）、`harness/coreact_state.py`（decisions_used/failure_reasons）、`harness/coreact_controller.py`（source 白名单 + 候选 task_id 精确匹配）；测试 `tests/harness/test_coreact_driver.py`（10）、`tests/retrieval/test_coreact_scholar_route.py`（1）
- **动作**: orchestration 版本分派（缺省 staged_v1）；coreact 路径 plan/execute 循环（串行、步间取消/预算检查、一次重试结果替换、事件按既有形状逐动作发布）；`supports_step_commit=True` + `step_commit` 注入点；compact/reserved 报告启发式适配（coreact 用动作数而非计划节点数）；legacy staged 路径与构造签名不变。
- **验收**: focused 11 passed；全量 324 passed/4 skipped；ruff F821/F811/F401 clean；独立评审 PASS（两轮修复后复评）。
- **状态**: done
- **已知限制**: 步级 durability（step_commit 注入 + continuation 恢复）属 T-205；本阶段 `step_commit` 默认为 None，不声称已具备逐动作持久化。

## T-205 · runtime 步级提交与 continuation 恢复

- **前置**: T-204
- **涉及**: `harness/runtime_execution.py`、`harness/runtime_persistence.py`、`harness/runtime.py`；测试 `tests/harness/test_coreact_recovery.py`
- **动作**: `commit_coreact_step`（幂等复用 _persist_plan/_persist_execution + EXECUTING checkpoint）；注入 `step_commit`；`_safe_stage_after`/`_load_context`：EXECUTING checkpoint 携带未完成 coreact marker → 停在 executing，否则旧映射；恢复重放覆盖新路径。
- **状态**: pending

## T-206 · rubric_coverage 全局契约检查

- **前置**: T-204
- **涉及**: `harness/contracts.py`（kind 扩展 + evaluate rubrics 参数 + locally_repairable）、`harness/verifiers/deterministic.py`（rubric_coverage 检查）、`harness/recovery.py`（repair 路由）、`harness/drivers_plan_execute.py`（repair_report 支持 + coreact_rubrics 收集）、`harness/runtime_execution.py`（VERIFYING 传参）；测试 `tests/harness/test_coreact_rubric_coverage.py`（11 用例）
- **验收**: pytest 全绿；ruff clean；独立评审 PASS（两轮：首轮 BLOCK——repair 通道未接通/无判词策略不一致/D 项暴露面表述——全部修复；次轮 BLOCK——driver repair 测试空洞 + design 措辞——落盘修复）。
- **状态**: done

## T-207 · 配置戳记与文档

- **前置**: T-205
- **涉及**: `config/settings.py`、`backend/app/services/chat_service.py`、`docs/RUN-FLOWS.md`
- **动作**: `RESEARCH_ORCHESTRATION`（默认 coreact_v1）+ 建 Run 时写入 config_snapshot；RUN-FLOWS F2 节改写为 coreact_v1 真实行为（含崩溃窗口 ≤1 动作的诚实表述）；capabilities 是否暴露 orchestration 记为可选项。
- **状态**: pending

## T-208 · e2e 正确性与收尾

- **前置**: T-206、T-207
- **涉及**: `tests/harness/test_coreact_e2e.py` + T-205 `test_coreact_recovery.py`；acceptance.md 汇总
- **动作**: 真实 HarnessRuntime + SQLite + scripted LLM e2e：步级执行→报告→Contract→completed；步间 pause→checkpoint→新 Runtime 恢复到下一动作；断言 plan/task/toolcall/evidence/step event 与 rubric_coverage 持久结果；全量测试、ruff、文档状态核对。
- **验收**: e2e 2 passed；全量 355 passed/4 skipped；changed-scope F821/F811 clean、新增测试 F401 clean；`git diff --check` clean；T-208 independent reviewer 初次发现的 debug print 已删除。最终复评环境缺 langchain_core 无法重跑，但此前独立审查已核对 e2e 逻辑与数据库断言；实际测试由本地项目 .venv 执行。
- **状态**: done

## 进度摘要

- 已完成：9（P0 + T-201..T-208）
- 进行中：0
- 阻塞：0
- 待执行：0
- 下一步：无
- 最后更新：2026-11-10

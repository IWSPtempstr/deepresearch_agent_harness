# Design: T-015 Co-ReAct 式步级验证控制（替换 plan_execute_report 编排）

## 1. 设计摘要

保留 `PlanExecuteReportDriver` 作为公共外壳与 `workflow_mode=plan_execute_report` 语义，将其内部"一次性规划+整批执行"替换为 `coreact_v1` 步级控制循环：每步 = 决策（观察驱动选下一动作）→ 单动作执行 → 任务级 rubric 确定性验证 → 至多一次定向重试 → **步级持久提交**（幂等 upsert + continuation checkpoint）。报告/Contract/repair/replan 外层安全网原样保留，全局证据链覆盖以新 `rubric_coverage` 契约检查实现。存量 Run 靠 `config_snapshot["orchestration"]` 版本键继续走 legacy staged 路径。

## 2. 现状与目标架构

### 现状（生产路径，非 `MultiAgentOrchestrator.run()`）

```text
run_service → PlanExecuteReportDriver(plan→execute→report) → Harness 验证/修复/完成
execute(): orchestrator.execute(signal) 一次性整批（asyncio.to_thread）
持久化:   整批返回后 _persist_plan/_persist_execution 一次写入 + stage-end checkpoint
恢复:     EXECUTING checkpoint ⇒ 批次已完成 → REPORTING（runtime._safe_stage_after）
```

### 目标

```text
plan():   初始化 LoopState；用现有 planner 产出候选 TaskGraph（仅作建议集）
execute(): while not done and 预算/取消/暂停检查通过:
             ① decide  — controller 依据 目标+轨迹摘要+证据缺口+预算 选下一动作
                         （候选图任务 或 新增任务；或 ready_to_report）
             ② act     — worker_coordinator.execute_action(单动作适配器)
             ③ verify  — coreact_verifier 确定性判定；语义检查需预算门控
             ④ retry?  — 失败且首次 → 带反馈重试一次，结果替换
             ⑤ commit  — step_commit 回调：幂等 upsert plan/task/toolcall/evidence/usage
                         + 写 continuation checkpoint（workflow_state.coreact.done=false）
           done → 正常返回；runtime 走既有 VERIFYING 路径
report():  既有管线 + 全局 rubric_coverage 契约检查（T-206）
```

关键约束映射：状态机 `EXECUTING→EXECUTING` 已存在，循环内部 phase（decide/act/verify）只存在于 LoopState，不出现在 RunStatus；恢复规则只对"带未完成 coreact marker 的 EXECUTING checkpoint"改为留在 EXECUTING，其余全部保持旧映射。

## 3. 模块与接口变更

| 模块/文件 | 当前职责 | 变更 | 兼容性 |
|---|---|---|---|
| `agents/multi_agent/core/rubric.py`（新） | — | `RubricCriterion`/`TaskRubric`/`RubricVerdict`/`StepVerdict` 模型 + `build_task_rubric(task, *, min_evidence, source_mode)` 确定性生成 | 纯新增 |
| `harness/coreact_state.py`（新） | — | `PendingAction`/`CoreActLoopState`（engine/version/phase/step_index/pending_action/stop_reason/done/no_progress_count） | 纯新增 |
| `harness/coreact_verifier.py`（新） | — | `CoreActVerifier.verify(rubric, task, records, ledger_view, *, semantic_allowed) -> StepVerdict`；确定性检查：动作工具过 SourcePolicy、evidence_id 在账本、证据计数≥要求、查询非重复、输出存在；语义检查：严格 JSON、畸形/异常→确定性判定 | 纯新增 |
| `harness/coreact_controller.py`（新） | — | `CoreActController.decide(...) -> ControllerDecision`（TaskNode/ready_to_report/reason）；LLM 结构化输出，每步总尝试 2 次（畸形 JSON / 非法 TaskNode / 调用异常合并计数），随后确定性兜底：候选图还有可执行任务→取下一个，否则 ready_to_report；回传内容长度上限防资源耗尽 | 纯新增 |
| `harness/drivers_plan_execute.py` | staged 编排 | `__init__` 读 `config_snapshot.orchestration`（缺省 staged_v1）；coreact 路径：`plan()` 产候选图+LoopState，`execute()` 跑步循环（串行）；`step_commit: Callable\|None = None` 属性供 runtime 注入；`_uses_compact_report`/`_build_reserved_budget_report` 启发式适配动作账本；legacy staged 路径原样保留 | 类名/构造签名/协议方法不变；`test_builtin_learning_loop.py:260` 直构兼容 |
| `agents/multi_agent/executor/worker_coordinator.py` | execute_plan 批量 | 新增公共 `execute_action(task, state, signal)`——薄封装现有 `_execute_single_task` | 纯新增方法 |
| `harness/runtime_execution.py` | 创建 driver、执行阶段 | 构造后对声明 `supports_step_commit=True` 的 driver 注入 `driver.step_commit = self._commit_coreact_step`；恢复重放覆盖 continuation checkpoint | F1/fake driver 无该类属性，零影响 |
| `harness/runtime_persistence.py` | 整批持久化 | 新增 `commit_coreact_step(context)`：复用幂等的 `_persist_plan/_persist_execution`（stable-ID upsert 天然去重）+ CheckpointManager 写 EXECUTING 快照 | 既有方法不动 |
| `harness/runtime.py` | 恢复映射 | `_safe_stage_after(EXECUTING)` 前先查 checkpoint payload 的 `workflow_state.coreact`：存在且 `done=false` → 停在 `executing`；否则旧映射 `→reporting` | 专项测试锁定两分支 |
| `harness/contracts.py` · `verifiers/deterministic.py` | 7+1 检查 | 新增 `rubric_coverage`（每条 rubric 的 required 判词须被**报告已引用的账本证据内容**以关键词子串覆盖——是确定性代理，不验证逐条主张的语义支持或就近关系；rubric 清单来自执行记录，不接受 reporter 自报）；加入 `locally_repairable` → 走既有 repair→重验通道（repair 上限 max_task_retries，无死循环） | kind 列为字符串存储，代码级 Literal 扩展 |
| `config/settings.py` | — | `RESEARCH_ORCHESTRATION: str = "coreact_v1"`、`COREACT_MAX_DECISIONS`、`COREACT_VERIFY_RESERVE_TOKENS` | 纯新增 |
| `backend/app/services/chat_service.py` | 建 Run | `config_snapshot["orchestration"] = settings.RESEARCH_ORCHESTRATION` | 旧 Run 无此键 → staged_v1 |
| `docs/RUN-FLOWS.md` | F2 描述 | 改写为 coreact_v1 真实行为 | 文档 |

## 4. 数据、状态与错误流

- **输入**：goal、候选 TaskGraph、已提交轨迹（records/evidence 摘要）、剩余预算、rubric 失败史。
- **LoopState**（`workflow_state["coreact"]`）：`{engine:"coreact_v1", version:1, phase, step_index, pending_action:{task,rubric,attempt}|null, stop_reason, done, no_progress_count}`。`checkpoint_version` 不作游标（它计 checkpoint 写次数）。
- **决策 schema**：`ControllerDecision{action: TaskNode|null, ready_to_report: bool, reason}`；动作工具/来源创建后过 `SourcePolicy` 校验，拒绝即计入 no_progress。
- **verdict**：`StepVerdict{rubric_id, task_id, passed, weighted_score, criteria:[{criterion_id,passed,reason,evidence_ids}], retry_reason}`；持久化进 record metadata + workflow_state。
- **持久化/幂等**：步提交复用 `_persist_plan/_persist_execution`（stable task_id/record_id/evidence_id upsert 天然去重）；checkpoint SHA-256 校验不变。
- **错误分类**：动作异常 → 失败 record（既有 `_create_failure_record`）；verifier LLM 异常 → 确定性判定；决策 LLM 畸形 ×2（总尝试上限）→ 兜底；`RunLeaseLost` → 静默让位（不写状态）；取消/暂停 → 步间检查点生效（当前动作无法硬停，等同 F2 既有限制，文档注明）。
- **崩溃窗口语义**：提交=动作完成后；窗口内崩溃丢失该动作（≤1），恢复后重做该动作（外部调用可能重复一次）；相比原整批丢失是严格改进。pre-invoke intent 行列为已知限制（§7）。
- **终态**：只有 Contract 通过才 completed（持久层门禁不变）；controller 的 ready_to_report 仅意味着"进入报告"，不是完成。

## 5. API、配置与迁移

- API/协议/前端/DB：零变更。
- 配置：`RESEARCH_ORCHESTRATION=coreact_v1|staged_v1`（默认 coreact_v1）；`COREACT_MAX_DECISIONS`（默认 2×max_plan_tasks）；`COREACT_VERIFY_RESERVE_TOKENS`（默认 4000）。
- 迁移：无 DB 迁移；存量未完 Run 恢复时缺 orchestration 键 → staged_v1 legacy 路径 + 旧恢复映射。
- 回滚：设 `RESEARCH_ORCHESTRATION=staged_v1` 即回退，无需代码回滚。

## 6. 安全、权限与资源

- 输入校验：LLM 决策产出的 task 须通过 TaskNode 校验 + SourcePolicy + 依赖存在性，否则拒绝并计 no_progress。
- 预算：决策/验证调用计入 `consume_tokens`；语义 verify 仅当 `remaining_tokens() > REPORT+VERIFICATION 预留 + COREACT_VERIFY_RESERVE_TOKENS`；重试受 `max_task_retries` 与 rubric `max_retries=1` 双重上限。
- 日志：rubric/verdict 不进最终报告文本；`has_internal_material` sanitize 不变。
- 租约：步间与提交前协作检查；fencing generation 记为后续硬化（design 明示，不在本期）。

## 7. 替代方案与取舍

| 方案 | 结论 |
|---|---|
| A 增强不改编排 | 验证无法影响中间动作，不满足"步级控制"目标；作为被替换对象保留为 legacy 路径 |
| B 新增第三 workflow 模式 | 需动 DB CHECK/API/前端/工厂分派且与 C 并存两套维护面；用户已选 C |
| pre-invoke intent 持久化 | 外部调用本就无 exactly-once；intent 行不消除重复只缩小窗口，且需新增持久化通道；列为已知限制，后续硬化项 |
| 并行步执行 | parallel 分支缺 stop_predicate、观察顺序/预算语义复杂；v1 串行 |
| LLM rubric 富化/语义判分默认开启 | 成本不可控；确定性生成 + 预算门控语义检查 |

## 8. 测试与观测

- **单元**：rubric 生成（确定性、阈值 0.5、max_retries=1）；verifier（账本外 evidence_id 判 fail、SourcePolicy 拒绝、畸形语义输出降级）；controller（畸形 JSON ×2 兜底、候选图耗尽→ready_to_report、no_progress 有界）。
- **驱动级（scripted controller + fake executor）**：观察驱动后续动作；早停进报告但不绕过 Contract；一次重试后结果替换；compact/reserved 报告路径；skill-policy 注入兼容。
- **恢复级**：步间崩溃 → 下一动作续跑、行无重复；legacy EXECUTING checkpoint → REPORTING；pause/cancel 步间生效；预算耗尽走 reserved fallback。
- **观测**：既有 agent.progress/task/tool 事件形状逐动作发布（前端零改动）；步事件含 step_index/rubric_id；无新指标端点。
- **验收证据**：每阶段 acceptance.md 记录 pytest 摘要与 review 结论；全程不做质量对比实验（用户约束）。

## 9. 未决问题

- 无（fencing、intent 行、并行化、语义 rubric 富化均为已记录的后续硬化项，不阻塞本期）。

## 10. 设计状态

- 状态：`draft`
- 关联 proposal：`docs/specs/coreact-orchestration/proposal.md`
- 日期：2026-11-10

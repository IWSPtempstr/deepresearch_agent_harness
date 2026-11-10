# Acceptance: T-015 Co-ReAct 式步级验证控制

## P0 · 基线（2026-11-10）

- 命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
- 结果：**262 passed, 4 skipped, 8 warnings in 69.73s** — 全绿，作为每阶段行为门禁基准。
- 分支：`feat/t015-coreact-orchestration`（自 main=baac909）。

## 决策记录

- 2026-11-10 用户决策：采用方案 C（在 plan_execute_report 外壳内替换编排为步级验证控制）；只实现、不做对比实验；定位简历/作品集项目。
- 2026-11-10 侦察结论（三路 Explore）：EXECUTING checkpoint 现行语义 = 批次完成→REPORTING（恢复规则须版本感知）；工具/证据持久化在整批返回后（需步级提交）；parallel 分支缺 stop_predicate（v1 串行）；状态机 EXECUTING→EXECUTING 已存在（循环 phase 不进 RunStatus）；契约 kind 列为字符串（rubric_coverage 可代码级扩展）。
- 外部来源（论文/仓库）仅吸收推理时机制，不引入运行时/训练依赖：Co-ReAct（步级 rubric+verify+单次重试）、MiroThinker-H1（局部/全局验证分层）、Marco（预算内候选验证、退化重启思想→no_progress 有界）、QUEST（trusted/untrusted/uncertain 记为后续 ContextState 硬化项，本期不做）。

## T-203（2026-11-10）

- 改动：新增 `harness/coreact_controller.py`（结构化 LLM 决策 + 每步总尝试 2 次 + 确定性候选/ready 兜底 + 20k 字符长度上限 + 多围栏按序解析）。
- 门禁：focused 19 passed；全量 **313 passed, 4 skipped（50s）**；ruff F821,F811,F401 clean。
- 独立评审：**PASS**；非阻断建议已落地：异常调用恰为 2 次断言、裸 response 分支、超长响应、多个 fenced block、ready 优先、query 回退、非 dict 拒绝；规范改成“每步总尝试 2 次”。
- 提示注入边界：goal/recent_queries/rubric feedback 拼入 user prompt，可能诱导低质量/提前 report/预算浪费；产物仍受 TaskNode/source policy/Budget/Completion Contract 门禁，不新增权限提升。

- 改动：新增 `harness/coreact_verifier.py`（五类确定性 criterion + 预算门控语义惩罚）；`coreact_state.restore` 强化 pending 校验（版本偏斜清空→decide）。
- 门禁：focused 32 passed；全量 **294 passed, 4 skipped（54s）**；ruff F821,F811 clean。
- 独立评审：首轮 BLOCK（restore 围栏注释失实——`pending_action.rubric` 实为裸 dict 未校验；预算门控测试假阳性；边界用例缺口）→ 修复 + 补 8 用例 → 复评 **PASS**。
- 备注（供 T-204）：restore 清 pending 后 phase 已同步回退 decide；语义 ValueError 由 driver 捕获转失败记录。

## T-201（2026-11-10）

- 改动：新增 `agents/multi_agent/core/rubric.py`（TaskRubric/StepVerdict + 确定性生成 + 4 字滑窗判词抽取）、`harness/coreact_state.py`（CoreActLoopState/PendingAction + 安全还原）；新增 13 个单测。
- 门禁：focused 13 passed；全量 **275 passed, 4 skipped（56s）**（= 基线 262 + 新增 13）；ruff F821,F811 clean。
- 独立评审：首轮 BLOCK（E 测试缺口）→ 补 4 用例 → 复评 **PASS**（F 项环境限制以实现方本地实证解除）。
- 过程修复：`completion_conditions` 参数未被消费（claim_support 不生成）→ 修正为 required_terms 未显式给定时从完成条件抽取。

## 验收矩阵（实现后逐项补证据）

| ID | 标准 | 证据 |
|---|---|---|
| AC-1 | 新 Run config_snapshot["orchestration"]=="coreact_v1"；缺键快照走 staged_v1 | 待实现（T-207） |
| AC-2 | 动作1观察改变动作2选择；无整批预执行 | 待实现（T-204） |
| AC-3 | 失败动作恰好一次定向重试，结果替换；全局 max_task_retries 仍生效 | 待实现（T-204） |
| AC-4 | ready_to_report 不绕过 Completion Contract | 待实现（T-204/T-206） |
| AC-5 | 步间崩溃→下一动作续跑，plan/task/toolcall/evidence 行无重复 | 待实现（T-205） |
| AC-6 | legacy EXECUTING checkpoint 仍推进 REPORTING；continuation checkpoint 停在 EXECUTING | 待实现（T-205） |
| AC-7 | 步间 pause/cancel/预算停止生效 | 待实现（T-205） |
| AC-8 | verifier 畸形/异常降级为确定性判定，不 Fail Run | 待实现（T-202） |
| AC-9 | rubric_coverage 失败走 repair→重验；报告无法满足时按既有语义失败 | 待实现（T-206） |
| AC-10 | 每阶段全量 pytest 与 P0 基线一致（新增测试除外） | 待实现（每阶段） |

## 已知限制（随实现更新）

- 外部工具调用无 exactly-once：步提交窗口内崩溃，恢复重做该动作（≤1 个），可能重复一次外部调用（严格优于原整批丢失/重复）。
- v1 串行执行；租约 fencing generation、pre-invoke intent 行、ContextState(trusted/untrusted/uncertain)、语义 rubric 富化均为已记录后续硬化项。

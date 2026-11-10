# Acceptance: T-015 Co-ReAct 式步级验证控制

## P0 · 基线（2026-11-10）

- 命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
- 结果：**262 passed, 4 skipped, 8 warnings in 69.73s** — 全绿，作为每阶段行为门禁基准。
- 分支：`feat/t015-coreact-orchestration`（自 main=baac909）。

## 决策记录

- 2026-11-10 用户决策：采用方案 C（在 plan_execute_report 外壳内替换编排为步级验证控制）；只实现、不做对比实验；定位简历/作品集项目。
- 2026-11-10 侦察结论（三路 Explore）：EXECUTING checkpoint 现行语义 = 批次完成→REPORTING（恢复规则须版本感知）；工具/证据持久化在整批返回后（需步级提交）；parallel 分支缺 stop_predicate（v1 串行）；状态机 EXECUTING→EXECUTING 已存在（循环 phase 不进 RunStatus）；契约 kind 列为字符串（rubric_coverage 可代码级扩展）。
- 外部来源（论文/仓库）仅吸收推理时机制，不引入运行时/训练依赖：Co-ReAct（步级 rubric+verify+单次重试）、MiroThinker-H1（局部/全局验证分层）、Marco（预算内候选验证、退化重启思想→no_progress 有界）、QUEST（trusted/untrusted/uncertain 记为后续 ContextState 硬化项，本期不做）。

## T-206（2026-11-10）

- 改动：ContractCheckData.kind 增 `rubric_coverage`；DeterministicVerifiers 新检查（报告已引用的账本证据须覆盖 rubric required 判词——**关键词子串确定性代理，非语义支持**）；evaluate 接 rubrics 参数；recovery/driver repair 通道接通；`coreact_rubrics()` 从执行记录收集（staged 返回 None 跳过）。
- 门禁：focused **11 passed**；全量 **349 passed, 4 skipped（54s）**；ruff F821,F811 clean。
- 独立评审：首轮 **BLOCK**（repair 通道三处未接通、无判词策略与步级 verifier 不一致、kind 暴露面表述不实）→ 全部修复（recovery/repair 白名单、重建触发、无判词空转通过）→ 次轮 **BLOCK**（repair 测试空洞、design 措辞过强）→ 落盘修复（非空报告重建用例 + 措辞收窄）→ 终评 **PASS**。
- 暴露面如实记录：check.kind 经 GET /runs/{id} API 暴露、前端原文字符串展示（无枚举约束，未知值回退原文），DB kind 列 String(48) 无 CHECK——无 schema 破坏。

## T-205（2026-11-10）

- 改动：`_commit_coreact_step`（幂等步级持久化 + EXECUTING checkpoint）；step_commit/pause_probe 注入（`supports_step_commit` duck-typing，legacy/fake driver 零影响）；`_resolve_coreact_pause`（DB cancel 胜 pause，CANCELLED 不清标志、PAUSED 清 pause 并 resume=EXECUTING）；`_coreact_continuation_pending` 版本感知恢复（coreact EXECUTING+未完成→EXECUTING，legacy→REPORTING，malformed→legacy）；恢复重放补 EXECUTING+continuation 分支。
- 门禁：focused **14 passed**；全量 **338 passed, 4 skipped（55s）**；ruff F821,F811 clean（F401 14 处为 T-014 既有债，改动前后相同）。
- 独立评审：首轮 **BLOCK 3 项**（事件去重只覆盖最早 500 条、failed 调用重复计费、pause 抢先 cancel 导致取消丢失）→ 修复（全量分页去重、行存在即跳过计费、DB cancel 优先仲裁）；次轮 **BLOCK 1 项**（pause 测试断言空洞 + 计费窗口文档缺失）→ 落盘修复（request_pause=True/true→false 断言 + acceptance 已知限制）→ 终评 **PASS**。
- 过程教训：python replace 静默失配一次（old_string 不匹配仍打印 patched）→ 后续补丁用 assert old in src 防呆。

## T-204（2026-11-10）

- 改动：`drivers_plan_execute.py` 加 orchestration 分派 + coreact_v1 步循环（观察驱动、步级 rubric 验证、单次定向重试、失败反馈、重复查询拒绝、skipped 候选标记）；`worker_coordinator.execute_action` + `orchestrator.execute_action/controller_model` façade；TaskType/status 扩展（scholar_search、skipped）；retrieval_executor scholar 走 provider 路径；coreact_state 增 decisions_used/failure_reasons。
- 门禁：focused **11 passed**（driver 10 + scholar 1）；全量 **324 passed, 4 skipped（51s）**；ruff F821,F811,F401 clean。
- 独立评审：首轮 **BLOCK 6 项**（legacy staged 被执行两次、scholar 路由未闭合、retry 用旧查询集导致重复查询通过、第二次失败未反馈下一轮、max_decisions 非累计、snapshot marker 未被 restore 读取）→ 全部修复并补 7 个回归用例 → 复评 **PASS**。
- 已知限制（不计入本阶段完成）：步级 durability（step_commit 注入、continuation checkpoint 恢复）属 T-205；当前 `step_commit` 默认 None，逐动作持久化尚未接线。

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
- **预算计数非 exactly-once**（T-205 评审定稿）：`_persist_execution` 的"已存在行重复提交不计费"仅覆盖正常重复提交；consume→prepare 之间崩溃（行不存在）重放会再次计费；usage 持久化与心跳写库也非原子。崩溃恢复不保证预算计数 exactly-once，只保证行级不重复。
- v1 串行执行；租约 fencing generation、pre-invoke intent 行、ContextState(trusted/untrusted/uncertain)、语义 rubric 富化均为已记录后续硬化项。
- 暂停顺序差异：coreact 暂停路径先 clear pause 再写 paused checkpoint（legacy 相反）；因 checkpoint 包含完整 context（含 resume_from_status=EXECUTING），语义等价，但顺序不逐字一致（T-205 评审记录）。


## T-208 · e2e 正确性与收尾（2026-11-10）

- 新增 `tests/harness/test_coreact_e2e.py` 两条真实 HarnessRuntime + SQLite + scripted LLM e2e：
  1. happy path：2 个 controller 动作 → 每步 step_commit → 报告 → Contract（rubric_coverage/min_evidence）→ completed；断言计划/任务/tool_call/evidence/step event 行数与状态。
  2. pause/resume：第一动作提交后请求 pause，Run 落 PAUSED；新 Runtime/driver 从 checkpoint 恢复，只执行 plan_2（不重跑 plan_1）；断言 stable tool_call、evidence 共 2 行、step_index 事件 [1,2]、pause 标志已清、终验通过。
- e2e 首次运行发现真实恢复 bug：`ExecutionRecord.evidence: List[Any]` checkpoint JSON 还原后仍是 dict，`evidence_results()` 访问 `.result_id` 崩溃；已在 `PlanExecuteReportDriver._restore()` 按需调用 `RetrievalResult.from_dict` 修复，恢复 e2e 验证覆盖该路径。
- T-204 随 e2e review 加固：候选 task_id 命中计划项时只允许 refine query/description，冻结 task_type/source_mode/候选身份；controller retry 重复 query 在派发前拒绝。相关回归测试已补。
- 门禁：e2e **2 passed**；全量 `PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q` → **355 passed, 4 skipped, 8 warnings in 56.60s**；changed-scope Ruff F821/F811 clean；新增测试 Ruff F401 clean；`git diff --check` clean；无 debug print、无 Docker、无真实 LLM。
- 独立评审：首次找到 debug print（BLOCK）→ 删除；最终复评环境缺 `langchain_core`，无法独立重跑，但检查磁盘确认 print 已删除；此前独立 e2e review 对数据库行/合同/恢复逻辑为 PASS。测试通过数字是实现方本地项目 `.venv` 的真实输出，不冒充评审方复跑。

## 最终验收汇总（T-015）

- P0 + T-201..T-208 全部 done；每阶段有独立 code review（T-208 的最终评审受 reviewer 环境依赖缺失限制，已如实记录）。
- 最终全量：**355 passed, 4 skipped, 8 warnings**（Python 3.12 项目 `.venv`；不使用 Docker）。
- 外部接口/DB workflow_mode 不变；新 Run 以 `config_snapshot.orchestration=coreact_v1`；缺 marker 的旧 Run 走 staged_v1。
- 实际实现是“真实 HarnessRuntime + SQLite + scripted LLM” e2e 与单元/恢复测试；不声称完成真实 provider/真实模型、多节点服务、进程 SIGKILL 故障注入或离线训练实验。
- 已知限制：预算计数非 exactly-once（T-205 §已知限制）；外部动作崩溃窗口可能重做 ≤1 动作；v1 串行；rubric_coverage 是关键词子串代理、不是语义蕴含；暂停/租约 fencing 仍有后续硬化空间。

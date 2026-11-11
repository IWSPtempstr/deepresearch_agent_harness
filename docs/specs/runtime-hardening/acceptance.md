# Acceptance: T-016 角色级模型分层与运行时硬化

## P0 · 基线（2026-11-10）

- 命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
- 结果：**355 passed, 4 skipped, 8 warnings in 59.22s** — 全绿基线。
- 分支：`feat/t016-runtime-hardening`（自 main=acef9ce，即 T-015 合并后）。

## 决策记录

- 2026-11-10 用户决策：执行三部分——角色级模型分层（A）、T-015 硬化项（B：fencing/intent/ContextState/并行）、T-014 尾巴（C：6 文件拆分 + F401 清理）。
- 执行顺序：C → A → B（风险递增；C 机械可先锁定进度）。
- 继承 T-015 约束：只实现不做对比实验；不推送远端（待用户指令）；不使用 Docker。

## 验收矩阵（实现后逐项补证据）

| ID | 标准 | 证据 |
|---|---|---|
| AC-A1 | resolve_role_model 四层解析顺序正确 | 待实现（T-304） |
| AC-A2 | F2 组件实际模型 == recorded role_models（归因修复） | 待实现（T-305） |
| AC-A3 | model_snapshot.role_models 审计可查 | 待实现（T-304） |
| AC-B1 | 接管后旧 driver 步提交 → RunLeaseLost，无双写 | 待实现（T-306） |
| AC-B2 | 派发前 intent 行可见（task status=running） | 待实现（T-307） |
| AC-B3 | 验证失败 → uncertain 进 controller prompt；随 checkpoint 往返 | 待实现（T-308） |
| AC-B4 | MAX_PARALLEL=2 并发执行、逐动作验证、预算门生效 | 待实现（T-309） |
| AC-C1 | 6 文件 ≤500 行；settings 公开名字集合不变 | 待实现（T-301/302） |
| AC-C2 | src/tests 非 facade F401=0 | 待实现（T-303） |
| AC-10 | 每阶段全量 pytest 与基线一致（新增除外） | 每阶段 |

## T-310 · 收尾验收（2026-11-11）

- 全量：**372 passed, 4 skipped**（基线 355 + 新增 17）；F821/F811/F401 clean（changed-scope 与全仓）。
- 行数门禁：src/ 最大 575（settings_sections.py，纯配置常量、无逻辑，豁免）；6 个点名文件全部 ≤500。
- F401：299 处（T-014/T-301 机械复制）+ 8 处（T-015/T-016 新增测试）全清；facade 以 `__all__`/显式 noqa 豁免。
- **过程教训（两条）**：ruff F401 --fix 误删拆分模块必要 import（base_reporter re-export 被全量测试抓回）——拆分后改用 splitlib 生成 import + F821 门禁；`_skip_docstring` 不支持 docstring 前注释行导致 import 为空——drivers 文件踩中后修复 splitlib。
- e2e：2 passed（happy path + pause/resume，真实 Runtime + SQLite）。

## 已知限制（随实现更新）

- fencing 为 check-then-write（TOCTOU 窗口缩小非消除）；全量条件 UPDATE 留后续。
- intent 为任务粒度；工具调用级 intent 不做。
- 角色分层 v1 边界：research 工具内部 LLM、EvidenceCardPipeline 内部 LLM 仍用全局默认。
- ContextState 不对最终报告做语义强制。
- 角色分层收益无实验数据（沿用"只实现"约束）；成本结构可经 model_snapshot 审计。


## T-310 · 最终收尾（2026-11-11）

- 拆分收尾：T-016 三处新增长文件 `drivers_plan_execute.py`（967→123+4 Mixin）、`runtime_execution.py`（584→491+runtime_step_hooks 103）全部 ≤500；splitlib 更新以支持前导注释后的模块 docstring；全仓 F401 清理中 base_reporter re-export 曾被误删（测试捕获后恢复 + `__all__` 保护）；拆分 Mixin 多余 import 手工清理，不再对拆分产物盲用 ruff --fix。
- T-016 主审复核（main 合并后审查触发的修复分支）：
  1. K>1 并行块的 verdict 未定义 + ContextState 重复更新 → 修复；补 K=2 并行、fillup 去重、pending resume 测试。
  2. 并行补位重复 append 计划节点 → 改为复用已有 ready node；批内/历史重复 query 过滤；并发宽度受 max_concurrency 限制；批内各 verdict 更新 ContextState。
  3. LLM_ROLE_TIER_DEFAULTS 未在 settings_sections.__all__ 导出 → 修复，env→facade→resolver 可见。
  4. role_models 从 config_snapshot 错移到 model_snapshot；run_service factory 改优先消费冻结映射，记录的 role tier/model 与实际组件注入一致。
  5. 全仓 F401=0；新 F811（base_reporter 重复 ReporterConfig）清理。
  6. intent 前置状态顺序调整：pending_action/cursor 先进入 snapshot，之后派发前持久 intent；恢复时 unfinished pending action 直接重放、不重问 controller；attempt=2 重放后不叠加 retry。
  7. 预算 reserve：每个单动作/批次预留 controller + verifier 额度；并行批次预检 tool-call/tavily-call 预算。
- 最终门禁（项目 `.venv`）：全量 **376 passed, 4 skipped, 8 warnings in 56.33s**；全仓 F401 zero；coreact/harness changed-scope F821/F811 clean；`git diff --check` clean。
- 行数门禁：src/ 最大文件 `config/settings_sections.py`=576 行（纯配置常量分组、无逻辑且刚由 settings facade 拆分产出，design/proposal 列明豁免）；无其他 ≥500 行非豁免文件。
- **审查限制**：T-016 首次 PR #6 曾 squash 合并 `a5f4509`；后续审查发现并修复上述多项问题，当前 `fix/t016-review-blockers` 分支含修复提交，需另开 PR 合入。全量测试证据由本地 `.venv` 产生；部分独立复评 agent 环境缺 langchain_core，只做源码静态核查（有的结论 BLOCK 后均由本地测试/复评解除）。
- 已知限制保持：fencing 为 check-then-write（TOCTOU 缩小非消除）；intent 任务粒度；预算计数非 exactly-once；ContextState 不强制最终报告语义；并行只对独立就绪动作补位；无真实模型/真实 provider、无对比实验（用户目标是简历实现）。

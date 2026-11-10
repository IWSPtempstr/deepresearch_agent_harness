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

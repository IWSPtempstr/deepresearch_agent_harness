# Proposal: T-016 角色级模型分层与运行时硬化

## 1. 背景与问题

T-015 合并后（main=acef9ce）三项已记录的后续工作由用户决策（2026-11-10）一并执行：

1. **角色级模型分层**（前次链路分析的改进 A）：T-009 的 tier 路由止步于 Run 级，F2 各角色组件实际仍用全局默认模型（`base_planner.py:123`、`section_writer.py:69` 等 `get_llm_model()` 兜底）；T-205 评审亦确认"F2 recorded tier ≠ actual per-component model"的归因缺口。对应 Anthropic "lead 强模型 + subagent 便宜模型"的降本结构。
2. **T-015 硬化项**（acceptance 已知限制）：租约 fencing 缺失（写路径无 owner 校验）、动作无 pre-invoke intent 行（崩溃窗口内 in-flight 状态不可见）、上下文无可信度分层（QUEST 的 trusted/untrusted/uncertain）、coreact 串行。
3. **T-014 尾巴**：500–599 行的 6 个文件（incremental_update 561、settings 536、manual_edit_manager 529、embedding_manager 527、entity_merger 526、graph_consistency_validator 520）与全仓 F401 清理。

## 2. 场景与用户

- 角色：项目维护者（简历/面试展示）。
- 价值：分层模型展示成本工程（Run 的 token 成本结构可讲解）；fencing/intent 展示分布式正确性思维；ContextState 展示上下文工程。
- 目标流程：Run 创建时记录 role→model 审计映射；F2 各角色组件按角色分层取模型；coreact 步提交带租约校验、动作派发前落 intent 行；controller 决策上下文包含可信度分层的证据状态；独立候选任务可并行执行。

## 3. 目标

- [ ] 角色→模型解析链：显式 role model > 角色默认 tier（LLM_ROLE_TIER_DEFAULTS）> Run tier > 全局默认；role→model 映射写入 model_snapshot（可审计）。
- [ ] F2 注入点：Planner（complex 默认）、Reporter 主写作组件（standard 默认）、coreact Controller（standard 默认）按角色取模型；归因缺口修复（recorded == actual 可验证）。
- [ ] coreact 步提交前做租约 owner 校验（fencing，check-then-write）；失效即 RunLeaseLost 让位。
- [ ] 动作派发前持久化 plan/task intent 行（status=running）+ checkpoint；崩溃恢复可见 in-flight。
- [ ] ContextState（trusted/untrusted/uncertain + next_action）随步更新、进 controller 决策上下文、随 checkpoint 持久化。
- [ ] `COREACT_MAX_PARALLEL`（默认 1）：>1 时 controller 动作 + 候选图独立就绪任务并发执行（上限 K），逐动作验证，批次一次提交。
- [ ] 6 个 500–599 文件全部拆至 ≤500 行（settings.py 用 facade 兼容策略），`from deepresearch_agent.config.settings import X` 全量名字保持可导入。
- [ ] src/ 与 tests/ 的 F401 清零（facade 模块以 `__all__` 声明 re-export 意图后豁免）。

## 4. 非目标

- 不做：真实模型对比实验、token 节省百分比声明；research 工具内部 LLM 调用与 EvidenceCardPipeline 内部 LLM 的角色化（v1 边界，如实记录）；全写路径 fencing（v1 只 fence coreact 步提交与 checkpoint 写，TOCTOU 窗口如实记录）；工具调用级 intent（v1 为任务粒度）；ContextState 的最终报告语义强制。
- 不改：workflow_mode/API/前端/DB schema（fencing 用现有 lease 字段，不新增列）；两个工作流的外壳协议。

## 5. 范围与影响面

| 轨道 | 文件 |
|---|---|
| A 分层 | `models/model_tier.py`、`config/settings.py`、`backend/app/services/run_service.py`、`backend/app/services/chat_service.py`、`agents/multi_agent/integration/multi_agent_factory.py`、`agents/multi_agent/planner/base_planner.py`、`reporter/base_reporter.py`、`harness/coreact_controller.py` |
| B 硬化 | `harness/runtime_execution.py`、`runtime_persistence.py`、`drivers_plan_execute.py`、`coreact_state.py`、`coreact_controller.py` |
| C 拆分 | `integrations/build/incremental_update.py`、`integrations/build/incremental/manual_edit_manager.py`、`graph/indexing/embedding_manager.py`、`graph/processing/entity_merger.py`、`graph/graph_consistency_validator.py`、`config/settings.py`；F401：facade 加 `__all__` + ruff --fix（排除 facade） |
| 测试 | `tests/harness/test_role_tier.py`、`test_coreact_fencing.py`、`test_coreact_context_state.py`、`test_coreact_parallel.py`、复用 tests/retrieval、tests/integrations |

## 6. 约束与不变量

- 每阶段全量 pytest 与基线一致（新增除外）；changed-scope F821/F811 + 新测试 F401 clean。
- 确定性安全门（SourcePolicy/Contract/BudgetManager/租约）不被分层或并行化削弱；并行动作各自过 SourcePolicy 与预算检查。
- 分层只改"取哪个模型"，不改提示词结构（coreact prompt 分节新增除外）。
- 每阶段独立 code review 后本地提交；不使用 Docker；不推送远端（待用户指令）。

## 7. 风险与开放问题

| 问题 | 处理方式 |
|---|---|
| settings.py 拆分的 import 环与求值顺序 | 依赖单向化：core 原语（env helper/MAX_WORKERS 等）留 settings.py 上游子模块；拆分后 diff `dir(settings)` 名字集合必须一致；全量测试 + import 冒烟 |
| F401 fix 误删 facade re-export | 先给 facade 模块补 `__all__`，再对非 facade 文件 --fix；fix diff 人工过目 |
| 并行动作的预算与验证竞态 | 每动作独立 verify；批次提交前统一预算检查；超预算动作不派发 |
| fencing check-then-write 仍有 TOCTOU | v1 如实记录为"大幅缩小窗口"而非消除；全量条件 UPDATE 留后续 |
| 角色分层的实际收益无实验数据 | 只实现不实验（沿用 T-015 约束）；成本结构变化可从 model_snapshot 审计 |

## 8. 成功标准

- [ ] Given coreact Run，When 查 model_snapshot.role_models，Then planner/worker/reporter/controller 各有 (model,tier,reason) 且与组件实际构造一致。
- [ ] Given 租约被接管，When 旧 driver step_commit，Then RunLeaseLost 且无双写。
- [ ] Given 动作派发后崩溃，When 恢复，Then DB 可见 status=running 的 intent 行。
- [ ] Given 验证失败动作，When 构建 ContextState，Then untrusted/uncertain 进 controller prompt。
- [ ] Given COREACT_MAX_PARALLEL=2 且 2 个独立就绪任务，Then 并发执行、逐动作验证、行数正确。
- [ ] 6 文件 ≤500 行 + settings 导出名集合不变 + F401 清零（facade 豁免）。
- [ ] 验收命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q` 与 P0 基线一致（新增除外）。

## 9. 决策状态

- 状态：`proposed`（随首提交入库；实现即视为确认，冲突时回写）
- 日期：2026-11-10

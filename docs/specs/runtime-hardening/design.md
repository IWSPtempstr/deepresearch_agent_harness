# Design: T-016 角色级模型分层与运行时硬化

## 1. 设计摘要

三条轨道一个 spec：A) 角色级模型分层（解析链 + 三处注入点 + 审计）；B) coreact 四项硬化（fencing/intent/ContextState/有界并行）；C) T-014 尾巴（5 个类文件 splitlib 拆分 + settings.py facade 化拆分 + F401 清理）。全部在现有协议与 DB schema 内完成。

## 2. 轨道 A：角色级模型分层

### 解析链（`models/model_tier.py` 新增）
```python
ROLE_TIERS_DEFAULT = {"planner": "complex", "worker": "simple",
                      "reporter": "standard", "controller": "standard"}
def resolve_role_model(config_snapshot, *, role, settings=None) -> {"model","tier","reason"}
# 顺序：config_snapshot[f"{role}_model"]（显式覆盖）
#      → settings 角色默认 tier（LLM_ROLE_TIER_DEFAULTS="planner:complex,worker:simple,..."，可覆盖）
#      → LLM_TIER_{TIER}_MODEL → 全局 OPENAI_LLM_MODEL
```
- worker 角色的"证据检索"多数不走 LLM（tavily/graphrag provider）；该角色 tier 主要影响 research 工具内部与未来语义 verifier。
- 审计：`chat_service` 建 Run 时 model_snapshot 增 `role_models: {role: {model,tier,reason}}`；`run_service._tier_model_for` 保留（Run 级）但 F2 归因缺口修复——bundle 构造时实际注入。

### 注入点（v1 边界如实声明）
| 角色 | 注入位置 | 方式 |
|---|---|---|
| planner | `multi_agent_factory.create_default_bundle(llms={"planner": m})` → `BasePlanner(llm=m)`（子组件共享同一实例，现状即如此） | 构造参数 |
| reporter | `BaseReporter(outline_builder=OutlineBuilder(llm=m), section_writer=SectionWriter(llm=m, config=...))` | 构造参数（两组件已接受 llm） |
| controller | `orchestrator.controller_model` 改读注入的 planner 模型？否——controller 独立：`CoreActController(llm=role_models["controller"])`，driver 从 bundle/参数取 | driver 构造参数 |
| worker | v1 不注入（research 工具内部 LLM 留全局默认），如实记录 | — |

- `multi_agent_factory.create_default_bundle` 增 `role_models: dict | None`；`run_service` 的 F2 分支用 `_role_model_for(context, role)` 解析后传入。
- F1 `DeepResearchAgent` 维持 Run 级 tier（单 agent 无角色分解）。

## 3. 轨道 B：coreact 硬化

### B1 租约 fencing（check-then-write）
- `_make_step_commit` 闭包已持有 owner（`_execute_run_inner` 的 `owner`）；`_commit_coreact_step` 写入前查 `runs.get(run_id).lease_owner == owner`，不等 → raise `RunLeaseLost`（既有 handler 静默让位，无双写）。
- 同时 fence：步循环的 checkpoint 写（在 step_commit 内）与 `_persist_execution`/_persist_plan 调用——统一在 `_commit_coreact_step` 入口一次校验。
- driver 主循环每次迭代前由 runtime 注入的 `lease_probe`（同 pause_probe 模式）校验，失效提前让位。
- 如实记录：check-then-write 有 TOCTOU 窗口；全量条件 UPDATE（`WHERE lease_owner=?`）留后续。

### B2 pre-invoke intent 行（任务粒度）
- `_execute_coreact_action` 派发前：`_persist_plan`（新任务 status="running" 已入图）+ checkpoint（pending_action 已在 workflow_state）——intent = plan/task 行 + 游标快照。
- 崩溃恢复：DB 可见 status=running 的任务行；循环从 pending_action 重做该动作（现状语义），文档注明 in-flight 外部结果不可见。
- 工具调用级 intent 不做（executor 内部生成 tool_call_id，重构面大），如实记录。

### B3 ContextState
```python
class ContextState(BaseModel):  # coreact_state.py
    trusted_ids: list[str]        # 通过验证动作的账本证据
    untrusted_ids: list[str]      # source_policy/重复查询违规动作的证据
    uncertain: list[dict]         # {term, reason, next_action}（claim_support 未覆盖判词）
    updated_step: int
```
- 每步 commit 后由 driver 从 StepVerdict 更新；存 `workflow_state.coreact.context_state`（随 checkpoint 持久化）。
- 注入 controller prompt 新分节「已验证事实 / 存疑主张（含建议动作）」；Evidence Ledger 仍是唯一事实源（ContextState 只存 id 与元信息）。
- 不对最终报告做语义强制（如实记录）。

### B4 有界并行
- `COREACT_MAX_PARALLEL`（默认 1）。>1 时：controller 选定主动作后，从候选图**确定性**补齐独立就绪任务（无共享依赖、来源一致）至 K 个；`asyncio.gather`（每动作 `asyncio.to_thread`）并发执行；逐动作独立 verify（SourcePolicy/预算各自过）；批次一次 step_commit（幂等）；失败动作的重试串行追加在批次后（各至多一次）。
- step_index += 批大小；no_progress 按批内新增证据总数判定。

## 4. 轨道 C：T-014 尾巴

- 5 个类文件用 splitlib（T-014 已验证）拆至 ≤500，原文件类骨架 ≤200，facade 语义。
  - incremental_update.py 561 / manual_edit_manager.py 529 / embedding_manager.py 527 / entity_merger.py 526 / graph_consistency_validator.py 520
- settings.py（536）：拆为 `settings.py`（facade，re-export 全部名字）+ `settings_core.py`（env helper/MAX_WORKERS/模型与 API 基础项）+ `settings_sections.py`（检索/多智能体/报告/graph 等分组）。依赖单向：sections → core。**验收 = 拆分前后 `dir(settings)` 公开名字集合一致 + 全量测试。**
- F401：先给 T-014 facade 模块（deeper_research_tool/deep_research_tool/hybrid_tool/chain_of_exploration/thinking/evidence/workflow 等）补 `__all__`；然后对 src/ + tests/ 非 facade 文件 `ruff --select F401 --fix`；fix diff 人工过目 + 全量测试。
- memory/service.py 的 F811（2 处 redefinition）属既有兼容模式（有 noqa 注释），不在 F401 范围，如实保留。

## 5. 测试与观测

- A：解析顺序单测（显式/角色默认/Run tier/全局四层）、工厂注入断言（组件实际 llm.model）、model_snapshot 审计。
- B1：接管后旧 driver step_commit → RunLeaseLost、无双写（复用 T-205 测试基建）。
- B2：派发后崩溃恢复，DB 可见 running intent 行。
- B3：失败动作 → uncertain 进 prompt（脚本 LLM 断言 prompt 内容）；ContextState 随 checkpoint 往返。
- B4：K=2 双就绪任务并发、行数/事件正确、预算超限动作不派发。
- C：wc -l 门禁 + settings 名字集合一致性 + 全量测试。
- 观测：role_models 进 model_snapshot（既有 /runs API 已展示 model_snapshot）。

## 6. 替代方案与取舍

| 方案 | 取舍 |
|---|---|
| 全写路径条件 UPDATE fencing | 更强但侵入所有写路径；v1 check-then-write + 如实记录 |
| 工具调用级 intent | 需重构 executor 工具 ID 生成；v1 任务粒度 |
| ContextState 进最终报告强制 | 无法语义 enforcing；只做决策上下文与审计 |
| controller 多动作决策（批量返回） | 改决策 schema；v1 单决策 + 确定性并行补位 |
| settings.py 不拆（豁免） | 用户点名 6 个文件；facade 化后名字集合一致性可验证，风险可控 |

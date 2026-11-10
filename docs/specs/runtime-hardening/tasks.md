# Tasks: T-016 角色级模型分层与运行时硬化

> 执行约束：每阶段 = 实现 → 全量 pytest 门禁 → 独立 code review → 修复 → 单次本地提交。
> 测试命令：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q`
> 轨道执行顺序：C（机械低风险，先锁定进度）→ A（中等）→ B（最复杂）→ 收尾。

## P0 · 基线

- **状态**: done
- **证据**: 2026-11-10，分支 `feat/t016-runtime-hardening`（自 main=acef9ce），`pytest tests/ -q` → **355 passed, 4 skipped, 8 warnings in 59.22s**。

## T-301 · Track C：5 个类文件拆分

- **前置**: P0
- **涉及**: `integrations/build/incremental_update.py`、`integrations/build/incremental/manual_edit_manager.py`、`graph/indexing/embedding_manager.py`、`graph/processing/entity_merger.py`、`graph/graph_consistency_validator.py`
- **动作**: splitlib Mixin 拆分（骨架 ≤200、模块 ≤500、verbatim 断言）。
- **结果**: 5 文件 → 17 个 Mixin 模块 + 骨架（骨架 39–87 行、模块 62–264 行）；verbatim 全过；拆分后清理 stats 模块多余的 conflict_strategy 导入（F811）。
- **门禁**: 全量 355 passed/4 skipped（与基线一致）；F821/F811 clean。
- **状态**: done

## T-302 · Track C：settings.py facade 化拆分

- **前置**: T-301
- **涉及**: `config/settings.py`（18 行 facade）+ 新 `config/settings_core.py`（166 行：helpers/路径/基础设施）+ `config/settings_sections.py`（570 行：业务分组）
- **动作**: AST 验证依赖单向（core 引用 sections 名字=空）；sections 显式 import core 名字与 os/Path 模块；facade star re-export + 私有 helper/顶层 import（os/Path/Optional/load_dotenv）显式 re-export。
- **结果**: `dir(settings)` 公开名字集合拆分前后 **173 个名字完全一致**；config reload 测试按依赖顺序修正（core→sections→facade）。
- **门禁**: 全量 355 passed/4 skipped；F821/F811 clean。
- **状态**: done

## T-303 · Track C：F401 清理

- **前置**: T-302
- **涉及**: T-014 facade 模块（补 `__all__`）+ src/ tests/ 非 facade 文件 ruff --fix
- **动作**: `ruff --select F401 --fix`（排除 facade），fix diff 人工过目；memory/service.py F811 为既有兼容模式不在范围（如实记录）。
- **验收**: 全量测试绿；src/ tests/ 非 facade F401=0。
- **状态**: pending

## T-304 · Track A：角色级模型解析与审计

- **前置**: T-303
- **涉及**: `models/model_tier.py`（resolve_role_model + _role_tier + _model_for_tier）、`config/settings_sections.py`（LLM_ROLE_TIER_DEFAULTS）、`backend/app/services/chat_service.py`（model_snapshot.role_models）
- **结果**: 四层解析链实现（显式 role model > 角色默认 tier（env 可覆盖内置 planner:complex/worker:simple/reporter:standard/controller:standard）> Run tier 回落 > 全局）；畸形 env pair 忽略；7+4 用例。
- **状态**: done

## T-305 · Track A：F2 角色注入与归因修复

- **前置**: T-304
- **涉及**: `agents/multi_agent/integration/multi_agent_factory.py`（role_models 参数 + get_llm_model 实例化 + controller_model ID/实例分离）、`backend/app/services/run_service.py`（F2 分支 _role_model_for 解析注入）、`agents/multi_agent/orchestrator.py`（controller_model property setter，ID 审计/实例 invoke 分离）
- **结果**: planner→BasePlanner(llm)、reporter→OutlineBuilder/SectionWriter(llm)、controller→CoreActController(llm)；归因断言点 = LLM 实例的 model_name（recorded==actual 可测）；staged 构造无害。归因缺口修复。
- **边界如实记录**: worker 角色（research 工具内部 LLM）与 EvidenceCardPipeline 内部 LLM 仍走全局默认；F1 维持 Run 级 tier。
- **状态**: done

## T-306 · Track B1：coreact 步提交租约 fencing

- **前置**: T-305
- **涉及**: `harness/runtime_execution.py`（owner 闭包 + lease_probe + intent persist 共享 fencing）、`harness/runtime_persistence.py`（_commit_coreact_step 入口 owner 校验）
- **结果**: check-then-write fencing；失效 → RunLeaseLost 静默让位；driver 动作边界 lease_probe 提前让位；接管后无双写测试通过。
- **状态**: done

## T-307 · Track B2：pre-invoke intent 行

- **前置**: T-306
- **涉及**: `harness/drivers_plan_execute.py`（派发前 _persist_plan + checkpoint，intent 也过 fencing）、`harness/runtime_execution.py`（_make_intent_persist）
- **结果**: intent = plan/task 行（status=running）+ 游标 checkpoint；崩溃恢复可见 in-flight；工具调用级 intent 不做（如实记录）。intent 测试 2 例（含 stale owner 拒绝）。
- **状态**: done

## T-308 · Track B3：ContextState 可信度分层

- **前置**: T-307
- **涉及**: `harness/coreact_state.py`（ContextState 模型，随 LoopState 持久化）、`harness/drivers_plan_execute.py`（_update_context_state 步后更新 + controller prompt 分节）、`harness/coreact_controller.py`（DecisionContext.context_state + prompt 渲染）
- **结果**: trusted/untrusted/uncertain 随步更新、随 checkpoint 往返（roundtrip 测试）、controller prompt 含「已验证可信/不可信/存疑主张+建议动作」分节；Ledger 仍是事实源；报告不语义强制（如实记录）。4 用例。
- **状态**: done

## T-309 · Track B4：coreact 有界并行

- **前置**: T-308
- **涉及**: `harness/drivers_plan_execute.py`（COREACT_MAX_PARALLEL + 批量 admit（_admit_parallel_fillup）/gather 执行/逐动作验证/批次提交/失败动作串行定向重试）、`config/settings_sections.py`（COREACT_MAX_PARALLEL，默认 1）
- **结果**: 默认 1 行为不变（既有测试全过）；>1 时 controller 动作 + 候选补位并发执行，逐动作 verify，批次一次 commit（step_index += 批大小）；失败动作串行定向重试（allow_llm 与派发前重复查询拒绝均已接通——首轮评审发现 allow_llm=False 写死与 recent 快照过期两个回归，已修）。
- **状态**: done

## T-310 · 收尾

- **前置**: T-309
- **涉及**: acceptance.md 汇总；wc -l 复核；全量测试；drivers_plan_execute.py（967 行，T-015 期间增长）与 runtime_execution.py（584 行）补拆。
- **结果**: drivers_plan_execute → {coreact_loop 356, coreact_support 88, report_pipeline 281, driver_adapters 99} + 骨架 123；runtime_execution → runtime_step_hooks 103 + 主体 491。settings_sections.py（575）为纯配置常量分组、无逻辑，豁免并记录。F401 全仓归零（含 T-015 期间新增测试的 8 处）。
- **教训**: ruff F401 --fix 会误删拆分模块的必要 import 与 facade re-export——拆分后禁用 --fix，改用 splitlib 生成 import + F821 门禁；`_skip_docstring` 需支持 docstring 前的注释行（drivers 文件踩中）。
- **门禁**: 全量 372 passed/4 skipped；F821/F811/F401 clean；e2e 2 passed。
- **状态**: done

## 进度摘要

- 已完成：10（P0 + T-301..T-309）
- 进行中：0
- 阻塞：0
- 待执行：1（T-310 收尾）
- 最后更新：2026-11-11

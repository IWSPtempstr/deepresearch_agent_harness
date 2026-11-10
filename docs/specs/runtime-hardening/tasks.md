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
- **状态**: pending

## T-302 · Track C：settings.py facade 化拆分

- **前置**: T-301
- **涉及**: `config/settings.py` + 新 `config/settings_core.py`、`config/settings_sections.py`
- **动作**: 依赖单向化拆分；验收 = `dir(settings)` 公开名字集合前后一致 + 全量测试 + `RESEARCH_ORCHESTRATION` 等关键配置读取冒烟。
- **状态**: pending

## T-303 · Track C：F401 清理

- **前置**: T-302
- **涉及**: T-014 facade 模块（补 `__all__`）+ src/ tests/ 非 facade 文件 ruff --fix
- **动作**: `ruff --select F401 --fix`（排除 facade），fix diff 人工过目；memory/service.py F811 为既有兼容模式不在范围（如实记录）。
- **验收**: 全量测试绿；src/ tests/ 非 facade F401=0。
- **状态**: pending

## T-304 · Track A：角色级模型解析与审计

- **前置**: T-303
- **涉及**: `models/model_tier.py`（resolve_role_model）、`config/settings.py`（LLM_ROLE_TIER_DEFAULTS）、`backend/app/services/chat_service.py`（model_snapshot.role_models）
- **动作**: 四层解析链（显式 role model > 角色默认 tier > Run tier > 全局）；审计映射落 model_snapshot。
- **状态**: pending

## T-305 · Track A：F2 角色注入与归因修复

- **前置**: T-304
- **涉及**: `agents/multi_agent/integration/multi_agent_factory.py`（role_models 参数）、`backend/app/services/run_service.py`（F2 分支解析注入）、`harness/drivers_plan_execute.py`（controller 模型来源）、`reporter/base_reporter.py`（OutlineBuilder/SectionWriter 注入）
- **动作**: planner/reporter/controller 三角色注入；归因修复（recorded==actual）；v1 边界（worker/research 工具内部 LLM）如实记录。
- **状态**: pending

## T-306 · Track B1：coreact 步提交租约 fencing

- **前置**: T-305
- **涉及**: `harness/runtime_execution.py`（owner 闭包 + lease_probe）、`harness/runtime_persistence.py`（_commit_coreact_step 入口校验）
- **动作**: check-then-write fencing；失效 → RunLeaseLost 让位；接管后无双写测试。
- **状态**: pending

## T-307 · Track B2：pre-invoke intent 行

- **前置**: T-306
- **涉及**: `harness/drivers_plan_execute.py`（派发前 _persist_plan + checkpoint）
- **动作**: intent = plan/task 行（status=running）+ 游标快照；崩溃恢复可见 in-flight；工具调用级 intent 不做（如实记录）。
- **状态**: pending

## T-308 · Track B3：ContextState 可信度分层

- **前置**: T-307
- **涉及**: `harness/coreact_state.py`（ContextState 模型）、`harness/drivers_plan_execute.py`（步后更新 + controller prompt 分节）
- **动作**: trusted/untrusted/uncertain 随步更新、随 checkpoint 持久化、进 controller 决策上下文；Ledger 仍是事实源；报告不语义强制（如实记录）。
- **状态**: pending

## T-309 · Track B4：coreact 有界并行

- **前置**: T-308
- **涉及**: `harness/drivers_plan_execute.py`（COREACT_MAX_PARALLEL + 批量 admit/执行/提交）、`config/settings.py`
- **动作**: 默认 1（串行不变）；>1 时 controller 动作 + 确定性就绪补位并发执行、逐动作验证、批次一次提交、失败动作串行重试。
- **状态**: pending

## T-310 · 收尾

- **前置**: T-309
- **涉及**: acceptance.md 汇总；wc -l 复核；全量测试。
- **状态**: pending

## 进度摘要

- 已完成：1（P0）
- 进行中：0
- 阻塞：0
- 待执行：10
- 下一步：T-301
- 最后更新：2026-11-10

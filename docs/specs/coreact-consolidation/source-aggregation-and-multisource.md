# 来源通道聚合与 Run 级多来源冻结（BC-06 后续）

## 1. 状态与证据边界

- 状态：implemented（S1～S5 按阶段实现、测试、独立 review、本地提交）。
- 核实基线：`0e4862c`（BC-01～05 全部完成并推送）。
- 本文只依据源码静态核对；不宣称真实 provider/LLM 端到端效果。测试以 scripted / fake provider 为主；外部 API 的真实网络行为需另行验收。
- 约束：不改写历史 Run/evidence；不缩窄既有 DB CHECK；旧 scholar Run 保持只读可恢复。

## 2. 当前实现与缺口（0e4862c）

| 能力 | 已实现 | 尚缺 |
|---|---|---|
| 来源通道 | `SourceMode = graphrag \| web \| scholar`；每 Run 冻结**单一**通道（`ResearchBrief._freeze_sources`） | scholar 是独立通道中的单一 API 组合；web 不能同时使用多种 API |
| 产品面 | 前端 `SourceMode = graphrag \| web`；capabilities 只暴露两者 | scholar 在后端/测试存在但产品面不可达 |
| 工具白名单 | 5 处独立映射（research_brief / coreact_controller / coreact_loop / policies / brief_builder） | 无“通道→多 API 工具”的统一映射 |
| 证据归属 | 每行 `source_mode` + `provider`；`source_match` 要求全部等于单一冻结值 | 混合通道证据无法通过；provider 与通道一致性未校验 |
| 预算 | `max_tavily_calls` 单一 web 计数 | scholar API 无独立计数；聚合后 tavily 计数会被误用 |
| 查重 | 按 query 全局查重 | 同一 query 无法同时打到 Tavily 与 OpenAlex/arXiv |
| 多来源冻结 | 无 | `sources` 集合、brief v2、组合 router、多通道准入/契约均缺失 |

## 3. 设计

### 3.1 通道与 API 分层

- **`source_mode` = 冻结通道**；**`provider` = 通道内的具体 API**。
- `web` 通道聚合多个 API：Tavily（`web_search`）、OpenAlex/arXiv（`scholar_search`）。
  证据行仍写 `source_mode='web'`，`provider`/metadata 记录 `tavily|openalex|arxiv`。
- `scholar` 枚举保留为**只读兼容**：历史 Run/evidence 仍可解码、恢复；新请求在 API
  边界归一化为 `web`（沿用 T-407 `deep_research → plan_execute_report` 的先例）。
- 不改 `ck_runs_source_mode` / `ck_evidence_source_mode`，零 DDL。

### 3.2 Run 级多来源冻结

- Run 创建时冻结 `sources: list[SourceMode]`（首个为主通道，写入 `runs.source_mode`；
  完整集合写入 `config_snapshot["sources"]` 与 `ResearchBrief.allowed_sources`）。
- `ResearchBrief` schema v2：允许 `allowed_sources` 为多元素；v1 载荷（无 `sources`）
  按 `{source_mode}` 只读解释。高版本拒绝（`UnknownSchemaVersion`），不静默降级。
- 冻结集合不可在运行中扩大；`with_revision()` 仍禁止修改来源/预算。
- 执行层：白名单 = 各通道工具并集；`task.source_mode ∈ 冻结集合`；组合 router 按
  `tool → channel` 反查派发；证据按实际通道记录。
- 验收语义：`source_match` 改为“证据通道 ∈ 冻结集合 **且** provider 与通道匹配”；
  `source_diversity` 增加通道披露，通过规则仍为“≥2 独立来源或披露局限”。

### 3.3 诚实失败与成本

- 部分通道失败必须记录成功/失败通道，不得把单通道结果伪装成全量完成。
- 预算：`max_tool_calls`/`max_llm_tokens` 共享总额；`tavily_calls` 与
  `scholar_api_calls` 分别计数（S1 起生效），避免聚合后串账。

## 4. 实施任务

### S1 — scholar 纳入 web（聚合通道）

- 状态：done（2026-11-14）
- 依赖：none
- 触点：`harness/research_brief.py`、`policies.py`、`coreact_controller.py`、
  `coreact_loop.py`、`brief_builder.py`、新增 `retrieval/web_aggregate.py`、
  `retrieval/router.py`、`executor/retrieval_executor.py`、`backend/app/schemas/api.py`、
  `backend/app/api/v1/health.py`、`harness/budgets.py`、`config/settings_sections.py`、
  `harness/runtime_persistence.py`、新增 `harness/dedupe.py`、`action_runner.py`、
  `pre_action_advisor.py`、`coreact_support.py`。
- 动作：
  1. `web` 工具白名单加入 `scholar_search`（统一常量，5 处映射同步）；`scholar` 白名单保留给旧 Run。
  2. `AggregateWebProvider`：`web_search → Tavily`、`scholar_search → ChainedScholarProvider`；
     将 scholar 结果的 `source_mode` 归一为 `web`，provider 归属保留（`tavily|scholar`）。
  3. API 边界 `source_mode='scholar'` 归一化为 `web`；旧 Run 读取不受影响。
  4. 预算新增 `max_scholar_api_calls` / `scholar_api_calls`；提交层按工具名分别计数。
  5. 查重键改为 `(tool, query)`；同一 query 允许不同 API。
  6. capabilities：`web.available = tavily ∨ scholar`，并披露可用 API 列表。
- 验收：
  - API 提交 scholar → Run 冻结为 web；旧 scholar Run 仍可恢复执行。
  - `web` 白名单含双工具；controller 菜单/准入/policy 一致。
  - 聚合 provider 路由正确、证据 `source_mode='web'` 且 provider 归属正确。
  - tavily/scholar 计数分离且各自上限生效。
  - 同 query 不同工具可执行；同工具同 query 仍拒绝。
  - 现有单来源 web/graphrag 行为与测试不回归。
- 实际证据：`harness/policies.py` 收敛为单一事实源（`SOURCE_TASK_TYPES` /
  `ALLOWED_TOOLSETS` / `TOOL_SOURCE` / `TOOL_API`）；web 任务集为
  `{web_search, scholar_search}`，scholar 键保留只读兼容。`research_brief.SOURCE_TOOL_WHITELIST["web"]`
  与 controller/loop 白名单同步。新增 `retrieval/web_aggregate.py`：
  `web_search→Tavily`、`scholar_search→ChainedScholarProvider`，子结果统一
  `source_mode='web'` 并在 metadata 保留 `provider=tavily|scholar`；
  未配置 Tavily 时 `web_search` 明确 `SOURCE_UNAVAILABLE`，不静默换通道。
  router 的 web 注册为聚合 provider（scholar 独立路由保留给历史 Run）；
  executor 按 task_type 派发并以 `AggregateWebProvider.provider_label_for`
  记录证据归属。`MessageSend.source_mode='scholar'` 在 API 边界归一化为 web
  （历史 Run 只读）。预算新增 `max_scholar_api_calls` / `scholar_api_calls`，
  提交层按工具名分账（Tavily/学术互不串账）。查重键统一为
  `harness/dedupe.query_dedupe_key(tool, query)`，同 query 不同 API 允许、
  同工具重复仍拒绝（action_runner/advisor/verifier/coreact recent/correction
  全部对齐）。BriefBuilder 在 web 通道预留学术候选，确定性路径也能用上
  scholar_search。capabilities 增加 `web_providers` 披露 Tavily/学术 API；
  web 是否可选仍由主 API（Tavily）门控，避免默认 web_search 路径不可用。
  `tests/harness/test_web_aggregation.py` 15 项：白名单/policy/controller 一致、
  聚合路由与 source_mode 归一、缺失 Tavily 显式失败、executor 学术工具归属、
  router 兼容、预算分账、同 query 跨工具去重、API 边界归一、scholar-only 规划、
  skill policy、lazy provider 并发单次初始化、aclose、legacy raw 去重保守语义、
  未注入 client 的按调用创建/关闭。独立 review 2 轮（round1 2 HIGH+3 MEDIUM；
  round2 2 MEDIUM：scholar HTTP client 跨 loop 竞态、legacy raw 跨工具保守语义；
  已修复/记录并补测后 APPROVE）。门禁：pytest 654 passed/4 skipped、ruff、
  git diff --check。

### S2 — 多来源冻结契约（v2，零 DDL）

- 状态：done（2026-11-14）
- 依赖：S1
- 触点：`harness/research_brief.py`、`harness/run_context.py`、brief/context/run_service、
  `backend/app/schemas/api.py`、`backend/app/services/chat_service.py`。
- 动作：
  1. `ResearchBrief` v2：`allowed_sources` 允许 ≥1 个通道；v1 载荷只读兼容；高版本拒绝。
  2. `config_snapshot["sources"]` 冻结集合（首个为主通道）；`RunContext.sources`。
  3. `MessageSend.sources`（可选）：校验非空、去重、含主通道、均在可用通道内；
     未提供时默认 `[source_mode]`。
  4. brief 生成/修订写入冻结集合；`with_revision` 不允许改来源。
- 验收：v1/v2 roundtrip 与版本拒绝；sources 在主通道缺失/空/重复时拒绝；
  旧 Run（无 sources）按单通道解释；修订不可扩通道。
- 实际证据：`RESEARCH_BRIEF_SCHEMA_VERSION=2`：v2 允许多元素
  `allowed_sources`（首个为主通道），v1 载荷仍只读且必须单来源；高版本
  `UnknownSchemaVersion`。`allowed_tools` 为各通道白名单并集，
  `effective_sources` 暴露冻结集合；`with_revision` 不可改来源。
  `RunContext.sources` + `sources_from_config`：缺失/损坏/空值回退
  `{source_mode}`，保证主通道在首位；`_load_context` 以 Run 行为权威恢复
  集合（旧 checkpoint 兼容）。`MessageSend.sources` 可选，scholar→web 归一、
  去重、非空、必须包含主通道；ChatService 将 `[source_mode, ...]` 冻结进
  config_snapshot。Driver 的 planning 快照改为 config+context 合并，确保
  `sources`/`web_apis` 进入 BriefBuilder。`ResearchBrief` 若发现集合变化直接
  拒绝（Run 内不可扩/改来源）。API 422 处理器对 pydantic `ctx.error` 做
  字符串化，避免异常对象导致二次序列化失败。`tests/harness/test_multisource_contract.py`
  7 项 + API 用例：v2 roundtrip/工具并集、v1 单来源与高版本拒绝、修订不可
  改来源、RunContext 校验/回退、config 解析容错、driver 快照、BriefBuilder
  多来源 brief、API 多来源冻结与非法集合 422。
  独立 review 1 轮（2 HIGH：planning/context 快照可覆盖冻结集合、restore 信任
  checkpoint 主通道；2 MEDIUM：缺主通道的损坏列表被“补全”成并集、API 静默去重
  绕过 max_length/重复拒绝；1 既有缺口：未来版本 brief 被当作损坏重写）全部
  修复：冻结键只认 Run 行 config（checkpoint/Context Pack 不得覆盖，缺失即移除）、
  restore 校验 checkpoint 主通道与 Run 行一致否则大声失败、`sources_from_config`
  对缺主通道 fail closed 回退单通道、API 只做别名归一（重复/超长/别名冲突一律
  422）、BriefBuilder 用 `from_payload` 让高版本 brief 显式拒绝。复检门禁：
  pytest 667 passed/4 skipped、ruff、git diff --check。

### S3 — 执行层多来源

- 状态：done（2026-11-14）
- 依赖：S2
- 触点：`retrieval/router.py`、`retrieval/web_aggregate.py`、`retrieval_executor.py`、
  `runtime_persistence.py`/`run_service.py`、`action_runner.py`、`coreact_controller.py`、
  `coreact_loop.py`、`harness/policies.py`。
- 动作：
  1. `RetrievalRouter.for_modes(sources)` → 组合 provider；按 `tool → channel` 派发；
     每个结果携带真实通道；`TaskNode.source_mode` 必须 ∈ 冻结集合。
  2. 准入白名单 = 并集；`ActionSpec`/controller/loop 的 `==` 改为 `∈`。
  3. 组合 provider 的 tool 不可用（该通道未配置）→ `SOURCE_UNAVAILABLE`，
     不静默换通道。
  4. 失败/成功通道记录在事件/记录元数据（部分失败不伪装全量）。
- 验收：graphrag+web 同 Run 混合证据按通道落账；单来源回归；未配置通道的
  动作被拒绝；恢复重放使用同一冻结集合。
- 实际证据：`RetrievalRouter.for_modes`——单来源原样返回（legacy 语义），
  多来源返回 `CompositeRetrievalProvider`（mode=主通道，`sources` 冻结集合，
  `channel_for(tool)` 反查派发，未知/越界工具 `SOURCE_UNAVAILABLE`，aclose
  遍历成员）；冻结集合含未配置通道 fail fast。executor：`for_modes(冻结集合)`
  取 provider；`task.source_mode ∈ 冻结集合`（超界 ValueError，由 ActionRunner
  记为动作失败）；单来源保留严格一致性检查；组合派发时 `call_context`/policy
  按任务通道执行，结果 `source_mode` 必须等于任务通道（伪造归属拒绝）；
  `ToolCall.source_mode`、`tool_calls` 行按任务通道落账（预算仍按
  `TOOL_API` 分账）。Controller `DecisionContext.sources` + 三处决策上下文
  （主/修订/重试）携带冻结集合；`_decide_deterministic`/retry 校验/新动作
  准入全部改为集合成员判断；`coreact_loop` 的 eligible/并行补位/白名单改为
  冻结集合并集（`SOURCE_TASK_TYPES` union）。`run_service` 用
  `router.for_modes(context.effective_sources)`；`chat_service` 冻结集合写入
  config_snapshot（`frozen_sources` 属性）；评测 Run 亦冻结单通道集合。
  BriefBuilder 候选方向按冻结来源轮转派发（主通道优先，web 学术槽保持）。
  `tests/harness/test_multisource_execution.py` 6 项 + API 回归：组合路由与
  call_context 通道改写、未知工具 SOURCE_UNAVAILABLE、for_modes fail fast/
  单来源不变、executor 三通道混合执行与归属标签、超界任务拒绝、controller
  集合成员判断、policy TOOL_SOURCE 映射。独立 review 1 轮（1 BLOCKER：生产路径
  `TimeoutBoundProvider(组合)` 对通道解析不透明导致非主通道任务全失败——executor
  增加包装层解包并补生产接线回归测试；1 HIGH：`tasks` 行仍盖 Run 主通道——
  `save_plan` 改按任务通道；2 MEDIUM：controller LLM 只渲染主通道/新动作强制主
  通道——prompt 渲染冻结集合与各通道工具、新动作通道由 `TOOL_SOURCE` 反查；
  通道/工具错配无防御——executor 校验 `task.source_mode == TOOL_SOURCE[tool]`
  （legacy scholar 豁免）；若干 LOW：for_modes 非法通道 AppError、候选总数预算
  化（方向+学术槽+follow-up 计入 max）、`__all__` 导出）。已知依赖：**S4 之前**
  多来源 Run 的 `source_match` 仍按主通道校验（required），混合证据会在验收期
  失败——这是 S4 的任务，不在 S3 范围内放宽。门禁：pytest 676 passed/4 skipped、
  ruff、git diff --check。

### S4 — 验收语义与报告披露

- 状态：done（2026-11-14）
- 依赖：S3
- 触点：`harness/verifiers/deterministic.py`、`harness/contracts.py`、
  `harness/runtime_execution.py`、`harness/rubric_builder.py`（source policy 判据）。
- 动作：
  1. `source_match`：证据通道 ∈ 冻结集合，且 provider 与通道映射一致（防伪造归属）；
     `verifier_version` 升级并记录。
  2. `source_diversity`：披露实际通道集合；通过规则仍为 ≥2 独立来源或披露局限。
  3. rubric 的 `source_policy` 固定判据按冻结集合生成，不接受模型扩大。
- 验收：混合来源报告 Contract 通过；伪造 provider/通道不一致失败；单来源行为不变；
  required 的 source_policy 不因多来源被绕过。
- 实际证据：`DeterministicVerifiers` v2.0——`allowed_sources` 冻结集合（缺省
  单来源旧语义）；`source_match` 改为「证据通道 ∈ 冻结集合 **且** 已知 provider
  与通道一致」（PROVIDER_CHANNEL 映射；未知标签跳过一致性检查以兼容存量
  工具名标注），`observed` 披露 outside_allowed/provider_mismatch；
  `source_diversity` 按证据自身通道做 web 域名归并并披露 `channels`。
  `ContractEvaluator.evaluate` 增加 `allowed_sources` 透传；runtime VERIFYING
  以 `context.effective_sources` 验收。rubric `source_policy` 固定判据已由 S3
  的工具并集覆盖（`allowed_tools` 排序文案 + StepVerifier/Advisor 消费同一
  并集），模型不可扩大。`tests/harness/test_multisource_acceptance.py` 5 项：
  混合通道通过、越界通道失败、provider/通道伪造失败、单来源缺省语义不变、
  Evaluator 端到端（版本 2.0 + channels 披露）。门禁：pytest 681 passed/
  4 skipped、ruff、git diff --check。独立 review 1 轮（APPROVE + 3 MEDIUM：
  evaluation 服务泄漏指标仍按主通道——改为冻结集合判定且域名按证据自身通道
  归并；`scholar_search` 双时代语义无法纳入一致性映射——映射注释明确双时代
  语义并以集合成员/S3 executor 防御兜底，补 legacy/聚合两代回归测试；BC-04
  跨通道修订直到验收期才失败——`_prepare_artifact_edit` 继承前校验基线证据
  通道 ⊆ 冻结集合，越界 ARTIFACT_INTEGRITY_FAILED，补测试；LOW：PROVIDER_CHANNEL
  ClassVar、pytest 重复 import、继承行/legacy scholar 回归用例）。复检门禁：
  pytest 685 passed/4 skipped、ruff、git diff --check。

### S5 — 前端多选与披露

- 状态：pending
- 依赖：S3
- 触点：`frontend/src/types/api.ts`、`SourceSelector.tsx`、`ChatPage.tsx`、
  `useRunEvents` 展示、`ReportView`/证据台账。
- 动作：多选通道（至少一个）、提交 `sources`；Run 头部显示冻结集合；
  证据台账显示通道/API；capabilities 展示 web 可用 API。
- 验收：真实浏览器（fake driver）多选创建 Run、冻结集合展示、证据归属可见；
  空来源/非法来源被拒绝。

## 5. 验收矩阵（实施后）

| 项 | 任务 | 当前结果 |
|---|---|---|
| scholar API 纳入 web 聚合、旧 scholar 只读兼容 | S1 | PASS（白名单/policy/controller 统一；聚合路由 + 证据归属；tavily/scholar 预算分账；(tool,query) 去重；API 边界归一；pytest 654 passed/4 skipped） |
| 多来源冻结契约 v2 与 v1 兼容 | S2 | PASS（brief v2/单来源 v1/高版本拒绝；RunContext + config 冻结且 checkpoint/上下文不得覆盖；API sources 校验；pytest 667 passed/4 skipped） |
| 组合 router / 多通道准入 / 预算分离 | S3 | PASS（for_modes 组合派发 + fail fast；executor/controller/loop 集合成员判断；生产 TimeoutBound 包装回归；tasks/tool_calls 按通道落账；pytest 676 passed/4 skipped；注：source_match 放宽属 S4） |
| source_match/diversity/rubric 多来源语义 | S4 | PASS（verifier v2.0：集合成员 + provider/通道一致性；diversity 通道披露；BC-04 跨通道继承前置拒绝；evaluation 泄漏指标按冻结集合；pytest 685 passed/4 skipped） |
| 前端多选与冻结集合披露 | S5 | pending |

## 6. 范围声明

- 不改写历史 Run/evidence，不清库；旧 scholar Run 保持只读可恢复。
- 不缩窄 DB CHECK；不引入新表。
- 真实 Tavily/OpenAlex/arXiv 网络与限流行为未在本轮验证；测试以 fake/scripted provider 为主。
- 不自动部署/推送；每阶段实现→测试→独立 review→修复/复审→本地提交。

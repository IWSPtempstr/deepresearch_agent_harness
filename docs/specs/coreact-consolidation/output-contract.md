# T-017 输出契约：用户报告与内部动作结果

- 状态：T-402 已冻结 schema（2026-11-11）；行为接线在 T-403..T-407 完成，API/OpenAPI 冻结在 T-407。
- 输出字段、状态语义和约束以本文件为单一权威来源，proposal/tasks/acceptance 引用本文件。
- 实现位置：`harness/research_brief.py`（ResearchBrief/EvidenceAssessment/角色规则）、
  `harness/action_models.py`（ActionSpec/ActionRubric/ActionResult/ObservationSummary/
  ExecutionState 及旧 TaskNode/TaskRubric/CoreActLoopState 适配器）、
  `harness/run_result.py`（ResearchRunResult）。schema_version 当前均为 1；
  高于实现的版本一律拒绝（UnknownSchemaVersion / LegacyStateUnsupported）。

## 1. 交付分层

| 层 | 类型 | 消费方 | 权威性 |
|---|---|---|---|
| 用户正文 | UTF-8 Markdown 研究报告 | 报告页、下载、章节修订 | 只有最终 Contract 通过才是已验收报告；不等于事实真值 |
| 用户结果封装 | ResearchRunResult（JSON 逻辑视图） | API/前端/CLI | Run 状态由持久层决定，模型不能自行写 completed |
| 动作结果 | ActionResult（JSON 可序列化） | ActionRunner、Controller、审计 | 动作执行成功与动作验收通过分开 |
| 有界观察 | ObservationSummary | Controller | 账本内容的有损摘要，非新的事实源 |
| 证据/产物 | Evidence Ledger + Artifact metadata | Verifier、Reporter、来源查看 | 当前 Run 证据与已授权继承证据的可追溯存储 |

## 2. Markdown 研究报告

推荐完整研究任务使用以下结构，ResearchBrief 可按需求冻结必需章节；短定义/摘要只需“结论—依据—局限”，不能凑长文。

```markdown
# 研究主题

## 摘要与主要结论
直接回答问题，关键主张附就近证据引用。[ev_example]

## 研究范围
说明问题、时间/来源范围和排除项。

## 研究发现
### 发现一
说明事实、证据与分析，区分来源原话和推断。[ev_example]

## 对比与分析
按需求提供表格、时间线或论证；没有证据不编造指标。

## 建议与适用条件
推荐方案、适用前提与权衡。

## 局限与未解决问题
明确缺失、冲突、未确认信息及数据时效。

## 来源
来源标题、公开 URL 或授权私域文档定位信息。
```

- `[ev_example]` 仅为文档占位示意；真实引用必须为当前 Run ledger 内的稳定 ID，或按修订继承协议归入新 Run 的 ID。兼容历史 `[^ev_…]` 解析形式，写出统一形式由实现任务冻结。
- 前端可将 ID 显示为数字按钮，但必须保留到 evidence/来源的映射。
- 不自动给不相关主张挂全局证据充当语义支持；缺证据进入补检索、局限或失败。
- 不暴露 chain-of-thought、rubric 内部提示、系统指令、工具日志、密钥或未授权文档内容。
- 主文与证据台账/验收结果分开；通过关键词检查不写成“事实核验通过”。

## 3. ResearchRunResult（拟议逻辑结构）

```json
{
  "schema_version": 1,
  "run_id": "run_example",
  "status": "completed",
  "result": {
    "format": "markdown",
    "report": "# 研究报告\n\n示意正文",
    "artifact_id": "artifact_example",
    "evidence_ids": ["ev_example"]
  },
  "verification": {
    "status": "passed",
    "checks": []
  },
  "limitations": [],
  "clarification": null,
  "error": null,
  "allowed_actions": []
}
```

该结构可由现有 run/report/evidence 多个端点组装；不要求一次响应塞满所有证据或强制新增同名端点。大报告可分页或只返回 artifact 引用。实施前必须冻结 OpenAPI/DTO、前端类型及版本兼容，不直接破坏既有客户端。

### 状态约束

| Run status | 必需表现 | 禁止表现 |
|---|---|---|
| completed | result 是已验收报告；verification.status=passed；可查看来源/下载 | 仅因生成文本就 completed |
| needs_user_input | 实际 questions、请求版本、允许提交补充答案/取消 | 仅泛化“请补充信息”；模型继续消耗研究预算 |
| failed / budget_exhausted | 业务错误码、原因、已完成范围、未解决问题、合法恢复建议 | 无限“生成中”；把失败草稿当最终报告 |
| paused / interrupted | 当前进度、可恢复性与 allowed_actions | 声称调研已完成 |
| queued / executing / reporting / verifying 等 | 与持久状态一致的进度与操作 | API 因无本地 task 就确认远端 worker 停止 |

- result 在非 completed 时默认 null。可选草稿另存为显式 `draft` 视图，包含 `verified=false`、拒绝原因和权限检查，不能混入最终 result。
- `verification.status` 为 passed/failed/unknown/not_run；required check 的 unknown 不可支持 completed。
- `clarification` 拟含 question_id、questions、revision、answered_at/answers 引用；答案作为用户补充输入持久化，恢复规划必须消费对应 revision。
- `error` 拟含 code/message/retryable/details；保留 AppError 业务码，不退化成类名。
- `allowed_actions` 从持久状态和服务端能力派生，不由 LLM 任意给出；UI 与 API 的状态门一致。

### 澄清协议（BC-01 实现）

Run `config_snapshot` 持久化两个键（旧 Run 只有 `clarification_question` 时按只读回退）：

```json
{
  "clarification_request": {
    "schema_version": 1, "question_id": "clar_1_ab12cd34",
    "questions": ["实际问题 1", "实际问题 2"], "revision": 1,
    "source": "brief|context_builder|manual", "asked_at": "ISO8601",
    "status": "pending|answered", "answered_at": null, "answer_id": null
  },
  "clarifications": [
    {"schema_version": 1, "clarification_id": "clarify-<hash>", "content": "用户回答",
     "question_id": "clar_1_ab12cd34", "questions": ["实际问题 1"],
     "revision": 1, "message_id": "msg_...", "created_at": "ISO8601"}
  ]
}
```

- `POST /runs/{id}/clarifications` 接受 `content`、可选 `client_clarification_id`（幂等键）
  与 `question_id`；同键同内容重复提交返回成功且不重复写消息/历史，同键不同内容或
  回答错版本（question_id 不匹配）返回 409。
- 回答作为 user message 原子写入会话（含 `clarification` 元数据），ContextBuilder 把它
  并入 `resolved_query` 与受保护上下文块，BriefBuilder 并入
  `PlanContext.clarification_history`；同一 Run 已有回答时不再重复要求澄清。
- 回答只是用户输入约束，不是研究证据，不参与 Completion Contract。

## 4. ActionResult / ObservationSummary

```json
{
  "schema_version": 1,
  "run_id": "run_example",
  "action_id": "action_example",
  "task_id": "task_example",
  "attempt_id": "attempt_example",
  "attempt": 1,
  "execution_status": "succeeded",
  "verification": {
    "status": "passed",
    "verifier_version": "example_v1",
    "failed_criteria": []
  },
  "evidence_refs": ["ev_example"],
  "observation": {
    "findings": [
      {"statement": "本次得到的研究发现", "evidence_ids": ["ev_example"]}
    ],
    "contradictions": [],
    "unanswered_questions": [],
    "suggested_followups": []
  },
  "record_refs": [],
  "delta_ref": null,
  "usage": {"tool_calls": 1, "input_tokens": 0, "output_tokens": 0},
  "error": null
}
```

- 将口语示例中的单一 status 明确为 `execution_status`（succeeded/failed/cancelled/unknown）及独立 verification.status：工具返回成功不等于证据满足动作目标。
- ObservationSummary 即上例 observation 部分；记录内部可存 observation_ref 替代内嵌对象，但恢复必须能解析同一内容与版本。
- 纯检索调用可无模型 token；未知 usage 用 null/unknown，不造零成本。工具响应不可作为动作模型的训练 transcript 或计费唯一真值。
- findings 的每个 ID 在合并时必须通过当前 Run ledger/授权 provenance 校验；外部检索响应不能自行宣称 ledger 成员。
- 失败尝试保留其记录、费用和 verdict；active_result_refs 只选择最新有效结果，不抹去旧 attempt，不重复核销同一已提交 attempt。
- 工具原文、错误堆栈、模型原始响应、delta 存内部账本/工件；Controller 只接有界摘要。条数/字符/token 上限在 T-402/T-405 冻结并测试超限处理。
- 无证据的解释或猜测不得伪装成 finding；未解决或冲突内容单独列入 unanswered_questions/contradictions。

## 5. 修订输出

ResearchRunResult 可附 revision metadata（base_run_id、base_artifact_id/hash、target_section、changed_sections、citation_mapping/provenance_ref）。

- 修改章节之外的含义和布局保持；为满足新 Run 引用约束允许映射旧 evidence ID，因此不承诺未改段落字节完全一致。
- 旧证据继承到新 Run 后保留 original_run_id/original_evidence_id/source/hash；只允许同归属且符合新 Run 冻结来源策略的证据。
- 基线 hash 变化、章节不唯一、来源不匹配、越权必须明确拒绝或澄清，不静默退化为普通研究。
- 下载是已授权已验收 Markdown artifact；草稿下载若支持，应明确未验证且使用不同文件标识。

## 6. 必须验证的协议不变量

1. JSON roundtrip、状态/字段组合、未知版本处理与旧 API 兼容。
2. completed 一定由 Contract 判定；failed 草稿不进入最终报告；终态不再显示生成中。
3. 引用按钮/Markdown/下载/证据台账引用同一组真实 ID。
4. ActionResult execution/verification 可独立失败；保留失败 attempt 与实际费用。
5. ObservationSummary 与 rubric 不外发为 Provider 参数，不泄露私域权限/内部提示。
6. 修订结果能通过新 Run ledger 检查，且来源继承链可审计。


## 后续扩展：Prompt-only Rubric 与业务余项

详见 [rubric-guidance-followup.md](rubric-guidance-followup.md)。该文档是本规范的拟议兼容增量，记录 RG-01～RG-04 和 BC-01～BC-05（全部 pending），不重置 T-401～T-409 的历史完成状态。

区分动作前指导/建议性验证与动作后验收；advisory unknown 允许显式降级，required unknown 仍不得通过。RACE 风格质量维度不是官方 DRB 分数；不承诺复现论文增益。本次仅写文档，尚未实现这些后续项。

## 4.1 Rubric 指导协议（RG-01 增量，兼容旧记录）

动作前指导与动作后验收共用同一 rubric 身份（`rubric_id` + `content_hash`），
但判定独立保存、不得混同。字段定义见 `harness/action_models.py`：

| 类型 | 字段 | 语义 |
|---|---|---|
| ActionRubric | `content_hash` | 准则内容稳定指纹（不含 task 绑定）；前后判定须一致 |
| ActionRubric | `source` | `model` / `deterministic_fallback` / `fixed`（受控配置），用于来源审计 |
| ActionRubric | `phase` | `explore` / `deepen` / `synthesize`（确定性规则产生，版本随 rubric 固定） |
| ActionRubric | `generator_model` / `prompt_version` / `trajectory_ref` / `trajectory_hash` | 生成来源审计（可空） |
| ActionRubric | `phase_rule_version` | phase 规则版本（RG-02 持久化；审计字段，不入 content_hash） |
| ActionRubric | `pre_action` | `off` / `advisory` / `required`：动作前建议门强制级别 |
| ActionRubric | `fallback_reason` | 模型生成失败/非法/超预算时回退确定性生成的原因 |
| GuidanceCriterion | `check_kind` / `weight` / `required` / `expected_evidence` / `min_evidence` / `required_terms` | 原子准则；权重有限非负、至少一条必需、权重和 > 0 |

- `ActionVerdict`（动作后）与 `PreActionVerification`（动作前）均记录
  `rubric_id` / `rubric_hash` / verifier 版本 / status / 逐条准则判定 / usage；
  动作前另有 `advice`、`degraded` / `degraded_reason`、`proposal_revision_used`。
- `ActionResult.pre_action_verification` 缺省 `not_run`：旧记录缺该字段即解释为
  “未运行”，不回溯改写历史判定。
- `RubricInjectionRecord` 记录注入目标、实际 messages 哈希（≥16 hex）与可选引用；
  不允许只用 `injected=true` 声明注入生效。
- `ExecutionState.rubric_injections` 为有界注入证据列表（持久化，供恢复/审计）。
- **版本化读取**：`schema_version=1`（历史 TaskRubric 适配记录）按宽松语义只读
  兼容（权重 >10、判词 >8、全员 optional、空 ID/描述、带空白的原文均原样保留，
  不 strip、不改写身份）；`schema_version=2`（新写入）才执行严格不变量（空白
  拒绝、权重 ≤10、判词 ≤8、至少一条 required、权重和 > 0、嵌套内容冻结且
  tuple 不可变，content_hash 覆盖全部判定语义字段）。
- `source=fixed` 的准则来自受控配置（SourcePolicy/预算/完成硬门），模型输出只能
  补充研究质量标准，不能删除或降级这些硬门；`required` 的 unknown/fail 在
  required 模式下不得放行（RG-03 落实）。

### 来源固定、许可与流程时序（RG-01 核对）

- 上游参考固定为 Co-ReAct 仓库 `ZBWpro/Co-ReAct` **commit
  `ff5540a6ba69090c7b41b81c7fa7f4b5b9627a3a`**（committer date 2026-06-24），
  许可证 **Apache-2.0**（`LICENSE`）。本仓库**未复制其代码、提示词模板或 YAML**，
  仅借鉴机制思想，因此不附带上游 LICENSE/NOTICE 副本；若未来复制任何上游
  代码/模板，必须重新固定 commit 并按 Apache-2.0 保留 LICENSE/NOTICE 与声明。
- 上游流程时序在该 commit 的 `agent/workflows/co_react_eval.py` 文件头
  （第 4–9 行）与调用点（`rubric_gen.generate` 328/417 → `verifier.verify`
  366/453）核对：每步先按轨迹生成 3–5 条加权准则，注入 agent 提示词，
  再生成动作，再对该动作做事前验证，失败反馈后重试一次。上游 phase 提示按
  工具调用次数划分（`agent/dr_agent/rubric_generator.py` 第 322–334 行）。
- 本项目保留的关键差异（如实）：动作前建议门是可选项（`pre_action`
  off/advisory/required，默认 off）；phase 规则使用剩余预算/未解决问题/工具
  比例（`phase_rule_v1`），不复制上游的纯调用次数阈值；事后验收是本项目既有
  工程层，不宣称与上游逐步一致。
- 论文结果（如 prompt-only 变体在特定基准上的提升）不构成本项目收益承诺；
  本项目不做效果复现或 A/B 对比。
- 旧 rubric 记录按 `schema_version=1` 宽松只读兼容（历史上可能存在权重 >10、
  判词 >8 或全员 optional 的记录）；新写入 `schema_version=2` 才强制严格
  不变量（权重上限、必需准则、权重和 > 0）。

## 4.2 质量维度与前后判定衔接（RG-04 增量）

### 动作前 / 动作后判定

- 同一动作的 `guidance.content_hash`、`pre_action_verification.rubric_hash` 与
  记录中的动作后 `rubric_hash` **必须一致**（同一 rubric 身份，绑定 task_id
  不改 hash）；动作前 status（pass/fail/unknown/not_run）与动作后 verification
  独立保存，不互相覆盖。
- 失败尝试保留：每次 attempt 的记录携带自己的 hash 与 verdict，`active`
  只指向最新有效尝试；原批次被修订替换时，其判定与 usage 归档到
  `pre_action_history`（有界 16）。
- required 的动作前判定：fail/unknown 不得派发（整批口径，一次提案修订后
  仍不满足则整批不派发）；advisory 的 unknown 只记 degraded，不等于验证通过。

### 报告质量维度（advisory-only）

- 类型：`ReportQualityAssessment`（`harness/quality_assessment.py`），维度为
  comprehensiveness / insight / instruction_following / readability；
- **不是**官方 DRB/RACE 协议，不产生可比较官方分数；模型输出中的分数字段
  一律按 unknown 处理（不采用任何数值），协议本身没有分数字段；
- 默认关闭（`REPORT_QUALITY_ASSESSMENT_ENABLED=false`）；未启用/无模型/
  预算不足/输出非法一律 `not_run`，不造分；
- `advisory_only=true`：不替代确定性 Contract、证据引用与语义支持检查，
  不参与 `completed` 判定；
- 暴露方式：写入报告快照 `report_result.report_metrics.quality_assessment`
  （随 checkpoint 持久化），`GET /runs/{id}/report` 只读返回；失败/草稿
  运行没有报告快照，因此不会暴露诊断。

# T-017 后续：Prompt-only Rubric 行动指导与业务闭环余项

## 1. 状态与证据边界

- 状态：proposed；本次只新增文档，不实现、不提交、不推送。
- 核实基线：HEAD `87a1bc3`。T-401～T-409 的历史实施记录不回退为 pending；下列是独立后续任务。
- 依据：本次源码只读检查与既有对话中的 Co-ReAct 论文/仓库调查；本次未重跑 pytest。测试收集数量不当作测试通过结果。
- 来源：[Co-ReAct 专页](https://brenchcc.github.io/llm-compass/agent/deep-research/co-react)、[论文](https://arxiv.org/abs/2605.23590)、[官方仓库](https://github.com/ZBWpro/Co-ReAct)。复制代码/模板前固定 commit、核查 LICENSE/NOTICE 并保留必要声明。

## 2. 论文结论的准确使用

用户引用的是 Gemini 3.1 Pro 上 prompt-only 变体的特定实验，支持“无需专门训练也可以尝试 rubric 指导”的设计动机，不保证本项目收益。不能把 4.44% 当成本项目提升，也不能未核对原表就解释为百分点。

纠正先前分析：引文仅明确 Self-Refine、Best-of-N、CRITIC 未超过 ReAct，没有断言 Step-Back 未超过 ReAct；单个基准结果也不是这些方法普遍无效的证明。本项目暂不引入它们，是范围和成本取舍。

区分三件事：rubric 生成发生在动作提案之前；pre-action verifier 评价拟执行动作；post-action verifier 评价真实执行结果。最后一项是本项目保留的工程验收层，不把它宣称为已精确复现论文流程。论文与仓库具体调用顺序须在实施时对固定版本核对，不能只依据五元组名字推断。

## 3. 当前实现与缺口（87a1bc3）

| 能力 | 已实现 | 尚缺 |
|---|---|---|
| 单一 coreact / ActionRunner | 旧专属路径已退役；隔离执行、稳定合并、批次 intent/恢复已有实现 | 不重做这部分；后续改动需回归 |
| 观察输入 | Controller 已接 observations/assessments | rubric 生成函数未消费轨迹/阶段 |
| rubric | `core/rubric.py` 的 build_task_rubric 是确定性生成 | 无模型 RubricBuilder、无轨迹条件化、无 phase hint |
| 内部注入 | `action_runner.py` 写 context_snapshot.action_rubrics，已有不外发测试 | executor 未消费该键；Controller 不接完整 rubric 本体 |
| 语义验收 | CoreActVerifier 已支持三态/unknown 与 required 判定 | 默认关闭；主路径未传 semantic_required=True，不得宣称 required 语义验证已启用 |
| 最终检查 | rubric_coverage 关键词代理 | 无 RACE 风格质量维度；不能替代引用事实支持 |

## 4. 推荐设计

### 4.1 RubricBuilder：轨迹条件化，不训练

输入：ResearchBrief、当前 Run 有界 ObservationSummary、未解决问题、当前来源可用工具、预算和 phase。输出 3～5 条原子准则：criterion_id、description、weight、required、check_kind、expected_evidence。

来源显式记录 `generated | deterministic_fallback | fixed`。固定 rubric 来自受控配置，不能接受网页/模型伪造的权限规则；模型生成只补充研究质量标准，不能删除 SourcePolicy、预算和完成硬门。失败/非法输出回落现有确定性生成器，并记录 fallback 原因。

生产内部使用结构化 schema；如复用上游 `1. [0.5] criterion` 文本格式，必须经适配器校验再转结构体。限制条数、长度、有限非负权重和非空目标，不把任意字符串直接送执行层。

phase 为 `explore | deepen | synthesize`，依据剩余预算、未解决问题与已用工具次数的确定性规则产生，规则版本随 rubric 固定。不能仅因到了固定步数就认定证据充分。

### 4.2 生成、注入、提案、事前验证、执行、事后验收

```text
Brief + observations + gaps + budget
→ RubricBuilder（含 phase）
→ 将 rubric 注入 Controller 的下一动作提案上下文
→ 提案 → 确定性准入（来源/依赖/预算/去重）
→ 可选 PreActionAdvisor（独立上下文、同一 rubric）
→ ActionRunner → post-action verifier → ObservationSummary
```

Controller 和 RubricBuilder 可用同一个模型端点，但使用独立输入上下文，模型身份/配置冻结并审计。有模型参与的 worker 真正消费内部 rubric/反馈；纯检索 Provider 无 LLM，不伪称“读懂 rubric”，由 Controller 调整 query/filters 和后处理落实。严禁把内部 rubric、轨迹、密钥混入 Provider 公共参数。

### 4.3 事前建议门与硬门分开

- 来源、预算、依赖等确定性安全门始终强制；失败不能被 advisory pass 覆盖。
- PreActionAdvisor 输出 `pass | fail | unknown | not_run`，含逐 criterion 结果与建议。
- advisory fail：允许一次提案修订，冻结同一个 rubric_id/hash；这不是一次已执行工具重试。
- advisory unknown：仅当明确配置为 advisory 时可继续，并记录 degraded；不是验证通过。
- 一次修订后仍 fail 或提案未变：不反复循环，返回 Controller 的替代动作/停止决策；预算充足且策略显式允许时可降级执行，但必须保留 fail/degraded 标志。
- 若用户/任务配置某语义标准 required，fail/unknown 均不能放行该必需门或支持 completed。不得借 advisory 降级绕过 required 规则。
- post-action verifier 使用同一 rubric 检查结果，所有动作/传输/提案修订/报告修复计数分别记录，总预算共享。保留失败尝试和实际成本。

### 4.4 数据协议扩展（output-contract.md 的兼容增量）

ActionRubric 增加 rubric_id、version、content_hash、source、phase、criteria（含 weight）、generator_model、prompt_version、trajectory_ref/hash。注入记录独立保存 `injection_targets`、实际 messages hash/reference，不能仅写 injected=true 就当作注入生效证据。

ActionResult 分别关联 `pre_action_verification` 与既有 post-action `verification`，两者均记录 rubric_id/hash、verifier版本、status、criteria verdicts、usage、degraded_reason。旧记录缺 pre 字段解释为 not_run，不改写历史判定。只允许白名单元数据暴露用户，原始 prompt 和内部思考不进入最终报告。

### 4.5 报告质量维度

新增独立 quality_assessment，包含 comprehensiveness、insight、instruction_following、readability。该集合是“RACE 风格维度”，**不是运行官方 DRB/RACE 协议或可比较官方分数**。默认作为展示/开发诊断，不替代 deterministic Contract、证据引用和语义支持检查；未执行记 not_run，不造分数。

### 4.6 成本与失败

rubric 生成、事前建议、修订和事后语义检查全部计入统一预算、超时与调用计量。不能为 guidance 吃掉报告预留；缺额度跳过可选建议并记录 not_run/degraded，required 项按规则处理。实现不要求 GPU、GRPO 或上游训练权重。

## 5. 新增实施任务（全部 pending）

每阶段实现→正确性测试→独立 review→本地提交；不自动部署/推送。以下测试文件为拟新增，实际证据均未执行。

### RG-01 — 固定来源与协议适配
- 状态：done（2026-11-12）
- 依赖：none
- 触点：core/rubric.py、harness/action_models.py、output-contract.md。
- 动作：固定上游 commit/许可和流程时序；明确权重/schema、pre/post verdict、advisory/required，保证旧记录兼容。
- 验收：schema roundtrip、非法权重/空条件、旧记录 not_run、required unknown 用例；无运行外部仓库代码。
- 实际证据：上游固定为 ZBWpro/Co-ReAct `ff5540a6ba69090c7b41b81c7fa7f4b5b9627a3a`（2026-06-24，Apache-2.0，GitHub API 核实），流程时序经 co_react_eval.py 文件头/调用点与 rubric_generator.py phase 提示核对并写入 output-contract §4.1（未复制任何上游代码/模板）；ActionRubric 新增 content_hash/source/phase/generator_model/prompt_version/trajectory_ref/hash/pre_action/fallback_reason/phase_rule_version，GuidanceCriterion 结构化 schema（check_kind 别名兼容旧 kind）；v1 宽松只读兼容（空 ID/空白/高权重/多判词/全员 optional 原样保留）+ v2 严格不变量（空白拒绝/权重≤10/判词≤8/至少一条 required/权重和>0/嵌套冻结 tuple 不可变）；ActionVerdict 增 rubric_id/hash/criteria/usage/degraded_reason，ActionResult.pre_action_verification 缺省 not_run（旧记录不改写）；PreActionVerification/RubricInjectionRecord 形状冻结（messages_hash hex 16-128、注入列表≤32）；pre_action_blocks 协议助手（required+fail/unknown 阻止，advisory/off 不阻止）。`tests/harness/test_rubric_protocol.py` 27 项：roundtrip、非法权重/空条件、v1/v2 分界、旧记录 not_run、required unknown roundtrip、注入 hex/边界、不可变性。独立 review 三轮（发现：上游 provenance 缺失、v1 语义改写、frozen 不传递、空白/长度共享约束、注入格式）全部修复，终审 APPROVE。门禁：pytest 556 passed/4 skipped（含后续阶段）、ruff、git diff --check。

### RG-02 — RubricBuilder 与 phase
- 状态：done（2026-11-12）
- 依赖：RG-01
- 触点：拟新增 harness/rubric_builder.py；模型工厂、预算计量。
- 动作：有界轨迹/观察输入、phase、3～5条准则、模型生成/固定/确定性回退及来源审计。
- 验收：相同 query 不同轨迹/phase 的 scripted 输出，非法响应/超时回退，预算不足零额外调用，真实模型调用仅在获授权后。
- 实际证据：`harness/rubric_builder.py`（PHASE_RULE_VERSION=phase_rule_v1 持久化到 rubric.phase_rule_version；phase 规则 = 剩余预算比 <0.20 或工具比 ≥0.80 → synthesize，无账本证据或未解决问题 ≥2 → explore，否则 deepen——不按固定步数判定；确定性生成与 build_task_rubric 同构（evidence/source/output/claim/nodup 权重一致）+ 固定硬门 origin=fixed 合并（模型文本整体替换，不冒充 fixed）；模型路径：bind(max_tokens=generation_allocation_tokens) 可强制下发——不支持/失败 → **零调用**回退（model_no_generation_cap），响应侧超配额拒绝（generation_over_allocation），真实 deadline（ThreadPoolExecutor+future.result(timeout)）；准入 = 报告/验证预留 + CJK 感知 prompt 估算（count_tokens）+ 生成配额；失败尝试计入 usage（calls≥1、estimated、response_known 区分超时/已知响应）；claim_support 判词缺失 → 从验收条件抽取，仍无 → 回退；计数门（merge 后裁剪至 max、受控填充至 min，config 校验 min≥3/max≤5/max≥min）。`tests/harness/test_rubric_builder.py` 44 项覆盖全部验收点。独立 review 两轮（round1：超时未强制/预留未保护/失败零成本/装配越界/判词丢失/计数门/尾部截断/规则版本/混合来源/ID 碰撞；round2：配额不可强制、min5 回退抛错、已知响应成本、header 超预算、CJK 低估）全部修复，终审 APPROVE（2 项 minor 随 RG-03 落地：source_mode clip、CJK est）。门禁：ruff 通过；pytest 567 passed/4 skipped。

### RG-03 — 注入消费与事前建议门
- 状态：done（2026-11-13）
- 依赖：RG-02
- 触点：coreact_controller.py、action_runner.py、executor内部输入适配、拟新增 pre_action_advisor.py。
- 动作：Controller/模型worker实际消费 rubric；同 rubric 一次提案修订；内部通道与 Provider 参数隔离。
- 验收：捕获实际发送 messages 证明注入；pass/fail/unknown/not_run、required 与 advisory、提案未变、来源越权、费用上限；仅写 context 字段不算完成。
- 实际证据：`harness/pre_action_advisor.py`（确定性可判项 source_policy/no_duplicate_query；动作前不可判定 kind（evidence_presence/output_schema/claim_support）固定 unknown，模型增强**只能判 fail 不能判 pass**；模型调用须 bind(max_tokens=800)+真实 deadline（10s），不支持/失败 → 零调用确定性回退；失败调用如实计成本：已知响应计全量 token、传输失败按保守配额估算，bind 失败零成本）。`coreact_loop.py`：每步提案前用 RubricBuilder 构建 guidance（deterministic 默认）→ 注入 DecisionContext（controller prompt 新增"下一动作验收准则（系统内部约束）"段）→ 控制器真实调用后记录 `RubricInjectionRecord`（sha256(实际 prompt)，仅成功调用才记录）；动作级 bind（content_hash 不变）；**整批口径**动作前门——无问题保留（advisory unknown 记 degraded）、有问题先释放整批 tool 预约再做**一次**提案修订（同 rubric_id/hash；有实质变化且通过 detached 确定性准入 → 整批替换为单一动作并复检；未变/被拒/不存在 → 整批不派发、交控制器下一轮），原批次判定与成本归档到 `pre_action_history`（≤16）；每个动作前重算 LLM 许可并扣减/记账共享 `llm_tokens`；预约精确对账（各路径 reserved 归零）；pending intent 持久化 guidance+pre_verification（含修正重试），恢复重放还原并产出 worker 注入证据；worker 经隔离副本 `action_guidance` 消费（executor 记录 guidance 原样+匹配证据），Provider 公共参数不变。`tests/harness/test_rubric_guidance_injection.py` 24 项：prompt 实际含准则且注入哈希==sha256(captured prompt)、worker 消费证据与哈希一致、Provider 参数白名单、off/advisory unknown degraded/required 阻断/一次修订/修订被拒/提案未变/多问题 K=2/预算预约/共享预算/崩溃恢复/bind 失败零成本等。独立 review 四轮（round1 2 major+10 minor；round2 4 major；round3 5 项；round4 2 项计费）全部修复，终审 APPROVE。门禁：ruff、pytest 583 passed/4 skipped、git diff --check。

### RG-04 — 事后衔接与报告质量维度
- 状态：done（2026-11-13）
- 依赖：RG-03
- 触点：coreact_verifier.py、ReportService、结果DTO/前端、output-contract.md。
- 动作：同 rubric 前后关联、分别审计；RACE 风格维度可选输出，明确非官方分数；required unknown 不可通过。
- 验收：前后 hash 相同、失败历史保留、预算耗尽/畸形判分、草稿与最终结果隔离；运行完整 pytest 与前端构建（实际命令先核对 package.json）。
- 实际证据：`_attach_coreact_verdict` 增加 `rubric_hash`（动作后 verdict 与动作前/指导同一 content_hash，绑定 task_id 不变）；`harness/quality_assessment.py`（四维度 comprehensiveness/insight/instruction_following/readability；RACE 风格但**非官方协议/无分数字段**——响应级与维度级的 score/rating/points 字段一律按 unknown 处理；默认 not_run；准入 = VERIFICATION 预留 + CJK prompt 估算 + 800 生成配额；bind(max_tokens)+真实 deadline，bind 失败=未派发零成本；失败尝试成本如实：已知响应计全量 prompt+response、传输失败按保守配额估算、未派发零调用；聚合 any-fail→fail / all-pass→pass / 已执行含 unknown→unknown（不伪装 not_run）；归一化全程 try/except 容器化——畸形 status 类型（[]/{}/3/None）不崩溃不外溢）；report_pipeline 在报告生成后写 `report_result.report_metrics.quality_assessment`（advisory_only，不参与 Contract/completed；消耗计入共享 llm_tokens）；`GET /runs/{id}/report` 仅 completed 运行从 checkpoint 只读暴露白名单视图（status/dimensions/judge_model/prompt_version/note/advisory_only/usage.calls；异常文本不出网；嵌套损坏形状一律 None）；前端 types + 报告页“质量诊断（非官方）”展示块（not_run 不显示）。`tests/harness/test_report_quality.py` 21 项：维度协议无分数/未执行 not_run/准入预留保护（CJK 长报告）/合法与畸形（类型级/响应级/维度级 score）/失败成本三口径（未派发零、传输保守、已知全量）/管线开关与共享预算记账/前后 hash 相同/失败尝试历史保留/DTO completed-only 与草稿隔离/损坏 checkpoint 容错/密钥不外泄。独立 review 一轮（3 HIGH + 4 MEDIUM）+ 复审 1 项（失败调用成本）全部修复。门禁：pytest 605 passed/4 skipped、ruff、git diff --check、`npm run build`。

## 6. 之前业务计划的未完成项（独立排期，非本次实现）

| ID | 状态 | 当前缺口/触点 | 验收目标 |
|---|---|---|---|
| BC-01 | done（2026-11-13） | clarifications 写 config_snapshot 后没有消费端；brief_builder/context builder/ClarificationCard | 实际问题展示→答案进会话与规划→继续研究，不重复澄清；幂等提交 |
| BC-02 | done（2026-11-13） | run_service.pause 在队列API本地无 task 时直接 finalize_pause | API仅请求、远端worker确认；分离实例测试；取消优先且中断可恢复 |
| BC-03 | pending | event_stream.list_after 无全量翻页；提交去重翻页不等于 SSE 翻页 | terminal >1100事件完整重放，游标重连，无fanout数据库心跳补齐 |
| BC-04 | pending | artifact_edit 章节保留已做，旧引用继承/映射未做 | 显式修订目标、旧证据合法归入新Run、citation mapping/provenance、Contract通过、越权/hash变更拒绝 |
| BC-05 | pending | 历史/活动Run UI分离、来源可用性、错误/下载与会话分页交付链需专项验收 | 再盘点当前UI/API，未验证不宣称缺失全部已确诊；真实用户链路浏览器验收 |

BC-01 实际证据（2026-11-13）：`harness/clarifications.py` 冻结请求/回答协议
（问题≤5/条≤500 字、回答≤10000 字、历史≤16；revision 在已回答后 +1；旧
`clarification_question` 只读回退）；ContextBuilder 写入结构化请求并把回答并入
`resolved_query` + 受保护上下文块，已有回答时不再抛 NeedsUserInput；
BriefBuilder 将问答对并入 `PlanContext.clarification_history` 且已有回答时不再
阻塞；driver 把 Run 行回答补合并进恢复快照；Runtime 在 needs_user_input 前把
请求落库；`POST /runs/{id}/clarifications` 原子写入 user message + Run 快照，
支持 `client_clarification_id` 幂等（同键同内容 duplicate、同键异内容 409）、
`question_id` 版本校验；Run DTO 暴露白名单 `clarification.questions`，前端
ClarificationCard 展示实际问题。`tests/harness/test_clarifications.py` 8 项 +
API/context/runtime 用例；独立 review 1 轮（APPROVE，3 项 LOW：无唯一约束的并发
重复、落库失败日志、测试覆盖）已补测试与日志。门禁：pytest 613 passed/4 skipped。

BC-01～05 依赖已完成的 T-017 执行底座，互相可独立修复；BC-04 必须先冻结修订输出协议，BC-05 在相关后端行为确定后收尾。不存在授权清库、取消历史任务或访问付费服务的隐含许可。

BC-02 实际证据（2026-11-13）：`RunService.pause` 在队列模式（`queue is not None`）
只登记 `pause_requested` 并发布 `run.pause_requested`，不再本地 `finalize_pause`；
无租约时投递一次唤醒信号，已有租约时由运行中的 worker 在下一边界自行观察。确认
路径在 worker 的 `_pause_at_safe_boundary`/`_resolve_coreact_pause`：先把
`pause_resume_status` 写入 Run 行，再落 paused（崩溃窗口内最多是“仍在运行但已记录
安全阶段”），进程重启时 `_load_context` 以 Run 行的目标覆盖旧 checkpoint。paused
只允许显式 resume 恢复（`resume_requested` 一次性授权，重复唤醒信号直接返回），
避免队列重复投递把暂停 Run 自动跑完。`RunService.cancel` 在队列模式使用单条条件
UPDATE（`lease_owner IS NULL AND status NOT IN terminal`）原子落 cancelled；远端
仍持有租约时只登记请求，由 worker 确认。`release_lease_and_cancel_paused` 用两条
条件 UPDATE 在同一事务内先做 owner-fenced 释放、再对 paused+cancelled 请求落终态，
关闭“取消在检查后、释放租约前到达”的窗口；旧 worker 不会清掉接管者的租约。
`_pause_at_safe_boundary` 先检查 `cancellation_requested`，取消优先于暂停。
`tests/harness/test_run_queue.py` 新增 10 项（分离实例 pause、重复唤醒不自动续跑、
远端持租约不确认、cancel 覆盖 pause、queued/paused 本地取消、边界释放仲裁、
租约持有拒绝取消、接管者租约不被清、过期租约→interrupted→重新确认 pause 等），
`test_lease_heartbeat.py` 增加“旧 checkpoint + paused 行目标覆盖”1 项。独立 review
3 轮（round1 4 HIGH：暂停后取消搁置/取消与租约获取竞态/重复唤醒自动续跑/暂停确认
崩溃窗口；round2 3 HIGH：释放读旧快照、无条件清除接管者租约、SQLite 无行锁；
全部改为条件 UPDATE 后 APPROVE）。门禁：pytest 628 passed/4 skipped。

## 7. 验收矩阵与范围声明（实施后）

实施提交链：`d7f278f`(RG-01) → `aff885a`(RG-02) → `33f1aee`(RG-03) → `31db944`+`ee1fef6`(RG-04)。
每阶段 实现→正确性测试→独立 review→修复/复审→本地提交。

| 项 | 任务 | 当前结果 |
|---|---|---|
| 轨迹条件化、phase感知生成与回退 | RG-02 | PASS（test_rubric_builder.py 44 项；phase_rule_v1 持久化；非法/超时/无配额零调用回退；预留+CJK 估算+生成配额门；独立 review 3 轮 APPROVE） |
| 实际 messages 含 rubric，Provider 无内部字段 | RG-03 | PASS（test_rubric_guidance_injection.py 24 项；注入哈希==sha256(captured prompt)；worker 消费证据与记录 guidance 哈希一致；Provider 参数白名单断言；4 轮 review APPROVE） |
| 同 rubric 一次提案修订，advisory unknown 显式降级 | RG-03 | PASS（整批口径一次修订；detached 确定性准入；degraded/advisory_unknown_continued；预约精确对账；原判定与成本归档 pre_action_history） |
| required unknown 不放行；前后 verdict 不混同 | RG-01/03/04 | PASS（pre_action_blocks 协议层 + 门内阻断派发；record 元数据 pre/post 独立保存；前后 rubric_hash 相同断言 test_report_quality.py；失败尝试历史保留） |
| RACE 风格维度与官方评测分数不混淆 | RG-04 | PASS（协议无分数字段；响应级/维度级 score 字段按 unknown；默认 not_run 零调用；仅 completed 暴露；白名单 DTO 不透出异常文本；advisory_only 不参与 Contract/completed） |
| 澄清/控制/SSE/修订/历史交付闭环 | BC-01～05 | 未执行（独立排期，不属本轮） |

范围声明（如实）：本轮未做效果对比实验/A/B/基准分数；未验证真实 LLM/Provider 服务
端到端（测试以 scripted 模型 + 真实执行器栈为主）；上游 Co-ReAct 代码/模板未复制
（仅机制思想参考，commit/许可已固定记录于 output-contract §4.1）；模型参与的
guidance/建议/诊断调用全部默认关闭，开启需显式配置且受预留预算与生成配额约束。

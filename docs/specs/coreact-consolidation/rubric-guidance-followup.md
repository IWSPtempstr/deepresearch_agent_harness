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
- 状态：pending
- 依赖：RG-01
- 触点：拟新增 harness/rubric_builder.py；模型工厂、预算计量。
- 动作：有界轨迹/观察输入、phase、3～5条准则、模型生成/固定/确定性回退及来源审计。
- 验收：相同 query 不同轨迹/phase 的 scripted 输出，非法响应/超时回退，预算不足零额外调用，真实模型调用仅在获授权后。

### RG-03 — 注入消费与事前建议门
- 状态：pending
- 依赖：RG-02
- 触点：coreact_controller.py、action_runner.py、executor内部输入适配、拟新增 pre_action_advisor.py。
- 动作：Controller/模型worker实际消费 rubric；同 rubric 一次提案修订；内部通道与 Provider 参数隔离。
- 验收：捕获实际发送 messages 证明注入；pass/fail/unknown/not_run、required 与 advisory、提案未变、来源越权、费用上限；仅写 context 字段不算完成。

### RG-04 — 事后衔接与报告质量维度
- 状态：pending
- 依赖：RG-03
- 触点：coreact_verifier.py、ReportService、结果DTO/前端、output-contract.md。
- 动作：同 rubric 前后关联、分别审计；RACE 风格维度可选输出，明确非官方分数；required unknown 不可通过。
- 验收：前后 hash 相同、失败历史保留、预算耗尽/畸形判分、草稿与最终结果隔离；运行完整 pytest 与前端构建（实际命令先核对 package.json）。

## 6. 之前业务计划的未完成项（独立排期，非本次实现）

| ID | 状态 | 当前缺口/触点 | 验收目标 |
|---|---|---|---|
| BC-01 | pending | clarifications 写 config_snapshot 后没有消费端；brief_builder/context builder/ClarificationCard | 实际问题展示→答案进会话与规划→继续研究，不重复澄清；幂等提交 |
| BC-02 | pending | run_service.pause 在队列API本地无 task 时直接 finalize_pause | API仅请求、远端worker确认；分离实例测试；取消优先且中断可恢复 |
| BC-03 | pending | event_stream.list_after 无全量翻页；提交去重翻页不等于 SSE 翻页 | terminal >1100事件完整重放，游标重连，无fanout数据库心跳补齐 |
| BC-04 | pending | artifact_edit 章节保留已做，旧引用继承/映射未做 | 显式修订目标、旧证据合法归入新Run、citation mapping/provenance、Contract通过、越权/hash变更拒绝 |
| BC-05 | pending | 历史/活动Run UI分离、来源可用性、错误/下载与会话分页交付链需专项验收 | 再盘点当前UI/API，未验证不宣称缺失全部已确诊；真实用户链路浏览器验收 |

BC-01～05 依赖已完成的 T-017 执行底座，互相可独立修复；BC-04 必须先冻结修订输出协议，BC-05 在相关后端行为确定后收尾。不存在授权清库、取消历史任务或访问付费服务的隐含许可。

## 7. 验收矩阵与范围声明

| 项 | 任务 | 当前结果 |
|---|---|---|
| 轨迹条件化、phase感知生成与回退 | RG-02 | 未执行 |
| 实际 messages 含 rubric，Provider 无内部字段 | RG-03 | 未执行 |
| 同 rubric 一次提案修订，advisory unknown 显式降级 | RG-03 | 未执行 |
| required unknown 不放行；前后 verdict 不混同 | RG-01/03/04 | 未执行 |
| RACE 风格维度与官方评测分数不混淆 | RG-04 | 未执行 |
| 澄清/控制/SSE/修订/历史交付闭环 | BC-01～05 | 未执行 |

本次文档校验仅覆盖：新增任务唯一性、全部 pending、内部链接、前后判定字段和 hard/advisory 语义一致。代码测试与效果评测没有运行，不使用历史492项收集或其它测试数字冒充本次通过证据。

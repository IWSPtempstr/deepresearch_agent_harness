# 运行流程详解（RUN-FLOWS）

> 本文是运行流程的完整文字版（T-017 起唯一 coreact 工作流 + 平台机制流程）。描述与仓库实现一一对应；T-017 验收证据见 `docs/specs/coreact-consolidation/acceptance.md`。
> 前缀：F1 为 Harness 唯一生产工作流（coreact）；F2/F3/F4 为平台级机制流程。

---

## F1 · plan_execute_report 工作流（唯一生产入口，T-017）

coreact 步级验证控制循环：BriefBuilder → Controller → ActionRunner → ObservationSummary/Verification → Controller → Reporter → Completion Contract。独立 deep_research 引擎与 staged 整批引擎已于 T-017（T-407/T-408）退役并物理删除。

- **触发**：用户消息（HTTP `workflow_mode` 在边界归一化，CLI 默认同规则）；`orchestration=coreact_v1` 唯一写入值。
- **持久化**：ResearchBrief/候选计划/任务、每动作 ExecutionRecord、ToolCall、Evidence、rubric 与 verdict（写入 record 元数据）、观察摘要与 claim 评估入库；**逐动作持久提交**（幂等 upsert + EXECUTING checkpoint 携带全批次 in-flight 游标与已提交集合），恢复粒度为"单个已提交动作之后"。
- **成功出口**：9 项检查（含 rubric_coverage——报告已引用的账本证据须覆盖步级 rubric 的 required 判词，确定性代理）全部通过 → completed（Contract 唯一判定，模型不得自行宣称）。

流程：

1. **澄清 Clarify（可选）** — 复用 Clarifier；歧义不可消解且未允许未澄清时进 needs_user_input；简单请求走单任务捷径（不凑长文）。
2. **简报规划 Plan** — BriefBuilder 生成 ResearchBrief 契约（目标/问题/验收/冻结来源/预算口径，replan 时显式版本化）与**候选任务图**（全部就绪，仅作 Controller 建议池，不预执行整图）；final evidence 缺口（min_evidence 等）转为补充检索候选反馈 Controller，不回旧 Planner。
3. **步级循环 Execute** — 每批（批宽 `COREACT_MAX_PARALLEL`，默认 1=串行，受 Run 预算 max_concurrency 约束；K=2 重叠由测试 monkeypatch 证明）：Controller 消费有界观察摘要（带 evidence_id 的研究发现）与 claim 级评估选择下一动作/批次 → ActionRunner 统一承接（预派发预算预约、来源/去重门、**每动作深拷贝隔离**、sequence 顺序稳定合并）→ CoreActVerifier 确定性验证（账本证据计数/工具白名单/输出存在/判词覆盖/查询去重）+ 可选语义判定（pass/fail/unknown + 预算门；required 的 unknown 不得按 pass）→ 未通过则**每动作至多一次定向修正**（全部 attempt 的记录与成本保留）→ 逐动作提交。
4. **报告 Report** — 共用 Reporter 管线（大纲/分节/卡片/内联引用/一致性检查）；语义核验存在 unknown/fail 的动作在"局限（语义核验）"中显式标注（安全降级）。
5. **Completion Contract** — 9 项确定性检查；`rubric_coverage` 失败路由 repair（确定性重建，max_task_retries 上限，无死循环）。

分支与恢复：

- **观察驱动**（核心差异）：动作 N+1 的选择依赖动作 N 的验证结果与证据缺口——验证失败的 rubric 反馈与 claim 评估进入下一轮决策上下文。
- **有界性**（danger）：连续 2 次无证据进展 / 重复查询 2 次 / 决策总数超上限 / 预算触及报告预留 → 停止探索进报告；controller 决策畸形 2 次或无 LLM → 确定性兜底。
- **崩溃恢复**（warn）：EXECUTING checkpoint 携带未完成循环游标 → 重放未提交的 in-flight 批次与修正游标（不丢次要动作、不重做已提交动作）；legacy 快照无游标 → 原 REPORTING 跳转。
- **暂停/取消**（warn）：动作边界探测 pause；DB cancel 优先于 pause（仲裁不丢取消）。
- **已知限制**（danger，如实）：外部工具调用与预算计数非 exactly-once（提交窗口内崩溃重做 ≤1 动作；计费可低估不双收）；租约 fencing 为 check-then-write（TOCTOU 窗口保留）；关键词检查仅为代理，不升级为事实认证；语义判定默认关闭（COREACT_SEMANTIC_ENABLED 开启后为可选模式）。

### 退役引擎（历史兼容，T-017）

- **deep_research**：引擎已物理删除。新请求在边界归一化为 plan_execute_report；存量**活动** deep_research Run 由工厂显式拒绝（`LEGACY_WORKFLOW_UNSUPPORTED`，人工处理清单）且不进入自动恢复；**历史已完成 Run 只读可读**（workflow_mode/报告/证据不改写）。
- **staged_v1**：整批管线已物理删除。存量 staged checkpoint 恢复→执行时显式拒绝（同错误码）；`scripts/convert_legacy_checkpoints.py` 默认 dry-run 出具映射清单（可映射 coreact 游标 --apply 追加归一化行，原 checkpoint 不破坏）。

代码入口：`harness/drivers_plan_execute.py`（PlanExecuteReportDriver）、`harness/coreact_{controller,verifier,state,support}.py`、`harness/action_runner.py`（统一隔离执行）、`harness/research_brief.py` + `harness/action_models.py`（冻结契约）、`agents/multi_agent/planner/brief_builder.py`、`agents/multi_agent/core/rubric.py`、`executor/worker_coordinator.py`（execute_action）、`harness/runtime.py`（continuation 恢复）、`harness/contracts.py`（rubric_coverage）

---

## F3 · 平台消息与执行生命周期

从一条用户消息到可恢复的 Run：API → 队列 → worker → SSE。

- **触发**：用户向会话发送消息（POST /sessions/{id}/messages）。
- **持久化**：runs 表是唯一事实源；run_events 先落库再扇出；唤醒信号允许丢失（at-most-once），由租约扫描兜底重建。
- **成功出口**：Run 到达终态；任意 API 实例都能凭 event_id 游标向客户端完整重放事件流。

流程：

1. **幂等与预算门** — ChatService 按 client_message_id 幂等去重；日预算耗尽时直接拒绝（LLM_RATE_LIMITED，429 语义）。
2. **原子创建 Run** — 用户消息 + Run 行 + run.queued 事件在同一事务写入；config_snapshot 记录 min_evidence、难度模型 tier 等。
3. **投递唤醒信号** — schedule() 只向 Redis Streams（或可选 Kafka）发一条 run_id 信号；入队失败计数并可观测，Run 保持 queued。*（Kafka 为可选传输层：至少一次 + 执行后提交 offset。）*
4. **worker 领取执行** — worker 消费信号后以条件 UPDATE 抢租约（唯一互斥量），从 Checkpoint 恢复或从 queued 开始驱动状态机。
5. **事件 durable-first** — 事件先写 run_events，再经 Redis 扇出；扇出失败仅计数，不影响状态机。*（Kafka-only 模式下无实时扇出，SSE 依赖数据库重放 + 心跳。）*
6. **SSE 断线重放** — GET /runs/{id}/events 先按 Last-Event-ID 游标从数据库重放，再接实时流；心跳兜底防止丢终态。

分支与恢复：

- **信号丢失 → 扫描重建**（warn）：租约过期扫描（默认 15s 一轮）把 queued/interrupted 的 Run 重新投递，不依赖队列重投。
- **队列断连 → 自愈重连**（warn）：worker 消费流异常不退出：指数退避（1s→30s）重建生成器，重复投递由租约互斥去重。

代码入口：`backend/app/services/chat_service.py`、`backend/app/services/run_service.py`、`src/deepresearch_agent/queueing.py`、`backend/app/services/event_stream.py`、`src/deepresearch_agent/worker.py`

---

## F4 · Evidence 到报告验收

证据的一生：入账 → 压缩卡 → 章节路由 → 覆盖门禁。

- **触发**：执行阶段每次检索/工具调用返回结果时入账；报告阶段触发卡片与路由。
- **持久化**：Evidence 行含 run/task/tool/provider/source_id/content_hash 与 1200 字清洗摘要；EvidenceCard 持久化于 workflow_state；报告引用以 `[ev_*]` 写入最终 Artifact。
- **成功出口**：evidence_card_coverage 通过——账本 ID（由不可变 evidence 仓库读入）与卡片/路由/正文/附录四个集合比对，路由、正文与附录不得漏掉任何卡片；账本→卡片的构建完整性由 Report 阶段保证。

流程：

1. **确定性入账** — evidence_id = `ev_ + sha256(run:mode:source:content_hash)[:24]`；重放/重投生成相同 ID，upsert 幂等。
2. **构建 Evidence Card** — 每条证据压成 ≤200 token 的卡片，卡片 ID 回指账本 evidence_id。
3. **按章节路由** — 每张卡恰好分配一个 primary_section；确定性路由覆盖 LLM 的分配，保证集合可核对。
4. **分节写作与压缩** — 章节输入超预算时按批 Map→Digest→递归 Reduce；正文写作只消费路由到本章的卡片。
5. **全量附录** — 未被正文引用的证据进入报告附录索引。
6. **覆盖门禁** — 验证时从不可变 evidence 仓库读取真实 evidence_id 集合（不接受 reporter 自报），比对卡片/路由/正文/附录四个阶段，任一阶段遗漏即失败。

分支与恢复：

- **覆盖失败 → 修稿**（warn）：修复后重新验证，预算耗尽则 FAILED。
- **压缩不丢原文**（success）：卡片压缩只改变模型看到的载荷；原始证据完整保留在账本。

代码入口：`src/deepresearch_agent/harness/evidence.py`、`agents/multi_agent/reporter/evidence_cards.py`、`src/deepresearch_agent/harness/contracts.py`

---

## F5 · Worker 故障恢复与 Drain

崩溃接管、断点续跑与优雅关停的完整闭环。

- **触发**：worker 进程被 SIGKILL / 容器重启 / 租约过期，或收到 SIGTERM 关停信号。
- **持久化**：阶段末 Checkpoint（版本+sha256 校验）；租约 TTL 默认 30s、心跳 TTL/3 续约、扫描间隔 15s。
- **成功出口**：被接管的 Run 从最近 Checkpoint 恢复并推进到终态；SIGTERM 时 worker 最多等待 30s 让在跑 Run 收尾，超时不取消也不写终态（等价崩溃，由接管者续跑）。

流程：

1. **租约与心跳** — 执行期间每 TTL/3 续约；续约失败说明租约被抢，当前实例抛 RunLeaseLost 并静默让位（不写任何状态）。
2. **阶段末 Checkpoint** — 每个阶段结束保存 RunContext 快照；恢复时按 `_safe_stage_after` 定位第一个未提交操作。
3. **故障检测** — SIGKILL/崩溃后租约到期；扫描循环把活跃但租约过期的 Run 标记为 interrupted 并重新投递。
4. **接管续跑** — 新 worker 抢到租约后从 Checkpoint 恢复驱动状态；已提交的阶段不重做。
5. **幂等重放** — 事件/工具调用/证据三处去重。
6. **SIGTERM 优雅关停** — 第一次 SIGTERM：停止拉取新任务，已交付但尚未启动的信号不再启动新 Run（Kafka 未 ack 会重投，Redis 已 ACK 则由租约扫描兜底），并在 30s 宽限内等待在跑 Run 收尾。

分支与恢复：

- **宽限到期 → 等价崩溃**（danger）：超过 30s 宽限仍未完成的 Run 不取消、不写终态，进程直接退出，由租约过期扫描与接管者续跑。
- **第二次 SIGTERM → 快速退出**（danger）：升级为立即退出，避免关停被单任务卡死。
- **重复接管 → 租约互斥**（warn）：条件 UPDATE 只允许一个 worker 获得租约；失败方静默让位。
- **执行中断连 → 消费自愈**（warn）：队列连接异常时指数退避重建消费流，worker 进程不退出。

代码入口：`src/deepresearch_agent/harness/runtime.py`、`harness/recovery.py`、`src/deepresearch_agent/worker.py`、`tests/harness/test_takeover.py`

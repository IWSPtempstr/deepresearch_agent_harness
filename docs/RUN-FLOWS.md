# 运行流程详解（RUN-FLOWS）

> 本文是静态展示页中五条运行流程的完整文字版。描述与仓库实现一一对应，证据见 `docs/acceptance/acceptance-matrix.md`。
> 前缀：F1/F2 为 Harness 实际支持的两种工作流；F3/F4/F5 为平台级机制流程。

---

## F1 · deep_research 工作流

单任务深研究：多轮"思考—检索—推理"闭环。

- **触发**：用户消息选择 `workflow_mode=deep_research`（默认）。
- **持久化**：Plan/执行记录/证据/报告全部入库；阶段边界写 Checkpoint；事件先落库再扇出。
- **成功出口**：Completion Contract 通过 → 状态机以 `contract_passed=True` 进入 completed。

流程：

1. **合成研究任务** — DeepResearchDriver 将用户问题打包为单个 deep_research 任务，生成 PlanSpec 并落库。
2. **多轮思考与检索** — 研究工具按 thinking_stream 逐轮"思考→搜索→推理"，进度回调实时发 `agent.progress` / `iteration.completed` 事件。
3. **收集证据** — 每轮检索结果写入不可变 Evidence Ledger，稳定 evidence_id 由 run+mode+source+content 哈希确定。
4. **报告生成** — 答案经 sanitize 剥离思考与内部材料，按证据权威层级附加 `[ev_*]` 内联引用；来源不足 2 个时追加"局限"章节。
5. **Completion Contract** — 7 项确定性检查（此工作流无 evidence_card_coverage）：引用回账本、逐主张引用、来源匹配等。

分支与恢复：

- **证据不足 → 一次 replan**（warn）：min_evidence 未达标且未用过重规划：清空答案与结果后回到 planning 重新研究。
- **证据仍不足 → 诚实失败**（danger）：FAILED + NO_SOURCE_EVIDENCE/INSUFFICIENT_SOURCE_EVIDENCE，不硬编报告。
- **文风缺陷 → 定向修稿**（warn）：纯引用/章节/一致性失败：用证据点重写报告后重新验证。

代码入口：`src/deepresearch_agent/harness/workflow.py`（DeepResearchDriver）、`src/deepresearch_agent/agents/deep_research_agent.py`、`src/deepresearch_agent/harness/verifiers/deterministic.py`

---

## F2 · plan_execute_report 工作流

多智能体 DAG：Clarifier/Planner → Worker → Reflector → Reporter。

- **触发**：用户消息选择 `workflow_mode=plan_execute_report`。
- **持久化**：PlanSpec/TaskGraph、每任务 ExecutionRecord、ToolCall、Evidence、分节草稿与 Evidence Card 全部入库；每次计划修订 plan_version+1。
- **成功出口**：8 项检查（含 evidence_card_coverage）全部通过 → completed。

流程：

1. **澄清 Clarify** — Clarifier 识别歧义；不可消解时抛 NeedsUserInput，Run 进入 needs_user_input 等待用户补充。
2. **规划 Plan** — TaskDecomposer 拆解为任务 DAG（local/global/hybrid/deep 等类型），PlanReviewer 审校顺序与参数。
3. **并行执行 Execute** — WorkerCoordinator 按依赖调度：每个任务在深拷贝状态上隔离执行，协调者按计划顺序合并；事件实时发布 task.started / tool.completed。
4. **反思 Reflect** — Reflector 对目标任务做缺口分析与答案验证；needs_retry 时定向重试目标任务（受次数上限约束）。
5. **报告 Report** — OutlineBuilder 生成章节大纲；每节 SectionWriter 基于路由到该章的 Evidence Cards 写作并加内联引用；组装后做一致性检查。
6. **Completion Contract** — 8 项确定性检查，其中 evidence_card_coverage 使用不可变账本的真实 ID，对比卡片集合及其路由/正文/附录覆盖；注意：账本→卡片构建完整性由 Report 阶段保证，verifier 本身不重新核算 card 缺失集。

分支与恢复：

- **Token 告急 → 预算兜底报告**（warn）：剩余 Token 低于报告+验证预留时：零模型调用，用已路由卡片确定性生成证据报告（reserved_budget_fallback）。
- **单任务摘要 → 紧凑报告**（warn）：单节点计划且查询为摘要型：走紧凑确定性报告路径。
- **证据缺口 → replan**（danger）：min_evidence/claim_support/source_match 失败：清空先前结果回 planning，受 max_replans=2 约束。
- **卡片覆盖失败 → 修稿**（warn）：修复报告后用 LLM 复查一致性再回验证。

代码入口：`src/deepresearch_agent/agents/multi_agent/orchestrator.py`、`executor/worker_coordinator.py`、`reporter/base_reporter.py`、`harness/workflow.py`（PlanExecuteReportDriver）

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

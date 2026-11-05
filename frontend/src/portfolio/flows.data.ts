export const REPO_BASE = "https://github.com/IWSPtempstr/deepresearch_agent_harness";

export type FlowStep = {
  title: string;
  detail: string;
  optionalNote?: string;
};

export type FlowBranch = {
  label: string;
  detail: string;
  tone: "success" | "warn" | "danger";
};

export type RunFlow = {
  id: string;
  order: string;
  name: string;
  tagline: string;
  trigger: string;
  persistence: string;
  successExit: string;
  steps: FlowStep[];
  branches: FlowBranch[];
  codeRefs: Array<{ label: string; path: string }>;
  optionalNote?: string;
};

export const RUN_FLOWS: RunFlow[] = [
  {
    id: "flow-deep-research",
    order: "F1",
    name: "deep_research 工作流",
    tagline: "单任务深研究：多轮“思考—检索—推理”闭环",
    trigger: "用户消息选择 workflow_mode=deep_research（默认）。",
    persistence:
      "Plan/执行记录/证据/报告全部入库；阶段边界写 Checkpoint；run.started、tool.completed、evidence.added、verification.completed 等事件先落库再扇出。",
    successExit:
      "Completion Contract 通过 → 状态机以 contract_passed=True 进入 completed，报告与证据引用持久化。",
    steps: [
      { title: "合成研究任务", detail: "DeepResearchDriver 将用户问题打包为单个 deep_research 任务，生成 PlanSpec 并落库。" },
      { title: "多轮思考与检索", detail: "研究工具按 thinking_stream 逐轮“思考→搜索→推理”，进度回调实时发 agent.progress / iteration.completed 事件。" },
      { title: "收集证据", detail: "每轮检索结果写入不可变 Evidence Ledger，稳定 evidence_id 由 run+mode+source+content 哈希确定。" },
      { title: "报告生成", detail: "答案经 sanitize 剥离思考与内部材料，按证据权威层级附加 [ev_*] 内联引用；来源不足 2 个时追加“局限”章节。" },
      { title: "Completion Contract", detail: "7 项确定性检查（此工作流无 evidence_card_coverage）：引用回账本、逐主张引用、来源匹配等。" },
    ],
    branches: [
      { label: "证据不足 → 一次 replan", detail: "min_evidence 未达标且未用过重规划：清空答案与结果后回到 planning 重新研究。", tone: "warn" },
      { label: "证据仍不足 → 诚实失败", detail: "重规划后仍不足：FAILED + NO_SOURCE_EVIDENCE/INSUFFICIENT_SOURCE_EVIDENCE，不硬编报告。", tone: "danger" },
      { label: "文风缺陷 → 定向修稿", detail: "纯引用/章节/一致性失败：用证据点重写报告（evidence-only repair）后重新验证。", tone: "warn" },
    ],
    codeRefs: [
      { label: "harness/workflow.py · DeepResearchDriver", path: "src/deepresearch_agent/harness/workflow.py" },
      { label: "deep_research_agent.py", path: "src/deepresearch_agent/agents/deep_research_agent.py" },
      { label: "harness/verifiers", path: "src/deepresearch_agent/harness/verifiers/deterministic.py" },
    ],
  },
  {
    id: "flow-plan-execute-report",
    order: "F2",
    name: "plan_execute_report 工作流",
    tagline: "多智能体 DAG：Clarifier/Planner → Worker → Reflector → Reporter",
    trigger: "用户消息选择 workflow_mode=plan_execute_report。",
    persistence:
      "PlanSpec/TaskGraph、每任务 ExecutionRecord、ToolCall、Evidence、分节草稿与 Evidence Card 全部入库；每次计划修订 plan_version+1。",
    successExit:
      "8 项检查（含 evidence_card_coverage）全部通过 → completed；报告由分节草稿组装，附录保留全量证据索引。",
    steps: [
      { title: "澄清 Clarify", detail: "Clarifier 识别歧义；不可消解时抛 NeedsUserInput，Run 进入 needs_user_input 等待用户补充。" },
      { title: "规划 Plan", detail: "TaskDecomposer 拆解为任务 DAG（local/global/hybrid/deep 等类型），PlanReviewer 审校顺序与参数。" },
      { title: "并行执行 Execute", detail: "WorkerCoordinator 按依赖调度：每个任务在深拷贝状态上隔离执行，协调者按计划顺序合并；事件实时发布 task.started / tool.completed。" },
      { title: "反思 Reflect", detail: "Reflector 对目标任务做缺口分析与答案验证；needs_retry 时定向重试目标任务（受次数上限约束）。" },
      { title: "报告 Report", detail: "OutlineBuilder 生成章节大纲；每节 SectionWriter 基于路由到该章的 Evidence Cards 写作并加内联引用；组装后做一致性检查。" },
      { title: "Completion Contract", detail: "8 项确定性检查，其中 evidence_card_coverage 使用不可变账本的真实 ID，对比卡片集合及其路由/正文/附录覆盖；注意：账本→卡片构建完整性由 Report 阶段保证，verifier 本身不重新核算 card 缺失集。" },
    ],
    branches: [
      { label: "Token 告急 → 预算兜底报告", detail: "剩余 Token 低于报告+验证预留时：零模型调用，用已路由卡片确定性生成证据报告（reserved_budget_fallback）。", tone: "warn" },
      { label: "单任务摘要 → 紧凑报告", detail: "单节点计划且查询为摘要型：走紧凑确定性报告路径，节省模型调用。", tone: "warn" },
      { label: "证据缺口 → replan", detail: "min_evidence/claim_support/source_match 失败：清空先前结果回 planning，受 max_replans=2 约束。", tone: "danger" },
      { label: "卡片覆盖失败 → 修稿", detail: "evidence_card_coverage 或引用/章节失败：修复报告后用 LLM 复查一致性再回验证。", tone: "warn" },
    ],
    codeRefs: [
      { label: "multi_agent/orchestrator.py", path: "src/deepresearch_agent/agents/multi_agent/orchestrator.py" },
      { label: "worker_coordinator.py", path: "src/deepresearch_agent/agents/multi_agent/executor/worker_coordinator.py" },
      { label: "reporter/base_reporter.py", path: "src/deepresearch_agent/agents/multi_agent/reporter/base_reporter.py" },
      { label: "harness/workflow.py · PlanExecuteReportDriver", path: "src/deepresearch_agent/harness/workflow.py" },
    ],
  },
  {
    id: "flow-platform-message",
    order: "F3",
    name: "平台消息与执行生命周期",
    tagline: "从一条用户消息到可恢复的 Run：API → 队列 → worker → SSE",
    trigger: "用户向会话发送消息（POST /sessions/{id}/messages）。",
    persistence:
      "runs 表是唯一事实源：状态、租约、预算、检查点版本全部落 PostgreSQL/SQLite；run_events 先落库再扇出；唤醒信号允许丢失（at-most-once），由租约扫描兜底重建。",
    successExit:
      "Run 到达终态（completed/failed/budget_exhausted/cancelled）；任意 API 实例都能凭 event_id 游标向客户端完整重放事件流。",
    steps: [
      { title: "幂等与预算门", detail: "ChatService 按 client_message_id 幂等去重；日预算耗尽时直接拒绝（LLM_RATE_LIMITED，429 语义）。" },
      { title: "原子创建 Run", detail: "用户消息 + Run 行 + run.queued 事件在同一事务写入；config_snapshot 记录 min_evidence、难度模型 tier 等。" },
      { title: "投递唤醒信号", detail: "schedule() 只向 Redis Streams（或可选 Kafka）发一条 run_id 信号；入队失败计数并可观测，Run 保持 queued。", optionalNote: "Kafka 为可选传输层（T-010），语义：至少一次 + 执行后提交 offset。" },
      { title: "worker 领取执行", detail: "worker 消费信号后以条件 UPDATE 抢租约（唯一互斥量），从 Checkpoint 恢复或从 queued 开始驱动状态机。" },
      { title: "事件 durable-first", detail: "阶段/任务/工具/证据事件先写 run_events，再经 Redis 扇出给订阅者；扇出失败仅计数，不影响状态机。" },
      { title: "SSE 断线重放", detail: "GET /runs/{id}/events 先按 Last-Event-ID 游标从数据库重放，再接实时流；心跳兜底防止丢终态。", optionalNote: "Kafka-only 模式下无实时扇出，SSE 依赖数据库重放 + 心跳。" },
    ],
    branches: [
      { label: "信号丢失 → 扫描重建", detail: "租约过期扫描（默认 15s 一轮）把 queued/interrupted 的 Run 重新投递，不依赖队列重投。", tone: "warn" },
      { label: "队列断连 → 自愈重连", detail: "worker 消费流异常不退出：指数退避（1s→30s）重建生成器，重复投递由租约互斥去重。", tone: "warn" },
    ],
    codeRefs: [
      { label: "chat_service.py", path: "backend/app/services/chat_service.py" },
      { label: "run_service.py", path: "backend/app/services/run_service.py" },
      { label: "queueing.py", path: "src/deepresearch_agent/queueing.py" },
      { label: "event_stream.py", path: "backend/app/services/event_stream.py" },
      { label: "worker.py", path: "src/deepresearch_agent/worker.py" },
    ],
  },
  {
    id: "flow-evidence-contract",
    order: "F4",
    name: "Evidence 到报告验收",
    tagline: "证据的一生：入账 → 压缩卡 → 章节路由 → 覆盖门禁",
    trigger: "执行阶段的每次检索/工具调用返回结果时触发入账；报告阶段触发卡片与路由。",
    persistence:
      "Evidence 行含 run/task/tool/provider/source_id/content_hash 与 1200 字清洗摘要；EvidenceCard 持久化于 workflow_state；报告引用以 [ev_*] 形式写入最终 Artifact。",
    successExit:
      "evidence_card_coverage 通过：账本 ID（由不可变 evidence 仓库读入）与卡片/路由/正文/附录四个集合比对，路由、正文与附录不得漏掉任何卡片；账本→卡片的构建完整性由 Report 阶段保证。",
    steps: [
      { title: "确定性入账", detail: "evidence_id = ev_ + sha256(run:mode:source:content_hash)[:24]；重放/重投生成相同 ID，upsert 幂等。" },
      { title: "构建 Evidence Card", detail: "每条证据压成 ≤200 token 的卡片（声明+来源引用+局限），卡片 ID 回指账本 evidence_id，可全程反查。" },
      { title: "按章节路由", detail: "每张卡恰好分配一个 primary_section（大纲提示或词项重叠），确定性路由覆盖 LLM 的分配，保证集合可核对。" },
      { title: "分节写作与压缩", detail: "章节输入超预算时按批 Map→Digest→递归 Reduce；正文写作只消费路由到本章的卡片。" },
      { title: "全量附录", detail: "未被正文引用的证据进入报告附录索引，保证账本中每条证据在报告中可见。" },
      { title: "覆盖门禁", detail: "验证时从不可变 evidence 仓库读取真实 evidence_id 集合（不接受 reporter 自报），比对卡片/路由/正文/附录四个阶段，任一阶段遗漏即失败。" },
    ],
    branches: [
      { label: "覆盖失败 → 修稿", detail: "evidence_card_coverage 失败视为报告缺陷：修复后重新验证，预算耗尽则 FAILED。", tone: "warn" },
      { label: "压缩不丢原文", detail: "卡片压缩只改变模型看到的载荷；原始证据完整保留在账本，可随时回查。", tone: "success" },
    ],
    codeRefs: [
      { label: "harness/evidence.py", path: "src/deepresearch_agent/harness/evidence.py" },
      { label: "reporter/evidence_cards.py", path: "src/deepresearch_agent/agents/multi_agent/reporter/evidence_cards.py" },
      { label: "contracts.py", path: "src/deepresearch_agent/harness/contracts.py" },
    ],
  },
  {
    id: "flow-recovery-drain",
    order: "F5",
    name: "Worker 故障恢复与 Drain",
    tagline: "崩溃接管、断点续跑与优雅关停的完整闭环",
    trigger: "worker 进程被 SIGKILL / 容器重启 / 租约过期，或收到 SIGTERM 关停信号。",
    persistence:
      "阶段末 Checkpoint（版本+sha256 校验）；租约 TTL 默认 30s、心跳 TTL/3 续约、扫描间隔 15s；interrupted 状态与恢复映射全部落库。",
    successExit:
      "被接管的 Run 从最近 Checkpoint 恢复并推进到终态；SIGTERM 时 worker 最多等待 30s 让在跑 Run 收尾，超时不取消也不写终态（等价崩溃，由接管者续跑）。",
    steps: [
      { title: "租约与心跳", detail: "执行期间每 TTL/3 续约；续约失败说明租约被抢，当前实例抛 RunLeaseLost 并静默让位（不写任何状态）。" },
      { title: "阶段末 Checkpoint", detail: "每个阶段结束保存 RunContext 快照（版本递增 + 哈希）；恢复时按 _safe_stage_after 定位第一个未提交操作。" },
      { title: "故障检测", detail: "SIGKILL/崩溃后租约到期；扫描循环把活跃但租约过期的 Run 标记为 interrupted 并重新投递唤醒信号。" },
      { title: "接管续跑", detail: "新 worker 抢到租约后从 Checkpoint 恢复驱动状态；已提交的阶段不重做，未提交的按映射续跑。" },
      { title: "幂等重放", detail: "事件/工具调用/证据三处去重：已存在的 run_event、terminal 状态的 tool_call、相同 evidence_id 均跳过。" },
      { title: "SIGTERM 优雅关停", detail: "第一次 SIGTERM：停止拉取新任务，已交付但尚未启动的信号不再启动新 Run（Kafka 未 ack 会重投，Redis 已 ACK 则由租约扫描兜底），并在 30s 宽限内等待在跑 Run 收尾。" },
    ],
    branches: [
      { label: "宽限到期 → 等价崩溃", detail: "超过 30s 宽限仍未完成的 Run 不取消、不写终态，进程直接退出——等价一次崩溃，由租约过期扫描与接管者续跑。", tone: "danger" },
      { label: "第二次 SIGTERM → 快速退出", detail: "升级为立即退出（等价崩溃），由租约接管兜底，避免关停被单任务卡死。", tone: "danger" },
      { label: "重复接管 → 租约互斥", detail: "两个 worker 同时被唤醒时，条件 UPDATE 只允许一个获得租约；失败方静默让位。", tone: "warn" },
      { label: "执行中断连 → 消费自愈", detail: "队列连接异常时指数退避重建消费流，worker 进程不退出；期间 Run 由扫描兜底。", tone: "warn" },
    ],
    codeRefs: [
      { label: "harness/runtime.py", path: "src/deepresearch_agent/harness/runtime.py" },
      { label: "harness/recovery.py", path: "src/deepresearch_agent/harness/recovery.py" },
      { label: "worker.py", path: "src/deepresearch_agent/worker.py" },
      { label: "tests/harness/test_takeover.py", path: "tests/harness/test_takeover.py" },
    ],
  },
];

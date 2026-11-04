# Tasks: GitHub 项目作品集落地与平台运行保障

## 执行规则

- 本清单只描述工作，不代表任务已经完成。
- 只执行依赖已满足的任务；需求或设计冲突时先更新上游文档。
- 每项 `done` 必须在 `acceptance.md` 留下真实命令或人工验收证据。

## 任务清单

### T-001 — 确定作品集目标与验收边界

- 状态：`done`
- 目标：确定 GitHub 静态展示、本机 Docker 使用门槛、模型难度路由原则、RPO/RTO、保留周期和费用阈值。
- 前置依赖：`none`
- 涉及文件：`docs/specs/production-readiness/proposal.md`、`design.md`
- 验收：用户确认作品集范围和数值目标，所有未决项标记为 approved 或明确 deferred。
- 实际证据：用户确认：公开演示只放 GitHub 静态页面；Docker 操作前必须询问；模型按任务难度分层；RPO 24h、RTO 60m、Run/事件/审计保留 30d、Artifact 14d、Prometheus 15d、LLM 预算目标 $5/day 与 $50/month。
- 日期：2026-11-04

### T-002 — GitHub 静态展示与仓库安全检查

- 状态：`done`
- 目标：建立面试官可浏览的静态项目介绍；保证静态站点无密钥、无真实个人数据、无动态 API 依赖。
- 前置依赖：`T-001`
- 实现方式：复用现有 React/Vite 工具链，新增独立静态入口 `frontend/portfolio.html` + `frontend/src/portfolio/`（不导入 App/API client/QueryClient/router）；`npm run build:portfolio` 输出到 `frontend/portfolio-dist/` 并复制 `index.html` 供 Pages 根路径访问；`.github/workflows/pages.yml` 以 `/<repo>/` base 构建发布。早期独立 HTML 草稿 `site/` 已删除。
- 验收结果：两轮 code review（首轮修复 2 blocker + 5 major，复审 PASS；复审遗留 4 项亦已修复并重新构建验证）。证据明细见 acceptance.md「T-002 证据」。
- 已知遗留（非阻塞）：`vite.config.ts` 的 `process.env` 在 `tsconfig.node.json` 类型检查下报 TS2580，无脚本/CI 执行该配置，留待后续任务。
- Docker 门禁：本阶段未使用 Docker。
- 日期：2026-11-04

### T-003 — 依赖故障与队列重建

- 状态：`done`（review 修复完成，随本阶段提交）
- 目标：Redis/PostgreSQL 故障、Redis 数据丢失和 reconnect/rebuild 行为明确且可测。
- 前置依赖：`T-001`
- 实现方式（全部进程内健壮性加固，无新基础设施）：
  1. **enqueue 可靠化**：`_enqueue_with_retry`（3 次指数退避 0.2/0.4/0.8s）包装 Redis/Kafka 的 enqueue,耗尽抛 `QueueUnavailableError`;`schedule()` done-callback 观测失败(日志 + `QUEUE_ENQUEUE_FAILURES`),run 留 queued 由租约扫描重建——不在请求路径同步重试。
  2. **worker 消费循环断连自愈**：断连异常不再杀死进程——指数退避(1s→30s,stop 可打断)后重建消费生成器;新增 `WORKER_CONSUME_RECONNECTS` 计数。**顺带修复既有隐藏缺陷**：`wait_for(anext(gen), 1.0)` 超时会取消生成器(交付值丢失),改为 shield 保护的独立 anext task,超时只放弃等待。
  3. **DB 故障 API 语义**：`OperationalError` → `_map_unexpected_status` 纯函数 → HTTP 503 `DATABASE_UNAVAILABLE`(retryable=true),替代裸 500。
  4. **可观测补全**：`WAITING_RUNS` gauge 首次接线(worker 扫描轮次统计 queued 行数,`count_by_status` 新仓储方法)。
- 验收：5 个故障注入测试(enqueue 失败观测/重试后成功/Redis 层重试包装/断连自愈完成 Run/503 映射);全量 227 passed 无新增失败。
- Docker 门禁：本阶段本地未使用 Docker。
- 证据明细：见 acceptance.md「T-003 证据」。

### T-004 — 真实平台备份恢复演练

- 状态：`done`（review 修复完成，随本阶段提交）
- 目标：覆盖 PostgreSQL、Artifact、Neo4j 的备份/恢复；保留策略；恢复顺序；RPO 24h/RTO 60m 验证载体。
- 前置依赖：`T-001`
- 实现方式：
  1. `backup_restore.py` manifest v2（postgres/neo4j 段 + schema 校验）、`backup_postgres`/`restore_postgres`（pg_dump custom，DSN 转换 asyncpg→裸 postgresql://）、`verify_after_restore`（6 张核心表行数+artifact 回读）、`prune_backups`（30 天/最少 3 份）
  2. `backup_drill.py` 演练脚本（backup→破坏→restore→verify→RTO 计时 PASS/FAIL JSON）；sqlite/postgres 双 target
  3. `docs/runbooks/backup-restore.md`（每日备份/cron/恢复步骤/Neo4j dump/演练方法）
  4. `docs/acceptance/acceptance-matrix.md`（87 MUST id 全覆盖）+ scenario-template——**消除 2 个预存在测试失败**
  5. CI `backup-restore-drill` 作业：真实 PG services 容器跑演练 + RTO 阈值门禁 + 报告 artifact
- 验收：8 个新测试（演练全链路/manifest v2/verify 钩子/prune/PG 编排 stub/工具缺失指引/future schema/**PG 演练真实 restore**）；delivery 5/5;全量 237 passed 0 failed。
- Docker 门禁：本机未使用 Docker；真实 PG 演练经 CI services 容器完成。
- 证据明细：见 acceptance.md「T-004 证据」。
- 日期：2026-11-05

### T-005 — 单独迁移步骤与兼容窗口

- 状态：`done`（review 修复完成，随本阶段提交）
- 目标：将生产迁移从 API/worker 启动中分离，定义 expand/contract 和失败恢复。
- 前置依赖：`T-001`
- 实现方式：
  1. 新增 `SCHEMA_BOOTSTRAP_ON_STARTUP`（默认 true 保持本地零配置；false=生产语义，API lifespan 与 worker 启动不执行 create_schema）
  2. `scripts/migrate.py` 统一迁移入口（真实 alembic 子进程 + 5 次锁竞争重试）
  3. compose 平台模式：backend `SCHEMA_BOOTSTRAP_ON_STARTUP=false`（CMD 内 alembic 先行）、worker 先跑 migrate.py 再启动（启动不 DDL）
  4. 启动 fail-fast：跳过迁移就启动会在首次依赖查询处明确失败（诚实失败，测试断言 OperationalError + 零建表）
  5. 兼容窗口与不可逆点（0006 downgrade 清 scholar 数据）文档化：`docs/runbooks/deploy-migrate-rollback.md`
- 验收：4 个新测试（bootstrap false 零建表+fail-fast / true 行为不变 / create_schema 双调用幂等 / migrate.py 真实子进程 upgrade head）；全量 241 passed 0 failed。
- 证据明细：见 acceptance.md「T-005 证据」。
- 日期：2026-11-05

### T-006 — Worker drain、恢复和资源关闭

- 状态：`done`（review 待派，随本阶段提交）
- 目标：SIGTERM/SIGKILL 期间安全等待、checkpoint、释放资源，避免未 join 任务。
- 前置依赖：`T-003`
- 实现方式：
  1. **stop 后不启动新 Run**：消费循环在 stop 置位后关闭当前生成器并 break（在手信号由 Kafka 重投/扫描兜底）
  2. **双重 SIGTERM 升级**：第二次 SIGTERM 抛 SystemExit(130) 快速退出（等价崩溃，租约接管兜底）
  3. **扫描恢复路径改道**：queue 模式下 `recover_expired` 返回待重投列表，worker `_scan_loop` 经**队列**重投（旧行为进程内 spawn 绕过 WORKER_CONCURRENCY 且关停时被立即取消写 CANCELLED，违背 drain 语义）
  4. **资源关闭补全**：finally 全部 suppress 化（单点失败不再跳过 database.close）；worker 侧 RedisEventFanout 首次关闭；RunService.shutdown 关闭 router providers（scholar httpx aclose 首次有调用方，TimeoutBoundProvider 委托）
  5. **无界增长修复**：prefix_tracker 新增 `evict_run`，Run 终态后清除 per-run 记账；`clear_run_cancelled` 同点调用
- 验收：3 个 drain 测试（子进程 SIGTERM 优雅退出+在跑 Run 自然完成+不启动新 Run；升级分支；宽限/租约量级静态断言）；全量 244 passed 0 failed。
- 证据明细：见 acceptance.md「T-006 证据」。

### T-007 — Readiness、SLO、告警与成本控制

- 状态：`done`（随本阶段提交）
- 目标：readiness(硬依赖+成本门禁)、告警规则、平台级 LLM 成本天花板。
- 前置依赖：`T-003`
- 实现方式：
  1. `GET /api/v1/ready` readiness 端点：DB 可写（必需）+ 平台模式队列 Redis ping（必需）+ 日预算门禁；单机模式无队列自动通过；与 /health（人看面板）分工明确
  2. `LLM_DAILY_TOKEN_CEILING`（当日 output_tokens 聚合,0=不启用）：ChatService.send 超限抛 LLM_RATE_LIMITED（429 语义,幂等安全）；/ready 同步 503 摘流量
  3. `observability/alert-rules.yml` 6 条告警（queued 积压/enqueue 失败/断连重连/扇出失败/执行异常/LLM 限流暴发）+ Prometheus rule_files 挂载 + compose 卷
  4. 测试 6 个：readiness 单机 ok/DB 断连 503/成本超限 503+恢复/send 门禁 429/告警指标引用防漂移/prometheus 配置含 rule_files
- 证据明细：见 acceptance.md「T-007 证据」。
- 日期：2026-11-05

### T-008 — 本地运行、恢复与面试演示 runbook

- 状态：`done`
- 目标：运维文档索引 + 与实现一致的 runbook 套件。
- 前置依赖：`T-004`、`T-005`、`T-007`
- 交付：`docs/runbooks/README.md` 索引页（快速命令 + 第一响应速查表）；backup-restore / deploy-migrate-rollback 两份实现级 runbook（T-004/005/007 产出）；README 链接。全部为静态文档，非 Docker 步骤本地可复现。
- 验收：文档间链接有效；命令与脚本 CLI 一致（migrate/backup/restore/prune/drill/ready/health/metrics）。
- 日期：2026-11-05

### T-009 — 按任务难度配置模型路由

- 状态：`done`（随本阶段提交）
- 目标：为简单、常规、复杂/高风险任务定义 model tier 选择策略，配置化模型 ID，并默认避免产生未授权的付费调用。
- 前置依赖：`T-001`
- 实现方式：
  1. `src/deepresearch_agent/models/model_tier.py`：难度派生纯函数（report_type/min_evidence/内容中英文标记/迭代数；显式 `model_tier` 覆盖优先），tier→model 映射全部来自 `LLM_TIER_*_MODEL` 配置（空=回落 OPENAI_LLM_MODEL），无厂商硬编码
  2. chat_service 接线：请求路径解析 tier → 写入 config_snapshot["model_tier"]（可审计）→ model_snapshot 记录 model/tier/reason
  3. 执行端接线：`_execute` workflow_factory 按 Run config_snapshot 解析模型注入 DeepResearchAgent（BaseAgent 新增 llm_model 参数,默认 None 旧行为不变）
  4. 无密钥降级：路由只产出 model ID 字符串,不构造客户端;无 provider/密钥时由既有 get_llm_model/gateway 路径报错（诚实失败）
- 验收：7 个测试（推导矩阵/内容标记/显式覆盖/映射回落/开关关闭/chat_service 全链路快照断言）；全量 257 passed 0 failed。
- 日期：2026-11-05

### T-010 — 可选 Kafka 传输层（唤醒信号队列）

- 状态：`done`
- 目标：在保持 DB 事实源 + 租约互斥 + 扫描兜底语义不变的前提下，为唤醒信号队列增加可选 Kafka 实现（传输层可替换性验证与分布式队列语义的正确集成）。
- 前置依赖：`T-001`
- 实现方式：`queueing.py` 新增 `KafkaRunQueue`（至少一次 + 执行后提交 offset；run_id 分区键；幂等 producer；分区水位线单调提交；rebalance 监听器 + **分配代次防竞态**——代次校验在 `_commit_lock` 锁内复核，封堵等锁期间 rebalance 的 TOCTOU）；`create_run_queue` 按 `KAFKA_BOOTSTRAP_SERVERS > REDIS_URL > 进程内` 选择；worker 走工厂并支持 ack 流，QUEUE_DEPTH 指标按后端守卫，生成器显式 aclose；compose kafka 服务挂 `profiles: ["kafka"]`；CI `kafka-compat` 作业采用**双监听器**（EXTERNAL 广播 localhost:9094 供 runner 连接）。
- 验收结果：两轮 review（首轮 2 Major：stale-ack 竞态 + CI 广播监听器错误；次轮 1 Major：代次校验 TOCTOU——均按评审处方修复并补测试）。证据明细见 acceptance.md「T-010 证据」。
- Docker 门禁：本阶段本地未使用 Docker；真实 broker 验证经 CI services 容器完成。
- 日期：2026-11-04

### T-011 — Kafka 事件流分发

- 状态：`done`（随本阶段提交）
- 目标：全量事件流可选走 Kafka topic（DB 仍为事实源，双写失败仅计数）；独立消费组演示。
- 前置依赖：`T-010`
- 实现方式：`src/deepresearch_agent/events_kafka.py`——`KafkaEventFanout`（EventFanout Protocol 实现,key=run_id 有序写 dra-events;per-Run 订阅为过滤语义,SSE 实时仍以 Redis 为主通道）、`FanoutChain`（组合扇出,单通道失败只计数）、`AuditEventConsumer`（独立消费组 dra-audit→JSONL+按 offset 提交）。SSE 稳定主路径不动。
- 验收：4 个 stub 测试（publish 键控/per-Run 过滤/链隔离/审计消费+提交）+ CI 真实 broker 作业扩展；全量 261 passed 0 failed。
- 日期：2026-11-05

## 进度摘要

- 已完成：10
- 进行中：0
- 阻塞：0
- 待执行：0（T-002/003/004/005/006/007/008/009/010/011 全部完成；T-001 为决策基线）
- 下一步：全部任务完成；后续可选 = 用户同意 Docker 后的本机真实容器演练
- 最后更新：2026-11-04

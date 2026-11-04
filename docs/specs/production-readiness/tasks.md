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

- 状态：`pending`
- 目标：覆盖 PostgreSQL、Artifact、Neo4j，定义加密、异地保存、保留和恢复顺序。
- 前置依赖：`T-001`
- 涉及文件：`scripts/`、`docs/runbooks/`、部署配置
- 验收：干净环境恢复后读取 Run、Evidence、Report、Artifact 和 Graph 数据。
- 预期证据：恢复日志、校验命令输出、RPO/RTO 实测。

### T-005 — 单独迁移步骤与兼容窗口

- 状态：`pending`
- 目标：将生产迁移从 API/worker 启动中分离，定义 expand/contract 和失败恢复。
- 前置依赖：`T-001`
- 涉及文件：`backend/Dockerfile`、`backend/app/main.py`、`persistence/database.py`、migrations
- 验收：空库/历史库升级、旧新版本兼容、失败迁移处理。
- 预期证据：Alembic 输出和迁移测试。

### T-006 — Worker drain、恢复和资源关闭

- 状态：`pending`
- 目标：SIGTERM/SIGKILL 期间安全等待、checkpoint、释放资源，避免未 join 任务。
- 前置依赖：`T-003`
- 涉及文件：`src/deepresearch_agent/worker.py`、`run_service.py`
- 验收：长任务 SIGTERM/SIGKILL 子进程测试，验证可接管、无非法终态和无重复副作用。
- 预期证据：进程测试日志、Run 状态、事件和 tool_call 记录。

### T-007 — Readiness、SLO、告警与成本控制

- 状态：`pending`
- 目标：为本地/CI 可复现演示补齐 worker/依赖 readiness、运行告警、费用保护和难度模型路由证据；不要求部署公开监控服务。
- 前置依赖：`T-003`
- 涉及文件：`backend/app/api/v1/health.py`、`observability/`、Prometheus 配置、LLM gateway、模型配置
- 验收：依赖异常时 readiness 正确变化；模拟指标能触发告警；预算达到上限阻止新增调用；不同难度任务选择预期模型 tier 并留下记录。
- 预期证据：指标样例、告警/路由测试输出。
- Docker 门禁：任何基于容器的本机测试需先取得用户同意。

### T-008 — 本地运行、恢复与面试演示 runbook

- 状态：`pending`
- 目标：形成本地启动、迁移、备份恢复、故障演练和面试演示说明；公开部分只通过静态 GitHub 页面提供。
- 前置依赖：`T-002`、`T-004`、`T-005`、`T-006`、`T-007`
- 涉及文件：`docs/runbooks/`、README 或静态站点文档
- 验收：在无需公开动态后端的前提下，静态演示材料可浏览；本地 runbook 的非 Docker 步骤可复现。Docker 步骤只有在用户同意后执行。
- 预期证据：演练记录、静态站点链接检查和问题清单。

### T-009 — 按任务难度配置模型路由

- 状态：`pending`
- 目标：为简单、常规、复杂/高风险任务定义 model tier 选择策略，配置化模型 ID，并默认避免产生未授权的付费调用。
- 前置依赖：`T-001`
- 涉及文件：模型配置、LLM Gateway、Context/Workflow 调用入口、相关测试和文档
- 验收：确定性路由测试覆盖不同难度；无 provider/密钥时安全降级或明确报错；路由 tier/理由可审计但不泄露凭据。
- 预期证据：配置样例、路由单测输出、成本/Token 限制测试。

### T-010 — 可选 Kafka 传输层（唤醒信号队列）

- 状态：`done`
- 目标：在保持 DB 事实源 + 租约互斥 + 扫描兜底语义不变的前提下，为唤醒信号队列增加可选 Kafka 实现（传输层可替换性验证与分布式队列语义的正确集成）。
- 前置依赖：`T-001`
- 实现方式：`queueing.py` 新增 `KafkaRunQueue`（至少一次 + 执行后提交 offset；run_id 分区键；幂等 producer；分区水位线单调提交；rebalance 监听器 + **分配代次防竞态**——代次校验在 `_commit_lock` 锁内复核，封堵等锁期间 rebalance 的 TOCTOU）；`create_run_queue` 按 `KAFKA_BOOTSTRAP_SERVERS > REDIS_URL > 进程内` 选择；worker 走工厂并支持 ack 流，QUEUE_DEPTH 指标按后端守卫，生成器显式 aclose；compose kafka 服务挂 `profiles: ["kafka"]`；CI `kafka-compat` 作业采用**双监听器**（EXTERNAL 广播 localhost:9094 供 runner 连接）。
- 验收结果：两轮 review（首轮 2 Major：stale-ack 竞态 + CI 广播监听器错误；次轮 1 Major：代次校验 TOCTOU——均按评审处方修复并补测试）。证据明细见 acceptance.md「T-010 证据」。
- Docker 门禁：本阶段本地未使用 Docker；真实 broker 验证经 CI services 容器完成。
- 日期：2026-11-04

### T-011 — Kafka 事件流分发（未实现，登记）

- 状态：`pending`
- 目标：SSE 实时扇出与审计消费可选走 Kafka topic（DB 仍为事实源，双写失败仅计数）。
- 前置依赖：`T-010`
- 涉及文件：`event_bus.py`、`events_fanout.py`、`event_stream.py`、配置与文档
- 验收：多消费者组（SSE 网关组 + 审计组）+ 重放演示；双写一致性测试。
- 预期证据：集成测试输出与文档。

## 进度摘要

- 已完成：3
- 进行中：0
- 阻塞：0
- 待执行：7
- 下一步：按依赖进入 T-003（依赖故障与队列重建）
- 最后更新：2026-11-04

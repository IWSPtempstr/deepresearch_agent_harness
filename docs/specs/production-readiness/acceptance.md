# Acceptance Record: GitHub 项目作品集落地与平台运行保障

## 作品集运行基线（部分已确认）

- 公开展示：仅 GitHub 静态页面/文档，不提供公开运行中的 API 服务。**已由用户确认**。
- 本机 Docker：非必要不使用；任何 Docker pull/build/up 或 Docker 测试前先征求用户明确同意。**已由用户确认**。
- 模型选择：按任务难度分层路由，简单任务优先低成本/低延迟，复杂推理与高风险审查使用高能力模型；具体 model ID 配置化。**已由用户确认原则**。
- RPO 目标：24 小时（每日备份；非逐笔复制承诺）。**已由用户确认**。
- RTO 目标：60 分钟（干净演练环境按 runbook 恢复）。**已由用户确认**。
- 保留：Run/事件/Contract/审计 30 天；报告/Artifact 14 天；Prometheus 15 天。**已由用户确认**。
- 费用：LLM 目标上限 $5/日、$50/月；实际计费不可用时以 Token/调用数限额落实。**已由用户确认目标值**。
- 状态：作品集目标与数值已批准；具体 GitHub Pages 实现和模型 ID 映射待后续任务确定。

## 当前阶段记录：T-011 Kafka 事件流

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：`events_kafka.py`（KafkaEventFanout/FanoutChain/AuditEventConsumer）、CI 真实 broker 作业扩展、README 事件流说明。

### T-011 证据（本地，2026-11-05）

- 定向：`pytest tests/acceptance/test_events_kafka.py -q` → **4 passed**：publish 键控 JSON（懒启动）、per-Run 订阅过滤、FanoutChain 单通道失败只计数+其余通道继续+close 抑制、审计消费 JSONL 落盘+按 offset 提交。
- CI：kafka-compat 作业扩展运行 events kafka 测试（真实 broker,本机零 Docker）。
- 全量：`pytest tests/ -q` → **261 passed, 4 skipped, 0 failed**（基线 257 → 261）。
- 语义边界：SSE 实时通道仍以 Redis 为主（per-Run pub/sub 语义匹配）；Kafka 面向全量事件流的独立下游（审计/评测/数仓）——与 7.1 的引入触发条件一致。

## 当前阶段记录：T-009 模型路由

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：model_tier 纯函数（派生+映射）、chat_service 快照留痕、执行端模型注入、tier 配置块。

### T-009 证据（本地，2026-11-05）

- 定向：`pytest tests/acceptance/test_model_tier_routing.py -q` → **7 passed**：推导矩阵（long_document/min_evidence/迭代数/默认）、中英文内容标记、显式覆盖优先、tier→model 映射与空值回落、路由关闭（开关在调用方）、**chat_service 全链路**（brief→simple/fast-model，详细→complex/pro-model，快照与 model_snapshot 双留痕）。
- 全量：`pytest tests/ -q` → **257 passed, 4 skipped, 0 failed**（基线 250 → 257）。
- 文档：runbook 增补 tier 派生规则与配置说明；.env.example 三个 tier 变量。

## 当前阶段记录：T-007 readiness/告警/成本

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：/ready 端点（DB+队列+成本门禁）、LLM_DAILY_TOKEN_CEILING 请求路径强制、alert-rules.yml 6 条+Prometheus 挂载、6 个测试。

### T-007 证据（本地，2026-11-05）

- 定向：`pytest tests/acceptance/test_readiness_and_cost.py -q` → **6 passed**：
  - 单机 /ready 200（queue 非必需自动通过）；DB 断连 → 503 unavailable
  - 日预算超限 → /ready 503 + exhausted 标记；低于阈值自动恢复
  - send 请求路径超限 → AppError LLM_RATE_LIMITED（429 语义）
  - 告警规则引用的 dra_ 指标全部已注册（防漂移测试）；prometheus.yml 含 rule_files
- 全量：`pytest tests/ -q` → **250 passed, 4 skipped, 0 failed**（基线 244 → 250）。
- yamls：prometheus.yml/alert-rules.yml/compose 校验通过。

## 当前阶段记录：T-006 worker drain

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：stop 后不启动新 Run、双重 SIGTERM 升级、扫描恢复走队列重投、fanout/provider/prefix_tracker 资源关闭补全、3 个 drain 测试。

### T-006 证据（本地，2026-11-05）

- 定向：`pytest tests/harness/test_worker_drain.py -q` → **3 passed**（约 6s）：
  - 子进程 SIGTERM drain：Run 在 hold 中收到信号 → `_consume` 在宽限内优雅退出（exit_state=graceful）→ 在跑 Run 自然完成（started_runs==[run-A]，无第二次交付）
  - 双重 SIGTERM 升级分支（纯逻辑验证，不向主进程发真信号——首轮实现发真信号污染了 pytest 主循环导致 observability 测试假失败，已改）
  - SHUTDOWN_GRACE_SECONDS ≥ RUN_LEASE_SECONDS/3 静态断言
- 全量：`pytest tests/ -q` → **244 passed, 4 skipped, 0 failed**（基线 241 → 244）。
- 教训记录：向 pytest 主进程发 SIGTERM 会污染共享事件循环/registry,进程级信号语义测试必须用子进程或纯逻辑 stub。

## 当前阶段记录：T-005 迁移生命周期

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：`SCHEMA_BOOTSTRAP_ON_STARTUP` 开关（API/worker 双接线）、`scripts/migrate.py`、compose 生产语义、fail-fast 语义、迁移 runbook。

### T-005 证据（本地，2026-11-05）

- 定向：`pytest tests/acceptance/test_migration_lifecycle.py -q` → **4 passed**：
  - bootstrap=false：零建表 + lifespan 因缺表 fail-fast（OperationalError，诚实失败）
  - bootstrap=true：本地行为不变（建表）
  - create_schema 双调用幂等（含建数据后再调用）
  - `scripts/migrate.py` 真实 alembic 子进程空库 upgrade head（sessions/runs 表落位）
- 全量：`pytest tests/ -q` → **241 passed, 4 skipped, 0 failed**（基线 237 → 241）。
- 兼容窗口：历史迁移均为条件化 expand（0002/0005/0007 列存在性检查 + 回填），0006 收缩不可逆点已写入 runbook 回滚决策树。

## 当前阶段记录：T-004 备份恢复演练

- 分支：`feat/github-static-portfolio`
- 状态：`done`（待提交）
- 交付：manifest v2 + PG dump/restore + verify_after_restore + prune；backup_drill 演练脚本；runbook；acceptance-matrix（87 MUST id，消除 2 个预存在失败）；CI 演练作业+门禁。

### T-004 证据（本地，2026-11-05，review 修复后最终验证）

- 定向：`pytest tests/acceptance/test_backup_drill.py -q` → **8 passed**（含 **PG 演练全链路 stub 测试**：dump→CREATE DATABASE scratch→pg_restore 真实执行→对恢复后库 verify→pg_restore_exercised 披露断言；DSN 转换、prune 序列、future schema 干净 zip 重建拒绝）。
- sqlite 演练干跑：`run_sqlite_drill` 对真实 `data/app.db` 副本 → **passed: True, rto 0.02s**（目标 3600s），restored 5/5。
- delivery 套件：`test_delivery_phase8.py` → **5 passed**（历史 2 个失败因 docs/acceptance 缺失，本阶段补齐后消除）。
- 全量：`pytest tests/ -q` → **237 passed, 4 skipped, 0 failed**（基线 227+2failed → 237+0）。
- review 修复记录（1 轮 FAIL→修复）：2 blocker（CI 缺 PYTHONPATH 致脚本 import 崩溃；runner PG16 client 无法 dump PG17 → 安装 postgresql-client-17）、1 major（PG 演练未真实执行 pg_restore 被标记为 restore 演练 → 重写为真实恢复路径：`<db>_restore` scratch 库 + `pg_restore_exercised` 披露字段 + RTO 口径与 sqlite 对齐），minors 全修（非 PG DSN 显式拒绝、verify 表缺失包装、--archive argparse 报错、Neo4j dump 误用 load 参数、runbook DSN 转换说明、21→22 表、utcnow/重复 zip entry 清理）。
- RPO/RTO 声明与演练方法见 `docs/runbooks/backup-restore.md`；真实 PG 演练（含真实 pg_restore）由 CI `backup-restore-drill` 每次提交执行，报告 + 门禁（RTO<60m）上 GitHub。
- Neo4j dump 打包链路已支持（data/neo4j.dump 入 manifest；容器内 dump/load 步骤在 runbook，本机执行需 Docker 门禁同意）。

## 当前阶段记录：T-003 依赖故障与队列重建

- 分支：`feat/github-static-portfolio`
- 状态：`in_progress`（实现与本地验证完成，待 code review 后提交）
- 范围：enqueue 有界重试+观测、worker 消费循环断连自愈（含 anext shield 修复）、DB 故障 503 映射、WAITING_RUNS/QUEUE_ENQUEUE_FAILURES/WORKER_CONSUME_RECONNECTS 指标、5 个故障注入测试。
- 约束：本机未使用 Docker；未推送远端。

### T-003 证据（本地，2026-11-05，review 修复后最终验证）

- 定向：`python -m pytest tests/harness/test_queue_resilience.py tests/harness/test_run_queue.py -q` → **17 passed, 2 skipped**。
- 用例（resilience 5 个）：enqueue 重试耗尽→QueueUnavailableError+计数+run 留 queued；重试后成功；Redis xadd 层重试包装（精确断言退避序列 [0.2, 0.4]s）；**worker 断连自愈**（首轮抛 ConnectionError→退避重建→第二轮消费并完成整个 Run）；OperationalError/InterfaceError→503 retryable、ValueError→500。
- **实现缺陷修复（调试中发现）**：旧 `wait_for(anext(generator), 1.0)` 在超时路径取消生成器——asyncio 生成器在 yield 挂起点收到 CancelledError 即死亡，已交付值丢失；自愈重建会重复交付同一信号。改为独立 anext task + shield（超时仅放弃等待、next_item 跨轮保留），finally 先 cancel 悬挂 anext 再 aclose。
- review 修复记录（1 轮，PASS with 2 majors）：① worker.py 消费循环 done-TimeoutError 忙旋饿死 stop 信号 → `next_item.done()` 分流走自愈；② reconnect 测试 DDL 污染真实 `./data/app.db` → APP_DATABASE_URL 指向 tmp sqlite（评审实证：文件哈希字节一致但存在新建/ALTER 风险）。另修 minors：Kafka `_ensure_started` 纳入重试、503 谓词加 InterfaceError、全局 asyncio.sleep 补丁模式说明保留、WAITING_RUNS help 文本、死代码清理。
- 测试隔离（记录给后续维护者）：worker `_consume` 用 settings 默认 DB 与自建 RunService——测试 monkeypatch 注入 fake queue（create_run_queue）、测试 RunService、REDIS_URL 占位符、**APP_DATABASE_URL→tmp sqlite**、RECOVERY_SCAN_SECONDS=1s。
- 全量回归：`python -m pytest tests/ -q` → **227 passed, 4 skipped, 2 failed**（2 个失败为预存在 `docs/acceptance/` 交付文档缺失，与本阶段无关）。

## 当前阶段记录：T-010 可选 Kafka 传输层

- 分支：`feat/github-static-portfolio`
- 状态：`in_progress`（实现与本地验证完成，待 code review 后提交）
- 范围：`queueing.py` KafkaRunQueue + 工厂后端选择；settings/main/worker 接线（含 ack 流与 fanout 守卫）；compose `kafka` profile 服务；CI `kafka-compat` 作业；requirements aiokafka；.env.example/README/SDD 决策记录；T-011（事件 topic）登记为 pending。
- 约束：本机未使用 Docker；真实 broker 验证经 CI services 完成；未推送远端。

### T-010 证据（本地，2026-11-04，两轮 review 修复后最终构建）

- 定向：`python -m pytest tests/harness/test_run_queue.py -q` → **12 passed, 2 skipped**（Redis/Kafka 真实 broker 用例由 env 门控）。
- 新增用例（12 个中的 Kafka 部分）：工厂选择优先级（monkeypatch 构造器，不要求本地安装 aiokafka）、enqueue 懒启动 + run_id 分区键、至少一次（ack 前零提交/ack 后 offset+1）、乱序 ack 水位线不越位提交、rebalance 后旧代次 ack 在新水位线重建后调用仍被忽略（竞态时序）、**TOCTOU 锁等待交织**（ack 阻塞在 _commit_lock 上时发生 rebalance，锁内代次校验拦截提交）、consume() 显式禁用、close 生命周期。
- review 修复记录：首轮 2 Major（stale-ack 跨 rebalance 竞态 → 分配代次 + 闭包捕获；CI 广播监听器错误 → INTERNAL/EXTERNAL 双监听器，runner 连 localhost:9094）；次轮 1 Major（代次校验 TOCTOU → 检查移入 _commit_lock 锁内）；评审明确"修复并测试后即可提交"。
- 全量回归：`python -m pytest tests/ -q` → **222 passed, 4 skipped, 2 failed**；2 个失败为预存在（`docs/acceptance/` 交付文档缺失，stash 至已提交状态复现，与 T-010 无关）。
- 测试演进：`test_delivery_phase8` compose 服务集合断言改为"必选服务子集 + 新增服务必须挂 profiles"，约束默认拓扑不被静默扩张。
- 交互式前端未触碰；`PORTFOLIO_BASE` 构建不受影响。

## 当前阶段记录：T-002 GitHub 静态展示

- 分支：`feat/github-static-portfolio`
- 状态：`in_progress`（两轮 code review 完成：首轮修复 blocker/major，复审 PASS；复审遗留项已修复，待最终提交）
- 范围：React/Vite 独立静态作品集入口（`frontend/portfolio.html` + `frontend/src/portfolio/`）、`build:portfolio` 脚本（含构建后 `cp` 生成 `index.html`）、`.github/workflows/pages.yml`、README 静态介绍章节与工作流表述校正、`.gitignore` 增加 `frontend/portfolio-dist/` 与 `.zcode/`、删除遗留 `oauth-qr.png`。
- 约束：未使用 Docker；未推送远端；未触发 Pages 发布。

### T-002 证据（本地，2026-11-04，复审修复后最终构建）

- `npm run build:portfolio`：通过。脚本 = `tsc --noEmit && vite build --mode portfolio && cp portfolio-dist/portfolio.html portfolio-dist/index.html`；产物含 `index.html`（Pages 根路径可直接访问）与 `portfolio.html`；体积 portfolio.html 0.93–0.99 kB、css 13.25 kB（gzip 3.42 kB）、js 152.39 kB（gzip 50.05 kB）。
- bundle API/密钥扫描：`api/v1|API_BASE|api\.sessions|localStorage` → 0 命中；import 图仅 `react`、`react-dom/client`、`./PortfolioApp`、`./portfolio.css`（评审独立复核确认）。
- 子路径 base 重建：`PORTFOLIO_BASE=/deepresearch_agent_harness/` → 资产引用带子路径前缀，与 Actions 中 base 一致；评审以本地 HTTP 方式模拟 Pages 根路径访问返回 200。
- 对比度修复验证：`--pf-text-faint` 为 `#5b6b80`（bg 5.11 / sunken 4.88 / white 5.17，均 ≥4.5）；`prefers-reduced-motion` 块新增 `html:has(.pf-root){scroll-behavior:auto!important}`（已确认在构建产物中）。
- 交互式前端回归：`npm run build` 通过（默认产物哈希与改动前一致）；`npm test` 9 passed（sse.test.ts）。
- Code review：首轮 2 blocker + 5 major + 若干 minor，全部修复；复审 PASS；复审遗留 1 major（对比度 token 残余失败）+ 2 minor（reduced-motion、SDD 矛盾）已按建议修复并以重新构建验证。
- 已知遗留（非阻塞）：`vite.config.ts` 使用 `process.env.PORTFOLIO_BASE`，`tsc -p tsconfig.node.json` 下报 TS2580；无 npm/CI 脚本执行该配置（两个 build 脚本只查 `tsconfig.app.json`），暂不加 `@types/node` 依赖，留待后续任务处理。

## 当前状态

这是 SDD 的验收记录，不是“已生产上线”声明。T-001、T-002 已完成；T-003 及之后任务尚未执行。

## 基线

- 文档建立日期：2026-11-04
- 代码基线：以当前 Git 工作区为准
- 已有项目级检查：README 记录 `python -m pytest -q`、前端 `npm run build`，CI 包含后端测试、evaluation gate 和 PostgreSQL 兼容性作业。
- 本 SDD 任务结果：T-001 已完成（决策记录）；T-002 本地验证已通过并记录于上方，其余任务尚未执行。

## 验收清单

- [x] T-001 作品集目标与验收边界
- [x] T-002 GitHub 静态展示与仓库安全检查（两轮 review 通过，证据见上方；本地提交于 `feat/github-static-portfolio`，未推送）
- [x] T-003 依赖故障与队列重建（提交 604af58）
- [x] T-004 真实平台备份恢复（CI 真实 PG 演练作业 + sqlite 干跑 PASS；本机容器演练待用户同意 Docker）
- [x] T-005 单独迁移与兼容窗口（开关+独立入口+fail-fast+镜像 COPY scripts；证据见上方）
- [x] T-006 worker drain 与资源关闭（3 测试+资源关闭补全；证据见上方）
- [x] T-007 Readiness、SLO、告警、费用
- [x] T-009 按任务难度配置模型路由
- [x] T-008 runbook 索引与实现级文档套件（runbooks/README.md + 两份实现级 runbook）
- [ ] T-009 按任务难度配置模型路由
- [x] T-010 可选 Kafka 传输层（两轮 review 通过，证据见上方；本地提交于 `feat/github-static-portfolio`，未推送）
- [x] T-011 Kafka 事件流分发（FanoutChain+审计消费组；证据见上方）

## 证据记录格式

每项完成后填写：

```markdown
### T-xxx
- 日期：
- 命令/步骤：
- 结果：pass / fail / blocked
- 输出摘要：
- 日志/报告路径：
- 遗留问题：
```

## 未完成与风险

- 公开动态 API/TLS 服务不在范围内。
- Docker 本机操作需逐次取得用户明确同意；真实 broker 验证经 CI services 完成。
- 任务难度模型路由（T-009）尚未实施或测试。
- 真实 PostgreSQL/Artifact/Neo4j 备份恢复尚未演练；Redis/PostgreSQL 故障注入尚未完成（T-003）。
- `docs/acceptance/` 两个预存在失败（交付文档缺失）待后续任务修复。

## 交接

T-001、T-002、T-010 已完成。下一步按依赖进入 T-003（依赖故障与队列重建）。

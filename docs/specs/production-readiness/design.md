# Design: GitHub 项目作品集落地与平台运行保障

## 1. 设计摘要

采用“保留现有 Harness 语义、把仓库整理成可审查和可复现的作品集项目”的方案。GitHub 只承载静态展示；本地运行仍可使用现有 API/worker/数据库架构，但 Docker 仅为可选路径，任何本机 Docker 操作前都必须得到用户同意。设计重点是数据恢复、依赖故障、迁移、worker 生命周期、测试证据和按难度路由模型，不建设公开在线服务。

## 2. 现状与目标架构

### 现状

- `backend/app/services/run_service.py`：调度 Run、恢复过期租约。
- `src/deepresearch_agent/queueing.py`：Redis Streams consumer group，读取后 ACK；数据库是事实源。
- `src/deepresearch_agent/worker.py`：消费 Run 信号、周期扫描和关停处理。
- `src/deepresearch_agent/harness/runtime.py`：Lease、Checkpoint、预算、状态机和 Contract。
- `backend/app/services/event_stream.py`：DB 重放 + 实时订阅的 SSE。
- `docker-compose.yaml`：PostgreSQL、Redis、Neo4j、backend、worker、frontend 等平台拓扑。

### 目标

```text
GitHub static pages / README
  └── architecture, design decisions, screenshots/recordings, test evidence, local run guide

Optional local execution (Docker path requires prior user approval)
  ├── backend API ──> PostgreSQL (Run/Evidence/Event fact source)
  │               └──> Redis Streams (wake-up signal)
  ├── worker ────────> HarnessRuntime (Lease + Checkpoint + Contract)
  └──> Artifact / Neo4j / explicitly configured LLM provider

PostgreSQL/Artifact/Neo4j -> encrypted backup -> off-host storage -> restore drill
Redis -> rebuildable signal layer; DB scan reconstructs eligible work
Metrics/Logs/Trace -> local or CI evidence; no public live service requirement
```

## 3. 模块与接口变更

| 模块/文件 | 当前职责 | 计划变更 | 兼容性 |
|---|---|---|---|
| `backend/app/main.py` | API lifespan 和启动 | 移除生产环境中的隐式 DDL；增加 fail-closed 配置和 readiness | 保留开发模式兼容 |
| `src/deepresearch_agent/queueing.py` | Redis 唤醒队列 | 增加连接重试、降级状态和信号重建入口 | 保持 `RunQueue` Protocol |
| `src/deepresearch_agent/worker.py` | 消费/扫描/关停 | 统一 drain、取消、checkpoint、join 和资源关闭 | 不改变 Lease 语义 |
| `backend/app/services/run_service.py` | 调度/恢复 | 明确 Redis 故障时的 DB 扫描与重入队策略 | 保持幂等 |
| `persistence/`/`scripts/` | 数据/本地备份 | 增加平台数据库、Artifact、Neo4j 的备份恢复流程 | SQLite 工具继续用于开发 |
| `observability/` | 指标/追踪 | 增加 worker/readiness/SLO/成本指标与告警 | 保持现有指标兼容 |
| `docker-compose.yaml`/nginx | 本地可选运行拓扑 | 保留本地启动说明；任何 Docker 操作须先询问用户，不要求公开 TLS/API 服务 | 本地开发兼容 |
| README / GitHub Pages | 项目入口和公开展示 | 展示架构、证据、静态 demo 和本地运行步骤；不包含密钥/真实用户数据 | 不依赖动态后端 |
| 模型配置/路由 | LLM provider 初始化 | 增加按任务难度选择 model tier 的配置和记录 | 保留显式 opt-in provider |

## 4. 数据、状态与错误流

- Redis enqueue 失败：记录可观测错误，Run 仍以 PostgreSQL 状态为准；重建扫描负责重新投递。
- Redis consume 失败：worker 进入 degraded/reconnect 状态，指数退避；恢复后从 DB 扫描可执行/中断 Run。
- PostgreSQL 不可用：不宣称 Run 已提交或已完成；API 返回明确错误，worker 暂停需要事实源的推进。
- worker 被终止：Lease 过期后由扫描标记 interrupted，接管者从最近 Checkpoint 恢复；`RunLeaseLost` 的旧 worker 不得写状态。
- 外部写副作用：必须使用稳定 tool_call_id/幂等键和确认查询，不能依赖队列恰好只投递一次。
- 迁移失败：阻止应用晋级，按备份恢复或前向修复策略处理。

## 5. 配置、迁移与模型选择

- GitHub 静态页面不得存储或请求 API keys/secrets，也不直接连接真实后端。
- 本地运行必须显式配置数据库、Redis、Artifact 根目录和 LLM provider；默认使用 mock/offline 或不触发外部付费请求。
- 迁移改为独立的 release/migration job；API/worker 正常启动不执行竞争性 DDL。
- 使用 expand → deploy → backfill → contract 的兼容窗口；不可逆迁移必须有恢复演练。
- 模型路由按任务难度分层：简单任务用低成本/低延迟模型，普通分析/编码用通用模型，复杂推理/架构/高风险审查用高能力模型。具体模型 ID 配置化；每次路由记录 tier 和选择理由，费用/Token 预算仍为硬约束。
- 所有基于 Docker 的本机 pull/build/up/test 操作均需先取得用户明确同意；优先使用本机现有依赖、pytest、静态检查和 GitHub CI。

## 6. 安全、权限与资源

- GitHub Pages 只发布静态资产；不得放入密钥、真实运行记录或个人数据。
- 本地依赖服务不应直接暴露公网；真实 provider 凭据通过本机环境配置，不写入仓库。
- 保留 per-run Token/tool/retry/wall-time 预算；按模型难度路由前检查可用预算，超出预算停止或请求用户确认。
- 公开 live demo 的 TLS、认证和 Secret 方案不在当前范围；如需求改变须新建/更新 proposal。
- 生产日志默认关闭 verbose/debug；Artifact 路径、权限和容量必须可监控。

## 7. 替代方案与取舍

| 方案 | 优点 | 缺点 | 选择结论 |
|---|---|---|---|
| 继续加固 Compose + 托管依赖 | 变化小、复用现有 Harness、落地快 | 单主机 Compose 本身不是全量 HA | 第一阶段选择 |
| 立即引入 Kubernetes | 调度/发布能力强 | 运维面和改造面显著扩大 | 非本阶段前置条件 |
| Kafka/RabbitMQ 替换 Redis Streams | 更丰富的队列/日志能力 | 当前队列只传唤醒信号，新增恢复语义会与 DB/Lease 打架 | 生产必要性上暂不引入；**T-010 作为可选传输层落地**（见 7.1） |
| 让 Redis 作为任务事实源 | 队列恢复更直接 | 与当前 DB 事实源和审计模型冲突 | 不选择 |

### 7.1 T-010 决策记录：可选 Kafka 传输层（2026-11-04）

- **动机**：用户确认以"传输层可替换性验证 + 分布式队列语义的正确集成"为目标引入 Kafka（能力展示定位），非生产必选；README/SDD 明确标注该定位。
- **嵌入点**：仅替换唤醒信号传输（`RunQueue` Protocol 新增 `KafkaRunQueue` 实现），事件流分发（Kafka topic）登记为 T-011 未实现；SSE 实时扇出仍走 Redis，Kafka-only 模式下 SSE 依赖 DB 重放 + 心跳兜底。
- **消费语义**（与 Redis 版"读出即 ACK"的有意差异）：**至少一次 + 执行后提交 offset**——crash/rebalance 未提交信号重投，重复执行由租约条件 UPDATE（唯一互斥量）去重；分区键 = run_id 保序；幂等 producer（acks=all + enable_idempotence）；按分区水位线单调提交（乱序完成的任务不越位提交）；rebalance 监听器清理本地水位线。DB 扫描兜底语义不变。
- **不采用 exactly-once/事务生产者**：唤醒信号幂等，事务性开销不值；此取舍与 7 的"队列不做事实源"一脉相承。
- **默认拓扑不变**：kafka 服务挂 compose `profiles: ["kafka"]`；CI 新增 `kafka-compat` 作业（services 容器跑真实 broker 回归，本机无 Docker 需求）。

## 8. 测试与观测

- 单元：配置 fail-closed、队列重连、重建候选、worker drain、幂等和错误分类。
- 集成：真实 PostgreSQL/Redis 启停、Redis 丢失、DB 不可用、worker SIGTERM/SIGKILL、SSE 断线重放。
- 恢复：实际部署拓扑的 PG/Artifact/Neo4j 备份恢复与 Redis 队列重建。
- 发布：迁移升级、滚动兼容、回滚/前向修复。
- 指标：队列年龄/积压、Run 卡住、worker 存活、依赖错误、LLM 延迟/失败、Token/费用、SSE 扇出失败。
- 依据：`tests/harness/`、`tests/persistence/`、`tests/api/`、`scripts/evaluation_gate.py`、`.github/workflows/ci.yml`。

## 9. 作品集部署决策基线

为服务 GitHub 面试展示，目标是“静态页面可浏览、仓库可审查、本地可复现”，不搭建公开运行中的后端服务：

- **公开展示**：GitHub Pages 或等价 GitHub 静态页面只展示架构、流程、截图/录屏、测试证据和本地运行说明；不得嵌入 API 密钥或从浏览器直连私有后端。
- **本地 Docker**：Docker Compose 保留为可选部署路径，但不是默认测试方式。任何 Docker pull/build/up 或涉及容器的本机验证，执行前必须先征求用户明确同意；优先使用本机已有依赖、pytest、静态检查和 CI 配置验证。
- **RPO 24h / RTO 60m**：只作为平台模式备份恢复演练目标，不宣称 GitHub 静态展示本身提供服务 SLA。
- **保留周期**：Run/事件/Contract/审计 30 天；报告/Artifact 14 天；Prometheus 15 天；静态 GitHub 页面不得发布个人数据、凭据或生产运行数据。
- **认证/TLS**：静态站点不承载认证凭据；本地 HTTP 仅供开发。未来如增加 live demo，须另立 proposal 决定 TLS、身份认证和 Secret 管理。
- **成本**：真实 LLM provider 默认不自动调用，需用户显式配置；目标限额每日 $5、每月 $50，无法精确计费的 provider 使用 Token/调用预算硬限制。
- **难度模型路由**：模型配置按任务等级分层：简单确定性/格式整理任务→快速低成本模型；常规分析与代码修改→通用模型；复杂架构、跨模块推理、安全/可靠性审查→高能力模型。具体 provider/model ID 放配置中，不能写成不可替换的硬编码规则。

## 10. 未决问题

- [x] 用户确认 RPO 24h、RTO 60m、数据保留周期及费用目标（详见 proposal）。
- [ ] 确认 GitHub Pages 或 README 静态展示的具体实现方式。
- [ ] 确定简单/常规/复杂任务各自映射的模型 ID 和是否提供可选 provider 配置。
- [ ] Docker 仅在用户逐次明确同意后用于本机运行或测试。

## 11. 设计状态

- 状态：`reviewed-pending-decision`
- 关联 proposal：`./proposal.md`
- 日期：2026-11-04

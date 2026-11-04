# Proposal: GitHub 项目作品集落地与平台运行保障

## 1. 背景与问题

当前项目已经具备平台模式的基础：PostgreSQL 持久化 Run 状态、Redis Streams 投递唤醒信号、独立 worker 执行、Lease/Checkpoint 恢复、SSE 事件重放和基础 Prometheus 指标。目标是把它整理成**面试官可通过 GitHub 静态页面浏览、可审查代码、可按说明本地复现**的完整项目；不部署公开运行中的 API 服务。

参考：`README.md` 的平台拓扑与测试说明、`src/deepresearch_agent/queueing.py`、`backend/app/services/run_service.py`、`src/deepresearch_agent/worker.py`。

## 2. 场景与用户

- 角色：平台运维人员、后端开发者、研究任务用户
- 场景：用户通过受保护的 API/前端提交长时研究任务，任务在 worker 中执行；服务或 worker 发生故障后可恢复，运营人员可备份、升级、监控和回滚。
- 当前流程：Compose 启动服务，依赖本地配置和持久卷，具备基础恢复演示。
- 目标流程：面试官通过 GitHub 静态页面理解项目并核对测试证据；本地按说明复现运行与故障恢复；模型调用按难度路由并受预算约束。

## 3. 目标

- [ ] 建立可通过 GitHub 静态页面审查的项目展示：架构、设计决策、演示素材、测试证据和本地启动说明。
- [ ] 证明 PostgreSQL、Artifact、Neo4j 的备份可恢复，并定义 Redis 丢失后的队列重建。
- [ ] 明确 Redis/PostgreSQL/worker 故障行为并用测试验证。
- [ ] 将迁移、发布、worker drain、回滚变成可重复运行的本地/CI 流程。
- [ ] 建立 worker/依赖/SLO/成本告警和运维 runbook。
- [ ] 建立按任务难度选择模型的配置与使用规则，并避免默认产生外部模型费用。

## 4. 非目标

- 不处理单租户/多租户架构。
- 不部署公开运行中的后端/API 服务；公开展示限 GitHub 静态页面和文档。
- 不把更换 Kafka/RabbitMQ 作为本阶段目标。
- 不默认引入 Kubernetes；Compose 保留为可选本地复现路径，非默认测试方式。
- 未经用户明确同意，不在本机执行 Docker pull/build/up 或基于 Docker 的测试。
- 不在本 proposal 中直接修改业务实现。

## 5. 范围与影响面

- 代码模块：`backend/app/`、`src/deepresearch_agent/harness/`、`src/deepresearch_agent/worker.py`、`src/deepresearch_agent/persistence/`。
- 基础设施：`docker-compose.yaml`、`backend/Dockerfile`、`frontend/nginx.conf`、Prometheus 配置。
- 运维：`scripts/`、`docs/`、备份恢复和发布 runbook。
- 测试：故障注入、迁移兼容、恢复演练、SSE/队列和 worker 生命周期测试。

## 6. 约束与不变量

- Run 状态、事件和关键轨迹以 PostgreSQL 为事实源。
- Redis Streams 仍只承担唤醒信号；不能新增与 Lease 扫描冲突的第二套恢复语义。
- Completion Contract、Evidence Ledger、Checkpoint、Lease、幂等和 Honest Failure 语义不能被生产化改造削弱。
- 外部依赖不可用时必须明确 degraded/failed，不伪造成功。
- 所有破坏性迁移必须有恢复或前向修复策略。

## 7. 作品集运行目标（T-001 已确认）

本项目的目标是作为**可复现、可审查的 GitHub 完整项目**展示，不宣称具备商业 SaaS 级基础设施高可用。以下目标用于约束 README、部署演练和验收口径。

- **RPO：24 小时**。作品集版本要求至少展示每日备份策略；不承诺 24 小时内任意时刻的数据都可恢复。
- **RTO：60 分钟**。在一台准备好的主机或干净演练环境中，按照 runbook 恢复核心服务并验证 Run/Evidence/Report 可读。
- **数据保留**：Run、事件、Contract 和审计记录 30 天；报告和 Artifact 14 天；Prometheus 指标 15 天。保留周期可通过配置调整，但必须有清理策略和容量说明。
- **展示形态**：公开演示仅在 GitHub 放置静态页面/文档，不部署公开运行中的 API 服务，不要求公开站点提供真实研究能力。
- **本地运行**：README 提供可复现的本地启动说明；Docker 是可选路径，不是默认测试手段。任何本机 Docker 拉取、构建或运行操作前，必须先征求用户同意；优先使用已有环境、静态检查和非 Docker 测试。
- **认证/TLS**：静态 GitHub 页面不承载 API 密钥，也不连接真实后端；如后续提供受保护的 live demo，再单独设计 TLS、认证和密钥托管，不纳入当前默认目标。
- **费用上限**：真实 LLM provider 采用显式 opt-in，并设置每日 5 美元、每月 50 美元的作品集保护目标；provider 无法精确计费时以 Token/调用预算作为硬限制，并明确预算不是云账单保证。
- **模型路由**：按任务难度分层选择模型：简单任务使用低成本/低延迟模型，中等任务使用通用模型，复杂推理、架构设计或高风险审查使用高能力模型；模型映射应配置化并记录选择理由，避免把具体厂商/型号写死在流程规范中。

## 8. 风险与开放问题

| 问题 | 影响 | 处理方式 | 决策人 |
|---|---|---|---|
| RPO/RTO、保留周期和费用数值 | 已确认：RPO 24h、RTO 60m、运行数据 30d、Artifact 14d、Prometheus 15d、LLM $5/day 与 $50/month | 用户已确认，按此验收 | 用户 |
| GitHub 静态展示使用 Pages 还是 README 为主 | 面试官浏览体验 | 选择静态页面实现；不得部署动态 API | 用户 |
| 模型难度分级具体对应哪些 provider/model ID | 质量、延迟和费用 | 配置化，并根据模型可用性验证 | 用户/开发者 |
| Redis 丢失后是否完全依赖 DB 重建唤醒信号 | 恢复延迟 | 增加重建脚本和演练 | 用户/开发者 |

## 9. 成功标准

- [ ] GitHub 静态页面可浏览架构、设计决策、演示素材、测试证据和本地启动方式；不依赖动态后端。
- [ ] 默认启动/演示不泄露密钥、不自动调用付费模型。
- [ ] 模型路由策略按任务难度选择模型，映射通过配置管理，并有可复现的验证记录。
- [ ] 在干净环境中从实际 PostgreSQL/Artifact/Neo4j 备份恢复，并验证 Run、证据和报告可读。
- [ ] Redis 重启/丢失后，数据库中的可执行 Run 可以被重新发现并恢复。
- [ ] worker SIGTERM/SIGKILL 后，不出现未受控的重复副作用，Run 可从 checkpoint 接管。
- [ ] 迁移由单独步骤执行，旧版本与新版本的兼容窗口有测试证据。
- [ ] 本机 Docker 拉取/构建/运行或容器测试均先取得用户明确同意。

## 10. 决策状态

- 状态：`approved`
- 用户确认：GitHub 静态展示、Docker 须事先询问、模型按难度路由；RPO/RTO/保留周期/费用目标已于 2026-11-04 确认
- 日期：2026-11-04

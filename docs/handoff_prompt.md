# 交接 Prompt：deepresearch_agent_harness 平台化改造

> 使用方式：fork 并 clone 仓库后，把本文全部内容作为首条消息发给仓库目录下的 agent（Codex 等）。
> 注意：先自行替换「环境事实」一节中的路径与密钥占位符；密钥永远走环境变量，不要写进仓库。

---

## 0. 你的角色与使命

你是本仓库的资深后端/Agent 平台工程师，负责把一个单机的多智能体深度研究系统改造成**多实例、可观测、多租户的 Agent Runtime 平台**。改造分三个阶段（P0/P1/P2），每阶段结束必须满足验收清单并通过全部测试后才进入下一阶段。

大原则：
- **不重写 Harness 核心语义**（状态机、Completion Contract、Evidence Card、预算、幂等）。它们是本项目最有价值的资产，只做「存储与调度层」的替换和扩展。
- 每个阶段以小步提交推进，每次改动后全量跑 `pytest`，保持原有 134 个测试始终绿色（行为变化处允许修改测试，但要在提交说明里写明理由）。
- 所有新配置走环境变量 / `.env` + pydantic-settings，提供合理默认值，密钥绝不入仓库。
- 遵循仓库现有代码风格与目录约定；先读代码再动手，README 与本文档的描述如有出入，以代码为准并在 PR 描述中指出差异。

## 1. 仓库现状（模块地图，开工前先自行核对）

- 语言栈：Python 3.10+，FastAPI 后端（`backend/`），React 18 + TS + Vite + TanStack Query 前端（`frontend/`），Neo4j 5.22（+APOC+GDS）图库，SQLite（SQLAlchemy + Alembic）持久化，Faiss + SentenceTransformers 向量。
- 核心包 `src/deepresearch_agent/`：
  - `agents/multi_agent/`：Planner（问题→DAG 任务图）、Worker（隔离状态执行，串行/并行）、Coordinator（确定性归并）、Reflection（重试/重规划）、Reporter。
  - `harness/`：Runtime、状态机、租约(lease)、版本化 checkpoint、幂等工具调用、多维预算、Completion Contract 质量门、暂停/取消/恢复。
  - `context/`：上下文块、预算、信任边界、Evidence Card 压缩。
  - `memory/`：Curated Memory（跨 session 原子事实/偏好，带来源与生命周期）、Session Summary、FTS5 情景回忆。
  - `evolution/`：轨迹蒸馏→Lint&Safety→离线评测→人工晋升门→版本化 Skill Registry。
  - `evaluation/` + `scripts/evaluate_runs.py`：完成率、引用有效率、时延、token、precision/recall/MRR/nDCG。
  - `persistence/`（SQLite/SQLAlchemy/Alembic）、`retrieval/`（GraphRAG / Web-Tavily 双源，Run 内禁止跨源）、`graph/`、`search/`。
- 后端 API：sessions / runs（含 pause/cancel/resume）/ 持久化 SSE / evidence ledger / reports / memories CRUD / skills / evaluations。
- 根目录 `build_knowledge_graph.py` 构建 GraphRAG。

第一步行动：通读以上模块（重点 `harness/` 与 `persistence/`），输出一份《现状核实与差距确认》笔记（500 字内）后再开工。

## 2. 最终形态

> 一句话：以「学术科研情报深度研究」为演示工作负载的多租户 Agent Runtime 平台。

- 多实例 API + 多 worker 执行池，PostgreSQL 主存，Redis 做队列/租约/SSE 扇出，Neo4j 图库。
- 自研 LLM 网关：多 provider 路由、failover、令牌桶限流排队、token 计量落库。
- OTel 全链路追踪 + Prometheus 指标 + Grafana 面板。
- k6 压测报告与 CI 评测回归门。

## 3. 阶段任务

### P0 · 从单机到分布式（核心）

1. **SQLite → PostgreSQL**：SQLAlchemy 已有抽象，逐模块切换连接串；Faiss 单机索引 → pgvector（迁移 `retrieval/` 的向量读写；Neo4j 不动）。补 Alembic 迁移脚本；docker-compose 增加 postgres 服务。
2. **执行层队列化**：调研 Redis Streams vs ARQ 并在 PR 里给出选型结论（默认 Redis Streams：可控性更好且与租约机制天然衔接）。Run 的执行从 API 进程剥离到独立 worker 进程；compose 中 api 与 worker 分容器。
3. **分布式租约**：把现有单机 lease 升级为 Redis TTL + 心跳续约 + 崩溃自动转移：worker 挂掉后租约到期，其他 worker 依据 checkpoint 接管半途 Run（断点恢复已有，验证跨进程接管正确性）。新增并发测试：模拟 worker 被杀（SIGKILL）后 Run 在另一 worker 恢复完成。
4. **SSE 跨实例**：事件写入 Redis pub/sub（channel 按 run_id），API 实例订阅后推给客户端；任何实例都能看到任意 Run 的事件流。保持现有 SSE 断线重放语义（durable stream）不回归。

P0 验收：`docker compose up`（api×2 + worker×2 + postgres + redis + neo4j）后，杀掉任一 worker，进行中的 Run 在 60 秒内被接管并完成；SSE 从两个 API 实例都能订阅同一 Run；pytest 全绿（含新增并发/接管测试）。

### P1 · LLM 网关与可观测性

5. **LLM 网关层**（新模块 `llm_gateway/`）：统一出站调用入口；provider 链式 failover（主→备，按健康度退避）；**令牌桶限流排队**（per-tenant 并发上限 + 排队，超时返回 429 语义）；每次调用记录 provider/model/token 用量/时延/错误到新表 `llm_calls`（与既有预算机制打通：预算扣减以网关计量为准）。
6. **OpenTelemetry**：接入 openllmetry/openinference 语义约定，trace 覆盖：HTTP 入口 → Planner 拆解 → 每个 Worker step（含工具调用、LLM 调用）→ Verify → Report；trace_id 贯穿日志。
7. **Prometheus 指标**：队列深度、活动/等待 Run 数、Run 时延直方图（P50/P99）、LLM 网关 per-provider 时延/错误率/限流命中、token 消耗（按 tenant/model 分维）；compose 加 Prometheus + Grafana，导出一份含 4+ 面板的 JSON。

P1 验收：手动 kill 主 provider 进程模拟故障，Run 自动 fallback 到备用并完成，`llm_calls` 有完整计量；Grafana 能看到一条 Run 的全链路 trace 与指标；限流场景（单租户并发压到上限）请求进入排队而非报错。

### P2 · 多租户、CI 与压测

8. **多租户骨架**：表 `tenants` + `api_keys`（hash 存储），FastAPI dependency 注入 tenant 上下文；核心业务表加 `tenant_id` 并行级隔离（查询强制过滤）；按租户的预算与限流配置。
9. **CI**（GitHub Actions）：lint + pytest + 前端 build + `scripts/evaluate_runs.py` 对固定 gold 集的回归门（指标低于阈值则失败，阈值先定在当前基线 -5%）。
10. **k6 压测**：场景「N 并发提交研究 Run + SSE 订阅」，输出吞吐、错误率、P99 报告（JSON + 图表入 `docs/perf/`），并在 README 给出容量结论。
11. **学术科研情报演示工作负载**：`retrieval/` 新增学术 provider（OpenAlex 为主：免费、基础查询免 key，polite pool 仅需配置 mailto；arXiv 作为第二 provider，顺带演示多 provider 路由）；`build_knowledge_graph.py` 支持从 OpenAlex 构建论文–作者–机构–venue–概念–引用图谱；预置 3 个演示研究问题（如「方法 X 近三年的演进路线与尚未解决的缺口」），产物报告每条结论可回溯到论文 DOI/原文；评测 gold 集基于 OpenAlex 快照固化，保证 P2-9 CI 回归门答案稳定。

P2 验收：两个租户数据严格隔离（自动化测试证明）；CI 全绿含评测门；k6 报告产出；README 含架构图、快速启动、压测与评测数字。

## 4. 工程守则

- 数据库变更一律走 Alembic 迁移，禁止手改 schema。
- 新增依赖须说明理由并锁版本（uv/pip-tools）。
- REST API 保持向后兼容；确需破坏性变更，先加 `/api/v2` 前缀。
- 每个 P 任务一个 PR/提交序列：标题 `P0-2: distributed lease via Redis TTL` 风格，正文含设计要点与测试证据。
- 中文注释用于解释「为什么」，代码命名用英文。
- 每阶段结束更新 `README.md` 的架构图与启动说明，并在 `docs/` 下写一篇阶段总结（改了什么、为什么、数字）。

## 5. 环境事实（开工前替换占位符）

- 运行环境：WSL2 Ubuntu-22.04，Python 3.10+，Node 18+，Docker + docker-compose 可用。
- 仓库路径：`<REPO_PATH>`（例：/home/new_work/deepresearch_agent_harness）。
- LLM 接入：`OPENAI_BASE_URL` / `OPENAI_API_KEY` 等环境变量注入（兼容任意 OpenAI 协议中转），另配一个备用 provider 做 failover 演示。
- Tavily key（web 检索）：`TAVILY_API_KEY`；OpenAlex 与 arXiv 免 key（建议配置 `OPENALEX_MAILTO` 进入 polite pool）。
- Neo4j：docker-compose 内自建（NEO4J_AUTH 环境变量）。
- 所有服务只绑本地/内网端口，项目仅用于求职演示。

## 6. 现在开始

先执行第 1 节末尾的「第一步行动」：通读 `harness/` 与 `persistence/`，输出《现状核实与差距确认》，然后从 P0-1（PostgreSQL 切换）开工。

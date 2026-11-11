<h1 align="center">DeepResearch Agent Harness</h1>

<p align="center">
  <strong>面向复杂研究任务的可恢复、多智能体、证据驱动 Agent Runtime</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black">
  <img alt="Neo4j" src="https://img.shields.io/badge/Neo4j-5.22-4581C3?logo=neo4j&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/CI-backend%20%2B%20frontend%20%2B%20PostgreSQL-22A06B">
  <img alt="Evidence" src="https://img.shields.io/badge/evidence%20coverage-100%25-7C3AED">
</p>

<p align="center">
  <a href="https://deepresearch-harness-qwzy.zai.site">在线演示</a> ·
  <a href="#核心亮点">核心亮点</a> ·
  <a href="#系统架构">系统架构</a> ·
  <a href="#快速启动">快速启动</a> ·
  <a href="#测试与评测">测试与评测</a> ·
  <a href="#常用-api">API</a>
</p>

> **在线站点**：项目已部署为可运行网站（[deepresearch-harness-qwzy.zai.site](https://deepresearch-harness-qwzy.zai.site)）——单机模式运行真实前后端。公开实例未配置模型/检索密钥：界面与会话管理可用，发起研究任务会诚实失败；完整能力请按下方说明在本地部署并配置密钥。

> 以可恢复的 **Agent Harness** 为运行底座，驱动唯一 **coreact 路径**（BriefBuilder 简报规划 → Controller 步级决策 → ActionRunner 隔离执行 → 步级验证与有限修正 → Reporter → Completion Contract）闭环，统一接入私域 GraphRAG 与 Web/学术搜索，并提供全量证据上下文压缩、持久化执行（多动作 intent 与耐久恢复）、实时可观测、受控自进化和系统化 Agent 评测能力。

> **定位说明**：本项目是工程化的研究 Agent 系统实现（简历/作品集项目），**不是 Co-ReAct 等论文的算法复现**，也未开展任何效果对比实验（A/B、基准分数）——正确性、故障恢复与安全测试见 `docs/specs/coreact-consolidation/acceptance.md`；引用论文仅作为 rubric/反馈/重试机制的思想来源。

系统关注的不只是“生成一篇报告”，而是让长时间、多步骤、强证据约束的 Agent 任务能够可靠执行、失败恢复、过程审计、结果验证和持续演进。

---

## 核心亮点

### 1. 可恢复 Agent Harness

Harness Runtime 将研究任务封装为**唯一 coreact 路径**（`plan_execute_report`）的持久化执行协议（T-017：独立 deep_research 与 staged 整批引擎已退役删除）：

- Session / Run 状态机管理会话与单次研究任务；
- Lease、版本化 Checkpoint 和幂等 ToolCall 防止重复副作用；
- Token、工具调用、并发数、重试次数和墙钟时间多维预算控制；
- 支持取消、澄清、中断恢复、错误分类重试和动态 Replan；
- Completion Contract 在 Run 完成前执行强制质量门禁。

### 2. 闭环 Agent Loop

~~~mermaid
flowchart LR
    U([User Request]) --> C[Context Build]
    C --> P[Plan]
    P --> E[Execute]
    E --> R[Report]
    R --> V{Completion Contract}

    V -->|8/8 Pass| DONE([Verified Completion])
    V -->|Report defect| FIX[Targeted Repair]
    V -->|Evidence gap| RP[Dynamic Replan]
    V -->|Budget / permission blocked| FAIL([Honest Failure])

    FIX --> R
    RP --> P

    M[(Memory)] -. bounded context .-> C
    H[(Session History)] -. on-demand recall .-> C
    S[(Approved Skills)] -. procedural guidance .-> C
    T[[Tools / Workers]] <--> E
    EL[(Evidence Ledger)] --> R
    EL --> V

    classDef entry fill:#111827,color:#fff,stroke:#111827,stroke-width:2px;
    classDef stage fill:#EEF2FF,color:#312E81,stroke:#6366F1,stroke-width:1.5px;
    classDef decision fill:#FFF7ED,color:#9A3412,stroke:#F97316,stroke-width:2px;
    classDef success fill:#ECFDF5,color:#065F46,stroke:#10B981,stroke-width:2px;
    classDef failure fill:#FEF2F2,color:#991B1B,stroke:#EF4444,stroke-width:2px;
    classDef store fill:#F8FAFC,color:#334155,stroke:#94A3B8,stroke-dasharray: 5 4;

    class U entry;
    class C,P,E,R,FIX,RP stage;
    class V decision;
    class DONE success;
    class FAIL failure;
    class M,H,S,EL store;
~~~

Planner 将研究问题拆解为 DAG 任务图，Worker 在隔离状态中串行或并行执行，Coordinator 确定性归并结果；Reflection、缺口分析和矛盾检测用于修复局部失败，避免一次模型异常导致整条研究链路失效。

### 3. 全量 Evidence Card 上下文工程

系统不精选或丢弃证据，而是将全部原始证据写入不可变 Evidence Ledger，再为每条证据生成可追溯的 Evidence Card：

- 原始证据完整保留，压缩卡始终回指稳定 evidence_id；
- 按报告章节路由相关 Card，避免每章重复注入全部原文；
- 章节超预算时执行分批 Map、Digest 和递归 Reduce；
- 阶段预算为报告和验证预留 Token；
- 全量证据附录与覆盖门禁确保任何证据都不会被静默遗漏。

| 上下文工程指标 | 优化效果 |
|---|---:|
| 累计上下文暴露量 | **↓ 93.58%** |
| 跨章节重复注入 | **↓ 80.00%** |
| 单章节最大证据上下文 | **↓ 97.48%** |
| 真实链路 LLM Token | **↓ 19.23%** |
| 全量证据覆盖率 | **100%** |

> 固定条件：58 条证据、5 个章节，原始证据完整保留。详见 [上下文冗余优化评测报告](docs/CONTEXT_REDUNDANCY_EVALUATION_REPORT.md)。

### 4. 持久化与全链路可观测

- SQLite 持久化 Session、Message、Run、Plan、Task、ToolCall、Evidence、Contract、Checkpoint、Memory、Skill 与审计记录；
- SSE 实时推送阶段变化、任务开始、工具输出、证据入账、报告生成和验证结果；
- “白板”按时间线展示每轮对话、AI 回复、Run 事件、工具参数与完整输出；
- 服务重启后可从 Checkpoint 恢复，并通过 Durable Event Replay 补齐前端进度；
- Evidence Ledger 打通“检索来源 → 工具调用 → 任务 → 报告引用 → 验证门禁”证据链。

### 5. 受控自进化

系统从验证通过的高质量 Run 轨迹中蒸馏 Skill Candidate，形成受控演进闭环：

~~~mermaid
flowchart LR
    A([Verified<br/>Trajectory]) --> B[Skill<br/>Distillation]
    B --> C{Lint & Safety}
    C -->|Reject| X([Rejected])
    C -->|Pass| D[Offline<br/>Evaluation]
    D -->|Regression| X
    D -->|Passed| E{Human<br/>Promote Gate}
    E -->|Hold| H([Candidate])
    E -->|Approve| F[(Versioned<br/>Skill Registry)]
    F --> G[Progressive Load]
    G --> I[Runtime Feedback]
    I -. new verified trajectory .-> A
    F -->|Rollback| PREV[Previous Version]

    classDef source fill:#111827,color:#fff,stroke:#111827;
    classDef process fill:#EEF2FF,color:#312E81,stroke:#6366F1;
    classDef gate fill:#FFF7ED,color:#9A3412,stroke:#F97316,stroke-width:2px;
    classDef success fill:#ECFDF5,color:#065F46,stroke:#10B981;
    classDef reject fill:#FEF2F2,color:#991B1B,stroke:#EF4444;
    classDef store fill:#F5F3FF,color:#5B21B6,stroke:#8B5CF6;

    class A source;
    class B,D,G,I,PREV process;
    class C,E gate;
    class F store;
    class H success;
    class X reject;
~~~

候选 Skill 在通过安全检查和离线评测前不会进入运行链；启用必须经过人工 Promote，支持版本管理和一键回滚。Skill 只沉淀程序性经验，不会扩大当前 Run 的信息源和工具权限。

### 6. Memory 与长会话上下文

- 持久化 Curated Memory 仅保存跨 Session 有价值的原子偏好、事实、决策或经验；
- 会话历史、Session Summary 与 FTS5 Episodic Recall 按需加载，不与长期 Memory 混为一体；
- Query Resolver 支持代词、省略和多轮追问消歧；
- Memory 有来源引用、容量上限、生命周期和人工确认门禁；
- 完整 AI 回复、研究报告、推理文本和研究证据不会被错误写入长期 Memory。

### 7. Agent 评测与完成门禁

Completion Contract 对每个报告执行 8 项强制检查：

| 证据与主张 | 报告与来源 |
|---|---|
| Citation Integrity | Report Consistency |
| Claim Support | Required Sections |
| Evidence Card Coverage | Source Diversity |
| Minimum Evidence | Source Match |

评测系统直接读取持久化运行轨迹，统计：

- Verified / First-pass Completion Rate；
- Required Contract Pass Rate 与 Citation Validity；
- Recovery、Replan、Checkpoint Integrity 和重复副作用率；
- P50/P95 Latency、Tokens per Verified Run、Tool Calls per Run；
- Prefix Cache Hit Rate；
- 提供 Gold Evidence 时的 Precision@K、Recall@K、MRR、nDCG@K；
- 提供人工或独立 Judge 标签时的 Semantic Claim Support、Report Quality 与 Honest Failure Rate。

---

## 系统架构

单机模式（默认，SQLite + 进程内调度）与平台模式（P0，PostgreSQL + Redis + 独立 worker）共用同一套 Harness 语义：

~~~mermaid
flowchart TB
    subgraph UI["Experience Layer · React Console"]
        direction LR
        CANVAS[Research Canvas]
        STREAM[Realtime SSE]
        WHITE[Whiteboard]
        MEMUI[Memory]
        SKILLUI[Skills]
    end

    subgraph API["Control Plane · FastAPI ×N"]
        direction LR
        SESSION[Session API]
        SCHED[Run Scheduler<br/>唤醒信号投递]
        EVENT[Event Stream<br/>Redis 订阅 + DB 重放]
        EVALAPI[Evaluation API]
    end

    subgraph WORKERS["Worker 进程 ×N（P0）"]
        direction LR
        QUEUE[Redis Streams 消费]
        SCAN[过期租约扫描<br/>崩溃自动接管]
        EXEC[HarnessRuntime 执行]
    end

    subgraph PLATFORM["平台设施（P1/P2）"]
        direction LR
        GATEWAY[LLM 网关<br/>failover/限流/计量]
        OBS[Prometheus + OTel + Grafana]
        TENANT[多租户<br/>tenants/api_keys/tenant_id]
        SCHOLAR[学术检索源<br/>OpenAlex + arXiv]
    end

    subgraph HARNESS["Agent Harness Runtime"]
        direction TB
        CORE[State Machine · Lease · Budget]
        RELIABILITY[Checkpoint · Recovery · Idempotency]
        GOVERNANCE[Completion Contract · Source Policy]
        OBS[Evidence Ledger · Audit Trail]
        CORE --> RELIABILITY --> GOVERNANCE --> OBS
    end

    subgraph INTELLIGENCE["Agent Intelligence"]
        direction LR
        subgraph RESEARCH["Multi-Agent Research"]
            PLAN[Planner]
            WORK[Worker DAG]
            REFLECT[Reflection]
            REPORT[Reporter]
            PLAN --> WORK --> REFLECT --> REPORT
            REFLECT -. retry / replan .-> PLAN
        end
        subgraph CONTEXT["Context & Evolution"]
            CTX[Context Builder]
            MEMORY[Curated Memory]
            RECALL[FTS5 Recall]
            SKILLS[Skill Registry]
            EVOLVE[Distill · Eval · Promote]
            MEMORY --> CTX
            RECALL --> CTX
            SKILLS --> CTX
            EVOLVE --> SKILLS
        end
    end

    subgraph DATA["Evidence & Data Plane"]
        direction LR
        WEB[(Tavily Web)]
        GRAPH[(Neo4j GraphRAG)]
        SQL[(SQLite)]
        ART[(Artifact Store)]
        VECTOR[(Faiss / Embeddings)]
    end

    UI -->|REST / SSE| API
    API --> HARNESS
    HARNESS --> RESEARCH
    HARNESS --> CONTEXT
    WORK --> WEB
    WORK --> GRAPH
    REPORT --> ART
    HARNESS <--> SQL
    GRAPH <--> VECTOR
    REPORT --> OBS
    OBS --> GOVERNANCE

    classDef layer fill:#0F172A,color:#fff,stroke:#0F172A,stroke-width:2px;
    classDef control fill:#E0F2FE,color:#075985,stroke:#0EA5E9;
    classDef runtime fill:#EEF2FF,color:#312E81,stroke:#6366F1,stroke-width:1.5px;
    classDef agent fill:#F5F3FF,color:#5B21B6,stroke:#8B5CF6;
    classDef data fill:#ECFDF5,color:#065F46,stroke:#10B981;

    class CANVAS,STREAM,WHITE,MEMUI,SKILLUI layer;
    class SESSION,SCHED,EVENT,EVALAPI control;
    class CORE,RELIABILITY,GOVERNANCE,OBS runtime;
    class PLAN,WORK,REFLECT,REPORT,CTX,MEMORY,RECALL,SKILLS,EVOLVE agent;
    class WEB,GRAPH,SQL,ART,VECTOR data;
~~~

## 信息源与研究工作流

每个 Run 冻结一种信息源，禁止执行过程中跨源污染：

- **graphrag**：私域文档、Chunk 向量检索、实体关系图和 Community 全局检索；
- **web**：Tavily 联网搜索；
- Memory 与历史对话仅用于理解问题，不可充当本轮研究证据。

Harness 实际支持唯一工作流（`WorkflowMode`，见 `src/deepresearch_agent/harness/contracts.py`）：

| 工作流 | 适用场景 |
|---|---|
| plan_execute_report（coreact） | BriefBuilder 简报规划 → Controller 步级决策 → ActionRunner 隔离执行 → 步级验证与有限修正 → 长报告生成与完成验证 |

> T-017（T-407/T-408）：独立 deep_research 引擎、staged 整批管线与 reflection 流程已退役并物理删除。历史 deep_research Run 只读可读；存量活动旧引擎 Run 显式拒绝（LEGACY_WORKFLOW_UNSUPPORTED，人工处理），不静默换引擎。

## 技术栈

| 层 | 技术 |
|---|---|
| Agent / LLM | LangChain、LangGraph、OpenAI-compatible API |
| Harness | Python、Pydantic、异步状态机、Durable Event、Checkpoint |
| Retrieval | Tavily、Neo4j 5.22、APOC、GDS、Faiss、SentenceTransformers |
| Backend | FastAPI、SSE、SQLAlchemy、SQLite、Alembic |
| Frontend | React 18、TypeScript、Vite、TanStack Query、Motion |
| Deployment | Docker Compose、Nginx、Uvicorn |

---

## 在线站点

项目已部署为可运行网站：**https://deepresearch-harness-qwzy.zai.site**

- 单机模式运行真实前后端：React 研究画布 + FastAPI API + 进程内 worker + SQLite
- 公开实例未配置模型/检索密钥——界面、会话与白板可用，发起研究任务会诚实失败；完整能力请本地部署并配置密钥
- 云端存储为临时存储（实例重建即清空）；`/api/v1/ready` 暴露 readiness，`/api/v1/health` 暴露组件状态
- 五条完整运行流程的文字版见 [docs/RUN-FLOWS.md](docs/RUN-FLOWS.md)

运维 runbook 索引见 [docs/runbooks/README.md](docs/runbooks/README.md)(备份恢复/迁移发布/告警响应/成本天花板)。

---

## 快速启动

### 1. 环境要求

- Python 3.10 或 3.11
- Node.js 20 LTS
- Docker Desktop

### 2. 配置

~~~bash
cp .env.example .env
~~~

主要配置项：

| 配置 | 说明 |
|---|---|
| OPENAI_API_KEY | OpenAI 兼容 API 密钥 |
| OPENAI_BASE_URL | 模型服务地址 |
| OPENAI_LLM_MODEL | 生成模型 |
| OPENAI_EMBEDDINGS_MODEL | 向量模型 |
| TAVILY_API_KEY | Web 联网模式密钥 |
| NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD | 私域 GraphRAG |
| APP_DATABASE_URL | SQLite（默认）或 `postgresql+asyncpg://...`（平台模式） |
| REDIS_URL | 平台模式队列/SSE 扇出；为空走进程内实现（单机兼容） |
| RUN_LEASE_SECONDS / RECOVERY_SCAN_SECONDS | 租约 TTL 与接管扫描间隔（默认 30/15s，接管窗口上界 45s） |
| LLM_FALLBACK_BASE_URL / LLM_FALLBACK_API_KEY | 备用 provider；配置后自动启用 LLM 网关 failover |
| TENANT_AUTH_REQUIRED | 多租户强制认证（有 api_keys 时自动启用） |
| OPENALEX_MAILTO | 学术检索 polite pool 标识（source_mode=scholar，免 key） |
| RUN_MAX_LLM_TOKENS / RUN_WALL_TIME_SECONDS | Harness 预算 |
| FRONTEND_PORT | 前端端口，默认 5173 |

完整选项见 [.env.example](.env.example)。

### 3. Docker 一键启动

~~~powershell
docker compose up -d --build
~~~

访问：

- 前端：http://127.0.0.1:5173
- API：http://127.0.0.1:8000
- Neo4j Browser：http://127.0.0.1:7474

compose 默认即平台模式拓扑（postgres + redis + backend + worker + neo4j + frontend）：Run 执行在 worker 进程，事件经 Redis 扇出，任何 API 实例都能服务任意 Run 的 SSE。多实例扩容与崩溃接管演示：

~~~bash
# 国内网络建议：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose up -d --build --scale backend=2 --scale worker=2
docker compose kill worker        # 杀掉一个 worker：进行中的 Run 在 ≤60s 内被接管完成
curl -N http://127.0.0.1:8000/api/v1/runs/{run_id}/events   # 8000/8001 两个实例看到同一事件流
~~~

若前端端口冲突：

~~~powershell
$env:FRONTEND_PORT = "5174"
docker compose up -d --build
~~~

### 4. 本地开发

~~~powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

$env:PYTHONPATH = "$PWD\src"
python -m alembic upgrade head
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
~~~

另开终端启动前端：

~~~powershell
cd frontend
npm install
npm run dev
~~~

也可使用本地脚本：

~~~powershell
.\scripts\start-local.ps1
.\scripts\stop-local.ps1
~~~

### 5. 可选 Kafka 传输层（T-010）

Run 的唤醒信号队列支持两种可替换实现，由环境变量选择（Kafka 优先于 Redis）：

| 后端 | 触发条件 | 消费语义 |
|---|---|---|
| Redis Streams（默认平台模式） | `REDIS_URL` 非空 | 读出即 ACK（at-most-once） |
| Kafka（可选） | `KAFKA_BOOTSTRAP_SERVERS` 非空 | **执行后提交 offset（at-least-once）** |

两种后端下可靠性语义一致：数据库是事实源，租约条件 UPDATE 是唯一互斥量，重复投递/信号丢失由过期租约扫描兜底。Kafka 侧的实现细节：分区键 = `run_id`（同 Run 信号保序）、幂等 producer（`acks=all` + `enable_idempotence`）、按分区水位线单调提交 offset（乱序完成的任务不会越位提交）、rebalance 时清理本地水位线并接受重投。

启用（Compose 演示，需先遵守项目 Docker 使用约束）：

~~~bash
# .env 中设置 KAFKA_BOOTSTRAP_SERVERS=kafka:9092（REDIS_URL 可保留用于 SSE 实时扇出）
docker compose --profile kafka up -d --build
~~~

事件流分发（T-011）：`dra-events` topic 以 key=run_id 承载全量事件（durable-first 语义不变——事件先落库），`FanoutChain` 组合 Redis（per-Run 实时）+ Kafka（全量流）双通道，单通道失败只计数；`AuditEventConsumer` 演示独立消费组（审计/评测/数仓场景）。

定位说明：这是**传输层可替换性的能力验证**，不是生产必选项——是否引入 Kafka 取决于流量与消费者规模，取舍记录见 `docs/specs/production-readiness/design.md`。CI 的 `kafka-compat` 作业用真实 broker 跑队列回归（`tests/harness/test_run_queue.py`，本地无 broker 时自动跳过）。

---

## 测试与评测

### 自动化回归

~~~powershell
python -m pytest -q
cd frontend
npm run build
~~~

当前完整后端回归：**218 个用例**（`python -m pytest tests/ --collect-only -q | tail -1` 统计口径；实际通过数以最近一次 CI 运行为准）；前端 TypeScript 类型检查与生产构建通过。

### 系统量化评测

~~~powershell
python scripts/evaluate_runs.py --output output/evaluation/summary.json
python scripts/evaluate_runs.py --labels evals/system/cases.json --retrieval-k 10
~~~

评测原则：

- 确定性指标直接从持久化 Run 轨迹计算；
- Claim Support 的规则结果只作为代理值；
- 语义事实支持率与报告质量必须来自人工或独立 Judge 标签；
- 建议固定模型、温度、预算和数据版本，并对非确定性案例重复运行。

详细说明见 [系统评测文档](evals/system/README.md)。

---

## 常用 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/v1/health | 系统与依赖健康检查 |
| POST/GET | /api/v1/sessions | 创建或查询会话 |
| POST | /api/v1/sessions/{id}/messages | 发送消息并创建 Run |
| GET | /api/v1/sessions/{id}/whiteboard | 全链路白板日志 |
| GET | /api/v1/runs/{id} | Run 状态、阶段与预算 |
| GET | /api/v1/runs/{id}/events | Durable SSE 事件流 |
| GET | /api/v1/runs/{id}/evidence | Evidence Ledger |
| GET | /api/v1/runs/{id}/report | 报告与 Contract 结果 |
| POST | /api/v1/runs/{id}/pause | 在安全阶段边界暂停并保存 Checkpoint |
| POST | /api/v1/runs/{id}/cancel | 取消任务 |
| POST | /api/v1/runs/{id}/resume | 从 Checkpoint 恢复同一 Run |
| GET/PATCH | /api/v1/memories | Curated Memory 管理 |
| GET/POST | /api/v1/skills | Skill 查看、评测、启用与回滚 |
| GET | /api/v1/evaluations/runs/{run_id} | 单 Run 评测 |
| GET | /api/v1/evaluations/summary | 聚合评测指标 |

---

## 项目结构

~~~text
.
├── backend/                         # FastAPI API 与 Run 调度
├── frontend/                        # React 研究画板、白板、Memory、Skills
├── src/deepresearch_agent/
│   ├── harness/                     # Runtime、状态机、预算、恢复、Contract
│   ├── agents/multi_agent/          # Planner、Worker、Reflection、Reporter
│   ├── context/                     # 上下文块、预算与可信边界
│   ├── memory/                      # Curated Memory、摘要与历史召回
│   ├── evolution/                   # Skill 蒸馏、评测、Promote、回滚
│   ├── evaluation/                  # Agent 与检索评测
│   ├── persistence/                 # SQLite、Repository、Artifact、Migration
│   ├── retrieval/                   # GraphRAG / Web Provider
│   ├── graph/                       # 知识图谱构建与 Community
│   └── search/                      # DeepResearch 检索工具
├── tests/                           # Harness、API、Memory、Evolution、E2E
├── evals/                           # Gold Evidence 与 Judge 标签
├── docs/                            # 设计、上下文工程与评测报告
├── skills/                          # 版本化 Skill 内容
├── scripts/                         # 启停、评测和真实链路脚本
├── data/                            # SQLite 与 Artifact 运行数据
└── docker-compose.yaml
~~~

## 设计原则

- **Verified Completion over plausible output**：通过验证才算完成；
- **Evidence first**：研究结论必须能回溯到证据；
- **Durable by default**：状态、事件和副作用默认持久化；
- **Bounded context**：每个阶段都有明确上下文和预算边界；
- **Controlled evolution**：经验可以演进，但必须经过评测和人工门禁；
- **Honest failure**：证据、预算或权限不足时明确失败，不伪造成功。

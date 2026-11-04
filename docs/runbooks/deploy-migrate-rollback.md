# 部署、迁移与回滚 Runbook(DeepResearch Agent Harness)

> 配套:`docs/runbooks/backup-restore.md`(备份恢复)、`docs/specs/production-readiness/design.md`(架构决策)。
> RPO 24h / RTO 60m 为作品集级目标,非商业 SLA。

## 1. Schema 迁移生命周期(T-005)

**原则:schema 只经单独的迁移步骤演进;API/worker 启动不执行竞争性 DDL。**

| 环境 | 启动期 DDL | 迁移方式 |
|---|---|---|
| 本地开发/测试(默认) | `create_schema` 引导(`SCHEMA_BOOTSTRAP_ON_STARTUP=true`) | 首次启动自动建表;需迁移历史时跑 migrate.py |
| 平台部署(compose/CI) | `SCHEMA_BOOTSTRAP_ON_STARTUP=false` | backend 与 worker 各自启动前先跑迁移(migrate.py 带锁竞争重试;两者可能并发,重试兜底) |

### 迁移步骤

~~~bash
# compose 平台模式:worker 容器 command 已内置;独立执行:
PYTHONPATH=src:. python -m alembic upgrade head
# 或统一入口(重试 5 次,间隔 2s,防多副本锁撞车):
python scripts/migrate.py
~~~

### 兼容窗口(expand → deploy → contract)

- **扩展(expand)**:新迁移只加表/加列(server_default 回填)→ 新旧代码都能跑(`0002/0005/0007` 均为此模式,列存在性条件化)
- **收缩(contract)**:删列/改约束必须等新代码全量部署后才可发布;引用旧列的代码先移除
- **已知不可逆点**:`20261025_0006`(source_mode CHECK 收缩至三值)的 downgrade 会**清空 scholar 行**——迁移链唯一数据丢失型回退,执行前必须备份

### 启动 fail-fast 语义

`SCHEMA_BOOTSTRAP_ON_STARTUP=false` 时若迁移未执行,服务启动会在首次依赖查询处**明确失败**(OperationalError)——诚实失败,不静默带病运行。回滚迁移失败时禁止带病启动。

## 2. 发布流程

~~~bash
# 1. 备份(见 backup-restore.md;迁移前必须)
python scripts/backup_restore.py backup --archive backups/pre-deploy-$(date +%Y%m%d-%H%M%S).zip --app-database-url "$APP_DATABASE_URL"
# 2. 迁移
python scripts/migrate.py
# 3. 滚动更新服务(compose 平台模式)
docker compose up -d --build backend worker
# 4. 冒烟:health + 一次真实 Run
curl -s http://127.0.0.1:8000/api/v1/health
~~~

## 3. 回滚决策树

| 情形 | 动作 |
|---|---|
| 新代码 bug,迁移仅 expand(加列) | 直接回滚镜像;旧代码兼容新列 |
| 新代码 bug,迁移含 contract(删列/改约束) | **恢复备份**(backup-restore runbook),不可仅回滚镜像 |
| 迁移本身失败 | 修复迁移脚本前向修复;或恢复备份后重发布 |
| 迁移成功但服务反复崩溃 | 检查 `SCHEMA_BOOTSTRAP_ON_STARTUP` 与迁移状态(`alembic current`)是否匹配 |

> 迁移回滚(`alembic downgrade`)仅用于**开发环境**;生产以"恢复备份"为回滚手段——`0006` 的 downgrade 会清 scholar 数据。

## 4. 密钥与配置变更

- 密钥经环境变量/部署平台 Secret 注入;轮换 = 更新 Secret → 滚动重启
- `.env` 不入库(`.gitignore`);`scan_secrets.py` 可作提交前检查

## 5. 告警响应(T-007)

告警规则:`observability/alert-rules.yml`(Prometheus rule_files 已挂载)。按 severity 响应:

| 告警 | severity | 第一响应 |
|---|---|---|
| RunStuckQueued | warning | 查 worker 存活(`docker compose ps worker`)与 Redis/Kafka 连通 |
| EnqueueFailures | warning | 队列故障;恢复后 queued Run 自动收敛(无需人工重投) |
| WorkerConsumeReconnects | warning | Redis/Kafka 抖动;持续 >15m 检查网络与持久化配置 |
| EventFanoutFailures | info | SSE 实时推送退化,DB 重放兜底;恢复 Redis 即可 |
| WorkerRunFailures | warning | 查最近 run.failed 事件的 error_code |
| LlmRateLimitedBurst | info | 评估 LLM_MAX_CONCURRENT_CALLS 与 provider 容量 |

**模型分层路由(T-009)**:`LLM_TIER_ROUTING_ENABLED`(默认 true)。派生规则(纯函数,`src/deepresearch_agent/models/model_tier.py`):详细/长文档/min_evidence≥3 → complex;默认 brief → simple;显式 `model_tier` 覆盖优先。模型 ID 配置:`LLM_TIER_SIMPLE/STANDARD/COMPLEX_MODEL`(空=回落 `OPENAI_LLM_MODEL`)。每次路由的 tier/reason/model 写入 run.model_snapshot_json,可审计;预算保护由 T-007 日预算兜底。

**成本天花板(T-007)**:`LLM_DAILY_TOKEN_CEILING`(当日 output_tokens;0=不启用)。超限后:
- 新 Run 被拒(LLM_RATE_LIMITED,429 语义,幂等重发安全)
- `/api/v1/ready` 返回 503(编排器摘流量)
- 解除:次日 UTC 0 点自动重置,或管理员调高/置 0 后滚动重启

readiness 与 health 的分工:`/ready` 给编排器(硬依赖+成本门禁,503=摘流量);`/health` 给人(全组件状态面板)。

## 6. 故障应急入口

- Redis/队列故障 → `docs/specs/production-readiness/design.md` 7.2(T-003 自愈语义)
- worker 崩溃接管 → README「多实例扩容与崩溃接管演示」
- 备份恢复 → `docs/runbooks/backup-restore.md`

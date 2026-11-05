# 运维 Runbook 索引(DeepResearch Agent Harness)

> 面向值班与交接。目标口径:RPO 24h / RTO 60m(作品集级,非商业 SLA)。

| 文档 | 覆盖 | 何时翻 |
|---|---|---|
| [backup-restore.md](backup-restore.md) | 每日备份、恢复步骤、Neo4j/PG dump、保留策略、演练 | 数据丢失/演练/备份告警 |
| [deploy-migrate-rollback.md](deploy-migrate-rollback.md) | 发布流程、schema 迁移、expand/contract、回滚决策树、告警响应、成本天花板 | 发版/迁移失败/告警第一响应 |
| [../specs/production-readiness/design.md](../specs/production-readiness/design.md) | 架构决策记录(Kafka 可选传输/Outbox 取舍/故障语义) | 深挖"为什么这样设计" |
| [../acceptance/acceptance-matrix.md](../acceptance/acceptance-matrix.md) | 87 个 MUST 机制与验收证据索引 | 交接与能力盘点 |

## 快速命令

~~~bash
python scripts/migrate.py                                    # schema 迁移(带重试)
python scripts/backup_restore.py backup --archive backups/x.zip [--app-database-url ...]
python scripts/backup_restore.py prune --backup-dir backups  # 保留 30 天/最少 3 份
python scripts/backup_drill.py --target sqlite               # 演练(零依赖)
curl -s http://127.0.0.1:8000/api/v1/ready                   # 编排器 readiness
curl -s http://127.0.0.1:8000/api/v1/health                  # 全组件状态
curl -s http://127.0.0.1:8000/api/v1/metrics                 # Prometheus 抓取
~~~

## 第一响应速查

| 症状 | 先看 | 文档 |
|---|---|---|
| Run 卡 queued | worker 存活 + 队列连通 | deploy-migrate-rollback.md §5 |
| RunStuckQueued / EnqueueFailures 告警 | 队列后端状态 | 同上 |
| 服务启动失败(缺表) | SCHEMA_BOOTSTRAP_ON_STARTUP 与 alembic current | deploy-migrate-rollback.md §1 |
| LLM 429「日预算已耗尽」 | LLM_DAILY_TOKEN_CEILING 与 llm_calls 当日聚合 | deploy-migrate-rollback.md §5 |
| 数据误删/迁移事故 | 备份恢复 | backup-restore.md §4 |

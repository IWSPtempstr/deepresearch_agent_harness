# 备份与恢复 Runbook(DeepResearch Agent Harness)

> 作品集级目标:**RPO 24 小时 / RTO 60 分钟**。这是备份恢复演练目标,不是商业 SLA;公开展示不提供在线服务。

## 1. 数据清单(备份覆盖什么)

| 数据 | 位置 | 备份方式 |
|---|---|---|
| 结构化数据(22 张表) | `data/app.db`(SQLite 单机)或 PostgreSQL 卷(平台) | SQLite online backup / `pg_dump --format=custom` |
| 研究证据与报告 | `data/artifacts/`(bind mount) | 目录打包(manifest 哈希) |
| 知识图谱 | Neo4j `neo4j_data` 卷 | `neo4j-admin database dump` → `data/neo4j.dump` |
| 技能与文件 | `skills/`、`files/`、`cache/` | 目录打包 |
| Redis(仅唤醒信号/SSE 扇出) | `redis_data` 卷 | **不备份**——DB 是事实源,可重建 |

## 2. 每日备份

### SQLite 单机模式

~~~bash
python scripts/backup_restore.py backup --archive backups/local-$(date +%Y%m%d-%H%M%S).zip
~~~

### 平台模式(PostgreSQL)

~~~bash
python scripts/backup_restore.py backup \
  --archive backups/platform-$(date +%Y%m%d-%H%M%S).zip \
  --app-database-url "$APP_DATABASE_URL"
~~~

cron 示例(每日 03:00):

~~~cron
0 3 * * * cd /opt/deepresearch_agent_harness && python scripts/backup_restore.py backup --archive backups/platform-$(date +\%Y\%m\%d-\%H\%M\%S).zip --app-database-url "$APP_DATABASE_URL" >> backups/backup.log 2>&1
~~~

### Neo4j 图谱(每周或重大变更前)

~~~bash
docker compose stop neo4j
docker compose run --rm neo4j neo4j-admin database dump neo4j --to-path=/backups
docker compose cp neo4j:/backups/neo4j.dump data/neo4j.dump
docker compose start neo4j
python scripts/backup_restore.py backup --archive backups/full-$(date +%Y%m%d-%H%M%S).zip
~~~

> 本机 Docker 操作按项目约束执行前须获得用户同意。

## 3. 保留策略

~~~bash
python scripts/backup_restore.py prune --backup-dir backups --keep-days 30 --keep-min 3
~~~

对齐已确认决策:运行数据保留 30 天,至少保留 3 份。

## 4. 恢复步骤(RTO 60 分钟内)

1. **停止写入**:停 backend/worker(`docker compose stop backend worker` 或本地脚本)
2. **校验归档**:恢复前 manifest 全量 sha256 校验(restore 自动执行,失败即中止)
3. **执行恢复**:

~~~bash
python scripts/backup_restore.py restore --archive backups/<archive>.zip --confirm
~~~

- 当前 DB 自动安全副本:`data/app.pre-restore-<时间戳>.db`
- PostgreSQL:`restore` 会把 `postgres.dump` 落到 `data/`;在服务停止窗口执行:

~~~bash
# 注意：+asyncpg 需转为裸 postgresql://（脚本 _pg_dsn 同款转换）
pg_restore --clean --if-exists --dbname "postgresql://...（转换后 DSN）" data/postgres.dump
~~~

- Neo4j(如归档含 dump):

~~~bash
docker compose stop neo4j
docker compose cp data/neo4j.dump neo4j:/backups/neo4j.dump
docker compose run --rm neo4j neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true
docker compose start neo4j
~~~

4. **恢复验证**(必做):

~~~bash
python -c "
import sys; sys.path.insert(0, '.')
from pathlib import Path
from scripts.backup_restore import verify_after_restore
print(verify_after_restore(Path('.')))  # 或传 app_database_url=... 走 PG
"
~~~

核对核心表行数(sessions/runs/messages/run_events/evidence/checkpoints)与 artifact 可读性。

5. **重启服务**并抽查一个历史 Run 的报告与证据引用。

## 5. 演练(每季度或重大变更后)

~~~bash
# SQLite 干跑(零依赖)
python scripts/backup_drill.py --target sqlite --output output/drill.json
# PostgreSQL 演练(dump → pg_restore 到 <db>_restore scratch 库 → 对恢复后库 verify)
PYTHONPATH=src:. python -m scripts.backup_drill --target postgres --dsn "$APP_DATABASE_URL" --output output/drill.json
~~~

> SQLite 演练会在本地 `data/app.db` 建一张 `drill_notes` 演练表并留下 `app.pre-restore-*.db` 安全副本,属预期残留;PG 演练会创建 `<db>_restore` scratch 库。
>
> 演练输出 PASS/FAIL JSON(RTO 计 pg_restore+verify 墙钟,报告含 pg_restore_exercised 披露字段)。CI 的 `backup-restore-drill` 作业每次提交都会执行真实 PG 演练(含真实 pg_restore)。

## 6. 升级/回滚衔接

- 数据库迁移回滚通常不安全:优先「恢复备份 + 旧镜像」,或按 `docs/runbooks/`(T-008)的前向修复流程
- 迁移与兼容窗口策略见 `docs/specs/production-readiness/design.md`(T-005)

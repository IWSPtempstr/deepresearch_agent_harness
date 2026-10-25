# P0 阶段总结：从单机到分布式

> 对应交接方案 P0-1 ~ P0-4。目标：多实例 API + 多 worker 执行 + PostgreSQL 主存 + Redis 队列/扇出 + 崩溃自动接管，且原有 Harness 语义（状态机、Completion Contract、Evidence Card、预算、幂等、断点恢复）零重写。

## 改了什么

| 任务 | 提交 | 核心内容 |
|---|---|---|
| P0-0 准备 | `fcb899d` | torch 惰性导入（平台进程不再被迫装 torch）；docs/ 纳入版本管理；交接方案入库 |
| P0-3a 租约心跳 | `67a5c50` | **修复潜在双执行 bug**：心跳按 TTL/3 同 owner 续约；被抢占即 RunLeaseLost 让位（不写终态） |
| P0-1a 方言中立 | `d22ace1` | `postgresql+asyncpg` 支持；SearchBackend 抽象（FTS5 ↔ pg_trgm）；`postgresql_where` 修复；env.py 驱动映射 |
| P0-1b 迁移与编排 | `9a07de7` | 迁移链方言分派（空库 PG `upgrade head` 20 表 + 4 trgm 索引，可逆）；compose 增加 postgres/redis |
| P0-2 队列化 | `2cf0f77` | RunQueue 抽象（进程内兼容 / Redis Streams）；RunService 队列模式只投递唤醒信号；独立 worker 进程（SIGTERM 宽限→崩溃等价，交给租约接管）；API/worker 分容器 |
| P0-3b 崩溃转移 | `6dfef5a` | worker 内置周期扫描（默认 15s）；过期租约 → interrupted → 重新投递；SIGKILL 端到端接管测试 |
| P0-4 SSE 扇出 | `2aec3b6` | durable-first 不变，落库后 Redis pub/sub 扇出；任意 API 实例可订阅任意 Run；断线重放以 DB 为准 |

## 关键设计决策

1. **流只是唤醒信号，数据库是事实源**（P0-2 选型）：Redis Streams 读出即 ACK（at-most-once），恢复统一走 runs 表状态 + 租约扫描，避免与 ARQ/重投类机制双轨打架。租约条件 UPDATE 是唯一互斥量，重复投递天然安全。
2. **PG 检索不用 tsvector**：中文 trigram 子串匹配语义下，pg_trgm GIN + ILIKE 是 SQLite FTS5 的最近等价物（tsvector 需要额外中文分词扩展）。命中集合一致，仅排序策略不同（已注释）。
3. **优雅关停 ≠ 取消**：worker SIGTERM 给 30s 宽限，超时不写终态直接退出——等价一次崩溃，由租约接管；避免「优雅关停把 Run 误标 CANCELLED 导致无法接管」。
4. **Faiss→pgvector 重估**：Faiss 仅在 `cache_manager`（LLM 响应缓存层），不在检索路径；迁移范围远小于原方案假设，推迟到后续需要共享缓存命中率时再做。

## 数字

- 测试：基线 153 通过 / 2 个预存在失败（`docs/acceptance` 交付文档原作者未入库）→ **P0 后 163 通过 / 3 门控跳过（PG/Redis）/ 同 2 个预存在失败**；新增 13 个分布式语义测试。
- 接管时延（测试配置 TTL=2s）：SIGKILL → 租约过期 → interrupted → 接管完成，端到端 **≈6s**；生产默认 TTL=30s + 扫描 15s，窗口上界 45s < 验收要求的 60s。
- PG 实测（pgvector/pgvector:pg17）：`alembic upgrade head` 空库建 20 表 + 4 trgm 索引；`downgrade base` 可逆；迁移产出的 schema 通过全部仓储行为断言（CJK 检索 / 租约抢占 / 外键）。

## 待人工验证（低配环境未跑）

compose 全拓扑（`backend×2 + worker×2 + postgres + redis + neo4j`）在本机未实际拉起。验证命令：

~~~bash
cp .env.example .env   # 填 POSTGRES_PASSWORD / NEO4J_PASSWORD
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  docker compose up -d --build --scale backend=2 --scale worker=2
# 杀掉任一 worker：docker compose kill worker（默认杀 1 个）
docker compose kill worker  # 进行中的 Run 应在 ≤60s 内被接管完成
# SSE 双实例：curl -N http://127.0.0.1:8000/api/v1/runs/{id}/events 与 :8001 应看到同一事件流
~~~

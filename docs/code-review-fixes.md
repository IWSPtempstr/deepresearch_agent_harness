# Code Review 修复记录（2026-10-26）

> 评审范围：平台化改造 fcb899d..4fb0ce7（14 提交）。本页记录评审发现与修复；P0/P1/P2 阶段总结见同目录。

## 已修复（六项，每项一提交）

| 编号 | 提交 | 问题 | 修复 |
|---|---|---|---|
| H1 | `80aeb4d` | 迁移 0006 在**有数据**的 SQLite 库上必然失败：batch 重建表遇 `foreign_keys=ON` 报外键约束失败（此前迁移验证全用空库未暴露） | env.py 迁移期 `PRAGMA foreign_keys=OFF`（batch 模式官方要求，须在事务前设置）；回归测试用播种六表数据的库验证 upgrade 保全数据 + 约束放宽 + 往返 |
| H3 | `adcb79c` | 租户中间件无路径豁免：首个 api_key 签发后 healthcheck（无 key）判容器 unhealthy → frontend 永不启动；Prometheus 抓 /metrics 401 | `/api/v1/health`、`/api/v1/metrics` 豁免（无租户数据面）；业务面仍强制 401（测试断言） |
| H2 | `02e5ada` | EventBus 扇出无容错：Redis 抖动异常传播进状态机，执行中 Run 被误标 FAILED | 扇出 best-effort（已 durable 落库），失败仅记 `dra_event_fanout_failures_total`；SSE 有 DB 重放兜底 |
| M1 | `cfb4084` | `_ensure_additive_columns` 字典 sessions 重复键被静默覆盖，旧库 memory_snapshot 自愈列丢失 | 合并单键并纳入 tenant_id |
| M2 | `cfb4084` | sessions PATCH/DELETE/whiteboard 无归属校验：跨租户改名/归档/软删除 | 统一 `_assert_session_tenant`（读路径同步复用）；测试补写路径越权用例 |
| M3 | `6c90290` | memories 无 tenant_id：user/project/domain 作用域记忆全局召回，跨租户记忆泄漏 | 迁移 0007 加列+索引；list_available/list_curated/search/API 直查全过滤；PATCH/DELETE 补归属校验。坑：`index=True` 的自动命名索引使按名判断失准，改为按「列上全部索引」 |
| M4 | `6c90290` | llm_calls.tenant_id 恒为 default，per-tenant token 面板失真 | 网关 _meter 传当前租户；测试断言 tenant-xyz 落库 |
| M6 | `c8b3622` | CI 用完整 requirements.txt（textract/hanlp/torch 重依赖）安装易失败 | requirements-ci.txt 最小集；fresh venv 实证：安装成功 + 194 用例通过 + 评测门通过 |

## 二轮修复（150b3ad，全部离线闭环）

原遗留 1/2/4/6/7/8（部分）已修复：

1. **context 构建租约保护**：build 调用纳入 `_run_with_heartbeat`（构建期续约，被抢占取消让位）。
2. **SSE 终态兜底**：心跳超时路径查库，终态则 DB 补读后返回；心跳间隔参数化（heartbeat_seconds）。
4. **流式网关**：`FailoverChatOpenAI._astream` 补齐限流/failover/健康度/计量（token 取末尾 usage_metadata）。
6. **RUN_TOTAL 口径**：仅终态计数，非终态让位不再产生伪维度。
7. **worker 异常现场**：`execute_safely` 抽出，logger.exception + `dra_worker_run_failures_total`。
8. **`_escape_like` 纯函数单测补齐**（`%`/`_`/反斜杠）。

## 三轮修复（4f6423f，前端接入 X-API-Key）

原遗留 3（前端）已修复：方案经确认选 fetch 流重写——SSE 消费仅 useRunEvents 一处且重连本为手写游标续传，替换 EventSource 零功能损失、凭据不进 URL。改动：client.ts 凭据存取 + 全请求头注入 + 401 广播；useRunEvents 改 fetch + ReadableStream 解析（终态/断线语义不变）；ApiKeyDialog 全局入口（401 自动弹出）。验证：tsc + vite build 通过（浏览器联调需起服务，属部署验证）。

## 未修复（已知遗留，均需容器）

1. **PG 侧 0004-0007 未实测 + ILIKE 真实行为**：需要 PostgreSQL 容器（SQLite 全链 7/7/7 往返已验，迁移守卫为方言通用写法）。

## 数字

- 二轮修复后：194 → **200 通过**（+6：context 构建续约/抢占 2、SSE 兜底 1、流式网关 1、worker 现场 1、转义 1）；三轮为前端功能项（tsc + vite build 验证，pytest 数不变）。
- 评审后测试：188 → **194 通过**（+6：迁移回归 2、运维豁免 1、扇出容错 1、记忆隔离 1、租户计量 1；M2 用例并入既有隔离测试）；仍 3 门控跳过、2 预存在失败（原作者仓库缺失 acceptance 交付文档）。
- 迁移链：7 个迁移，SQLite upgrade(7)/downgrade(7)/re-upgrade(7) 全通过；有数据库升级专项测试覆盖。
- 依赖：fresh venv 从 requirements-ci.txt 冷启动全量测试通过。

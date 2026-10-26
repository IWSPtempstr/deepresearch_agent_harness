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

## 未修复（已知遗留，按优先级）

1. **context building 阶段无心跳续约**：`_run_with_heartbeat` 只包 plan/execute/report；`context_builder.build` 超 TTL（30s）租约可过期被抢占。当前 build 为纯 DB 读秒级完成，暂无实际风险；若未来引入远程上下文源需补。
2. **SSE fanout 丢终态事件 → 流悬挂**：Redis pub/sub at-most-once，终态事件丢失后流只发 heartbeat。客户端可自行断开；闭环需在心跳超时路径顺带查库。
3. **前端未接入 X-API-Key**：平台模式启用租户后 React 界面不可用（H3 已保住 health/metrics；H3 提交正文有注明）。
4. **流式 LLM 调用绕过网关**：`FailoverChatOpenAI` 未覆写 `_astream`（failover/限流/计量不覆盖流式）。当前 Run 执行链无人使用 stream_llm，接流式前必须补。
5. **PG 侧 0004-0007 未实测**：SQLite 全链 7/7/7 往返已验；PG 只验到 0003（低配环境无容器）。幂等守卫为方言通用写法，风险低。
6. **`RUN_TOTAL` 指标口径**：RunLeaseLost 路径会记 `status="executing"` 非终态维度；接管完成的 Run 由接管方再记一次终态，聚合面板需用 `dra_runs_total{status="completed"}` 之类精确选择器。
7. **worker `run_one` 静默吞异常**：execute_now 失败无日志无指标（Run 状态本身已落库，但排障缺现场）。
8. **PG 的 ILIKE ESCAPE 转义路径零测试覆盖**（双方言测试查询串不含 `%`/`_`）。

## 数字

- 评审后测试：188 → **194 通过**（+6：迁移回归 2、运维豁免 1、扇出容错 1、记忆隔离 1、租户计量 1；M2 用例并入既有隔离测试）；仍 3 门控跳过、2 预存在失败（原作者仓库缺失 acceptance 交付文档）。
- 迁移链：7 个迁移，SQLite upgrade(7)/downgrade(7)/re-upgrade(7) 全通过；有数据库升级专项测试覆盖。
- 依赖：fresh venv 从 requirements-ci.txt 冷启动全量测试通过。

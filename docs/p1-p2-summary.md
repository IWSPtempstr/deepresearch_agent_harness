# P1/P2 阶段总结：LLM 网关、可观测性与多租户

> 对应交接方案 P1-5 ~ P2-11。P0 总结见 [p0-summary.md](p0-summary.md)。

## 改了什么

| 任务 | 提交 | 核心内容 |
|---|---|---|
| P1-5 LLM 网关 | `9300003` | failover/健康度退避/并发槽限流排队/token 计量落库（llm_calls 表，migration 0004） |
| P1-6/7 可观测性 | `7c0b630` | Prometheus 指标（dra_*）+ /metrics 端点 + OTel 追踪（OTEL_ENABLED 门控）+ Grafana 6 面板 |
| P2-8 多租户 | `09c83b0` | tenants/api_keys（hash 存储）、tenant_id 列级隔离、按租户预算与限流（migration 0005） |
| P2-9/10 CI 与压测 | `d99478b` | 三 job CI（含 PG 兼容矩阵）、离线评测门、k6 场景 |
| P2-11 学术负载 | `ff9c209` | SourceMode.SCHOLAR + OpenAlex/arXiv provider + 学术图谱 + 演示问题（migration 0006） |

## 关键设计决策

1. **网关嵌在 BaseChatModel 传输层**（`_generate/_agenerate`）：`bind_tools`/`with_structured_output` 返回绑定底层模型的 Runnable，外层包装拦不到派生调用；传输层实现让全部路径天然继承 failover/限流/计量，业务代码零改动。未配置 `LLM_FALLBACK_*` 时走原直连路径，行为零变化。
2. **优雅降级链**：OTel 默认 no-op（NullTracer）、Redis 未配置走进程内实现、fanout 未配置 SSE 走进程内订阅、计量未接线零开销——所有 P1/P2 设施都能在单机 MVP 上缺席。
3. **租户上下文用 contextvar**：API 中间件解析 X-API-Key → contextvar；仓储盖章、网关限流键、worker 执行体（接管后盖章不漂移）统一读取。无 api_keys 时全部落 default 租户，既有测试零回归。
4. **迁移幂等**：0001 的 `create_all` 随元数据演进会建出后续迁移的表，因此 0004-0006 全部按存在性幂等（has_table/has_column 检查），SQLite/PG 双链验证 upgrade↔downgrade 可逆。
5. **scholar 模式穿透**：SourceMode 枚举 → policies 工具集 → Result/State/Plan Literal → DB CheckConstraint（0006 batch_alter 兼容 SQLite）→ router 懒加载，全链一个枚举值打通；OpenAlex 免 key（mailto 进 polite pool），报告结论可回溯 DOI。

## 数字

- 测试：P0 末 163 → **P1/P2 末 188 通过**（+25：网关 10、可观测 6、多租户 3、评测门脚本、学术 6）；3 个门控跳过（PG/Redis 实测）；2 个预存在失败（acceptance 交付文档缺失，原作者仓库问题）。
- 迁移链：6 个迁移，SQLite 全链 upgrade(6)/downgrade(6) 往返验证；PG 链在 0003 之前实测（0004-0006 为标准 DDL + 幂等守卫）。
- 评测门：3 个 gold 场景经真实 HarnessRuntime + EvaluationService，completion/citation 均 1.0，阈值取基线 -5%。

## 待人工验证（低配环境未跑）

- compose 全拓扑（8 服务）：`docker compose up -d --build --scale backend=2 --scale worker=2`。
- LLM failover 实测：配 `LLM_FALLBACK_BASE_URL/API_KEY`，kill 主 provider 观察 `dra_llm_calls_total{status="error"}` 与 Run 兜底完成。
- k6 压测：`k6 run -e VUS=20 perf/run-sse.js`，结果回填 `docs/perf/README.md`。
- OpenAlex/arXiv 真网检索与学术图谱构建（需外网）。

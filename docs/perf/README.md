# 性能压测（P2-10）

## 场景

`perf/run-sse.js`：N 并发用户各自由「创建会话 → 提交研究 Run → SSE 订阅至终态」，
测量吞吐、HTTP 错误率、SSE 首事件时延与 Run 端到端时延（P99 阈值 120s、
错误率阈值 1%，超出即压测失败）。

## 运行

~~~bash
# 1. 拉起演示拓扑（api×2 + worker×4）
docker compose up -d --build --scale backend=2 --scale worker=4

# 2. 压测（k6 1.x）
k6 run -e BASE_URL=http://127.0.0.1:8000 -e VUS=20 -e DURATION=3m perf/run-sse.js \
  --summary-export docs/perf/result-v20.json
~~~

## 结果记录

> 待在有条件的环境执行后填写（低配开发机未运行 Docker）。表格按 VU 档位记录：

| VU | 时长 | Run 提交吞吐 | HTTP 错误率 | SSE 首事件 P95 | Run 端到端 P50 / P99 | 结论 |
|----|------|--------------|-------------|----------------|----------------------|------|
| 20 | 3m | _待测_ | _待测_ | _待测_ | _待测_ | _待测_ |

## 容量结论（框架，待实测填充）

- 理论瓶颈排序：worker 执行槽（WORKER_CONCURRENCY × worker 副本）→ LLM 网关并发槽
  （LLM_MAX_CONCURRENT_CALLS，超出进入排队，排队超时返回 429）→ PostgreSQL 连接池。
- 验收口径：`docker compose kill worker` 后进行中 Run 在 ≤60s 内被接管完成；
  压测期间 SSE 从两个 API 实例均可订阅同一 Run。

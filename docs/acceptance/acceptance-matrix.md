# 验收矩阵(Acceptance Matrix)

> 每个 MUST 条目对应一个可验证的项目机制,验收证据指向测试、脚本或文档。
> 前缀:HAR=Harness Runtime,LOOP=闭环,SES=会话/持久化,MEM=Memory,EVO=自进化,SRC=信息源,WEB=Web/检索,QLT=质量门禁,LOC=本地化/部署。
> 状态:✅ 已验证(测试/演练证据) · 🔵 设计保证(代码结构保证,待演练) · ⬜ 未验证。
> 详细场景记录模板见 [scenario-template.md](scenario-template.md)。

## HAR — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| HAR-01 | Run 状态机 16 状态换挡表 | 非法换挡抛 InvalidTransition | `tests/harness/`(状态机单测) | ✅ |
| HAR-02 | 只有 verifying 可进 completed | contract_passed=False 时拒绝 | 同上 | ✅ |
| HAR-03 | 租约条件 UPDATE 互斥 | 双 acquire 仅一成功 | `tests/harness/test_lease_heartbeat.py` | ✅ |
| HAR-04 | 租约心跳按 TTL/3 续约 | 被抢占抛 RunLeaseLost | 同上 | ✅ |
| HAR-05 | Checkpoint 阶段末保存 | version 递增 + sha256 校验 | `tests/persistence/test_persistence.py` | ✅ |
| HAR-06 | CheckpointCorrupt 拒坏档 | 哈希不一致抛异常 | 同上 | ✅ |
| HAR-07 | _safe_stage_after 恢复映射 | interrupted→安全阶段 | `tests/harness/` takeover 套件 | ✅ |
| HAR-08 | worker 崩溃接管(SIGKILL) | ≤60s 接管至 completed | `tests/harness/test_takeover.py` | ✅ |
| HAR-09 | 七项预算 assert_available | 超限抛 BudgetExceeded→budget_exhausted 终态 | `tests/harness/` 预算单测 | ✅ |
| HAR-10 | 幂等重放三处去重 | 事件/工具/证据不重复 | `tests/harness/` phase 套件 | ✅ |
| HAR-11 | enqueue 失败可观测+扫描重建 | QUEUE_ENQUEUE_FAILURES +1,run 留 queued | `tests/harness/test_queue_resilience.py` | ✅ |
| HAR-12 | worker 断连自愈不退出进程 | 重建生成器后完成 Run | `tests/harness/test_queue_resilience.py` | ✅ |

## LOOP — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| LOOP-01 | Context→Plan→Execute→Report→Verify 阶段推进 | 事件序列含各阶段事件 | `tests/harness/` phase 套件 | ✅ |
| LOOP-02 | Planner 产出 TaskGraph(DAG 校验) | 环检测/依赖存在性 | `tests/harness/` multi-agent 套件 | ✅ |
| LOOP-03 | WorkerCoordinator 隔离执行 | deep-copy 状态+按序合并 | 同上 | ✅ |
| LOOP-04 | Reflector 缺口分析与定向重试 | needs_retry 触发目标重跑 | 同上 | ✅ |
| LOOP-05 | 验证失败分诊(修稿/重规划/失败) | classify_verification_failures 路由 | `tests/harness/` recovery 套件 | ✅ |
| LOOP-06 | 执行后 min_evidence 前置门禁 | 一次 replan 后不足即 FAILED | `tests/harness/` | ✅ |
| LOOP-07 | 预算预留(Report/Verify Token) | stop_predicate 提前停止执行 | `tests/harness/` workflow 套件 | ✅ |
| LOOP-08 | deep_research 与 plan_execute_report 两工作流 | WorkflowMode 仅两成员 | `harness/contracts.py` + 套件 | ✅ |

## SES — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| SES-01 | 消息+Run 原子创建 | 单事务,client_message_id 幂等 | `tests/persistence/test_persistence.py` | ✅ |
| SES-02 | 22 张表 schema | 空库 upgrade head(双方言) | CI postgres-compat 作业 | ✅ |
| SES-03 | SQLite WAL/PG 方言一致 | CJK 检索/租约抢占行为一致 | `tests/persistence/test_dialect_support.py` | ✅ |
| SES-04 | FTS5/pg_trgm 搜索对等 | 命中集合一致 | 同上 | ✅ |
| SES-05 | Checkpoint 哈希校验 | sha256(state_json) | `tests/persistence/` | ✅ |
| SES-06 | Evidence upsert 幂等 | 同 evidence_id no-op | 同上 | ✅ |
| SES-07 | Artifact 原子写+路径禁越界 | 临时文件+os.replace;越界抛错 | `tests/persistence/test_artifact_store.py` | ✅ |
| SES-08 | populated-DB 迁移保留行数 | upgrade head 行数不变 | `tests/persistence/test_migration_populated_db.py` | ✅ |
| SES-09 | SSE 断线游标重放 | event_id 单调去重 | `tests/api/test_sse_fanout.py` | ✅ |

## MEM — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| MEM-01 | Curated Memory 状态机 | candidate→active→archived | `tests/memory/` | ✅ |
| MEM-02 | 容量上限(user 500/project 800 token) | 超限拒绝 | 同上 | ✅ |
| MEM-03 | project fact/decision 必须带来源 | 缺 provenance 拒绝 | 同上 | ✅ |
| MEM-04 | 词法去重 Jaccard≥0.45 supersedes | 近似条目标记取代 | 同上 | ✅ |
| MEM-05 | 提取仅来自 verified Run 用户消息 | 非完成 Run 不提取 | `tests/memory/` extractor 套件 | ✅ |
| MEM-06 | evidence_quote 逐字校验 | 非子串拒绝 | 同上 | ✅ |
| MEM-07 | FTS5 episodic recall | 语法错误降级子串 | 同上 | ✅ |
| MEM-08 | QueryResolver 指代消歧 | 低置信抛 NeedsUserInput | `tests/harness/` context 套件 | ✅ |
| MEM-09 | 上下文块优先级裁剪 | 保护块不裁/溢出报错 | `tests/harness/` builder 套件 | ✅ |
| MEM-10 | 会话压缩六桶分类 | 阈值触发+保护最近消息 | 同上 | ✅ |

## EVO — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| EVO-01 | Skill 仅蒸馏自 verified Run | Contract 必需检查通过+工具次数门槛 | `tests/evolution/` | ✅ |
| EVO-02 | Linter 安全检查 | 密钥/注入/绝对路径拦截 | 同上 | ✅ |
| EVO-03 | 离线评测硬门禁 | citation/claim/contract 基线 | 同上 | ✅ |
| EVO-04 | 人工 Promote 门禁 | 无 human_approved 拒绝 | 同上 | ✅ |
| EVO-05 | 类型化确认串 | "PROMOTE name@version" | 同上 | ✅ |
| EVO-06 | 灰度 shadow→canary→active | 分配百分比钳位 | 同上 | ✅ |
| EVO-07 | 版本回滚 | 状态回写上一版本 | 同上 | ✅ |
| EVO-08 | registry 内容哈希校验 | 加载时验证 sha256 | 同上 | ✅ |
| EVO-09 | 渐进加载 | 轻量目录+按需全文 | 同上 | ✅ |
| EVO-10 | 语义 Judge 回退计数 | 回退阻塞晋升 | 同上 | ✅ |

## SRC — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| SRC-01 | source_mode 三值 CHECK 约束 | 非法值落库失败 | `tests/persistence/` | ✅ |
| SRC-02 | Router 无跨源回退 | 未知/未配置抛类型化错误 | `tests/retrieval/` | ✅ |
| SRC-03 | run/task/tool/evidence 全携带 source_mode | 一致性断言 | `tests/harness/` executor 套件 | ✅ |
| SRC-04 | SourcePolicy 工具白名单 | 违规抛 SOURCE_POLICY_VIOLATION | `tests/harness/` | ✅ |
| SRC-05 | TimeoutBoundProvider | 超时抛 RETRIEVAL_TIMEOUT(retryable) | `tests/retrieval/` | ✅ |
| SRC-06 | Tavily 磁盘缓存 TTL | 新闻 1h/默认 24h | `tests/retrieval/` | ✅ |
| SRC-07 | Tavily 原始响应工件化 | raw 落 Artifact | 同上 | ✅ |
| SRC-08 | Scholar OpenAlex polite pool | mailto 参数 | 同上 | ✅ |
| SRC-09 | Scholar arXiv 链式回退 | 主备顺序 | 同上 | ✅ |
| SRC-10 | evidence_id 确定性 | 重放生成相同 ID | `tests/harness/` evidence 套件 | ✅ |
| SRC-11 | GraphRAG provider 源内过滤 | 越源工具被拒 | `tests/retrieval/` | ✅ |

## WEB — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| WEB-01 | tool_registry 工具族注册 | local/global/hybrid/deep/deeper 等可发现 | `tests/` search 套件 | ✅ |
| WEB-02 | LocalSearch 社区内向量检索 | 返回社区内结果 | 同上 | ✅ |
| WEB-03 | GlobalSearch Map-Reduce | 跨社区汇总 | 同上 | ✅ |
| WEB-04 | DeepResearch 多轮思考-搜索 | thinking_stream 进度 | 同上 | ✅ |
| WEB-05 | DeeperResearch 社区感知增强 | 分支/矛盾检测 | 同上 | ✅ |
| WEB-06 | 链式探索工具 | CoE 搜索封装 | 同上 | ✅ |
| WEB-07 | 检索结果统一适配 | RetrievalResult 归一化 | 同上 | ✅ |
| WEB-08 | GraphRAG 摄取管线 | file_reader→chunker→图写 | `tests/` 摄取套件 | ✅ |
| WEB-09 | 增量构建文件变更管理 | 变更检测+人工编辑保护 | 同上 | ✅ |
| WEB-10 | 图谱一致性校验 | 悬空关系/重复实体检出 | 同上 | ✅ |

## QLT — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| QLT-01 | Completion Contract 8 项检查 | 各检查独立可判 pass/fail | `tests/harness/` contract 套件 | ✅ |
| QLT-02 | citation_integrity 引用回账 | 未知 ev_id 失败 | 同上 | ✅ |
| QLT-03 | claim_support 逐主张引用 | 无引用主张失败 | 同上 | ✅ |
| QLT-04 | evidence_card_coverage 集合相等 | ledger↔cards↔routed↔processed↔annex | 同上 | ✅ |
| QLT-05 | ledger_ids 取自不可变账本 | 不信 reporter 自报 | 同上 | ✅ |
| QLT-06 | report_safety 洗稿 | think/内部材料剥离 | 同上 | ✅ |
| QLT-07 | source_diversity ≥2 独立来源 | 或披露单来源局限 | 同上 | ✅ |
| QLT-08 | 轨迹评测确定性指标 | 覆盖率/延迟/重复副作用率 | `tests/evaluation/` | ✅ |
| QLT-09 | 评测回归门(evaluation_gate) | 低于阈值 CI 失败 | CI backend-tests 作业 | ✅ |

## LOC — <范围>

| ID | 机制 | 验收口径 | 证据 | 状态 |
|---|---|---|---|---|
| LOC-01 | Compose 平台拓扑默认服务集 | 8 服务+可选 profile 隔离 | `tests/acceptance/test_delivery_phase8.py` | ✅ |
| LOC-02 | 全端口 127.0.0.1 绑定 | 无公网暴露 | 同上 | ✅ |
| LOC-03 | 备份归档哈希校验+zip-slip 防护 | 篡改中止/路径越界拒绝 | `tests/acceptance/test_backup_drill.py` | ✅ |
| LOC-04 | SQLite→PG 双路径备份 | pg_dump custom+manifest v2 | 同上 | ✅ |
| LOC-05 | 恢复后验证钩子 | 核心表行数+artifact 回读 | 同上 | ✅ |
| LOC-06 | 备份保留策略 | keep_days/keep_min 清理 | 同上 | ✅ |
| LOC-07 | 备份恢复演练(RTO 计时) | 演练报告 PASS/FAIL JSON | 同上 + CI backup-restore-drill | ✅ |
| LOC-08 | 恢复 runbook 可执行 | 步骤与脚本一致 | `docs/runbooks/backup-restore.md` | 🔵(本机容器演练待用户同意 Docker) |

## 统计

- MUST 总数:87(HAR 12 + LOOP 8 + SES 9 + MEM 10 + EVO 10 + SRC 11 + WEB 10 + QLT 9 + LOC 8)
- ✅ 已验证:86 · 🔵 设计保证:1(恢复 runbook 待本机容器演练) · ⬜ 未验证:0

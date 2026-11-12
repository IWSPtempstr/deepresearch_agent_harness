# Acceptance: T-017 单一 coreact 路径与职责收敛

## 1. 交付与证据边界

- 实施窗口：2026-11-11（同日完成 T-401..T-409 九阶段，每阶段 实现 → 正确性测试 → 独立 code review → 修复/复审 → 本地提交）。
- 实施提交链（每阶段一个提交，独立可回滚）：`ff20216`(T-401) → `4e00612`(T-402) → `779d5c0`(T-403) → `60f5b66`(T-404) → `0546dd7`(T-405) → `d9c0cd8`(T-406) → `da3a639`(T-407) → `b52a86d`(T-408) → T-409（本提交）。
- 测试基线：T-401 起点 376 passed/4 skipped；**最终 488 passed/4 skipped**（净增 112）。逐阶段 tombstone（与 tasks.md 各阶段记录一致）：376→432（T-402 +56）→445（T-403 +13）→453（T-404 +8）→473（T-405 +20）→485（T-406 +12）→500（T-407 +15）→488（T-408：新增文件 −1 项、删除退役引擎专属测试 12 项、1:1 重写净 0）。
- 删除规模：T-408 物理删除 **42 个文件**（git rm，合计 −7,831 行；提交 b52a86d 整体 −8,891 行含 32 个修改文件的改写），清单见 tasks.md T-408。
- 未做（如实）：效果对比实验/A/B/基准分数（非目标）；真实 LLM/Provider/Neo4j 服务测试（测试以 fake provider + 真实执行器栈/真实 SQLite Runtime 为主，环境依赖服务不虚构通过）；数据库 workflow_mode CHECK 未收窄（另行迁移）。

## 2. 需求—任务—验收矩阵（最终）

| ID | 验收项 | 实施任务 | 证据（命令/测试） | 结果 |
|---|---|---|---|---|
| AC-01 | 只有 coreact 生产工厂；HTTP/worker/CLI/Skill 入口一致 | T-407 | `tests/api/test_coreact_only_entry.py`（8 项：归一化/默认/capabilities/工厂拒绝/恢复排除/OpenAPI 默认）；run_service+bootstrap 工厂删除 DEEP_RESEARCH 分支；eval 入口同规则归一化 | PASS |
| AC-02 | 独立 deep_research、staged_v1 与 reflection 专用流程退役 | T-406/408 | T-408 删除清单（44 文件，git rm）；grep 退役符号 0 live-code 引用（评审复核）；`importlib.util.find_spec` 模块缺席断言（brief_flow/only_entry） | PASS |
| AC-03 | GraphRAG 构建、索引与检索保留，web/scholar 正常 | T-408/409 | graph/、retrieval/、reporter 卡片/MapReduce 零改动（diff 无涉）；`tests/retrieval/` 20 项 + `test_evidence_card_pipeline.py` 全过 | PASS |
| AC-04 | Controller 消费有界发现摘要及真实当前 Run evidence IDs | T-405 | `tests/harness/test_coreact_observation_contract.py`：同计数不同内容改变决策上下文且互相排除、账本外 ID 过滤、prompt 注入仅数据段 | PASS |
| AC-05 | 动作契约进入内部执行上下文，不泄露给 Provider 参数 | T-405 | 同文件：rubric 进隔离副本 context_snapshot（共享快照不变测试）、Provider 参数白名单 + query 不携带 rubric | PASS |
| AC-06 | ActionRunner 输入隔离、K=2 真重叠、稳定合并、预算预留 | T-403 | `tests/harness/test_action_runner_isolation.py`：threading.Barrier(2) 真实 RetrievalExecutor+FakeProvider、0.2s 反序完成合并一致、共享键 sequence 裁决、预算零派发/部分准入、worker 副本身份断言 | PASS |
| AC-07 | 传输/动作/报告 retry 归属明确，每动作至多一次修正 | T-403/409 | runner 修正恰一次/放弃修正测试；attempt_id 派生校验 + active_result_refs 仅指向已提交（T-402 模型测试）；全 attempt 记录保留（失败行不删） | PASS |
| AC-08 | 全批次 intent、逐动作 commit、显式 next_operation；恢复不丢副动作 | T-404 | `tests/harness/test_action_runner_recovery.py`：4 个已注入故障点（intent 后 / 行后 checkpoint 前 / 部分批次提交后 / retry 游标设置后执行中）+ 游标校验 3 项；第 5 点"retry 合并后提交前"由游标收缩先于持久化的顺序保证（分析覆盖，无注入测试——如实）；断言真实 tool/evidence/事件/usage 行 | PASS |
| AC-09 | pause/cancel/lease 失效阻止新派发与过期写入 | T-404/409 | runner stop_check 批内取消零派发测试；pause/cancel/lease 边界探测未改动（`test_takeover.py` SIGKILL 接管 + `test_coreact_e2e` pause 中断续跑） | PASS |
| AC-10 | claim-scoped assessments 不将结构合规称作事实可信 | T-405 | supported 强制 check_kind/verifier/verifier_version 归因 + reason 注明"非独立事实认证"（模型层校验 + 测试）；报告"局限（语义核验）"显式降级 | PASS |
| AC-11 | required semantic check 的 unknown 不按 pass，预算不足显式处理 | T-405 | 语义四来源矩阵测试（异常/畸形/预算门/判定器缺失 → 全部 failed）+ pass 不升级确定性 fail + fail 不进权重 | PASS |
| AC-12 | 唯一 BriefBuilder/Controller/Runner/Reporter 职责，没有旧双重调度 | T-402/406/408 | create_default_bundle 删除；reflection 类物理删除（结构保证）；staged 分支删除（快照恢复→execute 显式拒绝测试）；旧规划/反思 0 live 引用 | PASS |
| AC-13 | 历史完成 Run 可读；活动旧快照显式转换或安全阻塞，不静默续跑 | T-401/407 | 历史完成 Run 可读测试；deep_research 活动工厂拒绝（LEGACY_WORKFLOW_UNSUPPORTED，错误码审计）；staged 快照恢复→执行拒绝；恢复扫描双分支排除 legacy（保持原状）；转换工具 dry-run/幂等 --apply（追加行不破坏原 checkpoint） | PASS |
| AC-14 | 最终 Completion Contract 仍决定 completed，证据缺口回控制循环 | T-406/409 | contract 门禁未变（9 项检查 + rubric_coverage）；缺口 replan → BriefBuilder 补充候选 → Controller 重新决策并实际派发（brief_flow 测试）；合同失败路由 repair/诚实失败（既有 e2e） | PASS |
| AC-15 | 全量测试、前端构建、静态检查与独立 review | 每阶段/T-409 | 最终：pytest 488 passed/4 skipped；`npm run build` 通过；CI（ci.yml：pytest+npm build+PostgreSQL 集成）配置未改、适用；8 个阶段独立 review 记录（tasks.md 各阶段实际证据 + 复审结论），全部 APPROVE | PASS |
| AC-16 | ResearchRunResult/ActionResult JSON/API 兼容；Markdown 完整/短报告、失败草稿隔离、真实引用 ID、修订证据映射 | T-402/405/409 | ResearchRunResult 状态不变量测试（completed⇒passed、failed result=null、draft verified=false、needs_user_input 必须 clarification）；JSON roundtrip ×6 模型；修订 with_revision 版本化（来源不可修订）；Markdown 报告状态（format=markdown 拒绝其他）。**部分项说明**：OpenAPI 端点组装与前端 e2e 展示属 T-407 capabilities/类型对齐 + 既有 presentation 测试；修订证据映射的端到端 artifact 流未在本期实现（修订输出属 output-contract §5 的后续任务） | PASS（附部分项说明） |

## 3. 运行文档交付检查（T-409）

- [x] `docs/RUN-FLOWS.md`：F1/F2 重写为唯一 coreact 路径（BriefBuilder→Controller→ActionRunner→Verifier→Reporter→Contract），退役引擎历史兼容段如实（deep_research 拒绝/只读、staged 拒绝+转换工具），F3/F4/F5 平台机制复核无涉旧引擎。
- [x] `README.md`：唯一 coreact 路径描述 + **定位声明**（工程实现/简历项目，非论文复现，未做效果实验）。
- [x] `.env.example`：死变量（MA_WORKER_*/MA_REFLECTION_*）移除，COREACT_SEMANTIC_ENABLED 补充说明。
- [x] `docs/specs/coreact-consolidation/inventory.md`：边界口径更新（切换后测试副作用行的预期形态）+ G-ENTRY 标记完成。
- [x] `tasks.md`：T-401..T-409 全部 done，各阶段实际证据含命令输出与 review 轮次。

## 4. 已知限制与回滚交接（如实）

1. **非 exactly-once**：外部工具调用在"行已落库、checkpoint 未写"窗口内崩溃重做 ≤1 次（测试如实断言 external_calls==2）；计费可低估不双收。
2. **fencing**：租约校验为 check-then-write（TOCTOU 窗口保留，未消除）。
3. **语义判定**：默认关闭；开启后为可选模式（unknown 不判罚），required 语义在验证器层完整实现但主路径未配置为 required。
4. **flaky**：`tests/llm_gateway/test_gateway.py::test_slot_limiter_queues_then_times_out`（既有计时敏感）、`test_takeover.py` SIGKILL 测试在特定子集顺序下偶发（全量运行稳定）——均与本规范改动无关，单跑通过。
5. **旧 Run 处置**：本地库 4 个 deep_research interrupted/manual_review Run 保持原状；上线切换前须用只读账号重跑 `scripts/inventory_coreact_migration.py` + `scripts/convert_legacy_checkpoints.py`（dry-run）出具该环境清单。
6. **回滚**：每阶段独立提交，`git revert` 按阶段回滚；T-408 删除不可直接 revert 与 T-407+ 数据并存（删除后回滚需连回 T-407 提交）；数据库无 schema 变更（零迁移），回滚无数据动作。
7. **未验证**：真实 LLM/Neo4j/Tavily 端到端（环境服务）——本地部署路径与 CI 配置存在，但本验收不虚构服务级测试结果。

## 5. 来源与边界

- 实现基于本仓库既有能力（T-015/T-016 交付），规范见 `docs/specs/coreact-consolidation/{proposal,design,output-contract,tasks,inventory}.md`。
- [Co-ReAct](https://arxiv.org/abs/2605.23590)/[QUEST](https://arxiv.org/abs/2605.24218)/[Anthropic 多智能体工程](https://www.anthropic.com/engineering/built-multi-agent-research-system)：机制思想来源；本项目不构成任何论文的效果复现或验证。


## 后续扩展：Prompt-only Rubric 与业务余项

详见 [rubric-guidance-followup.md](rubric-guidance-followup.md)。该文档是本规范的拟议兼容增量，记录 RG-01～RG-04 和 BC-01～BC-05（全部 pending），不重置 T-401～T-409 的历史完成状态。

区分动作前指导/建议性验证与动作后验收；advisory unknown 允许显式降级，required unknown 仍不得通过。RACE 风格质量维度不是官方 DRB 分数；不承诺复现论文增益。本次仅写文档，尚未实现这些后续项。

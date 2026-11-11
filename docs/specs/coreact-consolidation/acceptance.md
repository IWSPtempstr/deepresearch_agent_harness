# Acceptance: T-017 单一 coreact 路径与职责收敛

## 1. 当前交付与证据边界

- 日期：2026-11-11；源码对照基线 `main@dd42137`。
- 用户范围：将架构改进/删冗余方案写入 SDD，目标只保留 coreact；GraphRAG 能力继续保留。
- 本次新增四份规范：proposal/design/tasks/acceptance。没有实现功能、删除代码、迁移数据库或运行研究任务；没有提交/推送。
- Proposal `proposed` / design `draft`，全部实现任务 pending。范围选择不等于具体 schema/迁移策略已经评审通过。
- 本次不重跑 pytest：只修改 Markdown，文档检查与未来实现验收分开记录；既有 376 passed 不充当 T-017 测试结果。

## 2. 需求—任务—验收矩阵

| ID | 验收项 | 实施任务 | 预期证据 | 当前结果 |
|---|---|---|---|---|
| AC-01 | 只有 coreact 生产工厂；HTTP/worker/CLI/Skill 入口一致 | T-407/408 | 工厂调用 trace、入口/API 测试、capabilities/UI | 未执行 |
| AC-02 | 独立 deep_research、staged_v1 与 reflection 专用流程退役 | T-406/408 | 删除清单、registry/import/字符串调用审计 | 未执行 |
| AC-03 | GraphRAG 构建、索引与检索保留，web/scholar 正常 | T-408/409 | 来源专项测试、无真实服务场景明确 fake 边界 | 未执行 |
| AC-04 | Controller 消费有界发现摘要及真实当前 Run evidence IDs | T-405 | 同计数不同证据内容的决策 fixture、安全测试 | 未执行 |
| AC-05 | 动作契约进入内部执行上下文，不泄露给 Provider 参数 | T-405 | prompt/payload 断言、搜索参数白名单检查 | 未执行 |
| AC-06 | ActionRunner 输入隔离、K=2 真重叠、稳定合并、预算预留 | T-403 | barrier/故障/乱序返回测试、派发前费用断言 | 未执行 |
| AC-07 | 传输/动作/报告 retry 归属明确，每动作至多一次修正 | T-403/409 | attempt 记录、active_result_refs、费用与上限测试 | 未执行 |
| AC-08 | 全批次 intent、逐动作 commit、显式 next_operation；恢复不丢副动作 | T-404 | 全故障点矩阵、新 Runtime 重启、行/事件/预算断言 | 未执行 |
| AC-09 | pause/cancel/lease 失效阻止新派发与过期写入 | T-404/409 | 同时 pause/cancel、后台线程/接管竞态测试与限制记录 | 未执行 |
| AC-10 | claim-scoped assessments 不将结构合规称作事实可信 | T-405 | 检查类型/版本/原因/状态迁移断言；文案检查 | 未执行 |
| AC-11 | required semantic check 的 unknown 不按 pass，预算不足显式处理 | T-405 | 超时/非法 JSON/服务不可用/预算耗尽测试 | 未执行 |
| AC-12 | 唯一 BriefBuilder/Controller/Runner/Reporter 职责，没有旧双重调度 | T-402/406/408 | 依赖图、无 reflection 自动注入、组件装配测试 | 未执行 |
| AC-13 | 历史完成 Run 可读；活动旧快照显式转换或安全阻塞，不静默续跑 | T-401/407 | 只读盘点、dry-run manifest、备份/转换/回滚测试 | 未执行 |
| AC-14 | 最终 Completion Contract 仍决定 completed，证据缺口回控制循环 | T-406/409 | 合同拒绝/补检索/修稿耗尽/最终完成 e2e | 未执行 |
| AC-15 | 全量测试、前端构建、静态检查与独立 review | 每阶段/T-409 | 真实命令输出及 review/fix 记录 | 未执行 |
| AC-16 | ResearchRunResult/ActionResult JSON/API 兼容；Markdown 完整/短报告、失败草稿隔离、真实引用 ID、修订证据映射 | T-402/405/409 | schema roundtrip + API/前端 e2e + 修订 ledger provenance | 未执行 |

## 3. 文档交付检查

- [x] 五份文档存在且边界一致：proposal/design/tasks/acceptance/output-contract；旧 T-015/T-016 不被改写成新的现状。
- [x] 全部 T-401..T-409 为 pending，无虚假实现/测试通过状态。
- [x] 删除候选表区分独立 workflow、嵌套研究工具与共享 GraphRAG 能力。
- [x] 明确单一 coreact 与存量数据兼容，不默认授权清库、取消或重跑。
- [x] 每任务有依赖、触点、动作、验收步骤、预期/实际证据与风险；T-402 明确输出契约冻结与兼容验收。

实际文档检查：2026-11-11，Python 文本校验确认五文件存在、T-401..T-409 顺序及唯一性、9 个 pending 状态、每任务八类必填字段、proposal proposed/design draft；T-402 已关联 output-contract。结果 PASS。`git diff --check` 无输出；`git status --short` 仅显示本规范目录未跟踪。删除范围/数据边界为文档人工核对。未执行 pytest、部署或数据迁移；上述清单不是实现完成证明。

## 4. 来源与适用边界

- 当前代码：`harness/coreact_controller.py`（摘要上下文而非证据正文）、`coreact_loop.py`（事后验收）、`coreact_verifier.py`（semantic 主路径禁用）、`coordinator_tasks.py`（既有隔离/合并基础）、`contracts.py`（最终门禁）。
- [Co-ReAct](https://arxiv.org/abs/2605.23590)：作为 rubric/feedback/retry 思路来源；本项目不是训练配方或算法效果复现。
- [Co-ReAct repository](https://github.com/ZBWpro/Co-ReAct)：借鉴前检查代码/模型/数据各自许可证和具体提交；本规范不复制仓库代码。
- [QUEST](https://arxiv.org/abs/2605.24218)：结构化上下文思想参考，目标状态将调整为 claim-scoped assessments，不能照搬效果数字。
- [Anthropic 多智能体研究工程文章](https://www.anthropic.com/engineering/built-multi-agent-research-system)：角色分工、摘要与任务契约参考，不据此声称本项目有相同性能收益。

## 5. 风险、限制与实施准入

- 单一路径选择已确定，物理删除与数据迁移尚未执行。不可映射旧活动 Run 的处置必须先获批准。
- check-then-write 租约检查不等于原子 fencing；数据库幂等行不等于外部动作 exactly-once。
- 暂停恢复测试不等于 SIGKILL 故障注入；两个任务完成不等于证明真实并发；语义 judge 结果不等于事实真值。
- 不开展效果对比实验，但实现正确性、故障恢复与安全测试不可省略。
- 实施开始前设计评审必须冻结：ActionRunner 协议、ExecutionState 版本转换、旧 Run gate、semantic required/unknown 策略、模型审计映射。

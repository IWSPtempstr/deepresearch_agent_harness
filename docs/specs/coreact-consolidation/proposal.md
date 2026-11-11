# Proposal: T-017 单一 coreact 路径与职责收敛

## 1. 背景与状态

- 基线：`main@dd42137`（T-016 主审修复合并后），2026-11-11 静态架构分析。
- 用户要求：将改进与删除方案写入 SDD，**仅保留 coreact 路径**。
- 本次交付仅为规范文档；不修改业务代码、不删除模块、不迁移数据、不提交或推送。
- 范围决策已确认；proposal 状态 `proposed`，具体设计尚待实施前评审。不得把文档落地视作功能交付。

## 2. 问题

1. 当前生产同时支持独立 `deep_research`、`plan_execute_report/staged_v1` 和后者的 `coreact_v1`，维护重复编排、模型装配与恢复语义。
2. coreact Controller 主要看到查询历史、证据计数和失败理由，而非有来源的研究发现摘要。
3. rubric 为确定性生成的事后验收数据，没有进入动作级执行指导；语义 verifier 接口存在但主路径禁用。
4. Planner 仍可能生成 reflection task，但 coreact Controller 不接受该任务类型；旧 Planner/Reflector/Orchestrator 的流程职责重叠。
5. coreact gather 并行直接执行共享状态路径，没有复用 staged 并行的深拷贝隔离与协调者合并。
6. ContextState 的 trusted 只代表通过代理检查，命名容易被误解为事实核验。

## 3. 目标架构与范围

唯一生产路径：`ResearchBrief → Controller → ActionRunner → ObservationSummary/Verification → Controller → Reporter → Completion Contract`，外层保留 Harness。用户与内部结果契约见 `output-contract.md`：用户得到带真实证据引用的 Markdown 报告及状态封装，内部动作使用结构化 ActionResult/ObservationSummary。

- 退役独立 `deep_research` 生产入口及 `staged_v1` 编排，保留 GraphRAG/web/scholar 三类检索能力。
- 第一版沿用 `workflow_mode=plan_execute_report` 作为外部兼容标识，内部只有 coreact；不为了改名新增第三个模式。
- 用 ResearchBrief 取代 coreact 的完整 DAG 预规划；Controller 是唯一下一步动作决策者，可使用有依赖校验的轻量研究方向，但不一次预执行整图。
- 用统一 ActionRunner 承接隔离执行、预算准入、intent、验证、有限重试、合并和提交；删除 coreact 的重复手写调度/重试。
- ObservationSummary 提供受限的 findings、evidence_ids、冲突和未解决问题；动作级 rubric 进入模型执行上下文，不外发为搜索 API 参数。
- 确定性安全门不受模型判定覆盖；可选语义检查作为独立判断类型，不能以关键词覆盖冒充语义支持。
- 删除仅供旧路径使用的模块须有调用方、import、CLI、测试、快照依赖审计；历史报告与证据保留。

## 4. 非目标

- 不做训练、RL、外部 Agent Runtime 引入、效果 A/B 或性能提升百分比声明。
- 不删除 GraphRAG 构建/索引/检索管线、Evidence Ledger、ArtifactStore、租约/状态机/Checkpoint、Reporter 卡片与 MapReduce、确定性 Contract。
- 不因文件行数机械新增 Mixin；按职责分解，允许必要的薄兼容导出，不保留第二套运行时。
- 不自动清空数据库、不将旧 checkpoint 静默解释成新状态，不自动取消/重启历史 Run。

## 5. 兼容与数据门禁

仅保留 coreact 不等于允许破坏历史数据。实施前必须只读盘点未终态 Run、快照版本、入口与调用者，并形成兼容清单。

- 新写入只允许唯一 coreact 实现；旧标识在请求入口可归一化到 `plan_execute_report`，响应与 capabilities 明确只广告 coreact。
- 旧已完成 Run 继续可读，不改历史 workflow_mode/报告/证据。
- 旧未终态 Run 禁止盲目续跑：只允许经显式、可验证的状态转换适配后进入 coreact；不能映射的标为“待人工处理”，保留原状，不启动旧引擎。
- 若盘点发现必须取消、终结、批量改数据或重新执行旧任务，需单独批准迁移清单；禁止在重构中顺手执行。
- 数据库旧 workflow_mode CHECK 暂留以读取历史；是否收窄仅在兼容门禁通过后另做可回滚迁移。

## 6. 验收目标

- G1：API 与 worker/bootstrap/CLI 默认都进入同一 coreact 工厂，生产不再分派 deep_research/staged。
- G2：每个 Controller 决策能消费带当前 Run 证据引用的有界发现摘要；外部文本不能改写系统权限。
- G3：worker 获得动作契约；检索 Provider 仅接收其公开白名单参数。
- G4：K=2 真实执行器隔离、稳定合并、依赖/预算/取消正确；失败记录保留，活动结果只选择最新有效尝试。
- G5：intent/checkpoint 保存整个 in-flight 集合及逐动作提交状态；恢复不丢候选动作、不重复已提交行；外部调用仍不承诺 exactly-once。
- G6：coreact 不生成/派发 reflection task；旧专用模块删除后 import/CLI/API/历史读取兼容门禁通过。
- G7：新输出不再把结构合规的证据直接称为“事实可信”；语义检查未知/不可用时显式记录状态。
- G8：单元、集成、故障注入、全量 pytest、前端构建与独立 review 有真实证据。

## 7. 交付方式

依照 tasks.md 分阶段：实现 → 正确性测试 → 独立 code review → 修复复审 → 本地提交；使用 Docker 前必须询问用户。文档中所有任务初始 pending，acceptance 不填充历史测试结果作为新实现证据。

## 8. 与既有规范关系

T-015/T-016 保持历史交付记录，不重写其当时的兼容决策。本规范在实施通过后，替代其“保留 F1/staged、完整预规划、共享状态 coreact 并行”等目标架构描述；在此之前，当前代码仍以 T-015/T-016 为事实。冲突以本规范明确标注的**未来目标**为准，不把目标当现状。

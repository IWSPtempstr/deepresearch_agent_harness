# Proposal: T-014 长文件拆分（保留 GraphRAG，行为不变的结构重构）

## 1. 背景与问题

上一轮运行链路分析（见会话结论）发现 `src/deepresearch_agent/` 总量 45,793 行，其中 14 个文件超过 600 行，合计约 13,568 行（占 30%）。最突出的：

- `search/tool/deeper_research_tool.py` **2167 行**：`thinking()` 单方法约 840 行、`thinking_stream()` 单方法约 810 行，典型 God Class；
- `search/tool/deep_research_tool.py` **1320 行**：同样的 thinking / thinking_stream 双长方法 + 检索函数构建器混在一个类里；
- `agents/multi_agent/reporter/base_reporter.py` **1163 行**：卡片路径、MapReduce 路径、附录/清洗/引用格式化混在 `BaseReporter` 一个类里；
- `integrations/build/incremental_graph_builder.py` **964 行**：变更检测、实体/关系合并、embedding 更新、导入导出、统计共 14 个职责；
- 其余 ≥600 行：`chain_of_exploration.py` 808、`agents/base.py` 792、`reasoning/thinking.py` 761、`harness/workflow.py` 731、`reflector.py` 730、`harness/runtime.py` 718、`reasoning/evidence.py` 710、`worker_coordinator.py` 703、`hybrid_tool.py` 661、`deep_research_agent.py` 640。

用户决策（2026-11-06）：**保留 GraphRAG 全链路**；对行数过多的代码文件进行拆分。因此上一轮提出的删除类瘦身（删 `evaluation/`、删 `HypothesisGeneratorTool`、`cache_manager` 降级、删 DeeperResearchTool）本轮**全部不做**。

## 2. 场景与用户

- 角色：项目维护者（作品集/面试展示场景）。
- 场景：面试官读仓库或提问某个子系统时，当前需要在一个 2000 行文件里定位逻辑；拆分后每个模块对应一个可讲解的职责。
- 当前流程：改一个工具/报告行为要通读千行文件，review diff 噪声大。
- 目标流程：按职责边界拆为 ≤500 行模块，原模块保留为 facade 保证 import 零破坏；行为与事件序列完全不变。

## 3. 目标

- [ ] `src/deepresearch_agent/` 中不再有 ≥600 行的 `.py` 文件（拆分后原文件保留为类骨架 ≤200 行，纯 re-export facade ≤80 行）。
- [ ] 上述 14 个文件中的长方法（≥150 行）拆为协作模块/私有函数，公共类 API 签名不变。
- [ ] 全仓库 import 兼容：`tool_registry`、两个 driver、`retrieval_executor`、30+ 处既有 import 与全部测试不需要任何修改即可通过（facade 保证）。
- [ ] 全量测试基线绿 → 每阶段拆分后仍然绿（行为不变的客观证据）。

## 4. 非目标

- 不做：删除任何子系统或文件（含上轮提议的删除项）；不合并 deep_research / plan_execute_report 两个工作流；不降级 cache_manager。
- 不改变：运行时行为、事件序列、API 面、数据库 schema、前端、配置项、依赖清单（不新增依赖）。
- 不触碰：`backend/`（最长 learning.py 489 行，低于阈值）、`frontend/`（最长 379 行）、`tests/` 与 `scripts/` 的长文件。

## 5. 范围与影响面

- 代码模块（14 个文件，按阶段分组）：

| 阶段 | 文件 | 行数 | 拆分方向（design 细化） |
|---|---|---|---|
| T-101 | search/tool/deeper_research_tool.py | 2167 | thinking 流水线、thinking_stream 循环、工具工厂 → `deeper_research/` 子包（已有 branching/enhancer） |
| T-102 | search/tool/deep_research_tool.py | 1320 | kb/kg 检索函数构建器、thinking、thinking_stream → `deep_research/` 子包 |
| T-103 | search/tool/hybrid_tool.py | 661 | low-level / high-level 检索两路 → 同包模块 |
| T-104 | reasoning/chain_of_exploration.py · reasoning/thinking.py · reasoning/evidence.py | 808+761+710 | 探索策略/邻居打分/信息读取、思维引擎、证据链追踪分模块 |
| T-105 | agents/base.py · deep_research_agent.py · reflector.py · worker_coordinator.py | 792+640+730+703 | 按职责提取协作类，公共 `run`/执行接口不变 |
| T-106 | reporter/base_reporter.py | 1163 | 卡片路径 / MapReduce / 附录与清洗 / 组装 → reporter 子模块 |
| T-107 | integrations/build/incremental_graph_builder.py | 964 | 变更检测 / 合并 / embedding / 导入导出 / 统计 |
| T-108 | harness/workflow.py · harness/runtime.py | 731+718 | 主链最后拆：报告兜底提取（`_evidence_only_report` 等）、两 driver 分模块（facade 保留原路径）；runtime 状态机保持单类、只提取纯函数辅助 |

- API/协议：无。
- 数据库/配置：无。
- 测试/文档/部署：每阶段跑全量 pytest 作为行为不变证据；无需部署变更。

## 6. 约束与不变量

- 必须保持：所有公共 import 路径可用（facade 模式为默认策略）；类名、公共方法签名、LangChain 工具注册行为（`tool_registry.py` 的 6+3 工具）不变。
- 安全边界：无外部写入、无密钥处理变化。
- 兼容性要求：拆分前后同一输入产生相同 Run 状态序列、事件序列与报告内容。
- 性能/成本约束：不引入运行时开销（拆分仅为模块重组，不改调用路径深度）。
- 流程约束：沿用既定交付方式——每阶段独立 code review 通过后本地提交；不使用 Docker。

## 7. 风险与开放问题

| 问题 | 影响 | 处理方式 | 负责人/决策人 |
|---|---|---|---|
| God 方法拆分时引入隐蔽行为差异 | 主链回归 | 纯机械提取（不重写逻辑），全量 pytest + 相关专项测试门禁；harness 两文件放最后阶段 | ZCode 实现，用户 review |
| facade 长期留存形成新旧双入口 | 结构再次混乱 | facade 只做 re-export ≤80 行并在文件头注明"实现已迁移"；是否更新 importer 到新路径列为开放问题 2 | 用户决策 |
| 500–599 行文件（6 个：incremental_update 561、settings 536、manual_edit_manager 529、embedding_manager 527、entity_merger 526、graph_consistency_validator 520）未纳入 | 阈值边界争议 | 本期不做，记为 backlog | 用户决策 |
| tests/scripts 长文件（test_harness_phase3.py 705、evaluate_recovery_e2e.py 595） | 范围蔓延 | 本期不做 | 用户决策 |

开放问题（需确认）：
1. 阈值 600 行是否认可（备选 500）。
2. facade 策略：A. 全部保留 facade（importer 零改动，推荐）；B. 顺手更新部分内部 importer 到新路径并删 facade。

## 8. 成功标准

- [ ] Given 拆分完成，When `find src -name "*.py" | xargs wc -l | sort -rn`，Then 除 facade（≤80 行）外无 ≥600 行文件。
- [ ] Given 每阶段完成，When 运行全量测试（`python -m pytest`，不含需外部服务的用例，与 CI 等价），Then 结果与 T-101 前记录的基线一致（全绿）。
- [ ] Given 拆分完成，When `python -c "from deepresearch_agent.search.tool_registry import TOOL_REGISTRY, EXTRA_TOOL_FACTORIES"` 及两个 workflow driver import 冒烟，Then 不抛异常。
- [ ] 预期证据：每阶段在 acceptance.md 记录 pytest 摘要、wc -l 前后对比、review 结论。

## 9. 决策状态

- 状态：`approved`
- 用户确认：GraphRAG 保留（2026-11-06）；阈值 600 行、保留 facade 策略（AskUserQuestion 确认，2026-11-06）
- 日期：2026-11-06

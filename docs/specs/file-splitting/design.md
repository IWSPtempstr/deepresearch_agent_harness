# Design: T-014 长文件拆分

## 1. 现状与目标架构

现状：14 个 ≥600 行文件（见 proposal 第 5 节），核心问题是 God Class（单类 20+ 方法、单方法 300–850 行）与多职责混杂。

目标架构：**类不动、文件拆开**。每个原文件保留"类骨架"（imports + 类声明 + Mixin 继承 + 少量核心方法），方法组按主题移入同包或子包的 Mixin 模块。类名、导入路径、MRO、`self` 状态访问全部不变——importer 零改动。

## 2. 统一拆分模式

### 模式 A：Mixin 拆分（默认，用于 God Class）
```python
# 原文件（类骨架，≤200 行）
from deepresearch_agent.search.tool.deep_research.pipeline import DeepThinkingMixin
class DeepResearchTool(DeepThinkingMixin, ..., BaseSearchTool):
    """docstring 不变"""
# pipeline.py
class DeepThinkingMixin:
    def thinking(self, query): ...  # 原样搬移，禁止改逻辑
```
- **MRO 硬规则：Mixin 必须排在 ABC 基类之前**。`abc` 在类创建时沿 MRO `getattr` 检查抽象方法，基类在前会先命中抽象定义、忽略 Mixin 中的具体实现，导致类仍被判为 abstract（T-102 实测踩中，pytest 抓到）。
- **抽象 Mixin 必须继承 ABC**：携带 `@abstractmethod` 的 Mixin 若是普通类，其抽象方法不会进入子类的 `__abstractmethods__`（ABCMeta 只汇总 ABC 基类的清单），抽象契约缩水（T-105 实测踩中：BaseAgent 4→2，修复后恢复 4）。
- **docstring 与模块级赋值（如 `_LOGGER`）**：拆分库需跳过模块 docstring 再解析 import，并把被引用的模块级赋值作为 prologue 随模块携带（T-105 实测踩中 worker_coordinator/reflector）。
- Mixin 之间不得定义同名属性/方法；`__init__` 只出现在一个 Mixin（或类骨架）中。
- MRO 声明顺序按"初始化 → 检索 → 思考 → 流式 → 工厂"排列并在类骨架注释说明。

### 模式 B：模块函数提取（无状态纯函数）
直接移动为模块级函数；原模块 `from ... import x` re-import 保持旧路径可用（如 `workflow.py` 的 `_evidence_only_report`，tests 可能直接引用）。

### 硬性规则
1. 禁止改动：方法逻辑、签名、异常类型、日志/事件文案、print 语句。
2. 拆出模块 ≤500 行；类骨架 ≤200 行；纯 re-export facade ≤80 行。
3. 每个新文件头加一行注释：`# 由 <原文件> 拆分（T-014），逻辑与 <原位置> 完全一致`。
4. 不新增第三方依赖；不做顺手重构（重命名、类型标注、样式修改均不做）。

## 3. 逐文件设计

行号基于 2026-11-06 的 main（`fa38d29`），实现时以实际方法边界微调。

### T-101 `search/tool/deeper_research_tool.py`（2167 → 骨架 30）
`thinking()`(~838 行) 与 `thinking_stream()`(~812 行) 为单一方法，无法按方法边界拆到 ≤500 行，因此在方法内部原有 `步骤1-8` 注释接缝处做**阶段分解**：阶段体逐行原样搬移（脚本断言校验），仅新增 def/return/`ctx` 字典传递行（同步阶段用返回值、流式阶段用 `ctx` 传递 `think`/`query_id` 等跨界变量）。新子包 `search/tool/deeper_research/`（已有 branching.py/enhancer.py，追加）：

| 新模块 | Mixin | 内容 |
|---|---|---|
| `wiring.py` (125) | `DeeperInitMixin` | `__init__`(48–141)、`_log`(141–144) |
| `thinking_pipeline.py` (266) | `DeeperThinkingMixin` | `extract_keywords`(146–156)、`thinking` 编排器(169–178 原文 + 5 次阶段调用)、`_thinking_prepare`(179–361)、`_thinking_branches`(363–388) |
| `thinking_iterate.py` (405) | `DeeperIterateMixin` | `_thinking_iterate`＝原迭代主循环(390–778) |
| `thinking_synthesize.py` (234) | `DeeperSynthesizeMixin` | `_thinking_resolve`(780–812)、`_thinking_synthesize`(814–996，含无搜索早返回) |
| `search_entry.py` (102) | `DeeperSearchMixin` | `search`(998–1084)、`close`(2163–2167) |
| `tool_factories.py` (148) | `DeeperToolFactoryMixin` | `get_*`(1086–1199)、`_async_*`(1201–1220) |
| `streaming.py` (371) | `DeeperStreamingMixin` | `search_stream`(1222–1343)、`_progress`(1345–1348)、`thinking_stream` 编排器(1350–1359 + ctx 子生成器循环)、`_stream_preamble`(1360–1563) |
| `streaming_loop.py` (402) | `DeeperStreamIterateMixin` | `_stream_iterate`＝原流式迭代主循环(1565–1950) |
| `streaming_finalize.py` (231) | `DeeperStreamFinalizeMixin` | `_stream_finalize`＝原收尾(1952–2161，末尾 bare `return` 仍等价终止整个流) |

原文件 = imports + `class DeeperResearchTool(9 个 Mixin)`。行为等价关键点：`thinking` 的思考缓存早返回保留在编排器（原 175–177 行）；`_thinking_synthesize` 的两个 return 形状相同（都是 result dict）；流式阶段的子生成器循环中原 bare `return` 位于最后一个阶段，提前结束等价于整体终止。

### T-102 `search/tool/deep_research_tool.py`（1320 → 骨架 ~100）
新子包 `search/tool/deep_research/`：
| 新模块 | Mixin | 移入内容 |
|---|---|---|
| `wiring.py` | `DeepResearchInitMixin` | `__init__`(37–97)、`_setup_chains`(97–102)、`_progress`(102–107) |
| `retrieval_funcs.py` | `DeepRetrievalMixin` | `_parse_search_result`(123–178)、`_get_chunk_content`(178–204)、`_create_kb_retrieval_func`(204–342)、`_create_kg_retrieval_func`(342–383)、`_search_current_provider`(449–455)、`_provider_results_to_legacy`(455–477) |
| `thinking_pipeline.py` | `DeepThinkingMixin` | `extract_keywords`(107–123)、`_generate_final_answer`(383–413)、`_async_generate_next_query`/`_async_search`/`_async_extract_info`/`_async_generate_final_answer`(413–502)、`_log`(502)、`thinking`(507–738) |
| `streaming.py` | `DeepStreamingMixin` | `thinking_stream`(843–1165)、`search_stream`(1165–1259)、`_fix_answer`(1287–1309) |
| `tool_factories.py` | `DeepToolFactoryMixin` | `get_tool`(808)、`get_thinking_tool`(822)、`get_thinking_stream_tool`(1259–1287) |

原文件保留 `search`(738–808) 与 `close`(1309)。

### T-103 `search/tool/hybrid_tool.py`（661 → 骨架 169）
评审修订（2026-11-07）：`extract_keywords`(98 行)+`_retrieve_low_level_content`(207)+`_retrieve_high_level_content`(81) 合入单模块会超 500 行预算，故拆为两个同包模块：
- `search/tool/hybrid_retrieval.py`（372 行）— `HybridRetrievalMixin`：`db_query`、`_vector_search`、`_fallback_text_search`、`_retrieve_low_level_content`、`_retrieve_high_level_content`。
- `search/tool/hybrid_keywords.py`（149 行）— `HybridKeywordsMixin`：`extract_keywords`、`get_global_tool`。
- 原文件保留 `HybridSearchTool(HybridRetrievalMixin, HybridKeywordsMixin, BaseSearchTool)` + `__init__`/`_setup_chains`/`structured_search`/`search`/`close`。

### T-104 `search/tool/reasoning/`（3 文件 2279 → 骨架 ~220）
- `chain_of_exploration.py`(808) → 同级 4 模块：
  - `chain_strategy.py` — `ChainStrategyMixin`：`_generate_exploration_strategy`(174)、`_calculate_adaptive_width`(238)、`_estimate_query_complexity`(266)。
  - `chain_scoring.py` — `ChainScoringMixin`：`_score_neighbors_enhanced`(339)、`_get_entity_type`(422)、`_rank_content_by_relevance`(701)。
  - `chain_info.py` — `ChainInfoMixin`：`_get_neighbors`(296)、`_get_entity_info`(555)、`_get_relationship_info`(590)、`_get_content_info`(632)、`_get_community_info`(667)。
  - `chain_explorer.py` — `ChainExplorerMixin`：`explore`(33–174)、`_decide_next_step_with_memory`(455–555)、`explore_async`(745)、`_explore_async_impl`(786)。
  - 原文件保留 `ChainOfExplorationSearcher(ChainStrategyMixin, ChainScoringMixin, ChainInfoMixin, ChainExplorerMixin)` + `__init__`。
- `thinking.py`(761)：类骨架保留 `__init__`(32–48)+`initialize_with_query`(48–58)；移出：
  - `thinking_hypotheses.py` — `generate_initial_thinking`(58)、`generate_hypotheses`(71)、`_extract_hypotheses_fallback`(109)、`verify_hypothesis`(160)、`_assess_verification_status`(193)、`think_deeper`(223)、`update_thinking_based_on_verification`(266)、`integrate_thinking_process`(319)。
  - `thinking_branching.py` — `add_reasoning_step`(359)、`branch_reasoning`(377)、`switch_branch`(402)、`merge_branches`(417)、`counter_factual_analysis`(460)、`_extract_conclusion`(503)。
  - `thinking_state.py` — `generate_next_query`(576)、`add_ai_message`(632)、`add_human_message`(641)、`update_continue_message`(650)、`prepare_truncated_reasoning`(673)、`get_full_thinking`(725)、`has_executed_query`(742)、`add_executed_query`(754)。
  - `thinking_text.py` — `remove_query_tags`、`remove_result_tags`、`extract_queries`：评审修订（2026-11-07）——原定模式 B 模块函数，实现为 `ThinkingTextMixin`（3 方法），调用方 `self.remove_query_tags` 经 Mixin 绑定零改动，安全等价。
- `evidence.py`(710)：骨架保留 `__init__`(15–26)、`start_new_query`(26–50)、`add_reasoning_step`(50–86)；移出：
  - `evidence_record.py` — `add_evidence`(86)、`add_evidence_with_confidence`(127)、`_update_citation_index`(166)、`_extract_key_phrases`(185)。
  - `evidence_contradiction.py` — `detect_contradictions`(229)、`_extract_numbers_with_context`(297)、`_context_similarity`(340)、`_detect_semantic_contradiction`(364)。
  - `evidence_citations.py` — `generate_citations`(409)、`_extract_key_statements`(444)、`_find_matching_evidence`(475)、`_add_citations_to_answer`(517)。
  - `evidence_summary.py` — `get_reasoning_chain`(551)、`get_step_evidence`(606)、`summarize_reasoning`(630)、`get_evidence_source_stats`(684)。

### T-105 `agents/`（4 文件 2865 → 骨架 ~450）
- `agents/base.py`(792)：骨架保留 `BaseAgent(ABC)` + `__init__`(24–79)、`_setup_tools`(79)、`_log_execution`(174)、`_log_performance`(183)、`_extract_keywords`(233)、`_validate_answer`(748)、`close`(784)。
  - `agent_graph.py` — `AgentGraphMixin`：`_setup_graph`(83)、`_stream_process`(117)、`_add_retrieval_edges`(170)、`_agent_node`(194)、`_generate_node`(238)、`_generate_node_stream`(242)、`_generate_node_async`(263)。
  - `agent_cache.py` — `AgentCacheMixin`：`check_fast_cache`(281)、`_check_all_caches`(303)、`mark_answer_quality`(663)、`clear_cache_for_query`(684)。
  - `agent_ask.py` — `AgentAskMixin`：`ask_with_trace`(361–470)、`ask`(470–529)、`ask_stream`(529–663)。
- `agents/deep_research_agent.py`(640)：骨架保留 `__init__`(38–94)、`_setup_chains`(94)、`_setup_tools`(98)、`_add_retrieval_edges`(123)、`is_deeper_tool`(589)、`close`(633)。
  - `deep_research_agent_nodes.py` — `DeepResearchNodesMixin`：`_generate_node`(133–234)。
  - `deep_research_agent_asks.py` — `DeepResearchAskMixin`：`ask`(234)、`ask_with_thinking`(268)、`ask_stream`(306–399)。
  - `deep_research_agent_analysis.py` — `DeepResearchAnalysisMixin`：`explore_knowledge`(399)、`analyze_reasoning_chain`(486)、`detect_contradictions`(513–589)。
- `executor/reflector.py`(730)：骨架保留 `__init__`(37–47)、`can_handle`(47)、`_update_state`(654)。评审修订（2026-11-07）：`execute_task`(167 行) 留骨架会超 200 预算，拆入 `reflector_execute.py`。
  - `reflector_answer.py` — `ReflectionAnswerMixin`：`_resolve_query_answer`(217)、`_lookup_answer_from_state`(255)、`_extract_answer_from_record`(286)、`_fallback_from_intermediate`(303)、`_extract_from_dict`(334)、`_extract_answer_content`(591)、`_extract_possible_text`(611)。
  - `reflector_keywords.py` — `ReflectionKeywordsMixin`：`_derive_keyword_suggestions`(354)、`_resolve_evaluation_text`(400)、`_build_reference_keywords`(437)、`_tokenize_reference_text`(486)、`_accumulate_keywords_from_entries`(512)、`_accumulate_keywords_from_strings`(531)、`_normalize_keyword_token`(627)、`_counter_to_list`(636)、`_contains_chinese`(651)。
  - `reflector_evidence.py` — `ReflectionEvidenceMixin`：`_collect_evidence_from_records`(546)、`_extract_evidence_text`(572)。
- `executor/worker_coordinator.py`(703)：骨架保留 `__init__`(46–77)、`register_executor`(77)、`execute_plan`(81–123)、`_select_executor`(486)、`_prepare_tasks`(475)。
  - `coordinator_modes.py` — `CoordinatorModesMixin`：`_resolve_execution_mode`(123)、`_execute_sequential`(147)、`_execute_parallel`(185–320)。
  - `coordinator_tasks.py` — `CoordinatorTasksMixin`：`_execute_isolated_task`(320)、`_merge_isolated_state`(341)、`_execute_single_task`(398)、`_create_failure_record`(492)、`_check_dependencies`(540)、`_handle_reflection_retry`(607)。

### T-106 `reporter/base_reporter.py`（1163 → 骨架 ~160）
- `reporter/report_pipeline.py` — `ReportPipelineMixin`：`generate_report`(171–336)、`recheck_consistency`(336)、`_generate_sections_traditional`(535–597)、`_assemble_report`(792–807)。
- `reporter/card_sections.py` — `CardSectionsMixin`：`_collect_evidence`(345)、`_build_plan_summary`(374)、`_build_evidence_summary`(386)、`_build_card_outline_summary`(411)、`_load/_save_evidence_card_cache`(420–447)、`_generate_sections_from_cards`(447–524)、`_collect_section_evidence`(688–702)。
- `reporter/mapreduce_sections.py` — `MapReduceSectionsMixin`：`_should_use_mapreduce`(524)、`_generate_sections_mapreduce`(597–688)、`_map_evidence_batches`(702–757)、`_assemble_report_mapreduce`(757–780)、`_ensure_mapreduce_components`(780–792)。
- `reporter/report_formatting.py` — `ReportFormattingMixin`：`_format_evidence_for_check`(807)、`_append_evidence_annex`(824–889)、`_normalize_heading_text`(889)、`_sanitize_section_text`(893)、`_normalize_section_references`(909)、`_format_references`(933)、`_resolve_report_id`(952)、`_build_evidence_fingerprint`(956)。
- 原文件保留 pydantic schema（`ReporterConfig`/`SectionContent`/`ReportResult`，68–116）+ `BaseReporter(ReportPipelineMixin, CardSectionsMixin, MapReduceSectionsMixin, ReportFormattingMixin)` + `__init__`(121–171)。

### T-107 `integrations/build/incremental_graph_builder.py`（964 → 骨架 ~90）
- `incremental_file_changes.py` — `GraphFileChangesMixin`：`detect_changes`(91)、`process_new_files`(100–315)、`process_deleted_files`(572–677)。
- `incremental_merge.py` — `GraphMergeMixin`：`integrate_new_entities`(315)、`integrate_new_relationships`(365)、`merge_graph_structures`(455–526)。
- `incremental_io.py` — `GraphIoMixin`：`update_changed_file_embeddings`(526–572)、`export_graph_structure`(677–733)、`import_graph_structure`(733–804)。
- `incremental_stats.py` — `GraphStatsMixin`：`get_graph_statistics`(804)、`display_graph_statistics`(859)、`process_incremental_update`(886–964)。

### T-108 `harness/`（主链，最后拆）
- `workflow.py`(731)：
  - `harness/report_fallbacks.py` — **模式 B**：`_evidence_only_report`(50–94) 整体移入（无状态纯函数）；`workflow.py` re-import 保持 `harness.workflow._evidence_only_report` 路径可用。
  - `harness/drivers_plan_execute.py` — `PlanExecuteReportDriver`(94–537) 整类移入（含 `_build_reserved_budget_report`/`_build_compact_report`/`repair_report`）。
  - `harness/drivers_deep_research.py` — `DeepResearchDriver`(537–731) 整类移入。
  - `workflow.py` 保留 `WorkflowDriver` Protocol(36–48) + 两个类的 re-export（`run_service.py:19` 的 `from deepresearch_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver` 零改动）。
- `runtime.py`(718)：`HarnessRuntime` 类骨架保留 `__init__`(35–75)、`_attach_prefix_usage`(75)、`_load_context`(497)、`_safe_stage_after`(536)、`_transition`(548)、`_pause_at_safe_boundary`(572)、`_assert_not_cancelled`(585)。
  - `runtime_execution.py` — `RuntimeExecutionMixin`：`execute_run`(89)、`_execute_run_inner`(114–497，**383 行状态机主循环原样搬移，内部禁止再拆**)、`_run_with_heartbeat`(591–623)。
  - `runtime_persistence.py` — `RuntimePersistMixin`：`_persist_plan`(623)、`_persist_execution`(633–685)、`_token_usage_total`(685)、`_save_report_artifact`(703)、`_apply_artifact_edit`(710)。

## 4. 兼容性策略

- 所有类留在原模块（Mixin 继承）→ 既有 import（`tool_registry.py`、`deep_research_agent.py`、`retrieval_executor.py`、30+ 处、全部 tests）零改动。
- 仅 `workflow.py` 的两个 driver 移到新模块，由 `workflow.py` re-export 兜底。
- `reasoning/__init__.py` 与 `search/tool/__init__.py` 的延迟导入映射不变。

## 5. 验证策略与门禁

每阶段（P0 基线之后）依次执行，全绿才进 review：
1. `PYTHONPATH=src:. .venv/bin/python -m pytest tests/ -q` —— 与基线（P0 记录）一致的全绿；
2. import 冒烟：`python -c "from deepresearch_agent.search.tool_registry import TOOL_REGISTRY, EXTRA_TOOL_FACTORIES; from deepresearch_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver"`（T-108 后）；
3. 行数门禁：`find src -name "*.py" | xargs wc -l | sort -rn | head -5`，确认阶段目标文件已 ≤500/骨架 ≤200；
4. review 通过后本地提交（`refactor(split): T-10x <文件> 拆分`），review 修复不单独成提交、与阶段同提交。

## 6. 不采用的替代方案

- **删除式瘦身**（上轮提议的删 evaluation/、DeeperResearchTool 等）：用户已决策保留 GraphRAG、以拆分代替删除。
- **更新 importer 后删 facade**：用户已选择保留 facade；Mixin 模式下除 workflow 外甚至无需 facade。
- ** collaborating 对象提取（组合替代继承）**：更"纯 OO"但要重写 `self` 状态传递，行为回归风险高，违背"纯机械提取"约束；留作后续独立提案。

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| Mixin 移动时遗漏闭包/装饰器依赖（如 `@staticmethod`、模块级常量引用） | 搬移时连同其依赖的模块级常量一起移动或 import；pytest 全量门禁 |
| `workflow.py` driver 移动造成循环导入 | drivers 模块只 import Protocol/公共类型，不 import workflow；由测试暴露 |
| `runtime._execute_run_inner` 383 行搬移抄写错误 | 使用整块文本移动（不逐行重写）；`tests/harness/` 全覆盖 |
| 阶段中断留下半拆分状态 | 每阶段一个提交，回滚 = revert 单提交 |

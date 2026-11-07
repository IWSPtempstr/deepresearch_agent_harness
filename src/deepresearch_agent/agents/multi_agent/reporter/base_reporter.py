"""
Reporter编排基类

负责串联纲要生成、章节写作、引用整理与一致性校验
"""

# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import List, Dict, Any, Optional, Iterable, Tuple
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import (
    OutlineBuilder,
    ReportOutline,
    SectionOutline,
)
from deepresearch_agent.agents.multi_agent.reporter.section_writer import (
    SectionWriter,
    SectionWriterConfig,
    SectionDraft,
)
from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import (
    EvidenceCard,
    EvidenceCardPipeline,
)
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import (
    ConsistencyChecker,
    ConsistencyCheckResult,
)
from deepresearch_agent.agents.multi_agent.reporter.formatter import CitationFormatter
from deepresearch_agent.agents.multi_agent.reporter.mapreduce import (
    EvidenceMapper,
    EvidenceSummary,
    SectionReducer,
    ReduceStrategy,
    ReportAssembler,
)
from deepresearch_agent.cache_manager.manager import CacheManager
from deepresearch_agent.config.settings import (
    MULTI_AGENT_DEFAULT_REPORT_TYPE,
    MULTI_AGENT_ENABLE_CONSISTENCY_CHECK,
    MULTI_AGENT_ENABLE_MAPREDUCE,
    MULTI_AGENT_MAPREDUCE_THRESHOLD,
    MULTI_AGENT_MAX_TOKENS_PER_REDUCE,
    MULTI_AGENT_ENABLE_PARALLEL_MAP,
    MULTI_AGENT_SECTION_MAX_EVIDENCE,
    MULTI_AGENT_SECTION_MAX_CONTEXT_CHARS,
    EVIDENCE_CARD_MAX_TOKENS,
    REPORT_BATCH_BUDGET_RATIO,
    REPORT_BATCH_DIGEST_MAX_TOKENS,
    REPORT_MAX_MODEL_CALLS,
    REPORT_MAX_SECTIONS,
    REPORT_SECTION_EVIDENCE_BUDGET,
)

from deepresearch_agent.agents.multi_agent.reporter.report_pipeline import ReportPipelineMixin
from deepresearch_agent.agents.multi_agent.reporter.card_sections import CardSectionsMixin
from deepresearch_agent.agents.multi_agent.reporter.mapreduce_sections import MapReduceSectionsMixin
from deepresearch_agent.agents.multi_agent.reporter.report_formatting import ReportFormattingMixin
from deepresearch_agent.agents.multi_agent.reporter.report_cache import ReportCacheMixin
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import ReporterConfig, SectionContent, ReportResult

class BaseReporter(ReportPipelineMixin, CardSectionsMixin, MapReduceSectionsMixin, ReportFormattingMixin, ReportCacheMixin):
    """
    Reporter基类，串联各个子组件
    """

    def __init__(
        self,
        config: Optional[ReporterConfig] = None,
        *,
        outline_builder: Optional[OutlineBuilder] = None,
        section_writer: Optional[SectionWriter] = None,
        consistency_checker: Optional[ConsistencyChecker] = None,
        citation_formatter: Optional[CitationFormatter] = None,
        cache_manager: Optional[CacheManager] = None,
        evidence_mapper: Optional[EvidenceMapper] = None,
        section_reducer: Optional[SectionReducer] = None,
        report_assembler: Optional[ReportAssembler] = None,
    ) -> None:
        if config is None:
            section_writer_config = SectionWriterConfig(
                max_evidence_per_call=MULTI_AGENT_SECTION_MAX_EVIDENCE,
                max_previous_context_chars=MULTI_AGENT_SECTION_MAX_CONTEXT_CHARS,
            )
            config = ReporterConfig(
                default_report_type=MULTI_AGENT_DEFAULT_REPORT_TYPE,
                enable_consistency_check=MULTI_AGENT_ENABLE_CONSISTENCY_CHECK,
                enable_mapreduce=MULTI_AGENT_ENABLE_MAPREDUCE,
                mapreduce_evidence_threshold=MULTI_AGENT_MAPREDUCE_THRESHOLD,
                max_tokens_per_reduce=MULTI_AGENT_MAX_TOKENS_PER_REDUCE,
                enable_parallel_map=MULTI_AGENT_ENABLE_PARALLEL_MAP,
                section_writer=section_writer_config,
                evidence_card_max_tokens=EVIDENCE_CARD_MAX_TOKENS,
                section_evidence_budget=REPORT_SECTION_EVIDENCE_BUDGET,
                batch_budget_ratio=REPORT_BATCH_BUDGET_RATIO,
                batch_digest_max_tokens=REPORT_BATCH_DIGEST_MAX_TOKENS,
                max_report_sections=REPORT_MAX_SECTIONS,
                max_report_model_calls=REPORT_MAX_MODEL_CALLS,
            )
        self.config = config
        self._outline_builder = outline_builder or OutlineBuilder()
        self._section_writer = section_writer or SectionWriter(config=self.config.section_writer)
        self._consistency_checker = consistency_checker or ConsistencyChecker()
        self._citation_formatter = citation_formatter or CitationFormatter()
        self._cache_manager = cache_manager
        self._evidence_mapper = evidence_mapper
        self._section_reducer = section_reducer
        self._report_assembler = report_assembler
        self._evidence_card_pipeline = EvidenceCardPipeline(
            card_max_tokens=self.config.evidence_card_max_tokens,
            section_token_budget=self.config.section_evidence_budget,
            batch_budget_ratio=self.config.batch_budget_ratio,
            digest_max_tokens=self.config.batch_digest_max_tokens,
            max_cards_per_section_call=self.config.section_writer.max_evidence_per_call,
        )

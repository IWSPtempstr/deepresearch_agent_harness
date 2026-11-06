# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""报告层 pydantic schema（原 base_reporter.py 模块级类，整体迁出）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
from pydantic import BaseModel, Field
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
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import (
    ConsistencyChecker,
    ConsistencyCheckResult,
)
from deepresearch_agent.agents.multi_agent.reporter.mapreduce import (
    EvidenceMapper,
    EvidenceSummary,
    SectionReducer,
    ReduceStrategy,
    ReportAssembler,
)


class ReporterConfig(BaseModel):
    """
    Reporter层配置
    """
    default_report_type: str = Field(default="long_document", description="默认报告类型")
    citation_style: str = Field(default="default", description="引用格式类型")
    max_evidence_summary: int = Field(default=30, description="纲要生成时展示的最大证据条数")
    section_writer: SectionWriterConfig = Field(default_factory=SectionWriterConfig, description="章节写作配置")
    enable_consistency_check: bool = Field(default=True, description="是否启用一致性检查")
    enable_mapreduce: bool = Field(default=True, description="是否启用Map-Reduce写作模式")
    reduce_strategy: ReduceStrategy = Field(default=ReduceStrategy.TREE, description="章节Reduce策略")
    max_tokens_per_reduce: int = Field(default=4000, ge=1000, description="Reduce阶段单次调用的最大token估算")
    enable_parallel_map: bool = Field(default=True, description="证据Map阶段是否启用并行")
    mapreduce_evidence_threshold: int = Field(default=20, ge=0, description="触发Map-Reduce模式的证据数量阈值")
    evidence_card_max_tokens: int = Field(default=200, ge=60, description="单张 Evidence Card 的最大 Token")
    section_evidence_budget: int = Field(default=12000, ge=1000, description="单章节 Evidence Card 上下文预算")
    batch_budget_ratio: float = Field(default=0.70, ge=0.4, le=0.9, description="批处理占章节预算比例")
    batch_digest_max_tokens: int = Field(default=600, ge=100, description="单个 Digest 最大 Token")
    max_report_sections: int = Field(default=6, ge=1, description="报告最大章节数")
    max_report_model_calls: int = Field(default=15, ge=1, description="报告阶段最大模型调用数")


class SectionContent(BaseModel):
    """
    章节内容包装
    """
    section_id: str = Field(description="章节ID")
    title: str = Field(description="章节标题")
    content: str = Field(description="章节内容（Markdown）")
    used_evidence_ids: List[str] = Field(default_factory=list, description="引用的证据ID")


class ReportResult(BaseModel):
    """
    Reporter最终结果
    """
    outline: ReportOutline = Field(description="报告纲要")
    sections: List[SectionContent] = Field(description="章节内容列表")
    final_report: str = Field(description="最终报告Markdown文本")
    references: Optional[str] = Field(default=None, description="引用列表Markdown")
    consistency_check: Optional[ConsistencyCheckResult] = Field(default=None, description="一致性检查结果")
    evidence_cards: List[Dict[str, Any]] = Field(default_factory=list, description="全量 Evidence Card")
    evidence_routing: Dict[str, List[str]] = Field(default_factory=dict, description="全量章节路由")
    evidence_digests: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict, description="章节 Digest")
    evidence_card_coverage: Dict[str, Any] = Field(default_factory=dict, description="全量覆盖结果")
    report_metrics: Dict[str, Any] = Field(default_factory=dict, description="报告阶段指标")

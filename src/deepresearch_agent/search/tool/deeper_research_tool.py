# 由 search/tool/deeper_research_tool.py 拆分（T-014）：方法组已按主题迁至
# deeper_research/ 子包的 Mixin（阶段体与原文件逐行一致），本文件仅保留类声明与导入路径。
from deepresearch_agent.search.tool.deeper_research.wiring import DeeperInitMixin
from deepresearch_agent.search.tool.deeper_research.search_entry import DeeperSearchMixin
from deepresearch_agent.search.tool.deeper_research.tool_factories import DeeperToolFactoryMixin
from deepresearch_agent.search.tool.deeper_research.thinking_pipeline import DeeperThinkingMixin
from deepresearch_agent.search.tool.deeper_research.thinking_iterate import DeeperIterateMixin
from deepresearch_agent.search.tool.deeper_research.thinking_synthesize import DeeperSynthesizeMixin
from deepresearch_agent.search.tool.deeper_research.streaming import DeeperStreamingMixin
from deepresearch_agent.search.tool.deeper_research.streaming_loop import DeeperStreamIterateMixin
from deepresearch_agent.search.tool.deeper_research.streaming_finalize import DeeperStreamFinalizeMixin


class DeeperResearchTool(
    DeeperInitMixin,
    DeeperSearchMixin,
    DeeperToolFactoryMixin,
    DeeperThinkingMixin,
    DeeperIterateMixin,
    DeeperSynthesizeMixin,
    DeeperStreamingMixin,
    DeeperStreamIterateMixin,
    DeeperStreamFinalizeMixin,
):
    """
    增强版深度研究工具

    整合社区感知、动态知识图谱和Chain of Exploration等功能，
    提供更全面的深度研究能力，并充分利用所有高级推理功能
    """

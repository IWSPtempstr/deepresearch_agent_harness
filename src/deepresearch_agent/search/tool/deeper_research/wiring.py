# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""__init__ 组件装配与共享日志（原 DeeperResearchTool.__init__/_log）。"""

import os
from deepresearch_agent.config.prompts import (
    system_template_build_graph,
    human_template_build_graph,
)
from deepresearch_agent.config.settings import entity_types, relationship_types
from deepresearch_agent.graph.core import connection_manager
from deepresearch_agent.graph.extraction.entity_extractor import EntityRelationExtractor
from deepresearch_agent.models.get_models import get_llm_model, get_embeddings_model
from deepresearch_agent.search.tool.deep_research_tool import DeepResearchTool
from deepresearch_agent.search.tool.hybrid_tool import HybridSearchTool
from deepresearch_agent.search.tool.reasoning.chain_of_exploration import (
    ChainOfExplorationSearcher,
)
from deepresearch_agent.search.tool.reasoning.community_enhance import (
    CommunityAwareSearchEnhancer,
)
from deepresearch_agent.search.tool.reasoning.evidence import EvidenceChainTracker
from deepresearch_agent.search.tool.reasoning.kg_builder import (
    DynamicKnowledgeGraphBuilder,
)
from deepresearch_agent.search.tool.reasoning.thinking import ThinkingEngine


class DeeperInitMixin:
    def __init__(self, config=None, llm=None, embeddings=None, graph=None, provider=None, run_id=None):
        """
        初始化增强版深度研究工具
        
        Args:
            config: 配置参数
            llm: 语言模型
            embeddings: 嵌入模型
            graph: 图数据库连接
        """
        # 关键词缓存
        self._keywords_cache = {}

        # 初始化基础组件
        self.llm = llm or get_llm_model()
        self.embeddings = embeddings or get_embeddings_model()
        self.graph = graph or connection_manager.get_connection()

        self.hybrid_tool = HybridSearchTool()
        
        # 初始化增强模块
        # 1. 社区感知搜索增强器
        self.community_search = CommunityAwareSearchEnhancer(
            self.graph, 
            self.embeddings, 
            self.llm
        )
        
        # 2. 动态知识图谱构建器
        self.knowledge_builder = DynamicKnowledgeGraphBuilder(
            self.graph,
            EntityRelationExtractor(self.llm, system_template_build_graph, human_template_build_graph, entity_types, relationship_types),
        )
        
        # 3. Chain of Exploration检索器
        self.chain_explorer = ChainOfExplorationSearcher(
            self.graph,
            self.llm,
            self.embeddings
        )
        
        # 4. 证据链跟踪器
        self.evidence_tracker = EvidenceChainTracker()
        
        # 5. 继承原有的深度研究工具功能
        self.deep_research = DeepResearchTool(provider=provider, run_id=run_id)

        # 6. 查询生成器
        self.query_generator = self.deep_research.query_generator
        
        # 缓存设置
        self.enable_cache = True
        self.cache_dir = "./cache/deeper_research"
        
        # 确保缓存目录存在
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
                
        # 添加执行日志容器
        self.execution_logs = []

        # 迭代进度回调（由 DeepResearchDriver 注入）
        self.progress_callback = None
        
        # 添加性能指标跟踪
        self.performance_metrics = {"total_time": 0}
        
        # 记录当前查询的上下文信息
        self.current_query_context = {}
        
        # 记录已探索的查询分支
        self.explored_branches = {}
        
        # 共享思考引擎实例，避免重复初始化
        if hasattr(self.deep_research, 'thinking_engine'):
            self.thinking_engine = self.deep_research.thinking_engine
        else:
            self.thinking_engine = ThinkingEngine(self.llm)
            self.deep_research.thinking_engine = self.thinking_engine
        
        # 添加各种缓存字典
        self._search_cache = {}
        self._thinking_cache = {}
        self._contradiction_cache = {}
        self._hypotheses_cache = {}
        self._counter_cache = {}
        self._coe_cache = {}
        self._specific_coe_cache = {}
        self._contradiction_detailed_cache = {}
        self._stream_search_cache = {}
        self._stream_thinking_cache = {}
        self._subquery_cache = {}

    def _log(self, message):
        """记录执行日志"""
        self.execution_logs.append(message)
        # print(message)  # 可选：同时打印到控制台

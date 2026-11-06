# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""组件装配、链式提示初始化与进度回调（原 __init__/_setup_chains/_progress）。"""

from typing import Dict, List, Any, Optional, AsyncGenerator
import uuid
from deepresearch_agent.config.prompts import BEGIN_SEARCH_QUERY, BEGIN_SEARCH_RESULT, END_SEARCH_RESULT, MAX_SEARCH_LIMIT, \
    END_SEARCH_QUERY, RELEVANT_EXTRACTION_PROMPT, SUB_QUERY_PROMPT, FOLLOWUP_QUERY_PROMPT, FINAL_ANSWER_PROMPT
from deepresearch_agent.search.tool.reasoning.thinking import ThinkingEngine
from deepresearch_agent.search.tool.reasoning.validator import AnswerValidator
from deepresearch_agent.search.tool.reasoning.search import DualPathSearcher, QueryGenerator
from deepresearch_agent.config.settings import KB_NAME
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.retrieval.base import RetrievalProvider, SearchFilters, ToolCallContext, run_async_from_sync


class DeepResearchInitMixin:
    def __init__(self, provider: Optional[RetrievalProvider] = None, *, run_id: Optional[str] = None):
        """初始化深度研究工具"""
        super().__init__(
            cache_dir="./cache/deep_research",
            enable_graph=not (provider is not None and provider.mode == SourceMode.WEB),
            enable_vector_cache=False if provider is not None and provider.mode == SourceMode.WEB else None,
        )
        self.retrieval_provider = provider
        self.run_id = run_id or f"legacy_{uuid.uuid4().hex}"
        self.provider_results = []
        self.provider_calls = []
        self.progress_callback = None  # 由 DeepResearchDriver 注入，上报迭代进度事件

        # 关键词缓存
        self._keywords_cache = {}
        
        # Web Run 不实例化任何 GraphRAG 工具，避免私有源连接和隐式降级。
        if provider is not None and provider.mode == SourceMode.WEB:
            self.hybrid_tool = None
            self.global_tool = None
            self.local_tool = None
        else:
            from deepresearch_agent.search.tool.hybrid_tool import HybridSearchTool
            from deepresearch_agent.search.tool.local_search_tool import LocalSearchTool
            from deepresearch_agent.search.tool.global_search_tool import GlobalSearchTool
            self.hybrid_tool = HybridSearchTool()  # 用于关键词提取和混合搜索
            self.global_tool = GlobalSearchTool()  # 用于社区检索
            self.local_tool = LocalSearchTool()    # 用于本地搜索
        
        # 初始化思考引擎
        self.thinking_engine = ThinkingEngine(self.llm)
        
        # 初始化查询生成器
        self.query_generator = QueryGenerator(
            self.llm, 
            SUB_QUERY_PROMPT, 
            FOLLOWUP_QUERY_PROMPT
        )
        
        # 初始化答案验证器
        self.validator = AnswerValidator(self.extract_keywords)
        
        # 初始化搜索器
        self._kb_retrieve = self._create_kb_retrieval_func()
        self._kg_retrieve = self._create_kg_retrieval_func()
        self.dual_searcher = DualPathSearcher(
            self._kb_retrieve, 
            self._kg_retrieve, 
            KB_NAME
        )
        
        # 存储重要信息
        self.all_retrieved_info = []
        
        # 设置最大迭代次数
        self.max_iterations = MAX_SEARCH_LIMIT
        
        # 用于存储执行日志
        self.execution_logs = []

    def _setup_chains(self):
        """设置处理链"""
        # 深度研究工具主要依赖于其他工具的功能和思考方法
        pass

    async def _progress(self, kind: str, **extra) -> None:
        """向驱动回调上报迭代进度（回调由 DeepResearchDriver 注入）"""
        if self.progress_callback is not None:
            await self.progress_callback({"kind": kind, **extra})

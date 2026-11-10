# 由 agents/deep_research_agent.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _setup_chains, _setup_tools, _add_retrieval_edges, _extract_keywords, is_deeper_tool, close。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import List, Dict, Optional
from deepresearch_agent.search.tool.deep_research_tool import DeepResearchTool 
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.retrieval.base import RetrievalProvider

from deepresearch_agent.agents.base import BaseAgent
from deepresearch_agent.agents.deep_research_agent_nodes import DeepResearchNodesMixin
from deepresearch_agent.agents.deep_research_agent_asks import DeepResearchAskMixin
from deepresearch_agent.agents.deep_research_agent_analysis import DeepResearchAnalysisMixin

class DeepResearchAgent(DeepResearchNodesMixin, DeepResearchAskMixin, DeepResearchAnalysisMixin, BaseAgent):
    """
    深度研究Agent
    
    该Agent扩展了基础Agent架构，使用多回合的思考、搜索和推理来解决复杂问题。
    主要特点：
    1. 显式推理过程
    2. 迭代式搜索
    3. 高质量知识整合
    4. 支持流式输出
    5. 社区感知和知识图谱增强
    6. 多分支推理和矛盾检测
    7. 知识图谱探索能力
    8. 推理链分析
    """
    

    def __init__(self, use_deeper_tool=True, *, retrieval_provider: Optional[RetrievalProvider] = None, run_id: Optional[str] = None):
        """
        初始化增强版深度研究Agent
        
        Args:
            use_deeper_tool: 是否使用增强版研究工具
        """
        # 初始化研究工具
        self.retrieval_provider = retrieval_provider
        self.run_id = run_id
        self.use_deeper_tool = bool(use_deeper_tool and (retrieval_provider is None or retrieval_provider.mode == SourceMode.GRAPHRAG))
        
        if self.use_deeper_tool:
            # 使用增强版研究工具
            try:
                from deepresearch_agent.search.tool.deeper_research_tool import DeeperResearchTool
                self.research_tool = DeeperResearchTool(provider=retrieval_provider, run_id=run_id)
                print("已加载增强版深度研究工具")
                
                # 加载额外工具
                self.exploration_tool = self.research_tool.get_exploration_tool()
                self.reasoning_analysis_tool = self.research_tool.get_reasoning_analysis_tool()
                self.stream_tool = self.research_tool.get_stream_tool()
            except Exception as e:
                print(f"加载增强版研究工具失败: {e}，将使用标准版")
                self.research_tool = DeepResearchTool(provider=retrieval_provider, run_id=run_id)
                self.use_deeper_tool = False
                
                # 标准版工具
                self.stream_tool = self.research_tool.get_thinking_stream_tool()
        else:
            # 使用标准版研究工具
            self.research_tool = DeepResearchTool(provider=retrieval_provider, run_id=run_id)
            self.stream_tool = self.research_tool.get_thinking_stream_tool()
        
        # 设置缓存目录
        self.cache_dir = "./cache/enhanced_research_agent"
        
        # 设置查看推理过程的模式
        self.show_thinking = False
        
        # 添加会话上下文
        self.session_context = {}
        
        # 加载社区知识增强器
        if use_deeper_tool and hasattr(self.research_tool, 'community_search'):
            self.community_enhancer = self.research_tool.community_search
        else:
            self.community_enhancer = None

        # 调用父类构造函数
        super().__init__(
            cache_dir=self.cache_dir,
            enable_vector_cache=False if retrieval_provider is not None and retrieval_provider.mode == SourceMode.WEB else None,
        )

    def _setup_chains(self):
        """设置处理链 - 由于我们直接使用工具，不需要特别设置"""
        pass

    def _setup_tools(self) -> List:
        """设置工具，根据模式选择不同的工具组合"""
        tools = []
        
        # 基础研究工具 - 根据显示思考过程的模式选择
        if self.show_thinking:
            # 思考过程可见模式
            tools.append(self.research_tool.get_thinking_tool())
        else:
            # 标准模式
            tools.append(self.research_tool.get_tool())
        
        # 添加增强工具 - 只有使用增强版深度研究工具时才有
        if self.use_deeper_tool:
            # 添加知识图谱探索工具
            tools.append(self.exploration_tool)
            
            # 添加推理链分析工具
            tools.append(self.reasoning_analysis_tool)
        
        # 流式工具总是添加
        tools.append(self.stream_tool)
            
        return tools

    def _add_retrieval_edges(self, workflow):
        """添加从检索到生成的边"""
        # 简单的从检索直接到生成
        workflow.add_edge("retrieve", "generate")

    def _extract_keywords(self, query: str) -> Dict[str, List[str]]:
        """从查询中提取关键词"""
        # 使用研究工具的关键词提取功能
        return self.research_tool.extract_keywords(query)

    def is_deeper_tool(self, use_deeper=True):
        """
        切换是否使用增强版研究工具
        
        参数:
            use_deeper: 是否使用增强版
            
        返回:
            str: 状态消息
        """
        # 切换工具
        if use_deeper and self.retrieval_provider is not None and self.retrieval_provider.mode == SourceMode.WEB:
            return "Web 信息源不启用知识图谱专用增强工具，继续使用标准 DeepResearch"
        self.use_deeper_tool = use_deeper
        
        if use_deeper:
            # 切换到增强版
            try:
                from deepresearch_agent.search.tool.deeper_research_tool import DeeperResearchTool
                self.research_tool = DeeperResearchTool(provider=self.retrieval_provider, run_id=self.run_id)
                
                # 加载额外工具
                self.exploration_tool = self.research_tool.get_exploration_tool()
                self.reasoning_analysis_tool = self.research_tool.get_reasoning_analysis_tool()
                self.stream_tool = self.research_tool.get_stream_tool()
                
                # 重新设置工具
                self._tools = self._setup_tools()
                return "已切换到增强版研究工具，启用知识图谱探索和推理链分析功能"
            except Exception as e:
                self.use_deeper_tool = False
                self.stream_tool = self.research_tool.get_thinking_stream_tool() if hasattr(self.research_tool, 'get_thinking_stream_tool') else None
                return f"切换到增强版失败: {e}"
        else:
            # 切换回标准版
            self.research_tool = DeepResearchTool(provider=self.retrieval_provider, run_id=self.run_id)
            self.stream_tool = self.research_tool.get_thinking_stream_tool() if hasattr(self.research_tool, 'get_thinking_stream_tool') else None
            # 清除增强工具
            self.exploration_tool = None
            self.reasoning_analysis_tool = None
            # 重新设置工具
            self._tools = self._setup_tools()
            return "已切换到标准版研究工具，部分高级功能将不可用"

    def close(self):
        """关闭资源"""
        # 调用父类方法
        super().close()
        
        # 关闭研究工具资源
        if hasattr(self, 'research_tool') and hasattr(self.research_tool, 'close'):
            self.research_tool.close()

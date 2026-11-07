# 由 search/tool/reasoning/chain_of_exploration.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from deepresearch_agent.search.tool.reasoning.chain_strategy import ChainStrategyMixin
from deepresearch_agent.search.tool.reasoning.chain_scoring import ChainScoringMixin
from deepresearch_agent.search.tool.reasoning.chain_info import ChainInfoMixin
from deepresearch_agent.search.tool.reasoning.chain_explorer import ChainExplorerMixin

class ChainOfExplorationSearcher(ChainStrategyMixin, ChainScoringMixin, ChainInfoMixin, ChainExplorerMixin):
    """
    增强版Chain of Exploration检索器
    
    实现多步自主探索图谱的能力，具有适应性搜索宽度、记忆机制和路径优化功能
    """
    

    def __init__(self, graph, llm, embeddings_model):
        """
        初始化Chain of Exploration检索器
        
        Args:
            graph: 图数据库连接
            llm: 语言模型
            embeddings_model: 向量嵌入模型
        """
        self.graph = graph
        self.llm = llm
        self.embeddings = embeddings_model
        self.visited_nodes = set()
        self.exploration_path = []
        self.exploration_memory = {}  # 存储已探索路径的记忆
        self.performance_metrics = {}

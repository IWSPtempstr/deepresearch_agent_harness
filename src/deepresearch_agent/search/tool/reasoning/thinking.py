# 由 search/tool/reasoning/thinking.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, initialize_with_query。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from deepresearch_agent.search.tool.reasoning.thinking_hypotheses import ThinkingHypothesesMixin
from deepresearch_agent.search.tool.reasoning.thinking_branching import ThinkingBranchingMixin
from deepresearch_agent.search.tool.reasoning.thinking_state import ThinkingStateMixin
from deepresearch_agent.search.tool.reasoning.thinking_text import ThinkingTextMixin

class ThinkingEngine(ThinkingHypothesesMixin, ThinkingBranchingMixin, ThinkingStateMixin, ThinkingTextMixin):
    """
    思考引擎类：负责管理多轮迭代的思考过程
    提供思考历史管理和转换功能
    """
    

    def __init__(self, llm):
        """
        初始化思考引擎
        
        参数:
            llm: 大语言模型实例，用于生成思考内容
        """
        self.llm = llm
        self.all_reasoning_steps = []
        self.msg_history = []
        self.executed_search_queries = []
        self.hypotheses = []       # 存储假设
        self.verification_chain = [] # 验证步骤
        self.reasoning_tree = {}   # 推理树结构
        self.current_branch = "main" # 当前推理分支

    def initialize_with_query(self, query: str):
        """使用初始查询初始化思考历史"""
        self.all_reasoning_steps = []
        self.msg_history = [{"role": "user", "content": f'问题:"{query}"\n'}]
        self.executed_search_queries = []
        self.hypotheses = []
        self.verification_chain = []
        self.reasoning_tree = {"main": []} # 初始化主分支
        self.current_branch = "main"

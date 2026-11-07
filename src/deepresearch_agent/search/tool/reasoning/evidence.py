# 由 search/tool/reasoning/evidence.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, start_new_query, add_reasoning_step。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import Dict, List
import time
import hashlib
from deepresearch_agent.models.get_models import get_llm_model

from deepresearch_agent.search.tool.reasoning.evidence_record import EvidenceRecordMixin
from deepresearch_agent.search.tool.reasoning.evidence_contradiction import EvidenceContradictionMixin
from deepresearch_agent.search.tool.reasoning.evidence_citations import EvidenceCitationsMixin
from deepresearch_agent.search.tool.reasoning.evidence_summary import EvidenceSummaryMixin

class EvidenceChainTracker(EvidenceRecordMixin, EvidenceContradictionMixin, EvidenceCitationsMixin, EvidenceSummaryMixin):
    """
    证据链收集和推理跟踪器
    
    收集和管理深度研究过程中的证据链，
    追踪推理步骤使用的证据来源和推理逻辑
    """
    

    def __init__(self):
        """初始化证据链跟踪器"""
        self.llm = get_llm_model()
        self.reasoning_steps = []  # 推理步骤
        self.evidence_items = {}   # 证据项
        self.query_contexts = {}   # 查询上下文
        self.step_counter = 0      # 步骤计数器
        self.confidence_scores = {}  # 证据的可信度评分
        self.contradictions = {}     # 记录相互矛盾的证据
        self.citation_index = {}     # 引用索引

    def start_new_query(self, query: str, keywords: Dict[str, List[str]]) -> str:
        """
        开始新的查询跟踪
        
        参数:
            query: 用户查询
            keywords: 查询关键词
            
        返回:
            str: 查询ID
        """
        # 生成查询ID
        query_id = hashlib.md5(f"{query}:{time.time()}".encode()).hexdigest()[:10]
        
        # 存储查询上下文
        self.query_contexts[query_id] = {
            "query": query,
            "keywords": keywords,
            "start_time": time.time(),
            "step_ids": []
        }
        
        return query_id

    def add_reasoning_step(self, 
                         query_id: str, 
                         search_query: str, 
                         reasoning: str) -> str:
        """
        添加推理步骤
        
        参数:
            query_id: 查询ID
            search_query: 搜索查询
            reasoning: 推理过程
            
        返回:
            str: 步骤ID
        """
        # 生成步骤ID
        step_id = f"step_{self.step_counter}"
        self.step_counter += 1
        
        # 创建步骤记录
        step = {
            "step_id": step_id,
            "query_id": query_id,
            "search_query": search_query,
            "reasoning": reasoning,
            "evidence_ids": [],
            "timestamp": time.time()
        }
        
        # 添加步骤到列表并关联到查询
        self.reasoning_steps.append(step)
        if query_id in self.query_contexts:
            self.query_contexts[query_id]["step_ids"].append(step_id)
        
        return step_id

# 由 search/tool/hybrid_tool.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _setup_chains, structured_search, search, close。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
import time
from typing import List, Dict, Any, Tuple
import pandas as pd
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from deepresearch_agent.config.prompts import (
    LC_SYSTEM_PROMPT,
    HYBRID_TOOL_QUERY_PROMPT,
    LOCAL_SEARCH_KEYWORD_PROMPT,
)
from deepresearch_agent.config.settings import gl_description, response_type, HYBRID_SEARCH_SETTINGS
from deepresearch_agent.search.retrieval_adapter import (
    create_retrieval_metadata,
    create_retrieval_result,
    merge_retrieval_results,
    results_from_entities,
    results_from_relationships,
    results_to_payload,
)

from deepresearch_agent.search.tool.base import BaseSearchTool
from deepresearch_agent.search.tool.hybrid_retrieval import HybridRetrievalMixin
from deepresearch_agent.search.tool.hybrid_keywords import HybridKeywordsMixin

class HybridSearchTool(HybridRetrievalMixin, HybridKeywordsMixin, BaseSearchTool):
    """
    混合搜索工具，实现类似LightRAG的双级检索策略
    结合了局部细节检索和全局主题检索
    """
    

    def __init__(self):
        """初始化混合搜索工具"""
        # 检索参数
        self.entity_limit = HYBRID_SEARCH_SETTINGS["entity_limit"]
        self.max_hop_distance = HYBRID_SEARCH_SETTINGS["max_hop_distance"]
        self.top_communities = HYBRID_SEARCH_SETTINGS["top_communities"]
        self.batch_size = HYBRID_SEARCH_SETTINGS["batch_size"]
        self.community_level = HYBRID_SEARCH_SETTINGS["community_level"]
        
        # 调用父类构造函数
        super().__init__(cache_dir="./cache/hybrid_search")

        # 设置处理链
        self._setup_chains()

    def _setup_chains(self):
        """设置处理链"""
        # 创建主查询处理链 - 用于生成最终答案
        self.query_prompt = ChatPromptTemplate.from_messages([
            ("system", LC_SYSTEM_PROMPT),
            ("human", HYBRID_TOOL_QUERY_PROMPT),
        ])
        
        # 链接到LLM
        self.query_chain = self.query_prompt | self.llm | StrOutputParser()
        
        # 关键词提取链
        self.keyword_prompt = ChatPromptTemplate.from_messages([
            ("system", LOCAL_SEARCH_KEYWORD_PROMPT),
            ("human", "{query}"),
        ])
        
        self.keyword_chain = self.keyword_prompt | self.llm | StrOutputParser()

    def structured_search(self, query_input: Any) -> Dict[str, Any]:
        """
        执行混合搜索，返回包含证据与答案的结构化结果。
        """
        overall_start = time.time()
        
        # 解析输入
        if isinstance(query_input, dict) and "query" in query_input:
            query = query_input["query"]
            # 支持直接传入分类的关键词
            low_keywords = query_input.get("low_level_keywords", [])
            high_keywords = query_input.get("high_level_keywords", [])
        else:
            query = str(query_input)
            # 提取关键词
            keywords = self.extract_keywords(query)
            low_keywords = keywords.get("low_level", [])
            high_keywords = keywords.get("high_level", [])
        
        # 检查缓存
        cache_key = query
        if low_keywords or high_keywords:
            cache_key = self.cache_manager.key_strategy.generate_key(
                query, 
                low_level_keywords=low_keywords, 
                high_level_keywords=high_keywords
            )
            
        cached_result = self.cache_manager.get(cache_key)
        if isinstance(cached_result, dict):
            return cached_result
        
        try:
            # 1. 检索低级内容（实体和关系）
            low_level_content, low_evidence = self._retrieve_low_level_content(query, low_keywords)
            
            # 2. 检索高级内容（社区和主题）
            high_level_content, high_evidence = self._retrieve_high_level_content(query, high_keywords)
            
            # 3. 生成最终答案
            llm_start = time.time()
            
            # 调用LLM生成最终答案
            answer = self.query_chain.invoke({
                "query": query,
                "low_level": low_level_content,
                "high_level": high_level_content,
                "response_type": response_type
            })
            
            self.performance_metrics["llm_time"] += time.time() - llm_start
            
            all_evidence = merge_retrieval_results(low_evidence, high_evidence)
            structured_result = {
                "query": query,
                "low_level_content": low_level_content,
                "high_level_content": high_level_content,
                "final_answer": answer if answer else "未找到相关信息",
                "retrieval_results": results_to_payload(all_evidence),
            }
            
            # 缓存结果
            self.cache_manager.set(
                cache_key, 
                structured_result, 
                low_level_keywords=low_keywords,
                high_level_keywords=high_keywords
            )
            
            self.performance_metrics["total_time"] = time.time() - overall_start

            return structured_result
            
        except Exception as e:
            error_msg = f"搜索过程中出现错误: {str(e)}"
            print(error_msg)
            return {
                "query": query,
                "low_level_content": "",
                "high_level_content": "",
                "final_answer": error_msg,
                "retrieval_results": [],
                "error": error_msg,
            }

    def search(self, query_input: Any) -> str:
        """
        执行混合搜索，结合低级和高级内容
        
        参数:
            query_input: 字符串查询或包含查询和关键词的字典
            
        返回:
            str: 生成的最终答案
        """
        structured = self.structured_search(query_input)
        return structured.get("final_answer", "未找到相关信息")

    def close(self):
        """关闭资源"""
        super().close()

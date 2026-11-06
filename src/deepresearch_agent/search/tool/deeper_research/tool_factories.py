# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""LangChain 工具工厂与异步辅助（原 get_* / _async_*）。"""

from typing import Dict, Any, AsyncGenerator
import asyncio
from langchain_core.tools import BaseTool
from deepresearch_agent.search.tool.deeper_research import (
    enhance_search_with_coe,
    detect_and_resolve_contradictions,
)


class DeeperToolFactoryMixin:
    def get_tool(self):
        """获取搜索工具"""
        class DeeperResearchRetrievalTool(BaseTool):
            name : str = "deeper_research"
            description : str = "增强版深度研究工具：通过社区感知和知识图谱分析，结合多轮推理和搜索解决复杂问题。"
            
            def _run(self_tool, query: Any) -> str:
                return self.search(query)
            
            def _arun(self_tool, query: Any) -> str:
                raise NotImplementedError("异步执行未实现")
        
        return DeeperResearchRetrievalTool()
    
    def get_thinking_tool(self):
        """获取思考过程可见的工具版本"""
        class DeepThinkingTool(BaseTool):
            name : str = "deep_thinking"
            description : str = "深度思考工具：显示完整思考过程的深度研究，适用于需要查看推理步骤的情况。"
            
            def _run(self_tool, query: Any) -> Dict:
                # 解析输入
                if isinstance(query, dict) and "query" in query:
                    tk_query = query["query"]
                else:
                    tk_query = str(query)
                
                # 执行思考过程
                return self.thinking(tk_query)
            
            def _arun(self_tool, query: Any) -> Dict:
                raise NotImplementedError("异步执行未实现")
        
        return DeepThinkingTool()
    
    def get_stream_tool(self):
        """获取流式搜索工具"""        
        class DeeperResearchStreamTool(BaseTool):
            name : str = "deeper_research_stream"
            description : str = "增强版流式深度研究工具：通过社区感知和知识图谱分析，结合多轮推理和搜索解决复杂问题，支持流式输出。"
            
            def _run(self_tool, query: Any) -> AsyncGenerator:
                return self.search_stream(query)
            
            async def _arun(self_tool, query: Any) -> AsyncGenerator:
                return await self.search_stream(query)
        
        return DeeperResearchStreamTool()
    
    def get_exploration_tool(self):
        """获取专注于知识图谱探索的工具"""
        class KnowledgeExplorationTool(BaseTool):
            name : str = "knowledge_exploration"
            description : str = "知识图谱探索工具：专注于从起始实体出发，探索知识图谱，发现潜在关联。"
            
            def _run(self_tool, query: Any) -> Dict:
                if isinstance(query, dict) and "entities" in query:
                    entities = query["entities"]
                    search_query = query.get("query", "")
                else:
                    # 提取关键词作为起始实体
                    search_query = str(query)
                    keywords = self.extract_keywords(search_query)
                    entities = keywords.get("high_level", []) + keywords.get("low_level", [])
                    entities = entities[:3]  # 最多使用3个实体
                
                # 执行探索
                if entities:
                    return self.chain_explorer.explore(search_query, entities, max_steps=3)
                else:
                    return {"status": "error", "message": "未找到起始实体"}
            
            async def _arun(self_tool, query: Any) -> Dict:
                return self._run(query)
        
        return KnowledgeExplorationTool()
    
    def get_reasoning_analysis_tool(self):
        """获取推理链分析工具"""        
        class ReasoningAnalysisTool(BaseTool):
            name : str = "reasoning_analysis"
            description : str = "推理链分析工具：分析推理过程中的证据、矛盾和支持度。"
            
            def _run(self_tool, query_id: str) -> Dict:
                # 如果没有提供查询ID，使用当前上下文的
                if not query_id and hasattr(self, 'current_query_context'):
                    query_id = self.current_query_context.get("query_id", "")
                
                if not query_id:
                    return {"status": "error", "message": "未找到有效的查询ID"}
                
                # 获取完整推理链
                reasoning_chain = self.evidence_tracker.get_reasoning_chain(query_id)
                
                # 检测矛盾
                contradiction_result = detect_and_resolve_contradictions(self, query_id)
                
                # 生成推理摘要
                reasoning_summary = self.evidence_tracker.summarize_reasoning(query_id)
                
                # 获取证据统计
                evidence_stats = self.evidence_tracker.get_evidence_source_stats(query_id)
                
                return {
                    "reasoning_chain": reasoning_chain,
                    "contradictions": contradiction_result["contradictions"],
                    "summary": reasoning_summary,
                    "evidence_stats": evidence_stats
                }
            
            async def _arun(self_tool, query_id: str) -> Dict:
                return self._run(query_id)
        
        return ReasoningAnalysisTool()

    async def _async_enhance_search(self, query, keywords):
        """异步执行社区感知搜索增强"""
        def enhance_wrapper():
            return enhance_search_with_coe(self, query, keywords)
        
        return await asyncio.to_thread(enhance_wrapper)

    async def _async_build_graph(self, query, entities):
        """异步构建知识图谱"""
        def build_wrapper():
            return self.knowledge_builder.build_query_graph(query, entities, depth=1)
        
        return await asyncio.to_thread(build_wrapper)
    
    async def _async_detect_contradictions(self, query_id):
        """异步检测矛盾"""
        def detect_wrapper():
            return detect_and_resolve_contradictions(self, query_id)
        
        return await asyncio.to_thread(detect_wrapper)

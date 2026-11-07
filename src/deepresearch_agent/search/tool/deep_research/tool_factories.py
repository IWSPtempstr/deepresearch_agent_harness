# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""LangChain 工具工厂（原 get_tool/get_thinking_tool/get_thinking_stream_tool）。"""

from typing import Dict, List, Any, Optional, AsyncGenerator
from langchain_core.tools import BaseTool


class DeepToolFactoryMixin:
    def get_tool(self) -> BaseTool:
        """获取搜索工具"""
        class DeepResearchRetrievalTool(BaseTool):
            name : str = "deep_research"
            description : str = "深度研究工具：通过多轮推理和搜索解决复杂问题，尤其适用于需要深入分析的查询。"
            
            def _run(self_tool, query: Any) -> str:
                return self.search(query)
            
            def _arun(self_tool, query: Any) -> str:
                raise NotImplementedError("异步执行未实现")
        
        return DeepResearchRetrievalTool()

    def get_thinking_tool(self) -> BaseTool:
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

    def get_thinking_stream_tool(self) -> BaseTool:
        """获取流式思考过程工具"""
        class DeepStreamThinkingTool(BaseTool):
            name : str = "deep_thinking_stream"
            description : str = "流式深度思考工具：显示完整思考过程的深度研究，适用于需要查看推理步骤的情况。"
            
            def _run(self_tool, query: Any) -> AsyncGenerator:
                # 解析输入
                if isinstance(query, dict) and "query" in query:
                    tk_query = query["query"]
                else:
                    tk_query = str(query)
                
                # 返回流式生成器
                return self.thinking_stream(tk_query)
            
            async def _arun(self_tool, query: Any) -> AsyncGenerator:
                # 解析输入
                if isinstance(query, dict) and "query" in query:
                    tk_query = query["query"]
                else:
                    tk_query = str(query)
                
                # 返回流式生成器
                return await self.thinking_stream(tk_query)
        
        return DeepStreamThinkingTool()

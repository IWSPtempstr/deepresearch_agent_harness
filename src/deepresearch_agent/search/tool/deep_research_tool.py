# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：search, close。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import Any
import time
import traceback

from deepresearch_agent.search.tool.base import BaseSearchTool
from deepresearch_agent.search.tool.deep_research.wiring import DeepResearchInitMixin
from deepresearch_agent.search.tool.deep_research.retrieval_funcs import DeepRetrievalMixin
from deepresearch_agent.search.tool.deep_research.thinking_pipeline import DeepThinkingMixin
from deepresearch_agent.search.tool.deep_research.streaming import DeepStreamingMixin
from deepresearch_agent.search.tool.deep_research.tool_factories import DeepToolFactoryMixin

class DeepResearchTool(DeepResearchInitMixin, DeepRetrievalMixin, DeepThinkingMixin, DeepStreamingMixin, DeepToolFactoryMixin, BaseSearchTool):
    """
    深度研究工具：整合多种搜索策略，实现多步骤的思考-搜索-推理过程
    
    该工具实现了多步骤的研究过程，可以执行以下步骤：
    1. 思考分析用户问题
    2. 生成搜索查询
    3. 执行搜索
    4. 整合信息并进一步思考
    5. 迭代上述过程直到获得完整答案
    """
    

    def search(self, query_input: Any) -> str:
        """
        执行深度研究搜索
        
        参数:
            query_input: 搜索查询或包含查询的字典
                
        返回:
            str: 搜索结果
        """
        overall_start = time.time()
        
        # 记录开始搜索
        self._log(f"\n[深度搜索] 开始处理查询...")
        
        # 解析输入
        if isinstance(query_input, dict) and "query" in query_input:
            query = query_input["query"]
        else:
            query = str(query_input)
        
        self._log(f"\n[深度搜索] 解析后的查询: {query}")
        
        # 检查缓存
        cache_key = f"deep:{query}"
        cached_result = self.cache_manager.get(cache_key)
        if cached_result:
            self._log(f"\n[深度搜索] 缓存命中，返回缓存结果")
            return cached_result
        
        try:
            # 执行思考过程
            self._log(f"\n[深度搜索] 开始执行思考过程")
            result = self.thinking(query)
            answer = result["answer"]
            chunk_info = result.get("reference", {})
            
            # 格式化参考资料
            references = []
            if "doc_aggs" in chunk_info:
                for doc in chunk_info["doc_aggs"]:
                    doc_id = doc.get("doc_id", "")
                    if doc_id and doc_id not in references:
                        references.append(doc_id)
            
            # 添加引用信息
            if references and "{'data': {'Chunks':" not in answer:
                ref_str = ", ".join([f"'{ref}'" for ref in references[:5]])
                answer += f"\n\n{{'data': {{'Chunks':[{ref_str}] }} }}"
            
            # 验证答案质量
            validation_results = self.validator.validate(query, answer)
            if validation_results["passed"]:
                self._log(f"\n[深度搜索] 答案验证通过，缓存结果")
                self.cache_manager.set(cache_key, answer)
            else:
                self._log(f"\n[深度搜索] 答案验证失败，不缓存")
            
            # 记录总时间
            total_time = time.time() - overall_start
            self._log(f"\n[深度搜索] 完成，耗时 {total_time:.2f}秒")
            self.performance_metrics["total_time"] = total_time
            
            return answer
                
        except Exception as e:
            error_msg = f"深度研究过程中出错: {str(e)}\n{traceback.format_exc()}"
            print(error_msg)
            return f"抱歉，处理您的问题时遇到了错误: {str(e)}"

    def close(self):
        """关闭资源"""
        # 调用父类方法
        super().close()
        
        # 关闭复用的工具资源
        if getattr(self, 'hybrid_tool', None) is not None:
            self.hybrid_tool.close()
        if getattr(self, 'global_tool', None) is not None:
            self.global_tool.close()
        if getattr(self, 'local_tool', None) is not None:
            self.local_tool.close()

# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""同步搜索入口与资源关闭（原 search/close）。"""

from typing import Any
import time
import traceback


class DeeperSearchMixin:
    def search(self, query_input: Any) -> str:
        """
        执行增强版深度研究搜索
        
        Args:
            query_input: 搜索查询或包含查询的字典
                    
        Returns:
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
        
        # 检查多级缓存
        # 1. 首先检查deeperresearch缓存
        cache_key = f"deeper:{query}"
        if hasattr(self, '_search_result_cache') and cache_key in self._search_result_cache:
            self._log(f"\n[深度搜索] 内部缓存命中，返回缓存结果")
            return self._search_result_cache[cache_key]
            
        # 2. 然后检查deep_research缓存
        deep_cache_key = f"deep:{query}"
        cached_result = self.deep_research.cache_manager.get(deep_cache_key)
        if cached_result:
            self._log(f"\n[深度搜索] 缓存命中，返回缓存结果")
            
            # 同时更新内部缓存
            if not hasattr(self, '_search_result_cache'):
                self._search_result_cache = {}
            self._search_result_cache[cache_key] = cached_result
            
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
            validation_results = self.deep_research.validator.validate(query, answer)
            if validation_results["passed"]:
                self._log(f"\n[深度搜索] 答案验证通过，缓存结果")
                self.deep_research.cache_manager.set(deep_cache_key, answer)
                
                # 更新内部缓存
                if not hasattr(self, '_search_result_cache'):
                    self._search_result_cache = {}
                self._search_result_cache[cache_key] = answer
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
        # 关闭deep_research的资源
        if hasattr(self, 'deep_research'):
            self.deep_research.close()

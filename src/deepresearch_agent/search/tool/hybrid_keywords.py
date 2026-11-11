# 由 search/tool/hybrid_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""查询关键词提取与全局检索工具工厂（原 extract_keywords/get_global_tool）。"""

import time
import json
from typing import List, Dict, Any
from langchain_core.tools import BaseTool
from deepresearch_agent.config.settings import gl_description


class HybridKeywordsMixin:
    def extract_keywords(self, query: str) -> Dict[str, List[str]]:
        """
        从查询中提取双级关键词
        
        参数:
            query: 查询字符串
            
        返回:
            Dict[str, List[str]]: 分类关键词字典
        """
        # 检查缓存
        cached_keywords = self.cache_manager.get(f"keywords:{query}")
        if cached_keywords:
            return cached_keywords
            
        try:
            llm_start = time.time()
            
            # 调用LLM提取关键词
            result = self.keyword_chain.invoke({"query": query})
            
            print(f"DEBUG - LLM关键词结果: {result[:100]}...") if len(str(result)) > 100 else print(f"DEBUG - LLM关键词结果: {result}")
            
            # 解析JSON结果
            try:
                # 尝试直接解析
                if isinstance(result, dict):
                    # 结果已经是字典，无需解析
                    keywords = result
                elif isinstance(result, str):
                    # 清理字符串，移除可能导致解析失败的字符
                    result = result.strip()
                    # 检查字符串是否以JSON格式开始
                    if result.startswith('{') and result.endswith('}'):
                        keywords = json.loads(result)
                    else:
                        # 尝试提取JSON部分 - 寻找第一个{和最后一个}
                        start_idx = result.find('{')
                        end_idx = result.rfind('}')
                        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                            json_str = result[start_idx:end_idx+1]
                            keywords = json.loads(json_str)
                        else:
                            # 没有有效的JSON结构，使用简单的关键词提取
                            raise ValueError("No valid JSON structure found")
                else:
                    # 不是字符串也不是字典
                    raise TypeError(f"Unexpected result type: {type(result)}")
                    
            except (json.JSONDecodeError, ValueError, TypeError) as json_err:
                print(f"JSON解析失败: {json_err}，尝试备用方法提取关键词")
                
                # 备用方法：手动提取关键词
                if isinstance(result, str):
                    # 简单分词提取关键词
                    import re
                    # 移除标点符号，按空格分词
                    words = re.findall(r'\b\w+\b', query.lower())
                    # 过滤停用词（简化版，实际需要更完整的停用词表）
                    stopwords = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
                                "in", "on", "at", "to", "for", "with", "by", "about", "of", "and", "or"}
                    keywords = {
                        "high_level": [word for word in words if len(word) > 5 and word not in stopwords][:3],
                        "low_level": [word for word in words if 3 <= len(word) <= 5 and word not in stopwords][:5]
                    }
                else:
                    # 如果不是字符串，返回基于原始查询的简单关键词
                    keywords = {
                        "high_level": [query],
                        "low_level": []
                    }
            
            # 记录LLM处理时间
            self.performance_metrics["llm_time"] += time.time() - llm_start
            
            # 确保包含必要的键
            if not isinstance(keywords, dict):
                keywords = {}
            if "low_level" not in keywords:
                keywords["low_level"] = []
            if "high_level" not in keywords:
                keywords["high_level"] = []
                
            # 确保列表类型
            if not isinstance(keywords["low_level"], list):
                keywords["low_level"] = [str(keywords["low_level"])]
            if not isinstance(keywords["high_level"], list):
                keywords["high_level"] = [str(keywords["high_level"])]
                
            # 缓存结果
            self.cache_manager.set(f"keywords:{query}", keywords)
            
            return keywords
            
        except Exception as e:
            print(f"关键词提取失败: {e}")
            # 返回基于原始查询的默认值
            return {"low_level": [query], "high_level": [query.split()[0] if query.split() else query]}

    def get_global_tool(self) -> BaseTool:
        """
        获取全局搜索工具
        
        返回:
            BaseTool: 全局搜索工具实例
        """
        class GlobalSearchTool(BaseTool):
            name : str = "global_retriever"
            description : str= gl_description
            
            def _run(self_tool, query: Any) -> str:
                # 设置为仅使用高级内容
                if isinstance(query, dict) and "query" in query:
                    original_query = query["query"]
                    keywords = query.get("keywords", [])
                    # 转换为高级关键词
                    high_keywords = keywords
                    query = {
                        "query": original_query,
                        "high_level_keywords": high_keywords,
                        "low_level_keywords": []  # 不使用低级关键词
                    }
                else:
                    # 提取关键词
                    keywords = self.extract_keywords(str(query))
                    query = {
                        "query": str(query),
                        "high_level_keywords": keywords.get("high_level", []),
                        "low_level_keywords": []
                    }
                
                return self.search(query)
            
            def _arun(self_tool, query: Any) -> str:
                raise NotImplementedError("异步执行未实现")
                
        return GlobalSearchTool()

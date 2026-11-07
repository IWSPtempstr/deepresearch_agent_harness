# 由 search/tool/reasoning/thinking.py 拆分（T-014），方法体与原位置逐行一致。
"""思考文本的标签清洗与查询提取（原 remove_query_tags 等 3 个方法；评审修订：保持 Mixin 方法而非模块函数，调用方 self.* 零改动）。"""

import re
from typing import List, Dict, Any
from deepresearch_agent.search.tool.reasoning.nlp import extract_between
from deepresearch_agent.config.prompts import (
    BEGIN_SEARCH_QUERY,
    BEGIN_SEARCH_RESULT,
    END_SEARCH_RESULT,
    REASON_PROMPT,
    END_SEARCH_QUERY,
    INITIAL_THINKING_PROMPT,
    HYPOTHESIS_GENERATION_PROMPT,
    HYPOTHESIS_VERIFICATION_PROMPT,
    VERIFICATION_STATUS_PROMPT,
    UPDATE_THINKING_PROMPT,
    COUNTERFACTUAL_ANALYSIS_PROMPT,
    COUNTERFACTUAL_COMPARISON_PROMPT,
)


class ThinkingTextMixin:
    def remove_query_tags(self, text: str) -> str:
        """
        移除文本中的查询标签
        
        参数:
            text: 包含标签的文本
            
        返回:
            str: 移除标签后的文本
        """
        pattern = re.escape(BEGIN_SEARCH_QUERY) + r"(.*?)" + re.escape(END_SEARCH_QUERY)
        return re.sub(pattern, "", text, flags=re.DOTALL)

    def remove_result_tags(self, text: str) -> str:
        """
        移除文本中的结果标签
        
        参数:
            text: 包含标签的文本
            
        返回:
            str: 移除标签后的文本
        """
        pattern = re.escape(BEGIN_SEARCH_RESULT) + r"(.*?)" + re.escape(END_SEARCH_RESULT)
        return re.sub(pattern, "", text, flags=re.DOTALL)

    def extract_queries(self, text: str) -> List[str]:
        """
        从文本中提取搜索查询
        
        参数:
            text: 包含查询的文本
            
        返回:
            List[str]: 提取的查询列表
        """
        return extract_between(text, BEGIN_SEARCH_QUERY, END_SEARCH_QUERY)

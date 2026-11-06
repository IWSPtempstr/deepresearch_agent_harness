# 由 agents/deep_research_agent.py 拆分（T-014），方法体与原位置逐行一致。
"""研究节点实现（原 _generate_node，覆盖 BaseAgent 抽象方法）。"""

from typing import List, Dict, AsyncGenerator, Optional
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
import re
from deepresearch_agent.config.prompts import (
    LC_SYSTEM_PROMPT,
    DEEP_RESEARCH_THINKING_SUMMARY_PROMPT,
    EXPLORATION_SUMMARY_PROMPT,
    CONTRADICTION_IMPACT_PROMPT,
)
from deepresearch_agent.config.settings import response_type


class DeepResearchNodesMixin:
    def _generate_node(self, state):
        """生成回答节点逻辑 - 处理各种返回格式"""
        messages = state["messages"]
        
        # 安全地获取问题和检索结果
        try:
            # 原始问题在倒数第三个消息
            question = messages[-3].content if len(messages) >= 3 else "未找到问题"
            # 检索结果在最后一个消息
            retrieval_result = messages[-1].content if messages[-1] else "未找到相关信息"
        except Exception as e:
            return {"messages": [AIMessage(content=f"生成回答时出错: {str(e)}")]}

        # 首先尝试全局缓存
        global_result = self.global_cache_manager.get(question)
        if global_result:
            self._log_execution("generate", 
                            {"question": question, "source": "全局缓存"}, 
                            "全局缓存命中")
            return {"messages": [AIMessage(content=global_result)]}

        # 获取当前会话ID
        thread_id = state.get("configurable", {}).get("thread_id", "default")
            
        # 然后检查会话缓存
        cached_result = self.cache_manager.get(question, thread_id=thread_id)
        if cached_result:
            self._log_execution("generate", 
                            {"question": question, "source": "会话缓存"}, 
                            "会话缓存命中")
            # 将命中内容同步到全局缓存
            self.global_cache_manager.set(question, cached_result)
            return {"messages": [AIMessage(content=cached_result)]}

        # 处理流式输出的情况 - 生成器或字典结果
        if isinstance(retrieval_result, (AsyncGenerator, Dict)) and not isinstance(retrieval_result, str):
            # 如果结果是字典且包含'answer'字段，提取答案
            if isinstance(retrieval_result, dict) and 'answer' in retrieval_result:
                answer = retrieval_result['answer']
                # 根据结果结构处理
                if '<think>' in answer and '</think>' in answer:
                    # 包含思考过程，提取干净的答案
                    clean_answer = re.sub(r'<think>.*?</think>\s*', '', answer, flags=re.DOTALL)
                    # 缓存清理后的答案
                    if clean_answer and len(clean_answer) > 10:
                        self.cache_manager.set(question, clean_answer, thread_id=thread_id)
                        self.global_cache_manager.set(question, clean_answer)
                    return {"messages": [AIMessage(content=clean_answer)]}
                else:
                    # 没有特殊标记，直接使用
                    if answer and len(answer) > 10:
                        self.cache_manager.set(question, answer, thread_id=thread_id)
                        self.global_cache_manager.set(question, answer)
                    return {"messages": [AIMessage(content=answer)]}
            # 生成器或其他复杂结构，直接返回
            return {"messages": [AIMessage(content=retrieval_result)]}

        # 如果检索结果不是包含思考过程的字符串，直接返回
        if not isinstance(retrieval_result, str) or not retrieval_result.startswith("<think>"):
            # 直接返回检索结果
            if self.cache_manager.validate_answer(question, retrieval_result):
                # 更新会话缓存
                self.cache_manager.set(question, retrieval_result, thread_id=thread_id)
                # 更新全局缓存
                self.global_cache_manager.set(question, retrieval_result)
            return {"messages": [AIMessage(content=retrieval_result)]}
        
        # 处理思考过程（当使用思考工具时）
        try:
            # 提取思考过程
            thinking = retrieval_result
            
            # 创建总结提示
            prompt = ChatPromptTemplate.from_messages([
                ("system", LC_SYSTEM_PROMPT),
                ("human", DEEP_RESEARCH_THINKING_SUMMARY_PROMPT),
            ])
            
            # 创建处理链
            chain = prompt | self.llm | StrOutputParser()
            
            # 生成回答
            response = chain.invoke({
                "thinking": thinking,
                "question": question,
                "response_type": response_type
            })
            
            # 缓存结果 - 同时更新会话缓存和全局缓存
            if self.cache_manager.validate_answer(question, response):
                # 更新会话缓存
                self.cache_manager.set(question, response, thread_id=thread_id)
                # 更新全局缓存
                self.global_cache_manager.set(question, response)
            
            return {"messages": [AIMessage(content=response)]}
            
        except Exception as e:
            error_msg = f"处理思考过程时出错: {str(e)}"
            return {"messages": [AIMessage(content=error_msg)]}

# 由 agents/deep_research_agent.py 拆分（T-014），方法体与原位置逐行一致。
"""研究问答入口（原 ask/ask_with_thinking/ask_stream）。"""

from typing import List, Dict, AsyncGenerator, Optional
import asyncio
import re


class DeepResearchAskMixin:
    def ask(self, query: str, thread_id: str = "default", recursion_limit: Optional[int] = None,
            show_thinking: bool = False, exploration_mode: bool = False, *, bypass_cache: bool = False):
        """
        向Agent提问，可选显示思考过程

        参数:
            query: 用户问题
            thread_id: 会话ID
            recursion_limit: 递归限制
            show_thinking: 是否显示思考过程
            exploration_mode: 是否使用知识图谱探索模式
            bypass_cache: 是否跳过 answer 缓存（Web 单源 Run 必须为 True，
                          避免复用无 provenance 的旧缓存导致验证失败）

        返回:
            str: 生成的回答或包含思考过程的字典
        """
        # 设置是否显示思考过程
        old_thinking = self.show_thinking
        self.show_thinking = show_thinking

        try:
            # 检查是否使用知识图谱探索模式
            if exploration_mode and self.use_deeper_tool:
                # 知识图谱探索模式
                return self.explore_knowledge(query, thread_id)

            # 正常模式 - 调用父类方法
            result = super().ask(query, thread_id, recursion_limit, bypass_cache=bypass_cache)
            return result
        finally:
            # 重置状态
            self.show_thinking = old_thinking

    def ask_with_thinking(self, query: str, thread_id: str = "default", community_aware: bool = True):
        """
        提问并返回带思考过程的答案
        
        参数:
            query: 用户问题
            thread_id: 会话ID
            community_aware: 是否启用社区感知
            
        返回:
            dict: 包含思考过程和答案的字典
        """
        # 如果启用社区感知且工具支持
        if community_aware and self.community_enhancer:
            # 提取关键词
            keywords = self.research_tool.extract_keywords(query)
            
            # 使用社区感知增强搜索
            enhanced_context = self.community_enhancer.enhance_search(query, keywords)
            
            # 传递增强上下文到思考过程
            result = self.research_tool.thinking(query)
            
            # 添加社区上下文到结果
            if "community_info" in enhanced_context:
                result["community_context"] = enhanced_context["community_info"]
            
            return result
        else:
            # 直接调用研究工具的thinking方法
            result = self.research_tool.thinking(query)
            
            # 确保结果包含执行日志
            if "execution_logs" not in result:
                result["execution_logs"] = []
                
            return result

    async def ask_stream(self, query: str, thread_id: str = "default", 
                         recursion_limit: Optional[int] = None, show_thinking: bool = False) -> AsyncGenerator[str, None]:
        """
        向Agent提问，返回流式响应
        
        参数:
            query: 用户问题
            thread_id: 会话ID
            recursion_limit: 递归限制
            show_thinking: 是否显示思考过程
            
        返回:
            AsyncGenerator: 流式响应内容
        """
        # 使用父类的缓存检查逻辑
        fast_result = self.check_fast_cache(query, thread_id)
        if fast_result:
            # 缓存命中，按句子分割返回
            chunks = re.split(r'([.!?。！？]\s*)', fast_result)
            buffer = ""
            
            for i in range(0, len(chunks)):
                buffer += chunks[i]
                
                # 当缓冲区包含完整句子或达到合理大小时输出
                if (i % 2 == 1) or len(buffer) >= self.deep_stream_flush_threshold:
                    yield buffer
                    buffer = ""
                    await asyncio.sleep(0.01)
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
            return
        
        # 无缓存，根据是否显示思考过程和工具类型选择流式方法
        if show_thinking:
            # 使用工具的流式思考接口
            if self.use_deeper_tool:
                async for chunk in self.research_tool.thinking_stream(query):
                    if isinstance(chunk, dict) and "answer" in chunk:
                        # 这是最终答案，处理后返回
                        final_answer = chunk["answer"]
                        
                        # 如果包含思考标记，提取干净部分
                        if "<think>" in final_answer and "</think>" in final_answer:
                            clean_answer = re.sub(r'<think>.*?</think>\s*', '', final_answer, flags=re.DOTALL)
                            
                            # 缓存清理后的答案
                            if clean_answer and len(clean_answer) > 10:
                                self.cache_manager.set(f"deep:{query}", clean_answer, thread_id=thread_id)
                            
                            yield clean_answer
                        else:
                            # 没有思考标记，直接使用
                            if final_answer and len(final_answer) > 10:
                                self.cache_manager.set(f"deep:{query}", final_answer, thread_id=thread_id)
                            
                            yield final_answer
                    else:
                        # 思考过程，直接返回
                        yield chunk
            else:
                # 标准版深度研究工具
                async for chunk in self.research_tool.thinking_stream(query):
                    if isinstance(chunk, dict) and "answer" in chunk:
                        # 最终答案，提取干净部分
                        final_answer = chunk["answer"]
                        if "<think>" in final_answer and "</think>" in final_answer:
                            clean_answer = re.sub(r'<think>.*?</think>\s*', '', final_answer, flags=re.DOTALL)
                            # 缓存清理后的答案
                            if clean_answer and len(clean_answer) > 10:
                                self.cache_manager.set(f"deep:{query}", clean_answer, thread_id=thread_id)
                            yield clean_answer
                        else:
                            # 没有思考标记，直接使用
                            if final_answer and len(final_answer) > 10:
                                self.cache_manager.set(f"deep:{query}", final_answer, thread_id=thread_id)
                            yield final_answer
                    else:
                        # 思考过程，直接返回
                        yield chunk
        else:
            # 普通搜索，仅返回最终答案
            if self.use_deeper_tool:
                # 增强版深度研究工具的流式搜索
                async for chunk in self.research_tool.search_stream(query):
                    yield chunk
            else:
                # 标准版深度研究工具的流式搜索
                async for chunk in self.research_tool.search_stream(query):
                    yield chunk

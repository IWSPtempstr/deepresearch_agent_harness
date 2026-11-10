# 由 agents/base.py 拆分（T-014），方法体与原位置逐行一致。
"""问答入口三件套（原 ask_with_trace/ask/ask_stream）。"""

from typing import Dict, AsyncGenerator, Optional
from langchain_core.messages import HumanMessage
import pprint
import time
import asyncio


class AgentAskMixin:
    def ask_with_trace(self, query: str, thread_id: str = "default", recursion_limit: Optional[int] = None) -> Dict:
        """执行查询并获取带执行轨迹的回答"""
        overall_start = time.time()
        self.execution_log = []  # 重置执行日志
        recursion_limit = (
            recursion_limit
            if recursion_limit is not None
            else self.default_recursion_limit
        )
        
        # 确保查询字符串是干净的
        safe_query = query.strip()
        
        # 首先尝试全局缓存（跨会话缓存）
        global_cache_start = time.time()
        global_result = self.global_cache_manager.get(safe_query)
        global_cache_time = time.time() - global_cache_start
        
        if global_result:
            print(f"全局缓存命中: {safe_query[:30]}... ({global_cache_time:.4f}s)")
            
            return {
                "answer": global_result,
                "execution_log": [{"node": "global_cache_hit", "timestamp": time.time(), "input": safe_query, "output": "全局缓存命中"}]
            }
        
        # 首先尝试快速路径 - 跳过验证的高质量缓存
        fast_cache_start = time.time()
        fast_result = self.check_fast_cache(safe_query, thread_id)
        fast_cache_time = time.time() - fast_cache_start
        
        if fast_result:
            print(f"快速路径缓存命中: {safe_query[:30]}... ({fast_cache_time:.4f}s)")
            
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(safe_query, fast_result)
            
            return {
                "answer": fast_result,
                "execution_log": [{"node": "fast_cache_hit", "timestamp": time.time(), "input": safe_query, "output": "高质量缓存命中"}]
            }
        
        # 尝试常规缓存路径
        cache_start = time.time()
        cached_response = self.cache_manager.get(safe_query, thread_id=thread_id)
        cache_time = time.time() - cache_start
        
        if cached_response:
            print(f"完整问答缓存命中: {safe_query[:30]}... ({cache_time:.4f}s)")
            
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(safe_query, cached_response)
            
            return {
                "answer": cached_response,
                "execution_log": [{"node": "cache_hit", "timestamp": time.time(), "input": safe_query, "output": "常规缓存命中"}]
            }
        
        # 未命中缓存，执行标准流程
        process_start = time.time()
        
        config = {
            "configurable": {
                "thread_id": thread_id,
                "recursion_limit": recursion_limit
            }
        }
        
        inputs = {"messages": [HumanMessage(content=query)]}
        try:
            # 执行完整的处理流程
            for output in self.graph.stream(inputs, config=config):
                pprint.pprint(f"Output from node '{list(output.keys())[0]}':")
                pprint.pprint("---")
                pprint.pprint(output, indent=2, width=80, depth=None)
                pprint.pprint("\n---\n")
                
            chat_history = self.memory.get(config)["channel_values"]["messages"]
            answer = chat_history[-1].content
            
            # 缓存处理结果 - 同时更新会话缓存和全局缓存
            if answer and len(answer) > 10:
                # 更新会话缓存
                self.cache_manager.set(safe_query, answer, thread_id=thread_id)
                # 更新全局缓存
                self.global_cache_manager.set(safe_query, answer)
            
            process_time = time.time() - process_start
            print(f"完整处理耗时: {process_time:.4f}s")
            
            overall_time = time.time() - overall_start
            self._log_performance("ask_with_trace", {
                "total_duration": overall_time,
                "cache_check": cache_time,
                "processing": process_time
            })
            
            return {
                "answer": answer,
                "execution_log": self.execution_log
            }
        except Exception as e:
            error_time = time.time() - process_start
            print(f"处理查询时出错: {e} ({error_time:.4f}s)")
            return {
                "answer": f"抱歉，处理您的问题时遇到了错误。请稍后再试或换一种提问方式。错误详情: {str(e)}",
                "execution_log": self.execution_log + [{"node": "error", "timestamp": time.time(), "input": query, "output": str(e)}]
            }

    def ask(self, query: str, thread_id: str = "default", recursion_limit: Optional[int] = None, *, bypass_cache: bool = False):
        """向Agent提问"""
        overall_start = time.time()
        
        # 确保查询字符串是干净的
        safe_query = query.strip()
        
        if not bypass_cache:
            cached_result = self._check_all_caches(safe_query, thread_id)
            if cached_result:
                return cached_result
        
        # 未命中缓存，执行标准流程
        process_start = time.time()
        
        recursion_value = (
            recursion_limit
            if recursion_limit is not None
            else self.default_recursion_limit
        )
        
        # 正常处理请求
        config = {
            "configurable": {
                "thread_id": thread_id,
                "recursion_limit": recursion_value
            }
        }
        
        inputs = {"messages": [HumanMessage(content=query)]}
        try:
            for output in self.graph.stream(inputs, config=config):
                pass
                    
            chat_history = self.memory.get(config)["channel_values"]["messages"]
            answer = chat_history[-1].content
            
            # 缓存处理结果 - 同时更新会话缓存和全局缓存
            if not bypass_cache and answer and len(answer) > 10:
                # 更新会话缓存
                self.cache_manager.set(safe_query, answer, thread_id=thread_id)
                # 更新全局缓存
                self.global_cache_manager.set(safe_query, answer)
            
            process_time = time.time() - process_start
            overall_time = time.time() - overall_start
            
            self._log_performance("ask", {
                "total_duration": overall_time,
                "cache_check": 0,  # 由_check_all_caches记录
                "processing": process_time
            })
            
            return answer
        except Exception as e:
            error_time = time.time() - process_start
            print(f"处理查询时出错: {e} ({error_time:.4f}s)")
            return f"抱歉，处理您的问题时遇到了错误。请稍后再试或换一种提问方式。错误详情: {str(e)}"

    async def ask_stream(self, query: str, thread_id: str = "default", recursion_limit: Optional[int] = None) -> AsyncGenerator[str, None]:
        """
        向Agent提问，返回流式响应
        
        参数:
            query: 用户问题
            thread_id: 会话ID
            recursion_limit: 递归限制
                
        返回:
            AsyncGenerator[str, None]: 流式响应生成器
        """
        overall_start = time.time()
        
        # 确保查询字符串是干净的
        safe_query = query.strip()
        
        # 首先尝试全局缓存（跨会话缓存）
        global_result = self.global_cache_manager.get(safe_query)
        if global_result:
            # 对于缓存响应，按自然语言单位分块返回
            import re
            chunks = re.split(r'([.!?。！？]\s*)', global_result)
            buffer = ""
            
            for i in range(0, len(chunks)):
                buffer += chunks[i]
                
                # 当缓冲区包含完整句子或达到合理大小时输出
                if (i % 2 == 1) or len(buffer) >= self.stream_flush_threshold:
                    yield buffer
                    buffer = ""
                    await asyncio.sleep(0.01)
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
            return
        
        # 首先尝试快速路径 - 跳过验证的高质量缓存
        fast_result = self.check_fast_cache(safe_query, thread_id)
        if fast_result:
            # 对于缓存响应，按自然语言单位分块返回
            import re
            chunks = re.split(r'([.!?。！？]\s*)', fast_result)
            buffer = ""
            
            for i in range(0, len(chunks)):
                buffer += chunks[i]
                
                # 当缓冲区包含完整句子或达到合理大小时输出
                if (i % 2 == 1) or len(buffer) >= self.stream_flush_threshold:
                    yield buffer
                    buffer = ""
                    await asyncio.sleep(0.01)
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
                
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(safe_query, fast_result)
            return
        
        # 尝试常规缓存路径
        cache_start = time.time()
        cached_response = self.cache_manager.get(safe_query, thread_id=thread_id)
        cache_time = time.time() - cache_start
        
        if cached_response:
            # 同样按自然语言单位分块
            import re
            chunks = re.split(r'([.!?。！？]\s*)', cached_response)
            buffer = ""
            
            for i in range(0, len(chunks)):
                buffer += chunks[i]
                
                # 当缓冲区包含完整句子或达到合理大小时输出
                if (i % 2 == 1) or len(buffer) >= self.stream_flush_threshold:
                    yield buffer
                    buffer = ""
                    await asyncio.sleep(0.01)
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
                
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(safe_query, cached_response)
            return
        
        # 未命中缓存，执行标准流程
        recursion_value = (
            recursion_limit
            if recursion_limit is not None
            else self.default_recursion_limit
        )
        config = {
            "configurable": {
                "thread_id": thread_id,
                "recursion_limit": recursion_value,
                "stream_mode": True  # 指示流式输出模式
            }
        }
        
        inputs = {"messages": [HumanMessage(content=query)]}
        answer = ""
        
        try:
            # 执行流式处理
            async for chunk in self._stream_process(inputs, config):
                yield chunk
                answer += chunk
            
            # 缓存完整回答 - 同时更新会话缓存和全局缓存
            if answer and len(answer) > 10:
                # 更新会话缓存
                self.cache_manager.set(safe_query, answer, thread_id=thread_id)
                # 更新全局缓存
                self.global_cache_manager.set(safe_query, answer)
            
            process_time = time.time() - overall_start
            self._log_performance("ask_stream", {
                "total_duration": process_time,
                "processing": process_time
            })
            
        except Exception as e:
            error_time = time.time() - overall_start
            error_msg = f"处理查询时出错: {str(e)} ({error_time:.4f}s)"
            print(error_msg)
            yield error_msg

# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""流式思考与流式搜索（原 thinking_stream/search_stream/_fix_answer）。"""

from typing import Any, AsyncGenerator
import time
import re
import asyncio
from deepresearch_agent.search.tool.reasoning.prompts import kb_prompt
from deepresearch_agent.search.tool.reasoning.search import QueryGenerator


class DeepStreamingMixin:
    async def thinking_stream(self, query: str) -> AsyncGenerator[str, None]:
        """
        执行深度研究推理过程，流式返回
        
        参数:
            query: 用户问题
                    
        返回:
            AsyncGenerator: 流式生成的思考和答案内容
        """
        # 清空执行日志
        self.execution_logs = []
        self._log(f"\n[深度研究] 开始处理查询: {query}")
        
        # 初始化结果容器
        chunk_info = {"chunks": [], "doc_aggs": []}
        self.all_retrieved_info = []
        
        # 初始化思考引擎
        self.thinking_engine.initialize_with_query(query)

        # 使用QueryGenerator生成初始子查询
        yield "\n**正在分析您的问题，生成研究方向**...\n"
        
        # 异步生成子查询
        def generate_sub_queries():
            return self.query_generator.generate_sub_queries(query)
        
        initial_sub_queries = await asyncio.to_thread(generate_sub_queries)
        self._log(f"\n[深度研究] 生成了{len(initial_sub_queries)}个初始子查询")
        
        think = ""
        
        # 将初始思考添加到思考过程
        initial_thinking = f"我需要回答问题：{query}\n\n"
        initial_thinking += "为了全面解答这个问题，我将从以下几个方面进行研究：\n"
        for i, sub_q in enumerate(initial_sub_queries, 1):
            initial_thinking += f"{i}. {sub_q}\n"
        initial_thinking += "\n让我逐步进行搜索和分析。"
        
        self.thinking_engine.add_reasoning_step(initial_thinking)
        think += initial_thinking
        
        # 分组返回初始思考内容
        yield initial_thinking
        
        # 迭代思考过程
        for iteration in range(self.max_iterations):
            # 发送迭代进度
            if iteration > 0:
                yield f"\n\n**正在进行第{iteration + 1}轮思考**...\n\n"

            self._log(f"\n[深度研究] 开始第{iteration + 1}轮迭代")
            await self._progress("iteration", iteration_index=iteration)

            # 检查是否达到最大迭代次数
            if iteration >= self.max_iterations - 1:
                summary_think = f"\n搜索次数已达上限。不允许继续搜索。\n"
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(summary_think)
                think += self.thinking_engine.remove_result_tags(summary_think)
                yield "**\n已达到最大搜索次数限制，准备生成最终答案**...\n"
                break

            # 更新消息历史，请求继续推理
            self.thinking_engine.update_continue_message()
            
            # 让事件循环有机会执行其他任务
            await asyncio.sleep(0)
            
            # 确定当前迭代要处理的查询
            queries_to_process = []
            
            if iteration == 0 and initial_sub_queries:
                # 第一轮迭代使用预先生成的子查询
                queries_to_process = initial_sub_queries[:2]  # 限制首轮使用的子查询数量
                query_think = "开始根据分解的子问题进行搜索"
                yield "\n**开始根据分解的子问题进行初始搜索**...\n"
            else:
                # 非首轮，使用思考引擎生成下一步查询
                result = await self._async_generate_next_query()
                
                # 处理生成结果
                if result["status"] == "empty":
                    empty_msg = "未能产生新的思考角度，尝试从新角度探索问题..."
                    self._log(empty_msg)
                    yield empty_msg
                    
                    # 异步生成多假设
                    def generate_hypotheses():
                        return QueryGenerator.generate_multiple_hypotheses(query, self.llm)
                    
                    hypotheses = await asyncio.to_thread(generate_hypotheses)
                    
                    if hypotheses:
                        self._log(f"\n[深度研究] 生成了{len(hypotheses)}个新假设，尝试从新角度探索")
                        queries_to_process = hypotheses
                        query_think = "尝试从新的角度思考这个问题:\n" + "\n".join([f"- {h}" for h in hypotheses])
                        self.thinking_engine.add_reasoning_step(query_think)
                        think += query_think
                        yield "\n**尝试从新角度思考问题**:\n" + query_think
                    else:
                        continue
                elif result["status"] == "error":
                    error_msg = f"生成查询时遇到错误: {result.get('error', '未知错误')}"
                    self._log(error_msg)
                    yield error_msg
                    break
                elif result["status"] == "answer_ready":
                    ready_msg = "已收集足够信息，准备生成答案..."
                    self._log(ready_msg)
                    yield ready_msg
                    break
                            
                else:
                    # 获取生成的思考内容
                    query_think = result["content"]
                    think_part = self.thinking_engine.remove_query_tags(query_think)
                    think += think_part
                    
                    # 分组返回思考内容，提高可读性
                    thoughts = re.split(r'(\n\n)', think_part)
                    thought_buffer = ""
                    
                    for part in thoughts:
                        thought_buffer += part
                        if len(thought_buffer) >= 80 or "\n\n" in thought_buffer:
                            yield thought_buffer
                            thought_buffer = ""
                            await asyncio.sleep(0.01)
                            
                    if thought_buffer:
                        yield thought_buffer
                    
                    # 获取搜索查询
                    queries_to_process = result["queries"]
                
            # 如果当前迭代没有查询，且我们已经检索到一些信息，尝试生成跟进查询
            if not queries_to_process and self.all_retrieved_info and iteration > 0:
                yield "\n**基于已发现的信息生成后续查询**...\n"
                
                # 异步生成跟进查询
                def generate_followup():
                    return self.query_generator.generate_followup_queries(query, self.all_retrieved_info)
                
                followup_queries = await asyncio.to_thread(generate_followup)
                
                if followup_queries:
                    self._log(f"\n[深度研究] 生成了{len(followup_queries)}个跟进查询")
                    queries_to_process = followup_queries
                    followup_think = "\n考虑到已发现的信息，我需要进一步探索以下问题:\n"
                    for i, fq in enumerate(followup_queries, 1):
                        followup_think += f"{i}. {fq}\n"
                    self.thinking_engine.add_reasoning_step(followup_think)
                    think += followup_think
                    yield followup_think
            
            # 如果没有生成搜索查询但不是第一轮，考虑结束
            if not queries_to_process:
                if not self.all_retrieved_info and iteration == 0:
                    # 如果第一轮没检索到任何信息，强制使用原始查询
                    queries_to_process = [query]
                    no_info_msg = "\n\n**没有检索到信息，尝试使用原始问题直接查询**...\n\n"
                    self._log(no_info_msg)
                    yield no_info_msg
                else:
                    # 已有信息，结束迭代
                    end_msg = "\n\n**没有发现新的查询角度，基于已有信息生成回答**...\n\n"
                    self._log(end_msg)
                    yield end_msg
                    break
            
            # 处理每个搜索查询
            for search_query in queries_to_process:
                search_start_msg = f"\n**正在搜索: {search_query}**\n"
                self._log(search_start_msg)
                yield search_start_msg
                    
                # 检查是否已执行过相同查询
                if self.thinking_engine.has_executed_query(search_query):
                    dupe_msg = f"\n**已搜索过类似查询，跳过重复执行**\n"
                    self._log(dupe_msg)
                    yield dupe_msg
                    continue
                    
                # 记录已执行查询
                self.thinking_engine.add_executed_query(search_query)
                    
                # 将搜索查询添加到消息历史
                self.thinking_engine.add_ai_message(f"{search_query}")
                think += f"\n\n> {iteration + 1}. {search_query}\n\n"
                    
                # 让事件循环有机会执行其他任务
                await asyncio.sleep(0)
                    
                # 执行实际搜索
                yield "\n**正在查询知识库**...\n"
                kbinfos = await self._async_search(search_query)
                    
                # 检查搜索结果是否为空
                has_results = (
                    kbinfos.get("chunks", []) or 
                    kbinfos.get("entities", []) or 
                    kbinfos.get("relationships", [])
                )
                    
                if not has_results:
                    no_result_msg = f"\n**没有找到与{search_query}相关的信息，尝试其他角度**...\n"
                    self._log(no_result_msg)
                    yield no_result_msg
                    self.thinking_engine.add_reasoning_step(f"\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n")
                    self.thinking_engine.add_human_message(f"\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n")
                    think += no_result_msg
                    await self._progress(
                        "search",
                        iteration_index=iteration,
                        query=search_query,
                        result_count=0,
                        found_useful=False,
                        useful_info_preview=None,
                    )
                    continue
                    
                # 正常处理有结果的情况
                truncated_prev_reasoning = self.thinking_engine.prepare_truncated_reasoning()
                    
                # 合并块信息
                chunk_info = self.dual_searcher._merge_results(chunk_info, kbinfos)
                    
                # 构建提取相关信息的提示
                kb_prompt_result = "\n".join(kb_prompt(kbinfos, 4096))
                    
                # 告知用户正在分析结果
                yield "\n**正在分析搜索结果**...\n"
                    
                # 使用异步LLM提取有用信息
                summary_think = await self._async_extract_info(search_query, truncated_prev_reasoning, kb_prompt_result)
                    
                # 保存重要信息
                has_useful_info = (
                    "**Final Information**" in summary_think and 
                    "No helpful information found" not in summary_think
                )
                    
                if has_useful_info:
                    useful_info = summary_think.split("**Final Information**")[1].strip()
                    self.all_retrieved_info.append(useful_info)
                    info_msg = f"发现有用信息: {useful_info[:100]}..."
                    self._log(info_msg)
                    yield "\n**找到相关信息！**\n"
                else:
                    no_useful_msg = "**\n未从搜索结果中发现特别有价值的信息**\n"
                    self._log(no_useful_msg)
                    yield no_useful_msg
                    
                # 上报本轮搜索的进度事件
                await self._progress(
                    "search",
                    iteration_index=iteration,
                    query=search_query,
                    result_count=sum(len(kbinfos.get(k, [])) for k in ("chunks", "entities", "relationships")),
                    found_useful=has_useful_info,
                    useful_info_preview=(useful_info[:100] + ("..." if len(useful_info) > 100 else "")) if has_useful_info else None,
                )

                # 更新推理历史
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(summary_think)
                think += self.thinking_engine.remove_result_tags(summary_think)
                    
                # 分组返回处理后的思考内容
                result_parts = re.split(r'(\n\n)', self.thinking_engine.remove_result_tags(summary_think))
                result_buffer = ""
                
                for part in result_parts:
                    result_buffer += part
                    if len(result_buffer) >= 80 or "\n\n" in result_buffer:
                        yield result_buffer
                        result_buffer = ""
                        await asyncio.sleep(0.01)
                        
                if result_buffer:
                    yield result_buffer
            
            # 本轮迭代结束：上报聚合进度事件
            await self._progress("iteration_done", iteration_index=iteration)

            # 在每轮迭代结束后，评估是否需要继续搜索
            if iteration > 0 and self.all_retrieved_info:
                # 异步判断是否需要继续生成查询
                def check_gap_needed():
                    return len(self.query_generator.generate_followup_queries(query, self.all_retrieved_info)) > 0
                
                gap_needed = await asyncio.to_thread(check_gap_needed)
                
                if not gap_needed:
                    reflection_msg = "\n**已收集到足够的信息，可以开始整合分析了**\n"
                    self._log(reflection_msg)
                    yield reflection_msg
                    self.thinking_engine.add_reasoning_step("\n已收集到足够的信息，可以开始整合分析了。")
                    think += "\n已收集到足够的信息，可以开始整合分析了。"
                    break
        
        # 确保至少执行了一次搜索
        if not self.thinking_engine.executed_search_queries:
            no_search_msg = f"\n**无法找到与{query}相关的信息，尝试给出基础回答**...\n"
            yield no_search_msg
            return
        
        # 生成最终答案
        yield "\n**正在根据所有收集的信息生成最终答案**...\n"
        
        # 使用检索到的信息生成答案
        retrieved_content = "\n\n".join(self.all_retrieved_info)
        final_answer = await self._async_generate_final_answer(query, retrieved_content, think)

        # 上报最终答案事件
        await self._progress("answer", iteration_index=iteration, answer_char_count=len(final_answer))

        # 向用户发送最终答案（一次性发送，因为前端会替换整个响应）
        yield {"answer": final_answer, "thinking": think}

    async def search_stream(self, query_input: Any) -> AsyncGenerator[str, None]:
        """
        执行深度研究搜索，流式返回
        
        参数:
            query_input: 搜索查询或包含查询的字典
                
        返回:
            AsyncGenerator: 流式输出
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
            self._log(f"\n[深度搜索] 缓存命中，分块返回缓存结果")
            # 分块返回缓存结果 - 更自然的分块
            chunks = re.split(r'([.!?。！？]\s*)', cached_result)
            buffer = ""
            
            for i in range(0, len(chunks)):
                buffer += chunks[i]
                
                # 当缓冲区包含完整句子或达到合理大小时输出
                if (i % 2 == 1) or len(buffer) >= 80:
                    yield buffer
                    buffer = ""
                    await asyncio.sleep(0.01)
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
            return
        
        try:
            # 执行思考过程流
            full_response = ""
            thinking_content = ""
            
            # 提示用户处理开始
            yield "\n**开始深度分析您的问题**...\n"
            
            # 使用更高级的流式思考过程
            async for chunk in self.thinking_stream(query):
                if isinstance(chunk, dict) and "answer" in chunk:
                    # 这是最终结果对象
                    full_response = chunk["answer"]
                    thinking_content = chunk["thinking"]
                    
                    # 不立即返回，而是检查并处理
                    validation_results = self.validator.validate(query, full_response)
                    if validation_results["passed"]:
                        self._log(f"\n[深度搜索] 答案验证通过，缓存结果")
                        self.cache_manager.set(cache_key, full_response)
                        
                        # 将思考过程和最终答案分离
                        if "<think>" in full_response and "</think>" in full_response:
                            clean_answer = re.sub(r'<think>.*?</think>\s*', '', full_response, flags=re.DOTALL)
                            yield clean_answer
                        else:
                            yield full_response
                    else:
                        self._log(f"\n[深度搜索] 答案验证失败，尝试修复")
                        yield "\n**正在完善最终答案**...\n"
                        
                        # 尝试修复答案
                        fixed_answer = await self._fix_answer(query, full_response)
                        yield fixed_answer
                else:
                    # 返回思考过程，避免发送太小的片段
                    yield chunk
            
            # 记录总时间
            total_time = time.time() - overall_start
            self._log(f"\n[深度搜索] 完成，耗时 {total_time:.2f}秒")
            self.performance_metrics["total_time"] = total_time
                
        except Exception as e:
            error_msg = f"深度研究过程中出错: {str(e)}"
            self._log(error_msg)
            yield f"**很抱歉，在处理您的问题时遇到了错误**: {str(e)}"

    async def _fix_answer(self, query, answer):
        """尝试修复低质量答案"""
        fix_prompt = f"""
        原问题是: {query}
        
        生成的答案可能存在问题: {answer}
        
        请提供一个修正后、质量更高的答案，更好地回应用户的问题。
        确保答案:
        1. 直接回答问题核心
        2. 删除不必要的重复内容
        3. 去除表示不确定的语言
        4. 结构清晰，重点突出
        """
        
        def llm_fix():
            response = self.llm.invoke(fix_prompt)
            return response.content if hasattr(response, 'content') else str(response)
        
        # 在线程池中运行同步LLM调用  
        return await asyncio.to_thread(llm_fix)

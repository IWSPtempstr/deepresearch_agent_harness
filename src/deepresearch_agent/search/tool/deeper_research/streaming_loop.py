# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""原 thinking_stream 的迭代思考主循环（原 1565-1950 行）。"""

import asyncio
import re
from deepresearch_agent.search.tool.reasoning.prompts import kb_prompt


class DeeperStreamIterateMixin:
    async def _stream_iterate(self, query, ctx):
        """原 thinking_stream 迭代主循环（原 1565-1950 行）。"""
        think = ctx["think"]
        query_id = ctx["query_id"]
        initial_sub_queries = ctx["initial_sub_queries"]
        # 迭代思考过程
        for iteration in range(self.deep_research.max_iterations):
            # 发送迭代进度
            iter_msg = f"\n\n**正在进行第{iteration + 1}轮思考**...\n\n"
            yield iter_msg
            self._log(f"\n[深度研究] 开始第{iteration + 1}轮迭代\n")
            await self._progress("iteration", iteration_index=iteration)
            
            # 跟踪迭代步骤
            iteration_step_id = self.evidence_tracker.add_reasoning_step(
                query_id,
                f"iteration_{iteration}",
                f"\n开始第 {iteration + 1} 轮迭代思考\n"
            )
            
            # 检查是否达到最大迭代次数
            if iteration >= self.deep_research.max_iterations - 1:
                limit_msg = "**\n已达到最大搜索次数限制，准备生成最终答案**...\n"
                yield limit_msg
                self._log("\n[深度研究] 达到最大迭代次数，结束搜索")
                
                summary_think = f"\n搜索次数已达上限。不允许继续搜索。\n"
                self.deep_research.thinking_engine.add_reasoning_step(summary_think)
                self.deep_research.thinking_engine.add_human_message(summary_think)
                think += self.deep_research.thinking_engine.remove_result_tags(summary_think)
                break
            
            # 更新消息历史，请求继续推理
            self.deep_research.thinking_engine.update_continue_message()
            
            # 让事件循环有机会执行其他任务
            await asyncio.sleep(0)
            
            # 确定当前迭代要处理的查询
            queries_to_process = []
            
            if iteration == 0 and initial_sub_queries:
                # 第一轮迭代使用预先生成的子查询
                queries_to_process = initial_sub_queries[:2]  # 限制首轮使用的子查询数量
                yield "\n**开始根据分解的子问题进行初始搜索**...\n"
            else:
                # 非首轮，使用思考引擎生成下一步查询
                result = await self.deep_research._async_generate_next_query()
                
                # 处理生成结果
                if result["status"] == "empty":
                    empty_msg = "未能产生新的思考角度，尝试从新角度探索问题..."
                    self._log(empty_msg)
                    yield empty_msg
                    
                    # 异步生成多假设
                    def generate_hypotheses():
                        return self.query_generator.generate_multiple_hypotheses(query, self.llm)
                    
                    hypotheses = await asyncio.to_thread(generate_hypotheses)
                    
                    if hypotheses:
                        self._log(f"\n[深度研究] 生成了{len(hypotheses)}个新假设，尝试从新角度探索")
                        queries_to_process = hypotheses
                        query_think = "尝试从新的角度思考这个问题:\n" + "\n".join([f"- {h}" for h in hypotheses])
                        self.deep_research.thinking_engine.add_reasoning_step(query_think)
                        think += query_think
                        yield "\n**尝试从新角度思考问题**:\n" + query_think
                    else:
                        continue
                elif result["status"] == "error":
                    error_msg = f"生成查询时遇到错误: {result.get('error', '未知错误')}"
                    self._log(error_msg)
                    yield error_msg
                    
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "generation_error",
                        f"生成查询出错: {result.get('error', '未知错误')}",
                        "reasoning_status"
                    )
                    break
                elif result["status"] == "answer_ready":
                    ready_msg = "已收集足够信息，准备生成答案..."
                    self._log(ready_msg)
                    yield ready_msg
                    
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "reasoning_complete",
                        "AI认为已有足够信息生成答案",
                        "reasoning_status"
                    )
                    break
                            
                else:
                    # 获取生成的思考内容
                    query_think = result["content"]
                    think_part = self.deep_research.thinking_engine.remove_query_tags(query_think)
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
            if not queries_to_process and self.deep_research.all_retrieved_info and iteration > 0:
                yield "\n**基于已发现的信息生成后续查询**...\n"
                
                # 异步生成跟进查询
                def generate_followup():
                    return self.query_generator.generate_followup_queries(query, self.deep_research.all_retrieved_info)
                
                followup_queries = await asyncio.to_thread(generate_followup)
                
                if followup_queries:
                    self._log(f"\n[深度研究] 生成了{len(followup_queries)}个跟进查询")
                    queries_to_process = followup_queries
                    followup_think = "\n考虑到已发现的信息，我需要进一步探索以下问题:\n"
                    for i, fq in enumerate(followup_queries, 1):
                        followup_think += f"{i}. {fq}\n"
                    self.deep_research.thinking_engine.add_reasoning_step(followup_think)
                    think += followup_think
                    yield followup_think
                    
                    # 记录为推理步骤
                    followup_step_id = self.evidence_tracker.add_reasoning_step(
                        query_id,
                        "followup_queries_generation",
                        f"基于已收集的信息生成跟进查询: {followup_queries}"
                    )
            
            # 如果没有生成搜索查询但不是第一轮，考虑结束
            if not queries_to_process:
                if not self.deep_research.all_retrieved_info and iteration == 0:
                    # 如果第一轮没检索到任何信息，强制使用原始查询
                    queries_to_process = [query]
                    no_info_msg = "\n\n**没有检索到信息，尝试使用原始问题直接查询**...\n\n"
                    self._log(no_info_msg)
                    yield no_info_msg
                    
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "query_fallback",
                        "没有检索到信息，使用原始查询",
                        "reasoning_status"
                    )
                else:
                    # 已有信息，结束迭代
                    end_msg = "\n\n**没有发现新的查询角度，基于已有信息生成回答**...\n\n"
                    self._log(end_msg)
                    yield end_msg
                    
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "iteration_complete",
                        "没有生成新查询且已有信息，结束迭代",
                        "reasoning_status"
                    )
                    break
            
            # 处理每个搜索查询
            for search_query in queries_to_process:
                search_start_msg = f"\n**正在搜索: {search_query}**\n"
                self._log(search_start_msg)
                yield search_start_msg
                    
                # 检查是否已执行过相同查询
                if self.deep_research.thinking_engine.has_executed_query(search_query):
                    dupe_msg = f"\n**已搜索过类似查询，跳过重复执行**\n"
                    self._log(dupe_msg)
                    yield dupe_msg
                    
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "duplicate_query",
                        "已搜索过该查询，跳过重复执行",
                        "search_status"
                    )
                    continue
                    
                # 记录已执行查询
                self.deep_research.thinking_engine.add_executed_query(search_query)
                    
                # 将搜索查询添加到消息历史
                self.deep_research.thinking_engine.add_ai_message(f"{search_query}")
                think += f"\n\n> {iteration + 1}. {search_query}\n\n"
                    
                # 让事件循环有机会执行其他任务
                await asyncio.sleep(0)
                    
                # 执行实际搜索 - 同时应用Chain of Exploration增强
                yield "\n**正在查询知识库**...\n"
                
                # 记录查询步骤
                search_step_id = self.evidence_tracker.add_reasoning_step(
                    query_id,
                    search_query,
                    f"执行查询: {search_query}"
                )
                
                # 提取搜索查询的关键词
                search_keywords = self.extract_keywords(search_query)
                
                # 使用Chain of Exploration增强搜索
                enhanced_search_context = await self._async_enhance_search(search_query, search_keywords)
                exploration_content = []
                
                # 从Chain of Exploration结果中提取内容
                if "exploration_results" in enhanced_search_context:
                    for content_item in enhanced_search_context["exploration_results"].get("content", []):
                        if "text" in content_item:
                            exploration_content.append({
                                "chunk_id": content_item.get("id", "exploration_content"),
                                "text": content_item["text"],
                                "content_with_weight": content_item["text"],
                                "weight": 1.0
                            })
                
                # 原始搜索，合并结果
                kbinfos = await self.deep_research._async_search(search_query)
                
                # 将探索内容加入结果
                if exploration_content:
                    if "chunks" not in kbinfos:
                        kbinfos["chunks"] = []
                    kbinfos["chunks"].extend(exploration_content)
                    
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
                    
                    self.evidence_tracker.add_evidence(
                        search_step_id,
                        "no_results",
                        f"没有找到与查询相关的信息: {search_query}",
                        "search_result"
                    )
                    
                    self.deep_research.thinking_engine.add_reasoning_step(f"\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n")
                    self.deep_research.thinking_engine.add_human_message(f"\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n")
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
                truncated_prev_reasoning = self.deep_research.thinking_engine.prepare_truncated_reasoning()
                    
                # 为查询结果更新知识图谱
                if "chunks" in kbinfos:
                    for chunk in kbinfos.get("chunks", []):
                        chunk_id = chunk.get("chunk_id", "")
                        chunk_text = chunk.get("text", "")
                        
                        if chunk_id and chunk_text:
                            # 记录chunk作为证据
                            self.evidence_tracker.add_evidence(
                                search_step_id,
                                chunk_id,
                                chunk_text,
                                "document_chunk"
                            )
                            
                            # 尝试从chunk中提取实体和关系
                            if hasattr(self.knowledge_builder, 'extractor') and self.knowledge_builder.extractor:
                                try:
                                    self.knowledge_builder.extract_subgraph_from_chunk(
                                        chunk_text, 
                                        chunk_id
                                    )
                                except Exception as e:
                                    self._log(f"\n[深度研究] 提取实体关系失败: {e}")
                    
                # 构建提取相关信息的提示
                kb_prompt_result = "\n".join(kb_prompt(kbinfos, 4096))
                    
                # 告知用户正在分析结果
                yield "\n**正在分析搜索结果**...\n"
                    
                # 使用异步LLM提取有用信息
                summary_think = await self.deep_research._async_extract_info(search_query, truncated_prev_reasoning, kb_prompt_result)
                    
                # 保存重要信息
                has_useful_info = (
                    "**Final Information**" in summary_think and 
                    "No helpful information found" not in summary_think
                )
                    
                if has_useful_info:
                    useful_info = summary_think.split("**Final Information**")[1].strip()
                    self.deep_research.all_retrieved_info.append(useful_info)
                    info_msg = f"发现有用信息: {useful_info[:100]}..."
                    self._log(info_msg)
                    yield "\n**找到相关信息！**\n"
                    
                    # 记录有用信息作为证据
                    self.evidence_tracker.add_evidence_with_confidence(
                        search_step_id,
                        f"useful_info_{search_query}",
                        useful_info,
                        "extracted_knowledge",
                        confidence=0.85,  # 高可信度
                        metadata={"query": search_query}
                    )
                else:
                    no_useful_msg = "**\n未从搜索结果中发现特别有价值的信息**\n"
                    self._log(no_useful_msg)
                    yield no_useful_msg
                    
                    self.evidence_tracker.add_evidence(
                        search_step_id,
                        f"no_useful_info_{search_query}",
                        "未从检索结果中提取到有用信息",
                        "extraction_status"
                    )
                    
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
                self.deep_research.thinking_engine.add_reasoning_step(summary_think)
                self.deep_research.thinking_engine.add_human_message(summary_think)
                think += self.deep_research.thinking_engine.remove_result_tags(summary_think)
                    
                # 分组返回处理后的思考内容
                result_parts = re.split(r'(\n\n)', self.deep_research.thinking_engine.remove_result_tags(summary_think))
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
            if iteration > 0 and self.deep_research.all_retrieved_info:
                # 异步判断是否需要继续生成查询
                def check_gap_needed():
                    return len(self.query_generator.generate_followup_queries(query, self.deep_research.all_retrieved_info)) > 0
                
                gap_needed = await asyncio.to_thread(check_gap_needed)
                
                if not gap_needed:
                    reflection_msg = "\n**已收集到足够的信息，可以开始整合分析了**\n"
                    self._log(reflection_msg)
                    yield reflection_msg
                    self.deep_research.thinking_engine.add_reasoning_step("\n已收集到足够的信息，可以开始整合分析了。")
                    think += "\n已收集到足够的信息，可以开始整合分析了。"
                    break
        ctx["think"] = think
        ctx["iteration"] = iteration  # 原循环变量在循环后仍被引用（原 2127 行）


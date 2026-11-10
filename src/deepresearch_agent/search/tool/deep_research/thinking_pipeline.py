# 由 search/tool/deep_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""关键词提取与多轮"思考-检索-推理"流水线（原 thinking 及其异步辅助）。"""

from typing import Dict, List
import uuid
import re
from langchain_core.messages import SystemMessage, HumanMessage
import asyncio
from deepresearch_agent.config.prompts import BEGIN_SEARCH_QUERY, BEGIN_SEARCH_RESULT, END_SEARCH_RESULT, END_SEARCH_QUERY, RELEVANT_EXTRACTION_PROMPT, FINAL_ANSWER_PROMPT
from deepresearch_agent.search.tool.reasoning.prompts import kb_prompt
from deepresearch_agent.search.tool.reasoning.search import QueryGenerator
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext


class DeepThinkingMixin:
    def extract_keywords(self, query: str) -> Dict[str, List[str]]:
        """从查询中提取关键词"""
        # 检查缓存
        if query in self._keywords_cache:
            return self._keywords_cache[query]

        if self.hybrid_tool is None:
            tokens = [token for token in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(token) > 1]
            keywords = {"high_level": tokens[:3], "low_level": tokens[3:8] or tokens[:3]}
        else:
            keywords = self.hybrid_tool.extract_keywords(query)
        
        # 缓存结果
        self._keywords_cache[query] = keywords
        return keywords

    def _generate_final_answer(self, query: str, retrieved_content: str, thinking_process: str) -> str:
        """
        基于检索的信息和思考过程生成最终答案
        
        参数:
            query: 原始查询
            retrieved_content: 已检索的内容
            thinking_process: 思考过程
            
        返回:
            str: 最终答案，包含思考过程
        """
        try:
            # 调用LLM生成最终答案
            response = self.llm.invoke(FINAL_ANSWER_PROMPT.format(
                query=query,
                retrieved_content=retrieved_content,
                thinking_process=thinking_process
            ))
            
            answer = response.content if hasattr(response, 'content') else str(response)
            
            # 将思考过程添加到答案中，使用Markdown引用格式
            formatted_answer = f"<think>{thinking_process}</think>\n\n{answer}"
            
            return formatted_answer
        except Exception as e:
            print(f"[最终答案生成错误] {str(e)}")
            return f"生成最终答案时出错: {str(e)}"

    async def _async_generate_next_query(self):
        """异步生成下一个查询"""
        def sync_generate():
            return self.thinking_engine.generate_next_query()
            
        # 在线程池中运行同步代码，避免阻塞事件循环
        return await asyncio.to_thread(sync_generate)

    async def _async_search(self, query: str):
        """异步执行搜索，避免阻塞事件循环"""
        if self.retrieval_provider is not None:
            tool_call_id = f"call_{uuid.uuid4().hex}"
            results = await self.retrieval_provider.search(
                query,
                top_k=5,
                search_depth="advanced",
                filters=SearchFilters(strategy="hybrid_search" if self.retrieval_provider.mode == SourceMode.GRAPHRAG else None),
                call_context=ToolCallContext(
                    run_id=self.run_id,
                    source_mode=self.retrieval_provider.mode,
                    tool_call_id=tool_call_id,
                ),
            )
            if any(result.source_mode != self.retrieval_provider.mode.value for result in results):
                raise ValueError("Provider 返回了与 Run.source_mode 不一致的证据")
            self.provider_results.extend(results)
            if not hasattr(self, "provider_calls"):
                self.provider_calls = []
            self.provider_calls.append({"tool_call_id": tool_call_id, "query": query, "results": results})
            return self._provider_results_to_legacy(results)
        def search_wrapper():
            return self.dual_searcher.search(query)
        
        # 在线程池中运行同步代码，避免阻塞事件循环
        return await asyncio.to_thread(search_wrapper)

    async def _async_extract_info(self, search_query, prev_reasoning, kb_prompt_result):
        """异步提取信息，避免阻塞"""
        extract_prompt = RELEVANT_EXTRACTION_PROMPT.format(
            prev_reasoning=prev_reasoning,
            search_query=search_query,
            document=kb_prompt_result
        )
        
        def llm_invoke():
            response = self.llm.invoke([
                {"role": "system", "content": extract_prompt},
                {"role": "user", "content": f'基于当前的搜索查询"{search_query}"和前面的推理步骤，分析每个知识来源并找出有用信息。'}
            ])
            return response.content if hasattr(response, 'content') else str(response)
        
        # 在线程池中运行同步LLM调用
        return await asyncio.to_thread(llm_invoke)

    async def _async_generate_final_answer(self, query, retrieved_content, thinking):
        """异步生成最终答案"""
        def generate_wrapper():
            return self._generate_final_answer(query, retrieved_content, thinking)
        
        return await asyncio.to_thread(generate_wrapper)

    def _log(self, message):
        """记录执行日志"""
        self.execution_logs.append(message)
        # print(message)  # 同时打印到控制台

    def thinking(self, query: str):
        """
        执行深度研究推理过程
        
        参数:
            query: 用户问题
                    
        返回:
            Dict: 包含思考过程和最终答案的字典
        """
        # 清空执行日志
        self.execution_logs = []
        self._log(f"\n[深度研究] 开始处理查询: {query}")

        self._keywords_cache = {}
        
        # 初始化结果容器
        chunk_info = {"chunks": [], "doc_aggs": []}
        self.all_retrieved_info = []
        
        # 初始化思考引擎
        self.thinking_engine.initialize_with_query(query)

        # 使用QueryGenerator生成初始子查询
        initial_sub_queries = self.query_generator.generate_sub_queries(query)
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
        
        # 迭代思考过程
        for iteration in range(self.max_iterations):
            self._log(f"\n[深度研究] 开始第{iteration + 1}轮迭代")
            
            # 检查是否达到最大迭代次数
            if iteration >= self.max_iterations - 1:
                summary_think = f"\n{BEGIN_SEARCH_RESULT}\n搜索次数已达上限。不允许继续搜索。\n{END_SEARCH_RESULT}\n"
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(summary_think)
                think += self.thinking_engine.remove_result_tags(summary_think)
                break

            # 更新消息历史，请求继续推理
            self.thinking_engine.update_continue_message()
            
            # 确定当前迭代要处理的查询
            queries_to_process = []
            
            if iteration == 0 and initial_sub_queries:
                # 第一轮迭代使用预先生成的子查询
                queries_to_process = initial_sub_queries[:2]  # 限制首轮使用的子查询数量
                query_think = "开始根据分解的子问题进行搜索"
            else:
                # 非首轮，使用思考引擎生成下一步查询
                result = self.thinking_engine.generate_next_query()
                
                # 处理生成结果
                if result["status"] == "empty":
                    self._log("\n[深度研究] 生成的思考内容为空")
                    # 尝试使用QueryGenerator的多假设生成功能寻找新方向
                    hypotheses = QueryGenerator.generate_multiple_hypotheses(query, self.llm)
                    if hypotheses:
                        self._log(f"\n[深度研究] 生成了{len(hypotheses)}个新假设，尝试从新角度探索")
                        queries_to_process = hypotheses
                        query_think = "尝试从新的角度思考这个问题:\n" + "\n".join([f"- {h}" for h in hypotheses])
                        self.thinking_engine.add_reasoning_step(query_think)
                        think += query_think
                    else:
                        continue
                elif result["status"] == "error":
                    self._log(f"\n[深度研究] 生成查询出错: {result.get('error', '未知错误')}")
                    break
                elif result["status"] == "answer_ready":
                    self._log("\n[深度研究] AI认为已有足够信息生成答案")
                    break
                else:
                    # 获取生成的思考内容
                    query_think = result["content"]
                    think += self.thinking_engine.remove_query_tags(query_think)
                    
                    # 获取搜索查询
                    queries_to_process = result["queries"]
            
            # 如果当前迭代没有查询，且我们已经检索到一些信息，尝试生成跟进查询
            if not queries_to_process and self.all_retrieved_info:
                followup_queries = self.query_generator.generate_followup_queries(
                    query, self.all_retrieved_info
                )
                
                if followup_queries:
                    self._log(f"\n[深度研究] 生成了{len(followup_queries)}个跟进查询")
                    queries_to_process = followup_queries
                    followup_think = "\n考虑到已发现的信息，我需要进一步探索以下问题:\n"
                    for i, fq in enumerate(followup_queries, 1):
                        followup_think += f"{i}. {fq}\n"
                    self.thinking_engine.add_reasoning_step(followup_think)
                    think += followup_think
            
            # 如果没有生成搜索查询但不是第一轮，考虑结束
            if not queries_to_process:
                if not self.all_retrieved_info and iteration == 0:
                    # 如果第一轮没检索到任何信息，强制使用原始查询
                    queries_to_process = [query]
                    self._log("\n[深度研究] 没有检索到信息，使用原始查询")
                else:
                    # 已有信息或已经不是第一轮，结束迭代
                    self._log("\n[深度研究] 没有生成新查询且已有信息，结束迭代")
                    break
            
            # 处理每个搜索查询
            for search_query in queries_to_process:
                self._log(f"\n[深度研究] 执行查询: {search_query}")
                
                # 检查是否已执行过相同查询
                if self.thinking_engine.has_executed_query(search_query):
                    summary_think = f"\n{BEGIN_SEARCH_RESULT}\n已搜索过该查询。请参考前面的结果。\n{END_SEARCH_RESULT}\n"
                    self.thinking_engine.add_reasoning_step(summary_think)
                    self.thinking_engine.add_human_message(summary_think)
                    think += self.thinking_engine.remove_result_tags(summary_think)
                    continue
                
                # 记录已执行查询
                self.thinking_engine.add_executed_query(search_query)
                
                # 将搜索查询添加到消息历史
                self.thinking_engine.add_ai_message(f"{BEGIN_SEARCH_QUERY}{search_query}{END_SEARCH_QUERY}")
                think += f"\n\n> {iteration + 1}. {search_query}\n\n"
                
                # 执行实际搜索
                kbinfos = self._search_current_provider(search_query)
                
                # 检查搜索结果是否为空
                has_results = (
                    kbinfos.get("chunks", []) or 
                    kbinfos.get("entities", []) or 
                    kbinfos.get("relationships", [])
                )
                
                if not has_results:
                    no_result_msg = f"\n{BEGIN_SEARCH_RESULT}\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n{END_SEARCH_RESULT}\n"
                    self.thinking_engine.add_reasoning_step(no_result_msg)
                    self.thinking_engine.add_human_message(no_result_msg)
                    think += self.thinking_engine.remove_result_tags(no_result_msg)
                    continue
                
                # 正常处理有结果的情况
                truncated_prev_reasoning = self.thinking_engine.prepare_truncated_reasoning()
                
                # 合并块信息
                chunk_info = self.dual_searcher._merge_results(chunk_info, kbinfos)
                
                # 构建提取相关信息的提示
                kb_prompt_result = "\n".join(kb_prompt(kbinfos, 4096))
                extract_prompt = RELEVANT_EXTRACTION_PROMPT.format(
                    prev_reasoning=truncated_prev_reasoning,
                    search_query=search_query,
                    document=kb_prompt_result
                )
                
                # 使用LLM提取有用信息
                extraction_msg = self.llm.invoke([
                    SystemMessage(content=extract_prompt),
                    HumanMessage(content=f'基于当前的搜索查询"{search_query}"和前面的推理步骤，分析每个知识来源并找出有用信息。')
                ])
                
                summary_think = extraction_msg.content if hasattr(extraction_msg, 'content') else str(extraction_msg)
                
                # 保存重要信息
                has_useful_info = (
                    "**Final Information**" in summary_think and 
                    "No helpful information found" not in summary_think
                )
                
                if has_useful_info:
                    useful_info = summary_think.split("**Final Information**")[1].strip()
                    self.all_retrieved_info.append(useful_info)
                    self._log(f"\n[深度研究] 发现有用信息: {useful_info}")
                else:
                    self._log("\n[深度研究] 未发现有用信息")
                
                # 更新推理历史
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(f"\n{BEGIN_SEARCH_RESULT}{summary_think}{END_SEARCH_RESULT}\n")
                think += self.thinking_engine.remove_result_tags(summary_think)
            
            # 在每轮迭代结束后，如果已有足够信息，使用QueryGenerator评估是否需要继续搜索
            if iteration > 0 and self.all_retrieved_info:
                # 类似于DeepSearch中的_generate_gap_queries方法
                gap_needed = len(self.query_generator.generate_followup_queries(query, self.all_retrieved_info)) > 0
                if not gap_needed:
                    self._log("\n[深度研究] 已收集足够信息，无需进一步查询")
                    reflection_think = "\n已收集到足够的信息，可以开始整合分析了。"
                    self.thinking_engine.add_reasoning_step(reflection_think)
                    think += reflection_think
                    break
        
        # 生成最终答案
        # 确保至少执行了一次搜索
        if not self.thinking_engine.executed_search_queries:
            return {
                "thinking_process": think,
                "answer": f"抱歉，我无法回答关于'{query}'的问题，因为没有找到相关信息。",
                "reference": chunk_info,
                "retrieved_info": [],
                "execution_logs": self.execution_logs,
            }
        
        # 使用检索到的信息生成答案
        retrieved_content = "\n\n".join(self.all_retrieved_info)
        final_answer = self._generate_final_answer(query, retrieved_content, think)
        
        # 返回结果
        result = {
            "thinking_process": think,
            "answer": final_answer,
            "reference": chunk_info,
            "retrieved_info": self.all_retrieved_info,
            "execution_logs": self.execution_logs,
        }
        
        return result

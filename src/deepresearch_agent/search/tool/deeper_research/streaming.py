# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""流式搜索入口与 thinking_stream 编排及前导阶段（原 search_stream/_progress/thinking_stream 1350-1563）。"""

from typing import Any, AsyncGenerator
import json
import time
import traceback
import asyncio
import re
from deepresearch_agent.search.tool.deeper_research import (
    create_multiple_reasoning_branches,
)
from deepresearch_agent.search.tool.reasoning.validator import complexity_estimate


class DeeperStreamingMixin:
    async def search_stream(self, query_input: Any) -> AsyncGenerator[str, None]:
        """
        执行带流式输出的增强深度研究
        
        Args:
            query_input: 查询或包含查询的字典
                
        Yields:
            流式内容
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
        cache_key = f"deeper:{query}"
        cached_result = self.deep_research.cache_manager.get(cache_key)
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
            last_chunk = None
            
            # 提示用户处理开始
            yield "\n**开始深度分析您的问题**...\n"
            
            # 评估查询复杂度以决定是否采用多假设思考
            complexity = complexity_estimate(query)
            if complexity > 0.7:
                yield "\n**检测到复杂查询，激活深度思考模式**...\n"
            
            # 使用异步探索器
            keywords = self.extract_keywords(query)
            if keywords.get("high_level", []):
                yield f"\n**使用链式探索方法搜索知识**...\n"
                
                # 异步启动探索任务，但不等待它完成
                # 我们会在后台处理结果
                asyncio.create_task(self.chain_explorer.explore_async(
                    query, 
                    keywords.get("high_level", [])[:3], 
                    max_steps=3
                ))
            
            async for chunk in self.thinking_stream(query):
                if isinstance(chunk, dict) and "answer" in chunk:
                    # 这是最终结果对象
                    full_response = chunk["answer"]
                    thinking_content = chunk["thinking"]
                    last_chunk = chunk
                    
                    # 在最终答案之前添加矛盾检测结果
                    if hasattr(self, 'current_query_context') and self.current_query_context.get("query_id"):
                        # 检测矛盾
                        query_id = self.current_query_context.get("query_id")
                        contradiction_result = await self._async_detect_contradictions(query_id)
                        
                        if contradiction_result["contradictions"]:
                            yield "\n**信息一致性分析**：发现信息中存在一些不一致之处。在综合答案时已考虑这些因素。\n\n"
                    
                    # 清理最终答案以移除思考过程部分
                    clean_answer = re.sub(r'<think>.*?</think>\s*', '', full_response, flags=re.DOTALL)
                    
                    # 添加推理摘要统计
                    if hasattr(self, 'current_query_context') and self.current_query_context.get("query_id"):
                        query_id = self.current_query_context.get("query_id")
                        reasoning_summary = self.evidence_tracker.summarize_reasoning(query_id)
                        
                        # 在答案末尾添加小字体的推理统计
                        if reasoning_summary and reasoning_summary.get("steps_count", 0) > 0:
                            stats = f"\n\n<small>*推理过程包含 {reasoning_summary.get('steps_count', 0)} 个步骤，"
                            stats += f"使用了 {reasoning_summary.get('evidence_count', 0)} 个证据，"
                            stats += f"耗时 {reasoning_summary.get('duration_seconds', 0):.1f} 秒*</small>"
                            clean_answer += stats
                    
                    # 缓存结果
                    self.deep_research.cache_manager.set(cache_key, clean_answer)
                    
                    yield clean_answer
                else:
                    # 正常返回流式块
                    yield chunk
            
            # 记录总时间
            total_time = time.time() - overall_start
            self._log(f"\n[深度搜索] 完成，耗时 {total_time:.2f}秒")
            self.performance_metrics["total_time"] = total_time
                
        except Exception as e:
            error_msg = f"深度研究过程中出错: {str(e)}"
            self._log(error_msg)
            traceback.print_exc()
            yield error_msg
    async def _progress(self, kind: str, **extra) -> None:
        """向驱动回调上报迭代进度（回调由 DeepResearchDriver 注入）"""
        if self.progress_callback is not None:
            await self.progress_callback({"kind": kind, **extra})
    async def thinking_stream(self, query: str) -> AsyncGenerator[str, None]:
        """
        执行带流式输出的增强深度研究
        
        Args:
            query: 用户问题
                    
        Yields:
            思考步骤和最终答案
        """
        ctx = {}
        async for _chunk in self._stream_preamble(query, ctx):
            yield _chunk
        async for _chunk in self._stream_iterate(query, ctx):
            yield _chunk
        async for _chunk in self._stream_finalize(query, ctx):
            yield _chunk
    async def _stream_preamble(self, query, ctx):
        """原 thinking_stream 前段：子查询分解/社区分析/图谱构建/上下文（原 1360-1563 行）。"""
        overall_start = time.time()
        
        # 清空执行日志
        self.execution_logs = []
        self._log(f"\n[深度研究] 开始处理查询: {query}")
        
        # 向用户发送初始状态消息
        yield "\n**正在分析您的问题**...\n"
        
        # 提取关键词
        keywords = self.extract_keywords(query)
        self._log(f"\n[深度研究] 提取关键词: {keywords}")
        
        # 开始新的查询跟踪
        query_id = self.evidence_tracker.start_new_query(query, keywords)
        self.current_query_context = {"query_id": query_id}
        
        # 重置分支信息
        self.explored_branches = {}
        
        # 使用QueryGenerator生成初始子查询
        yield "\n**正在将问题分解为子问题**...\n"
        
        # 异步生成子查询
        def generate_sub_queries():
            return self.query_generator.generate_sub_queries(query)
        
        initial_sub_queries = await asyncio.to_thread(generate_sub_queries)
        self._log(f"\n[深度研究] 生成了{len(initial_sub_queries)}个初始子查询: {initial_sub_queries}")
        
        # 显示子查询
        sub_queries_msg = "\n**问题已分解为以下子问题**:\n"
        for i, subq in enumerate(initial_sub_queries, 1):
            sub_queries_msg += f"{i}. {subq}\n"
        yield sub_queries_msg
        
        # 记录为推理步骤
        subquery_step_id = self.evidence_tracker.add_reasoning_step(
            query_id,
            "initial_query_decomposition",
            f"将原始问题分解为{len(initial_sub_queries)}个子问题: {initial_sub_queries}"
        )
        
        # 步骤1: 社区感知增强
        yield "\n**正在分析相关知识社区**...\n"
        enhanced_context = await self._async_enhance_search(query, keywords)
        
        # 步骤2: 使用社区信息增强搜索策略
        search_strategy = enhanced_context.get("search_strategy", {})
        follow_up_queries = search_strategy.get("follow_up_queries", [])
        
        # 合并社区生成的后续查询和初始子查询
        all_queries = list(set(initial_sub_queries + follow_up_queries))
        
        if follow_up_queries:
            query_msg = f"\n**发现潜在的深入探索方向**: {', '.join(follow_up_queries[:2])}\n"
            self._log(query_msg)
            yield query_msg
        
        # 添加增强搜索策略的推理步骤
        strategy_step_id = self.evidence_tracker.add_reasoning_step(
            query_id, 
            "knowledge_community_analysis", 
            f"基于社区分析，识别了关键实体和相关查询策略。将探索以下后续查询: {all_queries}"
        )
        
        # 记录社区信息作为证据
        community_summaries = []
        if "community_info" in enhanced_context and "summaries" in enhanced_context["community_info"]:
            for summary_obj in enhanced_context["community_info"]["summaries"]:
                if isinstance(summary_obj, dict) and "summary" in summary_obj:
                    community_summaries.append(summary_obj["summary"])
                else:
                    community_summaries.append(str(summary_obj))
                    
        if community_summaries:
            comm_msg = f"\n**找到 {len(community_summaries)} 个相关知识社区**\n"
            self._log(comm_msg)
            yield comm_msg
            
            for i, summary in enumerate(community_summaries[:2]):
                short_summary = summary[:100] + "..." if len(summary) > 100 else summary
                self._log(f"\n[深度研究] 社区 {i+1} 摘要: {short_summary}")
                
                self.evidence_tracker.add_evidence(
                    strategy_step_id,
                    f"community_summary_{i}",
                    summary,
                    "community_knowledge"
                )
        
        # 步骤3: 构建初始知识图谱
        initial_entities = search_strategy.get("focus_entities", [])
        discovered_entities = search_strategy.get("discovered_entities", [])
        
        # 合并实体
        graph_entities = list(set(initial_entities + discovered_entities))
        
        if graph_entities:
            kg_msg = f"\n**正在构建相关知识图谱**...\n"
            self._log(kg_msg)
            yield kg_msg
            
            # 异步构建知识图谱
            await self._async_build_graph(query, graph_entities)
            
            # 获取核心实体
            central_entities = self.knowledge_builder.get_central_entities(limit=5)
            central_entity_ids = [e["id"] for e in central_entities]
            
            if central_entity_ids:
                central_msg = f"\n**识别出核心相关实体**: {', '.join(central_entity_ids[:3])}\n"
                yield central_msg
            
            # 添加知识图谱分析步骤
            kg_step_id = self.evidence_tracker.add_reasoning_step(
                query_id,
                "knowledge_graph_analysis",
                f"构建了初始知识图谱，识别出核心实体: {central_entity_ids}"
            )
            
            # 记录知识图谱信息作为证据
            kg_info = {
                "entity_count": self.knowledge_builder.knowledge_graph.number_of_nodes(),
                "relation_count": self.knowledge_builder.knowledge_graph.number_of_edges(),
                "central_entities": central_entity_ids
            }
            
            self.evidence_tracker.add_evidence(
                kg_step_id,
                "knowledge_graph",
                json.dumps(kg_info),
                "graph_structure"
            )
        
        # 步骤4: 生成多个假设  
        complexity = complexity_estimate(query)
        if complexity > 0.7:  # 对复杂查询应用多假设思考
            yield "\n**生成多个思考假设**...\n"
            
            # 异步生成假设
            def generate_hypotheses():
                return self.query_generator.generate_multiple_hypotheses(query, self.llm)
            
            hypotheses = await asyncio.to_thread(generate_hypotheses)
            
            if hypotheses:
                hypothesis_msg = f"\n**探索 {len(hypotheses)} 个可能假设**\n"
                for i, hyp in enumerate(hypotheses[:2]):
                    hypothesis_msg += f"- 假设 {i+1}: {hyp}\n"
                yield hypothesis_msg
                
                # 创建多个推理分支
                branch_results = create_multiple_reasoning_branches(self, query_id, hypotheses)
                
                # 对第一个分支应用反事实分析
                if branch_results and "branch_1" in branch_results:
                    yield "\n**进行反事实分析**...\n"
        
        # 步骤5: 使用深度研究工具的流式思考过程
        yield "\n**正在进行深度研究**...\n"
        
        # 调用deep_research工具的流式API，但传递我们增强的上下文
        # 准备上下文消息
        context_message = ""
        
        # 添加初始子查询分解
        if initial_sub_queries:
            context_message += "为了全面解答这个问题，我将从以下几个方面进行研究：\n"
            for i, sub_q in enumerate(initial_sub_queries, 1):
                context_message += f"{i}. {sub_q}\n"
            context_message += "\n"
            
        if community_summaries:
            context_message += "找到了相关的知识社区信息:\n"
            context_message += "\n".join(community_summaries[:2])  # 只使用前两个
            
        # 添加探索路径信息
        exploration_path = enhanced_context.get("exploration_results", {}).get("exploration_path", [])
        if exploration_path:
            context_message += "\n\n通过Chain of Exploration探索路径发现:\n"
            path_description = []
            for i, step in enumerate(exploration_path[:5]):  # 限制路径长度
                if i > 0:  # 跳过起始实体
                    path_description.append(f"- 步骤{step['step']}: 实体 {step['node_id']} ({step['reasoning']})")
            context_message += "\n".join(path_description)
        
        # 如果有跟进查询，添加到思考过程
        if follow_up_queries:
            context_message += "\n\n可能的深入探索方向:\n"
            context_message += "\n".join([f"- {q}" for q in follow_up_queries])
            
        # 将增强上下文添加到思考引擎
        if context_message:
            self.deep_research.thinking_engine.initialize_with_query(query)
            self.deep_research.thinking_engine.add_reasoning_step(context_message)
            yield context_message
            
        # 执行深度研究迭代
        think = context_message
        
        # 初始化思考引擎并添加上下文
        self.deep_research.thinking_engine.initialize_with_query(query)
        self.deep_research.thinking_engine.add_reasoning_step(context_message)
        ctx["think"] = think
        ctx["query_id"] = query_id
        ctx["initial_sub_queries"] = initial_sub_queries
        ctx["community_summaries"] = community_summaries
        ctx["exploration_path"] = exploration_path


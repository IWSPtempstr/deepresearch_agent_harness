# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""原 thinking_stream 收尾：分支合并、矛盾检测与最终答案（原 1952-2161 行）。"""

import asyncio
import re
from deepresearch_agent.search.tool.deeper_research import (
    detect_and_resolve_contradictions,
    generate_citations,
    merge_reasoning_branches,
)


class DeeperStreamFinalizeMixin:
    async def _stream_finalize(self, query, ctx):
        """原 thinking_stream 收尾：分支合并/矛盾检测/最终答案（原 1952-2161 行）。"""
        think = ctx["think"]
        query_id = ctx["query_id"]
        initial_sub_queries = ctx["initial_sub_queries"]
        community_summaries = ctx["community_summaries"]
        exploration_path = ctx["exploration_path"]
        iteration = ctx["iteration"]  # 原循环变量（原 1566 行 for 循环的末值）
        # 步骤6: 合并推理分支
        if self.explored_branches:
            merge_msg = "\n**正在合并多个推理分支**...\n"
            self._log(merge_msg)
            yield merge_msg
            
            merged_results = merge_reasoning_branches(self, query_id)
            if merged_results:
                self.deep_research.thinking_engine.add_reasoning_step(merged_results)
                think += merged_results
        
        # 步骤7: 矛盾检测与处理
        if self.deep_research.thinking_engine.executed_search_queries:
            yield "\n**检测信息一致性**...\n"
            
            # 异步检测矛盾
            contradiction_result = await self._async_detect_contradictions(query_id)
            if contradiction_result["contradictions"]:
                contradiction_msg = f"\n**发现 {len(contradiction_result['contradictions'])} 个信息不一致**\n"
                self._log(contradiction_msg)
                yield contradiction_msg
                
                contradiction_analysis = "\n## 信息矛盾分析\n"
                contradiction_analysis += f"发现 {len(contradiction_result['contradictions'])} 个信息矛盾：\n"
                
                for i, contradiction in enumerate(contradiction_result["contradictions"]):
                    if contradiction["type"] == "numerical":
                        contradiction_analysis += f"{i+1}. 数值矛盾: 在\"{contradiction.get('context', '')}\"中，"
                        contradiction_analysis += f"发现值 {contradiction.get('value1')} 和 {contradiction.get('value2')}\n"
                    else:
                        contradiction_analysis += f"{i+1}. 语义矛盾: {contradiction.get('analysis', '')}\n"
                
                # 添加矛盾分析到思考过程
                self.deep_research.thinking_engine.add_reasoning_step(contradiction_analysis)
                think += contradiction_analysis
                yield contradiction_analysis
        
        # 确保至少执行了一次搜索
        if not self.deep_research.thinking_engine.executed_search_queries:
            no_search_msg = f"\n**无法找到与{query}相关的信息，尝试给出基础回答**...\n"
            yield no_search_msg
            return
        
        # 生成最终答案
        yield "\n**正在根据所有收集的信息生成最终答案**...\n"
        
        # 使用异步生成最终答案
        def generate_final_answer():
            try:
                # 使用检索到的信息生成答案
                retrieved_content = "\n\n".join(self.deep_research.all_retrieved_info)
                
                # 增强答案生成过程，整合知识图谱和社区分析结果
                enhanced_prompt = f"""
                用户问题：{query}
                
                我已经通过多种检索方法收集了以下信息：
                
                {retrieved_content}
                
                此外，我通过知识图谱分析发现了以下关键实体和关系：
                """
                
                # 添加知识图谱分析结果
                central_entities = self.knowledge_builder.get_central_entities(limit=5)
                if central_entities:
                    enhanced_prompt += "\n核心实体及其重要性：\n"
                    for entity in central_entities:
                        entity_id = entity.get("id", "")
                        importance = entity.get("centrality", entity.get("degree", 0))
                        entity_type = entity.get("type", "unknown")
                        properties = entity.get("properties", {})
                        description = properties.get("description", "无描述")
                        
                        enhanced_prompt += f"- {entity_id} (重要性: {importance:.3f}, 类型: {entity_type}): {description}\n"
                
                # 添加社区分析结果
                if community_summaries:
                    enhanced_prompt += "\n来自相关知识社区的见解：\n"
                    for i, summary in enumerate(community_summaries[:2]):
                        enhanced_prompt += f"- 社区{i+1}: {summary[:200]}...\n"
                
                # 添加探索路径分析
                if exploration_path:
                    enhanced_prompt += "\n知识探索路径分析：\n"
                    path_summary = []
                    current_entity = None
                    for step in exploration_path:
                        if step["step"] > 0:  # 跳过起始实体
                            if current_entity != step["node_id"]:
                                current_entity = step["node_id"]
                                path_summary.append(f"实体 {current_entity}: {step['reasoning']}")
                    
                    enhanced_prompt += "\n".join(path_summary[:3]) + "\n"
                
                # 添加初始子查询分析结果
                if initial_sub_queries:
                    enhanced_prompt += "\n问题分解与深入分析：\n"
                    for i, subq in enumerate(initial_sub_queries, 1):
                        related_info = []
                        for info in self.deep_research.all_retrieved_info:
                            if any(term.lower() in info.lower() for term in subq.lower().split()):
                                related_info.append(info[:100] + "..." if len(info) > 100 else info)
                        
                        if related_info:
                            enhanced_prompt += f"- 子问题{i}: {subq} - 找到{len(related_info)}条相关信息\n"
                        else:
                            enhanced_prompt += f"- 子问题{i}: {subq} - 未找到直接相关信息\n"
                
                # 添加矛盾分析结果
                contradiction_result = detect_and_resolve_contradictions(self, query_id)
                if contradiction_result["contradictions"]:
                    enhanced_prompt += "\n信息矛盾分析：\n"
                    for i, contradiction in enumerate(contradiction_result["contradictions"][:3]):
                        if contradiction["type"] == "numerical":
                            enhanced_prompt += f"- 矛盾{i+1}: 在数值上存在不一致，一处显示 {contradiction.get('value1')}，另一处显示 {contradiction.get('value2')}\n"
                        else:
                            enhanced_prompt += f"- 矛盾{i+1}: {contradiction.get('analysis', '')}\n"
                
                # 请求生成最终答案
                enhanced_prompt += """
                请基于以上所有信息，生成一个全面深入的回答。回答应该:
                1. 直接回答用户问题
                2. 结构清晰，逻辑性强
                3. 整合所有相关信息，包括知识图谱和社区分析的见解
                4. 如有必要，指出信息中的不确定性或矛盾
                5. 对问题的不同方面进行全面分析
                """
                
                # 使用增强提示生成最终答案
                final_response = self.llm.invoke(enhanced_prompt)
                enhanced_answer = final_response.content if hasattr(final_response, 'content') else str(final_response)
                
                # 将增强答案与思考过程结合
                final_answer = f"<think>{think}</think>\n\n{enhanced_answer}"
                
                # 生成引用标记
                cited_answer = generate_citations(self, enhanced_answer, query_id)
                
                # 如果引用生成成功，使用带引用的答案
                if len(cited_answer) > len(enhanced_answer):
                    final_answer = f"<think>{think}</think>\n\n{cited_answer}"
                
                return final_answer
            except Exception as e:
                # 如果增强生成失败，回退到原始方法
                self._log(f"\n[深度研究] 增强答案生成失败: {e}，回退到标准方法")
                return self.deep_research._generate_final_answer(query, retrieved_content, think)
        
        # 异步执行答案生成
        final_answer = await asyncio.to_thread(generate_final_answer)
        
        # 获取知识图谱中的核心实体
        central_entities = []
        if hasattr(self.knowledge_builder, 'knowledge_graph') and self.knowledge_builder.knowledge_graph.nodes:
            central_entities = self.knowledge_builder.get_central_entities(limit=5)
        
        # 记录最终回答步骤
        final_step_id = self.evidence_tracker.add_reasoning_step(
            query_id,
            "final_answer",
            "生成最终答案"
        )
        
        self.evidence_tracker.add_evidence(
            final_step_id,
            "final_answer_evidence",
            "\n\n".join(self.deep_research.all_retrieved_info),
            "synthesized_knowledge"
        )
        
        # 获取推理摘要
        reasoning_summary = self.evidence_tracker.summarize_reasoning(query_id)
        
        # 上报最终答案事件
        await self._progress("answer", iteration_index=iteration, answer_char_count=len(final_answer))

        # 在最终答案前增加子问题分析概述
        if initial_sub_queries and "<think>" in final_answer and "</think>" in final_answer:
            # 提取思考过程之外的部分
            clean_answer = re.sub(r'<think>.*?</think>\s*', '', final_answer, flags=re.DOTALL)
            
            # 构建子问题概述
            subq_overview = "\n\n**问题分析概述**:\n"
            for i, subq in enumerate(initial_sub_queries, 1):
                related_info_count = 0
                for info in self.deep_research.all_retrieved_info:
                    if any(term.lower() in info.lower() for term in subq.lower().split()):
                        related_info_count += 1
                
                subq_overview += f"- 子问题 {i}: {subq}"
                if related_info_count > 0:
                    subq_overview += f" - 找到 {related_info_count} 条相关信息\n"
                else:
                    subq_overview += " - 未找到直接相关信息\n"
            
            # 添加推理统计
            if reasoning_summary:
                subq_overview += f"\n<small>*分析过程包含 {reasoning_summary.get('steps_count', 0)} 个步骤，"
                subq_overview += f"使用了 {reasoning_summary.get('evidence_count', 0)} 个证据来源，"
                subq_overview += f"耗时 {reasoning_summary.get('duration_seconds', 0):.1f} 秒*</small>"
            
            # 重新组合答案
            enhanced_final = clean_answer + subq_overview
            
            # 返回最终答案及思考过程
            yield {"answer": f"<think>{think}</think>\n\n{enhanced_final}", "thinking": think}
        else:
            # 返回原始答案
            yield {"answer": final_answer, "thinking": think}


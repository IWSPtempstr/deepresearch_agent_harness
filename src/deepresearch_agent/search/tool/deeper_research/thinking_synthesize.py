# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""原 thinking() 收尾：分支合并、矛盾检测、最终答案与结果组装（原 780-996 行）。"""

from deepresearch_agent.search.tool.deeper_research import (
    detect_and_resolve_contradictions,
    generate_citations,
    merge_reasoning_branches,
)


class DeeperSynthesizeMixin:
    def _thinking_resolve(self, query_id, think):
        """原 thinking() 步骤7-8：合并推理分支与矛盾检测（原 780-812 行）。"""
        # 步骤7: 合并推理分支  
        if self.explored_branches:
            self._log("\n[深度研究] 合并多个推理分支")
            merged_results = merge_reasoning_branches(self, query_id)
            
            if merged_results:
                think += f"\n{merged_results}\n"
                self.thinking_engine.add_reasoning_step(merged_results)
        
        # 步骤8: 矛盾检测与处理  
        if not hasattr(self, '_contradiction_cache'):
            self._contradiction_cache = {}
            
        if query_id in self._contradiction_cache:
            contradiction_result = self._contradiction_cache[query_id]
        else:
            contradiction_result = detect_and_resolve_contradictions(self, query_id)
            self._contradiction_cache[query_id] = contradiction_result
            
        if contradiction_result["contradictions"]:
            contradiction_analysis = "\n## 信息矛盾分析\n"
            contradiction_analysis += f"发现 {len(contradiction_result['contradictions'])} 个信息矛盾：\n"
            
            for i, contradiction in enumerate(contradiction_result["contradictions"]):
                if contradiction["type"] == "numerical":
                    contradiction_analysis += f"{i+1}. 数值矛盾: 在\"{contradiction.get('context', '')}\"中，"
                    contradiction_analysis += f"发现值 {contradiction.get('value1')} 和 {contradiction.get('value2')}\n"
                else:
                    contradiction_analysis += f"{i+1}. 语义矛盾: {contradiction.get('analysis', '')}\n"
            
            # 添加矛盾分析到思考过程
            think += contradiction_analysis
            self.thinking_engine.add_reasoning_step(contradiction_analysis)
        return think, contradiction_result

    def _thinking_synthesize(self, query, query_id, think, initial_sub_queries, community_summaries, exploration_path, contradiction_result):
        """原 thinking() 收尾：最终答案生成与结果组装（原 814-996 行）。"""
        # 生成最终答案
        # 确保至少执行了一次搜索
        if not self.deep_research.thinking_engine.executed_search_queries:
            self._log("\n[深度研究] 未执行任何有效搜索，无法生成答案")
            result = {
                "thinking_process": think,
                "answer": f"抱歉，我无法回答关于'{query}'的问题，因为没有找到相关信息。",
                "reference": {"chunks": [], "doc_aggs": []},
                "retrieved_info": [],
                "reasoning_chain": self.evidence_tracker.get_reasoning_chain(query_id),
                "execution_logs": self.execution_logs,
            }
            
            # 缓存思考结果
            if not hasattr(self, '_thinking_cache'):
                self._thinking_cache = {}
            self._thinking_cache[query] = result
            
            return result
        
        # 使用检索到的信息生成答案
        retrieved_content = "\n\n".join(self.deep_research.all_retrieved_info)
        self._log("\n[深度研究] 生成最终答案")
        
        # 检查最终答案缓存
        final_answer_key = f"final_answer:{query}:{len(self.deep_research.all_retrieved_info)}"
        if not hasattr(self, '_final_answer_cache'):
            self._final_answer_cache = {}
            
        if final_answer_key in self._final_answer_cache:
            final_answer = self._final_answer_cache[final_answer_key]
        else:
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
            
            try:
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
                
                # 缓存最终答案
                self._final_answer_cache[final_answer_key] = final_answer
                
            except Exception as e:
                # 如果增强生成失败，回退到原始方法
                self._log(f"\n[深度研究] 增强答案生成失败: {e}，回退到标准方法")
                final_answer = self.deep_research._generate_final_answer(query, retrieved_content, think)
                # 依然缓存结果，防止再次失败
                self._final_answer_cache[final_answer_key] = final_answer
        
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
            retrieved_content,
            "synthesized_knowledge"
        )
        
        # 获取推理摘要  
        reasoning_summary = self.evidence_tracker.summarize_reasoning(query_id)
        
        # 生成证据来源统计  
        evidence_stats = self.evidence_tracker.get_evidence_source_stats(query_id)
        
        # 返回结果
        result = {
            "thinking_process": think,
            "answer": final_answer,
            "reference": {"chunks": [], "doc_aggs": []},
            "retrieved_info": self.deep_research.all_retrieved_info,
            "reasoning_chain": self.evidence_tracker.get_reasoning_chain(query_id),
            "reasoning_summary": reasoning_summary,    
            "evidence_stats": evidence_stats,    
            "knowledge_graph": {
                "entity_count": getattr(self.knowledge_builder.knowledge_graph, "number_of_nodes", lambda: 0)(),
                "relation_count": getattr(self.knowledge_builder.knowledge_graph, "number_of_edges", lambda: 0)(),
                "central_entities": central_entities
            },
            "initial_sub_queries": initial_sub_queries,
            "contradictions": contradiction_result["contradictions"],    
            "execution_logs": self.execution_logs,
        }
        
        # 缓存思考结果
        if not hasattr(self, '_thinking_cache'):
            self._thinking_cache = {}
        self._thinking_cache[query] = result
        
        return result


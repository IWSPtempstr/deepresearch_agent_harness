# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""原 thinking() 的迭代思考主循环（原 390-778 行）。"""

from deepresearch_agent.config.prompts import RELEVANT_EXTRACTION_PROMPT
from deepresearch_agent.search.tool.deeper_research import (
    enhance_search_with_coe,
    detect_and_resolve_contradictions,
)
from deepresearch_agent.search.tool.reasoning.prompts import kb_prompt


class DeeperIterateMixin:
    def _thinking_iterate(self, query, query_id, initial_sub_queries, think):
        """原 thinking() 迭代思考主循环（原 390-778 行），逐轮生成查询-检索-提取。"""
        # 迭代思考过程
        for iteration in range(self.deep_research.max_iterations):
            self._log(f"\n[深度研究] 开始第{iteration + 1}轮迭代\n")
            
            # 跟踪迭代步骤
            iteration_step_id = self.evidence_tracker.add_reasoning_step(
                query_id,
                f"iteration_{iteration}",
                f"\n开始第 {iteration + 1} 轮迭代思考\n"
            )
            
            # 检查是否达到最大迭代次数
            if iteration >= self.deep_research.max_iterations - 1:
                summary_think = f"\n搜索次数已达上限。不允许继续搜索。\n"
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(summary_think)
                think += self.thinking_engine.remove_result_tags(summary_think)
                self._log("\n[深度研究] 达到最大迭代次数，结束搜索")
                break

            # 更新消息历史，请求继续推理
            self.thinking_engine.update_continue_message()
            
            # 确定本轮迭代要处理的查询
            queries_to_process = []
            
            if iteration == 0 and initial_sub_queries:
                # 第一轮迭代使用预先生成的子查询
                queries_to_process = initial_sub_queries[:2]  # 限制首轮使用的子查询数量
                self._log(f"\n[深度研究] 使用初始子查询进行首轮搜索: {queries_to_process}")
            else:
                # 非首轮，使用思考引擎生成下一步查询
                result = self.thinking_engine.generate_next_query()
                
                # 处理生成结果
                if result["status"] == "empty":
                    self._log("\n[深度研究] 生成的思考内容为空")
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "generation_result",
                        "生成的思考内容为空",
                        "reasoning_status"
                    )
                    
                    # 尝试使用QueryGenerator的多假设生成功能寻找新方向
                    if iteration > 0:
                        if not hasattr(self, '_iteration_hypotheses_cache'):
                            self._iteration_hypotheses_cache = {}
                        
                        iter_cache_key = f"iter_hyp:{query}:{iteration}"
                        if iter_cache_key in self._iteration_hypotheses_cache:
                            hypotheses = self._iteration_hypotheses_cache[iter_cache_key]
                        else:
                            hypotheses = self.query_generator.generate_multiple_hypotheses(query, self.llm)
                            self._iteration_hypotheses_cache[iter_cache_key] = hypotheses
                            
                        if hypotheses:
                            self._log(f"\n[深度研究] 生成了{len(hypotheses)}个新假设，尝试从新角度探索")
                            queries_to_process = hypotheses
                            hypothesis_think = "尝试从新的角度思考这个问题:\n" + "\n".join([f"- {h}" for h in hypotheses])
                            self.thinking_engine.add_reasoning_step(hypothesis_think)
                            think += hypothesis_think
                        else:
                            continue
                    else:
                        continue
                elif result["status"] == "error":
                    self._log(f"\n[深度研究] 生成查询出错: {result.get('error', '未知错误')}")
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "generation_error",
                        f"生成查询出错: {result.get('error', '未知错误')}",
                        "reasoning_status"
                    )
                    break
                elif result["status"] == "answer_ready":
                    self._log("\n[深度研究] AI认为已有足够信息生成答案")
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "reasoning_complete",
                        "AI认为已有足够信息生成答案",
                        "reasoning_status"
                    )
                    break
                    
                # 获取生成的思考内容
                query_think = result["content"]
                think += self.thinking_engine.remove_query_tags(query_think)
                
                # 获取搜索查询
                queries_to_process = result["queries"]
            
            # 如果当前迭代没有查询，且我们已经检索到一些信息，尝试使用QueryGenerator生成跟进查询
            if not queries_to_process and self.deep_research.all_retrieved_info:
                # 检查跟进查询缓存
                followup_cache_key = f"followup:{query}:{len(self.deep_research.all_retrieved_info)}"
                if not hasattr(self, '_followup_cache'):
                    self._followup_cache = {}
                    
                if followup_cache_key in self._followup_cache:
                    followup_queries = self._followup_cache[followup_cache_key]
                else:
                    followup_queries = self.query_generator.generate_followup_queries(
                        query, self.deep_research.all_retrieved_info
                    )
                    self._followup_cache[followup_cache_key] = followup_queries
                
                if followup_queries:
                    self._log(f"\n[深度研究] 生成了{len(followup_queries)}个跟进查询")
                    queries_to_process = followup_queries
                    followup_think = "\n考虑到已发现的信息，我需要进一步探索以下问题:\n"
                    for i, fq in enumerate(followup_queries, 1):
                        followup_think += f"{i}. {fq}\n"
                    self.thinking_engine.add_reasoning_step(followup_think)
                    think += followup_think
                    
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
                    self._log("\n[深度研究] 没有检索到信息，使用原始查询")
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "query_fallback",
                        "没有检索到信息，使用原始查询",
                        "reasoning_status"
                    )
                else:
                    # 已有信息，结束迭代
                    self._log("\n[深度研究] 没有生成新查询且已有信息，结束迭代")
                    self.evidence_tracker.add_evidence(
                        iteration_step_id,
                        "iteration_complete",
                        "没有生成新查询且已有信息，结束迭代",
                        "reasoning_status"
                    )
                    
                    # 在结束前进行矛盾检测  
                    if not hasattr(self, '_contradiction_cache'):
                        self._contradiction_cache = {}
                        
                    if query_id in self._contradiction_cache:
                        contradiction_result = self._contradiction_cache[query_id]
                    else:
                        contradiction_result = detect_and_resolve_contradictions(self, query_id)
                        self._contradiction_cache[query_id] = contradiction_result
                    
                    if contradiction_result["contradictions"]:
                        think += "\n## 矛盾检测\n"
                        think += f"发现 {len(contradiction_result['contradictions'])} 个信息矛盾。\n"
                        for i, contradiction in enumerate(contradiction_result["contradictions"]):
                            if contradiction["type"] == "numerical":
                                think += f"{i+1}. 数值矛盾: {contradiction.get('context', '')}\n"
                            else:
                                think += f"{i+1}. 语义矛盾: {contradiction.get('analysis', '')}\n"
                    
                    break
            
            # 处理每个搜索查询
            for search_query in queries_to_process:
                self._log(f"\n[深度研究] 执行查询: {search_query}")
                
                # 记录查询步骤
                search_step_id = self.evidence_tracker.add_reasoning_step(
                    query_id,
                    search_query,
                    f"执行查询: {search_query}"
                )
                
                # 检查是否已执行过相同查询
                if self.thinking_engine.has_executed_query(search_query):
                    self._log(f"\n[深度研究] 已搜索过该查询: {search_query}")
                    summary_think = f"\n已搜索过该查询。请参考前面的结果。\n"
                    self.thinking_engine.add_reasoning_step(summary_think)
                    self.thinking_engine.add_human_message(summary_think)
                    think += self.thinking_engine.remove_result_tags(summary_think)
                    
                    self.evidence_tracker.add_evidence(
                        search_step_id,
                        "duplicate_query",
                        "已搜索过该查询，跳过重复执行",
                        "search_status"
                    )
                    continue
                
                # 记录已执行查询
                self.thinking_engine.add_executed_query(search_query)
                
                # 将搜索查询添加到消息历史
                self.thinking_engine.add_ai_message(f"{search_query}")
                think += f"\n\n> {iteration + 1}. {search_query}\n\n"
                
                # 执行实际搜索 - 添加缓存避免重复搜索
                search_results_cache_key = f"search_results:{search_query}"
                if not hasattr(self, '_search_results_cache'):
                    self._search_results_cache = {}
                    
                if search_results_cache_key in self._search_results_cache:
                    kbinfos = self._search_results_cache[search_results_cache_key]
                    self._log(f"\n[深度研究] 使用缓存的搜索结果: {search_query}")
                else:
                    self._log(f"\n[KB检索] 开始增强搜索: {search_query}")
                    
                    # 提取搜索查询的关键词
                    search_keywords = self.extract_keywords(search_query)
                    
                    # 使用Chain of Exploration增强搜索
                    enhanced_search_context = enhance_search_with_coe(self, search_query, search_keywords)
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
                    kbinfos = self.deep_research._search_current_provider(search_query)
                    
                    # 将探索内容加入结果
                    if exploration_content:
                        if "chunks" not in kbinfos:
                            kbinfos["chunks"] = []
                        kbinfos["chunks"].extend(exploration_content)
                    
                    # 缓存搜索结果
                    self._search_results_cache[search_results_cache_key] = kbinfos
                
                # 记录搜索结果基本信息
                chunks_count = len(kbinfos.get("chunks", []))
                entities_count = len(kbinfos.get("entities", []))
                rels_count = len(kbinfos.get("relationships", []))
                self._log(f"\n[KB检索] 结果: {chunks_count}个chunks, {entities_count}个实体, {rels_count}个关系")
                
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
                            # 减少实体提取，只对部分内容执行
                            if (hasattr(self.knowledge_builder, 'extractor') and 
                                self.knowledge_builder.extractor and 
                                chunks_count < 5):  # 只有当chunks数量较少时才提取
                                try:
                                    extract_cache_key = f"extract:{chunk_id}"
                                    if not hasattr(self, '_extract_cache'):
                                        self._extract_cache = {}
                                        
                                    if extract_cache_key not in self._extract_cache:
                                        self.knowledge_builder.extract_subgraph_from_chunk(
                                            chunk_text, 
                                            chunk_id
                                        )
                                        self._extract_cache[extract_cache_key] = True
                                except Exception as e:
                                    self._log(f"\n[深度研究] 提取实体关系失败: {e}")
                
                # 检查搜索结果是否为空
                has_results = (
                    kbinfos.get("chunks", []) or 
                    kbinfos.get("entities", []) or 
                    kbinfos.get("relationships", [])
                )
                
                if not has_results:
                    self._log(f"\n[KB检索] 没有找到与'{search_query}'相关的信息")
                    no_result_msg = f"\n没有找到与'{search_query}'相关的信息。请尝试使用不同的关键词进行搜索。\n"
                    self.thinking_engine.add_reasoning_step(no_result_msg)
                    self.thinking_engine.add_human_message(no_result_msg)
                    think += self.thinking_engine.remove_result_tags(no_result_msg)
                    
                    self.evidence_tracker.add_evidence(
                        search_step_id,
                        "no_results",
                        f"没有找到与查询相关的信息: {search_query}",
                        "search_result"
                    )
                    continue
                
                # 正常处理有结果的情况
                truncated_prev_reasoning = self.thinking_engine.prepare_truncated_reasoning()
                
                # 构建提取相关信息的提示
                kb_prompt_result = "\n".join(kb_prompt(kbinfos, 4096))
                
                # 检查提取缓存
                extract_cache_key = f"extract_info:{search_query}:{truncated_prev_reasoning[:100]}"
                if not hasattr(self, '_extraction_cache'):
                    self._extraction_cache = {}
                    
                if extract_cache_key in self._extraction_cache:
                    summary_think = self._extraction_cache[extract_cache_key]
                    self._log(f"\n[深度研究] 使用缓存的提取结果")
                else:
                    extract_prompt = RELEVANT_EXTRACTION_PROMPT.format(
                        prev_reasoning=truncated_prev_reasoning,
                        search_query=search_query,
                        document=kb_prompt_result
                    )
                    
                    # 使用LLM提取有用信息
                    self._log(f"\n[深度研究] 分析检索结果，提取有用信息")
                    extraction_msg = self.llm.invoke([
                        {"role": "system", "content": extract_prompt},
                        {"role": "user", "content": f'基于当前的搜索查询"{search_query}"和前面的推理步骤，分析每个知识来源并找出有用信息。'}
                    ])
                    
                    summary_think = extraction_msg.content if hasattr(extraction_msg, 'content') else str(extraction_msg)
                    # 缓存提取结果
                    self._extraction_cache[extract_cache_key] = summary_think
                
                # 保存重要信息
                has_useful_info = (
                    "**Final Information**" in summary_think and 
                    "No helpful information found" not in summary_think
                )
                
                if has_useful_info:
                    useful_info = summary_think.split("**Final Information**")[1].strip()
                    self.deep_research.all_retrieved_info.append(useful_info)
                    
                    # 记录有用信息作为证据
                    self._log(f"\n[深度研究] 发现有用信息: {useful_info}")
                    self.evidence_tracker.add_evidence_with_confidence(
                        search_step_id,
                        f"useful_info_{search_query}",
                        useful_info,
                        "extracted_knowledge",
                        confidence=0.85,  # 高可信度
                        metadata={"query": search_query}
                    )
                else:
                    self._log(f"\n[深度研究] 未从检索结果中提取到有用信息")
                    self.evidence_tracker.add_evidence(
                        search_step_id,
                        f"no_useful_info_{search_query}",
                        "未从检索结果中提取到有用信息",
                        "extraction_status"
                    )
                
                # 更新推理历史
                self.thinking_engine.add_reasoning_step(summary_think)
                self.thinking_engine.add_human_message(summary_think)
                think += self.thinking_engine.remove_result_tags(summary_think)
            
            # 在每轮迭代结束后，评估是否需要继续搜索
            if iteration > 0 and self.deep_research.all_retrieved_info:
                # 检查是否需要继续生成查询的缓存
                gap_check_key = f"gap_check:{query}:{len(self.deep_research.all_retrieved_info)}"
                if not hasattr(self, '_gap_check_cache'):
                    self._gap_check_cache = {}
                    
                if gap_check_key in self._gap_check_cache:
                    gap_needed = self._gap_check_cache[gap_check_key]
                else:
                    # 类似于DeepSearch中的_generate_gap_queries方法，检查是否需要继续生成查询
                    gap_needed = len(self.query_generator.generate_followup_queries(query, self.deep_research.all_retrieved_info)) > 0
                    self._gap_check_cache[gap_check_key] = gap_needed
                    
                if not gap_needed:
                    self._log("\n[深度研究] 已收集足够信息，无需进一步查询")
                    reflection_think = "\n已收集到足够的信息，可以开始整合分析了。"
                    self.thinking_engine.add_reasoning_step(reflection_think)
                    think += reflection_think
                    break
        return think


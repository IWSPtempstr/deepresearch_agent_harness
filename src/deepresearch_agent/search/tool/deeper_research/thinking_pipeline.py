# 由 search/tool/deeper_research_tool.py 拆分（T-014），方法体与原位置逐行一致。
"""关键词提取与多轮深度研究流水线编排（原 thinking 的入口与步骤1-6）。"""

from typing import Dict, Any, List
import json
from deepresearch_agent.search.tool.deeper_research import (
    enhance_search_with_coe,
    create_multiple_reasoning_branches,
)
from deepresearch_agent.search.tool.reasoning.validator import complexity_estimate


class DeeperThinkingMixin:
    def extract_keywords(self, query: str) -> Dict[str, List[str]]:
        """从查询中提取关键词"""
        # 检查缓存
        if query in self._keywords_cache:
            return self._keywords_cache[query]

        keywords = self.hybrid_tool.extract_keywords(query)
        
        # 缓存结果
        self._keywords_cache[query] = keywords
        return keywords
    def thinking(self, query: str) -> Dict[str, Any]:
        """
        执行增强版深度研究推理过程
        
        Args:
            query: 用户问题
                    
        Returns:
            Dict: 包含思考过程和最终答案的字典
        """
        # 清空执行日志
        self.execution_logs = []
        self._log(f"\n[深度研究] 开始处理查询: {query}")

        self._keywords_cache = {}
        
        # 检查思考缓存
        if hasattr(self, '_thinking_cache') and query in self._thinking_cache:
            return self._thinking_cache[query]
        
        think, query_id, initial_sub_queries, community_summaries, exploration_path = self._thinking_prepare(query)
        think = self._thinking_branches(query, query_id, think)
        think = self._thinking_iterate(query, query_id, initial_sub_queries, think)
        think, contradiction_result = self._thinking_resolve(query_id, think)
        return self._thinking_synthesize(query, query_id, think, initial_sub_queries, community_summaries, exploration_path, contradiction_result)
    def _thinking_prepare(self, query):
        """原 thinking() 步骤1-5：子查询/社区分析/图谱构建/上下文组装（原 179-361 行）。"""
        # 提取关键词
        keywords = self.extract_keywords(query)
        self._log(f"\n[深度研究] 提取关键词: {keywords}")
        
        # 开始新的查询跟踪
        query_id = self.evidence_tracker.start_new_query(query, keywords)
        self.current_query_context = {"query_id": query_id}
        
        # 重置分支信息
        self.explored_branches = {}
        
        # 步骤1: 使用QueryGenerator生成初始子查询
        if not hasattr(self, '_subquery_cache'):
            self._subquery_cache = {}
            
        if query in self._subquery_cache:
            initial_sub_queries = self._subquery_cache[query]
        else:
            initial_sub_queries = self.query_generator.generate_sub_queries(query)
            self._subquery_cache[query] = initial_sub_queries
            
        self._log(f"\n[深度研究] 生成了{len(initial_sub_queries)}个初始子查询: {initial_sub_queries}")
        
        # 记录为推理步骤
        subquery_step_id = self.evidence_tracker.add_reasoning_step(
            query_id,
            "initial_query_decomposition",
            f"将原始问题分解为{len(initial_sub_queries)}个子问题: {initial_sub_queries}"
        )
        
        # 步骤2: 社区感知和Chain of Exploration增强 - 添加缓存
        if not hasattr(self, '_search_cache'):
            self._search_cache = {}
            
        search_cache_key = f"enhanced_search:{query}"
        if search_cache_key in self._search_cache:
            enhanced_context = self._search_cache[search_cache_key]
        else:
            self._log(f"\n[深度研究] 开始社区感知与Chain of Exploration分析")
            enhanced_context = enhance_search_with_coe(self, query, keywords)
            self._search_cache[search_cache_key] = enhanced_context
            
        community_context = enhanced_context
        
        # 步骤3: 使用社区信息增强搜索策略
        search_strategy = community_context.get("search_strategy", {})
        follow_up_queries = search_strategy.get("follow_up_queries", [])
        
        # 合并社区生成的后续查询和初始子查询
        all_queries = list(set(initial_sub_queries + follow_up_queries))
        
        if follow_up_queries:
            self._log(f"\n[深度研究] 社区分析生成的后续查询: {follow_up_queries}")
        
        # 添加增强搜索策略的推理步骤
        strategy_step_id = self.evidence_tracker.add_reasoning_step(
            query_id, 
            "knowledge_community_analysis", 
            f"基于社区分析，识别了关键实体和相关查询策略。将探索以下后续查询: {all_queries}"
        )
        
        # 记录社区信息作为证据
        community_summaries = []
        if "community_info" in community_context and "summaries" in community_context["community_info"]:
            for summary_obj in community_context["community_info"]["summaries"]:
                if isinstance(summary_obj, dict) and "summary" in summary_obj:
                    community_summaries.append(summary_obj["summary"])
                else:
                    community_summaries.append(str(summary_obj))
                    
        if community_summaries:
            self._log(f"\n[深度研究] 找到 {len(community_summaries)} 个相关社区")
            for i, summary in enumerate(community_summaries):
                self._log(f"\n[深度研究] 社区 {i+1} 摘要: {summary[:100]}...")
                self.evidence_tracker.add_evidence(
                    strategy_step_id,
                    f"community_summary_{i}",
                    summary,
                    "community_knowledge"
                )
        
        # 步骤4: 构建初始知识图谱
        initial_entities = search_strategy.get("focus_entities", [])
        discovered_entities = search_strategy.get("discovered_entities", [])
        
        # 合并实体
        graph_entities = list(set(initial_entities + discovered_entities))
        
        if graph_entities:
            # 检查图谱缓存
            graph_cache_key = f"graph:{query}:{','.join(graph_entities[:5])}"
            if not hasattr(self, '_graph_cache'):
                self._graph_cache = {}
                
            if graph_cache_key not in self._graph_cache:
                self._log(f"\n[深度研究] 构建知识图谱，关注实体: {graph_entities}")
                self.knowledge_builder.build_query_graph(query, graph_entities, depth=2)  # 增加探索深度到2
                self._graph_cache[graph_cache_key] = True
            
            # 获取核心实体
            central_entities = self.knowledge_builder.get_central_entities(limit=5)
            central_entity_ids = [e["id"] for e in central_entities]
            
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
            self._log(f"\n[深度研究] 知识图谱统计: {kg_info['entity_count']} 个实体, {kg_info['relation_count']} 个关系")
            
            self.evidence_tracker.add_evidence(
                kg_step_id,
                "knowledge_graph",
                json.dumps(kg_info),
                "graph_structure"
            )
        
        # 步骤5: 整合探索结果到深度研究思考过程
        exploration_results = community_context.get("exploration_results", {})
        exploration_path = exploration_results.get("exploration_path", [])
        
        # 初始化结果容器
        think = ""
        self.deep_research.all_retrieved_info = []
        
        # 收集从探索中获取的内容
        exploration_content = []
        for content_item in exploration_results.get("content", []):
            if "text" in content_item:
                exploration_content.append(content_item["text"])
        
        # 初始化思考引擎
        self._log(f"\n[深度研究] 初始化思考引擎")
        if not hasattr(self.thinking_engine, 'initialized_for') or self.thinking_engine.initialized_for != query:
            self.thinking_engine.initialize_with_query(query)
            self.thinking_engine.initialized_for = query
        
        # 添加已有的社区信息和知识图谱信息作为上下文
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
        if exploration_path:
            context_message += "\n\n通过Chain of Exploration探索路径发现:\n"
            path_description = []
            for i, step in enumerate(exploration_path[:5]):  # 限制路径长度
                if i > 0:  # 跳过起始实体
                    path_description.append(f"- 步骤{step['step']}: 实体 {step['node_id']} ({step['reasoning']})")
            context_message += "\n".join(path_description)
            
        # 如果有探索内容，添加
        if exploration_content:
            context_message += "\n\n探索获取的相关内容:\n"
            for i, content in enumerate(exploration_content[:3]):  # 限制内容数量
                context_message += f"内容{i+1}: {content[:200]}...\n\n"
            
        # 如果有跟进查询，添加到思考过程
        if follow_up_queries:
            context_message += "\n\n可能的深入探索方向:\n"
            context_message += "\n".join([f"- {q}" for q in follow_up_queries])
        
        # 将上下文添加到思考引擎
        if context_message:
            self.thinking_engine.add_reasoning_step(context_message)
            think += context_message + "\n\n"
        return think, query_id, initial_sub_queries, community_summaries, exploration_path

    def _thinking_branches(self, query, query_id, think):
        """原 thinking() 步骤6：复杂查询多假设分支推理（原 363-388 行）。"""
        # 步骤6: 生成多个假设并创建多个推理分支  
        if complexity_estimate(query) > 0.7:  # 对复杂查询应用多分支推理
            self._log("\n[深度研究] 生成多个假设进行分支推理")
            
            # 检查假设缓存
            if not hasattr(self, '_hypothesis_cache'):
                self._hypothesis_cache = {}
                
            if query in self._hypothesis_cache:
                hypotheses = self._hypothesis_cache[query]
            else:
                # 生成假设并缓存
                hypotheses = self.query_generator.generate_multiple_hypotheses(query, self.llm)
                self._hypothesis_cache[query] = hypotheses
            
            if hypotheses:
                # 创建多个推理分支
                branch_results = create_multiple_reasoning_branches(self, query_id, hypotheses)
                
                # 添加分支概述
                branch_overview = "\n## 推理分支假设\n"
                for branch_name, info in branch_results.items():
                    branch_overview += f"- {branch_name}: {info['hypothesis']}\n"
                
                self.thinking_engine.add_reasoning_step(branch_overview)
                think += branch_overview + "\n\n"
        return think


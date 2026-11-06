# 由 agents/deep_research_agent.py 拆分（T-014），方法体与原位置逐行一致。
"""图谱探索/推理链分析/矛盾检测（原 explore_knowledge 等 3 个方法）。"""

from deepresearch_agent.config.prompts import (
    LC_SYSTEM_PROMPT,
    DEEP_RESEARCH_THINKING_SUMMARY_PROMPT,
    EXPLORATION_SUMMARY_PROMPT,
    CONTRADICTION_IMPACT_PROMPT,
)


class DeepResearchAnalysisMixin:
    def explore_knowledge(self, query: str, thread_id: str = "default"):
        """
        使用知识图谱探索模式探索主题
        
        参数:
            query: 用户查询或探索主题
            thread_id: 会话ID
            
        返回:
            dict: 包含探索结果的字典
        """
        # 需要使用增强版工具且有探索工具
        if not self.use_deeper_tool or not hasattr(self, 'exploration_tool'):
            return {"error": "知识图谱探索功能需要使用增强版研究工具"}
        
        # 提取关键词作为起始实体
        keywords = self.research_tool.extract_keywords(query)
        
        # 构建查询参数
        entities = keywords.get("high_level", []) + keywords.get("low_level", [])
        entities = entities[:3]  # 最多使用3个实体
        
        # 准备探索参数
        explore_query = {
            "query": query,
            "entities": entities
        }
        
        # 执行探索
        result = self.exploration_tool._run(explore_query)
        
        # 美化结果
        if isinstance(result, dict) and "exploration_path" in result:
            # 记录当前探索会话ID
            if "session_id" in result:
                self.session_context[thread_id] = {
                    "exploration_session_id": result["session_id"]
                }
                
            # 使用LLM生成探索摘要
            path_desc = []
            for step in result.get("exploration_path", []):
                step_num = step.get("step", 0)
                node_id = step.get("node_id", "")
                reasoning = step.get("reasoning", "")
                if step_num > 0:  # 跳过起始实体
                    path_desc.append(f"步骤{step_num}: 从'{node_id}'发现 - {reasoning}")
            
            path_summary = "\n".join(path_desc)
            
            # 提取关键内容
            key_content = []
            for item in result.get("content", [])[:5]:
                if "text" in item:
                    key_content.append(item["text"])
            
            content_summary = "\n\n".join(key_content)
            
            # 构建摘要提示
            summary_prompt = EXPLORATION_SUMMARY_PROMPT.format(
                query=query,
                path_summary=path_summary,
                content_summary=content_summary,
            )
            
            # 生成探索摘要
            try:
                response = self.llm.invoke(summary_prompt)
                exploration_summary = response.content if hasattr(response, 'content') else str(response)
            except Exception as e:
                exploration_summary = f"生成探索摘要时出错: {str(e)}"
            
            # 美化返回结果
            enhanced_result = {
                "exploration_path": result.get("exploration_path", []),
                "summary": exploration_summary,
                "key_entities": entities,
                "discovered_entities": [step.get("node_id") for step in result.get("exploration_path", []) if step.get("step", 0) > 0],
                "content_samples": key_content[:3],
                "path_visualization": path_desc,
            }
            
            return enhanced_result
        else:
            # 返回原始结果
            return result

    def analyze_reasoning_chain(self, query_id: str = None, thread_id: str = "default"):
        """
        分析推理链和证据
        
        参数:
            query_id: 查询ID，如果为None则使用当前会话的查询ID
            thread_id: 会话ID
            
        返回:
            dict: 包含推理链分析结果的字典
        """
        # 需要使用增强版工具
        if not self.use_deeper_tool or not hasattr(self, 'reasoning_analysis_tool'):
            return {"error": "推理链分析功能需要使用增强版研究工具"}
        
        # 如果未提供查询ID，尝试从会话上下文获取
        if not query_id:
            if thread_id in self.session_context and "latest_query_id" in self.session_context[thread_id]:
                query_id = self.session_context[thread_id]["latest_query_id"]
            elif hasattr(self.research_tool, 'current_query_context') and self.research_tool.current_query_context.get("query_id"):
                query_id = self.research_tool.current_query_context.get("query_id")
            else:
                return {"error": "未找到有效的查询ID，请先执行一次深度研究查询"}
        
        # 执行推理链分析
        return self.reasoning_analysis_tool._run(query_id)

    def detect_contradictions(self, query: str, thread_id: str = "default"):
        """
        检测和分析信息矛盾
        
        参数:
            query: 用户问题
            thread_id: 会话ID
            
        返回:
            dict: 包含矛盾分析的字典
        """
        # 需要使用增强版工具
        if not self.use_deeper_tool:
            return {"error": "矛盾检测功能需要使用增强版研究工具"}
            
        # 执行深度思考
        result = self.ask_with_thinking(query, thread_id)
        
        # 提取矛盾信息
        contradictions = result.get("contradictions", [])
        
        # 如果结果中没有矛盾信息但有查询ID，尝试使用矛盾检测方法
        if not contradictions and hasattr(self.research_tool, '_detect_and_resolve_contradictions'):
            query_id = None
            if hasattr(self.research_tool, 'current_query_context'):
                query_id = self.research_tool.current_query_context.get("query_id")
                
            if query_id:
                contradiction_result = self.research_tool._detect_and_resolve_contradictions(query_id)
                contradictions = contradiction_result.get("contradictions", [])
        
        # 结构化返回矛盾信息
        if contradictions:
            result = {
                "has_contradictions": True,
                "count": len(contradictions),
                "contradictions": contradictions,
                "analysis": "发现信息来源中存在矛盾，请谨慎对待最终结论。"
            }
            
            # 使用LLM分析矛盾影响
            if len(contradictions) > 0:
                # 准备提示
                contradiction_texts = []
                for i, contradiction in enumerate(contradictions):
                    if contradiction.get("type") == "numerical":
                        contradiction_texts.append(
                            f"{i+1}. 数值矛盾: 在'{contradiction.get('context', '')}'中，" +
                            f"发现值 {contradiction.get('value1')} 和 {contradiction.get('value2')}"
                        )
                    else:
                        contradiction_texts.append(f"{i+1}. 语义矛盾: {contradiction.get('analysis', '')}")
                
                contradictions_text = "\n".join(contradiction_texts)
                
                impact_prompt = CONTRADICTION_IMPACT_PROMPT.format(
                    query=query,
                    contradictions_text=contradictions_text,
                )
                
                try:
                    response = self.llm.invoke(impact_prompt)
                    impact_analysis = response.content if hasattr(response, 'content') else str(response)
                    result["impact_analysis"] = impact_analysis
                except Exception as e:
                    result["impact_analysis"] = f"分析矛盾影响时出错: {str(e)}"
            
            return result
        else:
            return {
                "has_contradictions": False,
                "count": 0,
                "contradictions": [],
                "analysis": "未在信息来源中检测到明显矛盾。"
            }

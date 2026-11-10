# 由 search/tool/reasoning/chain_of_exploration.py 拆分（T-014），方法体与原位置逐行一致。
"""探索策略生成与复杂度/宽度自适应（原 _generate_exploration_strategy 等）。"""

from typing import List, Dict, Any


class ChainStrategyMixin:
    def _generate_exploration_strategy(self, query: str, starting_entities: List[str]) -> Dict[str, Any]:
        """
        为查询生成探索策略
        
        Args:
            query: 查询字符串
            starting_entities: 起始实体
            
        Returns:
            Dict: 探索策略
        """
        prompt = f"""
        为以下查询生成图谱探索策略，从给定的起始实体开始探索:
        
        查询: "{query}"
        起始实体: {starting_entities}
        
        请提供以下信息:
        1. 探索重点: 探索应该关注哪些类型的关系和实体?
        2. 终止条件: 什么情况下应该终止特定方向的探索?
        3. 重要程度评分: 为不同类型的关系提供重要性权重(0-1)
        
        以JSON格式返回结果:
        {{
            "focus_relations": ["关系类型1", "关系类型2", ...],
            "focus_entity_types": ["实体类型1", "实体类型2", ...],
            "avoid_relations": ["应避免的关系类型1", ...],
            "termination_conditions": ["条件1", ...],
            "relation_weights": {{"关系类型1": 0.9, "关系类型2": 0.7, ...}}
        }}
        """
        
        try:
            response = self.llm.invoke(prompt)
            content = response.content if hasattr(response, 'content') else str(response)
            
            # 提取JSON部分
            import re
            import json
            
            json_match = re.search(r'{.*}', content, re.DOTALL)
            if json_match:
                strategy = json.loads(json_match.group(0))
                return strategy
            
            # 如果无法解析，返回默认策略
            return {
                "focus_relations": [],
                "focus_entity_types": [],
                "avoid_relations": [],
                "termination_conditions": [],
                "relation_weights": {}
            }
        except Exception as e:
            print(f"生成探索策略失败: {e}")
            # 默认策略
            return {
                "focus_relations": [],
                "focus_entity_types": [],
                "avoid_relations": [],
                "termination_conditions": [],
                "relation_weights": {}
            }

    def _calculate_adaptive_width(self, step, query, neighbors, base_width=3):
        """
        根据查询复杂度和当前步骤动态调整探索宽度
        
        Args:
            step: 当前步骤
            query: 查询字符串
            neighbors: 邻居节点列表
            base_width: 基础宽度
            
        Returns:
            int: 调整后的宽度
        """
        # 步骤越深，宽度越小，避免指数爆炸
        step_factor = max(0.5, 1.0 - step * 0.2)
        
        # 邻居节点数量因素 - 邻居越多，宽度越大但有上限
        neighbor_factor = min(1.5, len(neighbors) / 10)
        
        # 查询复杂度因素
        complexity_factor = self._estimate_query_complexity(query)
        
        # 计算最终宽度
        adjusted_width = int(base_width * step_factor * neighbor_factor * complexity_factor)
        
        # 确保宽度在合理范围内
        return max(1, min(5, adjusted_width))

    def _estimate_query_complexity(self, query):
        """
        估计查询复杂度
        
        Args:
            query: 查询字符串
            
        Returns:
            float: 复杂度评分(0.5-1.5)
        """
        # 基于查询长度、问号数量和关键词数量的简单启发式方法
        length_factor = min(1.5, len(query) / 50)
        question_marks = query.count("?") + query.count("？")
        question_factor = 1.0 + (question_marks * 0.1)
        
        # 识别复杂问题的关键词
        complexity_indicators = [
            "为什么", "如果", "原因", "关系", "比较", "区别",
            "影响", "分析", "评估", "预测"
        ]
        
        # 检查关键词
        indicator_count = sum(1 for indicator in complexity_indicators if indicator.lower() in query.lower())
        indicator_factor = 1.0 + (indicator_count * 0.1)
        
        # 综合评分,基础值0.5,最大1.5
        complexity = 0.5 + (length_factor * 0.3 + question_factor * 0.3 + indicator_factor * 0.4) / 3
        
        return min(1.5, complexity)

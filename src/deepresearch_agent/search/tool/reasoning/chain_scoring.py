# 由 search/tool/reasoning/chain_of_exploration.py 拆分（T-014），方法体与原位置逐行一致。
"""邻居打分与内容相关度排序（原 _score_neighbors_enhanced 等）。"""

from typing import List, Dict, Any
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import pandas as pd


class ChainScoringMixin:
    def _score_neighbors_enhanced(self, neighbors, query, query_embedding, exploration_strategy):
        """
        增强版邻居评分，考虑策略权重、相似度和关系权重
        
        Args:
            neighbors: 邻居节点列表
            query: 查询字符串
            query_embedding: 查询嵌入向量
            exploration_strategy: 探索策略
            
        Returns:
            List: 评分后的邻居列表
        """
        scored_neighbors = []
        relation_weights = exploration_strategy.get("relation_weights", {})
        focus_relations = exploration_strategy.get("focus_relations", [])
        focus_entity_types = exploration_strategy.get("focus_entity_types", [])
        avoid_relations = exploration_strategy.get("avoid_relations", [])
        
        for neighbor in neighbors:
            # 构建描述文本
            description = neighbor.get('description', '')
            relation_type = neighbor.get('relation_type', '')
            neighbor_id = neighbor.get('id', '')
            
            # 初始权重 - 基础值0.5
            base_weight = 0.5
            
            try:
                # 计算语义相似度
                if description:
                    neighbor_embedding = self.embeddings.embed_query(description)
                    similarity = cosine_similarity(
                        np.array(query_embedding).reshape(1, -1),
                        np.array(neighbor_embedding).reshape(1, -1)
                    )[0][0]
                else:
                    similarity = 0.0
                    
                # 获取关系权重
                relation_weight = relation_weights.get(relation_type, 1.0)
                
                # 计算策略匹配分数
                strategy_score = 1.0
                
                # 增加关注的关系类型的权重
                if relation_type in focus_relations:
                    strategy_score += 0.5
                
                # 检查实体类型
                entity_type = self._get_entity_type(neighbor_id)
                if entity_type in focus_entity_types:
                    strategy_score += 0.3
                
                # 降低需要避免的关系类型的权重
                if relation_type in avoid_relations:
                    strategy_score -= 0.5
                
                # 添加来自图的原始权重
                graph_weight = float(neighbor.get('weight', 1.0))
                
                # 计算最终得分(语义相似度*策略分数*关系权重*图权重)
                final_score = similarity * strategy_score * relation_weight * graph_weight
                
                # 添加到评分列表
                scored_neighbors.append({
                    "id": neighbor_id,
                    "description": description,
                    "relation_type": relation_type,
                    "source": neighbor.get('source', ''),
                    "target": neighbor.get('target', ''),
                    "similarity": similarity,
                    "strategy_score": strategy_score,
                    "relation_weight": relation_weight,
                    "graph_weight": graph_weight,
                    "final_score": final_score
                })
            except Exception as e:
                print(f"计算节点相似度失败: {e}")
                
        # 按最终得分排序
        return sorted(scored_neighbors, key=lambda x: x['final_score'], reverse=True)

    def _get_entity_type(self, entity_id):
        """
        获取实体类型
        
        Args:
            entity_id: 实体ID
            
        Returns:
            str: 实体类型
        """
        try:
            query = """
            MATCH (e:__Entity__ {id: $entity_id})
            RETURN labels(e) AS types
            """
            
            result = self.graph.query(query, params={"entity_id": entity_id})
            
            if not result or (hasattr(result, 'empty') and result.empty):
                return "unknown"
                
            if isinstance(result, pd.DataFrame):
                types = result.iloc[0]['types']
                # 过滤掉 "__Entity__" 标签
                entity_types = [t for t in types if t != "__Entity__"]
                return entity_types[0] if entity_types else "unknown"
            else:
                # 处理其他结果格式
                return "unknown"
        except Exception as e:
            print(f"获取实体类型失败: {e}")
            return "unknown"

    def _rank_content_by_relevance(self, query_embedding, content_list):
        """
        根据与查询的相关性排序内容
        
        Args:
            query_embedding: 查询嵌入向量
            content_list: 内容列表
            
        Returns:
            List: 排序后的内容列表
        """
        if not content_list:
            return []
            
        scored_content = []
        
        for content in content_list:
            text = content.get("text", "")
            
            if not text:
                continue
                
            try:
                # 计算文本嵌入
                text_embedding = self.embeddings.embed_query(text)
                
                # 计算相似度
                similarity = cosine_similarity(
                    np.array(query_embedding).reshape(1, -1),
                    np.array(text_embedding).reshape(1, -1)
                )[0][0]
                
                # 添加相似度分数
                scored_item = content.copy()
                scored_item["relevance_score"] = similarity
                scored_content.append(scored_item)
                
            except Exception as e:
                print(f"计算内容相似度失败: {e}")
                scored_content.append(content)
        
        # 按相关性排序
        return sorted(scored_content, key=lambda x: x.get("relevance_score", 0), reverse=True)

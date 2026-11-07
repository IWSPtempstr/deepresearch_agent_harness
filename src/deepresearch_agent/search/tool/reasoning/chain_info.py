# 由 search/tool/reasoning/chain_of_exploration.py 拆分（T-014），方法体与原位置逐行一致。
"""图上实体/关系/内容/社区信息读取（原 _get_neighbors 与 _get_*_info）。"""

from typing import List, Dict, Any
import numpy as np
import pandas as pd


class ChainInfoMixin:
    def _get_neighbors(self, entities):
        """
        获取实体的邻居节点
        
        Args:
            entities: 实体ID列表
            
        Returns:
            List: 邻居节点列表
        """
        try:
            query = """
            MATCH (e:__Entity__)-[r]-(neighbor:__Entity__)
            WHERE e.id IN $entity_ids AND NOT neighbor.id IN $visited_ids
            RETURN neighbor.id AS id, neighbor.description AS description,
                   type(r) AS relation_type, startNode(r).id AS source,
                   endNode(r).id AS target,
                   CASE WHEN r.weight IS NOT NULL THEN r.weight ELSE 1.0 END AS weight
            LIMIT 100
            """
            
            params = {
                "entity_ids": entities, 
                "visited_ids": list(self.visited_nodes)
            }
            
            result = self.graph.query(query, params=params)
            
            # 结果为空的处理
            if not result or (hasattr(result, 'empty') and result.empty):
                return []
                
            # 转换为列表格式
            if isinstance(result, pd.DataFrame):
                neighbors_list = result.to_dict('records')
                return neighbors_list
            else:
                return result
                
        except Exception as e:
            print(f"获取邻居节点失败: {e}")
            return []

    def _get_entity_info(self, entities):
        """
        获取实体详细信息
        
        Args:
            entities: 实体ID列表
            
        Returns:
            List: 实体信息列表
        """
        if not entities:
            return []
            
        try:
            query = """
            MATCH (e:__Entity__)
            WHERE e.id IN $entity_ids
            RETURN e.id AS id, e.description AS description,
                   labels(e) AS types
            """
            
            result = self.graph.query(query, params={"entity_ids": entities})
            
            if not result or (hasattr(result, 'empty') and result.empty):
                return []
                
            # 转换结果
            if isinstance(result, pd.DataFrame):
                return result.to_dict('records')
            return result
            
        except Exception as e:
            print(f"获取实体信息失败: {e}")
            return []

    def _get_relationship_info(self, entities):
        """
        获取实体关系信息
        
        Args:
            entities: 实体ID列表
            
        Returns:
            List: 关系信息列表
        """
        if not entities:
            return []
            
        try:
            query = """
            MATCH (e1:__Entity__)-[r]-(e2:__Entity__)
            WHERE e1.id IN $entity_ids AND e2.id IN $visited_ids
            RETURN startNode(r).id AS source, endNode(r).id AS target,
                   type(r) AS type, r.description AS description,
                   CASE WHEN r.weight IS NOT NULL THEN r.weight ELSE 1.0 END AS weight
            """
            
            result = self.graph.query(
                query, 
                params={
                    "entity_ids": entities,
                    "visited_ids": list(self.visited_nodes)
                }
            )
            
            if not result or (hasattr(result, 'empty') and result.empty):
                return []
                
            # 转换结果
            if isinstance(result, pd.DataFrame):
                return result.to_dict('records')
            return result
            
        except Exception as e:
            print(f"获取关系信息失败: {e}")
            return []

    def _get_content_info(self, entities):
        """
        获取与实体相关的内容信息
        
        Args:
            entities: 实体ID列表
            
        Returns:
            List: 内容信息列表
        """
        if not entities:
            return []
            
        try:
            query = """
            MATCH (c:__Chunk__)-[:MENTIONS]->(e:__Entity__)
            WHERE e.id IN $entity_ids
            RETURN DISTINCT c.id AS id, c.text AS text
            LIMIT 20
            """
            
            result = self.graph.query(query, params={"entity_ids": entities})
            
            if not result or (hasattr(result, 'empty') and result.empty):
                return []
                
            # 转换结果
            if isinstance(result, pd.DataFrame):
                return result.to_dict('records')
            return result
            
        except Exception as e:
            print(f"获取内容信息失败: {e}")
            return []

    def _get_community_info(self, entities):
        """
        获取实体所属社区信息
        
        Args:
            entities: 实体ID列表
            
        Returns:
            List: 社区信息列表
        """
        if not entities:
            return []
            
        try:
            query = """
            MATCH (e:__Entity__)-[:IN_COMMUNITY]->(c:__Community__)
            WHERE e.id IN $entity_ids
            RETURN DISTINCT c.id AS community_id, c.summary AS summary
            """
            
            result = self.graph.query(query, params={"entity_ids": entities})
            
            if not result or (hasattr(result, 'empty') and result.empty):
                return []
                
            # 转换结果
            if isinstance(result, pd.DataFrame):
                return result.to_dict('records')
            return result
            
        except Exception as e:
            print(f"获取社区信息失败: {e}")
            return []

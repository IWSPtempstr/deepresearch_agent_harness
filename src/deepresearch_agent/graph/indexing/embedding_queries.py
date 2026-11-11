# 由 src/deepresearch_agent/graph/indexing/embedding_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""待更新实体/Chunk 查询（原 get_*_needing_update）。"""

from typing import List, Dict, Any


class EmbeddingQueriesMixin:
    def get_entities_needing_update(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        获取需要更新Embedding的实体
        
        Args:
            limit: 返回的最大实体数量
            
        Returns:
            List[Dict]: 需要更新的实体列表
        """
        query = """
        MATCH (e:`__Entity__`)
        WHERE e.embedding IS NULL 
        OR (e.needs_reembedding IS NOT NULL AND e.needs_reembedding = true)
        RETURN elementId(e) AS neo4j_id,
            e.id AS entity_id, 
            CASE WHEN e.description IS NOT NULL THEN e.description ELSE e.id END AS text
        LIMIT $limit
        """
        
        result = self.graph.query(query, params={"limit": limit})
        return result if result else []

    def get_chunks_needing_update(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        获取需要更新Embedding的Chunk
        
        Args:
            limit: 返回的最大Chunk数量
            
        Returns:
            List[Dict]: 需要更新的Chunk列表
        """
        query = """
        MATCH (c:`__Chunk__`)
        WHERE c.embedding IS NULL 
            OR c.needs_reembedding = true
            OR (c.last_updated IS NOT NULL AND 
                (c.last_embedded IS NULL OR c.last_updated > c.last_embedded))
        RETURN elementId(c) AS neo4j_id,
               c.id AS chunk_id, 
               c.text AS text
        LIMIT $limit
        """
        
        result = self.graph.query(query, params={"limit": limit})
        return result if result else []

# 由 src/deepresearch_agent/graph/indexing/embedding_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""更新标记（原 mark_* 4 方法）。"""

from typing import List


class EmbeddingMarkMixin:
    def mark_entities_for_update(self, entity_ids: List[str]) -> int:
        """
        标记实体需要更新Embedding
        
        Args:
            entity_ids: 实体ID列表
            
        Returns:
            int: 标记的实体数量
        """
        if not entity_ids:
            return 0
            
        query = """
        UNWIND $entity_ids AS entity_id
        MATCH (e:`__Entity__` {id: entity_id})
        SET e.needs_reembedding = true,
            e.last_updated = datetime()
        RETURN count(e) AS marked
        """
        
        result = self.graph.query(query, params={"entity_ids": entity_ids})
        marked = result[0]["marked"] if result else 0
        
        self.console.print(f"[blue]已标记 {marked} 个实体需要更新Embedding[/blue]")
        
        return marked

    def mark_chunks_for_update(self, chunk_ids: List[str]) -> int:
        """
        标记Chunk需要更新Embedding
        
        Args:
            chunk_ids: Chunk ID列表
            
        Returns:
            int: 标记的Chunk数量
        """
        if not chunk_ids:
            return 0
            
        query = """
        UNWIND $chunk_ids AS chunk_id
        MATCH (c:`__Chunk__` {id: chunk_id})
        SET c.needs_reembedding = true,
            c.last_updated = datetime()
        RETURN count(c) AS marked
        """
        
        result = self.graph.query(query, params={"chunk_ids": chunk_ids})
        marked = result[0]["marked"] if result else 0
        
        self.console.print(f"[blue]已标记 {marked} 个Chunk需要更新Embedding[/blue]")
        
        return marked

    def mark_document_chunks_for_update(self, filename: str) -> int:
        """
        标记文档的所有Chunk需要更新Embedding
        
        Args:
            filename: 文件名
            
        Returns:
            int: 标记的Chunk数量
        """
        query = """
        MATCH (d:`__Document__` {fileName: $filename})<-[:PART_OF]-(c:`__Chunk__`)
        SET c.needs_reembedding = true,
            c.last_updated = datetime()
        RETURN count(c) AS marked
        """
        
        result = self.graph.query(query, params={"filename": filename})
        marked = result[0]["marked"] if result else 0
        
        self.console.print(f"[blue]已标记文件 {filename} 的 {marked} 个Chunk需要更新Embedding[/blue]")
        
        return marked

    def mark_changed_files_chunks(self, changed_files: List[str]) -> int:
        """
        标记变更文件的所有Chunk需要更新Embedding
        
        Args:
            changed_files: 变更的文件列表
            
        Returns:
            int: 标记的Chunk数量
        """
        if not changed_files:
            return 0
            
        total_marked = 0
        for filename in changed_files:
            # 获取文件名（不包含路径）
            file_name = filename.split("/")[-1]
            marked = self.mark_document_chunks_for_update(file_name)
            total_marked += marked
        
        return total_marked

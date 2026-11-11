# 由 src/deepresearch_agent/graph/graph_consistency_validator.py 拆分（T-016），方法体与原位置逐行一致。
"""五类修复（原 repair_* 5 方法 + repair_graph）。"""

import time
from typing import Dict, List, Any


class ConsistencyRepairsMixin:
    def repair_orphan_entities(self, orphan_ids: List[str] = None) -> int:
        """
        修复孤立实体节点（删除或标记）
        
        Args:
            orphan_ids: 要修复的孤立实体ID列表，如果为None则自动检测
            
        Returns:
            int: 修复的节点数量
        """
        if orphan_ids is None:
            orphan_ids, _ = self.check_orphan_entities()
        
        if not orphan_ids:
            return 0
        
        # 删除孤立实体
        delete_query = """
        UNWIND $orphan_ids AS entity_id
        MATCH (e:`__Entity__` {id: entity_id})
        WHERE NOT (e)<-[:MENTIONS]-()
          AND NOT e.manual_edit = true
          AND NOT e.protected = true
        DELETE e
        RETURN count(*) AS deleted
        """
        
        result = self.graph.query(delete_query, params={"orphan_ids": orphan_ids})
        
        deleted = result[0]["deleted"] if result else 0
        
        self.console.print(f"[green]已删除 {deleted} 个孤立实体节点[/green]")
        
        return deleted

    def repair_dangling_chunks(self, dangling_ids: List[str] = None) -> int:
        """
        修复悬空Chunk节点（删除）
        
        Args:
            dangling_ids: 要修复的悬空Chunk ID列表，如果为None则自动检测
            
        Returns:
            int: 修复的节点数量
        """
        if dangling_ids is None:
            dangling_ids, _ = self.check_dangling_chunks()
        
        if not dangling_ids:
            return 0
        
        # 删除悬空Chunk
        delete_query = """
        UNWIND $dangling_ids AS chunk_id
        MATCH (c:`__Chunk__` {id: chunk_id})
        WHERE NOT (c)-[:PART_OF]->()
        DETACH DELETE c
        RETURN count(*) AS deleted
        """
        
        result = self.graph.query(delete_query, params={"dangling_ids": dangling_ids})
        
        deleted = result[0]["deleted"] if result else 0
        
        self.console.print(f"[green]已删除 {deleted} 个悬空Chunk节点[/green]")
        
        return deleted

    def repair_empty_chunks(self, empty_ids: List[str] = None) -> int:
        """
        修复空Chunk节点（添加占位符文本或删除）
        
        Args:
            empty_ids: 要修复的空Chunk ID列表，如果为None则自动检测
            
        Returns:
            int: 修复的节点数量
        """
        if empty_ids is None:
            empty_ids, _ = self.check_empty_chunks()
        
        if not empty_ids:
            return 0
        
        # 为空Chunk添加占位符文本
        repair_query = """
        UNWIND $empty_ids AS chunk_id
        MATCH (c:`__Chunk__` {id: chunk_id})
        WHERE c.text IS NULL OR c.text = ''
        SET c.text = '[Empty Chunk]', c.repaired = true
        RETURN count(*) AS repaired
        """
        
        result = self.graph.query(repair_query, params={"empty_ids": empty_ids})
        
        repaired = result[0]["repaired"] if result else 0
        
        self.console.print(f"[green]已修复 {repaired} 个空Chunk节点[/green]")
        
        return repaired

    def repair_broken_doc_links(self) -> int:
        """
        修复断开的文档链接（创建缺失的FIRST_CHUNK关系）
        
        Returns:
            int: 修复的关系数量
        """
        repair_query = """
        MATCH (d:`__Document__`)
        WHERE NOT (d)-[:FIRST_CHUNK]->()
        
        MATCH (c:`__Chunk__`)-[:PART_OF]->(d)
        WHERE c.position = 1 OR c.position IS NULL
        
        WITH d, c ORDER BY c.position ASC LIMIT 1
        MERGE (d)-[r:FIRST_CHUNK]->(c)
        
        RETURN count(r) AS repaired
        """
        
        result = self.graph.query(repair_query)
        
        repaired = result[0]["repaired"] if result else 0
        
        self.console.print(f"[green]已修复 {repaired} 个断开的文档链接[/green]")
        
        return repaired

    def repair_broken_chunk_chains(self) -> int:
        """
        修复断开的Chunk链（重建NEXT_CHUNK关系）
        
        Returns:
            int: 修复的关系数量
        """
        repair_query = """
        MATCH (d:`__Document__`)
        WITH d
        MATCH (c1:`__Chunk__`)-[:PART_OF]->(d)
        WHERE c1.position IS NOT NULL
        WITH d, c1 ORDER BY c1.position ASC
        WITH d, collect(c1) AS chunks
        UNWIND range(0, size(chunks)-2) AS i
        WITH d, chunks[i] AS current, chunks[i+1] AS next
        WHERE NOT (current)-[:NEXT_CHUNK]->(next)
        MERGE (current)-[r:NEXT_CHUNK]->(next)
        RETURN count(r) AS repaired
        """
        
        result = self.graph.query(repair_query)
        
        repaired = result[0]["repaired"] if result else 0
        
        self.console.print(f"[green]已修复 {repaired} 个断开的Chunk链[/green]")
        
        return repaired

    def repair_graph(self) -> Dict[str, Any]:
        """
        执行图谱修复操作
        
        Returns:
            Dict: 修复结果统计
        """
        start_time = time.time()
        
        # 先进行全面验证
        validation_result = self.validate_graph()
        
        # 根据验证结果进行修复
        repairs = {
            "orphan_entities": self.repair_orphan_entities(validation_result.get("orphan_ids", [])),
            "dangling_chunks": self.repair_dangling_chunks(validation_result.get("dangling_ids", [])),
            "empty_chunks": self.repair_empty_chunks(validation_result.get("empty_ids", [])),
            "broken_doc_links": self.repair_broken_doc_links(),
            "broken_chunk_chains": self.repair_broken_chunk_chains()
        }
        
        # 计算总修复数量
        total_repaired = sum(repairs.values())
        self.validation_stats["repaired_issues"] = total_repaired
        
        # 计算修复时间
        self.repair_time = time.time() - start_time
        
        self.console.print(f"[blue]图谱修复完成，耗时: {self.repair_time:.2f}秒[/blue]")
        self.console.print(f"[blue]共修复 {total_repaired} 个一致性问题[/blue]")
        
        return {
            "validation_time": self.validation_time,
            "repair_time": self.repair_time,
            "validation_stats": self.validation_stats,
            "repairs": repairs
        }

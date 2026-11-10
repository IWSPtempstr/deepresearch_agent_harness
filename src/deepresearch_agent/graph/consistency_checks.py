# 由 src/deepresearch_agent/graph/graph_consistency_validator.py 拆分（T-016），方法体与原位置逐行一致。
"""五类一致性检查（原 check_* 5 方法 + validate_graph）。"""

import time
from typing import Dict, List, Any, Tuple


class ConsistencyChecksMixin:
    def check_orphan_entities(self) -> Tuple[List[str], int]:
        """
        检查孤立的实体节点（没有被任何Chunk引用）
        
        Returns:
            Tuple: 孤立实体ID列表和计数
        """
        query = """
        MATCH (e:`__Entity__`)
        WHERE NOT (e)<-[:MENTIONS]-()
          AND NOT e.manual_edit = true
          AND NOT e.protected = true
        RETURN e.id AS entity_id, count(e) AS count
        """
        
        result = self.graph.query(query)
        
        orphan_ids = []
        orphan_count = 0
        
        if result:
            orphan_count = result[0]["count"]
            # 获取最多1000个孤立实体ID
            id_query = """
            MATCH (e:`__Entity__`)
            WHERE NOT (e)<-[:MENTIONS]-()
              AND NOT e.manual_edit = true
              AND NOT e.protected = true
            RETURN e.id AS entity_id
            LIMIT 1000
            """
            id_result = self.graph.query(id_query)
            orphan_ids = [r["entity_id"] for r in id_result]
        
        self.validation_stats["orphan_entities"] = orphan_count
        
        return orphan_ids, orphan_count

    def check_dangling_chunks(self) -> Tuple[List[str], int]:
        """
        检查悬空的Chunk节点（没有关联到Document）
        
        Returns:
            Tuple: 悬空Chunk ID列表和计数
        """
        query = """
        MATCH (c:`__Chunk__`)
        WHERE NOT (c)-[:PART_OF]->()
        RETURN c.id AS chunk_id, count(c) AS count
        """
        
        result = self.graph.query(query)
        
        dangling_ids = []
        dangling_count = 0
        
        if result:
            dangling_count = result[0]["count"]
            # 获取最多1000个悬空Chunk ID
            id_query = """
            MATCH (c:`__Chunk__`)
            WHERE NOT (c)-[:PART_OF]->()
            RETURN c.id AS chunk_id
            LIMIT 1000
            """
            id_result = self.graph.query(id_query)
            dangling_ids = [r["chunk_id"] for r in id_result]
        
        self.validation_stats["dangling_chunks"] = dangling_count
        
        return dangling_ids, dangling_count

    def check_empty_chunks(self) -> Tuple[List[str], int]:
        """
        检查空的Chunk节点（没有文本内容）
        
        Returns:
            Tuple: 空Chunk ID列表和计数
        """
        query = """
        MATCH (c:`__Chunk__`)
        WHERE c.text IS NULL OR c.text = ''
        RETURN c.id AS chunk_id, count(c) AS count
        """
        
        result = self.graph.query(query)
        
        empty_ids = []
        empty_count = 0
        
        if result:
            empty_count = result[0]["count"]
            # 获取最多1000个空Chunk ID
            id_query = """
            MATCH (c:`__Chunk__`)
            WHERE c.text IS NULL OR c.text = ''
            RETURN c.id AS chunk_id
            LIMIT 1000
            """
            id_result = self.graph.query(id_query)
            empty_ids = [r["chunk_id"] for r in id_result]
        
        self.validation_stats["empty_chunks"] = empty_count
        
        return empty_ids, empty_count

    def check_broken_doc_links(self) -> int:
        """
        检查文档链接是否完整（Document应该有FIRST_CHUNK关系）
        
        Returns:
            int: 有问题的文档数量
        """
        query = """
        MATCH (d:`__Document__`)
        WHERE NOT (d)-[:FIRST_CHUNK]->()
        RETURN count(d) AS count
        """
        
        result = self.graph.query(query)
        
        count = result[0]["count"] if result else 0
        self.validation_stats["broken_doc_links"] = count
        
        return count

    def check_broken_chunk_chains(self) -> int:
        """
        检查文本块链是否完整（前后关系）
        
        Returns:
            int: 有问题的链数量
        """
        query = """
        MATCH (c:`__Chunk__`)-[:PART_OF]->(d:`__Document__`)
        WHERE c.position > 1 AND NOT (c)<-[:NEXT_CHUNK]-()
        RETURN count(c) AS count
        """
        
        result = self.graph.query(query)
        
        count = result[0]["count"] if result else 0
        self.validation_stats["broken_chunk_chains"] = count
        
        return count

    def validate_graph(self) -> Dict[str, Any]:
        """
        执行全面的图谱验证
        
        Returns:
            Dict: 验证结果统计
        """
        start_time = time.time()
        
        # 1. 检查孤立实体
        orphan_ids, orphan_count = self.check_orphan_entities()
        if orphan_count > 0:
            self.console.print(f"[yellow]发现 {orphan_count} 个孤立实体节点[/yellow]")
        
        # 2. 检查悬空Chunk
        dangling_ids, dangling_count = self.check_dangling_chunks()
        if dangling_count > 0:
            self.console.print(f"[yellow]发现 {dangling_count} 个悬空Chunk节点[/yellow]")
        
        # 3. 检查空Chunk
        empty_ids, empty_count = self.check_empty_chunks()
        if empty_count > 0:
            self.console.print(f"[yellow]发现 {empty_count} 个空Chunk节点[/yellow]")
        
        # 4. 检查文档链接
        broken_doc_count = self.check_broken_doc_links()
        if broken_doc_count > 0:
            self.console.print(f"[yellow]发现 {broken_doc_count} 个没有FIRST_CHUNK关系的文档[/yellow]")
        
        # 5. 检查块链完整性
        broken_chain_count = self.check_broken_chunk_chains()
        if broken_chain_count > 0:
            self.console.print(f"[yellow]发现 {broken_chain_count} 个断开的Chunk链[/yellow]")
        
        # 计算总问题数
        total_issues = (orphan_count + dangling_count + empty_count + 
                       broken_doc_count + broken_chain_count)
        self.validation_stats["total_issues"] = total_issues
        
        # 计算验证时间
        self.validation_time = time.time() - start_time
        
        self.console.print(f"[blue]图谱验证完成，耗时: {self.validation_time:.2f}秒[/blue]")
        self.console.print(f"[blue]共发现 {total_issues} 个一致性问题[/blue]")
        
        return {
            "validation_time": self.validation_time,
            "validation_stats": self.validation_stats,
            "orphan_ids": orphan_ids,
            "dangling_ids": dangling_ids,
            "empty_ids": empty_ids
        }

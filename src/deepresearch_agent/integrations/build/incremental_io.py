# 由 integrations/build/incremental_graph_builder.py 拆分（T-014），方法体与原位置逐行一致。
"""向量更新与图结构导入导出（原 update_changed_file_embeddings 等 3 个方法）。"""

import time
from typing import Dict, List, Any
from pathlib import Path


class GraphIoMixin:
    def update_changed_file_embeddings(self, changed_files: List[str]) -> Dict[str, int]:
        """
        更新变更文件相关的Embedding
        
        Args:
            changed_files: 变更的文件列表
            
        Returns:
            Dict: 更新统计
        """
        if not changed_files:
            return {"entities": 0, "chunks": 0}
            
        # 标记变更文件的Chunk需要更新Embedding
        marked_chunks = self.embedding_manager.mark_changed_files_chunks(changed_files)
        
        # 查找这些Chunk关联的实体，标记需要更新Embedding
        query = """
        MATCH (c:`__Chunk__`)-[:MENTIONS]->(e:`__Entity__`)
        WHERE c.fileName IN $filenames OR c.needs_reembedding = true
        SET e.needs_reembedding = true,
            e.last_updated = datetime()
        RETURN count(DISTINCT e) AS entity_count
        """
        
        # 获取文件名（不包含路径）
        filenames = [Path(file).name for file in changed_files]
        
        result = self.graph.query(query, params={"filenames": filenames})
        marked_entities = result[0]["entity_count"] if result else 0
        
        self.console.print(f"[blue]已标记 {marked_chunks} 个Chunk和 {marked_entities} 个实体需要更新Embedding[/blue]")
        
        # 执行更新
        updated_entities = self.embedding_manager.update_entity_embeddings()
        updated_chunks = self.embedding_manager.update_chunk_embeddings()
        
        # 更新统计
        self.stats["entities_updated"] += updated_entities
        self.stats["chunks_updated"] += updated_chunks
        
        return {
            "entities": updated_entities,
            "chunks": updated_chunks
        }

    def export_graph_structure(self) -> Dict[str, Any]:
        """
        导出当前图谱结构
        
        Returns:
            Dict: 图谱结构数据
        """
        # 查询所有实体节点
        node_query = """
        MATCH (e:`__Entity__`)
        RETURN e.id AS id, 
               labels(e) AS labels,
               properties(e) AS properties,
               e.last_updated AS last_updated
        """
        
        node_result = self.graph.query(node_query)
        
        # 查询所有关系
        edge_query = """
        MATCH (s:`__Entity__`)-[r]->(t:`__Entity__`)
        RETURN s.id AS source,
               t.id AS target,
               type(r) AS type,
               properties(r) AS properties,
               CASE WHEN r.last_updated IS NOT NULL THEN r.last_updated ELSE null END AS last_updated
        """
        
        edge_result = self.graph.query(edge_query)
        
        # 构建图谱结构
        nodes = {}
        for node in node_result:
            node_id = node["id"]
            nodes[node_id] = {
                "id": node_id,
                "labels": node["labels"],
                "properties": node["properties"],
                "last_updated": node["last_updated"] if node["last_updated"] else time.time()
            }
        
        edges = []
        for edge in edge_result:
            edges.append({
                "source": edge["source"],
                "target": edge["target"],
                "type": edge["type"],
                "properties": edge["properties"],
                "last_updated": edge["last_updated"] if edge["last_updated"] else time.time()
            })
        
        return {
            "nodes": nodes,
            "edges": edges
        }

    def import_graph_structure(self, graph_structure: Dict[str, Any]) -> Dict[str, int]:
        """
        导入图谱结构
        
        Args:
            graph_structure: 图谱结构数据
            
        Returns:
            Dict: 导入统计
        """
        if not graph_structure:
            return {"nodes": 0, "edges": 0}
            
        # 导入节点
        nodes = list(graph_structure.get("nodes", {}).values())
        node_count = 0
        
        if nodes:
            # 准备节点数据
            node_data = []
            for node in nodes:
                node_data.append({
                    "id": node["id"],
                    "labels": node.get("labels", ["__Entity__"]),
                    "properties": node.get("properties", {})
                })
            
            # 执行导入
            node_query = """
            UNWIND $nodes AS node
            CALL apoc.merge.node(
                node.labels,
                {id: node.id},
                node.properties,
                node.properties
            )
            YIELD node as n
            RETURN count(n) AS node_count
            """
            
            try:
                node_result = self.graph.query(node_query, params={"nodes": node_data})
                node_count = node_result[0]["node_count"] if node_result else 0
            except Exception as e:
                self.console.print(f"[yellow]导入节点时出错 (可能APOC未安装): {e}[/yellow]")
                
                # 使用备用方法
                simple_node_query = """
                UNWIND $nodes AS node
                MERGE (n:`__Entity__` {id: node.id})
                SET n += node.properties
                RETURN count(n) AS node_count
                """
                
                node_result = self.graph.query(simple_node_query, params={"nodes": node_data})
                node_count = node_result[0]["node_count"] if node_result else 0
        
        # 导入边
        edges = graph_structure.get("edges", [])
        edge_count = 0
        
        if edges:
            # 执行导入
            self.integrate_new_relationships(edges)
            edge_count = len(edges)
        
        return {
            "nodes": node_count,
            "edges": edge_count
        }

# 由 integrations/build/incremental_graph_builder.py 拆分（T-014），方法体与原位置逐行一致。
"""新实体/关系并入与图结构合并（原 integrate_* 与 merge_graph_structures）。"""

import time
from typing import Dict, List, Any


class GraphMergeMixin:
    def integrate_new_entities(self, new_entities: List[Dict[str, Any]]) -> int:
        """
        无缝集成新实体到现有图结构
        
        Args:
            new_entities: 新实体列表
            
        Returns:
            int: 集成的实体数量
        """
        if not new_entities:
            return 0
            
        # 合并实体
        query = """
        UNWIND $entities AS entity
        MERGE (e:`__Entity__` {id: entity.id})
        ON CREATE 
            SET e += entity.properties,
                e.created_at = datetime(),
                e.needs_reembedding = true
        ON MATCH 
            SET e += entity.properties,
                e.last_updated = datetime(),
                e.needs_reembedding = true
        RETURN count(e) AS entity_count
        """
        
        # 准备实体数据
        entities_data = []
        for entity in new_entities:
            entity_data = {
                "id": entity.get("id", ""),
                "properties": {
                    k: v for k, v in entity.items() if k != "id"
                }
            }
            entities_data.append(entity_data)
        
        # 执行查询
        result = self.graph.query(query, params={"entities": entities_data})
        entity_count = result[0]["entity_count"] if result else 0
        
        # 更新统计
        self.stats["entities_integrated"] += entity_count
        
        self.console.print(f"[green]已集成 {entity_count} 个实体[/green]")
        
        return entity_count

    def integrate_new_relationships(self, new_relationships: List[Dict[str, Any]]) -> int:
        """
        无缝集成新关系到现有图结构
        
        Args:
            new_relationships: 新关系列表
            
        Returns:
            int: 集成的关系数量
        """
        if not new_relationships:
            return 0
            
        # 合并关系
        query = """
        UNWIND $relationships AS rel
        MATCH (s:`__Entity__` {id: rel.source_id})
        MATCH (t:`__Entity__` {id: rel.target_id})
        CALL apoc.merge.relationship(s, rel.type, 
            {}, 
            rel.properties, 
            t, 
            {
                onMatch: {
                    properties: rel.properties,
                    last_updated: datetime()
                },
                onCreateProperties: {
                    created_at: datetime()
                }
            }
        )
        YIELD rel as created
        RETURN count(created) AS rel_count
        """
        
        try:
            # 执行查询
            result = self.graph.query(query, params={"relationships": new_relationships})
            rel_count = result[0]["rel_count"] if result else 0
            
            # 更新统计
            self.stats["relations_integrated"] += rel_count
            
            self.console.print(f"[green]已集成 {rel_count} 个关系[/green]")
            
            return rel_count
        except Exception as e:
            self.console.print(f"[yellow]集成关系时出错 (可能APOC未安装): {e}[/yellow]")
            
            # 使用备用方法
            integrated = 0
            for rel in new_relationships:
                source_id = rel.get("source_id", "")
                target_id = rel.get("target_id", "")
                rel_type = rel.get("type", "RELATED_TO")
                properties = rel.get("properties", {})
                
                if source_id and target_id:
                    try:
                        backup_query = """
                        MATCH (s:`__Entity__` {id: $source_id})
                        MATCH (t:`__Entity__` {id: $target_id})
                        MERGE (s)-[r:`%s`]->(t)
                        ON CREATE SET r += $properties, r.created_at = datetime()
                        ON MATCH SET r += $properties, r.last_updated = datetime()
                        RETURN count(r) AS created
                        """ % rel_type
                        
                        backup_result = self.graph.query(
                            backup_query, 
                            params={
                                "source_id": source_id, 
                                "target_id": target_id,
                                "properties": properties
                            }
                        )
                        
                        if backup_result and backup_result[0]["created"] > 0:
                            integrated += 1
                    except Exception as e2:
                        self.console.print(f"[red]使用备用方法集成关系时出错: {e2}[/red]")
            
            # 更新统计
            self.stats["relations_integrated"] += integrated
            
            self.console.print(f"[green]使用备用方法集成了 {integrated} 个关系[/green]")
            
            return integrated

    def merge_graph_structures(self, old_graph: Dict[str, Any], new_graph: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并原有图结构与新图结构
        
        Args:
            old_graph: 原有图结构
            new_graph: 新图结构
        
        Returns:
            Dict: 合并后的图结构
        """
        # 节点集合并
        merged_nodes = {**old_graph.get("nodes", {})}
        for node_id, node in new_graph.get("nodes", {}).items():
            if node_id in merged_nodes:
                # 如果节点已存在，根据时间戳决定是否更新
                old_timestamp = merged_nodes[node_id].get("last_updated", 0)
                new_timestamp = node.get("last_updated", time.time())
                
                if new_timestamp > old_timestamp:
                    # 新数据更新，更新节点
                    merged_nodes[node_id] = {**merged_nodes[node_id], **node}
                    merged_nodes[node_id]["last_updated"] = new_timestamp
                    merged_nodes[node_id]["needs_reembedding"] = True
            else:
                # 新节点直接添加
                merged_nodes[node_id] = node
                # 确保新节点有标记需要嵌入
                if "needs_reembedding" not in merged_nodes[node_id]:
                    merged_nodes[node_id]["needs_reembedding"] = True
        
        # 边集合并，避免重复关系
        merged_edges = {}
        
        # 先添加旧图的边
        for edge in old_graph.get("edges", []):
            source = edge.get("source", "")
            target = edge.get("target", "")
            rel_type = edge.get("type", "")
            
            # 创建唯一键
            key = f"{source}_{rel_type}_{target}"
            merged_edges[key] = edge
        
        # 再添加或更新新图的边
        for edge in new_graph.get("edges", []):
            source = edge.get("source", "")
            target = edge.get("target", "")
            rel_type = edge.get("type", "")
            
            # 创建唯一键
            key = f"{source}_{rel_type}_{target}"
            
            if key in merged_edges:
                # 如果关系已存在，根据时间戳决定是否更新
                old_timestamp = merged_edges[key].get("last_updated", 0)
                new_timestamp = edge.get("last_updated", time.time())
                
                if new_timestamp > old_timestamp:
                    # 新数据更新，更新关系
                    merged_edges[key] = {**merged_edges[key], **edge}
                    merged_edges[key]["last_updated"] = new_timestamp
            else:
                # 新关系直接添加
                merged_edges[key] = edge
        
        return {
            "nodes": merged_nodes,
            "edges": list(merged_edges.values())
        }

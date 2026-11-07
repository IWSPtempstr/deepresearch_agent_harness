# 由 integrations/build/incremental_graph_builder.py 拆分（T-014），方法体与原位置逐行一致。
"""图统计、展示与增量更新总入口（原 get_graph_statistics 等 3 个方法）。"""

import time
from typing import Dict, List, Any
from rich.table import Table


class GraphStatsMixin:
    def get_graph_statistics(self) -> Dict[str, Any]:
        """
        获取图谱统计信息
        
        Returns:
            Dict: 统计信息
        """
        # 节点统计
        node_query = """
        MATCH (n)
        RETURN 
            count(n) AS total_nodes,
            sum(CASE WHEN n:`__Document__` THEN 1 ELSE 0 END) AS doc_count,
            sum(CASE WHEN n:`__Chunk__` THEN 1 ELSE 0 END) AS chunk_count,
            sum(CASE WHEN n:`__Entity__` THEN 1 ELSE 0 END) AS entity_count
        """
        
        node_result = self.graph.query(node_query)
        
        # 关系统计
        rel_query = """
        MATCH ()-[r]->()
        RETURN count(r) AS total_relations,
               count(DISTINCT type(r)) AS relation_types
        """
        
        rel_result = self.graph.query(rel_query)
        
        # 嵌入统计
        embedding_query = """
        MATCH (n)
        WHERE n.embedding IS NOT NULL
        RETURN 
            count(n) AS nodes_with_embedding,
            sum(CASE WHEN n:`__Entity__` THEN 1 ELSE 0 END) AS entities_with_embedding,
            sum(CASE WHEN n:`__Chunk__` THEN 1 ELSE 0 END) AS chunks_with_embedding
        """
        
        embedding_result = self.graph.query(embedding_query)
        
        # 合并统计信息
        stats = {
            "total_nodes": node_result[0]["total_nodes"] if node_result else 0,
            "document_count": node_result[0]["doc_count"] if node_result else 0,
            "chunk_count": node_result[0]["chunk_count"] if node_result else 0,
            "entity_count": node_result[0]["entity_count"] if node_result else 0,
            "total_relations": rel_result[0]["total_relations"] if rel_result else 0,
            "relation_types": rel_result[0]["relation_types"] if rel_result else 0,
            "nodes_with_embedding": embedding_result[0]["nodes_with_embedding"] if embedding_result else 0,
            "entities_with_embedding": embedding_result[0]["entities_with_embedding"] if embedding_result else 0,
            "chunks_with_embedding": embedding_result[0]["chunks_with_embedding"] if embedding_result else 0
        }
        
        return stats

    def display_graph_statistics(self):
        """显示图谱统计信息"""
        stats = self.get_graph_statistics()
        
        # 创建统计表格
        stats_table = Table(title="图谱统计信息")
        stats_table.add_column("指标", style="cyan")
        stats_table.add_column("数量", justify="right")
        
        # 添加节点统计
        stats_table.add_row("总节点数", str(stats["total_nodes"]))
        stats_table.add_row("文档节点数", str(stats["document_count"]))
        stats_table.add_row("文本块节点数", str(stats["chunk_count"]))
        stats_table.add_row("实体节点数", str(stats["entity_count"]))
        
        # 添加关系统计
        stats_table.add_row("总关系数", str(stats["total_relations"]))
        stats_table.add_row("关系类型数", str(stats["relation_types"]))
        
        # 添加嵌入统计
        stats_table.add_row("具有嵌入的节点数", str(stats["nodes_with_embedding"]))
        stats_table.add_row("具有嵌入的实体数", str(stats["entities_with_embedding"]))
        stats_table.add_row("具有嵌入的文本块数", str(stats["chunks_with_embedding"]))
        
        # 显示统计表格
        self.console.print(stats_table)

    def process_incremental_update(self) -> Dict[str, Any]:
        """
        执行增量更新流程
        
        Returns:
            Dict: 更新结果统计
        """
        start_time = time.time()
        self.stats["start_time"] = start_time
        
        try:
            # 1. 检测文件变更
            self.console.print("[bold cyan]检测文件变更...[/bold cyan]")
            changes = self.detect_changes()
            
            # 分别处理新增、修改和删除的文件
            added_files = changes.get("added", [])
            modified_files = changes.get("modified", [])
            deleted_files = changes.get("deleted", [])
            
            changed_files = modified_files  # 只有修改的文件需要更新embedding
            self.stats["files_processed"] = len(added_files) + len(modified_files) + len(deleted_files)
            
            if not added_files and not changed_files and not deleted_files:
                self.console.print("[yellow]未检测到文件变更[/yellow]")
                return self.stats
            
            # 2. 处理已删除的文件
            if deleted_files:
                self.console.print("[bold cyan]处理已删除的文件...[/bold cyan]")
                self.process_deleted_files(deleted_files)
            
            # 3. 处理新文件 - 执行完整的处理流程
            if added_files:
                self.console.print("[bold cyan]处理新增文件...[/bold cyan]")
                new_file_results = self.process_new_files(added_files)
                # 更新统计信息
                self.stats["entities_integrated"] += new_file_results.get("entities_extracted", 0)
                self.stats["relations_integrated"] += new_file_results.get("relations_created", 0)
            
            # 4. 更新变更文件的Embedding
            if changed_files:
                self.console.print("[bold cyan]更新变更文件的Embedding...[/bold cyan]")
                embedding_stats = self.update_changed_file_embeddings(changed_files)
                
                # 显示Embedding更新结果
                self.console.print(f"[green]更新的实体Embedding: {embedding_stats['entities']}[/green]")
                self.console.print(f"[green]更新的Chunk Embedding: {embedding_stats['chunks']}[/green]")
            
            # 5. 更新文件注册表
            self.file_manager.update_registry()
            
            # 6. 显示图谱统计信息
            self.console.print("[bold cyan]图谱统计信息[/bold cyan]")
            self.display_graph_statistics()
            
            # 计算结束时间和总时间
            end_time = time.time()
            self.stats["end_time"] = end_time
            self.stats["total_time"] = end_time - start_time
            
            # 显示处理结果
            self.console.print("\n[bold green]增量更新完成![/bold green]")
            self.console.print(f"[green]总耗时: {self.stats['total_time']:.2f}秒[/green]")
            self.console.print(f"[green]处理的文件数: {self.stats['files_processed']}[/green]")
            if added_files:
                self.console.print(f"[green]新增实体数: {self.stats['entities_integrated']}[/green]")
                self.console.print(f"[green]新增关系数: {self.stats['relations_integrated']}[/green]")
            
            return self.stats
            
        except Exception as e:
            self.console.print(f"[red]增量更新过程中出现错误: {e}[/red]")
            
            # 记录结束时间和总时间
            end_time = time.time()
            self.stats["end_time"] = end_time
            self.stats["total_time"] = end_time - start_time
            
            raise

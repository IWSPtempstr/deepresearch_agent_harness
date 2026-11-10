# 由 src/deepresearch_agent/graph/graph_consistency_validator.py 拆分（T-016），方法体与原位置逐行一致。
"""统计与处理入口（原 display_graph_stats/process）。"""

from typing import Dict, List, Any, Tuple
from rich.table import Table


class ConsistencyStatsMixin:
    def display_graph_stats(self):
        """显示图谱统计信息"""
        # 获取图谱节点和关系统计
        stats_query = """
        MATCH (n)
        RETURN 
            count(n) AS total_nodes,
            sum(CASE WHEN n:`__Document__` THEN 1 ELSE 0 END) AS doc_count,
            sum(CASE WHEN n:`__Chunk__` THEN 1 ELSE 0 END) AS chunk_count,
            sum(CASE WHEN n:`__Entity__` THEN 1 ELSE 0 END) AS entity_count
        """
        
        stats_result = self.graph.query(stats_query)
        
        if not stats_result:
            self.console.print("[yellow]无法获取图谱统计信息[/yellow]")
            return
        
        node_stats = stats_result[0]
        
        # 获取关系统计
        rel_query = """
        MATCH ()-[r]->()
        RETURN type(r) AS rel_type, count(r) AS count
        ORDER BY count DESC
        """
        
        rel_result = self.graph.query(rel_query)
        
        # 显示节点统计表
        node_table = Table(title="图谱节点统计")
        node_table.add_column("节点类型", style="cyan")
        node_table.add_column("数量", justify="right")
        
        node_table.add_row("__Document__", str(node_stats["doc_count"]))
        node_table.add_row("__Chunk__", str(node_stats["chunk_count"]))
        node_table.add_row("__Entity__", str(node_stats["entity_count"]))
        node_table.add_row("总计", str(node_stats["total_nodes"]), style="bold")
        
        self.console.print(node_table)
        
        # 显示关系统计表
        if rel_result:
            rel_table = Table(title="图谱关系统计")
            rel_table.add_column("关系类型", style="cyan")
            rel_table.add_column("数量", justify="right")
            
            total_rels = 0
            for rel in rel_result:
                rel_table.add_row(rel["rel_type"], str(rel["count"]))
                total_rels += rel["count"]
                
            rel_table.add_row("总计", str(total_rels), style="bold")
            
            self.console.print(rel_table)

    def process(self, repair: bool = True) -> Dict[str, Any]:
        """
        执行完整的图谱一致性验证和修复流程
        
        Args:
            repair: 是否执行修复操作
            
        Returns:
            Dict: 处理结果统计
        """
        try:
            # 显示图谱基本统计
            self.display_graph_stats()
            
            # 验证图谱一致性
            validation_result = self.validate_graph()
            
            # 如果需要修复并且存在问题，执行修复
            if repair and validation_result["validation_stats"]["total_issues"] > 0:
                repair_result = self.repair_graph()
                return {
                    "validation_result": validation_result,
                    "repair_result": repair_result,
                    "total_time": self.validation_time + self.repair_time
                }
            
            return {
                "validation_result": validation_result,
                "total_time": self.validation_time
            }
            
        except Exception as e:
            self.console.print(f"[red]图谱一致性验证过程中出现错误: {e}[/red]")
            raise

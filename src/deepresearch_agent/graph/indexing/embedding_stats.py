# 由 src/deepresearch_agent/graph/indexing/embedding_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""统计与处理入口（原 display_stats/process）。"""

import time
from typing import Dict, Any


class EmbeddingStatsMixin:
    def display_stats(self):
        """显示统计信息"""
        self.console.print("\n[bold cyan]Embedding更新统计[/bold cyan]")
        self.console.print(f"[blue]实体更新: {self.stats['entity_updates']} 个[/blue]")
        self.console.print(f"[blue]Chunk更新: {self.stats['chunk_updates']} 个[/blue]")
        self.console.print(f"[blue]总更新: {self.stats['total_updates']} 个[/blue]")
        self.console.print(f"[blue]错误: {self.stats['errors']} 个[/blue]")
        
        self.console.print(f"[blue]总耗时: {self.total_time:.2f}秒，"
                          f"其中: 嵌入计算: {self.embedding_time:.2f}秒 ({self.embedding_time/self.total_time*100:.1f}%)，"
                          f"数据库操作: {self.db_time:.2f}秒 ({self.db_time/self.total_time*100:.1f}%)[/blue]")

    def process(self, entity_limit: int = 500, chunk_limit: int = 500) -> Dict[str, Any]:
        """
        执行完整的Embedding更新流程
        
        Args:
            entity_limit: 处理的最大实体数量
            chunk_limit: 处理的最大Chunk数量
            
        Returns:
            Dict: 处理结果统计
        """
        start_time = time.time()
        
        try:
            # 设置Embedding追踪
            self.setup_embedding_tracking()
            
            # 更新实体Embedding
            entity_count = self.update_entity_embeddings(limit=entity_limit)
            
            # 更新Chunk Embedding
            chunk_count = self.update_chunk_embeddings(limit=chunk_limit)
            
            # 显示统计信息
            self.display_stats()
            
            # 计算总时间
            self.total_time = time.time() - start_time
            
            return {
                "entity_updates": entity_count,
                "chunk_updates": chunk_count,
                "total_updates": entity_count + chunk_count,
                "total_time": self.total_time,
                "embedding_time": self.embedding_time,
                "db_time": self.db_time
            }
            
        except Exception as e:
            self.console.print(f"[red]Embedding更新过程中出现错误: {e}[/red]")
            raise

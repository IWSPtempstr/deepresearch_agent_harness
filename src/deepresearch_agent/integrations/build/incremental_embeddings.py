# 由 src/deepresearch_agent/integrations/build/incremental_update.py 拆分（T-016），方法体与原位置逐行一致。
"""实体/Chunk 嵌入更新（原 update_entity_embeddings/update_chunk_embeddings）。"""



class UpdateEmbeddingsMixin:
    def update_entity_embeddings(self):
        """
        更新实体Embedding
        
        Returns:
            int: 更新的实体数量
        """
        self.console.print("[bold cyan]更新实体Embedding...[/bold cyan]")
        
        try:
            # 获取需要更新的实体
            entities = self.embedding_manager.get_entities_needing_update()
            
            if entities:
                self.console.print(f"[green]发现 {len(entities)} 个需要更新Embedding的实体[/green]")
                
                # 执行更新
                updated_count = self.embedding_manager.update_entity_embeddings()
                
                # 更新统计信息
                self.stats["entities_updated"] += updated_count
                
                return updated_count
            else:
                self.console.print("[yellow]没有需要更新Embedding的实体[/yellow]")
                return 0
                
        except Exception as e:
            self.console.print(f"[red]更新实体Embedding时出错: {e}[/red]")
            self.stats["errors"] += 1
            return 0

    def update_chunk_embeddings(self):
        """
        更新Chunk Embedding
        
        Returns:
            int: 更新的Chunk数量
        """
        self.console.print("[bold cyan]更新Chunk Embedding...[/bold cyan]")
        
        try:
            # 获取需要更新的Chunk
            chunks = self.embedding_manager.get_chunks_needing_update()
            
            if chunks:
                self.console.print(f"[green]发现 {len(chunks)} 个需要更新Embedding的Chunk[/green]")
                
                # 执行更新
                updated_count = self.embedding_manager.update_chunk_embeddings()
                
                return updated_count
            else:
                self.console.print("[yellow]没有需要更新Embedding的Chunk[/yellow]")
                return 0
                
        except Exception as e:
            self.console.print(f"[red]更新Chunk Embedding时出错: {e}[/red]")
            self.stats["errors"] += 1
            return 0

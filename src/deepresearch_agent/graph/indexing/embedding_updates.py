# 由 src/deepresearch_agent/graph/indexing/embedding_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""实体/Chunk 嵌入计算与更新（原 update_*_embeddings/_compute_embeddings_batch）。"""

import time
import concurrent.futures
from typing import List, Optional


class EmbeddingUpdateMixin:
    def update_entity_embeddings(self, entity_ids: Optional[List[str]] = None) -> int:
        """
        更新实体Embedding
        
        Args:
            entity_ids: 要更新的实体ID列表，如果为None则自动检测
            
        Returns:
            int: 更新的实体数量
        """
        start_time = time.time()
        
        # 获取需要更新的实体
        if entity_ids:
            # 如果提供了特定的实体ID列表
            id_list = ", ".join([f"'{eid}'" for eid in entity_ids])
            query = f"""
            MATCH (e:`__Entity__`)
            WHERE e.id IN [{id_list}]
            RETURN elementId(e) AS neo4j_id,
                   e.id AS entity_id, 
                   CASE WHEN e.description IS NOT NULL THEN e.description ELSE e.id END AS text
            """
            entities = self.graph.query(query)
        else:
            # 自动检测需要更新的实体
            entities = self.get_entities_needing_update(limit=self.batch_size * 5)
        
        if not entities:
            self.console.print("[yellow]没有需要更新Embedding的实体[/yellow]")
            return 0
        
        self.console.print(f"[cyan]开始更新 {len(entities)} 个实体的Embedding...[/cyan]")
        
        # 批量处理实体
        updated_count = 0
        for i in range(0, len(entities), self.batch_size):
            batch = entities[i:i+self.batch_size]
            
            # 提取文本和ID
            texts = [entity["text"] for entity in batch]
            entity_ids = [entity["entity_id"] for entity in batch]
            neo4j_ids = [entity["neo4j_id"] for entity in batch]
            
            # 计算Embedding
            embedding_start = time.time()
            try:
                embeddings = self._compute_embeddings_batch(texts)
                self.embedding_time += time.time() - embedding_start
                
                # 准备更新数据
                updates = []
                for j, entity_id in enumerate(entity_ids):
                    if j < len(embeddings) and embeddings[j] is not None:
                        updates.append({
                            "neo4j_id": neo4j_ids[j],
                            "embedding": embeddings[j]
                        })
                
                # 更新数据库
                db_start = time.time()
                if updates:
                    query = """
                    UNWIND $updates AS update
                    MATCH (e) WHERE elementId(e) = update.neo4j_id
                    SET e.embedding = update.embedding,
                        e.last_embedded = datetime(),
                        e.needs_reembedding = false
                    RETURN count(e) AS updated
                    """
                    
                    result = self.graph.query(query, params={"updates": updates})
                    batch_updated = result[0]["updated"] if result else 0
                    updated_count += batch_updated
                    
                self.db_time += time.time() - db_start
                
                self.console.print(f"[green]批次 {i//self.batch_size + 1} 更新完成，"
                                  f"处理了 {len(batch)} 个实体，"
                                  f"成功更新 {batch_updated} 个[/green]")
                
            except Exception as e:
                self.console.print(f"[red]更新实体Embedding时出错: {e}[/red]")
                self.stats["errors"] += 1
        
        # 更新统计
        self.stats["entity_updates"] += updated_count
        self.stats["total_updates"] += updated_count
        
        # 计算总时间
        self.total_time += time.time() - start_time
        
        self.console.print(f"[blue]实体Embedding更新完成，共更新 {updated_count} 个实体，"
                          f"耗时: {time.time() - start_time:.2f}秒[/blue]")
        
        return updated_count

    def update_chunk_embeddings(self, chunk_ids: Optional[List[str]] = None) -> int:
        """
        更新Chunk Embedding
        
        Args:
            chunk_ids: 要更新的Chunk ID列表，如果为None则自动检测
            
        Returns:
            int: 更新的Chunk数量
        """
        start_time = time.time()
        
        # 获取需要更新的Chunk
        if chunk_ids:
            # 如果提供了特定的Chunk ID列表
            id_list = ", ".join([f"'{cid}'" for cid in chunk_ids])
            query = f"""
            MATCH (c:`__Chunk__`)
            WHERE c.id IN [{id_list}]
            RETURN elementId(c) AS neo4j_id,
                   c.id AS chunk_id, 
                   c.text AS text
            """
            chunks = self.graph.query(query)
        else:
            # 自动检测需要更新的Chunk
            chunks = self.get_chunks_needing_update(limit=self.batch_size * 5)
        
        if not chunks:
            self.console.print("[yellow]没有需要更新Embedding的Chunk[/yellow]")
            return 0
        
        self.console.print(f"[cyan]开始更新 {len(chunks)} 个Chunk的Embedding...[/cyan]")
        
        # 批量处理Chunk
        updated_count = 0
        for i in range(0, len(chunks), self.batch_size):
            batch = chunks[i:i+self.batch_size]
            
            # 提取文本和ID
            texts = [chunk["text"] for chunk in batch]
            chunk_ids = [chunk["chunk_id"] for chunk in batch]
            neo4j_ids = [chunk["neo4j_id"] for chunk in batch]
            
            # 计算Embedding
            embedding_start = time.time()
            try:
                embeddings = self._compute_embeddings_batch(texts)
                self.embedding_time += time.time() - embedding_start
                
                # 准备更新数据
                updates = []
                for j, chunk_id in enumerate(chunk_ids):
                    if j < len(embeddings) and embeddings[j] is not None:
                        updates.append({
                            "neo4j_id": neo4j_ids[j],
                            "embedding": embeddings[j]
                        })
                
                # 更新数据库
                db_start = time.time()
                if updates:
                    query = """
                    UNWIND $updates AS update
                    MATCH (c) WHERE elementId(c) = update.neo4j_id
                    SET c.embedding = update.embedding,
                        c.last_embedded = datetime(),
                        c.needs_reembedding = false
                    RETURN count(c) AS updated
                    """
                    
                    result = self.graph.query(query, params={"updates": updates})
                    batch_updated = result[0]["updated"] if result else 0
                    updated_count += batch_updated
                    
                self.db_time += time.time() - db_start
                
                self.console.print(f"[green]批次 {i//self.batch_size + 1} 更新完成，"
                                  f"处理了 {len(batch)} 个Chunk，"
                                  f"成功更新 {batch_updated} 个[/green]")
                
            except Exception as e:
                self.console.print(f"[red]更新Chunk Embedding时出错: {e}[/red]")
                self.stats["errors"] += 1
        
        # 更新统计
        self.stats["chunk_updates"] += updated_count
        self.stats["total_updates"] += updated_count
        
        # 计算总时间
        self.total_time += time.time() - start_time
        
        self.console.print(f"[blue]Chunk Embedding更新完成，共更新 {updated_count} 个Chunk，"
                          f"耗时: {time.time() - start_time:.2f}秒[/blue]")
        
        return updated_count

    def _compute_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        计算一批文本的embedding向量
        
        Args:
            texts: 文本列表
            
        Returns:
            List[List[float]]: embedding向量列表
        """
        embeddings = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 预创建嵌入任务
            embedding_tasks = []
            for text in texts:
                # 添加强健性处理，确保文本不为空
                safe_text = text if text and text.strip() else "empty content"
                embedding_tasks.append(safe_text)
            
            # 分析批处理的最佳大小
            embed_batch_size = min(32, len(embedding_tasks))
            
            # 批量执行嵌入任务
            for i in range(0, len(embedding_tasks), embed_batch_size):
                sub_batch = embedding_tasks[i:i+embed_batch_size]
                try:
                    # 尝试使用批量嵌入方法
                    if hasattr(self.embeddings_model, 'embed_documents'):
                        sub_batch_embeddings = self.embeddings_model.embed_documents(sub_batch)
                        embeddings.extend(sub_batch_embeddings)
                    else:
                        # 回退到单个嵌入
                        futures = [executor.submit(self.embeddings_model.embed_query, text) for text in sub_batch]
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                embeddings.append(future.result())
                            except Exception as e:
                                self.console.print(f"[yellow]嵌入计算失败: {e}[/yellow]")
                                # 添加零向量作为备用
                                if hasattr(self.embeddings_model, 'embedding_size'):
                                    embeddings.append([0.0] * self.embeddings_model.embedding_size)
                                else:
                                    # 假设使用通用嵌入大小
                                    embeddings.append([0.0] * 1536)
                except Exception as e:
                    self.console.print(f"[yellow]批量嵌入处理失败: {e}[/yellow]")
                    # 尝试单个嵌入作为回退
                    for text in sub_batch:
                        try:
                            embeddings.append(self.embeddings_model.embed_query(text))
                        except Exception as e2:
                            self.console.print(f"[yellow]单个嵌入计算失败: {e2}[/yellow]")
                            # 添加零向量作为备用
                            if hasattr(self.embeddings_model, 'embedding_size'):
                                embeddings.append([0.0] * self.embeddings_model.embedding_size)
                            else:
                                embeddings.append([0.0] * 1536)
        
        return embeddings

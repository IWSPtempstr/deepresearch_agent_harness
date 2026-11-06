# 由 integrations/build/incremental_graph_builder.py 拆分（T-014），方法体与原位置逐行一致。
"""文件变更检测与新增/删除文件处理（原 detect_changes 等 3 个方法）。"""

from typing import Dict, List, Any
from pathlib import Path
import os
import tempfile
import shutil


class GraphFileChangesMixin:
    def detect_changes(self) -> Dict[str, List[str]]:
        """
        检测文件变更
        
        Returns:
            Dict: 文件变更信息
        """
        return self.file_manager.detect_changes()

    def process_new_files(self, added_files: List[str]) -> Dict[str, Any]:
        """
        对新文件执行完整的处理流程（分块、实体抽取、关系创建）
        
        Args:
            added_files: 新增文件路径列表
            
        Returns:
            Dict: 处理结果统计
        """
        if not added_files:
            return {"files_processed": 0, "entities_extracted": 0, "relations_created": 0}
        
        results = {
            "files_processed": 0,
            "entities_extracted": 0,
            "relations_created": 0
        }
        
        self.console.print(f"[bold cyan]正在处理 {len(added_files)} 个新文件...[/bold cyan]")
        
        # 打印文件路径以便调试
        for file_path in added_files:
            self.console.print(f"[blue]处理文件路径: {file_path}[/blue]")
            if not os.path.exists(file_path):
                self.console.print(f"[red]警告: 文件不存在: {file_path}[/red]")
        
        # 使用临时目录
        # 1. 创建临时目录并复制新文件
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # 复制文件到临时目录
                copy_success = False
                for file_path in added_files:
                    try:
                        if os.path.exists(file_path):
                            file_name = os.path.basename(file_path)
                            dest_path = os.path.join(temp_dir, file_name)
                            shutil.copy2(file_path, dest_path)
                            self.console.print(f"[green]已复制 {file_path} 到临时目录[/green]")
                            copy_success = True
                        elif os.path.exists(os.path.join(self.files_dir, file_path)):
                            # 尝试将文件路径视为相对于files_dir的路径
                            full_path = os.path.join(self.files_dir, file_path)
                            file_name = os.path.basename(file_path)
                            dest_path = os.path.join(temp_dir, file_name)
                            shutil.copy2(full_path, dest_path)
                            self.console.print(f"[green]已复制 {full_path} 到临时目录[/green]")
                            copy_success = True
                        else:
                            # 最后尝试直接使用文件名
                            file_name = os.path.basename(file_path)
                            full_path = os.path.join(self.files_dir, file_name)
                            if os.path.exists(full_path):
                                dest_path = os.path.join(temp_dir, file_name)
                                shutil.copy2(full_path, dest_path)
                                self.console.print(f"[green]已复制 {full_path} 到临时目录[/green]")
                                copy_success = True
                            else:
                                self.console.print(f"[red]复制文件失败，文件不存在: {file_path}[/red]")
                    except Exception as e:
                        self.console.print(f"[red]复制文件 {file_path} 到临时目录时出错: {e}[/red]")
                
                if not copy_success:
                    self.console.print("[red]没有成功复制任何文件到临时目录，无法继续处理[/red]")
                    return results
                
                # 2. 保存原始目录并临时修改
                original_dir = self.document_processor.directory_path
                self.document_processor.directory_path = temp_dir
                self.document_processor.file_reader.directory_path = temp_dir
                
                # 3. 处理临时目录中的文件
                processed_documents = self.document_processor.process_directory()
                
                # 4. 恢复原始目录
                self.document_processor.directory_path = original_dir
                self.document_processor.file_reader.directory_path = original_dir
                
                # 记录处理的文件数
                if processed_documents:
                    results["files_processed"] = len(processed_documents)
                    self.console.print(f"[green]成功处理 {len(processed_documents)} 个文件[/green]")
                    
                    # 5. 构建图谱结构
                    for doc in processed_documents:
                        if "chunks" in doc and doc["chunks"]:
                            # 创建文档节点
                            self.console.print(f"[blue]为文件 {doc['filename']} 创建文档节点[/blue]")
                            self.struct_builder.create_document(
                                type="local",
                                uri=str(self.files_dir),
                                file_name=doc["filename"],
                                domain="document"
                            )
                            
                            # 创建chunk节点和关系
                            chunks_count = len(doc['chunks']) if doc['chunks'] else 0
                            self.console.print(f"[blue]为文件 {doc['filename']} 创建 {chunks_count} 个文本块节点[/blue]")
                            doc["graph_result"] = self.struct_builder.create_relation_between_chunks(
                                doc["filename"],
                                doc["chunks"]
                            )
                    
                    # 6. 准备实体抽取的数据
                    file_contents_format = []
                    for doc in processed_documents:
                        if "chunks" in doc and doc["chunks"]:
                            file_contents_format.append([
                                doc["filename"], 
                                doc["content"], 
                                doc["chunks"]
                            ])
                    
                    # 7. 抽取实体和关系
                    if file_contents_format:
                        self.console.print(f"[cyan]开始抽取实体和关系，文件数: {len(file_contents_format)}[/cyan]")
                        
                        total_chunks = sum(len(content[2]) for content in file_contents_format)
                        self.console.print(f"[blue]总计 {total_chunks} 个文本块需要处理[/blue]")
                        
                        processed_chunk_count = 0
                        def progress_callback(i):
                            nonlocal processed_chunk_count
                            processed_chunk_count += 1
                            if processed_chunk_count % 5 == 0 or processed_chunk_count == total_chunks:
                                self.console.print(f"[blue]已处理 {processed_chunk_count}/{total_chunks} 个文本块[/blue]")
                        
                        # 确保禁用缓存以处理新文件
                        original_cache_setting = getattr(self.entity_extractor, 'enable_cache', True)
                        self.entity_extractor.enable_cache = False
                        
                        try:
                            processed_contents = self.entity_extractor.process_chunks(
                                file_contents_format, 
                                progress_callback=progress_callback
                            )
                            
                            # 恢复缓存设置
                            self.entity_extractor.enable_cache = original_cache_setting
                            
                            # 输出处理结果
                            if processed_contents:
                                self.console.print(f"[green]实体抽取完成，已处理 {len(processed_contents)} 个文件[/green]")
                            
                                # 打印调试信息
                                for i, content in enumerate(processed_contents):
                                    if len(content) > 3:
                                        entity_data = content[3]
                                        entity_count = sum(1 for data in entity_data if '("entity"' in str(data))
                                        relation_count = sum(1 for data in entity_data if '("relationship"' in str(data))
                                        self.console.print(f"[blue]文件 {i+1}: {content[0]}, 抽取了 {entity_count} 个实体和 {relation_count} 个关系[/blue]")
                                    else:
                                        self.console.print(f"[yellow]文件 {i+1}: {content[0]}, 没有返回实体数据[/yellow]")
                            
                                # 8. 处理结果并写入图数据库
                                graph_writer_data = []
                                for doc in processed_documents:
                                    if "chunks" in doc and doc["chunks"] and "graph_result" in doc:
                                        # 查找对应的处理结果
                                        entity_data = None
                                        for content in processed_contents:
                                            if content[0] == doc["filename"] and len(content) > 3:
                                                entity_data = content[3]
                                                break
                                        
                                        if entity_data:
                                            # 估算实体和关系数量
                                            entity_count = sum(1 for data in entity_data if '("entity"' in str(data))
                                            relation_count = sum(1 for data in entity_data if '("relationship"' in str(data))
                                            self.console.print(f"[green]文件 {doc['filename']} 中识别出 {entity_count} 个实体和 {relation_count} 个关系[/green]")
                                            
                                            # 添加到写入数据
                                            graph_writer_data.append([
                                                doc["filename"],
                                                doc["content"],
                                                doc["chunks"],
                                                doc["graph_result"],
                                                entity_data
                                            ])
                                            
                                            # 更新统计
                                            results["entities_extracted"] += entity_count
                                            results["relations_created"] += relation_count
                                
                                # 9. 写入图数据库
                                if graph_writer_data:
                                    self.console.print(f"[cyan]开始写入 {len(graph_writer_data)} 个文件的图数据...[/cyan]")
                                    self.graph_writer.process_and_write_graph_documents(graph_writer_data)
                                    self.console.print(f"[green]图数据写入完成[/green]")
                                else:
                                    self.console.print("[yellow]没有有效的图数据需要写入[/yellow]")
                            else:
                                self.console.print("[yellow]实体抽取过程没有返回有效结果[/yellow]")
                        
                        except Exception as e:
                            self.console.print(f"[red]实体抽取过程中出错: {e}[/red]")
                            import traceback
                            self.console.print(f"[red]{traceback.format_exc()}[/red]")
                    else:
                        self.console.print("[yellow]没有找到可用于抽取实体的文本块[/yellow]")
                else:
                    self.console.print("[yellow]没有处理到任何文件[/yellow]")
            
            except Exception as e:
                self.console.print(f"[red]处理新文件时发生错误: {e}[/red]")
                import traceback
                self.console.print(f"[red]{traceback.format_exc()}[/red]")
        
        self.console.print(f"[green]已完成处理 {results['files_processed']} 个新文件[/green]")
        if results["entities_extracted"] > 0 or results["relations_created"] > 0:
            self.console.print(f"[green]抽取了 {results['entities_extracted']} 个实体和 {results['relations_created']} 个关系[/green]")
        
        return results

    def process_deleted_files(self, deleted_files: List[str]) -> int:
        """
        处理已删除的文件
        
        Args:
            deleted_files: 删除的文件列表
            
        Returns:
            int: 删除的节点数量
        """
        if not deleted_files:
            return 0
            
        self.console.print(f"[cyan]处理 {len(deleted_files)} 个已删除的文件...[/cyan]")
        
        deleted_count = 0
        for file_path in deleted_files:
            file_name = Path(file_path).name
            
            # 查找与文件关联的所有Chunk节点
            chunk_query = """
            MATCH (d:`__Document__` {fileName: $fileName})<-[:PART_OF]-(c:`__Chunk__`)
            RETURN collect(c.id) AS chunk_ids, count(c) AS chunk_count
            """
            
            chunk_result = self.graph.query(chunk_query, params={"fileName": file_name})
            
            if not chunk_result or not chunk_result[0]["chunk_ids"]:
                self.console.print(f"[yellow]文件 {file_name} 没有相关的数据需要删除[/yellow]")
                continue
                
            chunk_ids = chunk_result[0]["chunk_ids"]
            chunk_count = chunk_result[0]["chunk_count"]
            
            # 查找这些Chunk关联的实体，但排除被其他Chunk引用的实体
            entity_query = """
            MATCH (c:`__Chunk__`)-[:MENTIONS]->(e:`__Entity__`)
            WHERE c.id IN $chunk_ids
            WITH e, count(c) AS references
            MATCH (chunk:`__Chunk__`)-[:MENTIONS]->(e)
            WITH e, references, count(chunk) AS total_references
            WHERE references = total_references 
              AND NOT e.manual_edit = true  // 排除手动编辑的实体
              AND NOT e.protected = true    // 排除受保护的实体
            RETURN collect(e.id) AS entity_ids, count(e) AS entity_count
            """
            
            entity_result = self.graph.query(entity_query, params={"chunk_ids": chunk_ids})
            
            entity_ids = []
            entity_count = 0
            if entity_result and entity_result[0]["entity_ids"]:
                entity_ids = entity_result[0]["entity_ids"]
                entity_count = entity_result[0]["entity_count"]
            
            # 删除与文件关联的所有数据
            delete_query = """
            // 删除文档节点和关系
            MATCH (d:`__Document__` {fileName: $fileName})
            OPTIONAL MATCH (d)-[r]-()
            DELETE r
            
            // 删除Chunk节点和关系
            WITH d
            MATCH (c:`__Chunk__`)-[r1:PART_OF]->(d)
            OPTIONAL MATCH (c)-[r2]-()
            WHERE NOT type(r2) = 'PART_OF'
            DELETE r2
            
            // 删除孤立的实体节点
            WITH d, collect(c.id) as chunk_ids
            UNWIND $entity_ids AS entity_id
            MATCH (e:`__Entity__` {id: entity_id})
            WHERE NOT e.manual_edit = true AND NOT e.protected = true // 再次检查保护
            DELETE e
            
            // 删除Chunk节点
            WITH d, chunk_ids
            MATCH (c:`__Chunk__`)
            WHERE c.id IN chunk_ids
            DELETE c
            
            // 最后删除文档节点
            DELETE d
            
            RETURN count(d) AS deleted_docs
            """
            
            delete_result = self.graph.query(delete_query, params={
                "fileName": file_name, 
                "entity_ids": entity_ids
            })
            
            deleted_docs = delete_result[0]["deleted_docs"] if delete_result else 0
            file_deleted_count = chunk_count + entity_count + deleted_docs
            deleted_count += file_deleted_count
            
            self.console.print(f"[blue]已删除文件 {file_name} 相关数据: "
                              f"{chunk_count} 个Chunk节点, "
                              f"{entity_count} 个实体节点, "
                              f"{deleted_docs} 个文档节点[/blue]")
        
        self.console.print(f"[green]已完成删除文件处理，共删除 {deleted_count} 个节点[/green]")
        return deleted_count

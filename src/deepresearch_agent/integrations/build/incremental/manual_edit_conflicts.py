# 由 src/deepresearch_agent/integrations/build/incremental/manual_edit_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""编辑保护与冲突解决（原 preserve_manual_edits/resolve_conflicts）。"""

import time
from typing import List
from deepresearch_agent.config.settings import conflict_strategy


class ManualEditConflictMixin:
    def preserve_manual_edits(self, changed_files: List[str]) -> int:
        """
        确保增量更新不会覆盖手动编辑
        
        Args:
            changed_files: 变更的文件列表
            
        Returns:
            int: 保留的手动编辑数量
        """
        start_time = time.time()
        
        # 1. 标记与变更文件相关的手动编辑节点
        preserve_query = """
        MATCH (d:`__Document__`)<-[:PART_OF]-(c:`__Chunk__`)-[:MENTIONS]->(e:`__Entity__`)
        WHERE d.fileName IN $changed_files
          AND (e.manual_edit = true OR e.created_by IS NOT NULL OR e.edited_by IS NOT NULL)
        SET e.preserve_edit = true
        RETURN count(e) AS preserved_count
        """
        
        try:
            result = self.graph.query(preserve_query, params={"changed_files": changed_files})
            preserved_count = result[0]["preserved_count"] if result else 0
        except Exception as e:
            self.console.print(f"[yellow]标记保留手动编辑时出错: {e}[/yellow]")
            preserved_count = 0
        
        # 2. 创建保护规则，防止删除手动编辑的节点
        protection_query = """
        MATCH (e:`__Entity__`) 
        WHERE e.preserve_edit = true
        SET e.protected = true
        RETURN count(e) AS protected_count
        """
        
        try:
            protection_result = self.graph.query(protection_query)
            protected_count = protection_result[0]["protected_count"] if protection_result else 0
        except Exception as e:
            self.console.print(f"[yellow]创建保护规则时出错: {e}[/yellow]")
            protected_count = 0
        
        # 更新统计信息
        self.edit_stats["preserved_edits"] = preserved_count
        
        # 计算同步时间
        self.sync_time = time.time() - start_time
        
        self.console.print(f"[blue]手动编辑保护完成，耗时: {self.sync_time:.2f}秒[/blue]")
        self.console.print(f"[blue]已保护 {preserved_count} 个手动编辑，"
                          f"{protected_count} 个节点被标记为受保护[/blue]")
        
        return preserved_count

    def resolve_conflicts(self, conflict_strategy: str = conflict_strategy) -> int:
        """
        解决自动更新和手动编辑之间的冲突
        
        Args:
            conflict_strategy: 冲突解决策略，可以是 "manual_first"（优先保留手动编辑），
                            "auto_first"（优先自动更新）或 "merge"（尝试合并）
            
        Returns:
            int: 解决的冲突数量
        """
        start_time = time.time()
        
        # 检查属性是否存在
        try:
            props_result = self.graph.query("""
            CALL db.propertyKeys() YIELD propertyKey
            RETURN collect(propertyKey) AS all_props
            """)
            
            all_props = props_result[0]["all_props"] if props_result and props_result[0]["all_props"] else []
            
            # 构建动态查询条件
            where_clauses = []
            if "manual_edit" in all_props:
                where_clauses.append("e.manual_edit = true")
            if "edited_by" in all_props:
                where_clauses.append("e.edited_by IS NOT NULL")
                
            # 系统生成条件
            system_cond = "e.system_generated = true" if "system_generated" in all_props else "true"
            
            # 如果没有可用条件，使用一个始终为假的条件
            if not where_clauses:
                where_clauses.append("false")
            
            # 查找可能的冲突节点
            conflict_query = f"""
            MATCH (e:`__Entity__`)
            WHERE ({" OR ".join(where_clauses)})
            AND {system_cond}
            RETURN e.id AS entity_id, e.type AS entity_type
            """
            
            conflicts = self.graph.query(conflict_query)
        except Exception as e:
            self.console.print(f"[yellow]查找冲突节点时出错: {e}[/yellow]")
            conflicts = []
        
        resolved_count = 0
        
        # 基于策略解决冲突
        if conflicts:
            for conflict in conflicts:
                entity_id = conflict["entity_id"]
                
                if conflict_strategy == "manual_first":
                    # 保留手动编辑，移除自动生成标记
                    resolution_query = """
                    MATCH (e:`__Entity__` {id: $entity_id})
                    SET e.conflict_resolved = true,
                        e.conflict_resolution = 'manual_preferred'
                    """
                    
                    # 如果有system_generated属性，添加设置
                    if "system_generated" in all_props:
                        resolution_query += ",\n    e.system_generated = false"
                    
                    resolution_query += "\nRETURN e.id"
                
                elif conflict_strategy == "auto_first":
                    # 优先自动更新，移除手动编辑标记
                    resolution_query = """
                    MATCH (e:`__Entity__` {id: $entity_id})
                    SET e.conflict_resolved = true,
                        e.conflict_resolution = 'auto_preferred'
                    """
                    
                    # 如果有manual_edit属性，添加设置
                    if "manual_edit" in all_props:
                        resolution_query += ",\n    e.manual_edit = false"
                    
                    # 如果有edited_by属性，添加设置
                    if "edited_by" in all_props:
                        resolution_query += ",\n    e.edited_by = null"
                    
                    resolution_query += "\nRETURN e.id"
                
                else:  # "merge" 策略
                    # 尝试合并两种编辑
                    resolution_query = """
                    MATCH (e:`__Entity__` {id: $entity_id})
                    SET e.conflict_resolved = true,
                        e.conflict_resolution = 'merged',
                        e.merged_at = datetime()
                    RETURN e.id
                    """
                
                try:
                    result = self.graph.query(resolution_query, params={"entity_id": entity_id})
                    if result and result[0]:
                        resolved_count += 1
                except Exception as e:
                    self.console.print(f"[red]解决实体 {entity_id} 的冲突时出错: {e}[/red]")
        
        # 更新统计信息
        self.edit_stats["conflicts_resolved"] = resolved_count
        
        resolution_time = time.time() - start_time
        
        self.console.print(f"[blue]冲突解决完成，耗时: {resolution_time:.2f}秒[/blue]")
        self.console.print(f"[blue]已解决 {resolved_count} 个冲突，使用策略: {conflict_strategy}[/blue]")
        
        return resolved_count

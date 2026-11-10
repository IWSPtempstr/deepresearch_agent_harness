# 由 src/deepresearch_agent/integrations/build/incremental/manual_edit_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""手动编辑检测与标记（原 detect_manual_edits/mark_manual_edit）。"""

import time
from datetime import datetime
from typing import Dict, List, Any


class ManualEditDetectMixin:
    def detect_manual_edits(self) -> Dict[str, int]:
        """
        检测数据库中的手动编辑
        
        Returns:
            Dict: 手动编辑的统计
        """
        start_time = time.time()
        
        # 检查属性是否存在
        try:
            props_result = self.graph.query("""
            CALL db.propertyKeys() YIELD propertyKey
            RETURN collect(propertyKey) AS all_props
            """)
            
            all_props = props_result[0]["all_props"] if props_result and props_result[0]["all_props"] else []
            
            # 1. 检测手动创建的实体节点 - 构建动态查询
            entity_clauses = []
            if "manual_edit" in all_props:
                entity_clauses.append("e.manual_edit = true")
            if "created_by" in all_props:
                entity_clauses.append("e.created_by IS NOT NULL")
            if "edited_by" in all_props:
                entity_clauses.append("e.edited_by IS NOT NULL")
                
            # 如果没有可用条件，使用一个始终为假的条件
            if not entity_clauses:
                entity_clauses.append("false")
                
            manual_entity_query = f"""
            MATCH (e:`__Entity__`)
            WHERE {" OR ".join(entity_clauses)}
            RETURN count(e) AS manual_entities
            """
            
            entity_result = self.graph.query(manual_entity_query)
            manual_entities = entity_result[0]["manual_entities"] if entity_result else 0
            
            # 2. 检测手动创建的关系 - 构建动态查询
            rel_clauses = []
            if "manual_edit" in all_props:
                rel_clauses.append("r.manual_edit = true")
            if "created_by" in all_props:
                rel_clauses.append("r.created_by IS NOT NULL")
            if "edited_by" in all_props:
                rel_clauses.append("r.edited_by IS NOT NULL")
                
            # 如果没有可用条件，使用一个始终为假的条件
            if not rel_clauses:
                rel_clauses.append("false")
                
            manual_relation_query = f"""
            MATCH ()-[r]->()
            WHERE {" OR ".join(rel_clauses)}
            RETURN count(r) AS manual_relations
            """
            
            relation_result = self.graph.query(manual_relation_query)
            manual_relations = relation_result[0]["manual_relations"] if relation_result else 0
            
            # 3. 检测通过时间戳识别的可能手动编辑
            timestamp_entities = 0
            if "created_at" in all_props and "system_generated" in all_props:
                timestamp_query = """
                MATCH (e:`__Entity__`) 
                WHERE e.created_at IS NOT NULL 
                AND e.system_generated = false
                RETURN count(e) AS timestamp_entities
                """
                
                try:
                    timestamp_result = self.graph.query(timestamp_query)
                    timestamp_entities = timestamp_result[0]["timestamp_entities"] if timestamp_result else 0
                except:
                    timestamp_entities = 0
            
        except Exception as e:
            self.console.print(f"[yellow]检测手动编辑时出错: {e}[/yellow]")
            manual_entities = 0
            manual_relations = 0
            timestamp_entities = 0
        
        # 更新统计
        self.edit_stats["manual_entities"] = manual_entities
        self.edit_stats["manual_relations"] = manual_relations
        
        # 计算检测时间
        self.detection_time = time.time() - start_time
        
        # 显示检测结果
        self.console.print(f"[blue]手动编辑检测完成，耗时: {self.detection_time:.2f}秒[/blue]")
        self.console.print(f"[blue]检测到 {manual_entities} 个手动编辑的实体节点，"
                        f"{manual_relations} 个手动编辑的关系[/blue]")
        
        return {
            "manual_entities": manual_entities,
            "manual_relations": manual_relations,
            "timestamp_entities": timestamp_entities
        }

    def mark_manual_edit(self, entity_id: str, edit_info: Dict[str, Any]) -> bool:
        """
        标记实体为手动编辑
        
        Args:
            entity_id: 实体ID
            edit_info: 编辑信息
            
        Returns:
            bool: 标记是否成功
        """
        # 准备编辑信息
        params = {
            "entity_id": entity_id,
            "edited_by": edit_info.get("edited_by", "manual"),
            "edit_time": edit_info.get("edit_time", datetime.now().isoformat()),
            "edit_comment": edit_info.get("edit_comment", ""),
            "manual_edit": True
        }
        
        # 执行标记查询
        query = """
        MATCH (e:`__Entity__` {id: $entity_id})
        SET e.manual_edit = $manual_edit,
            e.edited_by = $edited_by,
            e.edit_time = $edit_time,
            e.edit_comment = $edit_comment
        RETURN e.id AS entity_id
        """
        
        try:
            result = self.graph.query(query, params=params)
            return bool(result and result[0]["entity_id"])
        except Exception as e:
            self.console.print(f"[red]标记实体为手动编辑时出错: {e}[/red]")
            return False

# 由 src/deepresearch_agent/integrations/build/incremental/manual_edit_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""手动编辑标记初始化与追踪（原 initialize_entity_properties/_setup_manual_edit_tracking）。"""



class ManualEditTrackingMixin:
    def initialize_entity_properties(self):
        """
        初始化实体的常用属性，仅对缺少属性的节点进行初始化，不覆盖已有的值
        """
        try:
            # 初始化manual_edit属性
            self.graph.query("""
            MATCH (e:`__Entity__`)
            WHERE e.manual_edit IS NULL
            SET e.manual_edit = false
            """)
            
            # 初始化created_by属性
            self.graph.query("""
            MATCH (e:`__Entity__`)
            WHERE e.created_by IS NULL
            SET e.created_by = null
            """)
            
            # 初始化edited_by属性
            self.graph.query("""
            MATCH (e:`__Entity__`)
            WHERE e.edited_by IS NULL
            SET e.edited_by = null
            """)
            
            # 初始化created_at属性
            self.graph.query("""
            MATCH (e:`__Entity__`)
            WHERE e.created_at IS NULL
            SET e.created_at = datetime()
            """)
            
            # 初始化system_generated属性
            self.graph.query("""
            MATCH (e:`__Entity__`)
            WHERE e.system_generated IS NULL
            SET e.system_generated = true
            """)
            
            # 同样为关系初始化属性
            self.graph.query("""
            MATCH ()-[r]->()
            WHERE r.manual_edit IS NULL
            SET r.manual_edit = false
            """)
            
            self.graph.query("""
            MATCH ()-[r]->()
            WHERE r.created_by IS NULL
            SET r.created_by = null
            """)
            
            self.graph.query("""
            MATCH ()-[r]->()
            WHERE r.edited_by IS NULL
            SET r.edited_by = null
            """)
            
            self.console.print("[green]实体和关系属性初始化完成[/green]")
        except Exception as e:
            self.console.print(f"[yellow]初始化属性时出错: {e}[/yellow]")

    def _setup_manual_edit_tracking(self):
        """
        设置手动编辑追踪，确保数据库支持追踪手动编辑
        """
        try:
            # 检查是否是集群模式，如果是，检查当前节点角色
            try:
                cluster_role = self.graph.query("CALL dbms.cluster.role()")
                if cluster_role and cluster_role[0].get("role") == "FOLLOWER":
                    self.console.print("[yellow]当前节点为FOLLOWER，无法设置触发器，跳过...[/yellow]")
                    return
            except Exception as e:
                self.console.print(f"[yellow]检查集群角色时出错: {e}[/yellow]")
                
            # 查询当前数据库名称
            try:
                db_info = self.graph.query("CALL db.info()")
                db_name = db_info[0]["name"] if db_info and "name" in db_info[0] else "neo4j"
            except:
                db_name = "neo4j"  # 默认数据库名称
                
            # 添加时间戳触发器追踪节点和关系的创建/修改时间
            try:
                self.graph.query(f"""
                CALL apoc.trigger.install(
                '{db_name}',
                'updateNodeTimestamps',
                '
                UNWIND $assignedLabels AS label
                UNWIND $createdNodes AS n
                WITH n WHERE label = "__Entity__" AND label IN labels(n)
                SET n.updated_at = datetime(),
                    n.created_at = coalesce(n.created_at, datetime())
                ',
                {{phase: 'after'}}
                )
                """)
                
                self.console.print("[green]成功设置节点时间戳追踪[/green]")
                
                # 添加关系时间戳触发器
                self.graph.query(f"""
                CALL apoc.trigger.install(
                '{db_name}',
                'updateRelationshipTimestamps',
                '
                UNWIND $createdRelationships AS r
                SET r.updated_at = datetime(),
                    r.created_at = coalesce(r.created_at, datetime())
                ',
                {{phase: 'after'}}
                )
                """)
                
                self.console.print("[green]成功设置关系时间戳追踪[/green]")
            except Exception as trigger_error:
                self.console.print(f"[yellow]设置触发器时出错: {trigger_error}[/yellow]")
                
        except Exception as e:
            self.console.print(f"[yellow]设置手动编辑追踪时出错 (可能APOC未安装): {e}[/yellow]")
            self.console.print("[yellow]将使用基础的手动编辑检测方法[/yellow]")

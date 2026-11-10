# 由 src/deepresearch_agent/integrations/build/incremental_update.py 拆分（T-016），方法体与原位置逐行一致。
"""手动编辑同步（原 sync_manual_edits/check_manual_edits）。"""



class ManualSyncMixin:
    def sync_manual_edits(self, changed_files=None):
        """
        同步手动编辑
        
        Args:
            changed_files: 变更的文件列表
            
        Returns:
            Dict: 同步结果
        """
        self.console.print("[bold cyan]同步手动编辑...[/bold cyan]")
        
        try:
            # 如果没有提供变更文件列表，获取所有变更
            if changed_files is None:
                changes = self.updater.detect_changes()
                changed_files = changes.get("added", []) + changes.get("modified", [])
            
            if changed_files:
                # 执行手动编辑同步
                result = self.edit_manager.process(changed_files)
                
                return result
            else:
                self.console.print("[yellow]没有变更的文件，跳过手动编辑同步[/yellow]")
                return {"status": "skipped"}
                
        except Exception as e:
            self.console.print(f"[red]同步手动编辑时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"error": str(e)}

    def check_manual_edits(self):
        """
        检查Neo4j中的手动编辑，并确保这些编辑在增量更新中被保留
        
        Returns:
            Dict: 手动编辑的统计信息
        """
        self.console.print("[bold cyan]检查手动编辑...[/bold cyan]")
        
        try:
            # 使用ManualEditManager检测手动编辑
            edit_stats = self.edit_manager.detect_manual_edits()
            
            # 输出手动编辑统计
            manual_entities = edit_stats.get("manual_entities", 0)
            manual_relations = edit_stats.get("manual_relations", 0)
            
            if manual_entities > 0 or manual_relations > 0:
                self.console.print(f"[green]检测到 {manual_entities} 个手动编辑的实体和 {manual_relations} 个手动编辑的关系[/green]")
                
                # 确保增量更新时保留这些手动编辑
                changes = self.updater.detect_changes()
                changed_files = []
                if changes:
                    changed_files = changes.get("added", []) + changes.get("modified", [])
                
                # 应用手动编辑保护
                if changed_files:
                    preserved_count = self.edit_manager.preserve_manual_edits(changed_files)
                    self.console.print(f"[green]已保护 {preserved_count} 个手动编辑，确保增量更新不会覆盖它们[/green]")
                
                return {
                    "manual_entities": manual_entities,
                    "manual_relations": manual_relations,
                    "preserved_edits": preserved_count if changed_files else 0
                }
            else:
                self.console.print("[blue]没有检测到手动编辑[/blue]")
                return {
                    "manual_entities": 0,
                    "manual_relations": 0
                }
                
        except Exception as e:
            self.console.print(f"[red]检查手动编辑时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"error": str(e)}

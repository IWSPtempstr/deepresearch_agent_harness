# 由 src/deepresearch_agent/integrations/build/incremental_update.py 拆分（T-016），方法体与原位置逐行一致。
"""调度器与统计（原 run_once/start_scheduler/stop_scheduler/display_stats/signal_handler）。"""

import time


class SchedulerStatsMixin:
    def run_once(self):
        """
        执行一次完整的增量更新流程
        
        Returns:
            Dict: 更新结果
        """
        start_time = time.time()
        
        self.console.print("\n[bold cyan]开始执行增量更新流程...[/bold cyan]")
        
        results = {}
        
        try:
            # 1. 检测文件变更
            changes = self.detect_file_changes()
            results["file_changes"] = changes
            
            # 2. 更新实体Embedding
            entity_updates = self.update_entity_embeddings()
            results["entity_updates"] = entity_updates
            
            # 3. 更新Chunk Embedding
            chunk_updates = self.update_chunk_embeddings()
            results["chunk_updates"] = chunk_updates
            
            # 4. 验证图谱一致性
            consistency_check = self.verify_graph_consistency()
            results["consistency_check"] = consistency_check
            
            # 5. 同步手动编辑
            if changes and (changes.get("added") or changes.get("modified")):
                edit_sync = self.sync_manual_edits(
                    changes.get("added", []) + changes.get("modified", [])
                )
                results["edit_sync"] = edit_sync
            
            # 6. 执行社区检测
            # 只有当文件有变更时才执行社区检测，避免不必要的计算
            if changes and (changes.get("added") or changes.get("modified") or changes.get("deleted")):
                community_detection = self.detect_communities()
                results["community_detection"] = community_detection
            
            # 计算总耗时
            end_time = time.time()
            total_time = end_time - start_time
            
            self.console.print(f"[bold green]增量更新流程完成，总耗时: {total_time:.2f}秒[/bold green]")
            
            return results
            
        except Exception as e:
            self.console.print(f"[red]执行增量更新流程时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"error": str(e)}

    def start_scheduler(self):
        """
        启动调度器，开始后台运行增量更新流程
        """
        self.console.print("[bold cyan]启动增量更新调度器...[/bold cyan]")
        
        # 注册处理方法
        self.scheduler.schedule_component("file_change", self.detect_file_changes)
        self.scheduler.schedule_component("entity_embedding", self.update_entity_embeddings)
        self.scheduler.schedule_component("chunk_embedding", self.update_chunk_embeddings)
        self.scheduler.schedule_component("graph_consistency", self.verify_graph_consistency)
        self.scheduler.schedule_component("community_detection", self.detect_communities)
        self.scheduler.schedule_component("manual_edit_check", self.check_manual_edits)
        
        # 启动调度器
        self.stop_event = self.scheduler.start()
        self.running = True
        
        self.console.print("[green]增量更新调度器已启动，正在后台运行...[/green]")
        
        # 显示调度器状态
        self.scheduler.print_status()

    def stop_scheduler(self):
        """停止调度器"""
        if self.running and self.stop_event:
            self.scheduler.stop(self.stop_event)
            self.running = False
            self.stop_event = None
            
            self.console.print("[yellow]增量更新调度器已停止[/yellow]")
        else:
            self.console.print("[yellow]调度器未运行[/yellow]")

    def display_stats(self):
        """显示统计信息"""
        self.console.print("\n[bold cyan]增量更新统计信息[/bold cyan]")
        self.console.print(f"[blue]执行的更新次数: {self.stats['updates_performed']}[/blue]")
        self.console.print(f"[blue]处理的文件数: {self.stats['files_processed']}[/blue]")
        self.console.print(f"[blue]更新的实体数: {self.stats['entities_updated']}[/blue]")
        self.console.print(f"[blue]检测的社区数: {self.stats['communities_detected']}[/blue]")
        self.console.print(f"[blue]错误数: {self.stats['errors']}[/blue]")
        
        if self.running:
            self.scheduler.print_status()

    def signal_handler(self, sig, frame):
        """
        信号处理函数，用于处理终止信号
        
        Args:
            sig: 信号
            frame: 帧
        """
        self.console.print("\n[yellow]正在退出...[/yellow]")
        
        if self.running:
            self.stop_scheduler()
            
        self.display_stats()
        
        self.console.print("[green]增量更新管理器已安全退出[/green]")
        exit(0)

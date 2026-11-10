# 由 src/deepresearch_agent/integrations/build/incremental/manual_edit_manager.py 拆分（T-016），方法体与原位置逐行一致。
"""统计与处理入口（原 display_edit_stats/process）。"""

from typing import Dict, List, Any
from rich.table import Table
from deepresearch_agent.config.settings import MAX_WORKERS, BATCH_SIZE


class ManualEditStatsMixin:
    def display_edit_stats(self):
        """显示手动编辑统计"""
        stats_table = Table(title="手动编辑统计")
        stats_table.add_column("指标", style="cyan")
        stats_table.add_column("值", justify="right")
        
        for key, value in self.edit_stats.items():
            stats_table.add_row(key, str(value))
        
        self.console.print(stats_table)
        
        # 显示时间统计
        self.console.print(f"[blue]检测耗时: {self.detection_time:.2f}秒, "
                          f"同步耗时: {self.sync_time:.2f}秒[/blue]")

    def process(self, changed_files: List[str], conflict_strategy: str = "manual_first") -> Dict[str, Any]:
        """
        执行完整的手动编辑同步流程
        
        Args:
            changed_files: 变更的文件列表
            conflict_strategy: 冲突解决策略
            
        Returns:
            Dict: 处理结果统计
        """
        try:
            # 设置手动编辑追踪
            self._setup_manual_edit_tracking()
            
            # 检测手动编辑
            edit_stats = self.detect_manual_edits()
            
            # 保留手动编辑
            preserved_count = self.preserve_manual_edits(changed_files)
            
            # 解决冲突
            resolved_count = self.resolve_conflicts(conflict_strategy)
            
            # 显示统计
            self.display_edit_stats()
            
            return {
                "detection_time": self.detection_time,
                "sync_time": self.sync_time,
                "edit_stats": self.edit_stats,
                "preserved_count": preserved_count,
                "resolved_count": resolved_count
            }
            
        except Exception as e:
            self.console.print(f"[red]手动编辑同步过程中出现错误: {e}[/red]")
            raise

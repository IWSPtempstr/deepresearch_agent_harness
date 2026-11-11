# 由 src/deepresearch_agent/integrations/build/incremental_update.py 拆分（T-016），方法体与原位置逐行一致。
"""变更检测与图谱一致性/社区检查（原 detect_file_changes 等 4 方法）。"""

from deepresearch_agent.community import CommunityDetectorFactory, CommunitySummarizerFactory
from deepresearch_agent.config.neo4jdb import get_db_manager
from deepresearch_agent.config.settings import community_algorithm, NEO4J_CONFIG


class UpdateChecksMixin:
    def detect_file_changes(self):
        """
        检测文件变更并更新图谱
        
        Returns:
            Dict: 变更信息
        """
        self.console.print("[bold cyan]检测文件变更...[/bold cyan]")
        
        try:
            # 检测变更
            changes = self.updater.detect_changes()
            
            # 更新统计信息
            added_count = len(changes.get("added", []))
            modified_count = len(changes.get("modified", []))
            deleted_count = len(changes.get("deleted", []))
            total_changed = added_count + modified_count + deleted_count
            
            if total_changed > 0:
                self.console.print(f"[green]检测到 {total_changed} 个文件变更：[/green]")
                self.console.print(f"[green]新增: {added_count}, 修改: {modified_count}, 删除: {deleted_count}[/green]")
                
                # 如果有变更，执行增量更新
                self.updater.process_incremental_update()
                
                # 如果有文件删除，执行图一致性检查
                if deleted_count > 0:
                    self.verify_graph_consistency()
                
                # 更新统计信息
                self.stats["updates_performed"] += 1
                self.stats["files_processed"] += total_changed
            else:
                self.console.print("[yellow]未检测到文件变更[/yellow]")
            
            return changes
            
        except Exception as e:
            self.console.print(f"[red]检测文件变更时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"error": str(e)}

    def verify_graph_consistency(self, repair=True):
        """
        验证图谱一致性
        
        Args:
            repair: 是否执行修复
            
        Returns:
            Dict: 验证结果
        """
        self.console.print("[bold cyan]验证图谱一致性...[/bold cyan]")
        
        try:
            if repair:
                # 执行验证和修复
                result = self.validator.repair_graph()
                
                # 显示修复结果
                repaired_count = result["validation_stats"]["repaired_issues"]
                self.console.print(f"[green]图谱一致性验证完成，修复了 {repaired_count} 个问题[/green]")
            else:
                # 仅执行验证
                result = self.validator.validate_graph()
                
                # 显示验证结果
                issues_count = result["validation_stats"]["total_issues"]
                self.console.print(f"[green]图谱一致性验证完成，发现 {issues_count} 个问题[/green]")
            
            return result
            
        except Exception as e:
            self.console.print(f"[red]验证图谱一致性时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"error": str(e)}

    def detect_communities(self):
        """
        执行社区检测和摘要生成
        
        Returns:
            Dict: 社区检测结果
        """
        self.console.print("[bold cyan]执行社区检测...[/bold cyan]")
        
        try:
            # 获取数据库连接和GDS对象
            db_manager = get_db_manager()
            graph = db_manager.graph
            
            # 导入GraphDataScience库
            try:
                from graphdatascience import GraphDataScience
                gds = GraphDataScience(
                    NEO4J_CONFIG["uri"],
                    auth=(NEO4J_CONFIG["username"], NEO4J_CONFIG["password"])
                )
            except Exception as e:
                self.console.print(f"[yellow]导入GDS库失败，无法执行社区检测: {e}[/yellow]")
                return {"status": "error", "message": str(e)}
            
            # 创建社区检测器
            self.console.print(f"[blue]使用 {community_algorithm} 算法执行社区检测[/blue]")
            detector = CommunityDetectorFactory.create(
                algorithm=community_algorithm,
                gds=gds,
                graph=graph
            )
            
            # 执行社区检测
            detection_result = detector.process()
            
            if detection_result.get('status', '') == 'success':
                community_count = detection_result.get('details', {}).get('detection', {}).get('communityCount', 0)
                self.console.print(f"[green]社区检测完成，共检测到 {community_count} 个社区[/green]")
                
                # 更新统计信息
                self.stats["communities_detected"] += community_count
                
                # 执行社区摘要生成
                self.console.print("[blue]开始生成社区摘要...[/blue]")
                summarizer = CommunitySummarizerFactory.create_summarizer(
                    community_algorithm,
                    graph
                )
                summaries = summarizer.process_communities()
                
                self.console.print(f"[green]社区摘要生成完成，共生成 {len(summaries) if summaries else 0} 个摘要[/green]")
                
                return {
                    "status": "success", 
                    "communities": community_count,
                    "summaries": len(summaries) if summaries else 0
                }
            else:
                self.console.print(f"[yellow]社区检测失败: {detection_result.get('message', '未知错误')}[/yellow]")
                return detection_result
                
        except Exception as e:
            self.console.print(f"[red]执行社区检测时出错: {e}[/red]")
            self.stats["errors"] += 1
            return {"status": "error", "message": str(e)}

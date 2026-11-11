# 由 src/deepresearch_agent/graph/processing/entity_merger.py 拆分（T-016），方法体与原位置逐行一致。
"""重复候选处理入口（原 process_duplicates）。"""

import time
from typing import List, Dict, Any, Tuple
from deepresearch_agent.graph.core import timer, get_performance_stats, print_performance_stats


class EntityMergeProcessMixin:
    @timer
    def process_duplicates(self, duplicate_candidates: List[Any]) -> Tuple[int, Dict[str, Any]]:
        """
        处理重复实体的完整流程，包括获取合并建议和执行合并 - 性能优化版本
        
        Args:
            duplicate_candidates: 潜在的重复实体候选列表
            
        Returns:
            Tuple[int, Dict[str, Any]]: 合并的实体数量和性能统计
        """
        start_time = time.time()
        
        # 确保duplicate_candidates是列表的列表，处理不同的数据结构
        fixed_candidates = []
        for candidates in duplicate_candidates:
            # 检查候选组是否是字典或类似对象
            if isinstance(candidates, dict) and "combinedResult" in candidates:
                candidate_list = candidates["combinedResult"]
                if isinstance(candidate_list, list) and len(candidate_list) > 1:
                    fixed_candidates.append(candidate_list)
            # 检查候选组是否已经是列表
            elif isinstance(candidates, list) and len(candidates) > 1:
                fixed_candidates.append(candidates)
        
        # 过滤处理数量过少的候选组
        filtered_candidates = [
            candidates for candidates in fixed_candidates
            if len(candidates) > 1
        ]
        
        print(f"处理后候选实体组数: {len(filtered_candidates)}")
        print(f"开始处理 {len(filtered_candidates)} 组有效重复实体候选...")
        
        # 获取合并建议
        merge_groups = self.get_merge_suggestions(filtered_candidates)
        
        suggestion_time = time.time()
        suggestion_elapsed = suggestion_time - start_time
        print(f"生成合并建议完成，用时 {suggestion_elapsed:.2f} 秒, "
            f"找到 {len(merge_groups)} 组可合并实体")
        print(f"其中: LLM处理时间: {self.llm_time:.2f}秒, 解析时间: {self.parse_time:.2f}秒")
        
        # 如果有建议的合并组，执行合并
        merged_count = 0
        if merge_groups:
            merged_count = self.execute_merges(merge_groups)

        # 合并重复关系
        self.clean_duplicate_relationships()
                
        end_time = time.time()
        merge_elapsed = end_time - suggestion_time
        total_elapsed = end_time - start_time
        
        print(f"实体合并完成，用时 {merge_elapsed:.2f} 秒, 合并了 {merged_count} 个实体")
        print(f"数据库操作时间: {self.db_time:.2f}秒")
        print(f"总耗时: {total_elapsed:.2f} 秒")
        
        # 返回性能统计摘要
        time_records = {
            "LLM处理时间": self.llm_time,
            "解析时间": self.parse_time,
            "数据库时间": self.db_time
        }
        
        performance_stats = get_performance_stats(total_elapsed, time_records)
        performance_stats.update({
            "候选实体组数": len(filtered_candidates),
            "识别出的合并组数": len(merge_groups),
            "合并的实体数": merged_count
        })
        
        print_performance_stats(performance_stats)
        
        return merged_count, performance_stats

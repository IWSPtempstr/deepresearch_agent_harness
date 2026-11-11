# 由 src/deepresearch_agent/graph/processing/entity_merger.py 拆分（T-016），方法体与原位置逐行一致。
"""合并执行与重复关系清理（原 _process_candidate_group/execute_merges/clean_duplicate_relationships）。"""

import time
from typing import List, Optional


class EntityMergeExecMixin:
    def _process_candidate_group(self, candidates: List[str]) -> Optional[str]:
        """
        处理单个候选实体组
        
        Args:
            candidates: 候选实体列表
            
        Returns:
            str: LLM的分析结果
        """
        if not candidates or len(candidates) < 2:
            return None
            
        chat_history = []
        max_retries = 2
        for retry in range(max_retries + 1):
            try:
                # 调用LLM进行分析
                answer = self.chain.invoke({
                    "chat_history": chat_history,
                    "entities": candidates
                })
                return answer.content
            except Exception as e:
                if retry < max_retries:
                    print(f"LLM调用异常，尝试重试 ({retry+1}/{max_retries}): {e}")
                    time.sleep(1)  # 短暂延迟
                else:
                    print(f"LLM调用失败，最大重试次数已用尽: {e}")
                    return None

    def execute_merges(self, merge_groups: List[List[str]]) -> int:
        """
        执行实体合并操作 - 批处理优化版本
        
        Args:
            merge_groups: 要合并的实体分组列表
            
        Returns:
            int: 合并操作影响的节点数量
        """
        if not merge_groups:
            return 0
        
        db_start_time = time.time()
            
        # 动态批处理大小
        group_count = len(merge_groups)
        optimal_batch_size = min(self.batch_size, max(5, group_count // 10))
        total_batches = (group_count + optimal_batch_size - 1) // optimal_batch_size
        
        print(f"开始执行 {group_count} 组实体合并，批次大小: {optimal_batch_size}")
        
        total_merged = 0
        batch_times = []
        
        # 批量处理合并操作
        for batch_index in range(total_batches):
            batch_start = time.time()
            
            start_idx = batch_index * optimal_batch_size
            end_idx = min(start_idx + optimal_batch_size, group_count)
            batch = merge_groups[start_idx:end_idx]
            
            try:
                # 执行Neo4j合并操作
                result = self.graph.query("""
                UNWIND $data AS candidates
                CALL {
                  WITH candidates
                  MATCH (e:__Entity__) WHERE e.id IN candidates
                  RETURN collect(e) AS nodes
                }
                CALL apoc.refactor.mergeNodes(nodes, {properties: {
                    `.*`: 'discard'
                }})
                YIELD node
                RETURN count(*) as merged_count
                """, params={"data": batch})
                
                if result:
                    batch_merged = result[0]["merged_count"]
                    total_merged += batch_merged
                    
                    batch_end = time.time()
                    batch_time = batch_end - batch_start
                    batch_times.append(batch_time)
                    
                    # 计算平均时间和剩余时间
                    avg_time = sum(batch_times) / len(batch_times)
                    remaining_batches = total_batches - (batch_index + 1)
                    estimated_remaining = avg_time * remaining_batches
                    
                    print(f"已处理合并批次 {batch_index+1}/{total_batches}, "
                          f"批次合并: {batch_merged} 实体, "
                          f"批次耗时: {batch_time:.2f}秒, "
                          f"预计剩余: {estimated_remaining:.2f}秒")
            except Exception as e:
                print(f"批量合并出错，尝试单个处理: {e}")
                batch_merged = 0
                
                # 如果批处理失败，尝试逐个合并
                for group in batch:
                    try:
                        single_result = self.graph.query("""
                        MATCH (e:__Entity__) WHERE e.id IN $candidates
                        WITH collect(e) AS nodes
                        CALL apoc.refactor.mergeNodes(nodes, {properties: {
                            `.*`: 'discard'
                        }})
                        YIELD node
                        RETURN count(*) as merged_count
                        """, params={"candidates": group})
                        
                        if single_result:
                            group_merged = single_result[0]["merged_count"]
                            total_merged += group_merged
                            batch_merged += group_merged
                    except Exception as e2:
                        print(f"单个组合并失败: {e2}")
                
                print(f"单个处理完成，本批次合并: {batch_merged} 实体")
        
        self.db_time += time.time() - db_start_time
        
        return total_merged

    def clean_duplicate_relationships(self):
        """
        清除重复关系，包括：
        1. 相同方向的重复关系
        2. SIMILAR关系的双向冗余（保留一个方向）
        """
        print("开始清除重复关系...")
        
        # 第一步：清除相同方向的重复关系
        result1 = self.graph.query("""
        MATCH (a)-[r]->(b)
        WITH a, b, type(r) as type, collect(r) as rels
        WHERE size(rels) > 1
        WITH a, b, type, rels[0] as kept, rels[1..] as rels
        UNWIND rels as rel
        DELETE rel
        RETURN count(*) as deleted
        """)
        
        deleted_count1 = result1[0]["deleted"] if result1 else 0
        print(f"已删除 {deleted_count1} 个相同方向的重复关系")
        
        # 第二步：清除SIMILAR关系的双向冗余（保留一个方向）
        result2 = self.graph.query("""
        // 找出所有双向的SIMILAR关系
        MATCH (a)-[r1:SIMILAR]->(b)
        MATCH (b)-[r2:SIMILAR]->(a)
        WHERE a.id < b.id  // 确保每对节点只处理一次
        
        // 随机选择一个方向删除（这里选择删除b->a方向）
        DELETE r2
        
        RETURN count(*) as deleted_bidirectional
        """)
        
        deleted_count2 = result2[0]["deleted_bidirectional"] if result2 else 0
        print(f"已删除 {deleted_count2} 个双向SIMILAR关系的冗余方向")
        
        total_deleted = deleted_count1 + deleted_count2
        print(f"总共删除了 {total_deleted} 个重复关系")
        
        return total_deleted

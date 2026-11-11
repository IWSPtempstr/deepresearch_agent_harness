# 由 src/deepresearch_agent/graph/processing/entity_merger.py 拆分（T-016），方法体与原位置逐行一致。
"""合并建议生成与分组（原 _convert_to_list/get_merge_suggestions/_merge_overlapping_groups）。"""

import re
import ast
import time
import concurrent.futures
from typing import List, Any


class EntityMergeSuggestMixin:
    def _convert_to_list(self, result: str) -> List[List[str]]:
        """
        将LLM返回的实体列表文本转换为Python列表
        
        Args:
            result: LLM返回的文本结果，包含实体列表
            
        Returns:
            List[List[str]]: 二维列表，每个子列表包含一组可合并的实体
        """
        start_time = time.time()
        
        # 使用正则表达式匹配所有方括号包含的内容
        list_pattern = re.compile(r'\[.*?\]')
        entity_lists = []
        
        # 先尝试直接用ast.literal_eval解析整个结果
        try:
            # 查找可能的列表开始位置
            list_start = result.find('[')
            if list_start >= 0:
                # 尝试找出完整列表部分
                nested_level = 0
                for i in range(list_start, len(result)):
                    if result[i] == '[':
                        nested_level += 1
                    elif result[i] == ']':
                        nested_level -= 1
                        if nested_level == 0:
                            # 提取出可能是列表的部分
                            list_portion = result[list_start:i+1]
                            try:
                                parsed_list = ast.literal_eval(list_portion)
                                if isinstance(parsed_list, list):
                                    # 检查是否是二维列表
                                    if all(isinstance(item, list) for item in parsed_list):
                                        entity_lists = parsed_list
                                    else:
                                        entity_lists = [parsed_list]
                                    break
                            except:
                                pass  # 如果解析失败，继续使用正则方法
        except:
            pass  # 如果上述方法失败，回退到正则表达式方法
        
        # 如果直接解析失败，使用正则表达式方法
        if not entity_lists:
            # 解析每个匹配的列表字符串
            for match in list_pattern.findall(result):
                try:
                    # 将字符串转换为Python列表
                    entity_list = ast.literal_eval(match)
                    # 只添加非空列表
                    if entity_list and isinstance(entity_list, list):
                        if all(isinstance(item, list) for item in entity_list):
                            # 如果是嵌套列表，扩展它
                            entity_lists.extend(entity_list)
                        else:
                            # 如果是单个列表，添加它
                            entity_lists.append(entity_list)
                except Exception as e:
                    print(f"解析实体列表时出错: {str(e)}, 原文本: {match}")
        
        # 过滤和规范化结果
        valid_lists = []
        for entity_list in entity_lists:
            # 确保列表中的所有项目都是字符串
            if all(isinstance(item, str) for item in entity_list):
                # 去除重复项
                unique_list = list(dict.fromkeys(entity_list))
                if len(unique_list) > 1:  # 只保留至少有2个实体的组
                    valid_lists.append(unique_list)
        
        self.parse_time += time.time() - start_time
        return valid_lists

    def get_merge_suggestions(self, duplicate_candidates: List[Any]) -> List[List[str]]:
        """
        使用LLM分析并提供实体合并建议 - 并行优化版本
        
        Args:
            duplicate_candidates: 潜在的重复实体候选列表
            
        Returns:
            List[List[str]]: 建议合并的实体分组列表
        """
        # 检查是否有候选实体
        if not duplicate_candidates:
            return []
        
        llm_start_time = time.time()
            
        # 收集LLM的合并建议
        merged_entities = []
        
        # 动态调整批处理大小
        candidate_count = len(duplicate_candidates)
        optimal_batch_size = min(self.max_workers * 2, max(1, candidate_count // 5))
        
        print(f"处理 {candidate_count} 个候选实体组，批次大小: {optimal_batch_size}")
        
        # 分批处理，避免创建过多线程
        for batch_start in range(0, candidate_count, optimal_batch_size):
            batch_end = min(batch_start + optimal_batch_size, candidate_count)
            batch = duplicate_candidates[batch_start:batch_end]
            
            # 使用线程池并行处理LLM请求
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # 提交所有任务
                future_to_candidates = {
                    executor.submit(self._process_candidate_group, candidates): i
                    for i, candidates in enumerate(batch)
                }
                
                # 收集结果
                for future in concurrent.futures.as_completed(future_to_candidates):
                    try:
                        result = future.result()
                        if result:
                            merged_entities.append(result)
                    except Exception as e:
                        print(f"处理候选实体组时出错: {e}")
            
            # 报告进度
            print(f"已处理 {batch_end}/{candidate_count} 个候选实体组")
        
        self.llm_time += time.time() - llm_start_time
        
        parse_start_time = time.time()
        # 解析并整理最终的合并建议
        results = []
        for candidates in merged_entities:
            # 将每个建议转换为列表格式
            temp = self._convert_to_list(candidates)
            results.extend(temp)
        
        self.parse_time += time.time() - parse_start_time
        
        # 合并具有相同实体的组
        merged_results = self._merge_overlapping_groups(results)
        
        print(f"LLM分析完成，找到 {len(merged_results)} 组可合并实体")
        return merged_results

    def _merge_overlapping_groups(self, groups: List[List[str]]) -> List[List[str]]:
        """
        合并有重叠的实体组
        
        Args:
            groups: 实体组列表
            
        Returns:
            List[List[str]]: 合并后的实体组列表
        """
        if not groups:
            return []
            
        # 创建实体到组的映射
        entity_to_groups = {}
        for i, group in enumerate(groups):
            for entity in group:
                if entity not in entity_to_groups:
                    entity_to_groups[entity] = []
                entity_to_groups[entity].append(i)
        
        # 使用并查集合并连通的组
        parent = list(range(len(groups)))
        
        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]
        
        def union(x, y):
            parent[find(x)] = find(y)
        
        # 合并存在共同实体的组
        for entity, group_indices in entity_to_groups.items():
            if len(group_indices) > 1:
                for i in range(1, len(group_indices)):
                    union(group_indices[0], group_indices[i])
        
        # 收集合并后的组
        merged_groups = {}
        for i, group in enumerate(groups):
            root = find(i)
            if root not in merged_groups:
                merged_groups[root] = set()
            merged_groups[root].update(group)
        
        # 转换回列表格式
        return [list(entities) for entities in merged_groups.values()]

# 由 search/tool/reasoning/chain_of_exploration.py 拆分（T-014），方法体与原位置逐行一致。
"""探索主循环：步进决策与异步探索（原 explore/explore_async）。"""

from typing import List
import time
import asyncio
import re


class ChainExplorerMixin:
    def explore(self, query: str, starting_entities: List[str], max_steps: int = 5, exploration_width: int = 3):
        """
        从起始实体开始探索图谱
        
        Args:
            query: 用户查询
            starting_entities: 起始实体列表
            max_steps: 最大探索步数
            exploration_width: 基础探索宽度
            
        Returns:
            Dict: 探索结果
        """
        start_time = time.time()
        
        if not starting_entities:
            return {
                "entities": [],
                "relationships": [],
                "content": [],
                "exploration_path": []
            }
            
        # 重置状态
        self.visited_nodes = set(starting_entities)
        self.exploration_path = []
        query_embedding = self.embeddings.embed_query(query)
        
        # 添加起始节点到探索路径
        for entity in starting_entities:
            self.exploration_path.append({
                "step": 0,
                "node_id": entity,
                "action": "start",
                "reasoning": "初始实体"
            })
        
        # 根据查询内容生成探索策略
        exploration_strategy = self._generate_exploration_strategy(query, starting_entities)
        self.performance_metrics["strategy_generation_time"] = time.time() - start_time
        
        current_entities = starting_entities
        results = {
            "entities": [],
            "relationships": [],
            "content": [],
            "communities": []
        }
        
        # 多步探索
        for step in range(max_steps):
            step_start_time = time.time()
            
            if not current_entities:
                break
                
            # 1. 找出邻居节点
            neighbors = self._get_neighbors(current_entities)
            if not neighbors:
                break
                
            # 2. 动态宽度控制
            current_width = self._calculate_adaptive_width(
                step, 
                query, 
                neighbors, 
                base_width=exploration_width
            )
            
            # 3. 评估每个邻居与查询的相关性
            scored_neighbors = self._score_neighbors_enhanced(
                neighbors, 
                query, 
                query_embedding,
                exploration_strategy
            )
            
            # 4. 让LLM决定探索方向
            next_entities, reasoning = self._decide_next_step_with_memory(
                query, 
                current_entities, 
                scored_neighbors, 
                current_width,
                step
            )
            
            # 5. 更新已访问节点
            new_entities = [e for e in next_entities if e not in self.visited_nodes]
            self.visited_nodes.update(new_entities)
            
            # 6. 获取新发现实体的内容
            entity_info = self._get_entity_info(new_entities)
            results["entities"].extend(entity_info)
            
            # 7. 收集关系信息
            rel_info = self._get_relationship_info(new_entities)
            results["relationships"].extend(rel_info)
            
            # 8. 收集内容信息（如chunk）
            content_info = self._get_content_info(new_entities)
            results["content"].extend(content_info)
            
            # 9. 尝试获取所属社区信息
            community_info = self._get_community_info(new_entities)
            if community_info:
                results["communities"].extend(community_info)
            
            # 10. 记录探索路径
            for entity in new_entities:
                self.exploration_path.append({
                    "step": step + 1,
                    "node_id": entity,
                    "action": "explore",
                    "reasoning": reasoning
                })
            
            # 11. 更新当前实体
            current_entities = new_entities
            
            # 记录每步耗时
            self.performance_metrics[f"step_{step+1}_time"] = time.time() - step_start_time
        
        # 根据查询对所有收集的内容进行最终排序
        results["content"] = self._rank_content_by_relevance(query_embedding, results["content"])
        
        # 添加探索路径、数据统计和性能指标
        results["exploration_path"] = self.exploration_path
        results["visited_entities"] = list(self.visited_nodes)
        results["statistics"] = {
            "entity_count": len(results["entities"]),
            "relationship_count": len(results["relationships"]),
            "content_count": len(results["content"]),
            "path_length": len(self.exploration_path)
        }
        
        # 记录总耗时
        self.performance_metrics["total_time"] = time.time() - start_time
        results["performance_metrics"] = self.performance_metrics
        
        return results

    def _decide_next_step_with_memory(self, query, current_entities, scored_neighbors, width, current_step):
        """
        让LLM决定下一步探索方向，考虑已探索的记忆
        
        Args:
            query: 查询字符串
            current_entities: 当前实体列表
            scored_neighbors: 评分后的邻居列表
            width: 探索宽度
            current_step: 当前步骤
            
        Returns:
            Tuple[List[str], str]: 下一步实体列表和推理过程
        """
        memory_key = f"{query}_{','.join(sorted(current_entities))}"
        
        # 检查是否有记忆
        if memory_key in self.exploration_memory:
            remembered = self.exploration_memory[memory_key]
            # 检查记忆是否过期(根据步骤差异判断)
            if remembered["step"] == current_step:
                return remembered["entities"], remembered["reasoning"]
        
        # 构建提示
        prompt = f"""
        我正在使用Chain of Exploration方法探索知识图谱，以回答问题: "{query}"
        
        当前探索的实体有:
        {', '.join(current_entities)}
        
        当前是探索的第{current_step+1}步，需要决定下一步探索哪些实体。
        
        下面是一些可能的下一步探索选项(已按综合得分排序):
        """
        
        # 添加前10个最相关的选项(或全部如果少于10个)
        top_options = scored_neighbors[:10] if len(scored_neighbors) > 10 else scored_neighbors
        for i, neighbor in enumerate(top_options):
            prompt += f"{i+1}. {neighbor['id']} (得分: {neighbor['final_score']:.2f})\n"
            prompt += f"   - 描述: {neighbor['description']}\n"
            prompt += f"   - 关系类型: {neighbor['relation_type']} (连接到: {neighbor['source'] if neighbor['target'] in current_entities else neighbor['target']})\n\n"
            
        prompt += f"""
        请选择最多{width}个最有价值的实体继续探索。你的选择应该:
        1. 平衡相关性和覆盖广度
        2. 避免过于相似的实体
        3. 考虑探索多种关系类型的可能性
        4. 优先选择有助于回答问题的实体
        
        要求回复格式:
        ```
        实体: [实体1, 实体2, ...]
        推理: 你的选择理由...
        ```
        """
        
        try:
            # 调用LLM决策
            response = self.llm.invoke(prompt)
            content = response.content if hasattr(response, 'content') else str(response)
            
            # 解析结果
            entities_match = re.search(r'实体:\s*\[(.*?)\]', content, re.DOTALL)
            reasoning_match = re.search(r'推理:(.*?)($|```)', content, re.DOTALL)
            
            selected_entities = []
            reasoning = "无具体推理过程"
            
            if entities_match:
                entities_str = entities_match.group(1).strip()
                # 处理实体列表
                if entities_str:
                    # 分割并清理实体
                    entities = [e.strip().strip('"\'') for e in entities_str.split(',')]
                    selected_entities = [e for e in entities if e]
            
            if reasoning_match:
                reasoning = reasoning_match.group(1).strip()
            
            # 如果没有解析出实体，使用得分最高的几个
            if not selected_entities:
                selected_entities = [n['id'] for n in scored_neighbors[:width]]
                reasoning = "基于相似度得分自动选择"
            
            # 保存到记忆
            self.exploration_memory[memory_key] = {
                "entities": selected_entities,
                "reasoning": reasoning,
                "step": current_step
            }
            
            return selected_entities, reasoning
                
        except Exception as e:
            print(f"LLM决策失败: {e}")
            # 出错时使用简单启发式方法
            fallback_entities = [n['id'] for n in scored_neighbors[:width]]
            fallback_reasoning = "决策过程出错，默认选择得分最高的实体"
            return fallback_entities, fallback_reasoning

    async def explore_async(self, query: str, starting_entities: List[str], max_steps: int = 5):
        """
        异步执行探索过程
        
        Args:
            query: 用户查询
            starting_entities: 起始实体列表
            max_steps: 最大探索步数
            
        Returns:
            Dict: 探索结果
            AsyncGenerator: 进度更新生成器
        """
        async def progress_generator():
            """生成进度更新"""
            yield {"status": "started", "message": "开始探索过程"}
            
            # 等待探索开始
            await asyncio.sleep(0.1)
            
            for step in range(max_steps):
                if step in self.progress_updates:
                    yield self.progress_updates[step]
                    
                await asyncio.sleep(0.5)
                
            # 最终更新
            if "final" in self.progress_updates:
                yield self.progress_updates["final"]
        
        # 初始化进度更新存储
        self.progress_updates = {}
        
        # 创建任务
        exploration_task = asyncio.create_task(self._explore_async_impl(
            query, starting_entities, max_steps
        ))
        
        # 返回结果和进度生成器
        return await exploration_task, progress_generator()

    async def _explore_async_impl(self, query, starting_entities, max_steps):
        """异步探索实现"""
        # 包装同步方法
        def sync_explore():
            return self.explore(query, starting_entities, max_steps)
            
        # 更新进度
        self.progress_updates[0] = {
            "status": "exploring", 
            "step": 0,
            "message": f"开始从{len(starting_entities)}个起始实体探索"
        }
        
        # 执行同步探索
        result = await asyncio.to_thread(sync_explore)
        
        # 更新最终进度
        self.progress_updates["final"] = {
            "status": "completed",
            "message": f"探索完成，共发现{len(result.get('entities', []))}个实体，{len(result.get('content', []))}条内容"
        }
        
        return result

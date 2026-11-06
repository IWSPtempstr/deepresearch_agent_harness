# 由 agents/base.py 拆分（T-014），方法体与原位置逐行一致。
"""多级缓存查询与答案质量回写（原 check_fast_cache 等 4 个方法）。"""

import time


class AgentCacheMixin:
    def check_fast_cache(self, query: str, thread_id: str = "default") -> str:
        """专用的快速缓存检查方法，用于高性能路径"""
        start_time = time.time()
        
        # 提取关键词，确保在缓存键中使用
        keywords = self._extract_keywords(query)
        cache_params = {
            "thread_id": thread_id,
            "low_level_keywords": keywords.get("low_level", []),
            "high_level_keywords": keywords.get("high_level", [])
        }
        
        # 使用缓存管理器的快速获取方法，传递相关参数
        result = self.cache_manager.get_fast(query, **cache_params)
        duration = time.time() - start_time
        self._log_performance("fast_cache_check", {
            "duration": duration,
            "hit": result is not None
        })
        
        return result

    def _check_all_caches(self, query: str, thread_id: str = "default"):
        """整合的缓存检查方法"""
        cache_check_start = time.time()
        
        # 1. 首先尝试全局缓存（跨会话缓存）
        global_result = self.global_cache_manager.get(query)
        if global_result:
            print(f"全局缓存命中: {query[:30]}...")
            
            cache_time = time.time() - cache_check_start
            self._log_performance("cache_check", {
                "duration": cache_time,
                "type": "global"
            })
            
            return global_result
        
        # 2. 尝试快速路径 - 跳过验证的高质量缓存
        fast_result = self.check_fast_cache(query, thread_id)
        if fast_result:
            print(f"快速路径缓存命中: {query[:30]}...")
            
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(query, fast_result)
            
            cache_time = time.time() - cache_check_start
            self._log_performance("cache_check", {
                "duration": cache_time,
                "type": "fast"
            })
            
            return fast_result
        
        # 3. 尝试常规缓存路径，但优化验证
        cached_response = self.cache_manager.get(query, skip_validation=True, thread_id=thread_id)
        if cached_response:
            print(f"常规缓存命中，跳过验证: {query[:30]}...")
            
            # 将命中的内容同步到全局缓存
            self.global_cache_manager.set(query, cached_response)
            
            cache_time = time.time() - cache_check_start
            self._log_performance("cache_check", {
                "duration": cache_time,
                "type": "standard"
            })
            
            return cached_response
        
        # 没有命中任何缓存
        cache_time = time.time() - cache_check_start
        self._log_performance("cache_check", {
            "duration": cache_time,
            "type": "miss"
        })
        
        return None

    def mark_answer_quality(self, query: str, is_positive: bool, thread_id: str = "default"):
        """标记回答质量，用于缓存质量控制"""
        start_time = time.time()
        
        # 提取关键词
        keywords = self._extract_keywords(query)
        cache_params = {
            "thread_id": thread_id,
            "low_level_keywords": keywords.get("low_level", []),
            "high_level_keywords": keywords.get("high_level", [])
        }
        
        # 调用缓存管理器的质量标记方法，传递相关参数
        marked = self.cache_manager.mark_quality(query.strip(), is_positive, **cache_params)
        
        mark_time = time.time() - start_time
        self._log_performance("mark_quality", {
            "duration": mark_time,
            "is_positive": is_positive
        })

    def clear_cache_for_query(self, query: str, thread_id: str = "default"):
        """
        清除特定查询的缓存（会话缓存和全局缓存）
        
        参数:
            query: 查询字符串
            thread_id: 会话ID
        
        返回:
            bool: 是否成功删除
        """
        # 清除会话缓存
        success = False
        
        try:
            # 尝试移除可能存在的前缀
            clean_query = query.strip()
            if ":" in clean_query:
                parts = clean_query.split(":", 1)
                if len(parts) > 1:
                    clean_query = parts[1].strip()
            
            # 清除原始查询的会话缓存
            session_cache_deleted = self.cache_manager.delete(query.strip(), thread_id=thread_id)
            success = session_cache_deleted
            
            # 清除没有前缀的查询缓存
            if clean_query != query.strip():
                self.cache_manager.delete(clean_query, thread_id=thread_id)
            
            # 清除带前缀的查询缓存变体
            prefixes = ["generate:", "deep:", "query:"]
            for prefix in prefixes:
                self.cache_manager.delete(f"{prefix}{clean_query}", thread_id=thread_id)
            
            # 清除全局缓存 - 使用所有可能的变体
            if hasattr(self, 'global_cache_manager'):
                # 删除原始查询
                global_cache_deleted = self.global_cache_manager.delete(query.strip())
                success = success or global_cache_deleted
                
                # 删除清理后的查询
                if clean_query != query.strip():
                    self.global_cache_manager.delete(clean_query)
                
                # 删除带前缀的查询变体
                for prefix in prefixes:
                    self.global_cache_manager.delete(f"{prefix}{clean_query}")
            
            # 强制刷新缓存写入
            if hasattr(self.cache_manager.storage, '_flush_write_queue'):
                self.cache_manager.storage._flush_write_queue()
            
            if hasattr(self, 'global_cache_manager') and hasattr(self.global_cache_manager.storage, '_flush_write_queue'):
                self.global_cache_manager.storage._flush_write_queue()
                
            # 记录日志
            print(f"已清除查询缓存: {query.strip()}")
            
            return success
        except Exception as e:
            print(f"清除缓存时出错: {e}")
            return False

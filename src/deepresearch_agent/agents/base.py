# 由 agents/base.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _setup_tools, _log_execution, _log_performance, _extract_keywords, _validate_answer, close。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import Annotated, Sequence, TypedDict, List, Dict, Any, AsyncGenerator, Optional
from abc import ABC, abstractmethod
from langgraph.checkpoint.memory import MemorySaver
import time
from deepresearch_agent.models.get_models import get_llm_model, get_stream_llm_model, get_embeddings_model
from deepresearch_agent.cache_manager.manager import (
    CacheManager, 
    ContextAwareCacheKeyStrategy, 
    HybridCacheBackend
)
from deepresearch_agent.cache_manager.strategies.global_strategy import GlobalCacheKeyStrategy
from deepresearch_agent.config.settings import AGENT_SETTINGS

from deepresearch_agent.agents.agent_graph import AgentGraphMixin
from deepresearch_agent.agents.agent_cache import AgentCacheMixin
from deepresearch_agent.agents.agent_ask import AgentAskMixin

class BaseAgent(AgentGraphMixin, AgentCacheMixin, AgentAskMixin, ABC):
    """Agent 基类，定义通用功能和接口"""
    

    def __init__(self, cache_dir="./cache", memory_only=False, *, enable_vector_cache=None, llm_model: str | None = None):
        """
        初始化搜索工具
        
        参数:
            cache_dir: 缓存目录，用于存储搜索结果
        """
        # 初始化普通 LLM 和流式 LLM（T-009：llm_model 支持按任务难度注入分层模型）
        self.llm = get_llm_model(model=llm_model) if llm_model else get_llm_model()
        self.stream_llm = get_stream_llm_model() if not llm_model else get_llm_model(model=llm_model)
        self.embeddings = get_embeddings_model()
        self.default_recursion_limit = AGENT_SETTINGS["default_recursion_limit"]
        self.stream_flush_threshold = AGENT_SETTINGS["stream_flush_threshold"]
        self.deep_stream_flush_threshold = AGENT_SETTINGS["deep_stream_flush_threshold"]
        self.fusion_stream_flush_threshold = AGENT_SETTINGS["fusion_stream_flush_threshold"]
        self.chunk_size = AGENT_SETTINGS["chunk_size"]
        
        self.memory = MemorySaver()
        self.execution_log = []
    
        # 常规上下文感知缓存（会话内）
        self.cache_manager = CacheManager(
            key_strategy=ContextAwareCacheKeyStrategy(),
            storage_backend=HybridCacheBackend(
                cache_dir=cache_dir,
                memory_max_size=200,
                disk_max_size=2000
            ) if not memory_only else None,
            cache_dir=cache_dir,
            memory_only=memory_only,
            enable_vector_similarity=enable_vector_cache,
        )
        
        # 全局缓存（跨会话）
        self.global_cache_manager = CacheManager(
            key_strategy=GlobalCacheKeyStrategy(),
            storage_backend=HybridCacheBackend(
                cache_dir=f"{cache_dir}/global",
                memory_max_size=500,
                disk_max_size=5000
            ) if not memory_only else None,
            cache_dir=f"{cache_dir}/global",
            memory_only=memory_only,
            enable_vector_similarity=enable_vector_cache,
        )
        
        self.performance_metrics = {}  # 性能指标收集
        
        # 初始化工具
        self.tools = self._setup_tools()
        
        # 设置工作流图
        self._setup_graph()

    @abstractmethod
    def _setup_tools(self) -> List:
        """设置工具，子类必须实现"""
        pass

    def _log_execution(self, node_name: str, input_data: Any, output_data: Any):
        """记录节点执行"""
        self.execution_log.append({
            "node": node_name,
            "timestamp": time.time(),
            "input": input_data,
            "output": output_data
        })

    def _log_performance(self, operation, metrics):
        """记录性能指标"""
        self.performance_metrics[operation] = {
            "timestamp": time.time(),
            **metrics
        }
        
        # 输出关键性能指标
        if "duration" in metrics:
            print(f"性能指标 - {operation}: {metrics['duration']:.4f}s")

    @abstractmethod
    def _extract_keywords(self, query: str) -> Dict[str, List[str]]:
        """提取查询关键词，子类必须实现"""
        pass

    def _validate_answer(self, query: str, answer: str, thread_id: str = "default") -> bool:
        """验证答案质量"""
        
        # 使用缓存管理器的验证方法
        def validator(query, answer):
            # 基本检查 - 长度
            if len(answer) < 20:
                return False
                
            # 检查是否包含错误消息
            error_patterns = [
                "抱歉，处理您的问题时遇到了错误",
                "技术原因:",
                "无法获取",
                "无法回答这个问题"
            ]
            
            for pattern in error_patterns:
                if pattern in answer:
                    return False
                    
            # 相关性检查 - 检查问题关键词是否在答案中出现
            keywords = self._extract_keywords(query)
            if keywords:
                low_level_keywords = keywords.get("low_level", [])
                if low_level_keywords:
                    # 至少有一个低级关键词应该在答案中出现
                    keyword_found = any(keyword.lower() in answer.lower() for keyword in low_level_keywords)
                    if not keyword_found:
                        return False
            
            # 通过所有检查
            return True
        
        return self.cache_manager.validate_answer(query, answer, validator, thread_id=thread_id)

    def close(self):
        """关闭资源"""
        # 确保所有延迟写入的缓存项都被保存
        if hasattr(self.cache_manager.storage, '_flush_write_queue'):
            self.cache_manager.storage._flush_write_queue()
            
        # 同样确保全局缓存的写入被保存
        if hasattr(self.global_cache_manager.storage, '_flush_write_queue'):
            self.global_cache_manager.storage._flush_write_queue()

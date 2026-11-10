# 由 src/deepresearch_agent/graph/processing/entity_merger.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _create_indexes, _setup_llm_chain。
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate
)
from deepresearch_agent.models.get_models import get_llm_model
from deepresearch_agent.config.prompts import system_template_build_index, user_template_build_index
from deepresearch_agent.config.settings import ENTITY_BATCH_SIZE, MAX_WORKERS as DEFAULT_MAX_WORKERS
from deepresearch_agent.graph.core import connection_manager, timer, get_performance_stats, print_performance_stats

from deepresearch_agent.graph.processing.entity_merge_suggest import EntityMergeSuggestMixin
from deepresearch_agent.graph.processing.entity_merge_exec import EntityMergeExecMixin
from deepresearch_agent.graph.processing.entity_merge_process import EntityMergeProcessMixin

class EntityMerger(EntityMergeSuggestMixin, EntityMergeExecMixin, EntityMergeProcessMixin):
    """
    实体合并管理器，负责基于LLM决策合并相似实体。
    主要功能包括使用LLM分析实体相似性、解析合并建议，以及执行实体合并操作。
    """
    

    def __init__(self, batch_size: int = 20, max_workers: int = 4):
        """
        初始化实体合并管理器
        
        Args:
            batch_size: 批处理大小
            max_workers: 并行工作线程数
        """
        # 初始化图数据库连接
        self.graph = connection_manager.get_connection()
        
        # 获取语言模型
        self.llm = get_llm_model()
        
        # 批处理和并行参数
        self.batch_size = batch_size or ENTITY_BATCH_SIZE
        self.max_workers = max_workers or DEFAULT_MAX_WORKERS
        
        # 设置LLM处理链
        self._setup_llm_chain()
        
        # 创建索引
        self._create_indexes()
        
        # 性能监控
        self.llm_time = 0
        self.db_time = 0
        self.parse_time = 0

    def _create_indexes(self) -> None:
        """创建必要的索引以优化查询性能"""
        index_queries = [
            "CREATE INDEX IF NOT EXISTS FOR (e:`__Entity__`) ON (e.id)"
        ]
        
        connection_manager.create_multiple_indexes(index_queries)

    def _setup_llm_chain(self) -> None:
        """
        设置LLM处理链，用于实体合并决策
        包括创建提示模板和构建处理链
        """
        # 检查模型能力
        if not hasattr(self.llm, 'with_structured_output'):
            print("当前LLM模型不支持结构化输出")

        # 创建提示模板
        system_message_prompt = SystemMessagePromptTemplate.from_template(
            system_template_build_index
        )
        human_message_prompt = HumanMessagePromptTemplate.from_template(
            user_template_build_index
        )
        
        # 构建对话链
        self.chat_prompt = ChatPromptTemplate.from_messages([
            system_message_prompt,
            MessagesPlaceholder("chat_history"),
            human_message_prompt
        ])
        
        # 创建最终的处理链
        self.chain = self.chat_prompt | self.llm

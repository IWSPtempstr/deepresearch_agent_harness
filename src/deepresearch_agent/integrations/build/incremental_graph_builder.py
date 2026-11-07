# 由 integrations/build/incremental_graph_builder.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
from rich.console import Console
from deepresearch_agent.models.get_models import get_llm_model
from deepresearch_agent.config.prompts import system_template_build_graph, human_template_build_graph
from deepresearch_agent.config.settings import (
    entity_types, relationship_types, CHUNK_SIZE, OVERLAP, MAX_WORKERS, BATCH_SIZE,
    FILE_REGISTRY_PATH
)
from deepresearch_agent.pipelines.ingestion.document_processor import DocumentProcessor
from deepresearch_agent.graph import EntityRelationExtractor, GraphWriter, GraphStructureBuilder
from deepresearch_agent.config.neo4jdb import get_db_manager
from deepresearch_agent.integrations.build.incremental.file_change_manager import FileChangeManager
from deepresearch_agent.graph.indexing.embedding_manager import EmbeddingManager

from deepresearch_agent.integrations.build.incremental_file_changes import GraphFileChangesMixin
from deepresearch_agent.integrations.build.incremental_merge import GraphMergeMixin
from deepresearch_agent.integrations.build.incremental_io import GraphIoMixin
from deepresearch_agent.integrations.build.incremental_stats import GraphStatsMixin

class IncrementalGraphUpdater(GraphFileChangesMixin, GraphMergeMixin, GraphIoMixin, GraphStatsMixin):
    """
    增量图谱更新器，基于LightRAG理念实现高效的增量更新。
    
    主要功能：
    1. 无缝集成新数据到现有图结构
    2. 仅更新变更部分，避免重建整个索引
    3. 高效合并新旧图结构
    4. 保护现有图谱的完整性
    """
    

    def __init__(self, files_dir: str, registry_path: str = None):
        """
        初始化增量图谱更新器

        Args:
            files_dir: 文件目录
            registry_path: 文件注册表路径，默认使用配置中的路径
        """
        if registry_path is None:
            registry_path = str(FILE_REGISTRY_PATH)

        self.console = Console()
        self.graph = get_db_manager().graph
        
        # 初始化文件变更管理器
        self.file_manager = FileChangeManager(files_dir, registry_path)
        
        # 初始化优化的Embedding管理器
        self.embedding_manager = EmbeddingManager(batch_size=BATCH_SIZE, max_workers=MAX_WORKERS)
        
        # 保存文件目录路径
        self.files_dir = files_dir
        
        # 初始化LLM模型
        self.llm = get_llm_model()
        
        # 初始化文档处理器
        self.document_processor = DocumentProcessor(files_dir, CHUNK_SIZE, OVERLAP)
        
        # 初始化图结构构建器
        self.struct_builder = GraphStructureBuilder(batch_size=BATCH_SIZE)
        
        # 初始化实体关系抽取器
        self.entity_extractor = EntityRelationExtractor(
            self.llm,
            system_template_build_graph,
            human_template_build_graph,
            entity_types,
            relationship_types,
            max_workers=MAX_WORKERS
        )
        
        # 初始化图写入器
        self.graph_writer = GraphWriter(self.graph, batch_size=BATCH_SIZE, max_workers=MAX_WORKERS)
        
        # 处理统计
        self.stats = {
            "start_time": None,
            "end_time": None,
            "total_time": 0,
            "files_processed": 0,
            "entities_integrated": 0,
            "relations_integrated": 0,
            "entities_updated": 0,
            "chunks_updated": 0
        }

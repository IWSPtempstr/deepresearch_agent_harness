# 由 src/deepresearch_agent/graph/indexing/embedding_manager.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, setup_embedding_tracking。
from rich.console import Console
from deepresearch_agent.models.get_models import get_embeddings_model
from deepresearch_agent.config.neo4jdb import get_db_manager
from deepresearch_agent.config.settings import EMBEDDING_BATCH_SIZE, MAX_WORKERS as DEFAULT_MAX_WORKERS

from deepresearch_agent.graph.indexing.embedding_queries import EmbeddingQueriesMixin
from deepresearch_agent.graph.indexing.embedding_updates import EmbeddingUpdateMixin
from deepresearch_agent.graph.indexing.embedding_marks import EmbeddingMarkMixin
from deepresearch_agent.graph.indexing.embedding_stats import EmbeddingStatsMixin

class EmbeddingManager(EmbeddingQueriesMixin, EmbeddingUpdateMixin, EmbeddingMarkMixin, EmbeddingStatsMixin):
    """
    Embedding管理器，支持增量更新嵌入向量。
    
    主要功能：
    1. 仅处理需要更新的实体和Chunk的Embedding
    2. 高效的批处理和并行计算
    3. 维护Embedding更新状态
    """
    

    def __init__(self, batch_size: int = 100, max_workers: int = 4):
        """
        初始化Embedding管理器
        
        Args:
            batch_size: 批处理大小
            max_workers: 并行工作线程数
        """
        self.console = Console()
        self.graph = get_db_manager().graph
        self.embeddings_model = get_embeddings_model()
        
        self.batch_size = batch_size or EMBEDDING_BATCH_SIZE
        self.max_workers = max_workers or DEFAULT_MAX_WORKERS
        
        # 性能监控
        self.embedding_time = 0
        self.db_time = 0
        self.total_time = 0
        
        # 处理统计
        self.stats = {
            "entity_updates": 0,
            "chunk_updates": 0,
            "total_updates": 0,
            "errors": 0
        }

    def setup_embedding_tracking(self):
        """设置Embedding更新追踪"""
        try:
            # 添加实体修改时间追踪
            self.graph.query("""
                MATCH (e:`__Entity__`)
                WHERE e.created_at IS NULL
                SET e.created_at = datetime()
            """)

            # 添加Chunk修改时间追踪
            self.graph.query("""
                MATCH (c:`__Chunk__`)
                WHERE c.created_at IS NULL
                SET c.created_at = datetime()
            """)

            self.console.print("[green]Embedding更新追踪设置完成[/green]")
            
        except Exception as e:
            self.console.print(f"[yellow]设置Embedding追踪时出错: {e}[/yellow]")

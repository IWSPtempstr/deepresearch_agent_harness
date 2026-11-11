# 由 src/deepresearch_agent/integrations/build/incremental_update.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
from rich.console import Console
from incremental_graph_builder import IncrementalGraphUpdater
from deepresearch_agent.graph.graph_consistency_validator import GraphConsistencyValidator
from deepresearch_agent.integrations.build.incremental.manual_edit_manager import ManualEditManager
from deepresearch_agent.graph.indexing.embedding_manager import EmbeddingManager
from deepresearch_agent.config.neo4jdb import get_db_manager
from deepresearch_agent.config.settings import FILES_DIR, MAX_WORKERS, BATCH_SIZE
from deepresearch_agent.integrations.build.incremental.incremental_update_scheduler import IncrementalUpdateScheduler

from deepresearch_agent.integrations.build.incremental_update_checks import UpdateChecksMixin
from deepresearch_agent.integrations.build.incremental_embeddings import UpdateEmbeddingsMixin
from deepresearch_agent.integrations.build.incremental_manual_sync import ManualSyncMixin
from deepresearch_agent.integrations.build.incremental_scheduler import SchedulerStatsMixin

class IncrementalUpdateManager(UpdateChecksMixin, UpdateEmbeddingsMixin, ManualSyncMixin, SchedulerStatsMixin):
    """
    增量更新管理器，整合所有增量更新功能，支持后台运行和定期检测。
    
    主要功能：
    1. 检测文件变更并更新图谱
    2. 更新实体和Chunk的Embedding
    3. 验证图谱一致性
    4. 处理社区检测和摘要生成
    5. 支持手动编辑同步
    6. 后台运行和定时调度
    """
    

    def __init__(self, files_dir: str = FILES_DIR, config=None):
        """
        初始化增量更新管理器
        
        Args:
            files_dir: 监控的文件目录
            config: 配置参数
        """
        self.console = Console()
        
        # 配置参数
        self.files_dir = files_dir
        self.config = config or {}
        
        # 初始化子组件
        self.graph = get_db_manager().graph
        self.updater = IncrementalGraphUpdater(files_dir)
        self.validator = GraphConsistencyValidator()
        self.edit_manager = ManualEditManager()
        self.embedding_manager = EmbeddingManager(batch_size=BATCH_SIZE, max_workers=MAX_WORKERS)
        
        # 初始化调度器
        self.scheduler = IncrementalUpdateScheduler(self.config)
        
        # 运行状态
        self.running = False
        self.stop_event = None
        
        # 性能统计
        self.stats = {
            "updates_performed": 0,
            "files_processed": 0,
            "entities_updated": 0,
            "communities_detected": 0,
            "errors": 0
        }

# 由 src/deepresearch_agent/integrations/build/incremental/manual_edit_manager.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
from rich.console import Console
from deepresearch_agent.config.neo4jdb import get_db_manager
from deepresearch_agent.config.settings import MAX_WORKERS, BATCH_SIZE

from deepresearch_agent.integrations.build.incremental.manual_edit_tracking import ManualEditTrackingMixin
from deepresearch_agent.integrations.build.incremental.manual_edit_detect import ManualEditDetectMixin
from deepresearch_agent.integrations.build.incremental.manual_edit_conflicts import ManualEditConflictMixin
from deepresearch_agent.integrations.build.incremental.manual_edit_stats import ManualEditStatsMixin

class ManualEditManager(ManualEditTrackingMixin, ManualEditDetectMixin, ManualEditConflictMixin, ManualEditStatsMixin):
    """
    手动编辑同步管理器，负责识别、保留和处理Neo4j数据库中的手动编辑。
    
    主要功能：
    1. 识别手动编辑的节点和关系
    2. 保留手动编辑，确保增量更新不会覆盖它
    3. 解决自动更新和手动编辑之间的冲突
    """
    

    def __init__(self):
        """初始化手动编辑同步管理器"""
        self.console = Console()
        self.graph = get_db_manager().graph
        
        # 设置并行工作线程数和批处理大小
        self.max_workers = MAX_WORKERS
        self.batch_size = BATCH_SIZE

        # 初始化实体和关系的必要属性
        self.initialize_entity_properties()

        # 性能计时器
        self.detection_time = 0
        self.sync_time = 0
        
        # 编辑统计
        self.edit_stats = {
            "manual_entities": 0,
            "manual_relations": 0,
            "preserved_edits": 0,
            "conflicts_resolved": 0
        }

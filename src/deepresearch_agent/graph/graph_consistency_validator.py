# 由 src/deepresearch_agent/graph/graph_consistency_validator.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__。
from rich.console import Console
from deepresearch_agent.config.neo4jdb import get_db_manager

from deepresearch_agent.graph.consistency_checks import ConsistencyChecksMixin
from deepresearch_agent.graph.consistency_repairs import ConsistencyRepairsMixin
from deepresearch_agent.graph.consistency_stats import ConsistencyStatsMixin

class GraphConsistencyValidator(ConsistencyChecksMixin, ConsistencyRepairsMixin, ConsistencyStatsMixin):
    """
    图谱一致性验证器，负责验证图谱结构和内容的完整性。
    
    主要功能：
    1. 检查孤立节点
    2. 验证关系链完整性
    3. 修复常见的一致性问题
    """
    

    def __init__(self):
        """初始化图谱一致性验证器"""
        self.console = Console()
        self.graph = get_db_manager().graph
        
        # 性能计时器
        self.validation_time = 0
        self.repair_time = 0
        
        # 验证统计
        self.validation_stats = {
            "orphan_entities": 0,
            "dangling_chunks": 0,
            "empty_chunks": 0,
            "broken_doc_links": 0,
            "broken_chunk_chains": 0,
            "total_issues": 0,
            "repaired_issues": 0
        }

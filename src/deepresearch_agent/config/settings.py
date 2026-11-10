"""DeepResearch 配置 facade（T-016 拆分）：全部名字经此模块导出，导入路径不变。"""

from deepresearch_agent.config.settings_core import *  # noqa: F401,F403
from deepresearch_agent.config.settings_sections import *  # noqa: F401,F403

# 私有 helper 与 star-import 不可达的名字显式 re-export（旧导入路径兼容）
from pathlib import Path  # noqa: F401  （原 settings 顶层名字，保持可导入）
import os  # noqa: F401
from typing import Optional  # noqa: F401
from dotenv import load_dotenv  # noqa: F401

from deepresearch_agent.config.settings_core import (  # noqa: F401
    _get_env_bool,
    _get_env_choice,
    _get_env_float,
    _get_env_int,
    _require_positive,
)

# 由 config/settings.py 拆分（T-016）：基础层（helpers/路径/基础设施）。
# 依赖方向：settings_sections -> settings_core ->（无）；settings.py 为 facade。
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 统一加载环境变量，确保配置来源一致
load_dotenv()


def _get_env_int(key: str, default: Optional[int]) -> Optional[int]:
    """获取整型环境变量，未设置时返回默认值"""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {key} 需要整数值，但当前为 {raw}") from exc


def _get_env_float(key: str, default: Optional[float]) -> Optional[float]:
    """获取浮点型环境变量，未设置时返回默认值"""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {key} 需要浮点值，但当前为 {raw}") from exc


def _get_env_bool(key: str, default: bool) -> bool:
    """获取布尔型环境变量，支持 true/false/1/0 表达式"""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _get_env_choice(key: str, choices: set[str], default: str) -> str:
    """获取有限集合中的字符串配置，未设置时返回默认值"""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value not in choices:
        raise ValueError(
            f"环境变量 {key} 必须为 {', '.join(sorted(choices))} 之一，但当前为 {raw}"
        )
    return value


def _require_positive(key: str, value: int) -> int:
    if value < 1:
        raise ValueError(f"环境变量 {key} 必须大于等于 1")
    return value


# ===== 基础路径设置 =====

BASE_DIR = Path(__file__).resolve().parent.parent  # deepresearch_agent包目录（src/deepresearch_agent）
PROJECT_ROOT = BASE_DIR.parent.parent  # 项目根目录（src 布局：包目录上溯两级）
FILES_DIR = PROJECT_ROOT / "files"
FILE_REGISTRY_PATH = PROJECT_ROOT / "file_registry.json"  # 文件注册表路径

# ===== 知识库与系统参数 =====

KB_NAME = "临床医学"  # 知识库主题，用于deepsearch
workers = _get_env_int("FASTAPI_WORKERS", 1) or 1  # 本地 MVP 强制单进程
LOCAL_MVP_SINGLE_WORKER = workers == 1

# ===== 本地 MVP 应用与持久化配置 =====

APP_ENV = os.getenv("APP_ENV", "local")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = _require_positive("APP_PORT", _get_env_int("APP_PORT", 8000) or 8000)
FRONTEND_ORIGINS = tuple(
    item.strip()
    # 默认同时放行 localhost 与 127.0.0.1：仓库内 vite.config.ts 将 dev server
    # 绑定在 http://127.0.0.1:5173，仅放行 localhost 会导致浏览器 CORS 预检被拒。
    for item in os.getenv(
        "FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if item.strip()
)
APP_DATABASE_URL = os.getenv(
    "APP_DATABASE_URL", "sqlite+aiosqlite:///./data/app.db"
).strip()
# 平台模式（P0-2）：非空时 Run 执行经 Redis 队列交给独立 worker 进程
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
# 可选 Kafka 传输层（T-010）：非空时唤醒信号改走 Kafka（优先于 REDIS_URL）；
# 纯能力展示/传输层可替换性验证，非生产必选项——DB 事实源 + 租约 + 扫描兜底语义不变
KAFKA_BOOTSTRAP_SERVERS = (os.getenv("KAFKA_BOOTSTRAP_SERVERS") or "").strip()
KAFKA_TOPIC = (os.getenv("KAFKA_TOPIC") or "dra-runs-queue").strip()
KAFKA_CONSUMER_GROUP = (os.getenv("KAFKA_CONSUMER_GROUP") or "dra-workers").strip()
# worker 进程同时在跑的 Run 数上限（API 进程内模式不设此限）
WORKER_CONCURRENCY = _get_env_int("WORKER_CONCURRENCY", 4) or 4
# 租约 TTL（秒）；心跳按 TTL/3 续约。接管窗口上界 ≈ TTL + 扫描间隔
RUN_LEASE_SECONDS = _get_env_int("RUN_LEASE_SECONDS", 30) or 30
# 过期租约扫描间隔（秒），worker 进程内运行
RECOVERY_SCAN_SECONDS = _get_env_float("RECOVERY_SCAN_SECONDS", 15.0) or 15.0
# LLM 网关（P1-5）：备用 provider（OpenAI 协议兼容），主失败按健康度退避切换
LLM_FALLBACK_BASE_URL = (os.getenv("LLM_FALLBACK_BASE_URL") or "").strip()
LLM_FALLBACK_API_KEY = (os.getenv("LLM_FALLBACK_API_KEY") or "").strip()
LLM_FALLBACK_MODEL = (os.getenv("LLM_FALLBACK_MODEL") or "").strip()
# 网关并发槽与排队超时（超时抛 LLM_RATE_LIMITED，对上为 429 语义）
LLM_MAX_CONCURRENT_CALLS = _get_env_int("LLM_MAX_CONCURRENT_CALLS", 8) or 8
LLM_QUEUE_TIMEOUT_SECONDS = _get_env_float("LLM_QUEUE_TIMEOUT_SECONDS", 30.0) or 30.0
# provider 连续失败的冷却基数（指数退避，上限 300s）
LLM_PROVIDER_COOLDOWN_SECONDS = _get_env_float("LLM_PROVIDER_COOLDOWN_SECONDS", 30.0) or 30.0
# 多租户（P2-8）：true 时无 API key 一律 401；false 时库中存在 api_keys 才强制
TENANT_AUTH_REQUIRED = _get_env_bool("TENANT_AUTH_REQUIRED", False)
# 学术检索（P2-11）：OpenAlex polite pool 标识（邮箱即可，免 key）
OPENALEX_MAILTO = (os.getenv("OPENALEX_MAILTO") or "").strip()
ARTIFACT_ROOT = Path(os.getenv("ARTIFACT_ROOT", PROJECT_ROOT / "data" / "artifacts")).expanduser()
SKILLS_ROOT = Path(os.getenv("SKILLS_ROOT", PROJECT_ROOT / "skills")).expanduser()
AUTO_RESUME_RUNS = _get_env_bool("AUTO_RESUME_RUNS", True)
# T-007：平台级 LLM 成本天花板（当日 output_tokens 总和；0=不启用）。
# 超限后 /ready 返回 503、新 Run 创建被拒——作品集保护阈值，非云账单保证
LLM_DAILY_TOKEN_CEILING = _get_env_int("LLM_DAILY_TOKEN_CEILING", 0) or 0
# T-005：启动期 schema 引导开关。true（默认）= 进程内 create_schema（本地零配置可跑）；
# false = 生产部署语义——DDL 只经单独的 `alembic upgrade head` 迁移步骤执行，
# API/worker 启动不执行任何竞争性 DDL（多副本安全）
SCHEMA_BOOTSTRAP_ON_STARTUP = _get_env_bool("SCHEMA_BOOTSTRAP_ON_STARTUP", True)


__all__ = [
    "APP_DATABASE_URL",
    "APP_ENV",
    "APP_HOST",
    "APP_PORT",
    "ARTIFACT_ROOT",
    "AUTO_RESUME_RUNS",
    "BASE_DIR",
    "FILES_DIR",
    "FILE_REGISTRY_PATH",
    "FRONTEND_ORIGINS",
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_CONSUMER_GROUP",
    "KAFKA_TOPIC",
    "KB_NAME",
    "LLM_DAILY_TOKEN_CEILING",
    "LLM_FALLBACK_API_KEY",
    "LLM_FALLBACK_BASE_URL",
    "LLM_FALLBACK_MODEL",
    "LLM_MAX_CONCURRENT_CALLS",
    "LLM_PROVIDER_COOLDOWN_SECONDS",
    "LLM_QUEUE_TIMEOUT_SECONDS",
    "LOCAL_MVP_SINGLE_WORKER",
    "OPENALEX_MAILTO",
    "PROJECT_ROOT",
    "RECOVERY_SCAN_SECONDS",
    "REDIS_URL",
    "RUN_LEASE_SECONDS",
    "SCHEMA_BOOTSTRAP_ON_STARTUP",
    "SKILLS_ROOT",
    "TENANT_AUTH_REQUIRED",
    "WORKER_CONCURRENCY",
    "workers",
]

# 私有 helper 显式 re-export（保持 deepresearch_agent.config.settings._get_env_* 旧路径）
__all__ += ["_get_env_bool", "_get_env_choice", "_get_env_float", "_get_env_int", "_require_positive"]

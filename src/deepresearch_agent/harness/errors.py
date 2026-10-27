"""Typed public errors; messages are safe to expose and never contain secrets.

契约约定：
- ``code`` 是稳定契约——对外展示文案由客户端按 code 映射，message 仅作
  日志与调试回退（中文写死是有意的；i18n 时改展示层而非本模块）。
- raise 点分布：NOT_FOUND / TENANT_* / CONFLICT 等由 backend Web 层抛出，
  HARNESS 层只抛执行域错误；DATABASE_UNAVAILABLE / PERSISTENCE_FAILED 为
  防御性预留（持久层故障当前以原始异常向上传播，由通用 500 兜底）。
"""

from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID_SOURCE_MODE = "INVALID_SOURCE_MODE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"
    ARTIFACT_PATH_INVALID = "ARTIFACT_PATH_INVALID"
    ARTIFACT_INTEGRITY_FAILED = "ARTIFACT_INTEGRITY_FAILED"
    SOURCE_POLICY_VIOLATION = "SOURCE_POLICY_VIOLATION"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TAVILY_API_KEY_MISSING = "TAVILY_API_KEY_MISSING"
    TAVILY_AUTH_FAILED = "TAVILY_AUTH_FAILED"
    TAVILY_RATE_LIMITED = "TAVILY_RATE_LIMITED"
    RETRIEVAL_TIMEOUT = "RETRIEVAL_TIMEOUT"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_ALL_PROVIDERS_FAILED = "LLM_ALL_PROVIDERS_FAILED"
    TENANT_UNAUTHENTICATED = "TENANT_UNAUTHENTICATED"
    TENANT_FORBIDDEN = "TENANT_FORBIDDEN"


class AppError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable, "details": self.details}


class RunCancelled(RuntimeError):
    """Internal cooperative-cancellation signal; never exposed as a failure."""


class RunLeaseLost(RuntimeError):
    """本地租约已被其他 Runtime 抢占；本实例必须立即停止写入，由接管者驱动状态机。"""

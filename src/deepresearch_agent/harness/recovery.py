"""Typed retry/replan decisions and startup recovery scanning."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import RunRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str


def classify_verification_failures(failures: list[str]) -> RecoveryDecision:
    kinds = set(failures)
    # Evidence/source failures cannot be repaired by rewriting prose. They must
    # return to planning and retrieval before a new report is generated.
    if kinds & {"min_evidence", "claim_support", "source_match"}:
        return RecoveryDecision("replan", ",".join(sorted(kinds)))
    if kinds and kinds.issubset({"citation_integrity", "required_section", "report_consistency", "source_diversity", "evidence_card_coverage", "rubric_coverage"}):
        return RecoveryDecision("repair_report", ",".join(sorted(kinds)))
    return RecoveryDecision("fail", ",".join(sorted(kinds)) or "unknown")


def classify_exception(exc: Exception) -> RecoveryDecision:
    """Keep transport retry, auth failure, and plan/contract recovery distinct."""
    if isinstance(exc, AppError):
        if exc.code in {ErrorCode.TAVILY_AUTH_FAILED, ErrorCode.TAVILY_API_KEY_MISSING, ErrorCode.SOURCE_POLICY_VIOLATION}:
            return RecoveryDecision("fail", exc.code.value)
        if exc.retryable or exc.code in {ErrorCode.TAVILY_RATE_LIMITED, ErrorCode.RETRIEVAL_TIMEOUT}:
            return RecoveryDecision("retry_same", exc.code.value)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return RecoveryDecision("retry_same", type(exc).__name__)
    return RecoveryDecision("fail", type(exc).__name__)


class RecoveryManager:
    # T-407：唯一可执行工作流；旧模式的活动 Run 不自动续跑（人工处理清单）。
    SUPPORTED_WORKFLOW_MODES = frozenset({"plan_execute_report"})

    def __init__(self, run_repository: RunRepository):
        self.run_repository = run_repository

    async def scan(self, *, auto_resume: bool = True) -> list[str]:
        interrupted_ids = await self.run_repository.mark_expired_leases_interrupted()
        if not auto_resume:
            # T-407：旧引擎过滤同样适用于非自动恢复分支（AUTO_RESUME_RUNS=False
            # 的部署）——过期租约的 legacy Run 同样保持原状，不重新投递。
            return await self._filter_supported(interrupted_ids)
        recoverable = await self.run_repository.list_recoverable()
        # T-407：旧引擎（deep_research）活动 Run 安全拒绝——不自动续跑、不取消、
        # 不静默换引擎；显式记录供人工处理清单。
        resumable: list[str] = []
        for run in recoverable:
            if run.status != "interrupted":
                continue
            if run.workflow_mode not in self.SUPPORTED_WORKFLOW_MODES:
                logger.warning(
                    "恢复扫描：旧工作流 Run 不自动续跑（run_id=%s workflow_mode=%s status=%s）"
                    "——列入人工处理清单，保持原状",
                    run.run_id, run.workflow_mode, run.status,
                )
                continue
            resumable.append(run.run_id)
        return resumable

    async def _filter_supported(self, run_ids: list[str]) -> list[str]:
        resumable: list[str] = []
        for run_id in run_ids:
            run = await self.run_repository.get(run_id)
            if run is not None and run.workflow_mode not in self.SUPPORTED_WORKFLOW_MODES:
                logger.warning(
                    "恢复扫描：旧工作流 Run 不重新投递（run_id=%s workflow_mode=%s）——保持原状",
                    run_id, run.workflow_mode,
                )
                continue
            resumable.append(run_id)
        return resumable

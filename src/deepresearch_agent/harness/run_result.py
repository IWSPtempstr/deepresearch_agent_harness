"""T-402 冻结的用户结果封装：ResearchRunResult（output-contract.md §3）。

这是 API/前端/CLI 的逻辑视图：可由现有 run/report/evidence 端点组装，不强制
新增同名端点、不要求一次返回全部证据。字段与状态语义以 output-contract.md 为
单一权威。

状态不变量（模型层强制，非法组合直接拒绝）：
- completed ⇒ result 为已验收报告且 verification.status=passed（required check
  的 unknown 不可支持 completed）；
- needs_user_input ⇒ 必须携带实际 clarification questions；
- failed/budget_exhausted ⇒ result 为 null（失败草稿只进显式 draft 视图）；
- result 在非 completed 时默认 null，模型不得自行写 completed——Run 状态由
  持久层决定，本模型只承载并校验。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .research_brief import _reject_unknown_version

RUN_RESULT_SCHEMA_VERSION = 1

# 与 harness.contracts.RunStatus 的全部持久状态一一对应（冻结视图不得漏状态，
# 否则 mid-cancel 等过渡态无法呈现）。
RUN_RESULT_STATUS = Literal[
    "queued", "context_building", "planning", "executing", "reporting", "verifying",
    "retrying", "replanning", "paused", "cancelling", "needs_user_input", "completed",
    "failed", "budget_exhausted", "cancelled", "interrupted",
]


class RunResultPayload(BaseModel):
    """result 字段（仅 completed；大报告可只带 artifact 引用）。"""

    model_config = ConfigDict(extra="forbid")

    format: Literal["markdown"] = "markdown"
    report: str
    artifact_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class RunVerificationView(BaseModel):
    """verification 字段视图。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "unknown", "not_run"] = "not_run"
    checks: list[dict[str, Any]] = Field(default_factory=list)


class RunErrorView(BaseModel):
    """error 字段（保留 AppError 业务码，不退化成类名）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = ""
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RunClarificationView(BaseModel):
    """clarification 字段（needs_user_input 必填）。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    questions: list[str] = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    answered_at: str | None = None
    answers_ref: str | None = None


class ResearchRunResult(BaseModel):
    """用户结果封装（逻辑视图；详见 output-contract.md §3 的状态约束表）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RUN_RESULT_SCHEMA_VERSION, ge=1)
    run_id: str
    status: RUN_RESULT_STATUS
    result: RunResultPayload | None = None
    verification: RunVerificationView = Field(default_factory=RunVerificationView)
    limitations: list[str] = Field(default_factory=list)
    clarification: RunClarificationView | None = None
    error: RunErrorView | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    # 失败草稿隔离：仅显式 draft 视图携带（verified=false 强制），永不混入 result。
    draft: dict[str, Any] | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("research_run_result", value, RUN_RESULT_SCHEMA_VERSION)
        return value

    @model_validator(mode="after")
    def _status_invariants(self) -> ResearchRunResult:
        if self.status == "completed":
            if self.result is None:
                raise ValueError("completed 必须携带已验收报告 result")
            if self.verification.status != "passed":
                raise ValueError(
                    f"completed 要求 verification.status=passed，当前 {self.verification.status!r}"
                    "（required check 的 unknown 不可支持 completed）"
                )
            if self.error is not None:
                raise ValueError("completed 不得同时携带 error（验收通过即无业务错误）")
        elif self.status == "needs_user_input" and self.clarification is None:
            raise ValueError("needs_user_input 必须携带实际 clarification questions")
        elif self.status in {"failed", "budget_exhausted"} and self.result is not None:
            raise ValueError(
                f"{self.status} 的 result 必须为 null（失败草稿只进显式 draft 视图）"
            )
        if self.draft is not None and self.draft.get("verified") is not False:
            raise ValueError("draft 视图必须显式 verified=false（未验证草稿不得伪称已验收）")
        return self

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchRunResult:
        """带版本门禁的反序列化入口。"""
        _reject_unknown_version("research_run_result", payload.get("schema_version"), RUN_RESULT_SCHEMA_VERSION)
        return cls.model_validate(payload)

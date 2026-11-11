"""HTTP DTOs for the phase-4 FastAPI boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(ApiModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody
    request_id: str


class SessionPatch(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = None


class MessageSend(ApiModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=20000)
    source_mode: SourceMode
    # T-407：唯一生产入口——旧标识 deep_research 在边界归一化为
    # plan_execute_report（历史读取返回持久层原值，不受影响）。
    workflow_mode: WorkflowMode = WorkflowMode.PLAN_EXECUTE_REPORT
    report_type: Literal["brief", "long_document"] = "brief"

    @field_validator("workflow_mode", mode="before")
    @classmethod
    def _normalize_legacy_workflow(cls, value: Any) -> Any:
        if isinstance(value, str) and value == WorkflowMode.DEEP_RESEARCH.value:
            return WorkflowMode.PLAN_EXECUTE_REPORT
        return value


class ClarificationSubmit(ApiModel):
    content: str = Field(min_length=1, max_length=10000)


class RunAccepted(ApiModel):
    message_id: str
    run_id: str
    status: str
    events_url: str
    created: bool


class RunControl(ApiModel):
    run_id: str
    status: str


class MemoryLifecycleUpdate(ApiModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    status: Literal["candidate", "active", "rejected", "archived", "expired"] | None = None
    expires_at: str | None = Field(default=None, max_length=40)


class MemoryCreate(ApiModel):
    target: Literal["user", "project"]
    content: str = Field(min_length=1, max_length=10000)
    kind: Literal["preference", "fact", "decision", "lesson", "note"] = "note"
    provenance_refs: list[str] = Field(default_factory=list, max_length=20)
    activate: bool = False


class SkillActionResponse(ApiModel):
    accepted: bool
    reason: str

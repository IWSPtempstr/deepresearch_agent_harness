"""HTTP DTOs for the phase-4 FastAPI boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # S2：创建时冻结的来源集合（可选；缺省 = [source_mode]）。首个主通道由
    # source_mode 决定，集合内不得包含未知/重复值。
    sources: list[SourceMode] | None = Field(default=None, max_length=4)
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

    @field_validator("source_mode", mode="before")
    @classmethod
    def _normalize_legacy_source(cls, value: Any) -> Any:
        # S1：scholar 不再是独立通道（学术 API 并入 web 聚合通道）。
        # 历史 Run 的 scholar 记录只读保留；新请求在边界归一化，不拒绝旧客户端。
        if isinstance(value, str) and value == "scholar":
            return SourceMode.WEB
        return value

    @field_validator("sources", mode="before")
    @classmethod
    def _normalize_sources(cls, value: Any) -> Any:
        """只做别名归一（scholar→web），不静默去重/截断：重复与超长都要 422。"""
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("sources 必须是来源列表")
        normalized = [
            SourceMode.WEB if str(item) == "scholar" else SourceMode(item)
            for item in value
        ]
        if not normalized:
            raise ValueError("sources 不得为空（需要至少一个来源）")
        return normalized

    @model_validator(mode="after")
    def _sources_include_primary(self) -> "MessageSend":
        if self.sources is None:
            return self
        if len(self.sources) != len(set(self.sources)):
            # scholar→web 归一化后产生的重复同样拒绝（别名冲突也是冲突）。
            raise ValueError("sources 不得重复")
        if self.source_mode not in self.sources:
            raise ValueError("sources 必须包含主通道 source_mode")
        return self


class ClarificationSubmit(ApiModel):
    content: str = Field(min_length=1, max_length=10000)
    # BC-01：幂等键（客户端生成）；缺省时按 Run/修订/回答内容派生。
    client_clarification_id: str | None = Field(default=None, min_length=1, max_length=128)
    # 前端回传所回答的请求 ID；与服务端当前请求不一致时 409（避免回答错版本）。
    question_id: str | None = Field(default=None, max_length=64)


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

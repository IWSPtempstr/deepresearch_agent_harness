"""Run control, durable event streaming, evidence and report reads."""

import hashlib
import json
import re

from fastapi import APIRouter, Depends, Header, Query, Request
from sse_starlette.sse import EventSourceResponse

from backend.app.dependencies import get_database, get_event_stream, get_run_service
from backend.app.schemas import ClarificationSubmit, RunControl
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.clarifications import public_view, request_from_config
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EvidenceRepository,
    MessageRepository, RunRepository,
)
from deepresearch_agent.context import ArtifactEditContextBuilder

router = APIRouter(prefix="/runs", tags=["runs"])

_EVIDENCE_ANNEX = re.compile(r"(?ms)^##\s+全量证据索引\s*.*$")


def _report_presentation(content: str | None, *, evidence_count: int = 0) -> tuple[str | None, str, list[dict[str, object]]]:
    """Separate the readable report from legacy inline evidence annexes."""
    if not content:
        return None, "normal", []
    report_mode = "budget_fallback" if "预算保护模式" in content[:240] else "normal"
    body = _EVIDENCE_ANNEX.sub("", content).strip()
    if report_mode == "budget_fallback":
        body = _compact_fallback_report(body, evidence_count=evidence_count)
    sections: list[dict[str, object]] = []
    for index, match in enumerate(re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*$", body)):
        title = match.group(2).strip()
        sections.append({"id": f"report-section-{index}", "title": title, "level": len(match.group(1))})
    return body, report_mode, sections


def _compact_fallback_report(content: str, *, evidence_count: int) -> str:
    """Make legacy deterministic card dumps readable without mutating history."""
    output: list[str] = []
    kept_in_section = 0
    omitted_in_section = 0

    def flush_omitted() -> None:
        nonlocal omitted_in_section
        if omitted_in_section:
            output.extend(["", f"本节另有 {omitted_in_section} 条证据保存在“证据”视图中。"])
            omitted_in_section = 0

    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            flush_omitted()
            kept_in_section = 0
            title = stripped[3:].strip()
            output.extend(["", f"## {title if len(title) <= 46 else title[:46].rstrip() + '…'}"])
            continue
        if raw.startswith("- "):
            if kept_in_section >= 4:
                omitted_in_section += 1
                continue
            citations = re.findall(r"\[(?:\^)?ev_[A-Za-z0-9_-]+\]", stripped)
            claim = re.sub(r"\[(?:\^)?ev_[A-Za-z0-9_-]+\]", "", stripped[2:])
            claim = _evidence_summary(re.sub(r"[`#>*_|]+", " ", claim), limit=360)
            if not claim:
                continue
            output.append(f"- {claim}{(' ' + ' '.join(citations)) if citations else ''}")
            kept_in_section += 1
            continue
        if raw.startswith("  - "):
            continue
        output.append(raw.rstrip())
    flush_omitted()
    if evidence_count:
        output.extend(["", f"> 完整证据台账共 {evidence_count} 条，请在报告顶部切换到“证据”视图核查来源与摘要。"])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()


def _evidence_summary(value: str | None, *, limit: int = 480) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)skip to content|navigation menu|sign in|cookie settings|loading(?:\s+loading)+|documentation index",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def run_dict(run):
    config = json.loads(run.config_snapshot_json or "{}")
    return {
        "run_id": run.run_id, "session_id": run.session_id, "trigger_message_id": run.trigger_message_id,
        "source_mode": run.source_mode, "workflow_mode": run.workflow_mode, "status": run.status,
        "current_stage": run.current_stage, "usage": json.loads(run.usage_json or "{}"),
        "error_code": run.error_code, "error_message": run.error_message,
        "cancellation_requested": bool(run.cancellation_requested),
        "pause_requested": bool(config.get("pause_requested")), "created_at": run.created_at,
        "started_at": run.started_at, "completed_at": run.completed_at, "updated_at": run.updated_at,
        # BC-01：实际澄清问题（白名单视图；旧 Run 的通用提示作为回退）
        "clarification": public_view(request_from_config(config)),
    }


async def require_run(database, run_id: str):
    run = await RunRepository(database).get(run_id)
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND, "Run 不存在")
    # P2-8：Run 归属租户校验；default 兜底租户放行（单机兼容），其余强制一致
    from backend.app.tenancy import DEFAULT_TENANT_ID, current_tenant_id

    tenant_id = current_tenant_id()
    if tenant_id != DEFAULT_TENANT_ID and run.tenant_id != tenant_id:
        raise AppError(ErrorCode.TENANT_FORBIDDEN, "Run 不属于当前租户", retryable=False)
    return run


@router.get("/{run_id}")
async def get_run(run_id: str, database=Depends(get_database)):
    return run_dict(await require_run(database, run_id))


@router.post("/{run_id}/cancel", response_model=RunControl)
async def cancel_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.cancel(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 已结束或当前不能取消")
    return RunControl(run_id=run_id, status="cancelling")


@router.post("/{run_id}/pause", response_model=RunControl)
async def pause_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.pause(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 已结束、已暂停或当前不能暂停")
    return RunControl(run_id=run_id, status="pausing")


@router.post("/{run_id}/resume", response_model=RunControl)
async def resume_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.resume(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 当前不能恢复")
    return RunControl(run_id=run_id, status="resuming")


@router.post("/{run_id}/clarifications", response_model=RunControl)
async def clarify_run(run_id: str, payload: ClarificationSubmit, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    accepted, status = await run_service.submit_clarification(
        run_id, content=payload.content,
        client_clarification_id=payload.client_clarification_id,
        question_id=payload.question_id,
    )
    if not accepted:
        raise AppError(ErrorCode.CONFLICT, "Run 当前不等待澄清")
    return RunControl(run_id=run_id, status=status)


@router.get("/{run_id}/events")
async def run_events(
    run_id: str, request: Request, after_event_id: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    database=Depends(get_database), event_stream=Depends(get_event_stream),
):
    await require_run(database, run_id)
    cursor = after_event_id
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return EventSourceResponse(event_stream.stream(run_id, after_event_id=cursor), ping=None, headers={"Cache-Control": "no-cache"})


@router.get("/{run_id}/evidence")
async def get_evidence(run_id: str, database=Depends(get_database)):
    await require_run(database, run_id)
    items = await EvidenceRepository(database).list_for_run(run_id)
    return {"items": [
        {"evidence_id": item.evidence_id, "run_id": item.run_id, "task_id": item.task_id, "tool_call_id": item.tool_call_id,
         "source_mode": item.source_mode, "provider": item.provider, "source_id": item.source_id, "title": item.title,
         "summary": item.summary, "metadata": json.loads(item.metadata_json or "{}"), "content_hash": item.content_hash,
         "artifact_id": item.artifact_id, "score": item.score, "created_at": item.created_at}
        for item in items
    ], "total": len(items)}


def _public_quality_view(assessment: dict) -> dict | None:
    """只暴露白名单诊断字段；provider 异常文本等内部信息不出网。"""
    dimensions = assessment.get("dimensions")
    if not isinstance(dimensions, dict):
        return None
    public_dimensions = {}
    for name, entry in dimensions.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        reason = entry.get("reason")
        public_dimensions[name] = {
            "status": status if status in {"pass", "fail", "unknown", "not_run"} else "unknown",
            "reason": str(reason if isinstance(reason, str) else "")[:200],
        }
    status = assessment.get("status")
    usage = assessment.get("usage") if isinstance(assessment.get("usage"), dict) else {}
    return {
        "status": status if status in {"pass", "fail", "unknown", "not_run"} else "unknown",
        "dimensions": public_dimensions,
        "judge_model": assessment.get("judge_model") if isinstance(assessment.get("judge_model"), str) else None,
        "prompt_version": assessment.get("prompt_version") if isinstance(assessment.get("prompt_version"), str) else None,
        "note": str(assessment.get("note") or "")[:500],
        "advisory_only": bool(assessment.get("advisory_only", True)),
        # usage 只透出计数（不透明 reason 可能含异常文本）
        "usage": {"calls": int(usage.get("calls", 0) or 0)},
    }


async def _quality_assessment_for(database, run_id: str, run_status: str) -> dict | None:
    """RG-04：报告质量维度（诊断）从最新 checkpoint 的报告快照读取。

    只读、无副作用；仅 **completed**（Contract 通过）的 Run 暴露——失败/草稿/
    中间态运行的诊断不得对外呈现为"已验收报告的附属信息"。缺失/损坏一律
    返回 None（不造分、不阻塞报告读取；任何嵌套形状都不抛异常）。
    """
    if run_status != "completed":
        return None
    try:
        checkpoint = await CheckpointRepository(database).latest(run_id)
    except Exception:  # noqa: BLE001 —— 诊断字段失败不影响报告主链路
        return None
    if checkpoint is None:
        return None
    try:
        state = json.loads(checkpoint.state_json or "{}")
        if not isinstance(state, dict):
            return None
        workflow_state = state.get("workflow_state")
        if not isinstance(workflow_state, dict):
            return None
        report_result = workflow_state.get("report_result")
        if not isinstance(report_result, dict):
            return None
        metrics = report_result.get("report_metrics")
        if not isinstance(metrics, dict):
            return None
        assessment = metrics.get("quality_assessment")
        if not isinstance(assessment, dict):
            return None
        return _public_quality_view(assessment)
    except Exception:  # noqa: BLE001 —— 诊断读取全程容错
        return None


@router.get("/{run_id}/report")
async def get_report(run_id: str, database=Depends(get_database)):
    run = await require_run(database, run_id)
    message = await MessageRepository(database).get_assistant_for_run(run_id)
    artifact = await ArtifactRepository(database).get_report(run_id)
    checks = await ContractRepository(database).list_for_run(run_id)
    evidence = await EvidenceRepository(database).list_for_run(run_id)
    content, report_mode, sections = _report_presentation(message.content if message else None, evidence_count=len(evidence))
    return {
        "run_id": run_id, "status": run.status, "content": content,
        "report_mode": report_mode, "sections": sections,
        "evidence_index": [
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "source_id": item.source_id,
                "source_mode": item.source_mode,
                "provider": item.provider,
                "summary": _evidence_summary(item.summary),
                "url": (json.loads(item.metadata_json or "{}").get("url") or item.source_id),
                "score": item.score,
            }
            for item in evidence
        ],
        "artifact": None if artifact is None else {"artifact_id": artifact.artifact_id, "relative_path": artifact.relative_path, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
        "verification": [
            {"check_id": check.check_id, "kind": check.kind, "required": bool(check.required), "passed": None if check.passed is None else bool(check.passed), "evidence": json.loads(check.evidence_json or "{}")}
            for check in checks
        ],
        # RG-04：质量维度是展示/开发诊断（advisory-only），不参与 completed 判定；
        # 仅 completed（已验收）运行暴露
        "quality_assessment": await _quality_assessment_for(database, run_id, run.status),
    }


@router.get("/{run_id}/context")
async def get_context_inspector(
    run_id: str, database=Depends(get_database), run_service=Depends(get_run_service),
):
    """Return the durable, structured Context Pack without exposing the raw model prompt."""
    run = await require_run(database, run_id)
    checkpoint = await CheckpointRepository(database).latest(run_id)
    if checkpoint is None:
        return {"run_id": run_id, "ready": False, "status": run.status}
    if not CheckpointRepository.verify(checkpoint):
        raise AppError(ErrorCode.CONFLICT, "Run 上下文 checkpoint 完整性校验失败")

    state = json.loads(checkpoint.state_json or "{}")
    snapshot = state.get("context_snapshot") or {}
    artifact_edit = snapshot.get("artifact_edit")
    artifact_verification = None
    if artifact_edit:
        message = await MessageRepository(database).get_assistant_for_run(run_id)
        current_sections = {
            section.heading: section.sha256
            for section in ArtifactEditContextBuilder.parse_sections(message.content if message else "")
        }
        preserved = []
        mapped = bool(artifact_edit.get("citation_mapping"))
        for section in artifact_edit.get("preserve_sections", []):
            actual = current_sections.get(section["heading"])
            # BC-04：一旦存在 citation mapping，保留章节必须与映射后摘要一致；
            # 仅匹配基线摘要意味着旧引用未被改写，不能算通过。
            base_sha = section["sha256"]
            mapped_sha = section.get("mapped_sha256")
            expected = mapped_sha if mapped else base_sha
            passed = actual == expected if expected else False
            preserved.append({
                "heading": section["heading"], "expected_sha256": expected,
                "base_sha256": base_sha, "mapped_sha256": mapped_sha,
                "actual_sha256": actual, "passed": passed,
            })
        target_before = hashlib.sha256(artifact_edit.get("target_content", "").encode("utf-8")).hexdigest()
        target_after = current_sections.get(artifact_edit.get("target_heading"))
        artifact_verification = {
            "available": message is not None,
            "target_heading": artifact_edit.get("target_heading"),
            "target_changed": bool(target_after and target_after != target_before),
            "preserved_sections": preserved,
            "all_preserved": bool(preserved) and all(item["passed"] for item in preserved),
        }

    return {
        "run_id": run_id, "ready": bool(snapshot), "status": run.status,
        "checkpoint": {
            "version": checkpoint.version, "stage": checkpoint.stage,
            "state_hash": checkpoint.state_hash, "verified": True,
            "created_at": checkpoint.created_at,
        },
        "stable_snapshot_id": snapshot.get("stable_snapshot_id"),
        "memory_snapshot_version": snapshot.get("memory_snapshot_version", 0),
        "stable_blocks": snapshot.get("stable_blocks", []),
        "dynamic_blocks": snapshot.get("dynamic_blocks", []),
        "retrieval_trace": snapshot.get("retrieval_trace", []),
        "token_usage_by_block": snapshot.get("token_usage_by_block", {}),
        "total_input_tokens": snapshot.get("total_input_tokens", 0),
        "curated_memory": snapshot.get("curated_memory", []),
        "recent_messages": snapshot.get("recent_messages", []),
        "session_summary": snapshot.get("session_summary") or {},
        "historical_recall_searched": bool(snapshot.get("historical_recall_searched")),
        "historical_recall": snapshot.get("historical_recall", []),
        "used_session_ids": snapshot.get("used_session_ids", []),
        "selected_skill": snapshot.get("selected_skill"),
        "active_skill_policy": snapshot.get("active_skill_policy"),
        "skill_policy_consumers": snapshot.get("skill_policy_consumers", []),
        "artifact_edit": artifact_edit,
        "artifact_verification": artifact_verification,
    }

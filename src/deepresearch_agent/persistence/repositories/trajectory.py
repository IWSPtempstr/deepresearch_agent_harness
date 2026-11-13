"""Repositories for checkpoints, evidence, contracts and artifact metadata."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import func, select, update

from deepresearch_agent.harness.contracts import ContractCheckData, EvidenceData
from deepresearch_agent.harness.versioning import CHECKPOINT_SCHEMA_VERSION
from deepresearch_agent.persistence.artifact_store import StoredArtifact
from deepresearch_agent.persistence.database import Database
from deepresearch_agent.persistence.models import ArtifactModel, CheckpointModel, ContractCheckModel, EvidenceModel, PlanModel, TaskModel, ToolCallModel

from .utils import json_text, new_id, utc_now_iso


class PlanTaskToolRepository:
    """Persist a complete executable plan and idempotent tool-call intent/result."""

    def __init__(self, database: Database):
        self.database = database

    async def save_plan(self, *, run_id: str, plan_id: str, version: int, status: str, plan: dict[str, Any], tasks: list[dict[str, Any]], source_mode: str) -> PlanModel:
        now = utc_now_iso()
        async with self.database.transaction() as session:
            plan_model = await session.get(PlanModel, plan_id)
            if plan_model is None:
                plan_model = PlanModel(plan_id=plan_id, run_id=run_id, version=version, status=status, plan_json=json_text(plan), created_at=now, updated_at=now)
                session.add(plan_model)
                await session.flush()
            else:
                plan_model.version = version
                plan_model.status = status
                plan_model.plan_json = json_text(plan)
                plan_model.updated_at = now
            for item in tasks:
                task_model = await session.get(TaskModel, item["task_id"])
                if task_model is None:
                    session.add(TaskModel(task_id=item["task_id"], run_id=run_id, plan_id=plan_id, task_type=item["task_type"], source_mode=source_mode, status=item.get("status", "pending"), task_json=json_text(item), created_at=now, updated_at=now))
                else:
                    task_model.status = item.get("status", task_model.status)
                    task_model.task_json = json_text(item)
                    task_model.updated_at = now
            return plan_model

    async def prepare_tool_call(self, *, tool_call_id: str, run_id: str, task_id: Optional[str], tool_name: str, source_mode: str, args: dict[str, Any]) -> tuple[ToolCallModel, bool]:
        async with self.database.transaction() as session:
            existing = await session.get(ToolCallModel, tool_call_id)
            if existing:
                return existing, False
            model = ToolCallModel(tool_call_id=tool_call_id, run_id=run_id, task_id=task_id, tool_name=tool_name, source_mode=source_mode, status="prepared", args_json=json_text(args), created_at=utc_now_iso())
            session.add(model)
            return model, True

    async def complete_tool_call(self, tool_call_id: str, *, result: Any = None, error_code: Optional[str] = None) -> bool:
        values = {"status": "failed" if error_code else "completed", "result_json": json_text(result) if result is not None else None, "error_code": error_code, "completed_at": utc_now_iso()}
        async with self.database.transaction() as session:
            outcome = await session.execute(update(ToolCallModel).where(ToolCallModel.tool_call_id == tool_call_id, ToolCallModel.status != "completed").values(**values))
            return outcome.rowcount == 1

    async def get_tool_call(self, tool_call_id: str) -> Optional[ToolCallModel]:
        async with self.database.sessions() as session:
            return await session.get(ToolCallModel, tool_call_id)


class CheckpointRepository:
    def __init__(self, database: Database):
        self.database = database

    async def save(self, run_id: str, stage: str, state: dict[str, Any]) -> CheckpointModel:
        state_payload = {"schema_version": CHECKPOINT_SCHEMA_VERSION, **state}
        serialized = json_text(state_payload)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        async with self.database.transaction() as session:
            current = (await session.execute(select(func.max(CheckpointModel.version)).where(CheckpointModel.run_id == run_id))).scalar_one()
            model = CheckpointModel(checkpoint_id=new_id("chk"), run_id=run_id, version=(current or 0) + 1, stage=stage, state_json=serialized, state_hash=digest, schema_version=CHECKPOINT_SCHEMA_VERSION, created_at=utc_now_iso())
            session.add(model)
            await session.flush()
            return model

    async def latest(self, run_id: str) -> Optional[CheckpointModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(CheckpointModel).where(CheckpointModel.run_id == run_id).order_by(CheckpointModel.version.desc()).limit(1))).scalar_one_or_none()

    @staticmethod
    def verify(checkpoint: CheckpointModel) -> bool:
        return hashlib.sha256(checkpoint.state_json.encode("utf-8")).hexdigest() == checkpoint.state_hash


class EvidenceRepository:
    def __init__(self, database: Database):
        self.database = database

    async def upsert(self, data: EvidenceData, *, metadata: Optional[dict[str, Any]] = None) -> EvidenceModel:
        async with self.database.transaction() as session:
            existing = await session.get(EvidenceModel, data.evidence_id)
            if existing:
                return existing
            from backend.app.tenancy import current_tenant_id

            model = EvidenceModel(evidence_id=data.evidence_id, tenant_id=current_tenant_id(), run_id=data.run_id, task_id=data.task_id, tool_call_id=data.tool_call_id, source_mode=data.source_mode.value, provider=data.provider, source_id=data.source_id, title=data.title, summary=data.summary, metadata_json=json_text(metadata or {}), content_hash=data.content_hash, artifact_id=data.artifact_id, score=data.score, created_at=utc_now_iso())
            session.add(model)
            return model

    async def inherit(
        self, base: EvidenceModel, *, run_id: str, evidence_id: str, metadata: dict[str, Any],
    ) -> EvidenceModel:
        """BC-04：把基线 Run 的证据合法归入新 Run（新 ID + 明确 provenance）。

        不复制/移动基线行；新行保留原 source/summary/content_hash 与来源元数据，
        额外写入 inherited/base_run_id/base_evidence_id 等字段供审计。
        """
        async with self.database.transaction() as session:
            existing = await session.get(EvidenceModel, evidence_id)
            if existing:
                # 幂等重放必须对应同一新 Run / 同一基线内容；否则是 ID 碰撞或
                # 损坏数据，静默复用会把引用指向错误证据。
                from deepresearch_agent.harness.errors import AppError, ErrorCode

                try:
                    existing_meta = json.loads(existing.metadata_json or "{}")
                except (TypeError, ValueError):
                    existing_meta = {}
                if (
                    existing.run_id != run_id
                    or existing.content_hash != base.content_hash
                    or existing.source_id != base.source_id
                    or not isinstance(existing_meta, dict)
                    or existing_meta.get("inherited") is not True
                    or existing_meta.get("base_evidence_id") != metadata.get("base_evidence_id")
                ):
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                        "继承证据 ID 冲突：已有记录与本次基线不一致",
                        details={"evidence_id": evidence_id},
                    )
                return existing
            from backend.app.tenancy import current_tenant_id

            model = EvidenceModel(
                evidence_id=evidence_id, tenant_id=current_tenant_id(), run_id=run_id,
                task_id=(f"inherited:{base.task_id}" if base.task_id else "inherited")[:64],
                tool_call_id=base.tool_call_id, source_mode=base.source_mode,
                provider=base.provider, source_id=base.source_id, title=base.title,
                summary=base.summary, metadata_json=json_text(metadata),
                content_hash=base.content_hash, artifact_id=base.artifact_id,
                score=base.score, created_at=utc_now_iso(),
            )
            session.add(model)
            return model

    async def list_for_run(self, run_id: str) -> list[EvidenceModel]:
        async with self.database.sessions() as session:
            return list((await session.execute(select(EvidenceModel).where(EvidenceModel.run_id == run_id, EvidenceModel.invalidated_at.is_(None)).order_by(EvidenceModel.created_at))).scalars())


class ContractRepository:
    def __init__(self, database: Database):
        self.database = database

    async def upsert(self, data: ContractCheckData) -> ContractCheckModel:
        payload = {"observed": data.observed, "explanation": data.explanation, "artifact_refs": data.artifact_refs, "schema_version": data.schema_version}
        async with self.database.transaction() as session:
            model = await session.get(ContractCheckModel, data.check_id)
            if model is None:
                model = ContractCheckModel(check_id=data.check_id, run_id=data.run_id, kind=data.kind, required=int(data.required), threshold_json=json_text(data.threshold) if data.threshold is not None else None, verifier=data.verifier, verifier_version=data.verifier_version, passed=None if data.passed is None else int(data.passed), evidence_json=json_text(payload), created_at=utc_now_iso())
                session.add(model)
            else:
                model.passed = None if data.passed is None else int(data.passed)
                model.evidence_json = json_text(payload)
            return model

    async def required_checks_passed(self, run_id: str) -> bool:
        async with self.database.sessions() as session:
            checks = list((await session.execute(select(ContractCheckModel).where(ContractCheckModel.run_id == run_id, ContractCheckModel.required == 1))).scalars())
            return bool(checks) and all(check.passed == 1 for check in checks)

    async def list_for_run(self, run_id: str) -> list[ContractCheckModel]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(ContractCheckModel).where(ContractCheckModel.run_id == run_id).order_by(ContractCheckModel.kind)
            )
            return list(result.scalars())


class ArtifactRepository:
    def __init__(self, database: Database):
        self.database = database

    async def record(self, artifact: StoredArtifact, *, run_id: Optional[str] = None) -> ArtifactModel:
        async with self.database.transaction() as session:
            model = (await session.execute(select(ArtifactModel).where(ArtifactModel.relative_path == artifact.relative_path))).scalar_one_or_none()
            if model is None:
                model = ArtifactModel(artifact_id=artifact.artifact_id, run_id=run_id, relative_path=artifact.relative_path, mime_type=artifact.mime_type, size_bytes=artifact.size_bytes, sha256=artifact.sha256, created_at=utc_now_iso())
                session.add(model)
            else:
                model.run_id = run_id
                model.mime_type = artifact.mime_type
                model.size_bytes = artifact.size_bytes
                model.sha256 = artifact.sha256
            return model

    async def get_report(self, run_id: str) -> Optional[ArtifactModel]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(ArtifactModel)
                .where(ArtifactModel.run_id == run_id, ArtifactModel.mime_type.like("text/markdown%"))
                .order_by(ArtifactModel.created_at.desc())
            )
            return result.scalars().first()

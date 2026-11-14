"""BC-04：报告章节修订的证据继承、引用映射与 Contract 闭环。

验收：
- 显式修订目标（base_run_id/target_heading/target_sha256）与基线完整性校验；
- 旧证据以新 ID 合法归入新 Run（provenance 指向基线，不移动基线行）；
- 保留章节引用映射到新 Run 证据，citation_integrity/claim_support 可通过；
- 越权基线、hash 变更、保留章节篡改被拒绝。
"""

import json

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.context import ArtifactEditContextBuilder
from deepresearch_agent.context.artifact_edit import ArtifactEditTargetAmbiguous
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.contracts import ContractCheckData, ContractEvaluator, SourceMode as ContractSourceMode
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.harness.evidence import EvidenceLedger
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository, SessionRepository,
)


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bc04.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


def _runtime(database, tmp_path):
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database),
        workflow_factory=lambda context, events=None: None,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        artifact_repository=ArtifactRepository(database), lease_seconds=5,
    )


async def _create_run(database, session_id: str, client_id: str):
    message, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session_id, role="user", content="研究报告", client_message_id=client_id),
        RunCreate(
            session_id=session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            config_snapshot={"min_evidence": 1}, budget={},
        ),
    )
    return message, run


def _result(index: int, text: str) -> RetrievalResult:
    return RetrievalResult(
        result_id=f"raw-{index}", granularity="Chunk", evidence=text,
        metadata=RetrievalMetadata(source_id=f"doc-{index}", source_type="chunk", content_hash=f"{index:064d}"),
        source="local_search", source_mode="graphrag", score=0.9,
    )


async def _seed_completed_base(database, tmp_path, session_id: str, report_builder):
    ledger = EvidenceLedger(EvidenceRepository(database))
    _, base_run = await _create_run(database, session_id, "bc04-base")
    evidence = await ledger.record_results(
        run_id=base_run.run_id, task_id="task-base", tool_call_id="call-base",
        provider="graphrag", results=[_result(1, "基线证据一"), _result(2, "基线证据二")],
    )
    # 稳定证据 ID 由账本生成，报告引用它们
    old_ids = [item.evidence_id for item in evidence]
    report = report_builder(old_ids)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.write_text(
        f"{base_run.run_id}/report/final.md", report, mime_type="text/markdown; charset=utf-8",
    )
    await ArtifactRepository(database).record(artifact, run_id=base_run.run_id)
    await ContractRepository(database).upsert(ContractCheckData(
        check_id=f"contract_{base_run.run_id}_min_evidence", run_id=base_run.run_id,
        kind="min_evidence", required=True, passed=True,
        verifier="test", verifier_version="1",
    ))
    await RunRepository(database).complete_verified(
        base_run.run_id, assistant_content=report, usage={},
    )
    return base_run, old_ids


def _base_report(old_ids: list[str]) -> str:
    return (
        "# 摘要\n\n"
        f"基线结论需要证据支持 [{old_ids[0]}]。\n\n"
        "# 方法\n\n"
        "这是旧的、需要替换的方法说明。\n\n"
        "# 来源\n\n"
        f"来源说明保留原样 [{old_ids[1]}]。"
    )


@pytest.mark.asyncio
async def test_revision_inherits_evidence_and_contract_passes(database, tmp_path):
    session = await SessionRepository(database).create(SessionCreate(title="bc04"))
    base_run, old_ids = await _seed_completed_base(database, tmp_path, session.session_id, _base_report)

    _, revision = await _create_run(database, session.session_id, "bc04-revision")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    contract = await builder.build(
        session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
    )
    assert contract is not None
    assert contract["base_run_id"] == base_run.run_id
    assert contract["target_heading"] == "方法" and contract["target_sha256"]
    assert sorted(contract["base_citations"]) == sorted(old_ids)

    # 新 Run 自己检索到一条证据（目标章节使用）
    new_evidence = await EvidenceLedger(EvidenceRepository(database)).record_results(
        run_id=revision.run_id, task_id="task-new", tool_call_id="call-new",
        provider="graphrag", results=[_result(3, "修订后的新方法证据")],
    )
    new_id = new_evidence[0].evidence_id
    context = RunContext(
        run_id=revision.run_id, session_id=session.session_id,
        trigger_message_id=revision.trigger_message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="reporting", original_query="修改方法章节",
        context_snapshot={"artifact_edit": contract},
    )
    runtime = _runtime(database, tmp_path)
    await runtime._prepare_artifact_edit(context)
    updated = context.context_snapshot["artifact_edit"]
    assert set(updated["citation_mapping"]) == set(old_ids)
    assert all(old != new for old, new in updated["citation_mapping"].items())
    assert updated["inherited_evidence_ids"]

    merged = runtime._apply_artifact_edit(context, f"## 方法\n\n修订后的方法说明 [{new_id}]。")
    for old in old_ids:
        assert f"[{old}]" not in merged, "保留章节旧引用必须映射到新 Run 证据"
    assert f"[{new_id}]" in merged
    assert "修订后的方法说明" in merged and "旧的、需要替换的方法说明" not in merged

    revision_evidence = await EvidenceRepository(database).list_for_run(revision.run_id)
    inherited = [item for item in revision_evidence if json.loads(item.metadata_json).get("inherited")]
    assert len(inherited) == len(old_ids)
    assert all(
        json.loads(item.metadata_json)["base_run_id"] == base_run.run_id for item in inherited
    )
    # 基线行未被移动/改写
    assert len(await EvidenceRepository(database).list_for_run(base_run.run_id)) == len(old_ids)

    verdict = await ContractEvaluator(ContractRepository(database)).evaluate(
        run_id=revision.run_id, source_mode=ContractSourceMode.GRAPHRAG, report=merged,
        evidence=revision_evidence, min_evidence=1, consistency_passed=True,
        evidence_card_coverage={
            "card_ids": {new_id}, "routed_ids": {new_id},
            "processed_ids": {new_id}, "annex_ids": {new_id}, "card_count": 1,
        },
    )
    assert verdict.passed, [(check.kind, check.observed) for check in verdict.checks if not check.passed]
    coverage = next(check for check in verdict.checks if check.kind == "evidence_card_coverage")
    assert coverage.observed["actual"]["inherited_count"] == len(old_ids)


@pytest.mark.asyncio
async def test_revision_rejects_wrong_base_hash_and_foreign_run(database, tmp_path):
    session = await SessionRepository(database).create(SessionCreate(title="bc04-reject"))
    base_run, old_ids = await _seed_completed_base(database, tmp_path, session.session_id, _base_report)
    _, revision = await _create_run(database, session.session_id, "bc04-revision-2")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    contract = await builder.build(
        session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
    )
    runtime = _runtime(database, tmp_path)

    def context_with(payload):
        return RunContext(
            run_id=revision.run_id, session_id=session.session_id,
            trigger_message_id=revision.trigger_message_id,
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            status="reporting", original_query="修改方法章节",
            context_snapshot={"artifact_edit": payload},
        )

    wrong_hash = {**contract, "base_sha256": "0" * 64}
    with pytest.raises(AppError) as exc:
        await runtime._prepare_artifact_edit(context_with(wrong_hash))
    assert exc.value.code.value == "ARTIFACT_INTEGRITY_FAILED"

    # 跨会话基线被拒绝
    other = await SessionRepository(database).create(SessionCreate(title="other"))
    cross = RunContext(
        run_id=revision.run_id, session_id=other.session_id,
        trigger_message_id=revision.trigger_message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="reporting", original_query="修改方法章节",
        context_snapshot={"artifact_edit": contract},
    )
    with pytest.raises(AppError) as exc:
        await runtime._prepare_artifact_edit(cross)
    assert exc.value.code.value == "ARTIFACT_INTEGRITY_FAILED"
    assert base_run.run_id and old_ids


@pytest.mark.asyncio
async def test_revision_rejects_changed_preserved_section(database, tmp_path):
    session = await SessionRepository(database).create(SessionCreate(title="bc04-tamper"))
    _, old_ids = await _seed_completed_base(database, tmp_path, session.session_id, _base_report)
    _, revision = await _create_run(database, session.session_id, "bc04-revision-3")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    contract = await builder.build(
        session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
    )
    contract = {
        **contract,
        "preserve_sections": [
            {**item, "sha256": "f" * 64} for item in contract["preserve_sections"]
        ],
    }
    base_report = (tmp_path / "artifacts" / contract["relative_path"]).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="preserved section"):
        ArtifactEditContextBuilder.apply_replacement(
            base_report, contract, f"## 方法\n\n修订 [{old_ids[0]}]。",
        )


async def _revision_fixture(database, tmp_path, *, report_builder=_base_report):
    session = await SessionRepository(database).create(SessionCreate(title="bc04-fixture"))
    base_run, old_ids = await _seed_completed_base(database, tmp_path, session.session_id, report_builder)
    _, revision = await _create_run(database, session.session_id, "bc04-revision-fixture")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    contract = await builder.build(
        session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
    )
    return session, base_run, old_ids, revision, contract


def _revision_context(revision, session, contract):
    return RunContext(
        run_id=revision.run_id, session_id=session.session_id,
        trigger_message_id=revision.trigger_message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="reporting", original_query="修改方法章节",
        context_snapshot={"artifact_edit": contract},
    )


@pytest.mark.asyncio
async def test_prepare_rejects_tampered_preserve_metadata_and_identity(database, tmp_path):
    session, _, _, revision, contract = await _revision_fixture(database, tmp_path)
    runtime = _runtime(database, tmp_path)

    tampered = {
        **contract,
        "preserve_sections": [{**contract["preserve_sections"][0], "sha256": "e" * 64}],
    }
    with pytest.raises(AppError):
        await runtime._prepare_artifact_edit(_revision_context(revision, session, tampered))

    omitted = {**contract, "preserve_sections": []}
    with pytest.raises(AppError):
        await runtime._prepare_artifact_edit(_revision_context(revision, session, omitted))

    wrong_artifact = {**contract, "artifact_id": "art_not_the_base"}
    with pytest.raises(AppError):
        await runtime._prepare_artifact_edit(_revision_context(revision, session, wrong_artifact))

    wrong_citations = {**contract, "base_citations": ["ev_forged"]}
    with pytest.raises(AppError):
        await runtime._prepare_artifact_edit(_revision_context(revision, session, wrong_citations))

    wrong_operation = {**contract, "operation": "append_section"}
    with pytest.raises(AppError):
        await runtime._prepare_artifact_edit(_revision_context(revision, session, wrong_operation))

    for required_key in ("artifact_id", "outline", "target_sha256", "base_citations"):
        missing_field = {key: value for key, value in contract.items() if key != required_key}
        with pytest.raises(AppError):
            await runtime._prepare_artifact_edit(_revision_context(revision, session, missing_field))


@pytest.mark.asyncio
async def test_prepare_rejects_missing_base_evidence(database, tmp_path):
    def missing_citation_report(old_ids):
        return (
            "# 摘要\n\n"
            "保留段落引用了不存在的证据 [ev_missing0000000000000000]。\n\n"
            "# 方法\n\n旧方法。\n"
        )

    session, _, _, revision, contract = await _revision_fixture(
        database, tmp_path, report_builder=missing_citation_report,
    )
    runtime = _runtime(database, tmp_path)
    with pytest.raises(AppError) as exc:
        await runtime._prepare_artifact_edit(_revision_context(revision, session, contract))
    assert exc.value.code.value == "ARTIFACT_INTEGRITY_FAILED"
    assert exc.value.details["missing_evidence_ids"] == ["ev_missing0000000000000000"]


@pytest.mark.asyncio
async def test_prepare_is_idempotent_and_inherit_rejects_collision(database, tmp_path):
    session, _, old_ids, revision, contract = await _revision_fixture(database, tmp_path)
    runtime = _runtime(database, tmp_path)
    context = _revision_context(revision, session, contract)
    await runtime._prepare_artifact_edit(context)
    first = context.context_snapshot["artifact_edit"]
    count_after_first = len(await EvidenceRepository(database).list_for_run(revision.run_id))
    await runtime._prepare_artifact_edit(context)
    second = context.context_snapshot["artifact_edit"]
    assert second["citation_mapping"] == first["citation_mapping"]
    assert len(await EvidenceRepository(database).list_for_run(revision.run_id)) == count_after_first

    # 用新 Run 自有证据的 ID 冒充继承 ID → 冲突拒绝
    new_evidence = await EvidenceLedger(EvidenceRepository(database)).record_results(
        run_id=revision.run_id, task_id="t", tool_call_id="c",
        provider="graphrag", results=[_result(9, "新证据")],
    )
    base_row = (await EvidenceRepository(database).list_for_run(contract["base_run_id"]))[0]
    with pytest.raises(AppError):
        await EvidenceRepository(database).inherit(
            base_row, run_id=revision.run_id, evidence_id=new_evidence[0].evidence_id,
            metadata={"inherited": True, "base_evidence_id": old_ids[0]},
        )


@pytest.mark.asyncio
async def test_ambiguous_edit_target_requests_clarification(database, tmp_path):
    session = await SessionRepository(database).create(SessionCreate(title="bc04-ambiguous"))
    base_report = "# 方法\n\n旧方法。\n\n# 方法\n\n重复标题。\n"
    await _seed_completed_base(database, tmp_path, session.session_id, lambda _ids: base_report)
    _, revision = await _create_run(database, session.session_id, "bc04-ambiguous-revision")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    with pytest.raises(ArtifactEditTargetAmbiguous) as exc:
        await builder.build(
            session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
        )
    assert exc.value.questions and "章节" in exc.value.questions[0]


@pytest.mark.asyncio
async def test_citation_mapping_only_rewrites_preserved_content(database, tmp_path):
    def preamble_report(old_ids):
        return (
            f"导语引用需要保留 [{old_ids[0]}]。\n\n"
            "# 摘要\n\n"
            f"摘要引用 [{old_ids[1]}]。\n\n"
            "# 方法\n\n旧方法。\n"
        )

    session, _, old_ids, revision, contract = await _revision_fixture(
        database, tmp_path, report_builder=preamble_report,
    )
    runtime = _runtime(database, tmp_path)
    context = _revision_context(revision, session, contract)
    await runtime._prepare_artifact_edit(context)
    mapping = context.context_snapshot["artifact_edit"]["citation_mapping"]

    # 目标章节（新生成）错误复用旧 ID：不得被静默映射，Contract 应因此失败
    merged = runtime._apply_artifact_edit(
        context, f"## 方法\n\n新方法错误复用旧引用 [{old_ids[0]}]。",
    )
    assert f"[{mapping[old_ids[0]]}]" in merged.split("# 方法")[0], "导语引用必须映射"
    assert f"[{mapping[old_ids[1]]}]" in merged.split("# 方法")[0], "保留章节引用必须映射"
    assert f"[{old_ids[0]}]" in merged.split("# 方法")[1], "新目标章节中的旧 ID 不得被改写"


@pytest.mark.asyncio
async def test_prepare_rejects_cross_channel_inheritance(database, tmp_path):
    """S4：修订 Run 冻结集合不含基线证据通道时，继承前显式拒绝。"""
    ledger = EvidenceLedger(EvidenceRepository(database))
    session = await SessionRepository(database).create(SessionCreate(title="bc04-cross"))
    _, base_run = await _create_run(database, session.session_id, "bc04-cross-base")
    evidence = await ledger.record_results(
        run_id=base_run.run_id, task_id="task-graph", tool_call_id="call-graph",
        provider="graphrag",
        results=[RetrievalResult(
            result_id="raw-graph", granularity="Chunk", evidence="图谱证据",
            metadata=RetrievalMetadata(source_id="doc-graph", source_type="chunk", content_hash="b" * 64),
            source="hybrid_search", source_mode="graphrag", score=0.9,
        )],
    )
    old_ids = [item.evidence_id for item in evidence]
    report = f"# 摘要\n\n图谱结论 [{old_ids[0]}]。\n\n# 方法\n\n旧方法。\n"
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.write_text(
        f"{base_run.run_id}/report/final.md", report, mime_type="text/markdown; charset=utf-8",
    )
    await ArtifactRepository(database).record(artifact, run_id=base_run.run_id)
    await ContractRepository(database).upsert(ContractCheckData(
        check_id=f"contract_{base_run.run_id}_min_evidence", run_id=base_run.run_id,
        kind="min_evidence", required=True, passed=True, verifier="test", verifier_version="1",
    ))
    await RunRepository(database).complete_verified(base_run.run_id, assistant_content=report, usage={})
    _, revision = await _create_run(database, session.session_id, "bc04-cross-revision")
    builder = ArtifactEditContextBuilder(
        RunRepository(database), ArtifactRepository(database), ArtifactStore(tmp_path / "artifacts"),
    )
    contract = await builder.build(
        session_id=session.session_id, current_run_id=revision.run_id, query="修改方法章节",
    )
    context = RunContext(
        run_id=revision.run_id, session_id=session.session_id,
        trigger_message_id=revision.trigger_message_id,
        source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="reporting", original_query="修改方法章节",
        config_snapshot={"sources": ["web"]},
        context_snapshot={"artifact_edit": contract},
    )
    runtime = _runtime(database, tmp_path)
    with pytest.raises(AppError) as exc:
        await runtime._prepare_artifact_edit(context)
    assert exc.value.details["outside_channels"] == ["graphrag"]

"""学术 provider（P2-11）：OpenAlex/arXiv 解析、链式兜底、scholar 模式穿透。

全部离线：httpx.MockTransport 模拟上游响应。
"""


import httpx
import pytest

from deepresearch_agent.harness import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.scholar_provider import (
    ArxivProvider, ChainedScholarProvider, OpenAlexProvider, _inverted_abstract_to_text,
)


def _ctx():
    return ToolCallContext(run_id="run-x", source_mode=SourceMode.SCHOLAR)


def _openalex_payload():
    return {
        "results": [
            {
                "id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.1000/demo1",
                "display_name": "Attention Is All You Need",
                "publication_date": "2017-06-12",
                "relevance_score": 120.5,
                "abstract_inverted_index": {"attention": [0, 2], "is": [1], "all": [3], "you": [4], "need": [5]},
                "authorships": [{"author": {"id": "A1", "display_name": "Ashish Vaswani"}, "institutions": [{"id": "I1", "display_name": "Google Brain"}]}],
                "primary_location": {"source": {"id": "V1", "display_name": "NeurIPS"}, "landing_page_url": "https://arxiv.org/abs/1706.03762"},
            },
            {"id": "https://openalex.org/W2", "display_name": "No abstract work", "abstract_inverted_index": None},
        ]
    }


def _mock_openalex(mock_registry):
    async def handler(request: httpx.Request) -> httpx.Response:
        mock_registry.append(str(request.url))
        return httpx.Response(200, json=_openalex_payload())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _mock_openalex_client(mock_registry):
    return _mock_openalex(mock_registry)


async def test_openalex_maps_works_to_retrieval_results():
    registry = []
    provider = OpenAlexProvider(mailto="ops@example.com", client=_mock_openalex(registry))
    try:
        results = await provider.search("attention", top_k=5, search_depth="basic", filters=SearchFilters(), call_context=_ctx())
    finally:
        await provider.aclose()
    assert len(results) == 1, "无摘要的 work 应被过滤"
    result = results[0]
    assert result.result_id == "https://openalex.org/W1"
    assert result.metadata.url == "https://doi.org/10.1000/demo1"
    assert result.metadata.title == "Attention Is All You Need"
    assert "attention is all you need" in result.evidence.lower()
    assert "Ashish Vaswani" in result.evidence and "NeurIPS" in result.evidence
    assert "mailto=" in registry[0], "polite pool 标识必须携带 mailto"


def test_inverted_abstract_reconstruction():
    text = _inverted_abstract_to_text({"c": [2], "a": [0], "b": [1]})
    assert text == "a b c"


def _arxiv_atom(entries):
    body = "".join(
        f"<entry><id>http://arxiv.org/abs/{e['id']}</id><title>{e['title']}</title>"
        f"<summary>{e['summary']}</summary><published>2023-05-01T00:00:00Z</published>"
        f"<author><name>{e['author']}</name></author></entry>"
        for e in entries
    )
    return f"<feed xmlns=\"http://www.w3.org/2005/Atom\">{body}</feed>"


async def test_arxiv_parses_atom_entries():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_arxiv_atom([
            {"id": "2401.00001", "title": "Graph RAG", "summary": "Knowledge graph based retrieval.", "author": "Jane Doe"},
        ]))

    provider = ArxivProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        results = await provider.search("rag", top_k=3, search_depth="basic", filters=SearchFilters(), call_context=_ctx())
    finally:
        await provider.aclose()
    assert results[0].result_id == "arxiv:2401.00001"
    assert results[0].metadata.url == "https://arxiv.org/abs/2401.00001"
    assert "Graph RAG" in results[0].evidence


async def test_chain_falls_back_to_arxiv_when_openalex_empty():
    async def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async def arxiv_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_arxiv_atom([
            {"id": "2401.00002", "title": "Fallback paper", "summary": "From arXiv.", "author": "John"},
        ]))

    chain = ChainedScholarProvider(providers=[
        OpenAlexProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(empty_handler))),
        ArxivProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(arxiv_handler))),
    ])
    try:
        results = await chain.search("obscure query", top_k=3, search_depth="basic", filters=SearchFilters(), call_context=_ctx())
    finally:
        await chain.aclose()
    assert results[0].metadata.source_id == "2401.00002", "主源为空必须回落 arXiv"


async def test_chain_raises_source_unavailable_when_all_fail():
    async def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    chain = ChainedScholarProvider(providers=[
        OpenAlexProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))),
        ArxivProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))),
    ])
    try:
        with pytest.raises(AppError) as excinfo:
            await chain.search("x", top_k=3, search_depth="basic", filters=SearchFilters(), call_context=_ctx())
    finally:
        await chain.aclose()
    assert excinfo.value.code == ErrorCode.SOURCE_UNAVAILABLE


async def test_scholar_mode_end_to_end_through_runtime(tmp_path):
    """scholar 模式穿透 HarnessRuntime：检索→证据→Contract 门→完成。"""
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness import RunStatus, WorkflowMode
    from deepresearch_agent.harness.budgets import BudgetLimits
    from deepresearch_agent.harness.runtime import HarnessRuntime
    from deepresearch_agent.persistence import ArtifactStore, Database
    from deepresearch_agent.persistence.repositories import (
        ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
        EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository, SessionRepository,
    )
    from tests.harness.fake_driver import CompletedFakeDriver

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'scholar.db').as_posix()}")
    await database.create_schema()
    session = await SessionRepository(database).create(SessionCreate(title="scholar"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="学术问题", client_message_id="c1", metadata={}),
        RunCreate(session_id=session.session_id, trigger_message_id="atomic", source_mode=SourceMode.SCHOLAR,
                  workflow_mode=WorkflowMode.DEEP_RESEARCH, config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump()),
    )
    runtime = HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database),
        workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events),
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
    )
    context = await runtime.execute_run(run.run_id)
    assert context.status is RunStatus.COMPLETED
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.source_mode == "scholar"
    await database.close()

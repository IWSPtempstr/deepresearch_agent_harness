"""S4：多来源验收语义——source_match 集合成员 + provider/通道一致性。"""

from types import SimpleNamespace

import pytest

from deepresearch_agent.harness.contracts import ContractEvaluator, SourceMode
from deepresearch_agent.harness.verifiers.deterministic import DeterministicVerifiers


def _evidence(mode: str, provider: str, eid: str = "ev_1"):
    return SimpleNamespace(
        evidence_id=eid, source_mode=mode, provider=provider,
        source_id=f"doc-{provider}", title="t", summary="s", metadata_json="{}",
        content_hash="a" * 64,
    )


def _verifier(evidence, allowed):
    return DeterministicVerifiers(
        run_id="r", source_mode=SourceMode.WEB, report="# 报告", evidence=evidence,
        allowed_sources=allowed,
    )


def test_mixed_channels_pass_with_frozen_set():
    evidence = [_evidence("web", "tavily"), _evidence("graphrag", "graphrag", "ev_2")]
    check = _verifier(evidence, ["web", "graphrag"]).source_match()
    assert check.passed is True
    assert check.observed["actual"]["outside_allowed"] == []
    assert check.observed["actual"]["provider_mismatch"] == []


def test_outside_channel_fails():
    evidence = [_evidence("scholar", "openalex")]
    check = _verifier(evidence, ["web", "graphrag"]).source_match()
    assert check.passed is False
    assert check.observed["actual"]["outside_allowed"] == ["scholar"]


def test_provider_channel_mismatch_fails():
    evidence = [_evidence("web", "graphrag")]
    check = _verifier(evidence, ["web", "graphrag"]).source_match()
    assert check.passed is False
    assert check.observed["actual"]["provider_mismatch"] == ["graphrag->web"]


def test_default_single_source_semantics_unchanged():
    check = _verifier([_evidence("web", "unknown-provider")], None).source_match()
    assert check.passed is True
    check = _verifier([_evidence("graphrag", "graphrag")], None).source_match()
    assert check.passed is False




@pytest.mark.asyncio
async def test_evaluator_accepts_multi_source_run():
    evidence = [_evidence("web", "tavily", "ev_a"), _evidence("graphrag", "graphrag", "ev_b")]
    report = (
        "# 研究报告\n\n结论已有证据支持 [ev_a] [ev_b]。\n\n## 方法\n\n混合来源。\n\n## 局限\n\n略。"
    )
    verdict = await ContractEvaluator().evaluate(
        run_id="r", source_mode=SourceMode.WEB, report=report, evidence=evidence,
        min_evidence=1, consistency_passed=True, allowed_sources=["web", "graphrag"],
    )
    assert verdict.passed, [(c.kind, c.observed) for c in verdict.checks if not c.passed]
    match = next(c for c in verdict.checks if c.kind == "source_match")
    assert match.verifier_version == "2.0"
    diversity = next(c for c in verdict.checks if c.kind == "source_diversity")
    assert diversity.observed["actual"]["channels"] == ["graphrag", "web"]

def test_legacy_scholar_row_passes_membership():
    """双时代兼容：legacy scholar 行（provider=工具名、通道=scholar）只受集合成员约束。"""
    evidence = [_evidence("scholar", "scholar_search")]
    check = _verifier(evidence, ["scholar"]).source_match()
    assert check.passed is True


def test_aggregate_scholar_label_passes_on_web_channel():
    evidence = [_evidence("web", "scholar")]
    check = _verifier(evidence, ["web"]).source_match()
    assert check.passed is True


def test_inherited_rows_outside_frozen_set_fail():
    """BC-04 交互：继承行通道超出本次 Run 冻结集合 → source_match 失败。"""
    evidence = [SimpleNamespace(
        evidence_id="ev_i", source_mode="graphrag", provider="graphrag",
        source_id="doc-base", title="t", summary="s",
        metadata_json='{"inherited": true, "base_run_id": "base"}',
        content_hash="a" * 64,
    )]
    check = _verifier(evidence, ["web"]).source_match()
    assert check.passed is False

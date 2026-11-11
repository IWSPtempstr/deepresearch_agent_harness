"""CI 评测回归门（P2-9）：基线 gold 集跑真实 Harness，指标低于阈值则非零退出。

与 scripts/evaluate_runs.py 共用 EvaluationService 与指标口径；不依赖
LLM/Neo4j——三个基线场景用确定性 replay driver 执行（覆盖状态机、
Completion Contract、Evidence 落库全链路），使评测门在 CI 可稳定复现。

用法：python scripts/evaluation_gate.py [--thresholds evals/baseline/thresholds.json]
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.evaluation import EvaluationService
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import SessionRepository, RunRepository

from tests.harness.fake_driver import CompletedFakeDriver

DEFAULT_THRESHOLDS = {
    # 基线 -5%：确定性 driver 下 verified_completion_rate 恒为 1.0，
    # 阈值 0.95；citation_validity 同理。若未来 gold 集加入真实 LLM 场景，
    # 用 evaluate_runs.py 实测基线后更新此文件。
    "verified_completion_rate": 0.95,
    "first_pass_completion_rate": 0.90,
    "citation_validity": 0.95,
}


async def execute_gold_set(database: Database, cases: list[dict]) -> list[str]:
    run_ids: list[str] = []
    for case in cases:
        session = await SessionRepository(database).create(SessionCreate(title=case["id"]))
        _, run, _ = await RunRepository(database).create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content=case["query"], client_message_id=f"gate-{case['id']}", metadata={}),
            RunCreate(
                session_id=session.session_id, trigger_message_id="atomic",
                source_mode=SourceMode(case.get("source_mode", "graphrag")),
                workflow_mode=WorkflowMode(case.get("workflow", "plan_execute_report")),
                config_snapshot={"min_evidence": 1, "report_type": "brief"}, budget=BudgetLimits().model_dump(),
            ),
        )
        from backend.app.services.run_service import RunService

        service = RunService(
            database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events),
            artifact_root=Path(tempfile.mkdtemp()) / "artifacts",
        )
        context = await service.execute_now(run.run_id)
        status = context.status.value if hasattr(context.status, "value") else str(context.status)
        if status != "completed":
            raise SystemExit(f"gold case {case['id']} 未完成: {status}")
        run_ids.append(run.run_id)
    return run_ids


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thresholds", type=Path, default=PROJECT_ROOT / "evals/baseline/thresholds.json")
    parser.add_argument("--gold", type=Path, default=PROJECT_ROOT / "evals/baseline/cases.json")
    parser.add_argument("--output", type=Path, help="结果写入路径（CI artifact）")
    args = parser.parse_args()

    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8")) if args.thresholds.exists() else DEFAULT_THRESHOLDS
    cases = json.loads(args.gold.read_text(encoding="utf-8"))
    cases = cases.get("cases", cases) if isinstance(cases, dict) else cases

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'gate.db'}")
        await database.create_schema()
        try:
            run_ids = await execute_gold_set(database, cases)
            summary = await EvaluationService(database).summarize(run_ids=run_ids, retrieval_k=10, include_runs=False)
            metrics = summary.model_dump(mode="json")
        finally:
            await database.close()

    failures = []
    for metric, threshold in thresholds.items():
        if metric.startswith("_"):
            continue
        observed = metrics.get(metric)
        if observed is None:
            failures.append(f"指标缺失: {metric}")
        elif observed < threshold:
            failures.append(f"{metric}={observed:.3f} 低于阈值 {threshold:.3f}")

    report = {"metrics": metrics, "thresholds": thresholds, "passed": not failures, "failures": failures}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    if failures:
        print("评测门未通过", file=sys.stderr)
        raise SystemExit(1)
    print("评测门通过")


if __name__ == "__main__":
    asyncio.run(main())

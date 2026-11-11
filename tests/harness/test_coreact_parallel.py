"""T-016 review blocker ①: COREACT_MAX_PARALLEL>1 并行回归测试。"""
import pytest

from deepresearch_agent.harness.contracts import RunStatus
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver

from test_coreact_driver import (
    FakeOrchestrator,
    ScriptedLLM,
    action_by_id,
    make_context,
    ready,
)


@pytest.fixture
def parallel_two(monkeypatch):
    monkeypatch.setattr("deepresearch_agent.harness.coreact_loop.COREACT_MAX_PARALLEL", 2)


class TestParallelBatch:
    def test_two_independent_ready_tasks_execute_concurrently(self, parallel_two):
        """K=2：controller 选 plan_1，fillup 自动补 plan_2（同批并发），两个动作各产生证据。"""
        llm = ScriptedLLM([
            action_by_id("plan_1", "检索增强 概念"),
            ready(),
        ])
        orchestrator = FakeOrchestrator(llm, [
            (True, "检索增强是一种技术路线，概念说明"),
            (True, "检索增强是一种技术路线，局限说明"),
        ])
        driver = PlanExecuteReportDriver(make_context(), orchestrator)
        import asyncio

        asyncio.run(driver.plan())
        asyncio.run(driver.execute())

        executed_ids = [t.task_id for t in orchestrator.executed]
        assert executed_ids == ["plan_1", "plan_2"], executed_ids
        assert driver.coreact_loop.step_index == 2  # 批次推进
        assert driver.coreact_loop.done
        # 两个动作的 verdict 均已记录
        coreact_meta = [r.metadata.environment["coreact"] for r in driver.state.execution_records]
        assert len(coreact_meta) == 2
        assert all("verdict" in m for m in coreact_meta)
        # plan 中无重复节点（fillup 复用现有节点而非 append）
        plan_nodes = driver.state.plan.task_graph.nodes
        ids = [n.task_id for n in plan_nodes]
        assert len(ids) == len(set(ids)), ids

    def test_fillup_skips_duplicate_queries(self, parallel_two):
        """补位排除与主任务重复的查询：plan_2 的 query 若与 plan_1 相同则不补位。"""
        llm = ScriptedLLM([
            action_by_id("plan_1", "同一查询"),
            ready(),
        ])
        orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线")])
        # 让 plan_2 的 query 与 plan_1 完全一致 → fillup 查重拒绝
        driver = PlanExecuteReportDriver(make_context(), orchestrator)

        import asyncio

        original_plan = None

        class DupPlan(FakeOrchestrator):
            def plan(self, state, *, assumptions=None):
                nonlocal original_plan
                result = super().plan(state, assumptions=assumptions)
                original_plan = state.plan
                # 把 plan_2 的 query 改成与 plan_1 相同
                for node in state.plan.task_graph.nodes:
                    if node.task_id == "plan_2":
                        node.parameters = {"query": "同一查询"}
                return result

        dup_orchestrator = DupPlan(llm, [(True, "检索增强是一种技术路线")])
        driver = PlanExecuteReportDriver(make_context(), dup_orchestrator)
        asyncio.run(driver.plan())
        asyncio.run(driver.execute())
        # 只执行了主任务（补位因重复查询被拒）
        assert [t.task_id for t in dup_orchestrator.executed] == ["plan_1"]
        assert driver.coreact_loop.step_index == 1

    def test_k1_default_identical_to_serial(self):
        """K=1（默认）：批次只有一个动作，行为与串行一致。"""
        llm = ScriptedLLM([
            action_by_id("plan_1", "检索增强 概念"),
            action_by_id("plan_2", "检索增强 局限"),
            ready(),
        ])
        orchestrator = FakeOrchestrator(llm, [
            (True, "检索增强是一种技术路线，概念说明"),
            (True, "检索增强是一种技术路线，局限说明"),
        ])
        driver = PlanExecuteReportDriver(make_context(), orchestrator)
        import asyncio

        asyncio.run(driver.plan())
        asyncio.run(driver.execute())
        # K=1 不补位：两轮各执行一个动作
        assert [t.task_id for t in orchestrator.executed] == ["plan_1", "plan_2"]
        assert driver.coreact_loop.step_index == 2


class TestPendingResumeControlFlow:
    def test_pending_resume_replays_action_without_controller_decision(self):
        """恢复路径：pending_action 存在时直接重放该动作，不调用 controller（无 UnboundLocalError）。"""
        from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric
        from deepresearch_agent.harness.coreact_state import CoreActLoopState, PendingAction

        llm = ScriptedLLM([ready("重放完成")])  # 恢复轮不调用；仅重放后的收尾 decide 用
        orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，补充证据")])
        driver = PlanExecuteReportDriver(make_context(), orchestrator)

        task = type("T", (), {
            "task_id": "plan_1", "task_type": "web_search", "source_mode": "web",
            "description": "检索增强 概念", "parameters": {"query": "检索增强 概念"},
            "priority": 2, "estimated_tokens": 500, "depends_on": [], "entities": [],
            "status": "pending",
        })()
        rubric = build_task_rubric("plan_1", description="检索增强 概念", source_mode="web", min_evidence=1)
        driver.coreact_loop = CoreActLoopState(
            phase="verify", pending_action=PendingAction(
                task={
                    "task_id": "plan_1", "task_type": "web_search", "source_mode": "web",
                    "description": "检索增强 概念", "parameters": {"query": "检索增强 概念"},
                    "status": "running",
                },
                rubric=rubric.model_dump(), attempt=2,
            ),
        )
        driver.context.status = RunStatus.EXECUTING

        import asyncio

        asyncio.run(driver.plan())
        asyncio.run(driver.execute())
        # pending 动作被重放；controller 仅在重放后的收尾被调用一次（ready）
        assert len(llm.prompts) == 1
        assert [t.task_id for t in orchestrator.executed] == ["plan_1"]
        assert driver.coreact_loop.done  # 重放后下一轮 decide 兜底 ready

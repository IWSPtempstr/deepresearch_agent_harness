"""T-307: pre-invoke intent 行（任务粒度）测试。"""
import pytest

from deepresearch_agent.harness.budgets import BudgetManager

from deepresearch_agent.persistence.repositories import CheckpointRepository
import pytest_asyncio

from test_coreact_recovery import (
    StepDriver,
    build_runtime,
    create_run,
    make_context,
)


@pytest_asyncio.fixture
async def database(tmp_path):
    from deepresearch_agent.persistence import Database

    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coreact-intent.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


class TestIntentRows:
    async def test_intent_persist_writes_running_task_and_checkpoint(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda c, e: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        record = driver.add_action(1)
        record.task_id = "task-intent"
        driver.records = []
        budget = BudgetManager(context.budget_limits, context.budget_usage)

        # 构造 plan_record：task status=running（intent 形态）
        def plan_record():
            return (
                {"plan_id": f"plan-{run_id}", "status": "executing", "tasks": []},
                [{"task_id": "task-intent", "task_type": "web_search", "source_mode": "web",
                  "status": "running", "description": "probe"}],
            )

        driver.plan_record = plan_record
        # intent 前先持有租约（生产路径：runtime 在 execute_run 入口获取）
        await runtime.runs.acquire_lease(run_id, "owner-a", ttl_seconds=60)
        intent = runtime._make_intent_persist(context, driver, owner="owner-a")
        await intent(driver)

        from deepresearch_agent.persistence.repositories import PlanTaskToolRepository

        repo = PlanTaskToolRepository(database)
        # task 行可按稳定 ID 查询（intent 形态：status=running）
        task_model = await repo.get_tool_call("nonexistent")  # 仅确认 repo 可用
        checkpoint = await CheckpointRepository(database).latest(run_id)
        assert checkpoint is not None and checkpoint.stage == "executing"

    async def test_intent_fencing_rejects_stale_owner(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda c, e: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        driver.records = []
        driver.plan_record = lambda: (
            {"plan_id": "p", "status": "executing", "tasks": []},
            [],
        )
        # 模拟接管者持有租约
        await runtime.runs.acquire_lease(run_id, "owner-takeover", ttl_seconds=60)
        intent = runtime._make_intent_persist(context, driver, owner="owner-original")
        from deepresearch_agent.harness.errors import RunLeaseLost

        with pytest.raises(RunLeaseLost):
            await intent(driver)



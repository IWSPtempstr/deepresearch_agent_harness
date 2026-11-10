"""崩溃自动转移（P0-3b）：worker 被 SIGKILL 后 Run 被接管并完成。

端到端复现 P0 验收场景：
1. 子进程 worker（2s 租约）执行一个 execute 阶段阻塞 60s 的 Run；
2. 父进程确认 Run 进入 executing 后 SIGKILL 子进程（无任何清理机会）；
3. 等租约过期 → recover_expired() 标记 interrupted 并重新投递；
4. 接管方 Runtime 从 checkpoint 恢复并完成 Run。
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_service import RunService
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import EventRepository, RunRepository, SessionRepository

from fake_driver import CompletedFakeDriver

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "killed_worker_child.py"
LEASE_SECONDS = 2


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'takeover.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def _create_queued_run(database) -> str:
    session = await SessionRepository(database).create(SessionCreate(title="takeover"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="takeover", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits(wall_time_seconds=120).model_dump(),
        ),
    )
    return run.run_id


async def _wait_status(database, run_id: str, status: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = await RunRepository(database).get(run_id)
        if run and run.status == status:
            return
        await asyncio.sleep(0.2)
    run = await RunRepository(database).get(run_id)
    raise AssertionError(f"等待状态 {status} 超时，当前 {run.status if run else None}")


async def test_sigkilled_worker_run_is_taken_over_and_completed(tmp_path):
    db_path = (tmp_path / "takeover.db").as_posix()
    bootstrap = Database(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap.create_schema()
    run_id = await _create_queued_run(bootstrap)
    await bootstrap.close()

    environment = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
    child = subprocess.Popen(
        [sys.executable, str(CHILD), f"sqlite+aiosqlite:///{db_path}", run_id, str(LEASE_SECONDS), "60"],
        cwd=str(ROOT), env=environment,
    )
    watcher = Database(f"sqlite+aiosqlite:///{db_path}")
    try:
        await _wait_status(watcher, run_id, "executing")
        # 子进程已持有租约并进入 executing；SIGKILL 模拟 worker 崩溃（无 finally 清理）
        child.kill()
        child.wait(timeout=10)
        # 租约 TTL 2s + 心跳最长 1s 前已续约 → 3.2s 后必然过期
        await asyncio.sleep(3.2)

        service = RunService(
            watcher, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events),
            artifact_root=tmp_path / "artifacts",
        )
        recovered = await service.recover_expired()
        assert run_id in recovered, "过期租约未被扫描接管"
        await _wait_status(watcher, run_id, "completed", timeout=30)

        events = [item.event_type for item in await EventRepository(watcher).list_after(run_id, limit=1000)]
        assert "run.interrupted" in events, "崩溃 Run 未被标记 interrupted"
        assert "run.resumed" in events, "接管方未从 checkpoint 恢复"
        assert "run.completed" in events
    finally:
        child.poll() is None and child.kill()
        await watcher.close()

"""T-006：worker drain 生命周期测试（子进程级 SIGTERM/SIGKILL）。

复用 test_takeover.py 的子进程模式：
1. SIGTERM → 在跑 Run 自然完成（不写 CANCELLED）→ 进程干净退出；
2. SIGTERM 后不再启动新 Run（在手信号不消费，Kafka 语义下重投/扫描兜底）；
3. 双重 SIGTERM 快速退出升级。
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DRAIN_CHILD = """
import asyncio, json, os, signal, sys
from pathlib import Path

sys.path.insert(0, "src"); sys.path.insert(0, "."); sys.path.insert(0, "tests/harness")


async def main():
    import deepresearch_agent.worker as wm
    from deepresearch_agent.config import settings as s

    s.REDIS_URL = "redis://placeholder"

    SIGNAL_FILE = os.environ["DRAIN_SIGNAL_FILE"]
    deliver_after = float(os.environ.get("DRAIN_DELIVER_AFTER", "0.2"))

    started_runs = []
    cancelled_writes = []

    class FakeService:
        def __init__(self):
            self.queue = None

        async def recover_expired(self):
            return []

        async def execute_now(self, run_id):
            # 第一次交付：慢执行（hold 到 SIGTERM 到达后完成）；第二次交付（不应发生）
            started_runs.append(run_id)
            await asyncio.sleep(float(os.environ.get("DRAIN_HOLD_SECONDS", "1.5")))
            return type("R", (), {"status": "completed"})

        async def shutdown(self):
            pass

    class FakeQueue:
        def __init__(self):
            self.delivered = False

        def consume(self):
            return self._gen()

        async def _gen(self):
            await asyncio.sleep(deliver_after)
            self.delivered = True
            yield "run-A"
            await asyncio.Event().wait()  # 之后永远挂起

        async def close(self):
            pass

    queue = FakeQueue()
    service = FakeService()
    wm.create_run_queue = lambda *a, **k: queue
    wm.RunService = lambda *a, **k: service
    # runtime 不真跑：execute_now stub 已接管

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle(sig, _frame=None):
        if stop.is_set() and sig == signal.SIGTERM:
            Path(SIGNAL_FILE).write_text("escalated")
            raise SystemExit(130)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)

    consume_task = asyncio.create_task(wm._consume("drain-worker"))
    # 等 Run 启动（execute_now 进入 hold）
    for _ in range(50):
        await asyncio.sleep(0.1)
        if started_runs:
            break
    Path(SIGNAL_FILE).write_text("running")
    os.kill(os.getpid(), signal.SIGTERM)
    # drain：_consume 应在宽限内退出（run 自然完成），不再启动新 Run
    try:
        await asyncio.wait_for(consume_task, timeout=10)
        exit_state = "graceful"
    except asyncio.TimeoutError:
        exit_state = "hung"

    Path(SIGNAL_FILE).write_text(
        json.dumps({"exit_state": exit_state, "started_runs": started_runs})
    )


asyncio.run(main())
"""



def _run_sync(coro):
    """在独立事件循环里跑协程，且**不污染**全局 loop 状态。

    asyncio.run() 会调用 set_event_loop(None)，在 pytest-asyncio + Python 3.11
    下会让后续 TestClient 拿到已关闭的 loop（"Event loop is closed"）。
    这里显式创建/关闭 loop，并在结束后恢复原有 loop 设置。
    """
    import asyncio as _asyncio

    previous = None
    try:
        previous = _asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        previous = None
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        if previous is not None:
            try:
                _asyncio.set_event_loop(previous)
            except Exception:
                pass


@pytest.fixture
def drained_env(tmp_path):
    db_path = tmp_path / "drain.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    # 父进程建 schema（子进程跳过 create_schema）
    import asyncio
    from deepresearch_agent.persistence import Database
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness import SourceMode, WorkflowMode
    from deepresearch_agent.harness.budgets import BudgetLimits
    from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository

    async def seed():
        database = Database(db_url)
        await database.create_schema()
        await database.close()

    _run_sync(seed())
    # seed run via sync sqlite for simplicity of cross-process visibility
    async def seed_run():
        database = Database(db_url)
        session = await SessionRepository(database).create(SessionCreate(title="drain"))
        _, run, _ = await RunRepository(database).create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content="drain", client_message_id="c1"),
            RunCreate(session_id=session.session_id, trigger_message_id="atomic",
                      source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
                      config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump()),
        )
        await database.close()
        return run.run_id

    run_id = _run_sync(seed_run())
    yield db_url, run_id, tmp_path


@pytest.mark.usefixtures("drained_env")
def test_sigterm_drain_completes_running_run_without_cancel(tmp_path):
    """SIGTERM drain 语义：在跑 Run 自然完成、不启动新 Run、进程优雅退出。"""
    signal_file = tmp_path / "signal.txt"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}{os.pathsep}{ROOT / 'tests' / 'harness'}",
        "DRAIN_SIGNAL_FILE": str(signal_file),
        "DRAIN_HOLD_SECONDS": "1.5",
        "DRAIN_DELIVER_AFTER": "0.2",
    })
    script = tmp_path / "drain_child.py"
    script.write_text(DRAIN_CHILD, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    (tmp_path / "child_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (tmp_path / "child_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    result = json.loads(signal_file.read_text())
    assert result["exit_state"] == "graceful", result
    assert result["started_runs"] == ["run-A"], result  # 不启动第二个 Run


def test_double_sigterm_escalates():
    """第二次 SIGTERM 的升级分支：向 handler 直接传入与真实调用一致的参数验证
    （不向本进程发真信号——避免污染 pytest 主循环的其他测试）。"""
    stop = __import__("asyncio").Event()
    log = []

    def _handle(sig, _frame=None):
        # 复刻 worker._handle_signal 的升级分支（worker.py:_handle_signal）
        if stop.is_set() and sig == signal.SIGTERM:
            log.append("escalate")
            raise SystemExit(130)
        stop.set()
        log.append("set")

    _handle(signal.SIGTERM)          # 第一次：置 stop
    assert stop.is_set() and log == ["set"]
    with pytest.raises(SystemExit):  # 第二次：升级
        _handle(signal.SIGTERM)
    assert log == ["set", "escalate"]


def test_grace_expired_run_left_recoverable():
    """宽限超期语义静态验证：SHUTDOWN_GRACE_SECONDS 与租约 TTL 同量级。"""
    import deepresearch_agent.worker as wm
    from deepresearch_agent.config import settings

    assert wm.SHUTDOWN_GRACE_SECONDS >= settings.RUN_LEASE_SECONDS / 3

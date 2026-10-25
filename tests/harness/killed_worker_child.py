"""测试专用子进程：模拟一个会被 SIGKILL 的 worker。

用法：python killed_worker_child.py <database_url> <run_id> <lease_seconds> <hold_seconds>

以极短租约启动 HarnessRuntime 并让 execute() 长时间阻塞；父测试进程在
Run 进入 executing 后将其 SIGKILL，用于验证租约过期接管（P0-3b）。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_driver import CompletedFakeDriver

from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
)


class BlockingDriver(CompletedFakeDriver):
    def __init__(self, context, *, hold_seconds: float, events=None):
        super().__init__(context, events=events)
        self.hold_seconds = hold_seconds

    async def execute(self):
        self.executed = True
        await asyncio.sleep(self.hold_seconds)


async def main() -> None:
    database_url, run_id, lease_seconds, hold_seconds = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]),
    )
    database = Database(database_url)
    hold = {"seconds": hold_seconds}

    def factory(context, events=None):
        return BlockingDriver(context, hold_seconds=hold["seconds"], events=events)

    runtime = HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        lease_seconds=lease_seconds,
    )
    try:
        await runtime.execute_run(run_id)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())

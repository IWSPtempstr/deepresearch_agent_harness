"""测试共用的最小可完成 FakeDriver：单阶段直通，产出可通过 Completion Contract 的报告。"""

from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult


class CompletedFakeDriver:
    def __init__(self, context, events=None):
        self.context = context
        self.mode = context.source_mode.value
        self.tool_name = "tavily_search" if self.mode == "web" else ("scholar_search" if self.mode == "scholar" else "local_search")
        restored = context.workflow_state
        self.executed = bool(restored.get("executed"))
        self.results = [
            RetrievalResult(
                result_id=item_id, granularity="Chunk", evidence=f"evidence-{index}",
                metadata=RetrievalMetadata(
                    source_id=f"doc-{index}", source_type="chunk",
                    title=f"source-{index}", content_hash=character * 64,
                ),
                source=self.tool_name, source_mode=self.mode, score=0.9,
            )
            for index, (item_id, character) in enumerate((("raw-1", "a"), ("raw-2", "b")), start=1)
        ]
        self._report = restored.get("report")

    async def plan(self, failures=None):
        return None

    async def execute(self):
        self.executed = True

    async def report(self):
        self._report = f"# 研究报告\n\n结论内容已有充分说明 [{self.results[0].result_id}]\n\n## 方法\n\n基于证据。\n\n## 局限\n\n当前报告仅直接引用一个来源。"
        return self._report

    async def repair_report(self, failures):
        return False

    def snapshot(self):
        return {"executed": self.executed, "report": self._report, "result_ids": [item.result_id for item in self.results]}

    def execution_records(self):
        call = ToolCall(tool_name=self.tool_name, tool_call_id=f"call-{self.context.run_id}-0", source_mode=self.mode, args={"query": "safe"}, result={"ok": True})
        return [ExecutionRecord(task_id=f"task-{self.context.run_id}", session_id=self.context.session_id, worker_type="fake", tool_calls=[call], evidence=self.results, metadata=ExecutionMetadata(worker_type="fake", tool_calls_count=1, evidence_count=len(self.results)))]

    def evidence_results(self):
        return [(f"task-{self.context.run_id}", f"call-{self.context.run_id}-0", self.tool_name, item) for item in self.results]

    def report_consistency(self):
        return True

    def plan_record(self):
        tasks = [{"task_id": f"task-{self.context.run_id}", "task_type": "deep_research", "source_mode": self.mode, "status": "pending", "description": "research-0"}]
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": tasks}, tasks)


class SlowFakeDriver(CompletedFakeDriver):
    """执行阶段持锁 N 秒的变体：供 SIGTERM drain 测试观察“在跑 Run 自然完成”。"""

    def __init__(self, context, events=None, hold_seconds: float = 0.0, run_id: str | None = None):
        super().__init__(context, events=events)
        self._hold_seconds = hold_seconds

    async def execute(self):
        import asyncio

        await asyncio.sleep(self._hold_seconds)
        self.executed = True

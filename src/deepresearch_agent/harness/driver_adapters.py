# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
"""持久化适配与进度事件（原 snapshot/execution_records/evidence_results/report_consistency/evidence_card_coverage/report_metrics/plan_record/_publish_execution_progress）。"""

import json
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from .report_safety import add_inline_citations, citation_evidence_ids, has_internal_material, rank_evidence, sanitize_report


class DriverAdaptersMixin:
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.model_dump(mode="json"),
            "planner_result": self.planner_result.model_dump(mode="json") if self.planner_result else None,
            "report_result": self.report_result.model_dump(mode="json") if self.report_result else None,
            "repair_pending": self._repair_pending,
            "orchestration": self.orchestration,
            "coreact": self.coreact_loop.model_dump(mode="json") if self.coreact_loop else None,
        }

    def execution_records(self) -> list[ExecutionRecord]:
        return list(self.state.execution_records)

    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]:
        output = []
        for record in self.state.execution_records:
            tool_call_id = record.tool_calls[0].tool_call_id if record.tool_calls else None
            provider = record.tool_calls[0].tool_name if record.tool_calls else "unknown"
            for item in record.evidence:
                result = item if isinstance(item, RetrievalResult) else RetrievalResult.from_dict(item)
                output.append((record.task_id, tool_call_id, provider, result))
        return output

    def report_consistency(self) -> bool | None:
        if not self.report_result or has_internal_material(self.report_result.final_report):
            return False
        if self.report_result.consistency_check is None:
            return bool(self.report_result.final_report.strip())
        return self.report_result.consistency_check.is_consistent

    def evidence_card_coverage(self) -> dict[str, Any] | None:
        if self.report_result is not None and self.report_result.evidence_card_coverage:
            return dict(self.report_result.evidence_card_coverage)
        if self.state.report_context and self.state.report_context.evidence_card_coverage:
            return dict(self.state.report_context.evidence_card_coverage)
        return None

    def report_metrics(self) -> dict[str, Any]:
        return dict(self.report_result.report_metrics) if self.report_result else {}

    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        plan = self.state.plan if isinstance(self.state.plan, PlanSpec) else (self.planner_result.plan_spec if self.planner_result else None)
        if plan is None:
            return None
        return plan.model_dump(mode="json"), [node.model_dump(mode="json") for node in plan.task_graph.nodes]

    async def _publish_execution_progress(self, kind, task, record) -> None:
        task_payload = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "description": task.description[:300],
        }
        if kind == "task.started" or record is None:
            await self.events.publish(
                self.context.run_id, "task.started", stage="executing", payload=task_payload,
            )
            return

        for call in record.tool_calls:
            payload = {
                **task_payload,
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": call.status,
            }
            if isinstance(call.args, dict) and call.args.get("query"):
                payload["query"] = str(call.args["query"])[:300]
            if isinstance(call.result, dict):
                result_ids = call.result.get("result_ids", []) or []
                payload["result_count"] = len(result_ids) or len(record.evidence)
                rendered = json.dumps(call.result, ensure_ascii=False, default=str)
                payload["output_preview"] = rendered[:4000]
                payload["output_truncated"] = len(rendered) > 4000
            await self.events.publish(
                self.context.run_id,
                "tool.completed" if call.status != "failed" else "tool.failed",
                stage="executing",
                payload=payload,
            )
        failed = any(call.status == "failed" for call in record.tool_calls) or (
            record.reflection is not None and not record.reflection.success
        )
        await self.events.publish(
            self.context.run_id,
            "task.failed" if failed else "task.completed",
            stage="executing",
            payload={
                **task_payload,
                "record_id": record.record_id,
                "status": "failed" if failed else "completed",
                "evidence_count": len(record.evidence),
            },
        )

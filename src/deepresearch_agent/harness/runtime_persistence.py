# 由 harness/runtime.py 拆分（T-014），方法体与原位置逐行一致。
"""阶段持久化与工件保存（原 _persist_plan 等 5 个方法）。"""
from __future__ import annotations


import json
from typing import Any, Callable
from deepresearch_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.context.artifact_edit import ArtifactEditContextBuilder
from deepresearch_agent.persistence.artifact_store import ArtifactStore


class RuntimePersistMixin:
    async def _persist_plan(self, context: RunContext, driver: Any) -> None:
        if self.trajectory is None:
            return
        record = driver.plan_record()
        if record is None:
            return
        plan, tasks = record
        plan_id = str(plan.get("plan_id") or f"plan_{context.run_id}_{context.plan_version}")
        await self.trajectory.save_plan(run_id=context.run_id, plan_id=plan_id, version=context.plan_version, status=str(plan.get("status", "executing")), plan=plan, tasks=tasks, source_mode=context.source_mode.value)

    async def _persist_execution(self, context: RunContext, driver: Any, budget: BudgetManager) -> None:
        seen_calls: set[str] = set()
        live_events = await self.events.repository.list_after(context.run_id)
        live_task_ids = {
            str(json.loads(item.payload_json or "{}").get("task_id", ""))
            for item in live_events if item.event_type in {"task.completed", "task.failed"}
        }
        live_tool_ids = {
            str(json.loads(item.payload_json or "{}").get("tool_call_id", ""))
            for item in live_events if item.event_type in {"tool.completed", "tool.failed"}
        }
        published_evidence_ids = {item.evidence_id for item in await self.evidence_repository.list_for_run(context.run_id)}
        records = driver.execution_records()
        reported_tokens = sum(
            self._token_usage_total(record.metadata.token_usage)
            for record in records
        )
        if reported_tokens:
            budget.observe_tokens(reported_tokens)
        for record in records:
            if record.task_id not in live_task_ids:
                await self.events.publish(context.run_id, "task.completed", stage="executing", payload={"task_id": record.task_id, "evidence_count": len(record.evidence)})
            for call in record.tool_calls:
                if call.tool_call_id in seen_calls:
                    continue
                seen_calls.add(call.tool_call_id)
                existing = await self.trajectory.get_tool_call(call.tool_call_id) if self.trajectory else None
                if existing is not None and existing.status == "completed":
                    continue
                budget.consume_tool(tavily=call.source_mode == "web")
                if self.trajectory:
                    await self.trajectory.prepare_tool_call(tool_call_id=call.tool_call_id, run_id=context.run_id, task_id=record.task_id, tool_name=call.tool_name, source_mode=context.source_mode.value, args=call.args)
                    await self.trajectory.complete_tool_call(call.tool_call_id, result=call.result, error_code="TOOL_FAILED" if call.status == "failed" else None)
                tool_payload: dict[str, Any] = {"task_id": record.task_id, "tool_call_id": call.tool_call_id, "tool_name": call.tool_name}
                if isinstance(call.args, dict) and call.args.get("query"):
                    tool_payload["query"] = str(call.args["query"])[:300]
                if isinstance(call.result, dict):
                    tool_payload["result_count"] = len(call.result.get("result_ids", []) or [])
                if not tool_payload.get("result_count") and record.evidence:
                    tool_payload["result_count"] = len(record.evidence)
                if call.tool_call_id not in live_tool_ids:
                    await self.events.publish(context.run_id, "tool.completed" if call.status != "failed" else "tool.failed", stage="executing", payload=tool_payload)

        for task_id, tool_call_id, provider, result in driver.evidence_results():
            saved = await self.evidence_ledger.record_results(run_id=context.run_id, task_id=task_id, tool_call_id=tool_call_id, provider=str(provider), results=[result])
            for item in saved:
                if item.evidence_id in published_evidence_ids:
                    continue
                published_evidence_ids.add(item.evidence_id)
                await self.events.publish(context.run_id, "evidence.added", stage="executing", payload={"task_id": task_id, "evidence_id": item.evidence_id, "source_mode": item.source_mode.value})

    @staticmethod
    def _token_usage_total(usage: dict[str, Any] | None) -> int:
        """Normalize provider usage without double-counting a reported total."""
        values = usage or {}
        for key in ("total_tokens", "total", "tokens"):
            if values.get(key) is not None:
                return max(0, int(values[key]))
        input_tokens = next(
            (max(0, int(values[key])) for key in ("input_tokens", "prompt_tokens", "prompt", "input") if values.get(key) is not None),
            0,
        )
        output_tokens = next(
            (max(0, int(values[key])) for key in ("output_tokens", "completion_tokens", "completion", "output") if values.get(key) is not None),
            0,
        )
        if input_tokens or output_tokens:
            return input_tokens + output_tokens
        return sum(max(0, int(value or 0)) for value in values.values())

    async def _save_report_artifact(self, context: RunContext) -> None:
        if self.artifact_store is None:
            return
        artifact = self.artifact_store.write_text(f"{context.run_id}/report/final.md", context.report or "", mime_type="text/markdown; charset=utf-8")
        if self.artifact_repository is not None:
            await self.artifact_repository.record(artifact, run_id=context.run_id)

    def _apply_artifact_edit(self, context: RunContext, generated_report: str) -> str:
        contract = context.context_snapshot.get("artifact_edit")
        if not contract:
            return generated_report
        if self.artifact_store is None:
            raise ValueError("Artifact edit requires an ArtifactStore")
        # 基线被篡改时宁可失败也不能盲目替换章节（摘要随 contract 下发）
        base_report = self.artifact_store.read_verified(contract["relative_path"], contract.get("base_sha256", "")).decode("utf-8")
        return ArtifactEditContextBuilder.apply_replacement(base_report, contract, generated_report)

# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
"""coreact 支撑：账本/查询/重试预算/ContextState（原 _coreact_* 辅助）。"""

import json
from typing import Any
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode


class CoreActSupportMixin:
    def _coreact_ledger_ids(self) -> set[str]:
        return {
            str(getattr(item, "result_id", ""))
            for record in self.state.execution_records
            for item in record.evidence
            if getattr(item, "result_id", None)
        }

    @staticmethod
    def _coreact_query(task: TaskNode) -> str:
        params = task.parameters or {}
        for key in ("query", "search_query", "question"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(task.description or "").strip()

    def _coreact_recent_queries(self) -> list[str]:
        queries = []
        if self.state.execution_context is not None:
            for call in self.state.execution_context.tool_call_history:
                args = call.get("args") if isinstance(call, dict) else None
                query = args.get("query") if isinstance(args, dict) else None
                if query:
                    queries.append(str(query))
        for record in self.state.execution_records:
            for call in record.tool_calls:
                query = call.args.get("query") if isinstance(call.args, dict) else None
                if query:
                    queries.append(str(query))
        return list(dict.fromkeys(queries))[-10:]

    def _coreact_retry_available(self) -> bool:
        limits = self.context.budget_limits
        usage = self.context.budget_usage
        return usage.task_retries < limits.max_task_retries

    def _update_context_state(self, verdict) -> None:
        """T-016：从 StepVerdict 更新可信度分层上下文（QUEST 思想）。"""
        cs = self.coreact_loop.context_state
        cs.updated_step = self.coreact_loop.step_index
        if verdict.passed:
            cs.trusted_ids = sorted(set(cs.trusted_ids) | set(verdict.evidence_ids))
        elif any(c.kind == "source_policy" and not c.passed for c in verdict.criteria):
            cs.untrusted_ids = sorted(set(cs.untrusted_ids) | set(verdict.evidence_ids))
        for c in verdict.criteria:
            if c.kind == "claim_support" and not c.passed and "证据未覆盖主张词" in (c.reason or ""):
                missing = c.reason.split(": ", 1)[-1]
                cs.uncertain.append({
                    "term": missing.strip("[]"),
                    "reason": c.reason,
                    "next_action": f"针对 {missing.strip('[]')} 补充检索",
                })
        cs.uncertain = cs.uncertain[-10:]

    @staticmethod
    def _attach_coreact_verdict(record, rubric, verdict, *, attempt: int, error: str | None) -> None:
        if record is None:
            return
        record.metadata.environment["coreact"] = {
            "rubric": rubric.model_dump(mode="json"),
            "verdict": verdict.model_dump(mode="json"),
            "attempt": attempt,
            "execution_error": error,
        }

    def coreact_rubrics(self) -> list[dict[str, Any]] | None:
        """Collect deduplicated step rubrics from execution-record metadata.

        Returns None for the legacy staged path (the contract check is then
        skipped) or when no rubric was recorded.
        """
        if self.orchestration != "coreact_v1":
            return None
        rubrics: dict[str, dict[str, Any]] = {}
        for record in self.state.execution_records:
            meta = getattr(record.metadata, "environment", None) or {}
            payload = meta.get("coreact") if isinstance(meta, dict) else None
            rubric = payload.get("rubric") if isinstance(payload, dict) else None
            if isinstance(rubric, dict) and rubric.get("rubric_id"):
                rubrics.setdefault(str(rubric["rubric_id"]), rubric)
        return list(rubrics.values()) or None

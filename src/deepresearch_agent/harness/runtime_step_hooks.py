# 由 harness/runtime_execution.py 拆分（T-016），方法体与原位置逐行一致。
"""coreact 步级钩子工厂与暂停仲裁（原 _make_* 4 工厂 + _resolve_coreact_pause）。"""

import json
from typing import Any
from deepresearch_agent.harness.budgets import BudgetManager
from deepresearch_agent.harness.contracts import RunStatus
from deepresearch_agent.harness.errors import RunLeaseLost
from deepresearch_agent.harness.run_context import RunContext


class RuntimeStepHooksMixin:
    def _make_intent_persist(self, context: RunContext, driver: Any, *, owner: str = ""):
        """T-016 intent：动作派发前持久化 plan/task 行（running）与游标 checkpoint。"""

        async def _persist_intent(current_driver: Any) -> None:
            if self.trajectory is None:
                return
            if owner:
                run = await self.runs.get(context.run_id)
                if run is None or run.lease_owner != owner:
                    raise RunLeaseLost("租约已易主，放弃 intent 写入")
            await self._persist_plan(context, current_driver)
            current_driver.context.budget_usage = budget_holder_usage(context)
            context.workflow_state = current_driver.snapshot()
            await self.checkpoints.save(context, "executing")

        def budget_holder_usage(ctx):
            return ctx.budget_usage

        return _persist_intent

    def _make_lease_probe(self, context: RunContext, owner: str):
        """T-016 fencing：动作边界校验租约仍归属本实例；失效即让位。"""

        async def _lease_held() -> bool:
            run = await self.runs.get(context.run_id)
            return run is not None and run.lease_owner == owner

        return _lease_held

    def _make_step_commit(self, context: RunContext, driver: Any, budget: BudgetManager, *, owner: str = ""):
        """Build the per-action persistence callback for step-driven drivers."""
        last_published_step = {"value": -1}

        async def _step_commit(current_driver: Any) -> None:
            # 闭包捕获 owner 供 _commit_coreact_step 做 fencing 校验
            # Budget accounting is owned by the runtime; the driver only records
            # what it consumed so far.
            current_driver.context.budget_usage = budget.usage
            await self._commit_coreact_step(context, current_driver, budget, owner=owner)
            loop = getattr(current_driver, "coreact_loop", None)
            step_index = getattr(loop, "step_index", 0)
            if step_index != last_published_step["value"]:
                # 同一 step 的崩溃重放（重放走 _persist_execution，不经此回调）
                # 不会重复发布；这里再按 step_index 防御一次。
                last_published_step["value"] = step_index
                await self.events.publish(
                    context.run_id,
                    "coreact.step_committed",
                    stage="executing",
                    payload={
                        "step_index": step_index,
                        "decisions_used": getattr(loop, "decisions_used", 0),
                        "remaining_tokens": budget.remaining_tokens(),
                    },
                )

        return _step_commit

    def _make_action_commit(self, context: RunContext, driver: Any, budget: BudgetManager, *, owner: str = ""):
        """T-404：批内逐动作提交回调——只持久化，不发 step 事件。

        step 事件保持在批次边界（step_commit）；逐动作提交只做幂等落库，
        使崩溃恢复不丢已合并结果、不重做已提交动作。
        """

        async def _action_commit(current_driver: Any) -> None:
            current_driver.context.budget_usage = budget.usage
            await self._commit_coreact_step(context, current_driver, budget, owner=owner)

        return _action_commit

    def _make_pause_probe(self, context: RunContext):
        """Build the pause probe used at action boundaries by step drivers."""

        async def _pause_requested() -> bool:
            run = await self.runs.get(context.run_id)
            if run is None:
                return False
            if run.status == "pausing":
                return True
            snapshot = json.loads(run.config_snapshot_json or "{}")
            return bool(snapshot.get("pause_requested"))

        return _pause_requested

    async def _resolve_coreact_pause(self, context: RunContext) -> RunStatus:
        """Arbitrate pause vs a DB-level cancel: cancellation wins, otherwise pause.

        Returns the terminal/boundary status the run landed in. Resume would
        clear pause/cancel flags, so a lost cancellation here would be final.
        """
        run = await self.runs.get(context.run_id)
        if run is not None and run.cancellation_requested:
            context.cancellation_requested = True
            if context.status is not RunStatus.CANCELLING:
                await self._transition(context, RunStatus.CANCELLING, event_type="run.cancelling")
            await self._transition(context, RunStatus.CANCELLED, event_type="run.cancelled")
            return RunStatus.CANCELLED
        context.resume_from_status = RunStatus.EXECUTING
        # 先持久化续跑目标，再写 paused（见 _pause_at_safe_boundary 的崩溃窗口说明）
        await self.runs.set_pause_resume_status(context.run_id, "executing")
        await self._transition(context, RunStatus.PAUSED, event_type="run.paused")
        await self.runs.clear_pause_request(context.run_id)
        return RunStatus.PAUSED

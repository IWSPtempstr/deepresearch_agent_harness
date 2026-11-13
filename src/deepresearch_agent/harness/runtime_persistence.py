# 由 harness/runtime.py 拆分（T-014），方法体与原位置逐行一致。
"""阶段持久化与工件保存（原 _persist_plan 等 5 个方法）。"""
from __future__ import annotations


import json
import hashlib
from typing import Any
from deepresearch_agent.harness.budgets import BudgetManager
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.context.artifact_edit import ArtifactEditContextBuilder


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

    async def _commit_coreact_step(self, context: RunContext, driver: Any, budget: BudgetManager, *, owner: str = "") -> None:
        """Persist one controller action durably, then snapshot the loop state.

        Idempotency: plan/task rows use stable IDs and tool calls/evidence use
        stable IDs, so re-running this after a crash re-writes the same rows
        instead of duplicating them. The checkpoint keeps the loop cursor so a
        takeover resumes at the next action instead of replaying the batch.
        """
        if self.trajectory is None:
            return
        # T-016 fencing（check-then-write）：租约已易主时本实例绝不能再写。
        # TOCTOU 窗口仍在（校验与写入非原子），全量条件 UPDATE 留后续硬化。
        if owner:
            run = await self.runs.get(context.run_id)
            if run is None or run.lease_owner != owner:
                from deepresearch_agent.harness.errors import RunLeaseLost

                raise RunLeaseLost("租约已易主，放弃步级提交")
        await self._persist_plan(context, driver)
        await self._persist_execution(context, driver, budget)
        context.budget_usage = budget.usage
        context.workflow_state = driver.snapshot()
        await self.checkpoints.save(context, "executing")

    async def _published_event_keys(self, run_id: str) -> tuple[set[str], set[str]]:
        """Collect task/tool keys of already-published events, paging past the
        500-row default window so late-run dedup still sees early events."""
        live_task_ids: set[str] = set()
        live_tool_ids: set[str] = set()
        after_event_id = 0
        while True:
            batch = await self.events.repository.list_after(run_id, after_event_id=after_event_id, limit=1000)
            if not batch:
                break
            for item in batch:
                payload = json.loads(item.payload_json or "{}")
                if item.event_type in {"task.completed", "task.failed"}:
                    live_task_ids.add(str(payload.get("task_id", "")))
                elif item.event_type in {"tool.completed", "tool.failed"}:
                    live_tool_ids.add(str(payload.get("tool_call_id", "")))
            after_event_id = batch[-1].event_id
            if len(batch) < 1000:
                break
        return live_task_ids, live_tool_ids

    async def _persist_execution(self, context: RunContext, driver: Any, budget: BudgetManager) -> None:
        seen_calls: set[str] = set()
        live_task_ids, live_tool_ids = await self._published_event_keys(context.run_id)
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
                if existing is None:
                    # 计费跟随首次写入：行已存在（含 failed）说明首次提交已计费，
                    # 崩溃重放/步级重复提交不得重复 consume_tool。
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
        return ArtifactEditContextBuilder.apply_replacement(
            base_report, contract, generated_report,
            citation_mapping=contract.get("citation_mapping"),
        )

    async def _prepare_artifact_edit(self, context: RunContext) -> None:
        """BC-04：校验修订契约、把基线引用合法归入新 Run 并建立 citation mapping。

        - 基线必须是同一会话的 completed Run，artifact 路径/摘要与登记一致；
        - 保留章节（含首个标题前的导语）引用的证据必须存在于基线 Run 账本；
        - 为每条旧证据创建**新 ID** 的继承行（provenance 指向基线），不改写/移动
          基线行；
        - 映射写入契约，`apply_replacement` 据此改写保留章节里的旧引用，使新 Run
          的 citation_integrity/claim_support 能针对当前 Run 证据通过。
        """
        contract = context.context_snapshot.get("artifact_edit")
        if not isinstance(contract, dict):
            return
        from deepresearch_agent.harness.errors import AppError, ErrorCode

        if self.artifact_store is None or self.artifact_repository is None or self.evidence_repository is None:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订缺少 artifact/evidence 依赖")
        if contract.get("schema_version") != ArtifactEditContextBuilder.CONTRACT_SCHEMA_VERSION:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订契约版本不受支持")
        if contract.get("operation") != "replace_section":
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订操作必须是 replace_section")
        base_run_id = str(contract.get("base_run_id") or "")
        if not base_run_id or base_run_id == context.run_id:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订目标无效：缺少合法基线 Run")
        base_run = await self.runs.get(base_run_id)
        if (
            base_run is None or base_run.session_id != context.session_id
            or base_run.status != "completed"
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                "修订目标必须是同一会话的已完成 Run（拒绝跨会话/未完成基线）",
            )
        artifact = await self.artifact_repository.get_report(base_run_id)
        if artifact is None or artifact.relative_path != contract.get("relative_path"):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订基线 artifact 不存在或路径不一致")
        declared_artifact_id = contract.get("artifact_id")
        if not isinstance(declared_artifact_id, str) or not declared_artifact_id:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订契约缺少 artifact_id")
        if declared_artifact_id != artifact.artifact_id:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订基线 artifact_id 与登记不一致")
        if contract.get("base_sha256") != artifact.sha256:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订基线摘要与登记不一致")
        base_report = self.artifact_store.read_verified(artifact.relative_path, artifact.sha256).decode("utf-8")
        sections = ArtifactEditContextBuilder.parse_sections(base_report)
        headings = [section.heading for section in sections]
        if len(headings) != len(set(headings)):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订基线存在重复章节标题，无法判定目标")
        outline = contract.get("outline")
        if not isinstance(outline, list) or [str(item) for item in outline] != headings:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订契约 outline 与基线章节不一致")
        target_heading = str(contract.get("target_heading") or "")
        targets = [section for section in sections if section.heading == target_heading]
        if len(targets) != 1:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订目标章节在基线中不存在或不唯一")
        target = targets[0]
        expected_target = contract.get("target_sha256")
        if not isinstance(expected_target, str) or not expected_target:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订契约缺少 target_sha256")
        if expected_target != target.sha256:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订目标章节摘要已变化")

        # 保留集合必须与基线完全一致：缺失/空列表/多余标题都视为篡改
        actual_preserved = [section for section in sections if section.heading != target.heading]
        preserve_entries = contract.get("preserve_sections")
        if not isinstance(preserve_entries, list):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订 preserve_sections 形状非法")
        declared_headings = [
            str(item.get("heading", "")) for item in preserve_entries if isinstance(item, dict)
        ]
        if (
            len(preserve_entries) != len(actual_preserved)
            or sorted(declared_headings) != sorted(section.heading for section in actual_preserved)
        ):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订保留章节集合与基线不一致")
        by_heading = {section.heading: section for section in sections}
        for entry in preserve_entries:
            section = by_heading.get(str(entry.get("heading")))
            if section is None or section.sha256 != entry.get("sha256"):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                    f"修订保留章节摘要与基线不一致: {entry.get('heading')}",
                )

        preserved_text = base_report.replace(target.content, "", 1)
        citations = sorted(set(ArtifactEditContextBuilder.CITATION.findall(preserved_text)))
        declared_citations = contract.get("base_citations")
        if (
            not isinstance(declared_citations, list)
            or sorted(str(item) for item in declared_citations) != citations
        ):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "修订 base_citations 与基线内容不一致")
        base_evidence = await self.evidence_repository.list_for_run(base_run_id)
        by_id = {item.evidence_id: item for item in base_evidence}
        missing = [citation for citation in citations if citation not in by_id]
        if missing:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                "修订保留内容引用了基线 Run 账本之外的证据",
                details={"missing_evidence_ids": missing[:10]},
            )

        from datetime import datetime, timezone

        inherited_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        mapping: dict[str, str] = {}
        for old_id in citations:
            base = by_id[old_id]
            new_id = ArtifactEditContextBuilder.inherited_evidence_id(
                context.run_id, old_id, base.content_hash or "",
            )
            mapping[old_id] = new_id
            try:
                base_metadata = json.loads(base.metadata_json or "{}")
            except (TypeError, ValueError):
                base_metadata = {}
            metadata = {
                **(base_metadata if isinstance(base_metadata, dict) else {}),
                "inherited": True,
                "run_id": context.run_id,
                "origin_run_id": (base_metadata.get("run_id") if isinstance(base_metadata, dict) else None) or base_run_id,
                "base_run_id": base_run_id,
                "base_evidence_id": old_id,
                "base_task_id": base.task_id,
                "base_tool_call_id": base.tool_call_id,
                "base_content_hash": base.content_hash,
                "target_heading": target_heading,
                "inherited_at": inherited_at,
            }
            await self.evidence_repository.inherit(
                base, run_id=context.run_id, evidence_id=new_id, metadata=metadata,
            )

        updated = dict(contract)
        updated["citation_mapping"] = dict(sorted(mapping.items()))
        updated["base_citations"] = citations
        updated["inherited_evidence_ids"] = sorted(mapping.values())
        preserved: list[dict[str, Any]] = []
        for entry in contract.get("preserve_sections", []) or []:
            heading = entry.get("heading")
            section = next((item for item in sections if item.heading == heading), None)
            if section is None:
                raise AppError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "保留章节在基线中不存在")
            mapped_content = ArtifactEditContextBuilder.rewrite_citations(section.content, mapping)
            preserved.append({
                **entry,
                "mapped_sha256": hashlib.sha256(mapped_content.encode("utf-8")).hexdigest(),
            })
        updated["preserve_sections"] = preserved
        context.context_snapshot["artifact_edit"] = updated

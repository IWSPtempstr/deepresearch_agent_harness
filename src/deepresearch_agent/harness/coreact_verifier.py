"""coreact_v1 步级验证器（T-015/T-202）。

确定性检查优先；语义检查仅在预算门控允许时运行，且：
- 语义结果只能追加失败（更严格的判罚），不能把确定性 fail 升级为 pass；
- 语义输出畸形/异常 → 静默降级为纯确定性判定，Run 不因此失败。

输入由 driver 组装：records（本动作的 ExecutionRecord，含重试）、
ledger_ids（账本内 evidence_id 集合）、executed_queries（本动作之前已执行查询）、
allowed_tools（SourcePolicy 派生 + 内部工具白名单）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from deepresearch_agent.agents.multi_agent.core.rubric import (
    CriterionVerdict,
    StepVerdict,
    TaskRubric,
)

VERIFIER_VERSION = "coreact_verifier_v1"

# 执行器内部工具（非检索面），不算违反 source policy
INTERNAL_TOOLS = frozenset({"answer_validator"})

SemanticFn = Callable[[str, List[Dict[str, Any]]], Dict[str, Any]]


def _record_evidence_texts(records: Iterable[Any]) -> List[Dict[str, Any]]:
    """抽取 (evidence_id, text) 视图；id 以账本回填的 result_id 为准。"""
    views: List[Dict[str, Any]] = []
    for record in records or []:
        for item in getattr(record, "evidence", None) or []:
            evidence_id = str(getattr(item, "result_id", "") or "")
            content = getattr(item, "evidence", "")
            if isinstance(content, dict):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except (TypeError, ValueError):
                    content = str(content)
            views.append({"evidence_id": evidence_id, "text": str(content or "")})
    return views


class CoreActVerifier:
    def __init__(self, *, version: str = VERIFIER_VERSION):
        self.version = version

    def verify(
        self,
        *,
        rubric: TaskRubric,
        task: Any,
        records: List[Any],
        ledger_ids: Set[str],
        executed_queries: Set[str],
        allowed_tools: Set[str],
        semantic_allowed: bool = False,
        semantic_fn: Optional[SemanticFn] = None,
    ) -> StepVerdict:
        criteria_verdicts: List[CriterionVerdict] = []
        evidence_views = _record_evidence_texts(records)
        ledger_backed = [
            view for view in evidence_views if view["evidence_id"] in ledger_ids and view["text"]
        ]

        for criterion in rubric.criteria:
            passed, reason, ev_ids = self._check_criterion(
                criterion_kind=criterion.kind,
                min_evidence=criterion.min_evidence,
                required_terms=criterion.required_terms,
                task=task,
                records=records,
                evidence_views=evidence_views,
                ledger_backed=ledger_backed,
                executed_queries=executed_queries,
                allowed_tools=allowed_tools,
            )
            criteria_verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.criterion_id,
                    kind=criterion.kind,
                    passed=passed,
                    reason=reason,
                    evidence_ids=ev_ids,
                )
            )

        # 语义检查（可选）：只能追加失败，不能升级确定性 fail；畸形/异常静默忽略
        semantic_note: Optional[str] = None
        if rubric.semantic_check and semantic_allowed and callable(semantic_fn):
            query = self._action_query(task)
            try:
                payload = semantic_fn(query, ledger_backed)
                if isinstance(payload, dict) and payload.get("passed") is False:
                    criteria_verdicts.append(
                        CriterionVerdict(
                            criterion_id=f"{rubric.task_id}_semantic_support",
                            kind="claim_support",
                            passed=False,
                            reason=str(payload.get("reason") or "语义检查判定证据不足以支撑任务目标"),
                            evidence_ids=[v["evidence_id"] for v in ledger_backed[:5]],
                        )
                    )
                elif isinstance(payload, dict) and payload.get("passed") is True:
                    semantic_note = str(payload.get("reason") or "语义检查通过（不改变确定性判定）")
            except Exception:
                semantic_note = "语义检查异常，降级为确定性判定"

        total_weight = sum(c.weight for c in rubric.criteria) or 1.0
        passed_weight = sum(
            c.weight for c, v in zip(rubric.criteria, criteria_verdicts[: len(rubric.criteria)]) if v.passed
        )
        weighted_score = round(passed_weight / total_weight, 4)
        required_all_passed = all(
            v.passed for c, v in zip(rubric.criteria, criteria_verdicts[: len(rubric.criteria)]) if c.required
        )
        # 语义追加的失败判罚也必须拉低通过判定
        passed = required_all_passed and weighted_score >= rubric.threshold and all(
            v.passed for v in criteria_verdicts[len(rubric.criteria):]
        )

        retry_reason = None
        if not passed:
            failed = [v for v in criteria_verdicts if not v.passed and v.kind != "no_duplicate_query"]
            retry_reason = "；".join(
                f"[{v.criterion_id}] {v.reason}" for v in failed[:3]
            ) or "步级验证未达标"

        return StepVerdict(
            rubric_id=rubric.rubric_id,
            task_id=rubric.task_id,
            passed=passed,
            weighted_score=weighted_score,
            threshold=rubric.threshold,
            criteria=criteria_verdicts,
            retry_reason=retry_reason,
            evidence_ids=[v["evidence_id"] for v in ledger_backed],
            verifier=self.version + (f"+{semantic_note}" if semantic_note else ""),
        )

    def _check_criterion(
        self,
        *,
        criterion_kind: str,
        min_evidence: int,
        required_terms: List[str],
        task: Any,
        records: List[Any],
        evidence_views: List[Dict[str, Any]],
        ledger_backed: List[Dict[str, Any]],
        executed_queries: Set[str],
        allowed_tools: Set[str],
    ) -> tuple[bool, str, List[str]]:
        if criterion_kind == "evidence_presence":
            ids = [v["evidence_id"] for v in ledger_backed]
            passed = len(set(ids)) >= max(1, min_evidence)
            reason = "" if passed else f"账本内新证据 {len(set(ids))} 条，少于要求 {min_evidence} 条"
            return passed, reason, list(dict.fromkeys(ids))

        if criterion_kind == "source_policy":
            tool_calls = [call for record in records or [] for call in getattr(record, "tool_calls", None) or []]
            if not tool_calls:
                return False, "动作没有产生任何工具调用", []
            violations = sorted(
                {
                    call.tool_name
                    for call in tool_calls
                    if call.tool_name not in allowed_tools and call.tool_name not in INTERNAL_TOOLS
                }
            )
            passed = not violations
            reason = "" if passed else f"工具超出冻结来源白名单: {violations}"
            return passed, reason, []

        if criterion_kind == "output_schema":
            has_output = bool(ledger_backed) or any(
                call.status == "success" and call.result not in (None, "", {}, [])
                for record in records or []
                for call in getattr(record, "tool_calls", None) or []
            )
            return has_output, ("" if has_output else "动作没有可消费的执行输出"), []

        if criterion_kind == "claim_support":
            if not required_terms:
                return True, "无可判定主张词，跳过代理判定", []
            haystack = "\n".join(v["text"].lower() for v in ledger_backed)
            missing = [term for term in required_terms if term.lower() not in haystack]
            passed = not missing and bool(ledger_backed)
            reason = "" if passed else f"证据未覆盖主张词: {missing}"
            return passed, reason, [v["evidence_id"] for v in ledger_backed]

        if criterion_kind == "no_duplicate_query":
            query = self._action_query(task)
            duplicated = bool(query) and query in executed_queries
            return (not duplicated), ("重复执行无增量查询" if duplicated else ""), []

        # kind 为 Literal；restore 侧（coreact_state._pending_action_valid）会把
        # 版本偏斜的 pending rubric 清空而非放行。若仍到达此处即代码缺陷——
        # 大声失败（由调用方转为该动作的失败记录），绝不静默放行。
        raise ValueError(f"未处理的 criterion kind {criterion_kind!r}")

    @staticmethod
    def _action_query(task: Any) -> str:
        params = getattr(task, "parameters", None) or {}
        if isinstance(params, dict):
            for key in ("query", "search_query", "question"):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return str(getattr(task, "description", "") or "").strip()

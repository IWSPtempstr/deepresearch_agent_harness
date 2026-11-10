"""T-203: coreact_controller 步级决策单测（scripted LLM，无真实模型）。"""

import json

from deepresearch_agent.harness.coreact_controller import (
    ControllerDecision,
    CoreActController,
    DecisionContext,
)


def make_context(**overrides):
    base = dict(
        goal="调研检索增强技术路线",
        source_mode="web",
        eligible_tasks=[
            {"task_id": "plan_1", "task_type": "web_search", "description": "检索技术路线综述", "source_mode": "web"}
        ],
        completed_count=0,
        evidence_count=0,
        allow_llm=True,
    )
    base.update(overrides)
    return DecisionContext(**base)


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return SimpleContent(payload)


class SimpleContent:
    def __init__(self, content):
        self.content = content


def llm_json(payload):
    return json.dumps(payload, ensure_ascii=False)


CANDIDATE_ACTION = {
    "ready_to_report": False,
    "task": {"task_type": "web_search", "description": "对比 RAG 与长上下文", "query": "RAG vs long context 对比研究"},
    "reason": "需要补充对比维度",
}


class TestLLMPath:
    def test_valid_action_decision(self):
        controller = CoreActController(llm=FakeLLM([llm_json(CANDIDATE_ACTION)]))
        decision = controller.decide(make_context())
        assert decision.action is not None
        assert decision.action.task_type == "web_search"
        assert decision.action.parameters["query"] == "RAG vs long context 对比研究"
        assert decision.action.source_mode == "web"
        assert decision.source == "llm"
        assert decision.ready_to_report is False

    def test_ready_to_report_decision(self):
        controller = CoreActController(llm=FakeLLM([llm_json({"ready_to_report": True, "reason": "证据足够"})]))
        decision = controller.decide(make_context())
        assert decision.ready_to_report is True and decision.action is None
        assert "证据足够" in decision.reason

    def test_fenced_json_parsed(self):
        fenced = "前置说明\n```json\n" + llm_json(CANDIDATE_ACTION) + "\n```\n后缀"
        controller = CoreActController(llm=FakeLLM([fenced]))
        decision = controller.decide(make_context())
        assert decision.action is not None

    def test_malformed_then_valid(self):
        controller = CoreActController(llm=FakeLLM(["不是 JSON", llm_json(CANDIDATE_ACTION)]))
        decision = controller.decide(make_context())
        assert decision.action is not None and decision.source == "llm"
        assert controller.llm.calls == 2

    def test_invalid_task_payload_counts_as_malformed_then_fallback(self):
        bad = llm_json({"ready_to_report": False, "task": {"description": "缺 task_type"}, "reason": "r"})
        controller = CoreActController(llm=FakeLLM([bad, bad]))
        decision = controller.decide(make_context())
        assert decision.action is not None  # 兜底候选任务
        assert decision.source == "fallback_candidate"
        assert controller.llm.calls == 2

    def test_llm_exception_degrades_to_fallback(self):
        controller = CoreActController(llm=FakeLLM([RuntimeError("llm down"), RuntimeError("llm down")]))
        decision = controller.decide(make_context())
        assert decision.source == "fallback_candidate"

    def test_unknown_task_type_rejected(self):
        bad = llm_json({"ready_to_report": False, "task": {"task_type": "quantum_search", "description": "d"}})
        controller = CoreActController(llm=FakeLLM([bad, bad]))
        decision = controller.decide(make_context())
        assert decision.source == "fallback_candidate"


class TestDeterministicFallback:
    def test_llm_disabled_skips_invoke(self):
        llm = FakeLLM([llm_json(CANDIDATE_ACTION)])
        controller = CoreActController(llm=llm)
        decision = controller.decide(make_context(allow_llm=False))
        assert decision.source == "fallback_candidate" and llm.calls == 0

    def test_decision_budget_exhausted_skips_invoke(self):
        llm = FakeLLM([llm_json(CANDIDATE_ACTION)])
        controller = CoreActController(llm=llm)
        decision = controller.decide(make_context(decisions_used=8, max_decisions=8))
        assert decision.source == "fallback_candidate" and llm.calls == 0

    def test_no_candidates_falls_back_to_ready(self):
        controller = CoreActController(llm=FakeLLM([RuntimeError("x"), RuntimeError("x")]))
        decision = controller.decide(make_context(eligible_tasks=[]))
        assert decision.ready_to_report is True
        assert decision.source == "fallback_ready"

    def test_candidate_pick_is_first_eligible(self):
        tasks = [
            {"task_id": "plan_a", "task_type": "web_search", "description": "A", "source_mode": "web"},
            {"task_id": "plan_b", "task_type": "web_search", "description": "B", "source_mode": "web"},
        ]
        controller = CoreActController(llm=None)
        decision = controller.decide(make_context(eligible_tasks=tasks, allow_llm=False))
        assert decision.action.task_id == "plan_a"


class TestDecisionModel:
    def test_decision_defaults(self):
        decision = ControllerDecision()
        assert decision.action is None and decision.ready_to_report is False


class TestHardening:
    def test_bare_string_response_uses_str_branch(self):
        # LLM 返回无 .content 属性的对象 → hasattr 分支按 str() 处理
        class Bare:
            pass

        controller = CoreActController(llm=FakeLLM([llm_json(CANDIDATE_ACTION)]))
        controller.llm.responses[0] = Bare()  # 无 content
        decision = controller.decide(make_context())
        assert decision.source == "fallback_candidate"  # str(Bare) 无 JSON → 畸形 → 兜底

    def test_exception_call_count_is_two_total(self):
        llm = FakeLLM([RuntimeError("x"), RuntimeError("x"), RuntimeError("x")])
        controller = CoreActController(llm=llm)
        decision = controller.decide(make_context())
        assert llm.calls == 2  # 总尝试上限
        assert decision.source == "fallback_candidate"

    def test_oversized_response_rejected(self):
        from deepresearch_agent.harness.coreact_controller import MAX_DECISION_CONTENT_CHARS

        huge = llm_json(CANDIDATE_ACTION) + " " * (MAX_DECISION_CONTENT_CHARS + 1)
        controller = CoreActController(llm=FakeLLM([huge, huge]))
        decision = controller.decide(make_context())
        assert decision.source == "fallback_candidate"  # 超长内容 → 畸形 → 兜底

    def test_second_fence_recovered_when_first_invalid(self):
        first = "```json\n{broken```"
        second = "```json\n" + llm_json(CANDIDATE_ACTION) + "\n```"
        controller = CoreActController(llm=FakeLLM([first + "\n" + second]))
        decision = controller.decide(make_context())
        assert decision.action is not None

    def test_ready_report_ignores_invalid_task(self):
        payload = {"ready_to_report": True, "task": {"task_type": "quantum_search", "description": "x"}, "reason": "完成"}
        controller = CoreActController(llm=FakeLLM([llm_json(payload)]))
        decision = controller.decide(make_context())
        assert decision.ready_to_report is True and decision.action is None

    def test_query_falls_back_to_description(self):
        payload = {"ready_to_report": False, "task": {"task_type": "web_search", "description": "检索增强发展史"}, "reason": "r"}
        controller = CoreActController(llm=FakeLLM([llm_json(payload)]))
        decision = controller.decide(make_context())
        assert decision.action.parameters["query"] == "检索增强发展史"

    def test_non_dict_payload_rejected(self):
        controller = CoreActController(llm=FakeLLM(["[1,2,3]", "[1,2,3]"]))
        decision = controller.decide(make_context())
        assert decision.source == "fallback_candidate"

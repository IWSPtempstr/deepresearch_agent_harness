"""T-305: 角色模型注入与归因一致性（recorded == actual）。"""
import asyncio

from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.run_context import RunContext


ROLE_MODELS = {
    "planner": {"model": "planner-model-x", "tier": "complex", "reason": "role tier complex"},
    "reporter": {"model": "reporter-model-y", "tier": "standard", "reason": "role tier standard"},
    "controller": {"model": "controller-model-z", "tier": "standard", "reason": "role tier standard"},
}


class FakeSettings:
    OPENAI_LLM_MODEL = "global-default"


def make_context(**overrides):
    values = dict(
        run_id="run-role-tier", session_id="session-role-tier", trigger_message_id="msg-role-tier",
        source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status=RunStatus.PLANNING, original_query="研究问题",
        config_snapshot={"orchestration": "coreact_v1"},
    )
    values.update(overrides)
    return RunContext(**values)


class TestFactoryInjection:
    def test_bundle_components_use_role_models(self):
        bundle = MultiAgentFactory.create_default_bundle(role_models=ROLE_MODELS)
        # LLM 实例的 model_name 字段是归因断言点（recorded == actual）
        assert bundle.planner._llm.model_name == "planner-model-x"
        assert bundle.reporter._outline_builder._llm.model_name == "reporter-model-y"
        assert bundle.reporter._section_writer._llm.model_name == "reporter-model-y"
        # controller 模型随 bundle 透传
        assert bundle.controller_model == "controller-model-z"  # ID 供审计
        assert bundle.orchestrator.controller_model.model_name == "controller-model-z"  # 实例供 invoke

    def test_no_role_models_falls_back_to_global(self):
        bundle = MultiAgentFactory.create_default_bundle()
        assert bundle.controller_model is None
        assert bundle.planner._llm.model_name == bundle.reporter._outline_builder._llm.model_name  # 同全局默认
        assert bundle.orchestrator.controller_model.model_name == bundle.planner._llm.model_name  # controller 回落 planner LLM


class TestDriverControllerWiring:
    def test_coreact_controller_uses_role_model(self):
        context = make_context()
        bundle = MultiAgentFactory.create_default_bundle(role_models=ROLE_MODELS)
        driver = PlanExecuteReportDriver(context, bundle.orchestrator)
        assert getattr(driver._coreact_controller.llm, "model_name", None) == "controller-model-z"

    def test_staged_driver_still_constructs_with_controller_model(self):
        context = make_context(config_snapshot={})
        bundle = MultiAgentFactory.create_default_bundle(role_models=ROLE_MODELS)
        driver = PlanExecuteReportDriver(context, bundle.orchestrator)
        assert driver.orchestration == "staged_v1"
        assert getattr(driver._coreact_controller.llm, "model_name", None) == "controller-model-z"  # staged 不用 controller，但构造无害

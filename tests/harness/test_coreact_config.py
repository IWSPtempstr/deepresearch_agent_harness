"""T-207: orchestration stamp on Run creation."""
import pytest

from deepresearch_agent.config import settings


class TestOrchestrationStamp:
    def test_default_is_coreact_v1(self):
        assert settings.RESEARCH_ORCHESTRATION == "coreact_v1"

    def test_invalid_value_rejected_at_import_time(self, monkeypatch):
        import importlib

        monkeypatch.setenv("RESEARCH_ORCHESTRATION", "bogus_mode")
        # 拆分后按依赖顺序 reload：sections 的赋值语句读取 env 时必须真的重执行
        import deepresearch_agent.config.settings_core as core
        import deepresearch_agent.config.settings_sections as sections

        with pytest.raises(ValueError):
            importlib.reload(core)
            importlib.reload(sections)
            importlib.reload(settings)
        monkeypatch.undo()
        importlib.reload(core)
        importlib.reload(sections)
        importlib.reload(settings)
        assert settings.RESEARCH_ORCHESTRATION == "coreact_v1"

    def test_chat_service_stamps_orchestration_for_plan_execute_report(self):
        """chat_service builds snapshot with orchestration for plan_execute_report only."""
        import inspect

        from backend.app.services import chat_service

        source = inspect.getsource(chat_service)
        assert 'snapshot["orchestration"] = settings.RESEARCH_ORCHESTRATION' in source
        # 仅 plan_execute_report 戳记：deep_research 不带该键（保持 legacy 语义）
        assert 'request.workflow_mode.value == "plan_execute_report"' in source

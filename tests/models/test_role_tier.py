"""T-304: 角色级模型解析单测。"""
import pytest

from deepresearch_agent.models.model_tier import (
    resolve_model_for,
    resolve_role_model,
)


class FakeSettings:
    OPENAI_LLM_MODEL = "global-default"
    LLM_TIER_SIMPLE_MODEL = "tier-simple"
    LLM_TIER_STANDARD_MODEL = "tier-standard"
    LLM_TIER_COMPLEX_MODEL = "tier-complex"
    LLM_ROLE_TIER_DEFAULTS = ""


class TestResolveRoleModel:
    def test_builtin_role_defaults(self):
        s = FakeSettings()
        assert resolve_role_model({}, role="planner", settings=s)["model"] == "tier-complex"
        assert resolve_role_model({}, role="worker", settings=s)["model"] == "tier-simple"
        assert resolve_role_model({}, role="reporter", settings=s)["model"] == "tier-standard"
        assert resolve_role_model({}, role="controller", settings=s)["model"] == "tier-standard"

    def test_env_override_wins_over_builtin(self):
        s = FakeSettings()
        s.LLM_ROLE_TIER_DEFAULTS = "planner:standard,worker:complex"
        assert resolve_role_model({}, role="planner", settings=s)["model"] == "tier-standard"
        assert resolve_role_model({}, role="worker", settings=s)["model"] == "tier-complex"
        assert resolve_role_model({}, role="reporter", settings=s)["model"] == "tier-standard"  # 未覆盖走内置

    def test_explicit_role_model_wins_over_everything(self):
        s = FakeSettings()
        result = resolve_role_model({"reporter_model": "my-reporter-8b"}, role="reporter", settings=s)
        assert result["model"] == "my-reporter-8b"
        assert result["tier"] == "explicit-reporter"

    def test_malformed_env_pairs_ignored(self):
        s = FakeSettings()
        s.LLM_ROLE_TIER_DEFAULTS = "garbage,planner:bogus,:simple,planner:complex"
        assert resolve_role_model({}, role="planner", settings=s)["model"] == "tier-complex"

    def test_unknown_role_falls_back_to_standard(self):
        s = FakeSettings()
        assert resolve_role_model({}, role="unknown_role", settings=s)["model"] == "tier-standard"

    def test_run_tier_fallback_when_tier_model_unset(self):
        s = FakeSettings()
        s.LLM_TIER_COMPLEX_MODEL = ""  # complex 未配置 → 回落全局
        result = resolve_role_model({"model_tier": "complex"}, role="planner", settings=s)
        assert result["model"] == "global-default"


class TestRunLevelUnchanged:
    def test_resolve_model_for_still_works(self):
        s = FakeSettings()
        assert resolve_model_for({"model_tier": "simple"}, settings=s)["model"] == "tier-simple"
        assert resolve_model_for({"report_type": "long_document"}, settings=s)["model"] == "tier-complex"

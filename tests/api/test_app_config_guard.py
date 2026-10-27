"""create_app 配置守卫（评审审计：CONFIGURATION_INVALID 补抛出点）。"""

import pytest

from backend.app.main import create_app
from deepresearch_agent.config import settings
from deepresearch_agent.harness.errors import AppError, ErrorCode


def test_single_worker_without_redis_raises_typed_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_MVP_SINGLE_WORKER", False, raising=False)
    monkeypatch.setattr(settings, "REDIS_URL", "", raising=False)
    with pytest.raises(AppError) as excinfo:
        create_app(workflow_factory=lambda context, events=None: None, auto_resume=False)
    assert excinfo.value.code == ErrorCode.CONFIGURATION_INVALID
    assert "REDIS_URL" in excinfo.value.message

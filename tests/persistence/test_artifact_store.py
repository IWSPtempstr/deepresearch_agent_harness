"""ArtifactStore 完整性校验（评审审计：ARTIFACT_INTEGRITY_FAILED 补抛出点）。"""

import pytest

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence import ArtifactStore


def test_read_verified_passes_on_matching_digest(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    stored = store.write_text("run-1/report/final.md", "# 报告\n\n正文", mime_type="text/markdown")
    content = store.read_verified(stored.relative_path, stored.sha256)
    assert content.decode("utf-8").startswith("# 报告")


def test_read_verified_rejects_tampered_content(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    stored = store.write_text("run-1/report/final.md", "# 原始内容")
    (tmp_path / "artifacts" / "run-1" / "report" / "final.md").write_text("# 被篡改", encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        store.read_verified(stored.relative_path, stored.sha256)
    assert excinfo.value.code == ErrorCode.ARTIFACT_INTEGRITY_FAILED
    assert excinfo.value.details["relative_path"] == stored.relative_path


def test_read_verified_rejects_missing_file(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(AppError) as excinfo:
        store.read_verified("run-x/report/final.md", "a" * 64)
    assert excinfo.value.code == ErrorCode.ARTIFACT_INTEGRITY_FAILED


def test_read_verified_allows_unregistered_digest(tmp_path):
    """expected_sha256 为空 = 未登记摘要，放行读取（兼容无记录调用方）。"""
    store = ArtifactStore(tmp_path / "artifacts")
    store.write_text("run-1/notes.txt", "随手写")
    assert store.read_verified("run-1/notes.txt", "").decode("utf-8") == "随手写"

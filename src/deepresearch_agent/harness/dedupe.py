"""S1：查询去重键——按“工具 + 查询”而非全局 query。

聚合通道（如 web 的 Tavily + OpenAlex/arXiv）下，同一查询打到不同 API 是有增量
的动作；只有**同一工具重复同一查询**才算无增量。集中此函数避免 5 处实现漂移。
"""

from __future__ import annotations


def query_dedupe_key(tool_name: str | None, query: str | None) -> str:
    """返回稳定去重键；空查询返回空串（调用方跳过查重）。"""
    normalized = str(query or "").strip()
    if not normalized:
        return ""
    return f"{str(tool_name or '').strip()}:{normalized}"


def agent_query_key(key: str) -> str:
    """从去重键取回查询部分（用于展示/审计，不参与比较）。"""
    _, separator, query = str(key or "").partition(":")
    return query if separator else str(key or "")


def is_duplicate_query(tool_name: str | None, query: str | None, executed: set[str] | list[str]) -> bool:
    """判断 (tool, query) 是否已执行。

    新调用方传 `(tool, query)` 键；旧调用方/旧快照可能只传原始 query。键匹配
    直接命中；原始 query 匹配是**保守兼容**：旧表示没有工具信息，无法区分
    “同 query 不同 API”，因此旧条目会对任意工具都判为重复（宁可少查一次，
    也不重复外呼）。新表示（带工具前缀）不受此影响。
    """
    normalized = str(query or "").strip()
    if not normalized:
        return False
    key = query_dedupe_key(tool_name, normalized)
    return key in executed or normalized in executed


__all__ = ["query_dedupe_key", "agent_query_key", "is_duplicate_query"]

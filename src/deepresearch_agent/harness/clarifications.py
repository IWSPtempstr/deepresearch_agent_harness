"""BC-01：澄清请求/回答的持久协议与消费端。

背景（docs/specs/coreact-consolidation/rubric-guidance-followup.md §6）：

- 旧实现只把澄清答案写进 ``config_snapshot["clarifications"]``，没有任何
  消费端；运行恢复后仍按原问题重新构建上下文，可能再次要求澄清；
- ``needs_user_input`` 只落一个通用提示，前端 ClarityCard 展示的不是**实际
  问题**；
- 同一澄清被重复提交时没有幂等键，答案会在历史里重复累积。

本模块定义冻结的请求/回答形状与纯函数，供三处消费：

1. ContextBuilder：把已回答内容并入 resolved query 与上下文块，并阻止对同一
   Run 重复澄清；
2. BriefBuilder：把问答对注入 PlanContext.clarification_history；
3. API/前端：从 ``config_snapshot`` 读取**实际问题**展示。

语义边界：回答只是用户输入，不是研究证据；不参与 Completion Contract。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLARIFICATION_SCHEMA_VERSION = 1
MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 10_000
MAX_ANSWER_HISTORY = 16
REQUEST_KEY = "clarification_request"
ANSWERS_KEY = "clarifications"
LEGACY_QUESTION_KEY = "clarification_question"

ClarificationStatus = Literal["pending", "answered"]
ClarificationSource = Literal["context_builder", "brief", "manual"]


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _digest(value: str, *, length: int = 40) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class ClarificationRequest(BaseModel):
    """一次待回答的澄清请求（存于 Run config_snapshot）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CLARIFICATION_SCHEMA_VERSION
    question_id: str = Field(min_length=1, max_length=64)
    questions: list[str] = Field(min_length=1, max_length=MAX_QUESTIONS)
    revision: int = Field(default=1, ge=1)
    source: ClarificationSource = "manual"
    asked_at: str = ""
    status: ClarificationStatus = "pending"
    answered_at: str | None = None
    answer_id: str | None = None

    @field_validator("questions", mode="before")
    @classmethod
    def _clean_questions(cls, value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("questions 必须是列表")
        cleaned: list[str] = []
        for item in value:
            text = _clean_text(item)
            if not text:
                continue
            if len(text) > MAX_QUESTION_CHARS:
                text = text[: MAX_QUESTION_CHARS - 1] + "…"
            if text not in cleaned:
                cleaned.append(text)
        cleaned = cleaned[:MAX_QUESTIONS]
        if not cleaned:
            raise ValueError("澄清问题不得为空")
        return cleaned


class ClarificationAnswer(BaseModel):
    """一次澄清回答（用户输入；可回溯到消息与请求版本）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CLARIFICATION_SCHEMA_VERSION
    clarification_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    question_id: str | None = Field(default=None, max_length=64)
    questions: list[str] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    revision: int | None = Field(default=None, ge=1)
    message_id: str | None = Field(default=None, max_length=64)
    created_at: str = ""

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("澄清回答不得为空")
        if len(text) > MAX_ANSWER_CHARS:
            raise ValueError(f"澄清回答超过 {MAX_ANSWER_CHARS} 字符")
        return text


def clarification_key(
    run_id: str, *, client_id: str | None = None, revision: int | None = None,
    content: str = "",
) -> str:
    """稳定幂等键：客户端键优先；缺省按 Run/修订/回答内容派生。"""
    basis = (
        f"client:{client_id}" if client_id
        else f"content:{revision or 0}:{_clean_text(content)}"
    )
    return f"clarify-{_digest(f'{run_id}|{basis}')}"


def _parse_request(payload: Any) -> ClarificationRequest | None:
    if not isinstance(payload, dict):
        return None
    try:
        request = ClarificationRequest.model_validate(payload)
    except Exception:  # noqa: BLE001 —— 损坏历史不阻塞 Run
        return None
    if request.schema_version > CLARIFICATION_SCHEMA_VERSION:
        return None
    return request


def request_from_config(config_snapshot: Any) -> ClarificationRequest | None:
    """读取结构化请求；旧 Run 的 ``clarification_question`` 作为只读回退。"""
    snapshot = config_snapshot if isinstance(config_snapshot, dict) else {}
    request = _parse_request(snapshot.get(REQUEST_KEY))
    if request is not None:
        return request
    legacy = _clean_text(snapshot.get(LEGACY_QUESTION_KEY))
    if not legacy:
        return None
    return ClarificationRequest(
        question_id="clar_legacy", questions=[legacy], revision=1, source="manual",
    )


def pending_request(config_snapshot: Any) -> ClarificationRequest | None:
    request = request_from_config(config_snapshot)
    return request if request is not None and request.status == "pending" else None


def build_request(
    config_snapshot: Any, *, questions: list[str], source: ClarificationSource,
    now: str,
) -> ClarificationRequest:
    """构造/推进请求：已回答过的请求再次触发时 revision + 1。"""
    snapshot = config_snapshot if isinstance(config_snapshot, dict) else {}
    existing = _parse_request(snapshot.get(REQUEST_KEY))
    cleaned = ClarificationRequest(questions=questions, question_id="pending").questions
    if existing is not None and existing.status == "pending" and existing.questions == cleaned:
        return existing  # 同一未回答请求：重复触发不推进版本
    revision = 1 if existing is None else (
        existing.revision + 1 if existing.status == "answered" else existing.revision
    )
    question_id = f"clar_{revision}_{_digest('|'.join(cleaned), length=8)}"
    return ClarificationRequest(
        question_id=question_id, questions=cleaned, revision=revision,
        source=source, asked_at=now, status="pending",
    )


def store_request(config_snapshot: dict[str, Any], request: ClarificationRequest) -> None:
    """写回结构化请求（并保留旧键供历史读取）。"""
    config_snapshot[REQUEST_KEY] = request.model_dump(mode="json")
    config_snapshot[LEGACY_QUESTION_KEY] = request.questions[0]


def parse_answers(config_snapshot: Any) -> list[ClarificationAnswer]:
    """容错读取回答历史；损坏条目跳过，不阻塞 Run。"""
    snapshot = config_snapshot if isinstance(config_snapshot, dict) else {}
    raw = snapshot.get(ANSWERS_KEY)
    if not isinstance(raw, list):
        return []
    answers: list[ClarificationAnswer] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            answer = ClarificationAnswer.model_validate(item)
        except Exception:  # noqa: BLE001
            continue
        if answer.schema_version > CLARIFICATION_SCHEMA_VERSION:
            continue
        answers.append(answer)
    return answers


def has_answers(config_snapshot: Any) -> bool:
    return bool(parse_answers(config_snapshot))


def answer_texts(config_snapshot: Any) -> list[str]:
    return [answer.content for answer in parse_answers(config_snapshot)]


def answers_block(config_snapshot: Any) -> str:
    """上下文/规划使用的有界回答块（无回答返回空串）。"""
    answers = parse_answers(config_snapshot)
    if not answers:
        return ""
    lines = ["# USER CLARIFICATIONS（用户补充信息，仅作输入约束；不是研究证据）"]
    for index, answer in enumerate(answers[-MAX_ANSWER_HISTORY:], start=1):
        questions = answer.questions or ([answer.question_id] if answer.question_id else [])
        if questions:
            lines.append(f"- 问题：{'；'.join(questions)}")
        lines.append(f"  回答（{index}）：{answer.content}")
    return "\n".join(lines)


def merge_into_history(config_snapshot: Any, history: list[dict[str, Any]] | None) -> None:
    """把已回答问答对并入 PlanContext.clarification_history（去重）。"""
    if not isinstance(history, list):
        return
    for answer in parse_answers(config_snapshot):
        questions = answer.questions or ([answer.question_id] if answer.question_id else [])
        for question in questions:
            entry = {"question": question, "answer": answer.content}
            if entry not in history:
                history.append(entry)


def apply_answer(
    config_snapshot: dict[str, Any], *,
    clarification_id: str,
    content: str,
    message_id: str | None = None,
    question_id: str | None = None,
    created_at: str,
) -> tuple[str, dict[str, Any]]:
    """把回答写入快照（纯函数）。

    返回 ``(outcome, snapshot)``，outcome 为：
    - ``recorded``：新回答已记录，当前请求标记 answered；
    - ``duplicate``：同一幂等键与内容已存在（不重复写入）；
    - ``conflict``：同一幂等键对应不同内容（调用方应 409）；
    - ``question_mismatch``：请求存在但 question_id 不匹配（调用方应 409）。

    快照取浅拷贝；``clarifications`` 列表写回为有界新列表。
    """
    snapshot = dict(config_snapshot or {})
    raw = snapshot.get(ANSWERS_KEY)
    entries = [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    for entry in entries:
        if entry.get("clarification_id") != clarification_id:
            continue
        if _clean_text(entry.get("content")) != content:
            return "conflict", snapshot
        return "duplicate", snapshot

    request = _parse_request(snapshot.get(REQUEST_KEY))
    if request is not None and question_id and question_id != request.question_id:
        return "question_mismatch", snapshot
    answer = ClarificationAnswer(
        clarification_id=clarification_id, content=content,
        question_id=request.question_id if request is not None else question_id,
        questions=request.questions if request is not None else [],
        revision=request.revision if request is not None else None,
        message_id=message_id, created_at=created_at,
    )
    entries.append(answer.model_dump(mode="json"))
    snapshot[ANSWERS_KEY] = entries[-MAX_ANSWER_HISTORY:]
    if request is not None and request.status == "pending":
        answered = request.model_copy(update={
            "status": "answered", "answered_at": created_at, "answer_id": clarification_id,
        })
        snapshot[REQUEST_KEY] = answered.model_dump(mode="json")
    return "recorded", snapshot


def public_view(request: ClarificationRequest | None) -> dict[str, Any] | None:
    """用户可见视图：只暴露问题/版本/状态，不透出内部推理。"""
    if request is None:
        return None
    return {
        "question_id": request.question_id,
        "questions": list(request.questions),
        "revision": request.revision,
        "status": request.status,
        "asked_at": request.asked_at or None,
        "answered_at": request.answered_at,
    }

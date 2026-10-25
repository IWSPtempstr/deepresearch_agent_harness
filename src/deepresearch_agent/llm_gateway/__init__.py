"""LLM 网关（P1-5）：provider failover、令牌桶限流排队、token 计量落库。

嵌入层次选择：LangChain 的 bind_tools / with_structured_output 会返回绑定
底层模型的 Runnable，外层包装类拦不到这些派生调用；因此 failover/限流/计量
统一实现在 BaseChatModel 传输层（_generate/_agenerate），所有派生 Runnable
天然继承，业务代码零改动。
"""

from deepresearch_agent.llm_gateway.gateway import (
    FailoverChatOpenAI, LlmRateLimited, ProviderHealth, SlotLimiter, build_failover_llm,
)
from deepresearch_agent.llm_gateway.metering import LlmCallRecorder, get_recorder, start_metering, stop_metering

__all__ = [
    "FailoverChatOpenAI", "LlmRateLimited", "ProviderHealth", "SlotLimiter", "build_failover_llm",
    "LlmCallRecorder", "get_recorder", "start_metering", "stop_metering",
]

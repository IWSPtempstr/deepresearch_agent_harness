# 由 agents/base.py 拆分（T-014），方法体与原位置逐行一致。
"""LangGraph 构图与节点/流式节点（原 _setup_graph 等 7 个方法，含抽象 _add_retrieval_edges/_generate_node）。"""

from typing import Annotated, Sequence, TypedDict, Dict, Any, AsyncGenerator
from abc import ABC, abstractmethod
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.graph.message import add_messages
import asyncio


class AgentGraphMixin(ABC):
    def _setup_graph(self):
        """设置工作流图 - 基础结构，子类可以通过_add_retrieval_edges自定义"""
        # 定义状态类型
        class AgentState(TypedDict):
            messages: Annotated[Sequence[BaseMessage], add_messages]

        # 创建工作流图
        workflow = StateGraph(AgentState)
        
        # 添加节点 - 节点与原始代码保持一致
        workflow.add_node("agent", self._agent_node)
        workflow.add_node("retrieve", ToolNode(self.tools))
        workflow.add_node("generate", self._generate_node)
        
        # 添加从开始到Agent的边
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges(
            "agent",
            tools_condition,
            {
                "tools": "retrieve",
                END: END,
            },
        )
        
        # 添加从检索到生成的边 - 这个逻辑由子类实现
        self._add_retrieval_edges(workflow)
        
        # 从生成到结束
        workflow.add_edge("generate", END)
        
        # 编译图
        self.graph = workflow.compile(checkpointer=self.memory)

    async def _stream_process(self, inputs: Dict[str, Any], config: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """
        执行流式处理的默认实现
        
        子类应该覆盖此方法以实现特定的流式处理逻辑
        
        参数:
            inputs: 输入消息
            config: 配置
            
        返回:
            AsyncGenerator[str, None]: 流式响应生成器
        """
        # 获取消息
        messages = inputs.get("messages", [])
        query = messages[-1].content if messages else ""
        
        # 构建状态字典
        state = {
            "messages": messages,
            "configurable": config.get("configurable", {})
        }
        
        # 获取生成结果
        result = await self._generate_node_async(state)
        
        if "messages" in result and result["messages"]:
            message = result["messages"][0]
            content = message.content if hasattr(message, "content") else str(message)
            
            # 按句子或段落分块，更自然
            import re
            chunks = re.split(r'([.!?。！？]\s*)', content)
            buffer = ""
            
            for i in range(0, len(chunks)):
                if i < len(chunks):
                    buffer += chunks[i]
                    
                    # 当缓冲区包含完整句子或达到合理大小时输出
                    if (i % 2 == 1) or len(buffer) >= self.stream_flush_threshold:
                        yield buffer
                        buffer = ""
                        await asyncio.sleep(0.01)  # 微小延迟确保流畅显示
            
            # 输出任何剩余内容
            if buffer:
                yield buffer
        else:
            yield "无法生成响应。"

    @abstractmethod
    def _add_retrieval_edges(self, workflow):
        """添加从检索到生成的边，子类必须实现"""
        pass

    def _agent_node(self, state):
        """Agent 节点逻辑"""
        messages = state["messages"]
        
        # 提取关键词优化查询
        if len(messages) > 0 and isinstance(messages[-1], HumanMessage):
            query = messages[-1].content
            keywords = self._extract_keywords(query)
            
            # 记录关键词
            self._log_execution("extract_keywords", query, keywords)
            
            # 增强消息，添加关键词信息
            if keywords:
                # 创建一个新的消息，带有关键词元数据
                enhanced_message = HumanMessage(
                    content=query,
                    additional_kwargs={"keywords": keywords}
                )
                # 替换原始消息
                messages = messages[:-1] + [enhanced_message]
        
        # 使用工具处理请求
        # 强制先检索后回答：DeepResearch 的完成契约要求证据支撑，
        # 若模型自行决定不调用检索工具，将产生无证据答案并在 verify 阶段空转。
        model = self.llm.bind_tools(self.tools)
        system = SystemMessage(
            content=(
                "你是深度研究助手。回答用户问题前，你必须先调用检索工具（如 deep_research / "
                "tavily_search）获取真实证据；未调用任何检索工具时不得直接作答。"
                "检索结果不足时应继续迭代检索，不要仅凭模型自身知识或编造内容作答。"
            )
        )
        response = model.invoke([system, *messages])
        
        self._log_execution("agent", messages, response)
        return {"messages": [response]}

    @abstractmethod
    def _generate_node(self, state):
        """生成回答节点逻辑，子类必须实现"""
        pass

    async def _generate_node_stream(self, state):
        """
        生成回答节点逻辑的流式版本
        
        参数:
            state: 当前状态
            
        返回:
            AsyncGenerator[str, None]: 流式响应生成器
        """
        # 默认实现 - 应由子类覆盖
        result = self._generate_node(state)
        if "messages" in result and result["messages"]:
            message = result["messages"][0]
            content = message.content if hasattr(message, "content") else str(message)
            
            # 模拟流式输出
            for i in range(0, len(content), self.chunk_size):
                yield content[i:i+self.chunk_size]
                await asyncio.sleep(0.01)

    async def _generate_node_async(self, state):
        """
        生成回答节点逻辑的异步版本
        
        参数:
            state: 当前状态
            
        返回:
            Dict: 包含消息的结果字典
        """
        # 这个默认实现只是调用同步版本
        # 子类应该提供真正的异步实现
        def sync_generate():
            return self._generate_node(state)
            
        # 在线程池中运行同步代码，避免阻塞事件循环
        return await asyncio.to_thread(sync_generate)

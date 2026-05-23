"""
Abstract Agent Interface.

所有 Agent 必须实现此接口，从而与 CLI / WebSocket / REST 解耦。
替换 Agent 只需实现新的子类，基础设施层无需改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentEvent:
    """
    统一的事件结构，用于 WebSocket / CLI 输出。

    type 约定：
      "log"     —— 过程日志，显示在日志区
      "result"  —— 最终结果/报告（完整文本）
      "stream"  —— 流式文本增量（逐 token 推送，与 result 配合使用）
      "status"  —— 状态变更（running / done / error）
      "tokens"  —— Token 用量（metadata 可含 input/output/total/provider/model）
      "custom"  —— Agent 自定义事件
    """
    type: str
    content: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
        }


class AgentInterface(ABC):
    """
    所有 Agent 的抽象基类。

    生命周期契约（重要）：
        run() 由 core.runner.run_agent() 统一驱动。run_agent 负责：
          - 调用 on_start，并 emit status=running
          - 正常结束后调用 on_finish，并 emit status=done
          - run() 抛异常时调用 on_error，并 emit error + status=error
        因此 Agent 内部 **不要** 自己调钩子、也不要 emit status / error 事件，
        只 yield 领域事件（log / tokens / result / custom），失败时直接抛异常。

    使用方式：
        class MyAgent(AgentInterface):
            async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
                yield AgentEvent(type="log", content="开始处理...")
                result = await do_something(task)   # 失败就让它抛，runner 会兜
                yield AgentEvent(type="result", content=result)

    接入新 Agent 只需：
        1. 继承 AgentInterface
        2. 实现 run() 异步生成器
        3. 在 .env 中设置 AGENT_CLASS=agents.my_agent.MyAgent
    """

    def __init__(self, config=None, websocket=None):
        self.config = config
        self.websocket = websocket      # 由基础设施层注入，Agent 内部可用于流式输出
        self.status: AgentStatus = AgentStatus.IDLE

    @abstractmethod
    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        """
        核心执行方法，必须是异步生成器。

        Args:
            task: 用户输入的任务/查询
            **kwargs: Agent 扩展参数

        Yields:
            AgentEvent: 过程事件流
        """
        ...

    @property
    def name(self) -> str:
        """Agent 的展示名称，子类可覆盖"""
        return self.__class__.__name__

    @property
    def description(self) -> str:
        """Agent 的功能描述，子类可覆盖"""
        return "An agent."

    async def on_start(self, task: str) -> None:
        """任务开始前的钩子，由 run_agent 调用；子类可覆盖（覆盖时记得维护 status）"""
        self.status = AgentStatus.RUNNING

    async def on_finish(self) -> None:
        """任务正常结束后的钩子，由 run_agent 调用；子类可覆盖"""
        self.status = AgentStatus.DONE

    async def on_error(self, error: Exception) -> None:
        """run() 抛异常时的钩子，由 run_agent 调用；子类可覆盖"""
        self.status = AgentStatus.ERROR

"""
WebSocket Manager

负责：
  - 管理 WebSocket 连接生命周期
  - 将 Agent 的 AsyncIterator[AgentEvent] 流式推送到前端
  - 与具体 Agent 实现完全解耦（通过 AgentInterface 约定）
"""

import importlib
import logging
from typing import Dict, Optional, Set

from fastapi import WebSocket

from core.agent_interface import AgentInterface, AgentEvent
from core.config import Config
from core.llm.base import TokenUsage
from core.runner import run_agent

logger = logging.getLogger(__name__)


def load_agent_class(agent_class_path: str) -> type:
    """
    动态加载 Agent 类。

    Args:
        agent_class_path: 形如 "agents.echo_agent.EchoAgent"

    Returns:
        AgentInterface 的子类
    """
    module_path, class_name = agent_class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    agent_cls = getattr(module, class_name)
    if not issubclass(agent_cls, AgentInterface):
        raise TypeError(f"{class_name} 必须继承 AgentInterface")
    return agent_cls


class WebSocketManager:
    """
    管理所有活跃的 WebSocket 连接。

    设计原则：
      - 每个连接独立维护一个消息队列
      - Agent 生成的 AgentEvent 通过队列异步推送
      - 连接断开时自动清理资源
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config.from_env()
        self.active: Set[WebSocket] = set()   # 仅追踪活跃连接用于计数；事件直接经 _send_event 推送
        self._session_usage: Dict[WebSocket, TokenUsage] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)
        self._session_usage[websocket] = TokenUsage()
        logger.info(f"WebSocket connected. 当前连接数: {len(self.active)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)
        self._session_usage.pop(websocket, None)
        logger.info(f"WebSocket disconnected. 当前连接数: {len(self.active)}")

    async def handle(self, websocket: WebSocket) -> None:
        """
        处理单个 WebSocket 连接的完整生命周期。

        前端消息格式：
            { "type": "run", "task": "<用户输入>", "kwargs": {...} }
            { "type": "ping" }
        """
        await self.connect(websocket)
        try:
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif msg_type == "run":
                    task = data.get("task", "").strip()
                    kwargs = data.get("kwargs", {})
                    if not task:
                        await websocket.send_json({
                            "type": "error",
                            "content": "task 不能为空"
                        })
                        continue
                    await self._run_agent(websocket, task, **kwargs)

                else:
                    await websocket.send_json({
                        "type": "error",
                        "content": f"未知消息类型: {msg_type}"
                    })

        except Exception as e:
            logger.warning(f"WebSocket 处理异常: {e}")
        finally:
            await self.disconnect(websocket)

    async def _run_agent(self, websocket: WebSocket, task: str, **kwargs) -> None:
        """
        加载 Agent 并将事件流推送到 WebSocket。

        加载/实例化错误在此处理；run() 阶段的生命周期与异常→error 事件
        由 core.runner.run_agent 统一负责。
        """
        try:
            agent_cls = load_agent_class(self.config.agent_class)
            agent = agent_cls(config=self.config, websocket=websocket)
        except Exception as e:
            logger.error(f"加载 Agent 失败: {e}", exc_info=True)
            await self._send_event(
                websocket,
                AgentEvent(type="error", content=f"加载 Agent 失败: {str(e)}"),
            )
            return

        logger.info(f"启动 Agent: {agent.name}，任务: {task[:50]}...")

        # 收集本次任务的 tokens 事件，用于会话级累计
        task_tokens_list: list[TokenUsage] = []
        task_cumulative: TokenUsage | None = None

        async for event in run_agent(agent, task, **kwargs):
            await self._send_event(websocket, event)
            if event.type == "tokens":
                meta = event.metadata
                usage = TokenUsage(
                    input_tokens=meta.get("input_tokens", 0),
                    output_tokens=meta.get("output_tokens", 0),
                )
                if meta.get("scope") == "cumulative":
                    # Agent 已汇总本任务全部调用，直接用这个
                    task_cumulative = usage
                elif meta.get("scope") is None:
                    # 单次调用事件（无 scope 字段）
                    task_tokens_list.append(usage)

        # 确定本任务的 Token 总量（有 cumulative 优先用，否则累加单次事件）
        task_total = task_cumulative if task_cumulative is not None else sum(
            task_tokens_list, TokenUsage()
        )

        # 累加到本连接的会话级计数器
        if websocket in self._session_usage:
            self._session_usage[websocket] = self._session_usage[websocket] + task_total
            s = self._session_usage[websocket]
            await self._send_event(
                websocket,
                AgentEvent(
                    type="tokens",
                    content=(
                        f"[会话累计] "
                        f"input={s.input_tokens}  "
                        f"output={s.output_tokens}  "
                        f"total={s.total_tokens}"
                    ),
                    metadata={**s.to_dict(), "scope": "session"},
                ),
            )

    async def _send_event(self, websocket: WebSocket, event: AgentEvent) -> None:
        try:
            await websocket.send_json(event.to_dict())
        except Exception as e:
            logger.warning(f"推送事件失败: {e}")

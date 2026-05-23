"""
流式输出工具。

统一处理 WebSocket 推送 + 控制台日志，
是基础设施层与 Agent 之间的通信桥梁。
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def stream_output(
    type: str,
    content: str,
    websocket: Optional[Any] = None,
    metadata: Optional[dict] = None,
    log_to_console: bool = True,
) -> None:
    """
    向 WebSocket 推送事件，同时写入日志。

    消息格式（与前端约定）：
        {
            "type":     "log" | "result" | "status" | "tokens" | "custom",
            "content":  "<文本内容>",
            "metadata": {...}   // 可选
        }

    Args:
        type:           事件类型
        content:        事件内容
        websocket:      FastAPI WebSocket 实例（为 None 时仅打印日志）
        metadata:       附加元数据
        log_to_console: 是否同时打印到控制台
    """
    if log_to_console:
        prefix = {
            "log": "📋",
            "result": "✅",
            "status": "⚙️",
            "tokens": "🔢",
            "error": "❌",
        }.get(type, "ℹ️")
        logger.info(f"{prefix} [{type}] {content}")

    if websocket:
        payload = {"type": type, "content": content}
        if metadata:
            payload["metadata"] = metadata
        try:
            await websocket.send_json(payload)
        except Exception as e:
            logger.warning(f"WebSocket send failed: {e}")

"""
WS 路径多轮验证 —— 在单个 WebSocket 连接上跑两轮，证明后端 session 在连接内贯穿
（这是新前端「一个连接=一个 session、不每轮清空」所依赖的后端保证）。

前置：服务已在 ws://127.0.0.1:8000/ws 运行。
    $PY tests/ws_multiturn.py
"""

import asyncio
import json

import websockets

URL = "ws://127.0.0.1:8000/ws"
TURNS = [
    "Transformer 注意力机制",
    "刚才报告里的多头注意力是什么意思",
]


async def run_turn(ws, task: str) -> None:
    print(f"\n=== 发送：{task} ===")
    await ws.send(json.dumps({"type": "run", "task": task, "kwargs": {}}))
    async for raw in ws:
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "log" and msg["content"].startswith("意图"):
            print("  [intent]", msg["content"])
        elif t == "status":
            print("  [status]", msg["content"])
            if msg["content"] in ("done", "error"):
                return
        elif t == "error":
            print("  [error]", msg["content"])
            return


async def main() -> None:
    async with websockets.connect(URL, open_timeout=10, ping_interval=None) as ws:
        for task in TURNS:
            await run_turn(ws, task)
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())

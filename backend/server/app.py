"""
FastAPI 主应用

路由：
  GET  /                  → 前端页面
  GET  /api/info          → Agent 信息（名称、描述）
  POST /api/run           → 同步执行 Agent（适合脚本调用）
  WS   /ws                → WebSocket 流式执行
"""

import importlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import Config
from core.agent_interface import AgentInterface
from core.runner import run_agent
from backend.server.websocket_manager import WebSocketManager, load_agent_class

logger = logging.getLogger(__name__)

# ── 配置 & 全局对象 ────────────────────────────────────────────────
cfg = Config.from_env()
ws_manager = WebSocketManager(config=cfg)

FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"


# ── 生命周期 ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Agent Shell 启动  |  Agent: {cfg.agent_class}")
    yield
    logger.info("Agent Shell 关闭")


# ── 应用初始化 ─────────────────────────────────────────────────────
app = FastAPI(
    title="Agent Shell",
    description="可替换 Agent 的通用基础设施",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载前端静态文件
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── 请求/响应模型 ──────────────────────────────────────────────────
class RunRequest(BaseModel):
    task: str
    kwargs: dict = {}


# ── 路由 ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """返回前端页面"""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="前端文件未找到")
    return HTMLResponse(content=index.read_text(encoding="utf-8"))


@app.get("/api/info")
async def agent_info():
    """返回当前 Agent 的元信息"""
    try:
        agent_cls = load_agent_class(cfg.agent_class)
        agent = agent_cls(config=cfg)
        return {
            "name": agent.name,
            "description": agent.description,
            "agent_class": cfg.agent_class,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/run")
async def run_agent_sync(req: RunRequest):
    """
    同步执行 Agent，收集所有事件后返回。
    适合脚本/测试调用，不适合长任务（用 WebSocket 代替）。
    """
    try:
        agent_cls = load_agent_class(cfg.agent_class)
        agent = agent_cls(config=cfg)

        events = []
        result = None

        async for event in run_agent(agent, req.task, **req.kwargs):
            events.append(event.to_dict())
            if event.type == "result":
                result = event.content

        return {"result": result, "events": events}

    except Exception as e:
        logger.error(f"同步执行出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 流式执行入口"""
    try:
        await ws_manager.handle(websocket)
    except WebSocketDisconnect:
        logger.info("WebSocket 客户端主动断开")
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}", exc_info=True)

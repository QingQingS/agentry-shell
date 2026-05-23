"""
服务启动入口

等同于：uvicorn backend.server.app:app --reload
"""

import uvicorn
import os
import sys
#AGENT_CLASS=agents.research_agent.ResearchAgent python main.py
# 确保项目根目录在 sys.path 里
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import Config

if __name__ == "__main__":
    cfg = Config.from_env()
    uvicorn.run(
        "backend.server.app:app",
        host=cfg.host,
        port=cfg.port,
        reload=True,
        log_level="info",
    )

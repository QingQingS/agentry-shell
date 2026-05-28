"""
Agent 注册表 —— v2 hub-and-spoke 的 pluggable 落点。

Coordinator 不再 if/elif 硬编码路由，而是查这张表：
    - `Task(agent=...)` 的 agent 字段在此查到对应 spoke 的工厂；
    - 每个 spec 的 description / input_contract / output_contract 渲染进
      Coordinator 的 system prompt（见 catalog()），于是 Coordinator 天然知道
      「选谁、给什么 prompt、会拿回什么」，无需暴露 spoke 内部细节（如 ResearchMode）。

设计约束：
    - spec 携带 factory: (config, websocket) -> AgentInterface，每次派发新建实例
      （上下文隔离：spoke 不共享状态）。
    - 本层纯数据 + 工厂，零 LLM、零执行逻辑（执行在 core/dispatch.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from core.agent_interface import AgentInterface

AgentFactory = Callable[..., AgentInterface]


@dataclass
class AgentSpec:
    """注册表中的一个 spoke 条目。"""

    name: str                 # dispatch key，即 Task.agent 的取值
    description: str          # 何时选它（渲染进 Coordinator system prompt）
    input_contract: str       # 该给什么 prompt/context
    output_contract: str      # 会拿回什么
    factory: AgentFactory     # (config, websocket) -> 新 agent 实例


class AgentRegistry:
    """按 name 持有 AgentSpec；向 Coordinator 暴露 catalog()，向 dispatch 暴露 get()。"""

    def __init__(self, specs: List[AgentSpec]):
        self._specs: Dict[str, AgentSpec] = {s.name: s for s in specs}

    def get(self, name: str) -> Optional[AgentSpec]:
        return self._specs.get(name)

    def names(self) -> List[str]:
        return list(self._specs.keys())

    def catalog(self) -> str:
        """渲染成给 Coordinator system prompt 的 agent 清单。"""
        blocks = []
        for s in self._specs.values():
            blocks.append(
                f"- {s.name}: {s.description}\n"
                f"    输入: {s.input_contract}\n"
                f"    返回: {s.output_contract}"
            )
        return "\n".join(blocks)


def build_default_registry() -> AgentRegistry:
    """生产用注册表：当前两个真正干活的 spoke。"""
    from agents.research_agent import ResearchAgent
    from agents.wiki_agent import WikiAgent

    return AgentRegistry([
        AgentSpec(
            name="researcher",
            description=(
                "研究员：给定一个研究问题或要查的目标，自主决定怎么做"
                "（广度调研 / 找某篇论文 / 找开源代码实现），检索并综合成一份报告。"
            ),
            input_contract=(
                "prompt = 一个自包含、可直接检索的研究子任务（指代须已消解）；"
                "context = 可选的上游背景。"
            ),
            output_contract="一份研究报告（artifact 落盘），附一句话结论摘要。",
            factory=lambda config=None, websocket=None: ResearchAgent(
                config=config, websocket=websocket
            ),
        ),
        AgentSpec(
            name="wiki_curator",
            description=(
                "知识策展：把一份/多份 .md 文档整合进持久化的主题 wiki"
                "（读相关页、写/更新页、重生 index）。"
            ),
            input_contract=(
                "prompt = 归档指令，含要处理的 .md 文件路径"
                "（如「把 reports/foo.md 归档进 wiki」）；context = 可选背景。"
            ),
            output_contract="写入/更新了哪些 wiki 页面的摘要。",
            factory=lambda config=None, websocket=None: WikiAgent(
                config=config, websocket=websocket
            ),
        ),
    ])

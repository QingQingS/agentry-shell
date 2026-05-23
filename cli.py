"""
CLI 入口

支持两种模式：
  1. 单次执行：  python cli.py "你的任务"
  2. 交互式对话：python cli.py --interactive

用法：
  python cli.py "分析 LangGraph 的架构设计"
  python cli.py "..." --agent agents.my_agent.MyAgent
  python cli.py --interactive
"""

import argparse
import asyncio
import importlib
import sys
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from core.config import Config
from core.agent_interface import AgentInterface, AgentEvent
from core.runner import run_agent


# ── ANSI 颜色（终端支持时启用）────────────────────────────────────
def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def dim(t):    return _color(t, "2")
def green(t):  return _color(t, "32")
def yellow(t): return _color(t, "33")
def red(t):    return _color(t, "31")
def bold(t):   return _color(t, "1")
def cyan(t):   return _color(t, "36")


def load_agent(agent_class_path: str, config: Config) -> AgentInterface:
    """动态加载 Agent 类并实例化"""
    module_path, class_name = agent_class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    agent_cls = getattr(module, class_name)
    return agent_cls(config=config)


async def run_task(task: str, config: Config) -> Optional[str]:
    """执行单个任务，将事件流打印到终端"""
    agent = load_agent(config.agent_class, config)

    print(f"\n{bold('▶ Agent')}  {cyan(agent.name)}")
    print(f"{bold('▶ 任务')}  {task}")
    print(dim("─" * 50))

    result = None
    streaming = False  # True after first "stream" event received

    async for event in run_agent(agent, task):
        if event.type == "stream":
            if not streaming:
                streaming = True
                print(f"\n{bold(green('📝 生成报告'))}  ", end="", flush=True)
            print(event.content, end="", flush=True)
        elif event.type == "result":
            if streaming:
                print()  # 换行，结束流式输出
            else:
                _print_event(event)
            result = event.content
        else:
            _print_event(event)

    print(dim("─" * 50))
    return result


def _print_event(event: AgentEvent) -> None:
    """将 AgentEvent 格式化输出到终端"""
    if event.type == "log":
        print(f"  {dim('›')} {event.content}")

    elif event.type == "result":
        print(f"\n{bold(green('✅ 结果'))}")
        print("─" * 50)
        print(event.content)

    elif event.type == "status":
        icons = {"running": yellow("⚙"), "done": green("✓"), "error": red("✗")}
        icon = icons.get(event.content, "·")
        print(f"  {icon} {dim(event.content)}")

    elif event.type == "error":
        print(f"  {red('✗')} {red(event.content)}")

    elif event.type == "tokens":
        print(f"  {dim('🔢')} {dim(event.content)}")


async def interactive_mode(config: Config) -> None:
    """交互式 REPL 模式"""
    agent_name = load_agent(config.agent_class, config).name
    print(bold(f"\n⚡ Agent Shell — 交互模式"))
    print(f"   Agent : {cyan(agent_name)}")
    print(f"   {dim('输入任务后按 Enter 执行，输入 :q 退出，:agent <class> 切换 Agent')}\n")

    while True:
        try:
            task = input(bold("You › ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{dim('再见！')}")
            break

        if not task:
            continue

        # 内置命令
        if task in (":q", ":quit", ":exit"):
            print(dim("再见！"))
            break

        if task.startswith(":agent "):
            new_cls = task[7:].strip()
            try:
                load_agent(new_cls, config)   # 验证能加载
                config.agent_class = new_cls
                print(green(f"✓ 已切换 Agent → {new_cls}\n"))
            except Exception as e:
                print(red(f"✗ 切换失败: {e}\n"))
            continue

        if task == ":info":
            a = load_agent(config.agent_class, config)
            print(f"  名称: {cyan(a.name)}\n  描述: {a.description}\n")
            continue

        # 执行任务
        try:
            await run_task(task, config)
        except Exception as e:
            print(red(f"\n✗ 执行出错: {e}\n"))

        print()


# ── CLI 参数解析 ───────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli",
        description="Agent Shell CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python cli.py "分析 GPT Researcher 的架构"
  python cli.py "..." --agent agents.echo_agent.EchoAgent
  python cli.py --interactive
        """,
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="要执行的任务（不填则需要 --interactive）",
    )
    parser.add_argument(
        "--agent", "-a",
        dest="agent_class",
        default=None,
        help="Agent 类路径，如 agents.echo_agent.EchoAgent（覆盖 .env 配置）",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="进入交互式对话模式",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    config = Config.from_env()
    if args.agent_class:
        config.agent_class = args.agent_class

    if args.interactive:
        asyncio.run(interactive_mode(config))

    elif args.task:
        asyncio.run(run_task(args.task, config))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

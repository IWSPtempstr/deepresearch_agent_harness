"""CLI 冒烟入口：经唯一 Harness 入口执行一次研究 Run（T-408 简化）。

旧 --agent deep_research/fusion 选项已随退役引擎删除；本脚本仅触发
run_persistent_query（coreact 唯一路径）。
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

# src 布局：脚本从项目根目录直接运行时，将 src 加入 sys.path 以定位 deepresearch_agent 包
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deepresearch_agent.harness.bootstrap import run_persistent_query


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one research query through the Harness (coreact)")
    parser.add_argument("query", nargs="?", default="急性脑血管病吃什么药？写一份研究报告")
    parser.add_argument("--source-mode", choices=["graphrag", "web", "scholar"], default="graphrag")
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()
    thread_id = args.thread_id or f"cli_{int(time.time())}"
    print(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    result = asyncio.run(
        run_persistent_query(args.query, source_mode=args.source_mode, client_message_id=thread_id)
    )
    print(f"\n[Harness] run_id={result.run_id} status={result.status}")
    print(result.report or "未生成报告")


if __name__ == "__main__":
    main()

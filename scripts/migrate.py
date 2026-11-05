"""T-005：独立迁移入口——生产部署的 schema 演进步骤。

backend 与 worker 容器启动前各跑一次（可能并发，带锁竞争重试）；
CI/runbook 亦可独立调用。不执行任何业务逻辑；成功即 schema 已至 head。

用法：python scripts/migrate.py [--retry N] [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_migrations(*, retries: int = 5, interval: float = 2.0) -> None:
    if retries < 1:
        raise SystemExit("--retry 必须 >= 1")
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [part for part in (str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)) if part not in existing.split(os.pathsep)]
    env["PYTHONPATH"] = os.pathsep.join([*parts, existing]) if existing else os.pathsep.join(parts)
    for attempt in range(1, retries + 1):
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        )
        if completed.returncode == 0:
            print(completed.stdout.strip() or "alembic upgrade head: OK")
            return
        if attempt == retries:
            print(completed.stderr.strip(), file=sys.stderr)
            raise SystemExit(f"迁移失败（已重试 {retries} 次）")
        print(f"迁移撞车/失败（第 {attempt} 次），{interval}s 后重试…", flush=True)
        tail = (completed.stderr or completed.stdout).strip().splitlines()
        if tail:
            print(f"  上次错误尾部: {tail[-1][:200]}", flush=True)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立 schema 迁移步骤（T-005）")
    parser.add_argument("--retry", type=int, default=5, help="锁竞争重试次数（>=1）")
    parser.add_argument("--interval", type=float, default=2.0, help="重试间隔秒")
    args = parser.parse_args()
    run_migrations(retries=args.retry, interval=args.interval)


if __name__ == "__main__":
    main()
